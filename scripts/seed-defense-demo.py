"""Small, idempotent presentation dataset. No MAX deliveries or model requests."""

import argparse
from datetime import datetime, timedelta
import json
import secrets
import sys
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy import select, text
from korsik.auth import ph, user_actor
from korsik.common import audit, save_entity
from korsik.db import SessionLocal, now
from korsik.models import Entity, User
from korsik.organization import _all, _validate_graph
from korsik.scenario_engine import add_months
from korsik.survey_campaigns import Draft, _definition

DATASET = "defense-demo-v1"
NAMESPACE = UUID("646b3926-1f01-46a3-8198-0a7d660b4da0")
NOTE = (
    "Вымышленные данные для демонстрации на защите. Не результаты реальных сотрудников."
)
UNITS = [
    ("commercial", "Коммерческое направление", "DIRECTION", None, 0),
    ("sales", "Отдел продаж", "DEPARTMENT", "commercial", 1),
    ("regional", "Региональные продажи", "TEAM", "sales", 1),
    ("service", "Отдел клиентского сервиса", "DEPARTMENT", "commercial", 5),
    ("orders", "Сопровождение заказов", "TEAM", "service", 5),
    ("production", "Производственное направление", "DIRECTION", None, 8),
    ("plant", "Производственный отдел", "DEPARTMENT", "production", 9),
    ("extrusion", "Команда экструзии", "TEAM", "plant", 9),
    ("quality", "Отдел качества", "DEPARTMENT", "production", 12),
    ("lab", "Лаборатория контроля качества", "TEAM", "quality", 12),
]
PEOPLE = [
    ("Александр Морозов", "Коммерческий директор", "commercial", None, 130),
    ("Елена Смирнова", "Руководитель отдела продаж", "regional", 0, 100),
    ("Михаил Соколов", "Менеджер по продажам", "regional", 1, 27),
    ("Анна Кузнецова", "Менеджер по продажам", "regional", 1, 18),
    ("Дмитрий Орлов", "Специалист по работе с партнёрами", "regional", 1, 7),
    ("Мария Волкова", "Руководитель клиентского сервиса", "orders", 0, 95),
    ("Ирина Новикова", "Специалист клиентского сервиса", "orders", 5, 14),
    ("Павел Фёдоров", "Координатор заказов", "orders", 5, 3),
    ("Сергей Васильев", "Директор по производству", "production", None, 160),
    ("Ольга Павлова", "Руководитель производственного отдела", "extrusion", 8, 92),
    ("Андрей Захаров", "Оператор экструзионной линии", "extrusion", 9, 30),
    ("Наталья Лебедева", "Инженер-технолог", "extrusion", 9, 11),
    ("Алексей Белов", "Руководитель отдела качества", "lab", 8, 110),
    ("Екатерина Крылова", "Инженер по качеству", "lab", 12, 20),
    ("Роман Никитин", "Лаборант", "lab", 12, -3),
]


def key(name):
    return str(uuid5(NAMESPACE, DATASET + ":" + str(name)))


