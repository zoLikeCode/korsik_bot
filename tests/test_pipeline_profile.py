"""An installed weight file alone does not authorize a changed prompt/runtime."""

from copy import deepcopy
from sqlalchemy.orm import sessionmaker
from korsik import local_ai
from korsik.common import save_entity
from korsik.db import make_engine, now
from korsik.models import Base
from korsik.rag_identity import pipeline_identities


def test_active_profile_pins_pipeline_and_actual_generation_runtime():
    engine = make_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessionmaker(engine, expire_on_commit=False)() as db:
        artifact = save_entity(
            db, "model_artifact", {"fingerprint": "weights"}, id="generator"
        )
        profile = save_entity(
            db,
            "local_ai_profile",
            {
                "configuration": {
                    "models": {"generator": artifact.id},
                    "generation_policy": "LOCAL_GENERATION",
                    "pipeline_identities": pipeline_identities(),
                }
            },
            id="profile",
            status="ACTIVE",
        )
        save_entity(db, "local_ai_control", {"active_profile_id": profile.id})
        states = {
            "generator": {
                "ready": True,
                "checked_at": now().isoformat(),
                "detail": {
                    "fingerprint": "weights",
                    "renderer_fingerprint": pipeline_identities()[
                        "generation_runtime_source_fingerprint"
                    ],
                },
            }
        }
        assert local_ai.active_identity_matches(db, states)
        assert local_ai.generation_policy(db) == "LOCAL_GENERATION"
        changed = deepcopy(states)
        changed["generator"]["detail"]["renderer_fingerprint"] = "different-runtime"
        assert not local_ai.active_identity_matches(db, changed)
        config = deepcopy(profile.data["configuration"])
        config["pipeline_identities"]["chunker_fingerprint"] = "different-chunker"
        profile.data = {**profile.data, "configuration": config}
        assert not local_ai.active_identity_matches(db, states)
        assert local_ai.generation_policy(db) is None
        profile.data = {
            **profile.data,
            "configuration": {
                "models": {"generator": artifact.id},
                "generation_policy": "LOCAL_GENERATION",
            },
        }
        assert not local_ai.active_identity_matches(db, states)
        assert local_ai.generation_policy(db) is None
    engine.dispose()
