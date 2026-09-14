from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.core.exceptions import ForbiddenError, NotFoundError
from src.modules.appointments.models import Appointment
from src.modules.identity.dependencies import AuthContext
from src.modules.identity.models import User, UserRoleAssignment
from src.modules.messaging.models import Conversation, ConversationMessage, ConversationParticipant
from src.modules.notifications.service import dispatch_notification
from src.modules.patients.models import GuardianProfile, PatientGuardianRelationship, PatientProfile
from src.modules.staff.models import StaffProfile
from src.shared.domain.enums import NotificationType, UserRole


async def _role_for_user(session: AsyncSession, agency_id: uuid.UUID, user_id: uuid.UUID) -> UserRole | None:
    return (await session.execute(select(UserRoleAssignment.role).where(UserRoleAssignment.agency_id == agency_id, UserRoleAssignment.user_id == user_id).limit(1))).scalar_one_or_none()


async def _patient_id(session: AsyncSession, agency_id: uuid.UUID, user_id: uuid.UUID) -> uuid.UUID | None:
    return (await session.execute(select(PatientProfile.id).where(PatientProfile.agency_id == agency_id, PatientProfile.user_id == user_id, PatientProfile.deleted_at.is_(None)))).scalar_one_or_none()


async def _guardian_patient_ids(session: AsyncSession, agency_id: uuid.UUID, user_id: uuid.UUID) -> set[uuid.UUID]:
    guardian_id = (await session.execute(select(GuardianProfile.id).where(GuardianProfile.agency_id == agency_id, GuardianProfile.user_id == user_id, GuardianProfile.deleted_at.is_(None)))).scalar_one_or_none()
    if guardian_id is None:
        return set()
    now = datetime.now(UTC).date()
    rows = (await session.execute(select(PatientGuardianRelationship.patient_id).where(PatientGuardianRelationship.guardian_id == guardian_id, PatientGuardianRelationship.valid_from <= now, (PatientGuardianRelationship.valid_until.is_(None)) | (PatientGuardianRelationship.valid_until >= now)))).scalars().all()
    return set(rows)


async def _staff_patient_ids(session: AsyncSession, agency_id: uuid.UUID, user_id: uuid.UUID) -> set[uuid.UUID]:
    staff_id = (await session.execute(select(StaffProfile.id).where(StaffProfile.agency_id == agency_id, StaffProfile.user_id == user_id))).scalar_one_or_none()
    if staff_id is None:
        return set()
    return set((await session.execute(select(Appointment.patient_id).where(Appointment.agency_id == agency_id, Appointment.staff_id == staff_id))).scalars().all())


async def _may_connect(session: AsyncSession, *, agency_id: uuid.UUID, sender_id: uuid.UUID, sender_role: UserRole, target_id: uuid.UUID, target_role: UserRole) -> bool:
    if sender_id == target_id or UserRole.AGENCY_ADMIN in {sender_role, target_role}:
        return True
    if sender_role == UserRole.STAFF and target_role == UserRole.STAFF:
        return True
    sender_patients = await (_staff_patient_ids(session, agency_id, sender_id) if sender_role == UserRole.STAFF else _guardian_patient_ids(session, agency_id, sender_id) if sender_role == UserRole.GUARDIAN else {_patient_id(session, agency_id, sender_id)} if sender_role == UserRole.PATIENT else set())
    target_patients = await (_staff_patient_ids(session, agency_id, target_id) if target_role == UserRole.STAFF else _guardian_patient_ids(session, agency_id, target_id) if target_role == UserRole.GUARDIAN else {_patient_id(session, agency_id, target_id)} if target_role == UserRole.PATIENT else set())
    return bool(sender_patients - {None} & target_patients - {None})


async def _load_visible(session: AsyncSession, *, ctx: AuthContext, conversation_id: uuid.UUID, details: bool = False) -> Conversation:
    options = [selectinload(Conversation.participants)]
    if details:
        options.append(selectinload(Conversation.messages))
    conversation = (await session.execute(select(Conversation).join(ConversationParticipant).where(Conversation.id == conversation_id, Conversation.agency_id == ctx.agency_id, ConversationParticipant.user_id == ctx.user_id).options(*options))).scalar_one_or_none()
    if conversation is None:
        raise NotFoundError("Conversation not found.")
    return conversation


