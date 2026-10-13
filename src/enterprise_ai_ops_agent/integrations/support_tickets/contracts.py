"""Application-owned wire contract, deliberately independent of the server implementation."""

from datetime import datetime
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_TICKETS = 5
OrderId = Annotated[str, Field(strict=True, min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")]
Timestamp = Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")]


class SupportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    order_id: OrderId


class SupportTicket(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    ticket_id: str = Field(pattern=r"^DEMO-SUP-\d{3}$")
    order_id: OrderId
    status: Literal["open", "in_review", "closed"]
    category: Literal["payment_question", "delivery_question", "internal_review"]
    priority: Literal["low", "medium", "high"]
    summary: str = Field(min_length=1, max_length=500)
    created_at: Timestamp
    updated_at: Timestamp

    @field_validator("created_at", "updated_at")
    @classmethod
    def valid_timestamp(cls, value: str) -> str:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        return value

    @field_validator("summary")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Summary must be nonblank.")
        return value

    @model_validator(mode="after")
    def chronological(self):
        if self.updated_at < self.created_at:
            raise ValueError("Ticket update predates creation.")
        return self


class SupportContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    order_id: OrderId
    tickets: list[SupportTicket] = Field(max_length=MAX_TICKETS)
    truncated: bool
    data_origin: Literal["synthetic_demo"] = "synthetic_demo"

    @model_validator(mode="after")
    def consistent(self):
        if any(ticket.order_id != self.order_id for ticket in self.tickets):
            raise ValueError("Ticket order does not match the request.")
        if len({ticket.ticket_id for ticket in self.tickets}) != len(self.tickets):
            raise ValueError("Duplicate ticket identities.")
        expected = sorted(sorted(self.tickets, key=lambda ticket: ticket.ticket_id),
                          key=lambda ticket: ticket.updated_at, reverse=True)
        if self.tickets != expected:
            raise ValueError("Unexpected ticket ordering.")
        if self.truncated and len(self.tickets) != MAX_TICKETS:
            raise ValueError("Truncation requires a full bounded result.")
        return self

