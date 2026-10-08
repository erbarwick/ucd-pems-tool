from __future__ import annotations

import argparse
import datetime as dt
import os
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
    event,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///db.sqlite3")

eng = create_engine(DATABASE_URL)

class Base(DeclarativeBase):
    pass

class FieldElement(Base):
    __tablename__ = "field_elements"
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    element_type: Mapped[str] = mapped_column(String(16))

    fwy: Mapped[str] = mapped_column(String(16))
    dist: Mapped[int] =