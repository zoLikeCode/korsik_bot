"""Actual authenticated HTTP→durable ANSWER worker latency; synthetic unbound card only."""

import argparse
from collections import Counter
from datetime import datetime
import json
import math
from pathlib import Path
import secrets
import time
import uuid
from zoneinfo import ZoneInfo
import httpx
from sqlalchemy import select
from korsik.db import SessionLocal
from korsik.models import Entity, User, Outbox
from korsik.settings import get_settings

TOPICS = [
    "Как передавать пароли к конфиденциальной информации?",
    "Нужно ли соблюдать коммерческую тайну после увольнения?",
    "Кто разрешает посетителю фотографировать конфиденциальные документы?",
    "Когда сотрудник подписывает антикоррупционное обязательство?",
    "Можно ли дарить деловому партнёру денежный подарок?",
    "Какова миссия компании ПОЛИПЛАСТИК?",
    "Как поступать при выявлении коррупционного нарушения?",
    "Какие основные цели политики в области качества и экологии?",
    "Какие обязанности сотрудника по сохранению коммерческой тайны?",
    "Кто должен соблюдать антикоррупционную политику компании?",
]
PREFIXES = [
    "",
    "Подскажи, пожалуйста: ",
    "Хочу уточнить: ",
    "Объясни правило: ",
    "Что сказано в документах: ",
    "Помоги разобраться: ",
    "Нужен ответ по внутренним документам: ",
    "Уточни для сотрудника: ",
    "Мой вопрос к базе знаний: ",
    "Кратко поясни: ",
]


