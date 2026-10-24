"""Deterministic scenario DSL, calendar compiler and questionnaire validation.

This module has no network calls and never executes text from an HR document.
"""

from __future__ import annotations
import calendar
import hashlib
import json
import re
import math
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KINDS = {"MESSAGE", "QUESTION", "SURVEY", "WAIT_SIGNAL", "BRANCH", "HR_TASK", "END"}
QUESTION_TYPES = {"YES_NO", "NUMERIC", "SINGLE_CHOICE", "MULTI_CHOICE", "TEXT", "PHOTO"}
VARIABLES = {
    "name",
    "preferred_name",
    "site_name",
    "start_date",
    "probation_end_date",
    "hr_name",
    "manager_name",
}


def semantic_hash(graph: dict) -> str:
    clean = {k: v for k, v in graph.items() if k != "editor_layout"}
    return hashlib.sha256(
        json.dumps(
            clean, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def add_months(day: date, months: int) -> date:
    y, m = divmod(day.year * 12 + day.month - 1 + months, 12)
    return date(y, m + 1, min(day.day, calendar.monthrange(y, m + 1)[1]))


def is_workday(day: date, cal: dict | None = None) -> bool:
    if cal is None:
        raise ValueError("NEEDS_CALENDAR_REVIEW: рабочий календарь не задан")
    cal = normalize_calendar(cal)
    if cal.get("configuration_error"):
        raise ValueError(cal["configuration_error"])
    if day < date.fromisoformat(cal["coverage_start"]) or day > date.fromisoformat(
        cal["coverage_end"]
    ):
        raise ValueError("NEEDS_CALENDAR_REVIEW: дата вне покрытия календаря")
    exceptions = cal.get("exceptions", cal.get("dated_exceptions", []))
    if isinstance(exceptions, dict):
        value = exceptions.get(day.isoformat())
        if value is not None:
            return bool(
                value
                if isinstance(value, bool)
                else value.get(
                    "working", value.get("is_working", bool(value.get("intervals")))
                )
            )
    else:
        for item in exceptions:
            if item.get("date") == day.isoformat():
                return bool(
                    item.get(
                        "working", item.get("is_working", bool(item.get("intervals")))
                    )
                )
    if cal.get("pattern") == "DATED_ONLY":
        return False
    weekdays = cal.get("working_weekdays", [0, 1, 2, 3, 4])
    return day.weekday() in weekdays


def workday_number(start: date, number: int, cal: dict | None = None) -> date:
    if not isinstance(number, int) or not 1 <= number <= 1000:
        raise ValueError("Номер рабочего дня должен быть от 1 до 1000")
    current, found = start, 0
    for _ in range(10000):
        if is_workday(current, cal):
            found += 1
            if found == number:
                return current
        current += timedelta(days=1)
    raise ValueError("В календаре не найден рабочий день")


def working_offset(start: date, offset: int, cal: dict | None = None) -> date:
    current, count = start, 0
    direction = 1 if offset >= 0 else -1
    for _ in range(10000):
        if count == abs(offset):
            return current
        current += timedelta(days=direction)
        if is_workday(current, cal):
            count += 1
    raise ValueError("Выход за пределы календаря")


def local_datetime(day: date, hhmm: str, timezone_name: str) -> datetime:
    zone = ZoneInfo(timezone_name)
    naive = datetime.combine(day, time.fromisoformat(hhmm))
    # First occurrence for an ambiguous time; advance through a DST gap.
    for _ in range(181):
        aware = naive.replace(tzinfo=zone, fold=0)
        if (
            aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
            == naive
        ):
            return aware.astimezone(timezone.utc)
        naive += timedelta(minutes=1)
    raise ValueError("Невозможно определить локальное время")


def calculate_due(
    trigger: dict,
    employee: dict,
    cal: dict | None = None,
    accepted: dict | None = None,
    signals: dict | None = None,
) -> datetime | None:
    kind = trigger.get("type", "ANCHOR_TIME")
    if kind == "SIGNAL":
        value = (signals or {}).get(trigger.get("signal_code"))
        return datetime.fromisoformat(value) if isinstance(value, str) else value
    if kind == "AFTER_STEP":
        key = f"{trigger.get('after_step_key')}:{trigger.get('after_event', 'STEP_COMPLETED')}"
        value = (accepted or {}).get(key)
        if not value:
            return None
        value = datetime.fromisoformat(value) if isinstance(value, str) else value
        return value + timedelta(minutes=trigger.get("offset_minutes", 0))
    anchor_field = (
        "probation_end_date"
        if trigger.get("anchor") == "PROBATION_END"
        else "start_date"
    )
    if not employee.get(anchor_field):
        return None
    day = date.fromisoformat(employee[anchor_field])
    if trigger.get("workday_number"):
        day = workday_number(day, trigger["workday_number"], cal)
    else:
        day = add_months(day, trigger.get("offset_months", 0))
        offset = trigger.get("offset_days", 0)
        day = (
            working_offset(day, offset, cal)
            if trigger.get("day_basis") == "WORKING"
            else day + timedelta(days=offset)
        )
    if cal and (trigger.get("day_basis") == "WORKING" or trigger.get("workday_number")):
        normalized = normalize_calendar(cal)
        if day < date.fromisoformat(
            normalized["coverage_start"]
        ) or day > date.fromisoformat(normalized["coverage_end"]):
            raise ValueError("NEEDS_CALENDAR_REVIEW: дата вне покрытия календаря")
    return local_datetime(
        day,
        trigger.get("local_time", "10:00"),
        employee.get("timezone", "Europe/Moscow"),
    )


def eligible(eligibility: dict, employee: dict) -> bool:
    for field, attr in [
        ("site_ids", "site_id"),
        ("employment_types", "employment_type"),
        ("employment_stages", "employment_stage"),
    ]:
        if eligibility.get(field) and employee.get(attr) not in eligibility[field]:
            return False
    return True


def evaluate_condition(
    cond: dict | None,
    answers: dict,
    employee: dict,
    signals: dict | None = None,
    depth=0,
) -> bool:
    if depth > 16:
        raise ValueError("Слишком глубокое условие")
    cond = cond or {"kind": "ALWAYS"}
    kind = cond.get("kind")
    if kind == "ALWAYS":
        return True
    if kind == "ALL":
        return all(
            evaluate_condition(c, answers, employee, signals, depth + 1)
            for c in cond.get("conditions", [])
        )
    if kind == "ANY":
        return any(
            evaluate_condition(c, answers, employee, signals, depth + 1)
            for c in cond.get("conditions", [])
        )
    if kind == "NOT":
        return not evaluate_condition(
            cond.get("condition"), answers, employee, signals, depth + 1
        )
    if kind == "SIGNAL":
        return bool((signals or {}).get(cond.get("signal_code")))
    if kind == "EMPLOYEE":
        value = employee.get(
            {
                "SITE_ID": "site_id",
                "EMPLOYMENT_TYPE": "employment_type",
                "EMPLOYMENT_STAGE": "employment_stage",
                "ADDRESS_FORM": "address_form",
            }.get(cond.get("field"), "")
        )
    elif kind == "ANSWER":
        value = answers.get(cond.get("question_key"))
        if isinstance(value, dict):
            if value.get("outcome") == "DECLINED":
                return False
            value = value.get("value")
    else:
        return False
    expected, op = cond.get("value"), cond.get("operator")
    if op == "IS_EMPTY":
        return value is None or value == "" or value == []
    if op == "IS_NOT_EMPTY":
        return value is not None and value != "" and value != []
    if value is None:
        return False
    if op == "EQ":
        return value == expected
    if op == "NE":
        return value != expected
    if op == "IN":
        return value in expected if isinstance(expected, list) else False
    if op == "CONTAINS_ANY":
        return (
            bool(set(value) & set(expected))
            if isinstance(value, list) and isinstance(expected, list)
            else False
        )
    if type(value) not in (int, float) or type(expected) not in (int, float):
        return False
    return {
        "LT": value < expected,
        "LTE": value <= expected,
        "GT": value > expected,
        "GTE": value >= expected,
    }.get(op, False)


def validate_answer(question: dict, payload: dict) -> dict:
    outcome = payload.get("outcome", payload.get("answer_outcome", "ANSWERED"))
    if outcome == "DECLINED":
        if "value" in payload and payload["value"] is not None:
            raise ValueError("Отказ не должен содержать ответ")
        return {
            "question_key": question["question_key"],
            "outcome": "DECLINED",
            "value": None,
            "comment_outcome": "NOT_REQUESTED",
        }
    if outcome != "ANSWERED":
        raise ValueError("Неизвестный результат ответа")
    value, typ = payload.get("value"), question["type"]
    if typ == "NUMERIC":
        if type(value) is not int or not question.get(
            "minimum", 1
        ) <= value <= question.get("maximum", 10):
            raise ValueError("Выберите целое число от 1 до 10")
    elif typ in ("SINGLE_CHOICE", "YES_NO"):
        if not isinstance(value, str) or value not in [
            o["value"] for o in question.get("options", [])
        ]:
            raise ValueError("Выберите предложенный вариант")
    elif typ == "MULTI_CHOICE":
        if (
            not isinstance(value, list)
            or any(not isinstance(v, str) for v in value)
            or len(value) != len(set(value))
        ):
            raise ValueError("Нужен список уникальных вариантов")
        options = question.get("options", [])
        if any(v not in [o["value"] for o in options] for v in value):
            raise ValueError("Неизвестный вариант")
        if (
            not question.get("min_selected", 1)
            <= len(value)
            <= question.get("max_selected", len(options))
        ):
            raise ValueError("Неверное число выбранных вариантов")
        for option in options:
            if option.get("exclusive") and option["value"] in value and len(value) > 1:
                raise ValueError("Вариант «Никто» выбирается отдельно")
    elif typ == "TEXT":
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > question.get("max_text_length", 6000)
        ):
            raise ValueError("Введите непустой текст допустимой длины")
        value = value.strip()
    elif typ == "PHOTO":
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f-]{36}", value):
            raise ValueError("Выберите загруженное фото")
    else:
        raise ValueError("Неподдерживаемый тип вопроса")
    result = {
        "question_key": question["question_key"],
        "outcome": "ANSWERED",
        "value": value,
    }
    needs_comment = (
        evaluate_condition(
            question.get("comment_when"), {question["question_key"]: result}, {}
        )
        if question.get("comment_when")
        else False
    )
    comment = payload.get("comment")
    comment_outcome = payload.get("comment_outcome", "PROVIDED" if comment else None)
    if needs_comment:
        if comment_outcome == "DECLINED":
            result.update(comment_outcome="DECLINED")
        elif isinstance(comment, str) and comment.strip() and len(comment) <= 6000:
            result.update(comment=comment.strip(), comment_outcome="PROVIDED")
        else:
            result.update(comment_outcome="PENDING", needs_comment=True)
    else:
        result["comment_outcome"] = "NOT_REQUESTED"
    return result


