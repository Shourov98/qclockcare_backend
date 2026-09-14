"""ORM models for agency-scoped conversations and messages."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.domain.base_entity import Base, IdMixin, TimestampedMixin


class Conversation(IdMixin, TimestampedMixin, Base):
    __tablename__ = "conversations"

    agency_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("agencies.id", ondelete="CASCADE"), nullable=False, index=True)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message_preview: Mapped[str | None] = mapped_column(String(255), nullable=True)
    participants: Mapped[list[ConversationParticipant]] = relationship(back_populates="conversation", cascade="all, delete-orphan")
    messages: Mapped[list[ConversationMessage]] = relationship(back_populates="conversation", cascade="all, delete-orphan", order_by="ConversationMessage.created_at")

    __table_args__ = (Index("idx_conversations_agency_last_message", "agency_id", "last_message_at"),)


class ConversationParticipant(IdMixin, TimestampedMixin, Base):
    __tablename__ = "conversation_participants"

    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    conversation: Mapped[Conversation] = relationship(back_populates="participants")

    __table_args__ = (UniqueConstraint("conversation_id", "user_id", name="uq_conversation_participant"), Index("idx_conversation_participants_user", "user_id"))


class ConversationMessage(IdMixin, TimestampedMixin, Base):
    __tablename__ = "conversation_messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (Index("idx_conversation_messages_conversation_created", "conversation_id", "created_at"),)
