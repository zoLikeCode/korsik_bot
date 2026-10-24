"""One employee command for MAX and web; no model can mutate survey or permissions."""

from copy import deepcopy
from datetime import datetime, timedelta
from typing import Literal, Annotated
import uuid, secrets, hashlib
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    UploadFile,
    File,
)
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, func, text
from sqlalchemy.orm import Session
from .db import get_db, new_id, now, aware
from .models import Entity, Message, Job, User
from .auth import Actor, get_actor, hash_token, user_actor, is_hidden_setup
from .common import (
    get_entity,
    save_entity,
    list_kind,
    assert_employee,
    actor_can_employee,
    require_role,
    serialize,
    commit,
    audit,
    enqueue,
    add_message,
    queue_message,
    fingerprint,
    check_etag,
    etag,
    employee_is_enabled,
)
from .settings import get_settings

router = APIRouter()


def can_conversation(db, actor, conv):
    if not actor_can_employee(db, actor, conv.data.get("employee_id", "")):
        return False
    if actor.role == "EMPLOYEE":
        employee = db.get(Entity, conv.data.get("employee_id"))
        if (
            not employee
            or employee.data.get("employment_stage") == "ENDED"
            or conv.data.get("episode_id") != employee.data.get("episode_id")
        ):
            return False
    return actor.role in {"EMPLOYEE", "HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"}