async def create_conversation(session: AsyncSession, *, ctx: AuthContext, participant_user_ids: list[uuid.UUID], title: str | None, opening_message: str) -> Conversation:
    if ctx.agency_id is None:
        raise ForbiddenError("Messaging requires an agency account.")
    recipients = set(participant_user_ids) - {ctx.user_id}
    if not recipients:
        raise ForbiddenError("Choose at least one other participant.")
    for target_id in recipients:
        target_role = await _role_for_user(session, ctx.agency_id, target_id)
        if target_role is None or not await _may_connect(session, agency_id=ctx.agency_id, sender_id=ctx.user_id, sender_role=ctx.role, target_id=target_id, target_role=target_role):
            raise ForbiddenError("You are not allowed to message one or more selected users.")
    now = datetime.now(UTC)
    conversation = Conversation(agency_id=ctx.agency_id, created_by_user_id=ctx.user_id, title=title.strip() if title else None, last_message_at=now, last_message_preview=opening_message[:255])
    session.add(conversation)
    await session.flush()
    session.add_all([ConversationParticipant(conversation_id=conversation.id, user_id=user_id, last_read_at=now if user_id == ctx.user_id else None) for user_id in recipients | {ctx.user_id}])
    message = ConversationMessage(conversation_id=conversation.id, sender_user_id=ctx.user_id, body=opening_message)
    session.add(message)
    await session.flush()
    await _notify_recipients(session, conversation=conversation, message=message, sender_id=ctx.user_id)
    return await _load_visible(session, ctx=ctx, conversation_id=conversation.id, details=True)


async def list_conversations(session: AsyncSession, *, ctx: AuthContext) -> list[Conversation]:
    return list((await session.execute(select(Conversation).join(ConversationParticipant).where(Conversation.agency_id == ctx.agency_id, ConversationParticipant.user_id == ctx.user_id).options(selectinload(Conversation.participants)).order_by(Conversation.last_message_at.desc().nullslast()))).scalars().unique().all())


async def send_message(session: AsyncSession, *, ctx: AuthContext, conversation_id: uuid.UUID, body: str) -> Conversation:
    conversation = await _load_visible(session, ctx=ctx, conversation_id=conversation_id, details=False)
    now = datetime.now(UTC)
    message = ConversationMessage(conversation_id=conversation.id, sender_user_id=ctx.user_id, body=body)
    session.add(message)
    conversation.last_message_at = now
    conversation.last_message_preview = body[:255]
    participant = next(item for item in conversation.participants if item.user_id == ctx.user_id)
    participant.last_read_at = now
    await session.flush()
    await _notify_recipients(session, conversation=conversation, message=message, sender_id=ctx.user_id)
    return await _load_visible(session, ctx=ctx, conversation_id=conversation_id, details=True)


async def mark_read(session: AsyncSession, *, ctx: AuthContext, conversation_id: uuid.UUID) -> Conversation:
    conversation = await _load_visible(session, ctx=ctx, conversation_id=conversation_id)
    next(item for item in conversation.participants if item.user_id == ctx.user_id).last_read_at = datetime.now(UTC)
    await session.flush()
    return conversation


async def _notify_recipients(session: AsyncSession, *, conversation: Conversation, message: ConversationMessage, sender_id: uuid.UUID) -> None:
    sender = (await session.execute(select(User).where(User.id == sender_id))).scalar_one_or_none()
    sender_name = sender.full_name if sender else "A care-team member"
    for participant in conversation.participants:
        if participant.user_id == sender_id:
            continue
        await dispatch_notification(
            session,
            agency_id=conversation.agency_id,
            recipient_user_id=participant.user_id,
            type=NotificationType.GENERIC,
            title=f"New message from {sender_name}",
            body=message.body[:500],
            metadata={"entity_id": str(message.id), "conversation_id": str(conversation.id), "kind": "MESSAGE"},
        )


async def serialize_conversation(session: AsyncSession, conversation: Conversation, viewer_id: uuid.UUID, details: bool = False) -> dict:
    user_ids = [participant.user_id for participant in conversation.participants]
    users = {user.id: user for user in (await session.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()}
    roles = {row.user_id: row.role.value for row in (await session.execute(select(UserRoleAssignment).where(UserRoleAssignment.user_id.in_(user_ids), UserRoleAssignment.agency_id == conversation.agency_id))).scalars().all()}
    viewer = next(participant for participant in conversation.participants if participant.user_id == viewer_id)
    unread_count = 0
    if details:
        unread_count = sum(1 for message in conversation.messages if message.sender_user_id != viewer_id and (viewer.last_read_at is None or message.created_at > viewer.last_read_at))
    payload = {"id": conversation.id, "title": conversation.title, "created_by_user_id": conversation.created_by_user_id, "last_message_at": conversation.last_message_at, "last_message_preview": conversation.last_message_preview, "unread_count": unread_count, "participants": [{"user_id": participant.user_id, "full_name": users.get(participant.user_id).full_name if users.get(participant.user_id) else None, "role": roles.get(participant.user_id), "last_read_at": participant.last_read_at} for participant in conversation.participants]}
    if details:
        payload["messages"] = [{"id": message.id, "sender_user_id": message.sender_user_id, "sender_name": users.get(message.sender_user_id).full_name if users.get(message.sender_user_id) else None, "body": message.body, "created_at": message.created_at} for message in conversation.messages]
    return payload
