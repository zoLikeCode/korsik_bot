"""Explicit reporting windows, denominator semantics and private audit reasons."""

import json
import csv
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import select
from starlette.requests import Request
from korsik import domain as d, auth_admin
from korsik.common import save_entity
from korsik.models import Entity, Job, User, Audit
from korsik.auth import audit_events
from korsik.auth import Actor
from test_domain import session as session, hr as hr, profile


def test_reporting_local_dates_and_strict_window_validation():
    filters = {
        "period_from": "2026-10-23",
        "period_to": "2026-10-23",
        "timezone": "Asia/Yekaterinburg",
    }
    window = d.reporting_window(filters)
    assert window["period_from"] == "2026-10-23T00:00:00+05:00"
    assert window["period_to"] == "2026-10-24T00:00:00+05:00"
    assert not d.in_report_period("2026-10-22T18:59:59+00:00", filters)
    assert d.in_report_period("2026-10-22T19:00:00+00:00", filters)
    assert d.in_report_period("2026-10-23T18:59:59+00:00", filters)
    assert not d.in_report_period("2026-10-23T19:00:00+00:00", filters)
    for invalid in (
        {"timezone": "fake/zone"},
        {"period_from": "2026-10-24", "period_to": "2026-10-22"},
        {"period_to": "junk"},
    ):
        with pytest.raises(HTTPException) as error:
            d.reporting_window(invalid)
        assert error.value.status_code == 422


def test_exact_ten_survey_denominators_missing_and_numeric_only_valid(session, hr):
    employee = profile(session, hr)
    statuses = (
        ["COMPLETED"] * 4 + ["IN_PROGRESS"] * 2 + ["DECLINED"] + ["IN_PROGRESS"] * 3
    )
    values = [5, 7, True, 99, 9, 3]  # bool and out-of-range must not affect averages
    for index, status in enumerate(statuses):
        save_entity(
            session,
            "survey_run",
            {
                "employee_id": employee.id,
                "episode_id": employee.data["episode_id"],
                "survey_key": "SYNTHETIC",
                "first_question_accepted_at": "2026-10-23T10:00:00+03:00",
                "definition": {
                    "questions": [
                        {
                            "question_key": "Q1",
                            "type": "NUMERIC",
                            "minimum": 1,
                            "maximum": 10,
                            "prompt": {"text_vy": "Оцените адаптацию"},
                        }
                    ]
                },
                "answers": {"Q1": {"value": values[index]}} if index < 6 else {},
            },
            status=status,
        )
    # Accepted opening lies outside the requested local day, so it is absent.
    save_entity(
        session,
        "survey_run",
        {
            "employee_id": employee.id,
            "episode_id": employee.data["episode_id"],
            "first_question_accepted_at": "2026-10-22T20:59:59+00:00",
            "answers": {},
        },
        status="IN_PROGRESS",
    )
    result = d.dashboard_data(
        session,
        hr,
        {
            "period_from": "2026-10-23",
            "period_to": "2026-10-23",
            "timezone": "Europe/Moscow",
        },
    )
    assert result["metrics"]["completion"] == {
        "numerator": 4,
        "denominator": 10,
        "rate": 0.4,
    }
    assert result["metrics"]["participation"] == {
        "numerator": 6,
        "denominator": 10,
        "rate": 0.6,
    }
    assert result["metrics"]["surveys_declined"] == 1
    assert result["metrics"]["surveys_missing"] == 3
    for key, detail in result["metric_details"].items():
        assert detail["timezone"] == "Europe/Moscow"
        if key.startswith("surveys_") or key in ("completion", "participation"):
            assert detail["missing_answers"] == 3
        assert (
            "numerator" in detail
            and "denominator" in detail
            and detail["period_to"].startswith("2026-10-24")
        )
    numeric = result["numeric_averages"][0]
    assert (
        numeric["numerator"] == 24
        and numeric["denominator"] == 4
        and numeric["average"] == 6
    )
    assert (
        numeric["missing_answers"] == 6
        and numeric["question_text"] == "Оцените адаптацию"
    )
    empty = d.dashboard_data(
        session, hr, {"period_from": "2025-01-01", "period_to": "2025-01-01"}
    )
    assert empty["metric_details"]["completion"]["no_data"] is True
    assert empty["metric_details"]["completion"]["rate"] is None


def test_reporting_clock_does_not_materialize_spurious_dashboards(
    session, hr, monkeypatch
):
    profile(session, hr)
    fixed = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(d, "now", lambda: fixed)
    d.refresh_dashboards(session)
    session.commit()
    before = len(d.entities(session, "dashboard_snapshot", hr.id))
    monkeypatch.setattr(d, "now", lambda: fixed + timedelta(minutes=5))
    d.refresh_dashboards(session)
    session.commit()
    assert len(d.entities(session, "dashboard_snapshot", hr.id)) == before == 1


