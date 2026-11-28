"""DocuMind Sign schema guarantees enforced by the database (design §3, §9.1)."""

# ruff: noqa: F811

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError

from tests.integration.conftest import Api
from tests.integration.test_infrastructure import (  # noqa: F401 - fixtures
    iapi,
    owner_org,
)
from tests.integration.test_processing_pipeline import sql

pytestmark = pytest.mark.integration


def _agreement(api: Api, w: str) -> uuid.UUID:
    agreement = uuid.uuid4()
    sql(
        api,
        "INSERT INTO agreements (id, workspace_id, title) VALUES (:a, :w, 'NDA')",
        a=agreement,
        w=uuid.UUID(w),
    )
    return agreement


def test_events_are_append_only(iapi: Api) -> None:
    w, _, _ = owner_org(iapi)
    agreement = _agreement(iapi, w)
    sql(
        iapi,
        "INSERT INTO agreement_events (id, agreement_id, workspace_id, seq, type, actor_type, "
        "occurred_at, hash) VALUES (:i, :a, :w, 1, 'agreement.created', 'system', now(), :h)",
        i=uuid.uuid4(),
        a=agreement,
        w=uuid.UUID(w),
        h="a" * 64,
    )
    with pytest.raises(DBAPIError, match="append-only"):
        sql(
            iapi,
            "UPDATE agreement_events SET type = 'tampered' WHERE agreement_id = :a",
            a=agreement,
        )
    # Deletion paths (draft or workspace deletion) still work through the cascade.
    sql(iapi, "DELETE FROM agreements WHERE id = :a", a=agreement)
    assert (
        sql(
            iapi, "SELECT count(*) AS n FROM agreement_events WHERE agreement_id = :a", a=agreement
        )[0].n
        == 0
    )


def test_rows_cannot_point_into_another_workspace(iapi: Api) -> None:
    w1, _, _ = owner_org(iapi, "One")
    w2, _, _ = owner_org(iapi, "Two")
    agreement = _agreement(iapi, w1)
    with pytest.raises(IntegrityError):  # composite (agreement_id, workspace_id) FK
        sql(
            iapi,
            "INSERT INTO agreement_recipients (id, agreement_id, workspace_id, name, email) "
            "VALUES (:i, :a, :w, 'Mallory', 'm@example.com')",
            i=uuid.uuid4(),
            a=agreement,
            w=uuid.UUID(w2),
        )


def test_a_field_cannot_belong_to_another_agreements_recipient(iapi: Api) -> None:
    w, _, _ = owner_org(iapi)
    a1, a2 = _agreement(iapi, w), _agreement(iapi, w)
    recipient = uuid.uuid4()
    sql(
        iapi,
        "INSERT INTO agreement_recipients (id, agreement_id, workspace_id, name, email) "
        "VALUES (:i, :a, :w, 'Ann', 'ann@example.com')",
        i=recipient,
        a=a1,
        w=uuid.UUID(w),
    )
    with pytest.raises(IntegrityError):  # (recipient_id, agreement_id, workspace_id) FK
        sql(
            iapi,
            "INSERT INTO agreement_fields (id, agreement_id, workspace_id, recipient_id, type, "
            "page_index, x, y, width, height, page_width, page_height) VALUES "
            "(:i, :a, :w, :r, 'signature', 0, 10, 10, 100, 30, 612, 792)",
            i=uuid.uuid4(),
            a=a2,
            w=uuid.UUID(w),
            r=recipient,
        )
