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
from src.modules.patients.models import GuardianProfile, PatientProfile
from src.shared.domain.enums import UserRole

router = APIRouter(prefix="/group-homes", tags=["group homes"])

def agency(ctx: CurrentAuth) -> uuid.UUID:
    if ctx.agency_id is None or ctx.role == UserRole.SUPER_ADMIN:
        raise ForbiddenError("An agency context is required.")
    return ctx.agency_id

class HomeCreate(BaseModel):
    location_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    patient_ids: list[uuid.UUID] = Field(min_length=1, max_length=4)
    guardian_id: uuid.UUID | None = None
    owner_patient_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def validate_owner(self):
        if len(set(self.patient_ids)) != len(self.patient_ids):
            raise ValueError("A patient can be added only once.")
        if self.guardian_id is None and self.owner_patient_id not in self.patient_ids:
            raise ValueError("Select one resident patient as owner when no guardian is assigned.")
        if self.guardian_id is not None and self.owner_patient_id is not None:
            raise ValueError("A guardian owner replaces a patient owner.")
        return self

class MemberCreate(BaseModel): patient_id: uuid.UUID
class HomeUpdate(BaseModel): name: str | None = Field(default=None, min_length=1, max_length=120); is_active: bool | None = None
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
        result.append({"id": home.id, "location_id": home.location_id, "name": home.name, "capacity": home.capacity, "is_active": home.is_active, "member_count": count or 0})
    return result

@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))])
async def create_home(payload: HomeCreate, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session)]):
    aid = agency(ctx)
    patients = (await session.execute(select(PatientProfile.id).where(PatientProfile.id.in_(payload.patient_ids), PatientProfile.agency_id == aid, PatientProfile.deleted_at.is_(None)))).scalars().all()
    if len(patients) != len(payload.patient_ids): raise ValidationError("All group-home patients must belong to this agency.")
    if payload.guardian_id is not None:
        guardian = (await session.execute(select(GuardianProfile.id).where(GuardianProfile.id == payload.guardian_id, GuardianProfile.agency_id == aid, GuardianProfile.deleted_at.is_(None)))).scalar_one_or_none()
        if guardian is None: raise ValidationError("Guardian was not found in this agency.")
    home = GroupHome(agency_id=aid, location_id=payload.location_id, name=payload.name, capacity=4, guardian_id=payload.guardian_id, owner_patient_id=None if payload.guardian_id else payload.owner_patient_id)
    session.add(home); await session.flush()
    session.add_all([GroupHomeMember(group_home_id=home.id, patient_id=patient_id) for patient_id in payload.patient_ids])
    await session.flush()
    return {"id": home.id, "location_id": home.location_id, "name": home.name, "capacity": 4, "is_active": True, "member_count": len(payload.patient_ids), "guardian_id": home.guardian_id, "owner_patient_id": home.owner_patient_id}

@router.patch("/{home_id}", dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))])
async def update_home(home_id: uuid.UUID, payload: HomeUpdate, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session)]):
    home = (await session.execute(select(GroupHome).where(GroupHome.id == home_id, GroupHome.agency_id == agency(ctx)))).scalar_one_or_none()
    if not home: raise ValidationError("Group home was not found.")
    if payload.name is not None: home.name = payload.name
    if payload.is_active is not None: home.is_active = payload.is_active
    await session.flush(); return {"id": home.id, "name": home.name, "is_active": home.is_active}

@router.get("/{home_id}")
async def get_home(home_id: uuid.UUID, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session)]):
    aid = agency(ctx)
    home = (await session.execute(select(GroupHome).where(GroupHome.id == home_id, GroupHome.agency_id == aid))).scalar_one_or_none()
    if not home: raise ValidationError("Group home was not found.")
    members = (await session.execute(select(GroupHomeMember.patient_id).where(GroupHomeMember.group_home_id == home_id, GroupHomeMember.removed_at.is_(None)))).scalars().all()
    appointments = (await session.execute(select(GroupHomeAppointment).where(GroupHomeAppointment.group_home_id == home_id).order_by(GroupHomeAppointment.scheduled_start.desc()))).scalars().all()
    return {"id": home.id, "location_id": home.location_id, "name": home.name, "capacity": home.capacity, "is_active": home.is_active, "patient_ids": members, "appointments": [{"id": row.id, "service": row.service, "scheduled_start": row.scheduled_start, "scheduled_end": row.scheduled_end, "staff_id": row.staff_id, "status": row.status} for row in appointments]}

@router.post("/{home_id}/members", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))])
async def add_member(home_id: uuid.UUID, payload: MemberCreate, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session)]):
    aid = agency(ctx)
    home = (await session.execute(select(GroupHome).where(GroupHome.id == home_id, GroupHome.agency_id == aid))).scalar_one_or_none()
    patient = (await session.execute(select(PatientProfile).where(PatientProfile.id == payload.patient_id, PatientProfile.agency_id == aid, PatientProfile.deleted_at.is_(None)))).scalar_one_or_none()
    if not home or not patient: raise ValidationError("Group home or patient was not found.")
    count = await session.scalar(select(func.count()).select_from(GroupHomeMember).where(GroupHomeMember.group_home_id == home_id, GroupHomeMember.removed_at.is_(None)))
    if (count or 0) >= 4: raise ValidationError("A group home can have at most four active patients.")
    session.add(GroupHomeMember(group_home_id=home_id, patient_id=payload.patient_id)); await session.flush()
    return {"group_home_id": home_id, "patient_id": payload.patient_id}

@router.delete("/{home_id}/members/{patient_id}", dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))])
async def remove_member(home_id: uuid.UUID, patient_id: uuid.UUID, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session)]):
    member = (await session.execute(select(GroupHomeMember).join(GroupHome).where(GroupHomeMember.group_home_id == home_id, GroupHomeMember.patient_id == patient_id, GroupHome.agency_id == agency(ctx), GroupHomeMember.removed_at.is_(None)))).scalar_one_or_none()
    if not member: raise ValidationError("Active group-home member was not found.")
    member.removed_at = datetime.now().astimezone(); await session.flush()

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
    session.add_all([GroupHomeAppointmentPatient(group_appointment_id=appt.id, patient_id=pid) for pid in participants]); await session.flush()
    return {"id": appt.id, "group_home_id": home_id, "service": appt.service, "scheduled_start": appt.scheduled_start, "scheduled_end": appt.scheduled_end, "patient_ids": participants}

@router.post("/{home_id}/appointments/{appointment_id}/cancel", dependencies=[Depends(require_role(UserRole.AGENCY_ADMIN))])
async def cancel_group_appointment(home_id: uuid.UUID, appointment_id: uuid.UUID, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session)]):
    row = (await session.execute(select(GroupHomeAppointment).where(GroupHomeAppointment.id == appointment_id, GroupHomeAppointment.group_home_id == home_id, GroupHomeAppointment.agency_id == agency(ctx)))).scalar_one_or_none()
    if not row: raise ValidationError("Group appointment was not found.")
    row.status = "CANCELLED"; await session.flush(); return {"id": row.id, "status": row.status}