def validate_graph(graph: dict) -> dict:
    if isinstance(graph, dict) and graph.get("editor_mode") == "SIMPLE":
        from .simple_scenarios import validate
        return validate(graph)
    errors, warnings = [], []

    def err(path, message):
        errors.append({"path": path, "message": message})

    if not isinstance(graph, dict):
        return {
            "valid": False,
            "errors": [{"path": "graph", "message": "Ожидался объект программы"}],
            "warnings": [],
        }
    if graph.get("schema_version") != "korsik-scenario-v2":
        err("schema_version", "Неподдерживаемая версия DSL")
    if graph.get("algorithm_version") != "calendar-v1":
        err("algorithm_version", "Неизвестный алгоритм календаря")
    steps = graph.get("steps", [])
    if not isinstance(steps, list) or not 1 <= len(steps) <= 500:
        return {
            "valid": False,
            "errors": [
                {
                    "path": "steps",
                    "message": "В программе должно быть от 1 до 500 шагов",
                }
            ],
            "warnings": [],
        }
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            err(f"steps[{i}]", "Шаг должен быть объектом")
            continue
        if not isinstance(step.get("step_key"), str):
            err(f"steps[{i}].step_key", "Идентификатор должен быть строкой")
        if "sequence_no" in step and (
            type(step["sequence_no"]) is not int
            or not 0 <= step["sequence_no"] <= 10000
        ):
            err(f"steps[{i}].sequence_no", "Неверный порядковый номер")
        if "mandatory" in step and type(step["mandatory"]) is not bool:
            err(f"steps[{i}].mandatory", "Ожидался логический признак")
        for name in (
            "content",
            "trigger",
            "eligibility",
            "delivery_policy",
            "question",
            "survey",
            "task",
        ):
            if name in step and not isinstance(step[name], dict):
                err(f"steps[{i}].{name}", "Ожидался объект")
        if isinstance(step.get("content"), dict):
            for text_key in ("text_ty", "text_vy"):
                if text_key in step["content"] and (
                    not isinstance(step["content"][text_key], str)
                    or len(step["content"][text_key]) > 12000
                ):
                    err(
                        f"steps[{i}].content.{text_key}",
                        "Нужен текст до 12000 символов",
                    )
        if isinstance(step.get("eligibility"), dict):
            for filter_key in ("site_ids", "employment_types", "employment_stages"):
                if filter_key in step["eligibility"] and (
                    not isinstance(step["eligibility"][filter_key], list)
                    or any(
                        not isinstance(v, str) for v in step["eligibility"][filter_key]
                    )
                ):
                    err(f"steps[{i}].eligibility", "Фильтры должны быть списками строк")
        if "transitions" in step and (
            not isinstance(step["transitions"], list)
            or any(not isinstance(t, dict) for t in step["transitions"])
        ):
            err(f"steps[{i}].transitions", "Ожидался список переходов")
        if isinstance(step.get("survey"), dict):
            qs = step["survey"].get("questions", [])
            if not isinstance(qs, list) or any(not isinstance(q, dict) for q in qs):
                err(f"steps[{i}].survey.questions", "Ожидался список вопросов")
    if errors:
        return {"valid": False, "errors": errors, "warnings": warnings}
    keys = [s.get("step_key") for s in steps]
    if len(set(keys)) != len(keys):
        err("steps", "Повторяются идентификаторы шагов")
    if graph.get("start_step_key") not in keys:
        err("start_step_key", "Начальный шаг отсутствует")
    edges = {key: [] for key in keys}
    question_keys = set()
    question_definitions = {}
    condition_references = []
    total_condition_nodes = [0]

    def check_condition(c, path, depth=0):
        total_condition_nodes[0] += 1
        if depth > 16 or total_condition_nodes[0] > 2000:
            err(path, "Превышен предел сложности условий")
            return
        if not isinstance(c, dict):
            err(path, "Ожидалось типизированное условие")
            return
        k = c.get("kind")
        if k not in {"ALWAYS", "ALL", "ANY", "NOT", "ANSWER", "EMPLOYEE", "SIGNAL"}:
            err(path, "Неизвестный вид условия")
            return
        if k in {"ALL", "ANY"}:
            cs = c.get("conditions", [])
            if not isinstance(cs, list) or not 1 <= len(cs) <= 32:
                err(path, "Нужно 1–32 дочерних условия")
                return
            for child in cs:
                check_condition(child, path, depth + 1)
        if k == "NOT":
            check_condition(c.get("condition"), path, depth + 1)
        if k == "ANSWER":
            condition_references.append((c, path))
        if k in {"ANSWER", "EMPLOYEE"} and c.get("operator") not in {
            "EQ",
            "NE",
            "LT",
            "LTE",
            "GT",
            "GTE",
            "IN",
            "CONTAINS_ANY",
            "IS_EMPTY",
            "IS_NOT_EMPTY",
        }:
            err(path, "Неизвестный оператор")
        if k == "EMPLOYEE" and c.get("field") not in {
            "SITE_ID",
            "EMPLOYMENT_TYPE",
            "EMPLOYMENT_STAGE",
            "ADDRESS_FORM",
        }:
            err(path, "Поле сотрудника не разрешено")
        if c.get("operator") in {"LT", "LTE", "GT", "GTE"} and type(
            c.get("value")
        ) not in (int, float):
            err(path, "Числовому оператору нужно число")

    def check_question(q, path):
        if not isinstance(q, dict):
            err(path, "Вопрос отсутствует")
            return
        key = q.get("question_key")
        if "options" in q and (
            not isinstance(q["options"], list)
            or any(not isinstance(o, dict) for o in q["options"])
        ):
            err(path + ".options", "Ожидался список вариантов")
            return
        if "transitions" in q and (
            not isinstance(q["transitions"], list)
            or any(not isinstance(t, dict) for t in q["transitions"])
        ):
            err(path + ".transitions", "Ожидался список переходов")
            return
        if not key or key in question_keys:
            err(path, "Идентификатор вопроса отсутствует или повторён")
        question_keys.add(key)
        question_definitions[key] = q
        if q.get("type") not in QUESTION_TYPES:
            err(path, "Неизвестный тип вопроса")
        if not isinstance(q.get("prompt"), dict) or not any(
            isinstance(q.get("prompt", {}).get(k), str) and q["prompt"][k].strip()
            for k in ("text_ty", "text_vy")
        ):
            err(path, "Нет текста вопроса")
        if q.get("type") in {"YES_NO", "SINGLE_CHOICE", "MULTI_CHOICE"}:
            options = q.get("options", [])
            vals = [o.get("value") for o in options]
            if (
                any(not isinstance(v, str) for v in vals)
                or not vals
                or len(vals) != len(set(vals))
            ):
                err(path, "Варианты отсутствуют или повторены")
        if q.get("type") == "NUMERIC" and (
            q.get("minimum") != 1 or q.get("maximum") != 10
        ):
            err(path, "Шкала должна быть от 1 до 10")
        if q.get("answer_binding") == "ADDRESS_FORM" and (
            q.get("type") != "SINGLE_CHOICE"
            or set(o.get("value") for o in q.get("options", [])) != {"TY", "VY"}
        ):
            err(path, "Обращение допускает только ТЫ/ВЫ")
        if q.get("comment_when"):
            check_condition(q["comment_when"], path + ".comment_when")

    for i, s in enumerate(steps):
        p = f"steps[{i}]"
        if not isinstance(s.get("step_key"), str) or not re.fullmatch(
            r"[a-zA-Z0-9_.:-]{1,120}", s.get("step_key", "")
        ):
            err(p + ".step_key", "Неверный идентификатор")
        if s.get("kind") not in KINDS:
            err(p + ".kind", "Этот тип блока не разрешён")
        if any(
            k in s
            for k in (
                "script",
                "code",
                "python",
                "javascript",
                "http_request",
                "secret",
            )
        ):
            err(p, "Исполняемый код в программе запрещён")
        text = json.dumps(s.get("content", {}), ensure_ascii=False)
        for variable in re.findall(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}", text):
            if variable not in VARIABLES:
                err(p + ".content", f"Неизвестная переменная {variable}")
        for j, t in enumerate(s.get("transitions", [])):
            target = t.get("target_step_key")
            if target not in keys:
                err(p + ".transitions", "Переход ведёт к отсутствующему шагу")
            else:
                edges[s["step_key"]].append(target)
            check_condition(
                t.get("condition", {"kind": "ALWAYS"}), p + f".transitions[{j}]"
            )
        tr = s.get("trigger", {})
        if s.get("kind") in {
            "MESSAGE",
            "QUESTION",
            "SURVEY",
            "HR_TASK",
            "WAIT_SIGNAL",
        } and tr.get("type") not in {"ANCHOR_TIME", "AFTER_STEP", "SIGNAL"}:
            err(p + ".trigger", "Нужно явно указать время или событие")
        if tr.get("type") == "ANCHOR_TIME":
            try:
                time.fromisoformat(tr.get("local_time", "10:00"))
            except (ValueError, TypeError):
                err(p + ".trigger.local_time", "Неверное время")
            if tr.get("anchor") not in {"START_DATE", "PROBATION_END"}:
                err(p + ".trigger.anchor", "Неизвестная точка отсчёта")
            if tr.get("workday_number") and tr.get("day_basis") != "WORKING":
                err(p + ".trigger", "Рабочий день требует рабочего календаря")
        if tr.get("type") == "AFTER_STEP" and tr.get("after_step_key") not in keys:
            err(p + ".trigger", "Зависимый шаг отсутствует")
        if s.get("kind") == "QUESTION":
            check_question(s.get("question"), p + ".question")
        if s.get("kind") == "SURVEY":
            survey = s.get("survey", {})
            qs = survey.get("questions", [])
            if not qs or len(qs) > 100:
                err(p + ".survey", "Нужен опрос с 1–100 вопросами")
            if survey.get("start_question_key") not in [
                q.get("question_key") for q in qs
            ]:
                err(p + ".survey", "Нет начального вопроса")
            for qi, q in enumerate(qs):
                check_question(q, p + f".survey.questions[{qi}]")
            qkeys = {q.get("question_key") for q in qs}
            for q in qs:
                if q.get("next_question_key") and q["next_question_key"] not in qkeys:
                    err(p + ".survey", "Следующий вопрос отсутствует")
                for qt in q.get("transitions", []):
                    if qt.get("target_question_key") not in qkeys:
                        err(p + ".survey", "Ветка вопроса ведёт в никуда")
                    check_condition(
                        qt.get("condition", {"kind": "ALWAYS"}),
                        p + ".survey.transitions",
                    )
        if s.get("content", {}).get("missing_assets"):
            warnings.append(
                {
                    "step_key": s["step_key"],
                    "message": "Исходный файл не предоставлен; вложение не отправляется",
                }
            )
    for condition, path in condition_references:
        question = question_definitions.get(condition.get("question_key"))
        if question is None:
            err(path, "Условие ссылается на отсутствующий вопрос")
        elif (
            condition.get("operator") in {"LT", "LTE", "GT", "GTE"}
            and question.get("type") != "NUMERIC"
        ):
            err(path, "Числовое сравнение применимо только к числовому вопросу")
        elif (
            condition.get("operator") == "CONTAINS_ANY"
            and question.get("type") != "MULTI_CHOICE"
        ):
            err(path, "CONTAINS_ANY требует вопрос с несколькими вариантами")
    colors = {}

    def visit(key):
        if colors.get(key) == 1:
            err("transitions", "В программе обнаружен цикл")
            return
        if colors.get(key) == 2:
            return
        colors[key] = 1
        for target in edges.get(key, []):
            visit(target)
        colors[key] = 2

    for key in keys:
        visit(key)
    if graph.get("start_step_key") in keys:
        seen = set()
        todo = [graph["start_step_key"]]
        while todo:
            k = todo.pop()
            if k in seen:
                continue
            seen.add(k)
            todo.extend(edges.get(k, []))
        for s in steps:
            if s.get("mandatory") and s["step_key"] not in seen:
                err(s["step_key"], "Обязательный шаг недостижим от начала")
    layout = graph.get("editor_layout", {})
    for n in layout.get("nodes", []):
        if not isinstance(n, dict):
            err("editor_layout", "Позиция должна быть объектом")
            continue
        if n.get("step_key") not in keys:
            err("editor_layout", "Позиция относится к отсутствующему шагу")
        if any(
            type(n.get(axis)) not in (int, float)
            or not math.isfinite(n[axis])
            or abs(n[axis]) > 1000000
            for axis in ("x", "y")
        ):
            err("editor_layout", "Некорректные координаты")
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "semantic_hash": semantic_hash(graph),
    }