def test_export_contains_frozen_local_reporting_context(
    session, hr, tmp_path, monkeypatch
):
    import korsik.settings as settings

    monkeypatch.setattr(
        settings, "get_settings", lambda: SimpleNamespace(blob_root=str(tmp_path))
    )
    profile(session, hr)
    response = d.create_report_export(
        {
            "dataset": "ADAPTATION",
            "format": "CSV",
            "reason": "Выборка для проверки периода",
            "filters": {
                "period_from": "2026-10-23",
                "period_to": "2026-10-23",
                "timezone": "Asia/Yekaterinburg",
            },
            "columns": ["name"],
        },
        session,
        hr,
    )
    operation = session.get(Entity, json.loads(response.body)["id"])
    job = session.scalar(select(Job).where(Job.kind == "DOMAIN_REPORT_EXPORT"))
    d.process_report_export(session, job)
    session.commit()
    assert operation.status == "SUCCEEDED", operation.data.get("error")
    text = Path(operation.data["artifact_path"]).read_text(encoding="utf-8-sig")
    assert "report_timezone" in text and "Asia/Yekaterinburg" in text
    assert "2026-10-24T00:00:00+05:00" in text
    assert (
        d.operation_view(operation)["reporting_window"]["period_semantics"]
        == "FROM_INCLUSIVE_TO_EXCLUSIVE"
    )
    for filters in ({"_reporting_window": {"timezone": "forged"}}, {"unknown": True}):
        with pytest.raises(HTTPException):
            d.validate_export_request(
                {
                    "dataset": "ADAPTATION",
                    "reason": "Test invalid fields",
                    "filters": filters,
                },
                hr,
            )


def test_manual_account_change_private_audit_reason(session, hr):
    user = User(
        id="target-hr",
        name="Target",
        email="target@local.test",
        role="HR_SPECIALIST",
        scope={"site_ids": ["GrPP"]},
        password_hash="not-login",
        enabled=True,
    )
    session.add(user)
    session.commit()
    request = Request(
        {
            "type": "http",
            "headers": [(b"if-match", auth_admin.user_etag(user).encode())],
        }
    )
    auth_admin.update_user(
        user.id,
        {"enabled": False, "reason": "Завершение работы специалиста"},
        request,
        session,
        hr,
    )
    row = session.scalar(select(Audit).where(Audit.action == "user.update"))
    assert "Завершение" not in json.dumps(row.data, ensure_ascii=False)
    view = next(
        item for item in audit_events(hr, session)["items"] if item["id"] == row.id
    )
    assert (
        view["reason"] == "Завершение работы специалиста"
        and view["entity_id"] == user.id
    )


def test_numeric_averages_export_is_scoped_suppressed_and_formula_safe(session, hr):
    visible = profile(session, hr)
    other = profile(session, hr, site_id="GTP")
    for employee, value in ((visible, 8), (other, 1)):
        save_entity(
            session,
            "survey_run",
            {
                "employee_id": employee.id,
                "episode_id": employee.data["episode_id"],
                "survey_key": "NUMERIC_EXPORT",
                "first_question_accepted_at": d.now().isoformat(),
                "definition": {
                    "questions": [
                        {
                            "question_key": "Q1",
                            "type": "NUMERIC",
                            "minimum": 1,
                            "maximum": 10,
                            "prompt": {
                                "text_vy": '=HYPERLINK("https://example.invalid")'
                            },
                        }
                    ]
                },
                "answers": {"Q1": {"value": value}},
            },
            status="COMPLETED",
        )
    session.commit()
    scoped = Actor(hr.id, hr.name, "HR_ADMIN", {"site_ids": ["GrPP"]})
    response = d.analytics_export(session, scoped)
    rows = list(csv.DictReader(io.StringIO(response.body.decode("utf-8-sig"))))
    average = next(row for row in rows if row["measure_kind"] == "NUMERIC_AVERAGE")
    assert average["metric"] == "numeric_average:NUMERIC_EXPORT:Q1"
    assert (
        float(average["average"]) == 8
        and average["numerator"] == "8"
        and average["denominator"] == "1"
    )
    assert (
        average["question_text"].startswith("'=HYPERLINK")
        and average["timezone"] == "Europe/Moscow"
    )
    head = Actor(hr.id, hr.name, "HEAD_TALENT", {"all": True})
    response = d.analytics_export(session, head)
    rows = list(csv.DictReader(io.StringIO(response.body.decode("utf-8-sig"))))
    average = next(row for row in rows if row["measure_kind"] == "NUMERIC_AVERAGE")
    assert (
        average["suppressed"] == "True"
        and average["numerator"] == average["denominator"] == average["average"] == ""
    )
