"""Live scoped HR dashboards. No generated scores or raw survey text responses."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
import hashlib
import json
import math
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy import select

from .auth import get_actor
from .common import require_role, employee_is_enabled
from .db import get_db
from .models import Entity

router = APIRouter()
MIN_GROUP = 5
CLOSED = {"RESOLVED", "CLOSED", "CONFIRMED", "CLOSED_WITHOUT_CONFIRMATION", "CANCELLED"}


def department_memberships(db, employees):
    units = {
        u.id: u
        for u in db.scalars(
            select(Entity).where(Entity.kind == "org_unit", Entity.status == "ACTIVE")
        )
    }
    result = {}
    for employee in employees:
        key, path = employee.data.get("org_unit_id"), set()
        while key:
            if key in path or len(path) >= 512:
                path = set()
                break
            unit = units.get(key)
            if not unit or unit.data.get("legal_entity_id") != employee.data.get(
                "legal_entity_id"
            ):
                break
            if unit.data.get("site_id") and unit.data["site_id"] != employee.data.get(
                "site_id"
            ):
                break
            path.add(key)
            key = unit.parent_id
        result[employee.id] = path
    return units, result


def _prompt(value):
    if isinstance(value, dict):
        return (
            value.get("text_vy")
            or value.get("text_ty")
            or value.get("vy")
            or value.get("ty")
            or "Вопрос"
        )
    return str(value or "Вопрос")


def _safe_cells(counts, distinct):
    return distinct >= MIN_GROUP and all(n == 0 or n >= MIN_GROUP for n in counts)


def build_insights(db, actor, filters=None):
    from . import domain as d
    from .dialog_issues import problem_index

    require_role(actor, "HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT", "MANAGER")
    filters = dict(filters or {})
    window = d.reporting_window(filters)
    filters["_reporting_window"] = window
    base = [
        e
        for e in d.employee_scope(db, actor)
        if employee_is_enabled(e) and e.data.get("employment_stage") != "ENDED"
    ]
    units, memberships = department_memberships(db, base)
    visible_units = set().union(*(memberships.values())) if memberships else set()
    base_ids = {p.id for p in base}
    employees = [
        e for e in d.filtered_employees(db, actor, filters) if e.id in base_ids
    ]
    people = {e.id: e for e in employees}
    kinds = {
        "survey_run",
        "journey",
        "case",
        "answer",
        "conversation",
        "survey_campaign",
        "survey_template_version",
    }
    if actor.role == "HEAD_TALENT":
        kinds.update({"feedback", "feedback_analysis"})
    grouped = defaultdict(list)
    for row in db.scalars(
        select(Entity)
        .where(Entity.kind.in_(kinds))
        .order_by(Entity.created_at, Entity.id)
    ):
        grouped[row.kind].append(row)
    by_id = {r.id: r for rows in grouped.values() for r in rows}

    def current(row):
        eid = row.data.get("employee_id") or row.parent_id
        employee = people.get(eid)
        if not employee or not employee.data.get("episode_id"):
            return False
        episode = row.data.get("episode_id")
        if row.kind == "answer":
            # Native chat answers inherit the hiring episode from their conversation.
            conversation_id = row.data.get("conversation_id") or row.parent_id
            if conversation_id:
                conversation = by_id.get(conversation_id)
                if (
                    not conversation
                    or conversation.kind != "conversation"
                    or conversation.data.get("employee_id") != employee.id
                    or conversation.data.get("episode_id")
                    != employee.data["episode_id"]
                ):
                    return False
                if episode is None:
                    episode = conversation.data.get("episode_id")
        return episode == employee.data["episode_id"]

    def in_period(value):
        return d.in_report_period(value, filters)

    journeys = [j for j in grouped["journey"] if current(j)]
    journey_by_employee = defaultdict(list)
    for j in journeys:
        journey_by_employee[j.data.get("employee_id") or j.parent_id].append(j)
    stages = Counter()
    person_stage = {}
    for employee in employees:
        runs = journey_by_employee[employee.id]
        state = (
            "in_progress"
            if any(j.status in {"ACTIVE", "RUNNING", "IN_PROGRESS"} for j in runs)
            else "paused"
            if any(j.status == "PAUSED" for j in runs)
            else "scheduled"
            if any(j.status == "SCHEDULED" for j in runs)
            else "completed"
            if any(j.status == "COMPLETED" for j in runs)
            else "not_assigned"
        )
        stages[state] += 1
        person_stage[employee.id] = state

    conversations = [
        c for c in grouped["conversation"] if current(c) and c.status != "CLOSED"
    ]
    issues = problem_index(db, conversations)
    unresolved_by_employee = Counter()
    for conv in conversations:
        unresolved_by_employee[conv.data["employee_id"]] += sum(
            i["status"] == "OPEN" for i in issues.get(conv.id, {}).values()
        )
    # The projection above resolves only explicitly answered original questions.
    open_questions = sum(unresolved_by_employee.values())
    current_cases = [c for c in grouped["case"] if current(c)]
    cases = [c for c in current_cases if c.status not in CLOSED]

    zone = ZoneInfo(window["timezone"])
    upper = d.aware(window["period_to"])
    answers = [
        a
        for a in grouped["answer"]
        if current(a) and a.status != "CANCELLED" and in_period(a.created_at)
    ]
    lower = (
        d.aware(window["period_from"])
        if window["period_from"]
        else min(
            (d.aware(a.created_at).astimezone(zone) for a in answers),
            default=upper - timedelta(days=30),
        ).replace(hour=0, minute=0, second=0, microsecond=0)
    )
    days = max(
        1, (upper.date() - lower.date()).days + (upper.time() != datetime.min.time())
    )
    step = max(1, math.ceil(days / 30))
    buckets = [
        {
            "date": (lower + timedelta(days=i)).date().isoformat(),
            "questions": 0,
            "automatic": 0,
            "handoff": 0,
        }
        for i in range(0, days, step)
    ]
    outcomes = Counter()
    for answer in answers:
        mode = answer.data.get("mode")
        result = (
            "automatic"
            if mode in {"LOCAL_GENERATED", "EXTRACT", "APPROVED_CARD"}
            and answer.status not in {"FAILED", "EXPIRED"}
            else "handoff"
            if mode == "HANDOFF"
            or answer.status in {"FAILED", "EXPIRED", "ABSTAINED"}
            and mode != "CLARIFY"
            else "clarify"
            if mode == "CLARIFY"
            else "pending"
        )
        outcomes[result] += 1
        occurred = d.aware(answer.created_at).astimezone(zone)
        if lower <= occurred < upper:
            index = min(len(buckets) - 1, (occurred.date() - lower.date()).days // step)
            if index >= 0:
                buckets[index]["questions"] += 1
                if result in {"automatic", "handoff"}:
                    buckets[index][result] += 1

    survey_groups = {}
    numeric = {}
    for run in grouped["survey_run"]:
        accepted = run.data.get("first_question_accepted_at")
        if (
            not current(run)
            or not accepted
            or not in_period(accepted)
            or run.status == "CANCELLED"
        ):
            continue
        campaign = by_id.get(run.data.get("campaign_id"))
        if run.data.get("campaign_id") and (
            not campaign
            or run.data.get("campaign_generation") != campaign.data.get("generation")
        ):
            continue
        definition = run.data.get("definition") or {}
        key = run.data.get("survey_key") or definition.get("survey_key") or "survey"
        version = (
            by_id.get(key.removeprefix("CUSTOM-"))
            if key.startswith("CUSTOM-")
            else None
        )
        title = version.data.get("title") if version else definition.get("title")
        title = title or {
            "SUR-D3": "Первые дни",
            "SUR-D14": "Вторая неделя",
            "SUR-M1": "Первый месяц",
            "SUR-M1W3": "Месяц 1, неделя 3",
            "SUR-END": "Завершение адаптации",
        }.get(key, "Опрос адаптации")
        group = survey_groups.setdefault(
            key,
            {
                "key": key,
                "title": title,
                "custom": bool(campaign),
                "people": set(),
                "states": Counter(),
                "delivered": 0,
            },
        )
        group["people"].add(run.data["employee_id"])
        group["delivered"] += 1
        state = (
            "completed"
            if run.status == "COMPLETED"
            else "declined"
            if run.status == "DECLINED"
            else "expired"
            if run.status == "EXPIRED"
            else "in_progress"
        )
        group["states"][state] += 1
        for q in definition.get("questions", []):
            if q.get("type") not in {"NUMERIC", "SINGLE_CHOICE", "MULTI_CHOICE"}:
                continue
            signature = hashlib.sha256(
                json.dumps(q, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()[:16]
            qkey = (key, q.get("question_key"), signature)
            item = numeric.setdefault(
                qkey,
                {
                    "title": title,
                    "question": _prompt(q.get("authored_prompt") or q.get("prompt")),
                    "definition": q,
                    "answers": {},
                },
            )
            a = run.data.get("answers", {}).get(q.get("question_key"), {})
            v = a.get("value")
            if a.get("outcome") != "ANSWERED":
                continue
            if q["type"] == "NUMERIC":
                lo, hi = q.get("minimum", 1), q.get("maximum", 10)
                if type(v) is not int or not lo <= v <= hi:
                    continue
            else:
                allowed = {o["value"] for o in q.get("options", [])}
                choices = (
                    v if isinstance(v, list) and q["type"] == "MULTI_CHOICE" else [v]
                )
                if not choices or any(
                    not isinstance(x, str) or x not in allowed for x in choices
                ):
                    continue
                v = list(set(choices))
            # Multiple runs of one immutable version count a person once, latest wins.
            item["answers"][run.data["employee_id"]] = v

    survey_rows = []
    for group in survey_groups.values():
        counts = {
            s: group["states"][s]
            for s in ("completed", "in_progress", "declined", "expired")
        }
        safe = not group["custom"] or _safe_cells(counts.values(), len(group["people"]))
        survey_rows.append(
            {
                "key": group["key"],
                "title": group["title"],
                "custom": group["custom"],
                "suppressed": not safe,
                "delivered": group["delivered"] if safe else None,
                **{k: v if safe else None for k, v in counts.items()},
            }
        )
    results = []
    for (key, question_key, signature), item in numeric.items():
        q, values = item["definition"], list(item["answers"].values())
        safe = len(values) >= MIN_GROUP
        distribution = None
        if q["type"] != "NUMERIC" and safe:
            counts = [
                sum(o["value"] in v for v in values) for o in q.get("options", [])
            ]
            if _safe_cells(counts, len(values)):
                distribution = [
                    {"label": _prompt(o["label"]), "count": n}
                    for o, n in zip(q["options"], counts)
                ]
        results.append(
            {
                "key": f"{key}:{question_key}:{signature}",
                "survey": item["title"],
                "question": item["question"],
                "type": q["type"],
                "suppressed": not safe,
                "respondents": len(values) if safe else None,
                "minimum": q.get("minimum", 1),
                "maximum": q.get("maximum", 10),
                "average": round(sum(values) / len(values), 2)
                if safe and q["type"] == "NUMERIC"
                else None,
                "distribution": distribution,
            }
        )

    department_rows = []
    for unit_id in sorted(
        {
            e.data.get("org_unit_id")
            for e in employees
            if e.data.get("org_unit_id") in memberships[e.id]
        }
    ):
        team = [e for e in employees if e.data.get("org_unit_id") == unit_id]
        department_rows.append(
            {
                "id": unit_id,
                "name": units[unit_id].data.get("title", "Подразделение"),
                "employees": len(team),
                "adapting": sum(
                    person_stage[e.id] in {"in_progress", "paused", "scheduled"}
                    for e in team
                ),
                "open_questions": sum(unresolved_by_employee[e.id] for e in team),
            }
        )
    unassigned = [
        e for e in employees if e.data.get("org_unit_id") not in memberships[e.id]
    ]
    if unassigned:
        department_rows.append(
            {
                "id": None,
                "name": "Без подразделения",
                "employees": len(unassigned),
                "adapting": sum(
                    person_stage[e.id] in {"in_progress", "paused", "scheduled"}
                    for e in unassigned
                ),
                "open_questions": sum(unresolved_by_employee[e.id] for e in unassigned),
            }
        )
    department_rows.sort(
        key=lambda r: (-r["open_questions"], -r["adapting"], r["name"])
    )
    topic_people, pending = defaultdict(set), 0
    latest_analyses = {
        a.data.get("feedback_id"): a for a in grouped["feedback_analysis"]
    }
    for feedback_id, analysis in latest_analyses.items():
        feedback = by_id.get(feedback_id)
        if (
            not feedback
            or feedback.kind != "feedback"
            or not current(feedback)
            or feedback.data.get("employee_id") != analysis.data.get("employee_id")
            or not in_period(feedback.created_at)
        ):
            continue
        if analysis.status == "PENDING_HR_REVIEW":
            pending += 1
        elif analysis.status == "REVIEWED":
            for topic in analysis.data.get("topics", []):
                if isinstance(topic, str) and topic.strip():
                    topic_people[topic.strip()].add(analysis.data["employee_id"])
    topics = [
        {
            "topic": name if len(p) >= MIN_GROUP else None,
            "employee_count": len(p) if len(p) >= MIN_GROUP else None,
            "suppressed": len(p) < MIN_GROUP,
            "method": "HUMAN_REVIEWED_FEEDBACK",
        }
        for name, p in sorted(topic_people.items())
    ]
    delivered = sum(r["delivered"] or 0 for r in survey_rows if not r["suppressed"])
    completed = sum(r["completed"] or 0 for r in survey_rows if not r["suppressed"])
    suppressed_surveys = sum(r["suppressed"] for r in survey_rows)
    return {
        "generated_at": d.now().isoformat(),
        "reporting_window": window,
        "minimum_group_size": MIN_GROUP,
        "filters": {
            "departments": [
                {"id": k, "name": units[k].data.get("title", "Подразделение")}
                for k in sorted(visible_units)
            ]
        },
        "summary": {
            "employees": len(employees),
            "adapting": sum(stages[k] for k in ("in_progress", "paused", "scheduled")),
            "completed": stages["completed"],
            "open_questions": open_questions,
            "problem_conversations": sum(
                any(i["status"] == "OPEN" for i in issues.get(c.id, {}).values())
                for c in conversations
            ),
            "open_cases": len(cases),
            "open_blockers": sum(
                c.data.get("kind") == "BLOCKER"
                and c.status
                not in {"CONFIRMED", "CLOSED_WITHOUT_CONFIRMATION", "CANCELLED"}
                for c in current_cases
            ),
            "surveys_delivered": None if suppressed_surveys else delivered,
            "surveys_completed": None if suppressed_surveys else completed,
            "completion_rate": completed / delivered
            if delivered and not suppressed_surveys
            else None,
            "questions": len(answers),
            **{k: outcomes[k] for k in ("automatic", "handoff", "clarify", "pending")},
        },
        "problems": {
            "items": topics,
            "pending_analysis_count": pending,
            "minimum_group_size": MIN_GROUP,
        },
        "adaptation": dict(stages),
        "trend": buckets,
        "trend_bucket_days": step,
        "surveys": survey_rows,
        "survey_results": results,
        "departments": department_rows,
        "privacy": {
            "raw_answers_included": False,
            "minimum_distinct_respondents": MIN_GROUP,
        },
        "methodology": {
            "current": "Активные сотрудники текущего периода найма; статусы адаптации и нерешённые вопросы на сейчас.",
            "surveys": "Анкеты адаптации и HR-опросы с доставленным первым вопросом за выбранный период; текущий статус прохождения.",
            "results": "Последний валидный ответ сотрудника по одной редакции вопроса. Среднее — в исходной шкале; текстовые ответы не выводятся.",
            "trend": "Вопросы Корсику по дате создания запроса. Передача HR не считается автоматическим ответом; доставка в MAX здесь не измеряется.",
        },
    }


@router.get("/analytics/insights")
def insights(
    db=Depends(get_db),
    actor=Depends(get_actor),
    period_from: str | None = None,
    period_to: str | None = None,
    timezone: str = "Europe/Moscow",
    site_id: str | None = None,
    org_unit_id: str | None = None,
):
    return build_insights(
        db,
        actor,
        {
            "period_from": period_from,
            "period_to": period_to,
            "timezone": timezone,
            "site_ids": [site_id] if site_id else [],
            "org_unit_id": org_unit_id,
        },
    )


@router.get("/analytics/insights/export")
def export_insights(
    db=Depends(get_db),
    actor=Depends(get_actor),
    period_from: str | None = None,
    period_to: str | None = None,
    timezone: str = "Europe/Moscow",
    site_id: str | None = None,
    org_unit_id: str | None = None,
):
    import csv
    import io
    from fastapi.responses import Response

    data = insights(db, actor, period_from, period_to, timezone, site_id, org_unit_id)
    output = io.StringIO()
    writer = csv.writer(output, delimiter=";")

    def row(*values):
        writer.writerow(
            [
                ("'" + v if v.lstrip().startswith(("=", "+", "-", "@")) else v)
                if isinstance(v, str)
                else ""
                if v is None
                else v
                for v in values
            ]
        )

    row("Раздел", "Показатель", "Значение", "Описание")
    labels = {
        "employees": "Сотрудники",
        "adapting": "На адаптации",
        "completed": "Завершили адаптацию",
        "open_questions": "Вопросы без ответа HR",
        "problem_conversations": "Проблемные диалоги",
        "surveys_delivered": "Доставлено анкет",
        "surveys_completed": "Завершено анкет",
        "questions": "Вопросы Корсику",
        "automatic": "Ответы Корсика",
        "handoff": "Передано HR",
        "clarify": "Нужно уточнение",
        "pending": "В обработке",
    }
    for key, label in labels.items():
        row(
            "Сводка",
            label,
            data["summary"][key],
            "Текущий срез"
            if key
            not in {
                "questions",
                "automatic",
                "handoff",
                "clarify",
                "pending",
                "surveys_delivered",
                "surveys_completed",
            }
            else "За выбранный период",
        )
    for survey in data["surveys"]:
        for key, label in (
            ("delivered", "Доставлено"),
            ("completed", "Завершено"),
            ("in_progress", "В процессе"),
            ("declined", "Отказ"),
            ("expired", "Срок истёк"),
        ):
            row(
                "Опросы",
                survey["title"] + ": " + label,
                survey[key],
                "Малая группа скрыта" if survey["suppressed"] else "",
            )
    for result in data["survey_results"]:
        row(
            "Результаты опросов",
            result["question"],
            result["average"],
            "Меньше пяти участников" if result["suppressed"] else result["survey"],
        )
        for option in result["distribution"] or []:
            row(
                "Варианты ответа",
                result["question"] + ": " + option["label"],
                option["count"],
                result["survey"],
            )
    for point in data["trend"]:
        for key, label in (
            ("questions", "Вопросы"),
            ("automatic", "Ответы Корсика"),
            ("handoff", "Передано HR"),
        ):
            row(
                "Динамика",
                point["date"] + ": " + label,
                point[key],
                f"Интервал {data['trend_bucket_days']} дн.",
            )
    return Response(
        "\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="hr-analytics.csv"'},
    )