def conversation_for(db, employee, create=True):
    # Serialize aggregate creation on the trusted employee, including concurrent GETs.
    employee = db.scalar(
        select(Entity)
        .where(Entity.id == employee.id, Entity.kind == "employee")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if employee is None:
        raise HTTPException(404, detail="Сотрудник не найден")
    if employee.data.get("employment_stage") == "ENDED":
        if not create:
            return None
        raise HTTPException(
            409,
            detail="Период найма завершён; новый диалог создаётся только после повторного найма",
        )
    episode_id = employee.data.get("episode_id", employee.id)
    for conv in list_kind(db, "conversation", parent_id=employee.id):
        if conv.data.get("episode_id") == episode_id and conv.status != "CLOSED":
            return conv
    if not create:
        return None
    scenario_id = employee.data.get("scenario_id")
    if not scenario_id:
        journeys = [
            j
            for j in list_kind(db, "journey", parent_id=employee.id)
            if j.status not in {"CANCELLED", "COMPLETED"}
        ]
        programs = set(
            j.data.get("scenario_id") for j in journeys if j.data.get("scenario_id")
        )
        if len(programs) == 1:
            scenario_id = programs.pop()
    return save_entity(
        db,
        "conversation",
        {
            "employee_id": employee.id,
            "episode_id": episode_id,
            "scenario_id": scenario_id,
            "mode": "BOT",
            "message_seq": 0,
            "state_seq": 0,
        },
        parent_id=employee.id,
        owner_id=employee.data.get("user_id"),
    )


def require_conversation(db, actor, conversation_id, lock=False):
    conv = get_entity(db, conversation_id, "conversation", lock=lock)
    if not can_conversation(db, actor, conv):
        raise HTTPException(404, detail="Диалог не найден")
    return conv


@router.get("/conversations")
def conversations(actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    from .organization import employee_photo_url

    items = []
    visible = []
    for employee in list_kind(db, "employee"):
        if employee.data.get("employment_stage") == "ENDED":
            continue
        if actor_can_employee(db, actor, employee.id) and actor.role in {
            "EMPLOYEE",
            "HR_ADMIN",
            "HR_SPECIALIST",
            "HEAD_TALENT",
        }:
            conv = conversation_for(db, employee)
            visible.append(conv)
            items.append(
                {
                    **serialize(conv),
                    "employee_name": employee.data.get("name", "Сотрудник"),
                    "employee_photo_url": employee_photo_url(db, actor, employee),
                }
            )
    from .dialog_issues import problem_index, unresolved_count
    issues = problem_index(db, visible) if actor.role != "EMPLOYEE" else {}
    for item in items:
        if actor.role != "EMPLOYEE":
            item["unresolved_question_count"] = unresolved_count(issues.get(item["id"], {}))
    commit(db)
    return {"items": items, "problem_conversations_count": sum(item.get("unresolved_question_count", 0) > 0 for item in items)}


@router.get("/conversations/problem-summary")
def problem_summary(actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST")
    from .dialog_issues import problem_index, unresolved_count
    employees = {e.id: e for e in list_kind(db, "employee")
                 if e.data.get("employment_stage") != "ENDED" and actor_can_employee(db, actor, e.id)}
    visible = [c for c in list_kind(db, "conversation") if c.status != "CLOSED"
               and c.data.get("employee_id") in employees
               and c.data.get("episode_id") == employees[c.data["employee_id"]].data.get("episode_id")]
    counts = [unresolved_count(i) for i in problem_index(db, visible).values()]
    return {"problem_conversations_count": sum(n > 0 for n in counts),
            "unresolved_question_count": sum(counts)}


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    employee_id: str | None = None


@router.post("/conversations", status_code=201)
def create_conversation(
    body: ConversationCreate,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    employee = assert_employee(db, actor, body.employee_id or actor.employee_id or "")
    conv = conversation_for(db, employee)
    commit(db)
    return serialize(conv)


@router.get("/conversations/{conversation_id}")
def conversation_detail(
    conversation_id: str,
    response: Response,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    from .organization import employee_photo_url

    conv = require_conversation(db, actor, conversation_id)
    response.headers["ETag"] = etag(conv)
    employee = get_entity(db, conv.data["employee_id"], "employee")
    journeys = {
        j.id
        for j in list_kind(db, "journey", parent_id=employee.id)
        if j.data.get("episode_id") == conv.data.get("episode_id")
    }
    questions = [
        serialize(q)
        for q in list_kind(db, "scenario_question")
        if q.status == "OPEN"
        and q.data.get("employee_id") == employee.id
        and q.parent_id in journeys
    ]
    capture = next(
        (
            c
            for c in reversed(list_kind(db, "input_capture", parent_id=employee.id))
            if c.status in {"OPEN", "DRAFT", "READY_TO_CONFIRM"}
            and c.data.get("expires_at")
            and aware(datetime.fromisoformat(c.data["expires_at"])) > now()
        ),
        None,
    )
    from .dialog_issues import problem_index, unresolved_count
    problem_fields = ({"unresolved_question_count": unresolved_count(problem_index(db, [conv]).get(conv.id, {}))}
                      if actor.role != "EMPLOYEE" else {})
    return {
        **serialize(conv),
        **problem_fields,
        "employee_name": employee.data.get("name", "Сотрудник"),
        "employee_photo_url": employee_photo_url(db, actor, employee),
        "scenario_questions": questions,
        "active_input_capture": serialize(capture) if capture else None,
    }


def message_quote(db, conv, message_id):
    message = db.get(Message, str(message_id)) if message_id else None
    if not message or message.conversation_id != conv.id:
        raise HTTPException(404, "Сообщение не найдено")
    text = message.text
    if (message.data or {}).get("citations"):
        from .retrieval import answer_still_authorized
        if not answer_still_authorized(db, conv, message.data):
            text = "Источники ответа больше недоступны."
    return {"message_id": message.id, "author_type": message.author_type,
            "text": text[:500], "seq": message.seq}


@router.get("/conversations/{conversation_id}/messages")
def messages(
    conversation_id: str,
    after_seq: int | None = None,
    limit: int = 100,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    conv = require_conversation(db, actor, conversation_id)
    limit = max(1, min(limit, 200))
    query = select(Message).where(Message.conversation_id == conversation_id)
    if after_seq is None:
        rows = list(
            reversed(db.scalars(query.order_by(Message.seq.desc()).limit(limit)).all())
        )
    else:
        rows = db.scalars(
            query.where(Message.seq > max(0, after_seq))
            .order_by(Message.seq)
            .limit(limit)
        ).all()
    result = []
    from .dialog_issues import problem_index
    issues = problem_index(db, [conv]).get(conv.id, {}) if actor.role != "EMPLOYEE" else {}
    from .retrieval import answer_still_authorized

    for message in rows:
        data = deepcopy(message.data or {})
        text = message.text
        if data.get("citations") and not answer_still_authorized(db, conv, data):
            text = "Источники этого ответа обновлены или больше недоступны в вашей области. Задайте вопрос заново, чтобы получить актуальную информацию."
            data = {
                "answer_id": data.get("answer_id"),
                "mode": "HANDOFF",
                "citations": [],
                "source_access_revoked": True,
            }
        result.append(
            {
                "message_id": message.id,
                "id": message.id,
                "conversation_id": message.conversation_id,
                "seq": message.seq,
                "author_type": message.author_type,
                "text": text,
                "data": data,
                **({"support_issue": issues[message.id]} if message.id in issues else {}),
                **({"reply_to_message_id": message.data["reply_to_message_id"],
                    "reply_to": message_quote(db, conv, message.data["reply_to_message_id"])}
                   if (message.data or {}).get("reply_to_message_id") else {}),
                "created_at": message.created_at.isoformat(),
                **({"answer_id": data["answer_id"]} if data.get("answer_id") else {}),
            }
        )
    return {
        "items": result,
        "next_after_seq": rows[-1].seq if rows else after_seq or 0,
        "has_more": bool(rows and rows[-1].seq < int(conv.data.get("message_seq", 0))),
        "latest_page": after_seq is None,
    }


def _eligible(item, employee):
    eligibility = item.get("eligibility") or {}
    d = employee.data
    for key, value in [
        ("site_ids", d.get("site_id")),
        ("employment_types", d.get("employment_type")),
        ("employment_stages", d.get("employment_stage", "ACTIVE")),
    ]:
        if eligibility.get(key) and value not in eligibility[key]:
            return False
    return True


def legacy_quick_prompts_for(db, conv):
    employee = get_entity(db, conv.data["employee_id"], "employee")
    sid = conv.data.get("scenario_id")
    if not sid:
        return {
            "menu_id": None,
            "menu_version_id": None,
            "items": [],
            "configuration_state": "NO_ASSIGNED_PROGRAM",
        }
    menus = [
        m for m in list_kind(db, "assistant_menu") if m.data.get("scenario_id") == sid
    ]
    if len(menus) != 1:
        return {
            "menu_id": None,
            "menu_version_id": None,
            "items": [],
            "configuration_state": "MENU_NOT_CONFIGURED",
        }
    menu = menus[0]
    vid = menu.data.get("published_version_id") or menu.data.get("active_version_id")
    version = db.get(Entity, vid) if vid else None
    if not version or version.status != "PUBLISHED":
        return {"menu_id": menu.id, "menu_version_id": None, "items": []}
    items = []
    for item in version.data.get("items", []):
        if not item.get("enabled", True) or not _eligible(item, employee):
            continue
        x = deepcopy(item)
        x["prompt_key"] = item.get("prompt_key") or item.get("id")
        x["query_text"] = item.get("query_text", "")
        x["label"] = item.get(
            "label_ty" if employee.data.get("address_form") == "TY" else "label_vy",
            item.get("label", "Вопрос"),
        )
        x["action"] = {
            "kind": "RAG_QUERY",
            "menu_id": menu.id,
            "menu_version_id": version.id,
            "prompt_key": x["prompt_key"],
        }
        items.append(x)
    return {
        "menu_id": menu.id,
        "menu_version_id": version.id,
        "items": sorted(items, key=lambda i: i.get("order", 0))[:12],
    }


def quick_prompts_for(db, conv):
    from .basic_formulations import basic_prompts_for

    employee = get_entity(db, conv.data["employee_id"], "employee")
    basic = basic_prompts_for(db, employee)
    if basic["configured"]:
        return basic
    for journey in list_kind(db, "journey", parent_id=employee.id):
        if journey.data.get("episode_id") != employee.data.get("episode_id"):
            continue
        version = db.get(Entity, journey.data.get("version_id"))
        if version and version.data.get("graph", {}).get("editor_mode") == "SIMPLE":
            return basic
    return legacy_quick_prompts_for(db, conv)


def has_immediate_simple_greeting(db, employee, journeys):
    from .simple_scenarios import due_for

    clock = now()
    for journey in journeys:
        version = get_entity(db, journey.data["version_id"], "scenario_version")
        graph = version.data["graph"]
        step = next(
            (
                item
                for item in graph["steps"]
                if item["step_key"] == graph["start_step_key"]
            ),
            None,
        )
        if step and step["kind"] == "MESSAGE":
            due = due_for(graph, step, employee.data, journey.data, clock)
            if due is not None and due <= clock:
                return True
    return False


@router.get("/conversations/{conversation_id}/quick-prompts")
def quick_prompts(
    conversation_id: str,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    return quick_prompts_for(db, require_conversation(db, actor, conversation_id))


class TextInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["TEXT"]
    text: str = Field(min_length=1, max_length=8000)


class RagInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["RAG_QUERY"]
    menu_id: str
    menu_version_id: str
    prompt_key: str = Field(min_length=1, max_length=120)


class TurnInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: Annotated[TextInput | RagInput, Field(discriminator="kind")]
    client_message_id: uuid.UUID
    reply_to_message_id: uuid.UUID | None = None


def continue_unclaimed_bot_search(db, conv):
    """An unclaimed support alert does not take the employee's bot away."""
    if conv.data.get("mode") != "WAITING_HR":
        return False
    case_id = conv.data.get("active_handoff_case_id")
    case = (
        db.scalar(
            select(Entity)
            .where(Entity.id == case_id, Entity.kind == "case")
            .with_for_update()
        )
        if case_id
        else None
    )
    if (
        not case
        or case.status not in {"OPEN", "ASSIGNED", "REOPENED"}
        or case.owner_id
        or case.data.get("claimed_at")
        or case.data.get("conversation_id") != conv.id
        or case.data.get("episode_id") != conv.data.get("episode_id")
    ):
        return False
    conv.data = {
        **conv.data,
        "mode": "BOT",
        "state_seq": int(conv.data.get("state_seq", 0)) + 1,
    }
    audit(
        db, None, "conversation.unclaimed_search_resumed", conv.id, {"case_id": case.id}
    )
    return True


def submit_turn(db, actor, conv, input, client_message_id, reply_to_message_id=None):
    """Caller commits. Lock on aggregate serializes seq, pending turn and handoff."""
    conv = get_entity(db, conv.id, "conversation", lock=True)
    if not can_conversation(db, actor, conv):
        raise HTTPException(404, detail="Диалог не найден")
    employee = get_entity(db, conv.data["employee_id"], "employee")
    if (
        not employee_is_enabled(employee)
        or employee.data.get("employment_stage") == "ENDED"
        or employee.status
        in {
            "ENDED",
            "CANCELLED",
        }
    ):
        raise HTTPException(403, detail="Доступ завершён")
    turn_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"korsik:{conv.id}:{client_message_id}")
    )
    request_hash = fingerprint({"input": input, "reply": reply_to_message_id})
    old = db.get(Entity, turn_id)
    if old:
        if (
            old.kind != "conversation_turn"
            or old.parent_id != conv.id
            or old.data.get("request_hash") != request_hash
        ):
            raise HTTPException(409, detail="Конфликт повторного запроса")
        return serialize(old)
    continue_unclaimed_bot_search(db, conv)
    if input.get("kind") == "RAG_QUERY":
        from .basic_formulations import basic_prompts_for

        # Old signed menu buttons retain their own publication/scope checks.
        # New greeting buttons resolve only the current base-question revision.
        menus = [basic_prompts_for(db, employee), legacy_quick_prompts_for(db, conv)]
        menu = next(
            (
                item
                for item in menus
                if item.get("menu_id") == input.get("menu_id")
                and item.get("menu_version_id") == input.get("menu_version_id")
            ),
            None,
        )
        if menu is None:
            raise HTTPException(
                409,
                detail={
                    "code": "stale_action",
                    "message": "Кнопки обновились. Выберите вопрос из нового меню",
                },
            )
        item = next(
            (i for i in menu["items"] if i["prompt_key"] == input.get("prompt_key")),
            None,
        )
        if not item:
            raise HTTPException(404, detail="Вопрос недоступен")
        question = item["query_text"]
    else:
        question = str(input.get("text", "")).strip()
    if not question or len(question) > 8000:
        raise HTTPException(422, detail="Вопрос должен содержать от1 до8000 символов")
    if reply_to_message_id:
        reply = db.get(Message, str(reply_to_message_id))
        if not reply or reply.conversation_id != conv.id:
            raise HTTPException(404, detail="Сообщение не найдено")
    message = add_message(
        db,
        conv,
        "EMPLOYEE",
        question,
        {"input_kind": input.get("kind"), "client_message_id": str(client_message_id),
         **({"reply_to_message_id": str(reply_to_message_id)} if reply_to_message_id else {})},
    )
    data = {
        "turn_id": turn_id,
        "conversation_id": conv.id,
        "message_id": message.id,
        "client_message_id": str(client_message_id),
        "request_hash": request_hash,
        "input": input,
        "context_upto_seq": message.seq,
        "created_at": now().isoformat(),
    }
    mode = conv.data.get("mode", "BOT")
    from .simple_scenarios import consume_free_answer

    scenario_response = (
        consume_free_answer(db, employee, conv, question, message.id)
        if input.get("kind") == "TEXT" and mode == "BOT"
        else None
    )
    if scenario_response:
        data.update(dispatch="SCENARIO_ANSWER", scenario_response=scenario_response)
    elif mode in {"HUMAN", "WAITING_HR"}:
        data["dispatch"] = "HR_INBOX"
        assigned = employee.data.get("assigned_hr_id")
        if assigned:
            queue_message(
                db,
                "USER",
                assigned,
                "В обращении сотрудника появилось новое сообщение. Откройте кабинет.",
                f"hr-inbox:{turn_id}",
            )
    else:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(76810103)"))
        pending = db.scalar(
            select(Entity).where(
                Entity.kind == "answer",
                Entity.parent_id == conv.id,
                Entity.status.in_(["QUEUED", "RUNNING"]),
            )
        )
        waiting = (
            db.scalar(
                select(func.count(Job.id)).where(
                    Job.kind == "ANSWER", Job.status.in_(["PENDING", "PROCESSING"])
                )
            )
            or 0
        )
        if pending or waiting >= get_settings().generation_max_waiting + 1:
            data.update(
                dispatch="BUSY",
                reason_code="CONVERSATION_BUSY" if pending else "LOCAL_QUEUE_FULL",
                retryable=True,
                retry_after_seconds=5,
            )
            notice = (
                "Предыдущий вопрос ещё обрабатывается. Ваше сообщение сохранено; повторите вопрос через несколько секунд."
                if pending
                else "Сейчас много вопросов. Сообщение сохранено; повторите немного позже."
            )
            add_message(
                db, conv, "SYSTEM", notice, {"turn_id": turn_id, "dispatch": "BUSY"}
            )
            queue_message(db, "EMPLOYEE", employee.id, notice, f"busy:{turn_id}")
        else:
            deadline = now() + timedelta(seconds=get_settings().chat_deadline_seconds)
            wait = now() + timedelta(
                seconds=get_settings().generation_queue_wait_seconds
            )
            answer = save_entity(
                db,
                "answer",
                {
                    "conversation_id": conv.id,
                    "employee_id": employee.id,
                    "turn_id": turn_id,
                    "question": question,
                    "reply_to_message_id": (
                        str(reply_to_message_id) if reply_to_message_id else None
                    ),
                    "context_upto_seq": message.seq,
                    "deadline_at": deadline.isoformat(),
                    "queue_wait_deadline_at": wait.isoformat(),
                },
                status="QUEUED",
                parent_id=conv.id,
                owner_id=actor.id,
            )
            enqueue(db, "ANSWER", {"answer_id": answer.id}, f"answer:{answer.id}")
            data.update(
                dispatch="BOT_ANSWER",
                answer_id=answer.id,
                answer_deadline_at=deadline.isoformat(),
                queue_wait_deadline_at=wait.isoformat(),
            )
    turn = save_entity(
        db,
        "conversation_turn",
        data,
        status="ACCEPTED",
        parent_id=conv.id,
        owner_id=actor.id,
        id=turn_id,
    )
    audit(db, actor, "conversation.turn", turn.id, {"dispatch": data["dispatch"]})
    return serialize(turn)


@router.post("/conversations/{conversation_id}/turns", status_code=202)
def turn(
    conversation_id: str,
    body: TurnInput,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    if actor.role != "EMPLOYEE":
        raise HTTPException(
            403,
            detail="Для проверки базы используйте sandbox, а не переписку сотрудника",
        )
    conv = require_conversation(db, actor, conversation_id, lock=True)
    result = submit_turn(
        db,
        actor,
        conv,
        body.input.model_dump(),
        str(body.client_message_id),
        str(body.reply_to_message_id) if body.reply_to_message_id else None,
    )
    commit(db)
    return result


class AnswerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question: str = Field(min_length=1, max_length=8000)
    conversation_id: str | None = None
    client_message_id: uuid.UUID | None = None
    as_employee_id: str | None = None
    candidate_snapshot_id: str | None = None
    preview_profile: dict | None = None


@router.post("/answers", status_code=202)
def answer_alias(
    body: AnswerInput, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)
):
    if actor.role == "EMPLOYEE":
        if body.as_employee_id or body.candidate_snapshot_id or body.preview_profile:
            raise HTTPException(403, detail="Недопустимый preview")
        employee = get_entity(db, actor.employee_id or "", "employee")
        conv = conversation_for(db, employee)
        if body.conversation_id and body.conversation_id != conv.id:
            raise HTTPException(404, detail="Диалог не найден")
        result = submit_turn(
            db,
            actor,
            conv,
            {"kind": "TEXT", "text": body.question},
            str(body.client_message_id or uuid.uuid4()),
        )
        commit(db)
        return result
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST")
    if not body.as_employee_id:
        raise HTTPException(422, detail="Выберите сотрудника для проверки применимости")
    employee = assert_employee(db, actor, body.as_employee_id)
    # Preview reuses current employee scope but never creates employee send/history.
    preview = {
        "employee_id": employee.id,
        "episode_id": employee.data.get("episode_id", employee.id),
        "mode": "BOT",
        "preview": True,
    }
    conv = Entity(id=new_id(), kind="conversation", data=preview, status="ACTIVE")
    from .retrieval import preview_answer

    result = preview_answer(
        db, actor, employee, body.question, body.candidate_snapshot_id
    )
    result["delivery_context"] = "EDITOR_PREVIEW"
    return result


@router.get("/answers/{answer_id}")
def answer_detail(
    answer_id: str, actor: Actor = Depends(get_actor), db: Session = Depends(get_db)
):
    result = get_entity(db, answer_id, "answer")
    assert_employee(db, actor, result.data["employee_id"])
    if actor.role not in {"EMPLOYEE", "HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"}:
        raise HTTPException(404, detail="Ответ не найден")
    conv = require_conversation(db, actor, result.data["conversation_id"])
    from .retrieval import answer_still_authorized

    if result.data.get("citations") and not answer_still_authorized(
        db, conv, result.data
    ):
        return {
            "id": result.id,
            "answer_id": result.id,
            "status": result.status,
            "conversation_id": conv.id,
            "mode": "HANDOFF",
            "text": "Источники этого ответа обновлены или больше недоступны. Задайте вопрос заново.",
            "citations": [],
            "evidence": [],
            "source_access_revoked": True,
        }
    return serialize(result)


class HumanReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=3900)
    client_message_id: uuid.UUID | None = None
    reply_to_message_id: uuid.UUID | None = None


@router.post("/conversations/{conversation_id}/messages", status_code=201)
def human_reply(
    conversation_id: str,
    body: HumanReply,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST")
    conv = require_conversation(db, actor, conversation_id, lock=True)
    employee = get_entity(db, conv.data["employee_id"], "employee")
    if (conv.status == "CLOSED" or not employee_is_enabled(employee)
            or employee.data.get("employment_stage") == "ENDED"
            or conv.data.get("episode_id") != employee.data.get("episode_id")):
        raise HTTPException(409, "Диалог или доступ сотрудника завершён")
    request_hash = fingerprint({"actor_id": actor.id, "text": body.text,
                                "reply_to_message_id": str(body.reply_to_message_id) if body.reply_to_message_id else None})
    command_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"korsik:hr-reply:{conv.id}:{body.client_message_id}")) if body.client_message_id else None
    old = db.get(Entity, command_id) if command_id else None
    if old:
        if old.kind != "hr_reply" or old.parent_id != conv.id or old.data.get("request_hash") != request_hash:
            raise HTTPException(409, "Конфликт повторного запроса")
        return deepcopy(old.data["response"])
    text = body.text.strip()
    if not text:
        raise HTTPException(422, "Введите сообщение")
    quote = message_quote(db, conv, body.reply_to_message_id) if body.reply_to_message_id else None
    wire_text = (f'В ответ на: «{quote["text"]}»\n\n' if quote else "") + text
    if len(wire_text) > 3900:
        raise HTTPException(422, "Сообщение вместе с цитатой должно быть не длиннее3900 символов")
    from .dialog_issues import problem_index, unresolved_count
    issues = problem_index(db, [conv]).get(conv.id, {})
    case_id = conv.data.get("active_handoff_case_id")
    case = get_entity(db, case_id, "case", lock=True) if case_id else None
    if case and (case.data.get("conversation_id") != conv.id
                 or case.data.get("episode_id") != conv.data.get("episode_id")
                 or case.data.get("employee_id") != employee.id):
        raise HTTPException(409, "Обращение не относится к текущему диалогу")
    if case and case.status in {"CLOSED", "RESOLVED", "CANCELLED"}:
        case = None
    if case:
        owner = case.owner_id or case.data.get("owner_id") or case.data.get("assigned_user_id")
        claimed = bool(case.owner_id or case.data.get("claimed_at") or case.status == "IN_PROGRESS" or conv.data.get("mode") == "HUMAN")
        if claimed and owner and owner != actor.id:
            raise HTTPException(409, "Диалог уже принят другим HR")
    if case is None:
        case = save_entity(db, "case", {"kind": "HANDOFF", "employee_id": employee.id,
            "episode_id": conv.data.get("episode_id"), "conversation_id": conv.id,
            "source": "HR_DIRECT_REPLY", "summary": "Ответ в диалоге", "created_at": now().isoformat()},
            parent_id=employee.id, status="OPEN")
    taking_over = case.status != "IN_PROGRESS" or case.owner_id != actor.id
    if taking_over:
        from .domain import event
        event(db, "case_event", case.id, "CLAIMED", actor.id, source="DIRECT_REPLY")
    case.owner_id = actor.id
    case.status = "IN_PROGRESS"
    case.data = {**case.data, "owner_id": actor.id, "assigned_user_id": actor.id,
                 "claimed_at": case.data.get("claimed_at") or now().isoformat()}
    conv.data = {**conv.data, "mode": "HUMAN", "active_handoff_case_id": case.id,
                 "state_seq": int(conv.data.get("state_seq", 0)) + 1}
    for answer in list_kind(db, "answer", parent_id=conv.id):
        if answer.status in {"QUEUED", "RUNNING"}:
            answer.status = "CANCELLED"
            answer.data = {**answer.data, "reason_code": "HUMAN_TAKEOVER"}
    metadata = {"actor_id": actor.id, "client_message_id": str(body.client_message_id) if body.client_message_id else None}
    if quote:
        metadata["reply_to_message_id"] = quote["message_id"]
        if issues.get(quote["message_id"], {}).get("status") == "OPEN":
            metadata["resolves_problem_message_id"] = quote["message_id"]
    mid = add_message(db, conv, "HR", text, metadata)
    queue_message(db, "EMPLOYEE", employee.id, wire_text,
        f"hr-reply:{conv.id}:{body.client_message_id or mid.id}",
        data={"conversation_id": conv.id, "message_id": mid.id, "case_id": case.id,
              "human_response": True, "author_id": actor.id,
              **({"reply_to_message_id": quote["message_id"]} if quote else {})})
    remaining = unresolved_count(problem_index(db, [conv]).get(conv.id, {}))
    result = {"message_id": mid.id, "seq": mid.seq, "text": mid.text,
              "created_at": mid.created_at.isoformat(), "unresolved_question_count": remaining}
    if command_id:
        save_entity(db, "hr_reply", {"request_hash": request_hash, "response": result},
                    parent_id=conv.id, owner_id=actor.id, id=command_id, status="ACCEPTED")
    audit(db, actor, "conversation.hr_reply", conv.id,
          {"reply_to_message_id": quote["message_id"] if quote else None,
           "resolved_problem_message_id": metadata.get("resolves_problem_message_id")})
    commit(db)
    return result


class HandoffInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(default="Сотрудник попросил помощь HR", max_length=2000)


def handoff(db, actor, conv, reason, *, notify_employee=True, wait_for_human=True):
    conv = get_entity(db, conv.id, "conversation", lock=True)
    employee = get_entity(db, conv.data["employee_id"], "employee")
    if (
        conv.status == "CLOSED"
        or employee.data.get("employment_stage") == "ENDED"
        or conv.data.get("episode_id") != employee.data.get("episode_id")
    ):
        raise HTTPException(409, "Нельзя открыть обращение в завершённом периоде найма")
    from .common import actor_can_employee

    recipients = [
        u
        for u in db.scalars(select(User).where(User.enabled.is_(True))).all()
        if u.role in {"HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"} and not is_hidden_setup(u)
        and actor_can_employee(db, user_actor(u), employee.id)
    ]
    recipient_ids = {u.id for u in recipients}
    assigned = employee.data.get("assigned_hr_id")
    valid_assigned = assigned if assigned in recipient_ids else None
    active = [
        c
        for c in list_kind(db, "case", parent_id=employee.id)
        if c.data.get("kind") == "HANDOFF"
        and c.data.get("episode_id") == employee.data.get("episode_id")
        and c.data.get("conversation_id") == conv.id
        and c.status not in {"CLOSED", "RESOLVED", "CANCELLED"}
    ]
    if active:
        case = active[0]
        if (
            case.status == "OPEN"
            and case.data.get("assigned_user_id") not in recipient_ids
        ):
            case.data = {**case.data, "assigned_user_id": valid_assigned}
    else:
        case = save_entity(
            db,
            "case",
            {
                "kind": "HANDOFF",
                "employee_id": employee.id,
                "episode_id": conv.data.get("episode_id"),
                "conversation_id": conv.id,
                "summary": reason,
                "sensitivity": "STANDARD",
                "assigned_user_id": valid_assigned,
                "created_at": now().isoformat(),
            },
            status="OPEN",
            parent_id=employee.id,
        )
    conv.data = {
        **conv.data,
        "mode": (
            "HUMAN"
            if conv.data.get("mode") == "HUMAN"
            else "WAITING_HR" if wait_for_human else conv.data.get("mode", "BOT")
        ),
        "active_handoff_case_id": case.id,
        "state_seq": int(conv.data.get("state_seq", 0)) + 1,
    }
    for a in list_kind(db, "answer", parent_id=conv.id):
        if wait_for_human and a.status in {"QUEUED", "RUNNING"}:
            a.status = "CANCELLED"
            a.data = {**a.data, "reason_code": "HUMAN_TAKEOVER"}
    text = "Вопрос принят. Уточняем информацию; ответ появится здесь. Можно добавить подробности обычным сообщением."
    if notify_employee:
        add_message(db, conv, "SYSTEM", text, {"case_id": case.id})
        queue_message(
            db,
            "EMPLOYEE",
            employee.id,
            text,
            f"handoff:{case.id}",
            data={
                "conversation_id": conv.id,
                "episode_id": conv.data.get("episode_id"),
            },
        )
    selected = [u for u in recipients if u.id == valid_assigned] or recipients
    for recipient in selected:
        queue_message(
            db,
            "USER",
            recipient.id,
            "Сотрудник не получил ответ на вопрос. Откройте раздел «Диалоги».",
            f"handoff-notify:{case.id}:{recipient.id}",
            data={
                "employee_id": employee.id,
                "case_id": case.id,
                "required_roles": ["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"],
            },
        )
    audit(db, actor, "conversation.handoff", case.id)
    return serialize(case)


# Domain module can own the same path; main mounts this router first.
@router.post("/conversations/{conversation_id}/handoff", status_code=201)
def create_handoff(
    conversation_id: str,
    body: HandoffInput,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    conv = require_conversation(db, actor, conversation_id, lock=True)
    result = handoff(db, actor, conv, body.reason)
    commit(db)
    return result


@router.post("/conversations/{conversation_id}/resume-bot")
def resume_bot(
    conversation_id: str,
    request: Request,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST")
    conv = require_conversation(db, actor, conversation_id, lock=True)
    check_etag(request, conv)
    conv.data = {
        **conv.data,
        "mode": "BOT",
        "state_seq": int(conv.data.get("state_seq", 0)) + 1,
    }
    for capture in list_kind(db, "input_capture", parent_id=conv.data["employee_id"]):
        if capture.status in {"OPEN", "DRAFT", "READY_TO_CONFIRM"}:
            capture.status = "CANCELLED"
    audit(db, actor, "conversation.resume", conv.id)
    commit(db)
    return serialize(conv)


class PrincipalInvite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["MAX"] = "MAX"


@router.get("/me/notification-bindings")
def my_bindings(actor: Actor = Depends(get_actor), db: Session = Depends(get_db)):
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST", "MANAGER", "HEAD_TALENT")
    return {
        "items": [
            _public_binding(b)
            for b in list_kind(db, "principal_binding", owner_id=actor.id)
        ]
    }


@router.post("/me/notification-bindings", status_code=201)
def issue_principal_binding(
    body: PrincipalInvite,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST", "MANAGER", "HEAD_TALENT")
    db.scalar(select(User).where(User.id == actor.id).with_for_update())
    for old in list_kind(db, "principal_binding"):
        if old.owner_id == actor.id and old.status in {"ISSUED", "PENDING_WEB_CONFIRM"}:
            old.status = "REVOKED"
        if old.owner_id == actor.id and old.status == "ACTIVE":
            raise HTTPException(
                409, detail="MAX уже подключён. Сначала отключите прежнюю привязку"
            )
    token = secrets.token_urlsafe(32)
    expires = now() + timedelta(minutes=10)
    binding = save_entity(
        db,
        "principal_binding",
        {
            "channel": "MAX",
            "token_hash": hash_token(token),
            "expires_at": expires.isoformat(),
        },
        status="ISSUED",
        owner_id=actor.id,
    )
    audit(db, actor, "principal_binding.issue", binding.id)
    commit(db)
    return {
        **_public_binding(binding),
        "challenge": f"bind_{token}",
        "invite_url": f"{get_settings().max_bot_url}?start=bind_{token}",
    }


def _public_binding(b):
    d = serialize(b)
    for k in ["token_hash", "max_user_id", "max_chat_id"]:
        d.pop(k, None)
    d["binding_id"] = b.id
    return d


@router.get("/me/notification-bindings/{binding_id}")
def principal_binding(
    binding_id: str,
    response: Response,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    b = get_entity(db, binding_id, "principal_binding")
    if b.owner_id != actor.id:
        raise HTTPException(404, detail="Привязка не найдена")
    response.headers["ETag"] = etag(b)
    return _public_binding(b)


class BindingConfirm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: Literal[True] = True


@router.post("/me/notification-bindings/{binding_id}/confirm")
def confirm_principal_binding(
    binding_id: str,
    body: BindingConfirm,
    request: Request,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    b = get_entity(db, binding_id, "principal_binding", lock=True)
    if b.owner_id != actor.id:
        raise HTTPException(404, detail="Привязка не найдена")
    check_etag(request, b)
    if b.status != "PENDING_WEB_CONFIRM" or not b.data.get("max_user_id"):
        raise HTTPException(409, detail="Сначала откройте ссылку в MAX")
    if (
        aware(__import__("datetime").datetime.fromisoformat(b.data["expires_at"]))
        <= now()
    ):
        raise HTTPException(410, detail="Срок привязки истёк")
    # A MAX principal cannot route notifications for two web identities.
    for other in list_kind(db, "principal_binding"):
        if (
            other.id != b.id
            and other.status == "ACTIVE"
            and other.data.get("max_user_id") == b.data["max_user_id"]
        ):
            raise HTTPException(
                409, detail="Этот MAX уже подключён к другой учётной записи"
            )
    b.status = "ACTIVE"
    b.data = {k: v for k, v in b.data.items() if k != "token_hash"}
    b.data["confirmed_at"] = now().isoformat()
    audit(db, actor, "principal_binding.confirm", b.id)
    commit(db)
    return _public_binding(b)


@router.post("/me/notification-bindings/{binding_id}/revoke")
def revoke_principal_binding(
    binding_id: str,
    request: Request,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    b = get_entity(db, binding_id, "principal_binding", lock=True)
    if b.owner_id != actor.id:
        raise HTTPException(404, detail="Привязка не найдена")
    check_etag(request, b)
    b.status = "REVOKED"
    audit(db, actor, "principal_binding.revoke", b.id)
    commit(db)
    return _public_binding(b)


@router.post("/conversations/{conversation_id}/attachments", status_code=201)
def upload_employee_attachment(
    conversation_id: str,
    file: UploadFile = File(...),
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    from pathlib import Path
    import os

    require_conversation(db, actor, conversation_id)
    raw = file.file.read(10 * 1024 * 1024 + 1)
    if len(raw) > 10 * 1024 * 1024:
        raise HTTPException(413, detail="Размер вложения превышает10МБ")
    if not raw:
        raise HTTPException(422, detail="Пустой файл")
    allowed = {"image/jpeg", "image/png", "application/pdf", "text/plain"}
    if file.content_type not in allowed:
        raise HTTPException(415, detail="Поддерживаются JPG,PNG,PDF,TXT")
    image = file.content_type.startswith("image/")
    if image:
        from PIL import Image
        import io

        try:
            Image.MAX_IMAGE_PIXELS = 30_000_000
            with Image.open(io.BytesIO(raw)) as im:
                im.verify()
        except Exception:
            raise HTTPException(422, detail="Изображение повреждено или слишком велико")
    if file.content_type == "application/pdf" and not raw.startswith(b"%PDF-"):
        raise HTTPException(422, detail="Неверный форматPDF")
    aid = new_id()
    root = Path(get_settings().blob_root) / "employee_attachments"
    root.mkdir(parents=True, exist_ok=True)
    path = root / aid
    path.write_bytes(raw)
    os.chmod(path, 0o600)
    asset = save_entity(
        db,
        "asset",
        {
            "conversation_id": conversation_id,
            "employee_id": actor.employee_id,
            "filename": Path(file.filename or "file").name[:255],
            "content_type": file.content_type,
            "size": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "storage_path": str(path),
            "validated": True,
        },
        status="READY",
        owner_id=actor.employee_id or actor.id,
        id=aid,
    )
    audit(db, actor, "employee_attachment.upload", aid, {"bytes": len(raw)})
    commit(db)
    return {
        "asset_id": aid,
        "attachment_id": aid,
        "status": asset.status,
        "filename": asset.data["filename"],
    }


class EmployeeInvitation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    channel: Literal["MAX"] = "MAX"
    expires_in_seconds: int = Field(default=86400, ge=600, le=604800)


@router.post("/employees/{employee_id}/binding-invitations", status_code=201)
def issue_employee_invite(
    employee_id: str,
    body: EmployeeInvitation,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST")
    employee_binding_lock(db)
    employee = get_entity(db, employee_id, "employee", lock=True)
    assert_employee(db, actor, employee_id)
    if employee.data.get("employment_stage") == "ENDED" or not employee_is_enabled(
        employee
    ):
        raise HTTPException(409, detail="Доступ сотрудника выключен или найм завершён")
    for old in list_kind(db, "employee_invite", parent_id=employee.id):
        if old.status == "ISSUED":
            old.status = "REVOKED"
    token = secrets.token_urlsafe(32)
    expires = now() + timedelta(seconds=body.expires_in_seconds)
    invite = save_entity(
        db,
        "employee_invite",
        {
            "employee_id": employee.id,
            "episode_id": employee.data.get("episode_id", employee.id),
            "token_hash": hash_token(token),
            "expires_at": expires.isoformat(),
            "issued_by": actor.id,
            "verification_policy": "HR_ISSUED_INVITATION",
        },
        status="ISSUED",
        parent_id=employee.id,
        owner_id=actor.id,
    )
    audit(db, actor, "employee_invite.issue", employee.id)
    commit(db)
    url = f"{get_settings().max_bot_url}?start=join_{token}"
    return {
        "invitation_id": invite.id,
        "employee_id": employee.id,
        "expires_at": expires.isoformat(),
        "invitation_url": url,
        "invite_url": url,
        "url": url,
        "status": "ISSUED",
    }


@router.get("/channel-bindings/{binding_id}")
def employee_binding(
    binding_id: str,
    response: Response,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    b = get_entity(db, binding_id, "max_binding")
    assert_employee(db, actor, b.parent_id)
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST")
    response.headers["ETag"] = etag(b)
    value = serialize(b)
    value["binding_id"] = b.id
    return value


class BindingApprove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    verified_method: str = Field(default="HR_CONFIRMED_KNOWN_CHANNEL", max_length=120)
    reason: str = Field(
        default="Личность подтверждена HR по известному каналу", max_length=2000
    )


def employee_binding_lock(db):
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(76810224)"))


def activate_employee_binding(db, actor, b, *, verified_method, reason):
    """Trusted activation shared by manual legacy approval and HR invite redemption.

    The caller commits: binding, invitation consumption and MAX inbox finish
    remain one atomic transaction when this helper runs in the inbox worker.
    """
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST")
    employee_binding_lock(db)
    employee = get_entity(db, b.parent_id, "employee", lock=True)
    assert_employee(db, actor, employee.id)
    if not employee_is_enabled(employee):
        raise HTTPException(409, "Доступ сотрудника выключен")
    if employee.data.get("employment_stage") == "ENDED" or b.data.get(
        "episode_id"
    ) != employee.data.get("episode_id", employee.id):
        raise HTTPException(409, detail="Период найма изменился")
    if (
        b.status == "ACTIVE"
        and employee.data.get("binding_id") == b.id
        and employee.data.get("max_binding_status") == "VERIFIED"
        and b.data.get("episode_id") == employee.data.get("episode_id")
    ):
        conv = conversation_for(db, employee)
        return {**serialize(b), "binding_id": b.id, "conversation_id": conv.id}
    if b.status != "PENDING_VERIFICATION":
        raise HTTPException(409, detail="Ожидающая привязка не найдена")
    if (
        b.data.get("verification_expires_at")
        and __import__("datetime").datetime.fromisoformat(
            b.data["verification_expires_at"]
        )
        <= now()
    ):
        raise HTTPException(
            410, detail="Срок подтверждения истёк. Создайте новое приглашение"
        )
    if (
        b.data.get("episode_id") != employee.data.get("episode_id", employee.id)
        or employee.data.get("employment_stage") == "ENDED"
    ):
        raise HTTPException(409, detail="Период найма изменился")
    bindings = list_kind(db, "max_binding")
    for other in bindings:
        if (
            other.id != b.id
            and other.status == "ACTIVE"
            and other.data.get("max_user_id") == b.data.get("max_user_id")
            and other.parent_id != employee.id
        ):
            raise HTTPException(409, detail="MAX уже подключён к другому сотруднику")
    for other in bindings:
        if (
            other.id != b.id
            and other.parent_id == employee.id
            and other.status == "ACTIVE"
        ):
            other.status = "REVOKED"
    b.status = "ACTIVE"
    b.data = {
        **b.data,
        "verified_method": verified_method,
        "verified_at": now().isoformat(),
        "verified_by": actor.id,
    }
    employee.data = {
        **employee.data,
        "binding_id": b.id,
        "pending_binding_id": b.id,
        "max_binding_status": "VERIFIED",
    }
    from .domain import notify_simple_bot_started

    started_scenarios = notify_simple_bot_started(db, employee)
    conv = conversation_for(db, employee)
    welcome = "Подключение подтверждено. Можно сразу написать свой вопрос обычным сообщением. Кнопки ниже — быстрые темы; проходить анкету для поиска по документам не требуется."
    from .basic_formulations import basic_prompts_for

    menu = basic_prompts_for(db, employee)
    buttons = [{"text": p["label"], "payload": p["action"]} for p in menu["items"]]
    buttons += [
        {"text": "Мой этап", "payload": {"action": "MY_STAGE"}},
    ]
    if not has_immediate_simple_greeting(db, employee, started_scenarios):
        welcome_message = add_message(db, conv, "BOT", welcome, {"kind": "WELCOME"})
        queue_message(
            db,
            "EMPLOYEE",
            employee.id,
            welcome,
            f"welcome:{employee.data.get('episode_id', employee.id)}",
            buttons=buttons,
            data={"conversation_id": conv.id, "message_id": welcome_message.id},
        )
    audit(
        db,
        actor,
        "employee_binding.approve",
        b.id,
        {"verified_method": verified_method},
        reason=reason,
    )
    return {**serialize(b), "binding_id": b.id, "conversation_id": conv.id}


@router.post("/channel-bindings/{binding_id}/approve")
def approve_employee_binding(
    binding_id: str,
    body: BindingApprove,
    request: Request,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST")
    employee_binding_lock(db)
    binding = get_entity(db, binding_id, "max_binding", lock=True)
    check_etag(request, binding)
    result = activate_employee_binding(
        db, actor, binding, verified_method=body.verified_method, reason=body.reason
    )
    commit(db)
    return result


@router.post("/channel-bindings/{binding_id}/revoke")
def revoke_employee_binding(
    binding_id: str,
    request: Request,
    actor: Actor = Depends(get_actor),
    db: Session = Depends(get_db),
):
    require_role(actor, "HR_ADMIN", "HR_SPECIALIST")
    b = get_entity(db, binding_id, "max_binding", lock=True)
    employee = assert_employee(db, actor, b.parent_id)
    check_etag(request, b)
    b.status = "REVOKED"
    employee.data = {**employee.data, "max_binding_status": "REVOKED"}
    audit(db, actor, "employee_binding.revoke", b.id)
    commit(db)
    return {**serialize(b), "binding_id": b.id}
