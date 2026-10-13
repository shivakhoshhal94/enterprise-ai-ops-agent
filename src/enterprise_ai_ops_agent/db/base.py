"""Shared registry for SQLAlchemy table definitions."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
