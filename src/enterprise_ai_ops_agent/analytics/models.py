"""Small public contracts without parser or database-driver objects."""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


class SQLValidationError(ValueError):
    """SQL rejected before opening a database connection."""


class AnalyticalQueryError(RuntimeError):
    """Credential-safe database execution failure."""


class AnalyticalQueryTimeout(AnalyticalQueryError):
    """PostgreSQL canceled an analytical statement."""


@dataclass(frozen=True)
class ValidatedQuery:
    normalized_sql: str
    referenced_tables: tuple[str, ...]


class AnalyticalResult(BaseModel):
    columns: list[str]
    rows: list[list[Any]]
    returned_row_count: int
    truncated: bool