def simulate(
    graph: dict,
    employee: dict,
    cal: dict | None = None,
    answers: dict | None = None,
    signals: dict | None = None,
) -> dict:
    if isinstance(graph, dict) and graph.get("editor_mode") == "SIMPLE":
        from .simple_scenarios import simulate as simulate_simple
        return simulate_simple(graph, employee, answers)
    result = validate_graph(graph)
    if not result["valid"]:
        return {**result, "timeline": []}
    answers = answers or {}
    signals = signals or {}
    events = {}
    rows = []
    selected_path = active_path(graph, answers, employee, signals)
    for step in sorted(graph["steps"], key=lambda s: s.get("sequence_no", 0)):
        if not eligible(step.get("eligibility", {}), employee):
            continue
        try:
            due = (
                calculate_due(step.get("trigger", {}), employee, cal, events, signals)
                if step.get("trigger")
                else None
            )
        except (ValueError, ZoneInfoNotFoundError) as e:
            result["errors"].append({"path": step["step_key"], "message": str(e)})
            continue
        row = {
            "step_key": step["step_key"],
            "kind": step["kind"],
            "due_at": due.isoformat() if due else None,
            "status": "READY" if due else "WAITING_EVENT",
            "reason": step.get("trigger"),
            "missing_assets": step.get("content", {}).get("missing_assets", []),
        }
        if step["step_key"] not in selected_path:
            row["status"] = "NOT_SELECTED_OR_WAITING_ANSWER"
        if due and step["step_key"] in selected_path:
            events[f"{step['step_key']}:ACTION_ACCEPTED"] = due.isoformat()
            if step["kind"] == "MESSAGE":
                events[f"{step['step_key']}:STEP_COMPLETED"] = due.isoformat()
            elif (
                step["kind"] == "QUESTION"
                and step.get("question", {}).get("question_key") in answers
            ):
                events[f"{step['step_key']}:STEP_COMPLETED"] = due.isoformat()
            elif step["kind"] == "SURVEY" and all(
                q["question_key"] in answers
                for q in step["survey"]["questions"]
                if q.get("required")
            ):
                events[f"{step['step_key']}:STEP_COMPLETED"] = due.isoformat()
                events[f"{step['step_key']}:SURVEY_COMPLETED"] = due.isoformat()
        if step.get("survey"):
            row["questions"] = [
                {
                    "question_key": q["question_key"],
                    "comment_requested": bool(
                        q.get("comment_when")
                        and evaluate_condition(
                            q["comment_when"], answers, employee, signals
                        )
                    ),
                }
                for q in step["survey"]["questions"]
            ]
        rows.append(row)
    rows.sort(key=lambda r: (r["due_at"] is None, r["due_at"] or "", r["step_key"]))
    return {
        **result,
        "valid": not result["errors"],
        "timeline": rows,
        "employee": employee,
        "simulation_only": True,
        "outbound_messages_created": 0,
    }


