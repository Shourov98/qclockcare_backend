from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated
from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.exceptions import ForbiddenError, ValidationError
from src.core.database import get_session
from src.modules.group_homes.models import GroupHome, GroupHomeAppointment, GroupHomeAppointmentPatient, GroupHomeMember
from src.modules.identity.dependencies import CurrentAuth, require_role
from src.modules.patients.models import PatientProfile
from src.shared.domain.enums import UserRole

router = APIRouter(prefix="/group-homes", tags=["group homes"])

def agency(ctx: CurrentAuth) -> uuid.UUID:
    if ctx.agency_id is None or ctx.role == UserRole.SUPER_ADMIN:
        raise ForbiddenError("An agency context is required.")
    return ctx.agency_id

class HomeCreate(BaseModel):
    location_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)

class MemberCreate(BaseModel): patient_id: uuid.UUID
class GroupAppointmentCreate(BaseModel):
    service: str = Field(min_length=1, max_length=255)
    scheduled_start: datetime
    scheduled_end: datetime
    staff_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=4000)
    patient_ids: list[uuid.UUID] | None = None
    @model_validator(mode="after")
    def window(self):
        if self.scheduled_end <= self.scheduled_start: raise ValueError("scheduled_end must be after scheduled_start")
        return self

@router.get("")
async def list_homes(ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session)]):
    aid = agency(ctx)
    homes = (await session.execute(select(GroupHome).where(GroupHome.agency_id == aid).order_by(GroupHome.name))).scalars().all()
    result = []
    for home in homes:
        count = await session.scalar(select(func.count()).select_from(GroupHomeMember).where(GroupHomeMember.group_home_id == home.id, GroupHomeMember.removed_at.is_(None)))
        result.append({"id": home.id, "location_id": home.location_id, "name": home.name, "capacity": home.capacity, "member_count": count or 0})
    return result

@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))])
async def create_home(payload: HomeCreate, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session)]):
    aid = agency(ctx)
    home = GroupHome(agency_id=aid, location_id=payload.location_id, name=payload.name, capacity=4)
    session.add(home); await session.commit(); await session.refresh(home)
    return {"id": home.id, "location_id": home.location_id, "name": home.name, "capacity": 4, "member_count": 0}

@router.get("/{home_id}")
async def get_home(home_id: uuid.UUID, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session)]):
    aid = agency(ctx)
    home = (await session.execute(select(GroupHome).where(GroupHome.id == home_id, GroupHome.agency_id == aid))).scalar_one_or_none()
    if not home: raise ValidationError("Group home was not found.")
    members = (await session.execute(select(GroupHomeMember.patient_id).where(GroupHomeMember.group_home_id == home_id, GroupHomeMember.removed_at.is_(None)))).scalars().all()
    appointments = (await session.execute(select(GroupHomeAppointment).where(GroupHomeAppointment.group_home_id == home_id).order_by(GroupHomeAppointment.scheduled_start.desc()))).scalars().all()
    return {"id": home.id, "location_id": home.location_id, "name": home.name, "capacity": home.capacity, "patient_ids": members, "appointments": [{"id": row.id, "service": row.service, "scheduled_start": row.scheduled_start, "scheduled_end": row.scheduled_end, "staff_id": row.staff_id} for row in appointments]}

@router.post("/{home_id}/members", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))])
async def add_member(home_id: uuid.UUID, payload: MemberCreate, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session)]):
    aid = agency(ctx)
    home = (await session.execute(select(GroupHome).where(GroupHome.id == home_id, GroupHome.agency_id == aid))).scalar_one_or_none()
    patient = (await session.execute(select(PatientProfile).where(PatientProfile.id == payload.patient_id, PatientProfile.agency_id == aid, PatientProfile.deleted_at.is_(None)))).scalar_one_or_none()
    if not home or not patient: raise ValidationError("Group home or patient was not found.")
    count = await session.scalar(select(func.count()).select_from(GroupHomeMember).where(GroupHomeMember.group_home_id == home_id, GroupHomeMember.removed_at.is_(None)))
    if (count or 0) >= 4: raise ValidationError("A group home can have at most four active patients.")
    session.add(GroupHomeMember(group_home_id=home_id, patient_id=payload.patient_id)); await session.commit()
    return {"group_home_id": home_id, "patient_id": payload.patient_id}

@router.post("/{home_id}/appointments", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))])
async def create_group_appointment(home_id: uuid.UUID, payload: GroupAppointmentCreate, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session)]):
    aid = agency(ctx)
    home = (await session.execute(select(GroupHome).where(GroupHome.id == home_id, GroupHome.agency_id == aid))).scalar_one_or_none()
    if not home: raise ValidationError("Group home was not found.")
    members = (await session.execute(select(GroupHomeMember.patient_id).where(GroupHomeMember.group_home_id == home_id, GroupHomeMember.removed_at.is_(None)))).scalars().all()
    participants = payload.patient_ids or list(members)
    if not participants or not set(participants).issubset(set(members)): raise ValidationError("Participants must be active patients of this group home.")
    appt = GroupHomeAppointment(agency_id=aid, group_home_id=home_id, staff_id=payload.staff_id, service=payload.service, scheduled_start=payload.scheduled_start, scheduled_end=payload.scheduled_end, notes=payload.notes)
    session.add(appt); await session.flush()
    session.add_all([GroupHomeAppointmentPatient(group_appointment_id=appt.id, patient_id=pid) for pid in participants]); await session.commit()
    return {"id": appt.id, "group_home_id": home_id, "service": appt.service, "scheduled_start": appt.scheduled_start, "scheduled_end": appt.scheduled_end, "patient_ids": participants}
