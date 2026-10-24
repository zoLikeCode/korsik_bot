import pytest
from fastapi import HTTPException
from sqlalchemy.orm import sessionmaker
from korsik.auth import Actor
from korsik.common import save_entity
from korsik.db import make_engine
from korsik.models import Base, Entity
from korsik.knowledge_warnings import document_warnings


def test_warnings_are_current_scope_filtered_and_advisory():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(engine, expire_on_commit=False)() as db:
        current = save_entity(
            db,
            "document",
            {"title": "Политика качества", "scope": {"site_ids": ["A"]}},
            status="ACTIVE",
        )
        same = save_entity(
            db,
            "document",
            {"title": "Политика качества", "scope": {"site_ids": ["A"]}},
            status="ACTIVE",
        )
        hidden = save_entity(
            db,
            "document",
            {"title": "Политика качества", "scope": {"site_ids": ["B"]}},
            status="ACTIVE",
        )
        for doc in (current, same, hidden):
            rev = save_entity(
                db,
                "document_revision",
                {"sha256": "same-source", "effective_from": "2026-01-01T00:00:00Z"},
                parent_id=doc.id,
                status="PUBLISHED",
            )
            doc.data = {**doc.data, "active_revision_id": rev.id}
        actor = Actor("hr", "HR", "HR_SPECIALIST", {"site_ids": ["A"]})
        report = document_warnings(current.id, actor, db)
        assert report["advisory_only"]
        assert {row["document_id"] for row in report["items"]} == {same.id}
        assert {row["code"] for row in report["items"]} == {
            "EXACT_FILE_DUPLICATE",
            "SIMILAR_TITLE",
            "POTENTIAL_APPLICABILITY_CONFLICT",
        }
        revision = db.get(Entity, same.data["active_revision_id"])
        revision.data = {
            **revision.data,
            "effective_from": "2020-01-01T00:00:00Z",
            "effective_to": "2021-01-01T00:00:00Z",
        }
        assert "POTENTIAL_APPLICABILITY_CONFLICT" not in {
            row["code"] for row in document_warnings(current.id, actor, db)["items"]
        }
        with pytest.raises(HTTPException) as error:
            document_warnings(hidden.id, actor, db)
        assert error.value.status_code == 404
        actor.role = "EMPLOYEE"
        with pytest.raises(HTTPException) as employee_error:
            document_warnings(current.id, actor, db)
        assert employee_error.value.status_code == 403
    engine.dispose()
