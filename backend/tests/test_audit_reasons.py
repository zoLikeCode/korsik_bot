"""Supplied HR reasons survive privately, including replaced configurations."""

import json
import warnings
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import select
from sqlalchemy.exc import SAWarning
from starlette.requests import Request

from korsik import chat, domain as d, local_ai, operations
from korsik.common import fingerprint, save_entity
from korsik.models import Audit, Entity
from test_domain import hr as hr, profile, req, session as session


def private_reasons(db, action, entity_id):
    events = db.scalars(
        select(Audit).where(Audit.action == action, Audit.entity_id == entity_id)
    ).all()
    values = []
    for event in events:
        assert event.data.get("has_reason") is True
        assert "reason" not in event.data
        private = db.get(Entity, event.data["reason_ref"])
        assert private.kind == "audit_reason" and private.status == "PRIVATE"
        assert private.parent_id == event.id
        assert private.owner_id == event.actor_id
        assert private.data["action"] == action
        assert private.data["entity_id"] == entity_id
        assert private.data["reason"] not in json.dumps(event.data, ensure_ascii=False)
        values.append(private.data["reason"])
    return values


def test_digest_create_and_update_retain_distinct_private_reasons(session, hr):
    payload = {
        "name": "Еженедельный HR отчёт",
        "scope": {"all": True},
        "recipient_user_ids": [hr.id],
        "channels": ["HR_PANEL"],
        "timezone": "Europe/Moscow",
        "weekday": 1,
        "local_time": "09:00",
        "reason": "Утверждено недельное расписание руководителей",
    }
    result = d.digest_schedule_create(payload, session, hr)
    row = session.get(Entity, json.loads(result.body)["id"])
    assert "reason" not in row.data  # Whitelisted schedule settings stay structured.
    updated = {**payload, "local_time": "10:30", "reason": "Перенесено после планёрки"}
    d.digest_schedule_put(row.id, updated, req(row), session, hr)
    assert private_reasons(session, "digest.schedule.create", row.id) == [
        payload["reason"]
    ]
    assert private_reasons(session, "digest.schedule.update", row.id) == [
        updated["reason"]
    ]


def test_retention_replacement_preserves_previous_reason_history(session, hr):
    first = "До согласования компании удаление запрещено"
    second = "Установлено удержание на время проверки"
    initial_etag = operations.knowledge_retention_view(None)["etag"]
    request = Request(
        {"type": "http", "headers": [(b"if-match", initial_etag.encode())]}
    )
    operations.put_knowledge_retention(
        operations.KnowledgeRetentionInput(enabled=False, reason=first),
        request,
        Response(),
        session,
        hr,
    )
    row = session.get(Entity, operations.KNOWLEDGE_RETENTION_ID)
    operations.put_knowledge_retention(
        operations.KnowledgeRetentionInput(enabled=False, hold=True, reason=second),
        req(row),
        Response(),
        session,
        hr,
    )
    assert row.data["reason"] == second
    assert set(
        private_reasons(session, "retention.knowledge_policy_configured", row.id)
    ) == {first, second}


def test_scenario_publish_requires_own_reason_and_keeps_it_private(session, hr):
    scenario = d.entities(session, "scenario")[0]
    result = d.create_scenario_version(scenario.id, {}, session, hr)
    version = session.get(Entity, json.loads(result.body)["id"])
    d.review_scenario(
        version.id, {"reason": "Содержание согласовано ответственным HR"}, session, hr
    )
    previous_head = scenario.data.get("published_version_id")
    for value in (None, "", "  ", "ab", 42, "x" * 2001, " " + "x" * 2000):
        with pytest.raises(HTTPException) as error:
            d.publish_scenario(
                version.id, {} if value is None else {"reason": value}, session, hr
            )
        assert error.value.status_code == 422
        assert version.status == "APPROVED"
        assert scenario.data.get("published_version_id") == previous_head
    reason = "Публикуем для новых сотрудников с понедельника"
    d.publish_scenario(version.id, {"reason": reason}, session, hr)
    assert version.status == "PUBLISHED"
    assert private_reasons(session, "scenario.publish", version.id) == [reason]


def test_binding_approval_retains_supplied_reason_without_transport_send(session, hr):
    employee = profile(session, hr)
    binding = save_entity(
        session,
        "max_binding",
        {
            "episode_id": employee.data["episode_id"],
            "max_user_id": "audit-fixture-max-user",
            "max_chat_id": "audit-fixture-chat",
        },
        parent_id=employee.id,
        status="PENDING_VERIFICATION",
    )
    session.commit()
    reason = "Проверен лично по кадровой карточке и известному телефону"
    chat.approve_employee_binding(
        binding.id, chat.BindingApprove(reason=reason), req(binding), hr, session
    )
    assert binding.status == "ACTIVE"
    assert private_reasons(session, "employee_binding.approve", binding.id) == [reason]


def test_local_ai_profile_create_and_activate_retain_separate_reasons(
    session, hr, monkeypatch
):
    # This synthetic validation fixture tests audit wiring, not model quality.
    artifacts = [
        save_entity(
            session,
            "model_artifact",
            {
                "role": role,
                "fingerprint": role + "-test",
                "deployment_selected": True,
                "quality_evaluation": {
                    "status": "PASS",
                    "pipeline_review_current": True,
                },
            },
            status="INSTALLED",
        )
        for role in ("embedder", "reranker", "generator")
    ]
    monkeypatch.setattr(local_ai, "register_models", lambda db: artifacts)
    monkeypatch.setattr(
        local_ai,
        "get_settings",
        lambda: SimpleNamespace(product_mode="FULL", generation_enabled=True),
    )
    from korsik import retrieval

    def health(url, **kwargs):
        role = (
            "embedder"
            if url.startswith(retrieval.EMBEDDER)
            else "reranker" if url.startswith(retrieval.RERANKER) else "generator"
        )
        assert url.endswith("/healthz") and kwargs["method"] == "GET"
        return {
            "status": "READY",
            "fingerprint": role + "-test",
            "renderer_fingerprint": local_ai.pipeline_identities()[
                "generation_runtime_source_fingerprint"
            ],
        }

    monkeypatch.setattr(retrieval, "_request", health)
    create_reason = "Создан профиль из проверенного локального комплекта"
    result = local_ai.create_profile(
        {"name": "Аудит конфигурации", "reason": create_reason}, session, hr
    )
    row = session.get(Entity, result["id"])
    save_entity(
        session,
        "local_ai_validation",
        {
            "configuration_fingerprint": fingerprint(row.data["configuration"]),
        },
        parent_id=row.id,
        status="PASSED",
    )
    session.commit()
    activate_reason = "Переключение после изолированной проверки оператором"
    local_ai.activate_profile(
        row.id, {"reason": activate_reason}, req(row), session, hr
    )
    assert row.status == "ACTIVE"
    assert private_reasons(session, "local_ai.profile.create", row.id) == [
        create_reason
    ]
    assert private_reasons(session, "local_ai.profile.activate", row.id) == [
        activate_reason
    ]


def test_suspended_local_ai_control_is_a_clean_no_active_profile(session, hr):
    save_entity(session, "local_ai_control", {"active_profile_id": None, "epoch": 3})
    session.commit()
    with warnings.catch_warnings():
        warnings.simplefilter("error", SAWarning)
        assert local_ai.active_profile(session) is None
        assert local_ai.generation_policy(session) is None
    assert local_ai.get_active(session, hr)["configuration_state"] == "SETUP_REQUIRED"
