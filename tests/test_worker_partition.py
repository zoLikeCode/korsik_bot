"""Slow operator validation cannot occupy the live MAX/business executor."""

from datetime import timedelta

from sqlalchemy.orm import sessionmaker

from korsik import worker
from korsik.db import aware, make_engine, now
from korsik.models import Base, Job


def test_validation_and_answers_do_not_block_business_claims(monkeypatch):
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(engine, expire_on_commit=False)() as db:
        due = now() - timedelta(minutes=1)
        for index, kind in enumerate(("LOCAL_AI_VALIDATE", "ANSWER", "MAX_UPDATE")):
            db.add(
                Job(
                    id=kind,
                    kind=kind,
                    payload={},
                    unique_key=kind,
                    run_at=due + timedelta(seconds=index),
                )
            )
        db.commit()
        monkeypatch.delenv("KORSIK_WORKER_KIND", raising=False)
        assert worker.claim_job(db)[0] == "MAX_UPDATE"
        assert worker.claim_job(db) is None
        assert worker.claim_job(db, ingest=True)[0] == "LOCAL_AI_VALIDATE"
        assert (
            245
            <= (
                aware(db.get(Job, "LOCAL_AI_VALIDATE").lease_until) - now()
            ).total_seconds()
            <= 250
        )
        monkeypatch.setenv("KORSIK_WORKER_KIND", "answer")
        assert worker.claim_job(db)[0] == "ANSWER"
    engine.dispose()