def seed_demo(db, password, site_id="GrPP", anchor=None):
    """Caller commits one transaction. Existing datasets and passwords stay unchanged."""
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(646392601)"))
    marker = db.get(Entity, key("dataset"))
    if marker:
        if marker.kind != "demo_dataset" or marker.data.get("demo_dataset") != DATASET:
            raise ValueError("Dataset ID collision")
        return {**marker.data["summary"], "created": False}
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise ValueError("Demo password must contain 12–128 characters")
    site = db.get(Entity, site_id)
    if not site or site.kind != "site" or not site.data.get("legal_entity_id"):
        raise ValueError("Select an existing site with a legal entity")
    legal = site.data["legal_entity_id"]
    issuer = next(
        (
            u
            for u in db.scalars(
                select(User).where(User.role == "HR_ADMIN", User.enabled.is_(True))
            )
            if u.scope.get("all") is True
        ),
        None,
    )
    if not issuer:
        raise ValueError("An enabled global HR administrator is required")
    today = anchor or now().astimezone(ZoneInfo("Europe/Moscow")).date()
    clock = datetime.combine(
        today, datetime.min.time(), tzinfo=ZoneInfo("Europe/Moscow")
    ) + timedelta(hours=10)
    managers = {u[4] for u in UNITS}

    def entity(name, kind, data, status="ACTIVE", parent=None):
        if db.get(Entity, key(name)):
            raise ValueError("Refusing to overwrite an existing entity: " + str(name))
        return save_entity(
            db,
            kind,
            {**data, "demo_dataset": DATASET, "data_origin": "SYNTHETIC_PRESENTATION"},
            id=key(name),
            status=status,
            parent_id=parent,
            owner_id=issuer.id,
        )

    def account(name, login, display, role, scope, employee=None, secret=None):
        if db.get(User, key(name)) or db.scalar(
            select(User.id).where(User.email == login)
        ):
            raise ValueError("Refusing to replace an existing account: " + login)
        u = User(
            id=key(name),
            email=login,
            name=display,
            role=role,
            scope=scope,
            enabled=True,
            employee_id=employee,
            password_hash=ph.hash(secret or secrets.token_urlsafe(32)),
        )
        db.add(u)
        db.flush()
        return u

    jury = account(
        "jury",
        "jury",
        "Руководитель по подбору — демо",
        "HEAD_TALENT",
        {"all": True},
        secret=password,
    )
    hr = account(
        "hr",
        "hr-demo@korsik.invalid",
        "Татьяна Иванова",
        "HR_SPECIALIST",
        {"site_ids": [site_id]},
    )
    for name, title, kind, parent, head in UNITS:
        entity(
            "unit-" + name,
            "org_unit",
            {
                "title": title,
                "type": kind,
                "legal_entity_id": legal,
                "site_id": site_id,
                "head_employee_id": key("employee-" + str(head)),
                "description": NOTE,
            },
            parent=key("unit-" + parent) if parent else None,
        )
    employees = []
    for i, (name, position, unit, manager, days) in enumerate(PEOPLE):
        start = today - timedelta(days=days)
        stage = "PREBOARDING" if start > today else "ACTIVE"
        ep = entity(
            "episode-" + str(i),
            "employment_episode",
            {
                "employee_id": key("employee-" + str(i)),
                "start_date": start.isoformat(),
                "probation_end_date": (
                    add_months(start, 3) - timedelta(days=1)
                ).isoformat(),
                "stage": stage,
            },
            status=stage,
            parent=key("employee-" + str(i)),
        )
        employee = entity(
            "employee-" + str(i),
            "employee",
            {
                "name": name,
                "preferred_name": name.split()[0],
                "position": position,
                "email": f"staff{i + 1:02}@korsik.example",
                "employee_id": key("employee-" + str(i)),
                "external_employee_id": f"DEFENSE-DEMO-{i + 1:03}",
                "site_id": site_id,
                "legal_entity_id": legal,
                "org_unit_id": key("unit-" + unit),
                "department": dict((u[0], u[1]) for u in UNITS)[unit],
                "start_date": start.isoformat(),
                "probation_end_date": ep.data["probation_end_date"],
                "employment_stage": stage,
                "episode_id": ep.id,
                "enabled": True,
                "timezone": "Europe/Moscow",
                "employment_type": "PRODUCTION" if i >= 8 else "OFFICE",
                "assigned_hr_id": hr.id,
                "max_binding_status": "UNBOUND",
                "notes": NOTE,
                "reports_to_employee_id": key("employee-" + str(manager))
                if manager is not None
                else None,
                "manager_id": key("manager-" + str(manager))
                if manager is not None
                else None,
            },
        )
        employees.append(employee)
        if i in managers:
            u = account(
                "manager-" + str(i),
                f"manager{i + 1:02}@korsik.invalid",
                name,
                "MANAGER",
                {},
                employee=employee.id,
            )
            employee.data = {
                **employee.data,
                "user_id": u.id,
                "miniapp_role_access": {
                    "user_id": u.id,
                    "episode_id": ep.id,
                    "role": "MANAGER",
                },
            }

    all_employees, all_units = _all(db)
    _validate_graph(db, all_employees, all_units, legal)
    scenario = next(
        (
            s
            for s in db.scalars(
                select(Entity)
                .where(Entity.kind == "scenario", Entity.status == "ACTIVE")
                .order_by(Entity.created_at)
            )
            if s.data.get("published_version_id")
        ),
        None,
    )
    if scenario:
        for i, employee in enumerate(employees):
            if i == 9:
                continue
            status = (
                "COMPLETED"
                if i in {0, 1, 8, 12}
                else "PAUSED"
                if i in {5, 11}
                else "SCHEDULED"
                if i == 14
                else "ACTIVE"
            )
            entity(
                "journey-" + str(i),
                "journey",
                {
                    "employee_id": employee.id,
                    "episode_id": employee.data["episode_id"],
                    "scenario_id": scenario.id,
                    "version_id": scenario.data["published_version_id"],
                    "schedule_revision": 1,
                    "signals": {},
                    "events": {},
                    "answers": {},
                    "completed_steps": [],
                    "demo_snapshot": True,
                },
                status=status,
                parent=employee.id,
            )

    draft = Draft(
        title="Обратная связь об адаптации (демо)",
        description=NOTE,
        purpose="ADAPTATION",
        reason=NOTE,
        questions=[
            {
                "question_key": "clarity",
                "prompt": "Насколько понятны ваши рабочие задачи?",
                "type": "LIKERT",
                "minimum": 1,
                "maximum": 10,
            },
            {
                "question_key": "support",
                "prompt": "Хватает ли поддержки коллег и руководителя?",
                "type": "LIKERT",
                "minimum": 1,
                "maximum": 10,
            },
            {
                "question_key": "help",
                "prompt": "Какая помощь будет полезнее?",
                "type": "SINGLE_CHOICE",
                "options": [
                    {"value": "tasks", "label": "Разбор рабочих задач"},
                    {"value": "mentor", "label": "Встреча с наставником"},
                ],
            },
        ],
    )
    template = entity(
        "survey-template",
        "survey_template",
        {
            "title": draft.title,
            "scope": {"site_ids": [site_id]},
            "published_version_id": key("survey-version"),
        },
    )
    version = entity(
        "survey-version",
        "survey_template_version",
        {
            **draft.model_dump(exclude={"reason"}),
            "version_number": 1,
            "published_at": (clock - timedelta(days=14)).isoformat(),
            "publication_note": NOTE,
        },
        status="PUBLISHED",
        parent=template.id,
    )
    entity(
        "survey-draft",
        "survey_template_version",
        {
            **draft.model_dump(exclude={"reason"}),
            "title": "Обратная связь об адаптации — новая редакция (демо)",
            "version_number": 2,
            "base_version_id": version.id,
        },
        status="DRAFT",
        parent=template.id,
    )
    definition = _definition(version)
    campaign = entity(
        "survey-campaign",
        "survey_campaign",
        {
            "name": "Первый месяц в компании (демо)",
            "version_id": version.id,
            "template_id": template.id,
            "scope": {"site_ids": [site_id]},
            "generation": 1,
            "created_by": jury.id,
            "creator_scope": {"all": True},
            "recipient_count": len(employees),
            "audience": {"employee_ids": [e.id for e in employees]},
            "schedule": {
                "starts_at": (clock - timedelta(days=14)).isoformat(),
                "ends_at": (clock - timedelta(days=1)).isoformat(),
                "timezone": "Europe/Moscow",
                "response_window_hours": 168,
                "reminder_hours": [],
            },
            "completed_at": (clock - timedelta(days=1)).isoformat(),
            "privacy_notice": NOTE,
        },
        status="COMPLETED",
    )
    for i, employee in enumerate(employees):
        stamp = clock - timedelta(days=2 + i % 10)
        answers = (
            {
                "CUSTOM-clarity": {
                    "outcome": "ANSWERED",
                    "value": [8, 9, 7, 8, 6, 9, 8, 7, 8, 10][i % 10],
                },
                "CUSTOM-support": {
                    "outcome": "ANSWERED",
                    "value": [9, 8, 8, 7, 9, 9, 8, 7, 8, 9][i % 10],
                },
                "CUSTOM-help": {
                    "outcome": "ANSWERED",
                    "value": "tasks" if i < 5 else "mentor",
                },
            }
            if i < 10
            else {}
        )
        run = entity(
            "survey-run-" + str(i),
            "survey_run",
            {
                "employee_id": employee.id,
                "episode_id": employee.data["episode_id"],
                "survey_key": definition["survey_key"],
                "definition": definition,
                "campaign_id": campaign.id,
                "campaign_generation": 1,
                "version_id": version.id,
                "answers": answers,
                "first_question_accepted_at": stamp.isoformat(),
                "completed_at": (stamp + timedelta(minutes=7)).isoformat(),
                "current_question_key": None,
                "closed_reason": "DEMO_SNAPSHOT",
            },
            status="COMPLETED" if i < 10 else "DECLINED",
            parent=campaign.id,
        )
        entity(
            "enrollment-" + str(i),
            "survey_campaign_enrollment",
            {
                "campaign_id": campaign.id,
                "employee_id": employee.id,
                "episode_id": employee.data["episode_id"],
                "version_id": version.id,
                "generation": 1,
                "run_id": run.id,
            },
            status=run.status,
            parent=campaign.id,
        )
        if i < 10:
            feedback = entity(
                "feedback-" + str(i),
                "feedback",
                {
                    "employee_id": employee.id,
                    "episode_id": employee.data["episode_id"],
                    "text": NOTE,
                    "source_site_id": site_id,
                },
                parent=employee.id,
            )
            feedback.created_at = stamp
            entity(
                "analysis-" + str(i),
                "feedback_analysis",
                {
                    "employee_id": employee.id,
                    "feedback_id": feedback.id,
                    "topics": [
                        "Демо: доступы к системам" if i < 5 else "Демо: рабочее место"
                    ],
                    "review_source": "SYNTHETIC_PRESENTATION",
                },
                status="REVIEWED",
                parent=feedback.id,
            )
    summary = {
        "dataset": DATASET,
        "login": jury.email,
        "role": jury.role,
        "employees": len(employees),
        "units": len(UNITS),
        "managers": len(managers),
        "hr_managers": 1,
        "surveys": 1,
        "completed_surveys": 10,
        "declined_surveys": 5,
        "anchor_date": today.isoformat(),
        "note": NOTE,
    }
    marker = entity(
        "dataset", "demo_dataset", {"summary": summary, "created_at": now().isoformat()}
    )
    audit(
        db, user_actor(issuer), "demo.dataset.create", marker.id, summary, reason=NOTE
    )
    return {**summary, "created": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Create demo records in one transaction"
    )
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="Read the new jury password from stdin; never print it",
    )
    parser.add_argument("--site-id", default="GrPP")
    args = parser.parse_args()
    if not args.apply:
        print(
            json.dumps(
                {
                    "mode": "preview",
                    "dataset": DATASET,
                    "employees": len(PEOPLE),
                    "units": len(UNITS),
                    "login": "jury",
                    "role": "HEAD_TALENT",
                    "note": NOTE,
                },
                ensure_ascii=False,
            )
        )
        return
    if not args.password_stdin:
        parser.error("--apply requires --password-stdin")
    password = sys.stdin.read(1024).strip()
    with SessionLocal() as db:
        result = seed_demo(db, password, args.site_id)
        db.commit()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