def stats(values):
    v = sorted(
        value
        for value in values
        if isinstance(value, (int, float)) and math.isfinite(value)
    )
    return (
        {
            "count": len(v),
            "p50_seconds": round(v[math.ceil(len(v) * 0.5) - 1], 4),
            "p95_seconds": round(v[math.ceil(len(v) * 0.95) - 1], 4),
            "max_seconds": round(max(v), 4),
        }
        if v
        else None
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--admin-password-file", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--mode", choices=["FULL", "CORE"], required=True)
    args = p.parse_args()
    settings = get_settings()
    assert settings.product_mode == args.mode, "Wrong runtime profile"
    run_id = uuid.uuid4().hex
    external = "PERF-KORSIK-" + run_id
    hr = httpx.Client(base_url="http://api:8000/api/v1", timeout=90, trust_env=False)
    employee_client = None
    employee_id = user_id = None
    rows = []

    def login(client, email, password):
        r = client.post("/auth/login", json={"email": email, "password": password})
        r.raise_for_status()
        client.headers.update(
            {
                "Cookie": "; ".join(f"{k}={v}" for k, v in r.cookies.items()),
                "X-CSRF-Token": r.json()["csrf_token"],
            }
        )
        return r.json()["user"]

    def call(client, method, path, **kw):
        headers = kw.pop("headers", {})
        if method != "GET":
            headers["Idempotency-Key"] = str(uuid.uuid4())
        r = client.request(method, path, headers=headers, **kw)
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP_{r.status_code}_{method}_{path}")
        return r

    hr_user = login(
        hr, settings.admin_email, Path(args.admin_password_file).read_text().strip()
    )
    employee = call(
        hr,
        "POST",
        "/employees",
        json={
            "name": "Синтетический тест задержки " + args.mode,
            "external_employee_id": external,
            "site_id": "GrPP",
            "start_date": datetime.now(ZoneInfo("Europe/Moscow")).date().isoformat(),
            "employment_type": "OFFICE",
            "timezone": "Europe/Moscow",
        },
    ).json()
    employee_id = employee["id"]
    episode = call(hr, "GET", "/employment-episodes/" + employee["episode_id"]).json()
    call(
        hr,
        "POST",
        "/employment-episodes/" + episode["id"] + "/stage",
        json={
            "stage": "ACTIVE",
            "expected_episode_id": episode["id"],
            "expected_state_version": episode["version"],
            "reason": "Synthetic performance card only",
        },
    )
    password = secrets.token_urlsafe(32)
    email = "performance-" + run_id + "@integration.invalid"
    user = call(
        hr,
        "POST",
        "/users",
        json={
            "name": "Synthetic latency account",
            "email": email,
            "role": "EMPLOYEE",
            "scope": {},
            "employee_id": employee_id,
            "password": password,
        },
    ).json()
    user_id = user["id"]
    employee_client = httpx.Client(
        base_url="http://api:8000/api/v1", timeout=90, trust_env=False
    )
    login(employee_client, email, password)
    conv = call(
        employee_client, "POST", "/conversations", json={"employee_id": employee_id}
    ).json()

    def query(question):
        with SessionLocal() as db:
            card = db.get(Entity, employee_id)
            assert card.data.get("external_employee_id") == external
            if (
                card.data.get("binding_id")
                or card.data.get("max_binding_status") == "VERIFIED"
            ):
                raise RuntimeError("MAX_BINDING_FORBIDDEN_FOR_LOAD_FIXTURE")
            bindings = db.scalars(
                select(Entity).where(
                    Entity.kind == "max_binding",
                    Entity.parent_id == employee_id,
                    Entity.status == "ACTIVE",
                )
            ).all()
            if bindings:
                raise RuntimeError("MAX_BINDING_FORBIDDEN_FOR_LOAD_FIXTURE")
        started = time.monotonic()
        turn = call(
            employee_client,
            "POST",
            "/conversations/" + conv["id"] + "/turns",
            json={
                "input": {"kind": "TEXT", "text": question},
                "client_message_id": str(uuid.uuid4()),
            },
        ).json()
        ack = time.monotonic() - started
        if turn.get("dispatch") != "BOT_ANSWER":
            return {
                "seconds": time.monotonic() - started,
                "ack_seconds": ack,
                "mode": "BUSY",
                "status": turn.get("dispatch"),
            }
        answer_id = turn["answer_id"]
        while time.monotonic() - started < 75:
            answer = call(employee_client, "GET", "/answers/" + answer_id).json()
            if answer["status"] not in {"QUEUED", "RUNNING"}:
                return {
                    "seconds": round(time.monotonic() - started, 4),
                    "ack_seconds": round(ack, 4),
                    "status": answer["status"],
                    "mode": answer.get("mode"),
                    "reason": answer.get("reason_code"),
                    "citations": len(answer.get("citations", [])),
                    "queue_seconds": answer.get("queue_seconds"),
                    "stages": answer.get("provenance", {}).get("timings", {}),
                }
            time.sleep(0.25)
        return {
            "seconds": round(time.monotonic() - started, 4),
            "ack_seconds": round(ack, 4),
            "mode": "TIMEOUT",
        }

    execution_error = None
    try:
        query(TOPICS[0])
        for prefix in PREFIXES:
            for question in TOPICS:
                row = query(prefix + question)
                row["id"] = len(rows) + 1
                rows.append(row)
                Path(args.output + ".checkpoint.json").write_text(
                    json.dumps(
                        {"status": "RUNNING", "mode": args.mode, "rows": rows},
                        ensure_ascii=False,
                    )
                )
                if len(rows) % 10 == 0:
                    print(
                        json.dumps({"completed": len(rows), "mode": args.mode}),
                        flush=True,
                    )
    except (Exception, KeyboardInterrupt) as error:
        execution_error = type(error).__name__
    finally:
        # No invite/binding was ever issued. Quarantine all synthetic sends and
        # disable this synthetic account even if a timing call failed.
        with SessionLocal() as db:
            card = db.get(Entity, employee_id)
            if card and card.data.get("external_employee_id") == external:
                from korsik.auth import Actor
                from korsik.domain import update, end_employee_access

                update(card, performance_test_completed=True, employment_stage="ENDED")
                end_employee_access(
                    db,
                    card,
                    Actor(
                        hr_user["id"], "Performance operator", "HR_ADMIN", {"all": True}
                    ),
                    "Synthetic latency fixture completed; no MAX binding was issued",
                )
                for item in db.scalars(
                    select(Outbox).where(
                        Outbox.recipient_id == employee_id,
                        Outbox.status.in_(
                            ["PENDING", "RETRY", "WAITING_BINDING", "QUEUED", "READY"]
                        ),
                    )
                ):
                    item.status = "SUPPRESSED"
                    item.data = {
                        **item.data,
                        "reason_code": "SYNTHETIC_PERFORMANCE_COMPLETED",
                    }
                account = db.get(User, user_id)
                if account and account.employee_id == employee_id:
                    account.enabled = False
                db.commit()
        if employee_client:
            employee_client.close()
        hr.close()
    report = {
        "schema": "korsik-http-durable-latency-v1",
        "mode": args.mode,
        "intended_questions": 100,
        "execution_error": execution_error,
        "questions": len(rows),
        "workload": "100 distinct Russian prompts: 10 document topics ×10 wording prefixes; warmed, one active benchmark turn",
        "quality_limit": "Timing workload, not 100 independent golden semantic questions or company acceptance",
        "transport": "Real HTTP session/CSRF/idempotency→PostgreSQL ANSWER job→answer-worker→local pipeline→persisted answer; MAX unbound, no messages sent",
        "http_end_to_end": stats([r["seconds"] for r in rows]),
        "http_ack": stats([r["ack_seconds"] for r in rows]),
        "durable_queue": stats(
            [
                r["queue_seconds"]
                for r in rows
                if isinstance(r.get("queue_seconds"), (int, float))
            ]
        ),
        "modes": dict(Counter(r.get("mode") for r in rows)),
        "completed_answers": sum(r.get("status") == "COMPLETED" for r in rows),
        "terminal_answers": sum(
            r.get("status") in {"COMPLETED", "ABSTAINED"} for r in rows
        ),
        "abstained_answers": sum(r.get("status") == "ABSTAINED" for r in rows),
        "reason_codes": dict(Counter(r.get("reason") for r in rows)),
        "full_generation_observed": any(
            r.get("mode") == "LOCAL_GENERATED" for r in rows
        ),
        "generated_only": stats(
            [r["seconds"] for r in rows if r.get("mode") == "LOCAL_GENERATED"]
        ),
        "stages": {
            k: stats([r["stages"][k] for r in rows if k in r.get("stages", {})])
            for k in sorted({k for r in rows for k in r.get("stages", {})})
        },
        "rows": rows,
        "target_p95_seconds": 30,
    }
    report["latency_target_met"] = (
        len(rows) == 100
        and report["http_end_to_end"]["p95_seconds"] <= 30
        and all(
            r.get("status") in {"COMPLETED", "ABSTAINED"}
            and r.get("mode")
            in {"LOCAL_GENERATED", "EXTRACT", "HANDOFF", "CLARIFY", "APPROVED_CARD"}
            for r in rows
        )
        and (args.mode == "CORE" or report["full_generation_observed"])
    )
    report["generated_latency_target_met"] = (
        bool(report["generated_only"] and report["generated_only"]["p95_seconds"] <= 30)
        if args.mode == "FULL"
        else None
    )
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(
        json.dumps(
            {k: v for k, v in report.items() if k != "rows"}, ensure_ascii=False
        ),
        flush=True,
    )
    if execution_error:
        raise SystemExit(130 if execution_error == "KeyboardInterrupt" else 1)


if __name__ == "__main__":
    main()
