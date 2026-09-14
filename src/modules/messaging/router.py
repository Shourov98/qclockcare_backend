from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.identity.dependencies import CurrentAuth, get_session_with_auth
from src.modules.messaging import service
from src.modules.messaging.schemas import (
    ConversationCreateRequest,
    ConversationDetailResponse,
    ConversationResponse,
    MessageCreateRequest,
)

router = APIRouter(prefix="/messages", tags=["messaging"])

@router.get("/conversations", response_model=list[ConversationResponse])
async def list_conversations(ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session_with_auth)]):
    rows = await service.list_conversations(session, ctx=ctx)
    return [await service.serialize_conversation(session, row, ctx.user_id) for row in rows]

@router.post("/conversations", response_model=ConversationDetailResponse, status_code=status.HTTP_201_CREATED)
async def create_conversation(payload: ConversationCreateRequest, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session_with_auth)]):
    conversation = await service.create_conversation(session, ctx=ctx, participant_user_ids=payload.participant_user_ids, title=payload.title, opening_message=payload.opening_message)
    await session.commit()
    return await service.serialize_conversation(session, conversation, ctx.user_id, details=True)

@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
async def get_conversation(conversation_id: uuid.UUID, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session_with_auth)]):
    conversation = await service._load_visible(session, ctx=ctx, conversation_id=conversation_id, details=True)
    return await service.serialize_conversation(session, conversation, ctx.user_id, details=True)

@router.post("/conversations/{conversation_id}/messages", response_model=ConversationDetailResponse)
async def send_message(conversation_id: uuid.UUID, payload: MessageCreateRequest, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session_with_auth)]):
    conversation = await service.send_message(session, ctx=ctx, conversation_id=conversation_id, body=payload.body)
    await session.commit()
    return await service.serialize_conversation(session, conversation, ctx.user_id, details=True)

@router.patch("/conversations/{conversation_id}/read", response_model=ConversationResponse)
async def mark_conversation_read(conversation_id: uuid.UUID, ctx: CurrentAuth, session: Annotated[AsyncSession, Depends(get_session_with_auth)]):
    conversation = await service.mark_read(session, ctx=ctx, conversation_id=conversation_id)
    await session.commit()
    return await service.serialize_conversation(session, conversation, ctx.user_id)