def condition_ready(
    condition: dict, answers: dict, employee: dict, signals: dict | None = None
) -> bool:
    """Unknown answers do not silently select an unconditional fallback."""
    kind = condition.get("kind")
    if kind == "ANSWER":
        return condition.get("question_key") in answers
    if kind in ("ALL", "ANY"):
        return all(
            condition_ready(c, answers, employee, signals)
            for c in condition.get("conditions", [])
        )
    if kind == "NOT":
        return condition_ready(
            condition.get("condition", {}), answers, employee, signals
        )
    return True


def active_path(
    graph: dict, answers: dict, employee: dict, signals: dict | None = None
) -> set[str]:
    """Evaluate first-match graph edges; wait for explicit answers at a branch.

    Time triggers remain independent along the selected path. A survey therefore
    does not block a later calendar milestone unless the HR graph has an actual
    answer-dependent edge.
    """
    if graph.get("editor_mode") == "SIMPLE":
        from .simple_scenarios import selected_path
        return set(selected_path(graph, answers))
    bykey = {s["step_key"]: s for s in graph.get("steps", [])}
    allowed = set()
    key = graph.get("start_step_key")
    for _ in range(501):
        if not key or key not in bykey or key in allowed:
            break
        allowed.add(key)
        step = bykey[key]
        next_key = None
        for transition in step.get("transitions", []):
            condition = transition.get("condition", {"kind": "ALWAYS"})
            if not condition_ready(condition, answers, employee, signals):
                return allowed
            if evaluate_condition(condition, answers, employee, signals):
                next_key = transition["target_step_key"]
                break
        key = next_key
    return allowed


