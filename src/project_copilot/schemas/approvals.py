"""Client contracts for scoped capabilities and explicit proposal decisions."""
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from project_copilot.schemas.tickets import TicketCreate


class ContextCredentials(BaseModel):
    context_id: UUID
    context_token: str
    expires_at: AwareDatetime


class ActionRead(BaseModel):
    model_config = ConfigDict(frozen=True)
    action_id: UUID
    context_id: UUID
    status: Literal['pending', 'completed', 'cancelled', 'expired']
    payload: TicketCreate
    payload_fingerprint: str
    created_at: AwareDatetime
    expires_at: AwareDatetime
    ticket_id: UUID | None


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    payload_fingerprint: str = Field(pattern=r'^[a-f0-9]{64}$')
