"""Tests for app.repositories.order — focused on allocate_order_seq.

These tests exercise the atomic upsert introduced to replace the
COUNT(*)-based TOCTOU-prone order number generation.  Each test runs
inside the conftest SAVEPOINT-style transaction and rolls back on
teardown, so counter state never leaks between tests.
"""

from __future__ import annotations

from datetime import date

from app.repositories.order import allocate_order_seq
from sqlalchemy.orm import Session


def test_allocate_order_seq_first_call_returns_1(db_session: Session) -> None:
    seq = allocate_order_seq(db_session, date(2026, 1, 15))
    assert seq == 1


def test_allocate_order_seq_increments_per_day(db_session: Session) -> None:
    d = date(2026, 1, 16)
    assert allocate_order_seq(db_session, d) == 1
    assert allocate_order_seq(db_session, d) == 2
    assert allocate_order_seq(db_session, d) == 3


def test_allocate_order_seq_independent_across_dates(db_session: Session) -> None:
    assert allocate_order_seq(db_session, date(2026, 1, 17)) == 1
    assert allocate_order_seq(db_session, date(2026, 1, 18)) == 1
