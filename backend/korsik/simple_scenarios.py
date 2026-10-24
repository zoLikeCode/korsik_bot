"""Small, opt-in message/answer graph. Legacy calendar routes are unchanged.

All state changes join the caller's transaction; nothing in this module sends
network requests. A MAX receipt, rather than queue insertion, advances a message.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from .common import employee_is_enabled

KINDS = {"MESSAGE", "ANSWER", "SURVEY", "END"}
KEY = re.compile(r"[a-zA-Z0-9_.:-]{1,120}\Z")
TERMINAL = {"COMPLETED", "DECLINED", "EXPIRED", "CANCELLED"}
FREE_ANSWER_PROMPT = "Дай, пожалуйста, ответ в свободной формулировке."


def is_simple(graph):
    return graph.get("editor_mode") == "SIMPLE"


def default_graph():
    from .db import new_id

    key = "message_" + new_id().replace("-", "")[:12]
    return {
        "schema_version": "korsik-scenario-v2",
        "algorithm_version": "calendar-v1",
        "editor_mode": "SIMPLE",
        "start_policy": {"condition": "ON_BOT_START", "timezone": "Europe/Moscow"},
        "resume_policy": {"reminder_hours": 24, "max_reminders": 3},
        "start_step_key": key,
        "source_mapping": [],
        "steps": [
            {
                "step_key": key,
                "kind": "MESSAGE",
                "sequence_no": 1,
                "trigger": {"type": "AFTER_PREVIOUS"},
                "content": {
                    "text_ty": "Добро пожаловать!",
                    "text_vy": "Добро пожаловать!",
                },
                "transitions": [],
            }
        ],
        "editor_layout": {},
    }


def answer_nodes(graph, step):
    by_key = {s["step_key"]: s for s in graph.get("steps", [])}
    return [
        by_key[t["target_step_key"]]
        for t in step.get("transitions", [])
        if t.get("target_step_key") in by_key
        and by_key[t["target_step_key"]].get("kind") == "ANSWER"
    ]


def choice_key(step_key):
    return "choice:" + step_key


def complete_choice(graph, step_key, response):
    value = response.get("value") if isinstance(response, dict) else response
    chosen = next(
        (s for s in graph.get("steps", []) if s.get("step_key") == value), None
    )
    return bool(chosen) and (
        not chosen.get("answer", {}).get("free_text")
        or (
            isinstance(response, dict)
            and isinstance(response.get("free_text"), str)
            and bool(response["free_text"].strip())
        )
    )


def selected_path(graph, answers):
    by_key = {s["step_key"]: s for s in graph.get("steps", [])}
    result = []
    key = graph.get("start_step_key")
    while key in by_key and key not in result and len(result) <= 500:
        result.append(key)
        step = by_key[key]
        choices = answer_nodes(graph, step) if step["kind"] == "MESSAGE" else []
        if choices:
            value = answers.get(choice_key(key), {})
            value = value.get("value") if isinstance(value, dict) else value
            if not complete_choice(graph, key, answers.get(choice_key(key), {})):
                value = None
            key = value if value in {a["step_key"] for a in choices} else None
        else:
            transitions = step.get("transitions", [])
            key = transitions[0].get("target_step_key") if transitions else None
    return result


def validate(graph):
    from .scenario_engine import semantic_hash, VARIABLES

    errors = []

    def err(path, message):
        errors.append({"path": path, "message": message})

    if graph.get("schema_version") != "korsik-scenario-v2":
        err("schema_version", "Неподдерживаемая версия DSL")
    if graph.get("algorithm_version") != "calendar-v1":
        err("algorithm_version", "Неизвестный алгоритм календаря")
    start = graph.get("start_policy", {})
    if not isinstance(start, dict):
        start = {}
    condition = start.get("condition")
    if not isinstance(condition, str) or condition not in {
        "ON_BOT_START",
        "START_DATE",
        "SCHEDULED",
        "RECURRING",
    }:
        err("start_policy.condition", "Выберите условие запуска")
    try:
        ZoneInfo(start.get("timezone", "Europe/Moscow"))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        err("start_policy.timezone", "Неизвестный часовой пояс")
    if condition == "SCHEDULED":
        try:
            date.fromisoformat(start.get("date", ""))
            if not re.fullmatch(r"\d{2}:\d{2}", start.get("time", "")):
                raise ValueError()
            time.fromisoformat(start["time"])
        except (ValueError, TypeError):
            err("start_policy", "Укажите дату календаря и время запуска")
    if condition == "RECURRING":
        if type(start.get("every_days")) is not int or not 1 <= start["every_days"] <= 365:
            err("start_policy.every_days", "Повторять каждые 1–365 дней")
        try:
            if not re.fullmatch(r"\d{2}:\d{2}", start.get("time", "")):
                raise ValueError()
            time.fromisoformat(start["time"])
        except (ValueError, TypeError):
            err("start_policy.time", "Укажите время повтора")
    resume = graph.get("resume_policy", {})
    hours = resume.get("reminder_hours", 24) if isinstance(resume, dict) else None
    if type(hours) is not int or not 1 <= hours <= 168:
        err("resume_policy.reminder_hours", "Повтор — от 1 до 168 часов")
    if (
        isinstance(resume, dict)
        and "max_reminders" in resume
        and (
            type(resume["max_reminders"]) is not int
            or not 0 <= resume["max_reminders"] <= 100
        )
    ):
        err(
            "resume_policy.max_reminders",
            "Количество повторов — целое число от 0 до 100",
        )
    steps = graph.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 500:
        err("steps", "В схеме должно быть от 1 до 500 блоков")
        return {"valid": False, "errors": errors, "warnings": []}
    if any(not isinstance(s, dict) for s in steps):
        err("steps", "Блок должен быть объектом")
        return {"valid": False, "errors": errors, "warnings": []}
    keys = [s.get("step_key") for s in steps]
    if any(not isinstance(k, str) or not KEY.fullmatch(k) for k in keys):
        err("steps.step_key", "Некорректный ID блока")
        return {"valid": False, "errors": errors, "warnings": []}
    if len(set(keys)) != len(keys):
        err("steps", "ID блоков повторяются")
    by_key = {s["step_key"]: s for s in steps}
    if (
        not isinstance(graph.get("start_step_key"), str)
        or graph.get("start_step_key") not in by_key
    ):
        err("start_step_key", "Начальный блок отсутствует")
    elif by_key[graph["start_step_key"]].get("kind") == "ANSWER":
        err("start_step_key", "Сценарий не может начинаться с ответа")
    parents = {k: [] for k in keys}
    edges = {k: [] for k in keys}
    for i, step in enumerate(steps):
        p = f"steps[{i}]"
        kind = step.get("kind")
        if not isinstance(kind, str) or kind not in KINDS:
            err(p + ".kind", "Допустимы Сообщение, Ответ, Опрос и Завершение")
        for forbidden in (
            "script",
            "code",
            "python",
            "javascript",
            "http_request",
            "secret",
        ):
            if forbidden in step:
                err(p, "Исполняемый код в программе запрещён")
        transitions = step.get("transitions", [])
        if not isinstance(transitions, list) or any(
            not isinstance(t, dict) for t in transitions
        ):
            err(p + ".transitions", "Ожидался список связей")
            continue
        targets = [t.get("target_step_key") for t in transitions]
        if any(
            t.get("condition", {"kind": "ALWAYS"}) != {"kind": "ALWAYS"}
            for t in transitions
        ):
            err(p + ".transitions", "Условия ветвления задаются блоками Ответ")
        if len(set(t for t in targets if isinstance(t, str))) != len(targets):
            err(p + ".transitions", "Связи повторяются или некорректны")
        for target in targets:
            if not isinstance(target, str) or target not in by_key:
                err(p + ".transitions", "Связь ведёт к отсутствующему блоку")
                continue
            parents[target].append(step["step_key"])
            edges[step["step_key"]].append(target)
        target_kinds = [
            by_key[t].get("kind") for t in targets if isinstance(t, str) and t in by_key
        ]
        if kind == "MESSAGE" and "ANSWER" in target_kinds:
            if any(k != "ANSWER" for k in target_kinds) or len(targets) > 10:
                err(
                    p + ".transitions",
                    "Сообщение связано либо с 1–10 ответами, либо с одним следующим блоком",
                )
        elif len(targets) > 1:
            err(p + ".transitions", "У блока может быть только один следующий блок")
        if kind == "END" and targets:
            err(p + ".transitions", "Завершение не имеет выходов")
        content = step.get("content", {})
        if not isinstance(content, dict):
            err(p + ".content", "Ожидался объект содержания")
            content = {}
        if kind == "MESSAGE":
            if not any(
                isinstance(content.get(k), str) and content[k].strip()
                for k in ("text_ty", "text_vy")
            ):
                err(p + ".content", "Введите текст сообщения")
            for k in ("text_ty", "text_vy"):
                if k in content and (
                    not isinstance(content[k], str) or len(content[k]) > 3900
                ):
                    err(p + ".content." + k, "Текст — до 3900 символов")
                if isinstance(content.get(k), str):
                    for variable in re.findall(
                        r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}", content[k]
                    ):
                        if variable not in VARIABLES:
                            err(p + ".content", f"Неизвестная переменная {variable}")
            prompts = content.get("custom_prompts", [])
            if not isinstance(prompts, list) or len(prompts) > 12:
                err(p + ".content.custom_prompts", "Допустимо до 12 формулировок")
            else:
                prompt_ids = set()
                for prompt in prompts:
                    if not isinstance(prompt, dict):
                        err(
                            p + ".content.custom_prompts",
                            "Формулировка должна быть объектом",
                        )
                        continue
                    pid = prompt.get("id")
                    if (
                        not isinstance(pid, str)
                        or not KEY.fullmatch(pid)
                        or pid in prompt_ids
                    ):
                        err(
                            p + ".content.custom_prompts",
                            "Нужен уникальный ID формулировки",
                        )
                    else:
                        prompt_ids.add(pid)
                    if (
                        not isinstance(prompt.get("label"), str)
                        or not 1 <= len(prompt["label"].strip()) <= 80
                    ):
                        err(
                            p + ".content.custom_prompts",
                            "Название формулировки — 1–80 символов",
                        )
                    if (
                        not isinstance(prompt.get("query_text"), str)
                        or not 1 <= len(prompt["query_text"].strip()) <= 8000
                    ):
                        err(
                            p + ".content.custom_prompts",
                            "Текст вопроса — 1–8000 символов",
                        )
        if kind == "ANSWER":
            answer = step.get("answer", {})
            if not isinstance(answer, dict) or not any(
                isinstance(answer.get(k), str) and answer[k].strip()
                for k in ("label_ty", "label_vy")
            ):
                err(p + ".answer", "Введите подпись ответа")
            elif any(
                k in answer and (not isinstance(answer[k], str) or len(answer[k]) > 80)
                for k in ("label_ty", "label_vy")
            ):
                err(p + ".answer", "Подпись ответа — до 80 символов")
            if (
                isinstance(answer, dict)
                and "free_text" in answer
                and type(answer["free_text"]) is not bool
            ):
                err(p + ".answer.free_text", "Свободный ответ — логическое значение")
        if kind == "SURVEY" and not step.get("survey_template_version_id"):
            err(p + ".survey_template_version_id", "Выберите опубликованный опрос")
        if kind in ("MESSAGE", "SURVEY"):
            trigger = step.get("trigger", {})
            if not isinstance(trigger, dict):
                trigger = {}
            if trigger.get("type") == "AFTER_PREVIOUS":
                if any(
                    k in trigger
                    for k in (
                        "offset_days",
                        "local_time",
                        "offset_months",
                        "workday_number",
                    )
                ):
                    err(
                        p + ".trigger",
                        "После предыдущего блока сдвиг и время не задаются",
                    )
            elif trigger.get("type") == "ANCHOR_TIME":
                if (
                    type(trigger.get("offset_days", 0)) is not int
                    or not -365 <= trigger.get("offset_days", 0) <= 365
                ):
                    err(
                        p + ".trigger.offset_days", "Сдвиг — целое число от -365 до 365"
                    )
                try:
                    if not re.fullmatch(r"\d{2}:\d{2}", trigger.get("local_time", "")):
                        raise ValueError()
                    time.fromisoformat(trigger["local_time"])
                except (ValueError, TypeError):
                    err(p + ".trigger.local_time", "Укажите время запуска")
            else:
                err(
                    p + ".trigger", "Выберите после предыдущего блока или сдвиг по дням"
                )
    for step in steps:
        if step.get("kind") == "ANSWER" and (
            not parents[step["step_key"]]
            or any(
                by_key[k].get("kind") != "MESSAGE" for k in parents[step["step_key"]]
            )
        ):
            err("steps." + step["step_key"], "Ответ должен быть связан с сообщением")
    colors = {}

    def visit(key):
        if colors.get(key) == 1:
            err("transitions", "В схеме обнаружен цикл")
            return
        if colors.get(key) == 2:
            return
        colors[key] = 1
        for target in edges[key]:
            visit(target)
        colors[key] = 2

    for key in keys:
        visit(key)
    reachable = set()

    def reach(key):
        if key in reachable or key not in edges:
            return
        reachable.add(key)
        for target in edges[key]:
            reach(target)

    if isinstance(graph.get("start_step_key"), str):
        reach(graph.get("start_step_key"))
    if set(keys) - reachable:
        err("steps", "Все блоки должны быть связаны с началом схемы")
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": [],
        "semantic_hash": semantic_hash(graph),
    }


def prepare_graph(db, actor, graph):
    """Resolve a published, scoped survey into an immutable scenario snapshot."""
    from . import domain as d, survey_campaigns as sc

    graph = deepcopy(graph)
    steps = graph.get("steps", [])
    if not isinstance(steps, list):
        return graph
    for step in steps:
        if not isinstance(step, dict) or step.get("kind") != "SURVEY":
            continue
        version_id = step.get("survey_template_version_id")
        if not isinstance(version_id, str) or not isinstance(step.get("step_key"), str):
            continue  # Bounded incomplete drafts remain saveable.
        version = sc._version(db, actor, version_id)
        if version.status not in {"PUBLISHED", "ARCHIVED"}:
            raise HTTPException(
                422, "В сценарий можно включить только опубликованный опрос"
            )
        definition = sc._definition(version)
        # A template may occur twice in a graph; each instance owns its answers.
        prefix = step["step_key"] + ":"
        definition["survey_key"] = prefix + definition["survey_key"]
        definition["start_question_key"] = prefix + definition["start_question_key"]
        for question in definition["questions"]:
            question["question_key"] = prefix + question["question_key"]
            if question.get("next_question_key"):
                question["next_question_key"] = prefix + question["next_question_key"]
        step["survey"] = definition
        step["survey_template_title"] = version.data["title"]
        template = d.get_entity(db, version.parent_id, "survey_template")
        step["survey_template_scope"] = deepcopy(template.data["scope"])
    return graph


def launch_at(graph, employee, journey_data, clock):
    from .scenario_engine import local_datetime

    policy = graph["start_policy"]
    zone = policy.get("timezone", employee.get("timezone", "Europe/Moscow"))
    condition = policy["condition"]
    if condition == "ON_BOT_START":
        value = journey_data.get("started_at")
        return datetime.fromisoformat(value) if value else None
    if condition == "RECURRING":
        from .scenario_recurrence import next_occurrence
        value = journey_data.get("occurrence_at")
        return datetime.fromisoformat(value) if value else next_occurrence(policy, clock)
    if condition == "SCHEDULED":
        return local_datetime(date.fromisoformat(policy["date"]), policy["time"], zone)
    if not employee.get("start_date"):
        return None
    return local_datetime(date.fromisoformat(employee["start_date"]), "00:00", zone)


def due_for(graph, step, employee, state, clock):
    from .scenario_engine import local_datetime

    launch = launch_at(graph, employee, state, clock)
    if launch is None:
        return None
    path = selected_path(graph, state.get("answers", {}))
    key = step["step_key"]
    if key not in path:
        return None
    previous = path[path.index(key) - 1] if path.index(key) else None
    # Control blocks and immediate sends follow accepted completion, never queue insertion.
    if (
        step.get("kind") in {"ANSWER", "END"}
        or step.get("trigger", {}).get("type") == "AFTER_PREVIOUS"
    ):
        if previous is None:
            return launch
        value = state.get("events", {}).get(previous + ":STEP_COMPLETED")
        return datetime.fromisoformat(value) if value else None
    trigger = step.get("trigger", {})
    zone = graph["start_policy"].get(
        "timezone", employee.get("timezone", "Europe/Moscow")
    )
    day = launch.astimezone(ZoneInfo(zone)).date() + timedelta(
        days=trigger.get("offset_days", 0)
    )
    due = local_datetime(day, trigger.get("local_time", "10:00"), zone)
    if graph["start_policy"]["condition"] != "START_DATE":
        due = max(due, launch)
    if previous:
        completed = state.get("events", {}).get(previous + ":STEP_COMPLETED")
        if not completed:
            return None
        due = max(due, datetime.fromisoformat(completed))
    return due


def simulate(graph, employee, answers=None, clock=None):
    result = validate(graph)
    if not result["valid"]:
        return {
            **result,
            "timeline": [],
            "simulation_only": True,
            "outbound_messages_created": 0,
        }
    clock = clock or datetime.now(timezone.utc)
    state = {"answers": answers or {}, "events": {}, "started_at": clock.isoformat()}
    path = selected_path(graph, state["answers"])
    by_key = {s["step_key"]: s for s in graph["steps"]}
    rows = []
    for key in path:
        step = by_key[key]
        due = due_for(graph, step, employee, state, clock)
        choices = answer_nodes(graph, step) if step["kind"] == "MESSAGE" else []
        rows.append(
            {
                "step_key": key,
                "kind": step["kind"],
                "due_at": due.isoformat() if due else None,
                "status": (
                    "WAITING_FREE_TEXT"
                    if choices
                    and state["answers"].get(choice_key(key))
                    and not complete_choice(
                        graph, key, state["answers"][choice_key(key)]
                    )
                    else (
                        "WAITING_ANSWER"
                        if choices and not state["answers"].get(choice_key(key))
                        else "READY" if due else "WAITING_EVENT"
                    )
                ),
                "reason": step.get("trigger"),
                "answer_options": [a["step_key"] for a in choices],
            }
        )
        if (
            due
            and not (
                choices
                and not complete_choice(
                    graph, key, state["answers"].get(choice_key(key))
                )
            )
            and step["kind"] != "SURVEY"
        ):
            state["events"][key + ":STEP_COMPLETED"] = due.isoformat()
    for step in graph["steps"]:
        if step["step_key"] not in path:
            rows.append(
                {
                    "step_key": step["step_key"],
                    "kind": step["kind"],
                    "due_at": None,
                    "status": "NOT_SELECTED_OR_WAITING_ANSWER",
                    "reason": step.get("trigger"),
                }
            )
    return {
        **result,
        "timeline": rows,
        "employee": employee,
        "simulation_only": True,
        "outbound_messages_created": 0,
    }


def enroll(db, employee, scenario):
    from . import domain as d

    # Lock the employee aggregate: publication, bot start and HR enrollment may race.
    employee = d.get_entity(db, employee.id, "employee", lock=True)
    if employee.status == "DELETED":
        return None
    scenario = d.get_entity(db, scenario.id, "scenario", lock=True)
    version = d.get_entity(db, scenario.data["published_version_id"], "scenario_version")
    if version.data["graph"].get("start_policy", {}).get("condition") == "RECURRING":
        from .scenario_recurrence import ensure
        return ensure(db, employee, scenario, version)
    episode = employee.data.get("episode_id")
    existing = next(
        (
            j
            for j in d.entities(db, "journey", employee.id)
            if j.data.get("episode_id") == episode
            and j.data.get("scenario_id") == scenario.id
        ),
        None,
    )
    if existing:
        return existing
    if scenario.status == "ARCHIVED":
        raise HTTPException(409, "Удалённая схема не назначается новым сотрудникам")
    journey = d.save_entity(
        db,
        "journey",
        {
            "employee_id": employee.id,
            "episode_id": episode,
            "scenario_id": scenario.id,
            "version_id": scenario.data["published_version_id"],
            "schedule_revision": 1,
            "signals": {},
            "events": {},
            "answers": {},
            "completed_steps": [],
            "last_step_key": None,
            "waiting_step_key": None,
        },
        status="SCHEDULED",
        parent_id=employee.id,
    )
    plan(db, journey, employee)
    return journey


def notify_bot_started(db, employee):
    from . import domain as d

    if (
        employee.data.get("max_binding_status") != "VERIFIED"
        or employee.data.get("employment_stage") == "ENDED"
        or employee.data.get("enabled", True) is False
    ):
        return []
    journeys = []
    # Archiving a parent blocks new enrollments, not its already pinned journeys.
    for journey in d.entities(db, "journey", employee.id):
        if (
            journey.data.get("episode_id") != employee.data.get("episode_id")
            or journey.status not in {"SCHEDULED", "ACTIVE"}
            or journey.data.get("started_at")
        ):
            continue
        pinned = db.get(d.Entity, journey.data.get("version_id"))
        scenario = db.get(d.Entity, journey.data.get("scenario_id"))
        if (
            not pinned
            or not is_simple(pinned.data.get("graph", {}))
            or pinned.data["graph"].get("start_policy", {}).get("condition")
            != "ON_BOT_START"
            or not scenario
            or not d._scenario_applies(scenario, employee)
        ):
            continue
        d.update(journey, started_at=d.now().isoformat())
        plan(db, journey, employee)
        journeys.append(journey)
    for scenario in d.entities(db, "scenario"):
        if scenario.status == "ARCHIVED":
            continue
        version = db.get(d.Entity, scenario.data.get("published_version_id"))
        if (
            not version
            or not is_simple(version.data.get("graph", {}))
            or not d._scenario_applies(scenario, employee)
        ):
            continue
        if (
            version.data["graph"].get("start_policy", {}).get("condition")
            != "ON_BOT_START"
        ):
            continue
        try:
            journey = enroll(db, employee, scenario)
        except HTTPException as error:
            if error.status_code == 409 and scenario.status == "ARCHIVED":
                continue
            raise
        if journey is None:
            continue
        pinned = db.get(d.Entity, journey.data.get("version_id"))
        if not pinned or not is_simple(pinned.data.get("graph", {})):
            continue  # A new publication never rewrites an existing legacy enrollment.
        if journey.status in {"CANCELLED", "COMPLETED"}:
            continue
        if not journey.data.get("started_at"):
            d.update(journey, started_at=d.now().isoformat())
            plan(db, journey, employee)
            journeys.append(journey)
    return journeys


def enroll_future_programs(db):
    """Only a human publication activates future, non-retroactive schedules."""
    from . import domain as d

    count = 0
    for scenario in d.entities(db, "scenario"):
        if scenario.status == "ARCHIVED":
            continue
        version = db.get(d.Entity, scenario.data.get("published_version_id"))
        if (
            not version
            or version.status != "PUBLISHED"
            or not version.data.get("activation_at")
        ):
            continue
        graph = version.data.get("graph", {})
        if not is_simple(graph):
            continue
        policy = graph["start_policy"]
        if policy["condition"] not in {"START_DATE", "SCHEDULED", "RECURRING"}:
            continue
        activated = max(
            d.aware(version.data["activation_at"]),
            d.aware(scenario.data.get("restored_at") or version.data["activation_at"]),
        )
        zone = ZoneInfo(policy.get("timezone", "Europe/Moscow"))
        if (
            policy["condition"] == "SCHEDULED"
            and launch_at(graph, {}, {}, d.now()) < activated
        ):
            continue
        for employee in d.entities(db, "employee"):
            if (
                employee.data.get("max_binding_status") != "VERIFIED"
                or employee.data.get("employment_stage") == "ENDED"
                or employee.data.get("enabled", True) is False
                or not d._scenario_applies(scenario, employee)
            ):
                continue
            if policy["condition"] == "RECURRING":
                from .scenario_recurrence import ensure
                if ensure(db, employee, scenario, version) is not None:
                    count += 1
                continue
            if policy["condition"] == "START_DATE" and (
                not employee.data.get("start_date")
                or date.fromisoformat(employee.data["start_date"])
                < activated.astimezone(zone).date()
            ):
                continue
            existing = next(
                (
                    j
                    for j in d.entities(db, "journey", employee.id)
                    if j.data.get("episode_id") == employee.data.get("episode_id")
                    and j.data.get("scenario_id") == scenario.id
                ),
                None,
            )
            if existing:
                continue
            try:
                journey = enroll(db, employee, scenario)
            except HTTPException as error:
                if error.status_code == 409 and scenario.status == "ARCHIVED":
                    break
                raise
            if journey is not None:
                count += 1
    return count


def plan(db, journey, employee, bump_revision=False):
    from . import domain as d

    version = d.get_entity(db, journey.data["version_id"], "scenario_version")
    graph = version.data["graph"]
    revision = journey.data.get("schedule_revision", 1) + int(bump_revision)
    if bump_revision:
        from sqlalchemy import select
        from .models import Outbox, TransportDelivery

        outbounds = [
            o
            for o in db.scalars(
                select(Outbox).where(Outbox.recipient_id == employee.id)
            )
            if (o.data or {}).get("journey_id") == journey.id
        ]
        if any(
            o.status in {"SENDING", "UNKNOWN_DELIVERY", "UNKNOWN"} for o in outbounds
        ):
            journey.status = "PAUSED"
            d.update(
                journey,
                requires_hr_review=True,
                review_reason="DELIVERY_RECONCILIATION_REQUIRED",
            )
            return
        for outbound in outbounds:
            if outbound.status in {"PENDING", "READY", "RETRY", "QUEUED"}:
                outbound.status = "CANCELLED"
                for delivery in db.scalars(
                    select(TransportDelivery).where(
                        TransportDelivery.outbox_id == outbound.id,
                        TransportDelivery.status.in_(["PENDING", "RETRY"]),
                    )
                ):
                    delivery.status = "CANCELLED"
                    delivery.error_code = "STALE_SCHEDULE"
                action_id = outbound.data.get("action_id")
                action = db.get(d.Entity, action_id) if action_id else None
                if action and action.status == "ENQUEUED":
                    action.status = "PLANNED"
    d.update(journey, schedule_revision=revision)
    existing = {
        a.data["step_key"]: a for a in d.entities(db, "scheduled_action", journey.id)
    }
    for step in graph["steps"]:
        old = existing.get(step["step_key"])
        if old and old.status != "PLANNED":
            continue
        due = due_for(graph, step, employee.data, journey.data, d.now())
        data = {
            "employee_id": employee.id,
            "journey_id": journey.id,
            "version_id": version.id,
            "step_key": step["step_key"],
            "sequence_no": step.get("sequence_no", 0),
            "schedule_revision": revision,
            "due_at": due.isoformat() if due else None,
            "step": deepcopy(step),
            "applicable": True,
        }
        if old:
            d.update(old, **data)
        else:
            d.save_entity(
                db, "scheduled_action", data, status="PLANNED", parent_id=journey.id
            )
    db.flush()


def message_buttons(db, journey, graph, step, employee):
    from . import domain as d

    buttons = []
    for answer in answer_nodes(graph, step):
        labels = answer.get("answer", {})
        content = {
            "text_ty": labels.get("label_ty") or labels.get("label_vy", ""),
            "text_vy": labels.get("label_vy") or labels.get("label_ty", ""),
        }
        buttons.append(
            {
                "text": d.render_text(content, employee),
                "payload": {
                    "action": "SCENARIO_CHOICE",
                    "journey_id": journey.id,
                    "step_key": step["step_key"],
                    "answer_key": answer["step_key"],
                },
            }
        )
    # Scenario choices and explicitly authored formulations own this keyboard.
    # Global knowledge topics belong to the conversational menu, never this run.
    seen_labels = {button["text"].strip().casefold() for button in buttons}
    for prompt in step.get("content", {}).get("custom_prompts", []):
        label = prompt["label"].strip().casefold()
        if label in seen_labels:
            continue
        seen_labels.add(label)
        buttons.append(
            {
                "text": prompt["label"],
                "payload": {
                    "action": "SCENARIO_FORMULATION",
                    "journey_id": journey.id,
                    "step_key": step["step_key"],
                    "prompt_id": prompt["id"],
                },
            }
        )
    return buttons


def _complete(db, journey, action, clock):
    from . import domain as d

    action.status = "COMPLETED"
    d.update(action, completed_at=clock.isoformat())
    key = action.data["step_key"]
    d.update(
        journey,
        events={
            **journey.data.get("events", {}),
            key + ":STEP_COMPLETED": clock.isoformat(),
        },
        completed_steps=list(
            dict.fromkeys(journey.data.get("completed_steps", []) + [key])
        ),
        last_step_key=key,
        waiting_step_key=None,
        waiting_since=None,
        last_reminder_at=None,
        last_reminder_enqueued_at=None,
        reminder_sequence=max(
            journey.data.get("reminder_sequence", 0),
            journey.data.get("reminder_count", 0),
        ),
        reminder_step_key=None,
        reminder_count=0,
    )


def _waiting(db, journey, step_key, clock):
    from . import domain as d

    if journey.data.get("waiting_step_key") != step_key:
        d.update(
            journey,
            waiting_step_key=step_key,
            waiting_since=clock.isoformat(),
            last_step_key=step_key,
            last_reminder_at=None,
            last_reminder_enqueued_at=None,
            reminder_sequence=max(
                journey.data.get("reminder_sequence", 0),
                journey.data.get("reminder_count", 0),
            ),
            reminder_step_key=step_key,
            reminder_count=0,
        )


def _resume_waiting(db, journey, graph, employee, actions, clock):
    from . import domain as d
    from .models import Outbox
    from sqlalchemy import select

    key = journey.data.get("waiting_step_key")
    action = actions.get(key)
    if not action or action.status not in {"WAITING_ANSWER", "WAITING_SURVEY"}:
        return
    if journey.data.get("reminder_step_key") != key:
        d.update(
            journey,
            reminder_sequence=max(
                journey.data.get("reminder_sequence", 0),
                journey.data.get("reminder_count", 0),
            ),
            reminder_step_key=key,
            reminder_count=0,
        )
    cap = graph.get("resume_policy", {}).get("max_reminders")
    if cap is not None and journey.data.get("reminder_count", 0) >= cap:
        return
    since = (
        journey.data.get("last_reminder_at")
        or journey.data.get("last_reminder_enqueued_at")
        or journey.data.get("waiting_since")
    )
    if not since or clock - d.aware(since) < timedelta(
        hours=graph.get("resume_policy", {}).get("reminder_hours", 24)
    ):
        return
    reminders = [
        o
        for o in db.scalars(select(Outbox).where(Outbox.recipient_id == employee.id))
        if (o.data or {}).get("simple_resume_journey_id") == journey.id
        and (o.data or {}).get("step_key") == key
        and (o.data or {}).get("schedule_revision")
        == journey.data.get("schedule_revision")
    ]
    if any(
        o.status
        in {
            "PENDING",
            "READY",
            "QUEUED",
            "SENDING",
            "RETRY",
            "UNKNOWN",
            "UNKNOWN_DELIVERY",
        }
        for o in reminders
    ):
        return
    latest_reminder = max(
        reminders,
        key=lambda o: (o.data or {}).get("simple_resume_sequence", 0),
        default=None,
    )
    if latest_reminder and latest_reminder.status in {
        "FAILED",
        "REJECTED",
        "CANCELLED",
        "SUPPRESSED",
    }:
        journey.status = "PAUSED"
        d.update(
            journey,
            requires_hr_review=True,
            review_reason="DELIVERY_FAILED",
            review_step_key=key,
        )
        return
    sequence = journey.data.get("reminder_sequence", 0) + 1
    step = action.data["step"]
    if action.status == "WAITING_ANSWER":
        context = {
            "journey_id": journey.id,
            "simple_resume_journey_id": journey.id,
            "simple_resume": True,
            "simple_resume_sequence": sequence,
            "step_key": key,
            "schedule_revision": journey.data["schedule_revision"],
        }
        capture = pending_free_answer(db, employee, d.conversation_for(db, employee.id))
        if (
            capture
            and capture.data.get("journey_id") == journey.id
            and capture.data.get("step_key") == key
        ):
            d.emit(
                db,
                employee.id,
                FREE_ANSWER_PROMPT,
                unique_key=f"simple:{journey.id}:resume:{sequence}",
                data={
                    **context,
                    "conversation_id": capture.data["conversation_id"],
                    "simple_free_answer_capture_id": capture.id,
                },
            )
        elif not _emit_message(
            db,
            journey,
            graph,
            step,
            employee,
            f"simple:{journey.id}:resume:{sequence}",
            context,
        ):
            return
    else:
        run = db.get(d.Entity, action.data.get("survey_run_id"))
        if not run or run.status in TERMINAL:
            return
        d.emit(
            db,
            employee.id,
            "Продолжим опрос с последнего вопроса?",
            [
                {
                    "text": "Продолжить опрос",
                    "payload": {"action": "SURVEY_RESUME", "survey_run_id": run.id},
                }
            ],
            f"simple:{journey.id}:resume:{sequence}",
            {
                "journey_id": journey.id,
                "simple_resume_journey_id": journey.id,
                "survey_run_id": run.id,
                "simple_resume": True,
                "simple_resume_sequence": sequence,
                "step_key": key,
                "schedule_revision": journey.data["schedule_revision"],
            },
        )
    d.update(
        journey, last_reminder_enqueued_at=clock.isoformat(), reminder_sequence=sequence
    )


def accept_resume(db, context, accepted_at):
    """Count a repeat only after an actual MAX receipt, once per domain context."""
    from . import domain as d
    from sqlalchemy import select

    journey = db.scalar(
        select(d.Entity)
        .where(d.Entity.id == context.get("journey_id"), d.Entity.kind == "journey")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not journey
        or journey.kind != "journey"
        or journey.status in {"CANCELLED", "COMPLETED"}
    ):
        return False
    key = context.get("step_key")
    if journey.data.get("waiting_step_key") != key or context.get(
        "schedule_revision"
    ) != journey.data.get("schedule_revision"):
        return False
    employee = db.get(d.Entity, journey.parent_id)
    if not employee or employee.data.get("episode_id") != journey.data.get(
        "episode_id"
    ):
        return False
    count = (
        journey.data.get("reminder_count", 0)
        if journey.data.get("reminder_step_key") == key
        else 0
    )
    d.update(
        journey,
        reminder_step_key=key,
        reminder_count=count + 1,
        last_reminder_at=accepted_at.isoformat(),
    )
    return True


def _emit_message(db, journey, graph, step, employee, unique_key, context):
    from . import domain as d
    from .retrieval import scope_matches

    text = d.render_text(step.get("content", {}), employee)
    reason = "MESSAGE_LENGTH_EXCEEDED" if not text or len(text) > 3900 else None
    asset_ids = step.get("content", {}).get("asset_ids", [])
    if (
        not isinstance(asset_ids, list)
        or len(asset_ids) > 10
        or any(not isinstance(a, str) for a in asset_ids)
    ):
        reason = "SCENARIO_ASSET_INVALID"
        asset_ids = []
    for asset_id in asset_ids:
        asset = db.get(d.Entity, asset_id)
        if (
            not asset
            or asset.kind != "asset"
            or asset.status not in {"READY", "APPROVED"}
            or asset.data.get("purpose") != "SCENARIO_MATERIAL"
            or not scope_matches(asset.data.get("scope", {}), employee)
        ):
            reason = "SCENARIO_ASSET_UNAVAILABLE"
    if reason:
        journey.status = "PAUSED"
        d.update(
            journey,
            requires_hr_review=True,
            review_reason=reason,
            review_step_key=step["step_key"],
        )
        return None
    outbound = d.emit(
        db,
        employee.id,
        text,
        message_buttons(db, journey, graph, step, employee),
        unique_key,
        context,
    )
    outbound.attachments = list(dict.fromkeys(asset_ids))
    return outbound


def run_due(db, journey, employee, clock):
    from . import domain as d

    if (
        journey.status not in {"SCHEDULED", "ACTIVE"}
        or journey.data.get("episode_id") != employee.data.get("episode_id")
        or employee.data.get("max_binding_status") != "VERIFIED"
        or employee.data.get("employment_stage") == "ENDED"
        or employee.data.get("enabled", True) is False
    ):
        return 0
    conversation = d.conversation_for(db, employee.id)
    if conversation and conversation.data.get("mode", "BOT") != "BOT":
        return 0
    graph = d.get_entity(db, journey.data["version_id"], "scenario_version").data[
        "graph"
    ]
    actions = {
        a.data["step_key"]: a for a in d.entities(db, "scheduled_action", journey.id)
    }
    path = selected_path(graph, journey.data.get("answers", {}))
    count = 0
    for key in path:
        action = actions[key]
        step = action.data["step"]
        if action.status in {"COMPLETED", "SKIPPED"}:
            continue
        if action.status == "WAITING_SURVEY":
            run = db.get(d.Entity, action.data.get("survey_run_id"))
            if run and run.status in TERMINAL:
                _complete(db, journey, action, clock)
                continue
            break
        if action.status in {"WAITING_ANSWER", "ENQUEUED", "SENDING", "UNKNOWN"}:
            if action.status == "ENQUEUED":
                from sqlalchemy import select
                from .models import Outbox

                outbound = next(
                    (
                        o
                        for o in db.scalars(
                            select(Outbox).where(Outbox.recipient_id == employee.id)
                        )
                        if (o.data or {}).get("action_id") == action.id
                        and (o.data or {}).get("schedule_revision")
                        == journey.data.get("schedule_revision")
                    ),
                    None,
                )
                if outbound and outbound.status in {
                    "REJECTED",
                    "FAILED",
                    "CANCELLED",
                    "SUPPRESSED",
                }:
                    journey.status = "PAUSED"
                    d.update(
                        journey,
                        requires_hr_review=True,
                        review_reason="DELIVERY_FAILED",
                        review_step_key=key,
                    )
            break
        due = due_for(graph, step, employee.data, journey.data, clock)
        if due is None or due > clock:
            break
        if (
            journey.data.get("access_resumed_at")
            and due < d.aware(journey.data["access_resumed_at"])
            and step.get("trigger", {}).get("type") == "ANCHOR_TIME"
            and not action.data.get("access_resume_current")
            and not answer_nodes(graph, step)
        ):
            d.skip_access_milestone(db, journey, action, clock)
            continue
        d.update(action, due_at=due.isoformat())
        if step["kind"] in {"ANSWER", "END"}:
            _complete(db, journey, action, clock)
            if step["kind"] == "END":
                journey.status = "COMPLETED"
                d.update(journey, completed_at=clock.isoformat())
                break
            continue
        if step["kind"] == "SURVEY":
            from .survey_campaigns import _scope_matches

            if not _scope_matches(
                step.get("survey_template_scope", {"all": True}), employee
            ):
                journey.status = "PAUSED"
                d.update(
                    journey,
                    requires_hr_review=True,
                    review_reason="SURVEY_SCOPE_CHANGED",
                )
                break
            run = d.open_survey(db, journey, step, employee)
            if not run:
                break
            action.status = "WAITING_SURVEY"
            d.update(action, survey_run_id=run.id)
            _waiting(db, journey, key, clock)
            count += 1
            break
        outbound = _emit_message(
            db,
            journey,
            graph,
            step,
            employee,
            f"action:{action.id}:schedule:{journey.data['schedule_revision']}",
            {
                "action_id": action.id,
                "journey_id": journey.id,
                "step_key": key,
                "schedule_revision": journey.data["schedule_revision"],
                "simple_scenario": True,
            },
        )
        if not outbound:
            break
        action.status = "ENQUEUED"
        d.update(action, access_resume_current=False)
        count += 1
        break
    else:
        if path and all(
            actions[key].status in {"COMPLETED", "SKIPPED"} for key in path
        ):
            journey.status = "COMPLETED"
            d.update(journey, completed_at=clock.isoformat())
    if journey.status != "COMPLETED":
        _resume_waiting(db, journey, graph, employee, actions, clock)
    return count


def accepted(db, journey, action, accepted_at):
    from . import domain as d

    if action.status in {"COMPLETED", "WAITING_ANSWER"}:
        return
    graph = d.get_entity(db, journey.data["version_id"], "scenario_version").data[
        "graph"
    ]
    step = action.data["step"]
    d.update(action, accepted_at=accepted_at.isoformat())
    d.update(
        journey,
        events={
            **journey.data.get("events", {}),
            step["step_key"] + ":ACTION_ACCEPTED": accepted_at.isoformat(),
        },
    )
    if answer_nodes(graph, step):
        action.status = "WAITING_ANSWER"
        _waiting(db, journey, step["step_key"], accepted_at)
    else:
        _complete(db, journey, action, accepted_at)
    plan(db, journey, d.get_entity(db, journey.parent_id, "employee"))


def _authorized_journey(db, employee, payload, lock=False):
    from . import domain as d

    journey = d.get_entity(db, payload.get("journey_id", ""), "journey", lock=lock)
    if (
        journey.parent_id != employee.id
        or journey.data.get("episode_id") != employee.data.get("episode_id")
        or employee.data.get("employment_stage") == "ENDED"
        or not employee_is_enabled(employee)
    ):
        raise HTTPException(404, "Сценарий недоступен")
    scenario = d.get_entity(db, journey.data["scenario_id"], "scenario")
    if not d._scenario_applies(scenario, employee) or journey.status == "CANCELLED":
        raise HTTPException(404, "Сценарий недоступен")
    version = d.get_entity(db, journey.data["version_id"], "scenario_version")
    graph = version.data["graph"]
    if not is_simple(graph):
        raise HTTPException(409, "Старая кнопка")
    return journey, graph


def cancel_free_answers(db, employee_id, reason):
    from . import domain as d

    for capture in d.entities(db, "scenario_free_answer", employee_id):
        if capture.status == "OPEN":
            capture.status = "CANCELLED"
            d.update(
                capture, cancellation_reason=reason, cancelled_at=d.now().isoformat()
            )


def pending_free_answer(db, employee, conversation):
    """Resolve server-owned capture; never trust a message-supplied journey ID."""
    from . import domain as d

    if (
        not conversation
        or conversation.status == "CLOSED"
        or conversation.data.get("mode", "BOT") != "BOT"
        or not employee_is_enabled(employee)
        or employee.data.get("max_binding_status") != "VERIFIED"
    ):
        return None
    captures = sorted(
        d.entities(db, "scenario_free_answer", employee.id),
        key=lambda row: d.aware(row.created_at),
        reverse=True,
    )
    for capture in captures:
        if capture.status != "OPEN":
            continue
        data = capture.data
        valid = (
            data.get("episode_id") == employee.data.get("episode_id")
            and data.get("conversation_id") == conversation.id
        )
        try:
            journey, graph = _authorized_journey(db, employee, data)
            valid = (
                valid
                and journey.status in {"ACTIVE", "SCHEDULED"}
                and journey.data.get("waiting_step_key") == data.get("step_key")
                and journey.data.get("schedule_revision")
                == data.get("schedule_revision")
            )
            step = next(
                (s for s in graph["steps"] if s["step_key"] == data.get("step_key")),
                None,
            )
            valid = (
                valid
                and step is not None
                and any(
                    a["step_key"] == data.get("answer_key")
                    and a.get("answer", {}).get("free_text")
                    for a in answer_nodes(graph, step)
                )
            )
            action = next(
                (
                    a
                    for a in d.entities(db, "scheduled_action", journey.id)
                    if a.data.get("step_key") == data.get("step_key")
                ),
                None,
            )
            valid = valid and action is not None and action.status == "WAITING_ANSWER"
        except HTTPException:
            valid = False
        # An explicit newer survey input owns subsequent text, never this capture.
        for other in d.entities(db, "input_capture", employee.id):
            if other.status in {"OPEN", "DRAFT"} and d.aware(
                other.created_at
            ) > d.aware(capture.created_at):
                valid = False
        if valid:
            return capture
        capture.status = "CANCELLED"
        d.update(capture, cancellation_reason="STALE_CONTEXT")
    return None


def consume_free_answer(db, employee, conversation, text, message_id):
    from . import domain as d

    capture = pending_free_answer(db, employee, conversation)
    if not capture:
        return None
    if not isinstance(text, str) or not text.strip() or len(text) > 8000:
        raise HTTPException(422, "Ответ должен содержать от 1 до 8000 символов")
    capture = d.get_entity(db, capture.id, "scenario_free_answer", lock=True)
    if capture.status != "OPEN":
        return None
    journey, _ = _authorized_journey(db, employee, capture.data, lock=True)
    action = next(
        a
        for a in d.entities(db, "scheduled_action", journey.id)
        if a.data.get("step_key") == capture.data["step_key"]
    )
    if (
        action.status != "WAITING_ANSWER"
        or journey.status not in {"ACTIVE", "SCHEDULED"}
        or journey.data.get("waiting_step_key") != capture.data["step_key"]
        or journey.data.get("schedule_revision")
        != capture.data.get("schedule_revision")
    ):
        capture.status = "CANCELLED"
        d.update(capture, cancellation_reason="STALE_CONTEXT")
        return None
    response = {
        "value": capture.data["answer_key"],
        "free_text": text,
        "outcome": "ANSWERED",
        "answered_at": d.now().isoformat(),
        "message_id": str(message_id),
    }
    d.update(
        journey,
        answers={
            **journey.data.get("answers", {}),
            choice_key(capture.data["step_key"]): response,
        },
    )
    capture.status = "ANSWERED"
    d.update(
        capture, text=text, message_id=str(message_id), answered_at=d.now().isoformat()
    )
    _complete(db, journey, action, d.now())
    plan(db, journey, employee)
    return {
        "journey_id": journey.id,
        "step_key": capture.data["step_key"],
        "answer_key": capture.data["answer_key"],
        "capture_id": capture.id,
    }


def choose(db, employee, payload):
    from . import domain as d

    journey, graph = _authorized_journey(db, employee, payload, lock=True)
    if employee.data.get("max_binding_status") != "VERIFIED":
        raise HTTPException(403, "Подключение MAX не подтверждено")
    conversation = d.conversation_for(db, employee.id)
    if conversation and conversation.data.get("mode", "BOT") != "BOT":
        raise HTTPException(409, "Сценарий приостановлен на время диалога")
    key, answer_key = payload.get("step_key"), payload.get("answer_key")
    existing = journey.data.get("answers", {}).get(choice_key(key or ""))
    if existing:
        if existing.get("value") != answer_key:
            raise HTTPException(409, "На это сообщение уже выбран другой ответ")
        return {"journey_id": journey.id, "answer_key": answer_key, "duplicate": True}
    if (
        journey.status not in {"SCHEDULED", "ACTIVE"}
        or journey.data.get("waiting_step_key") != key
    ):
        raise HTTPException(409, "Эта кнопка больше не ожидает ответа")
    step = next((s for s in graph["steps"] if s["step_key"] == key), None)
    if not step or answer_key not in {a["step_key"] for a in answer_nodes(graph, step)}:
        raise HTTPException(422, "Ответ не относится к этому сообщению")
    action = next(
        (
            a
            for a in d.entities(db, "scheduled_action", journey.id)
            if a.data["step_key"] == key
        ),
        None,
    )
    if not action or action.status != "WAITING_ANSWER":
        raise HTTPException(409, "Сообщение ещё не доставлено или уже обработано")
    chosen = next(s for s in graph["steps"] if s["step_key"] == answer_key)
    if chosen.get("answer", {}).get("free_text"):
        from .chat import conversation_for

        conversation = conversation_for(db, employee)
        capture = pending_free_answer(db, employee, conversation)
        if (
            capture
            and capture.data.get("journey_id") == journey.id
            and capture.data.get("step_key") == key
            and capture.data.get("answer_key") == answer_key
        ):
            return {
                "journey_id": journey.id,
                "answer_key": answer_key,
                "awaiting_free_text": True,
                "duplicate": True,
            }
        cancel_free_answers(db, employee.id, "REPLACED_BY_NEW_CHOICE")
        capture = d.save_entity(
            db,
            "scenario_free_answer",
            {
                "employee_id": employee.id,
                "episode_id": employee.data.get("episode_id"),
                "conversation_id": conversation.id,
                "journey_id": journey.id,
                "step_key": key,
                "answer_key": answer_key,
                "schedule_revision": journey.data.get("schedule_revision"),
                "requested_at": d.now().isoformat(),
            },
            parent_id=employee.id,
            status="OPEN",
        )
        d.emit(
            db,
            employee.id,
            FREE_ANSWER_PROMPT,
            unique_key=f"simple-free:{capture.id}:open",
            data={
                "journey_id": journey.id,
                "step_key": key,
                "conversation_id": conversation.id,
                "schedule_revision": journey.data.get("schedule_revision"),
                "simple_free_answer_capture_id": capture.id,
            },
        )
        return {
            "journey_id": journey.id,
            "answer_key": answer_key,
            "awaiting_free_text": True,
            "duplicate": False,
        }
    cancel_free_answers(db, employee.id, "REPLACED_BY_FIXED_CHOICE")
    answer = {
        "value": answer_key,
        "outcome": "ANSWERED",
        "answered_at": d.now().isoformat(),
    }
    d.update(
        journey, answers={**journey.data.get("answers", {}), choice_key(key): answer}
    )
    _complete(db, journey, action, d.now())
    plan(db, journey, employee)
    return {"journey_id": journey.id, "answer_key": answer_key, "duplicate": False}


def resolve_formulation(db, employee, payload):
    journey, graph = _authorized_journey(db, employee, payload)
    key = payload.get("step_key")
    if key not in selected_path(graph, journey.data.get("answers", {})):
        raise HTTPException(404, "Формулировка недоступна")
    from . import domain as d

    action = next(
        (
            a
            for a in d.entities(db, "scheduled_action", journey.id)
            if a.data["step_key"] == key
        ),
        None,
    )
    if not action or action.status not in {"WAITING_ANSWER", "COMPLETED"}:
        raise HTTPException(409, "Сообщение ещё не доставлено")
    step = next(s for s in graph["steps"] if s["step_key"] == key)
    prompt = next(
        (
            p
            for p in step.get("content", {}).get("custom_prompts", [])
            if p["id"] == payload.get("prompt_id")
        ),
        None,
    )
    if not prompt:
        raise HTTPException(404, "Формулировка недоступна")
    return prompt["query_text"]
