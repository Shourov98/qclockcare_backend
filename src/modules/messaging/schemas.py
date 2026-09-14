from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConversationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    participant_user_ids: list[UUID] = Field(min_length=1, max_length=20)
    title: str | None = Field(default=None, max_length=255)
    opening_message: str = Field(min_length=1, max_length=10000)

    @field_validator("opening_message")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class MessageCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=10000)

    @field_validator("body")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class ConversationParticipantResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    user_id: UUID
    full_name: str | None = None
    role: str | None = None
    last_read_at: datetime | None = None


class ConversationMessageResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    sender_user_id: UUID
    sender_name: str | None = None
    body: str
    created_at: datetime


class ConversationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    title: str | None = None
    created_by_user_id: UUID
    last_message_at: datetime | None = None
    last_message_preview: str | None = None
    unread_count: int = 0
    participants: list[ConversationParticipantResponse] = Field(default_factory=list)


class ConversationDetailResponse(ConversationResponse):
    messages: list[ConversationMessageResponse] = Field(default_factory=list)