def normalize_calendar(data: dict) -> dict:
    data = dict(data.get("definition", data))
    if "weekly_working_days" in data:
        data["working_weekdays"] = [d - 1 for d in data["weekly_working_days"]]
    data.setdefault("coverage_start", data.get("coverage_from", "2020-01-01"))
    data.setdefault("coverage_end", data.get("coverage_to", "2035-12-31"))
    if "dated_exceptions" in data:
        data["exceptions"] = data["dated_exceptions"]
    return data


def calendar_intervals(day: date, cal: dict) -> list[dict]:
    cal = normalize_calendar(cal)
    if day < date.fromisoformat(cal["coverage_start"]) or day > date.fromisoformat(
        cal["coverage_end"]
    ):
        raise ValueError("NEEDS_CALENDAR_REVIEW: дата вне покрытия календаря")
    for item in cal.get("exceptions", []):
        if item.get("date") == day.isoformat():
            if not item.get(
                "working", item.get("is_working", bool(item.get("intervals")))
            ):
                return []
            return item.get("intervals", [])
    if cal.get("pattern") == "DATED_ONLY":
        return []
    for item in cal.get("weekly_intervals", []):
        if item.get("weekday") == day.isoweekday():
            return item.get("intervals", [])
    return []


def add_working_minutes(start: datetime, minutes: int, cal: dict) -> datetime:
    cal = normalize_calendar(cal)
    zone = ZoneInfo(cal.get("timezone", "Europe/Moscow"))
    cursor = start.astimezone(timezone.utc)
    remaining = minutes
    if type(minutes) is not int or minutes <= 0:
        raise ValueError("SLA должен содержать положительное число минут")
    day = max(
        cursor.astimezone(zone).date() - timedelta(days=1),
        date.fromisoformat(cal["coverage_start"]),
    )
    # Include the previous day's overnight shift when the SLA starts after midnight.
    for _ in range(10000):
        intervals = calendar_intervals(day, cal)
        for interval in intervals:
            lo = local_datetime(
                day,
                interval.get(
                    "start_local", interval.get("start", interval.get("from", "09:00"))
                ),
                str(zone),
            )
            hi = local_datetime(
                day + timedelta(days=interval.get("end_day_offset", 0)),
                interval.get(
                    "end_local", interval.get("end", interval.get("to", "18:00"))
                ),
                str(zone),
            )
            pos = max(cursor, lo)
            if pos >= hi:
                continue
            available = int((hi - pos).total_seconds() // 60)
            if remaining <= available:
                return pos + timedelta(minutes=remaining)
            remaining -= available
            cursor = hi
        day += timedelta(days=1)
        cursor = max(cursor, local_datetime(day, "00:00", str(zone)))
    raise ValueError("NEEDS_CALENDAR_REVIEW: невозможно рассчитать рабочий срок")
