"""Trusted MAX inbox -> domain commands. Transport identity never grants HR access."""

from datetime import datetime, timedelta
from pathlib import Path
import hashlib, hmac, re, uuid, os
from fastapi import HTTPException
from sqlalchemy import select
from .db import now, aware, new_id
from .models import Entity, TransportInbox, User
from .common import (
    list_kind,
    get_entity,
    save_entity,
    queue_message,
    audit,
    actor_can_employee,
    employee_is_enabled,
)
from .auth import Actor, hash_token, user_actor
from .settings import get_settings, read_secret
from .chat import (
    conversation_for,
    submit_turn,
    handoff,
    quick_prompts_for,
    employee_binding_lock,
    activate_employee_binding,
)


def direct_notice(db, user_id, text, event_id, suffix="notice"):
    return queue_message(
        db,
        "MAX_USER",
        str(user_id),
        text,
        f"max-event:{event_id}:{suffix}",
        data={"trusted_event_id": event_id},
    )


def actor_for(employee):
    return Actor(
        id=employee.data.get("user_id") or employee.id,
        name=employee.data.get("name", "Сотрудник"),
        role="EMPLOYEE",
        scope={},
        employee_id=employee.id,
    )


def current_employee(db, user_id):
    candidates = [
        b
        for b in list_kind(db, "max_binding")
        if b.status == "ACTIVE" and str(b.data.get("max_user_id")) == str(user_id)
    ]
    for b in candidates:
        e = db.get(Entity, b.parent_id)
        if (
            e
            and e.kind == "employee"
            and e.data.get("binding_id") == b.id
            and b.data.get("episode_id") == e.data.get("episode_id", e.id)
            and e.data.get("employment_stage") != "ENDED"
            and employee_is_enabled(e)
        ):
            return e
    return None


def redeem_invitation(db, user, user_id, chat_id, payload, event_id):
    kind, _, token = payload.partition("_")
    if kind not in {"join", "bind"} or not token:
        return False
    hashed = hash_token(token)
    if kind == "join":
        employee_binding_lock(db)
        invite = next(
            (
                i
                for i in list_kind(db, "employee_invite")
                if i.data.get("token_hash") == hashed
            ),
            None,
        )
        if invite:
            invite = db.scalar(
                select(Entity)
                .where(Entity.id == invite.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        previous = None
        if invite and invite.status == "REDEEMED" and invite.data.get("binding_id"):
            previous = db.get(Entity, invite.data["binding_id"])
            previous_employee = db.get(Entity, previous.parent_id) if previous else None
            if (
                previous
                and previous.status == "ACTIVE"
                and str(previous.data.get("max_user_id")) == str(user_id)
                and previous_employee
                and employee_is_enabled(previous_employee)
                and previous.data.get("episode_id")
                == previous_employee.data.get("episode_id")
            ):
                direct_notice(
                    db,
                    user_id,
                    "Подключение уже выполнено. Откройте меню командой /start.",
                    event_id,
                )
                return True
        if (
            not invite
            or (
                invite.status != "ISSUED"
                and not (
                    invite.status == "REDEEMED"
                    and previous
                    and previous.status == "PENDING_VERIFICATION"
                    and str(previous.data.get("max_user_id")) == str(user_id)
                )
            )
            or aware(datetime.fromisoformat(invite.data["expires_at"])) <= now()
        ):
            direct_notice(
                db,
                user_id,
                "Ссылка недействительна или уже использована. Попросите HR выдать новую.",
                event_id,
            )
            return True
        employee = get_entity(db, invite.data["employee_id"], "employee", lock=True)
        if (
            invite.data["episode_id"] != employee.data.get("episode_id", employee.id)
            or employee.data.get("employment_stage") == "ENDED"
            or not employee_is_enabled(employee)
        ):
            direct_notice(
                db,
                user_id,
                "Период найма изменился. Обратитесь к HR за новой ссылкой.",
                event_id,
            )
            return True
        issuer = db.scalar(
            select(User)
            .where(User.id == invite.owner_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            not issuer
            or not issuer.enabled
            or issuer.role not in {"HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"}
            or not actor_can_employee(db, user_actor(issuer), employee.id)
        ):
            invite.status = "REVOKED"
            direct_notice(
                db,
                user_id,
                "Приглашение больше не действует. Попросите HR выдать новую ссылку.",
                event_id,
            )
            return True
        try:
            with db.begin_nested():
                binding = previous or save_entity(
                    db,
                    "max_binding",
                    {
                        "employee_id": employee.id,
                        "episode_id": invite.data["episode_id"],
                        "max_user_id": str(user_id),
                        "max_chat_id": str(chat_id or ""),
                        "display_name": user.get("name")
                        or user.get("first_name")
                        or "Пользователь MAX",
                        "invitation_id": invite.id,
                        "redeemed_at": now().isoformat(),
                        "verification_expires_at": (
                            now() + timedelta(days=1)
                        ).isoformat(),
                    },
                    parent_id=employee.id,
                    status="PENDING_VERIFICATION",
                )
                activate_employee_binding(
                    db,
                    user_actor(issuer),
                    binding,
                    verified_method="HR_ISSUED_INVITATION",
                    reason="Подключение по одноразовому приглашению HR",
                )
                invite.status = "REDEEMED"
                invite.data = {
                    **invite.data,
                    "binding_id": binding.id,
                    "redeemed_at": now().isoformat(),
                }
                for old in list_kind(db, "max_binding", parent_id=employee.id):
                    if old.id != binding.id and old.status == "PENDING_VERIFICATION":
                        old.status = "SUPERSEDED"
        except HTTPException:
            direct_notice(
                db,
                user_id,
                "Не удалось подключить этот аккаунт MAX. Возможно, он уже связан с другим сотрудником. Попросите HR проверить приглашение.",
                event_id,
            )
            return True
        audit(
            db,
            None,
            "employee_binding.redeem",
            binding.id,
            {"employee_id": employee.id},
        )
        return True
    binding = next(
        (
            b
            for b in list_kind(db, "principal_binding")
            if b.data.get("token_hash") == hashed
        ),
        None,
    )
    if binding:
        binding = db.scalar(
            select(Entity)
            .where(Entity.id == binding.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    if (
        not binding
        or binding.status != "ISSUED"
        or aware(datetime.fromisoformat(binding.data["expires_at"])) <= now()
    ):
        direct_notice(
            db,
            user_id,
            "Ссылка недействительна. Создайте новое подключение MAX в своём кабинете.",
            event_id,
        )
        return True
    binding.status = "PENDING_WEB_CONFIRM"
    binding.data = {
        **binding.data,
        "max_user_id": str(user_id),
        "max_chat_id": str(chat_id or ""),
        "display_name": user.get("name")
        or user.get("first_name")
        or "Пользователь MAX",
        "redeemed_at": now().isoformat(),
    }
    direct_notice(
        db,
        user_id,
        "MAX найден. Вернитесь в свой кабинет и подтвердите подключение уведомлений.",
        event_id,
    )
    return True


def callback_payload(db, raw, user_id, employee):
    match = re.fullmatch(r"k:([0-9a-f-]{36}):([0-9a-f]{24})", str(raw))
    if not match:
        raise HTTPException(409, "Кнопка не распознана. Откройте актуальное меню.")
    aid, sig = match.groups()
    expected = hmac.new(
        read_secret(get_settings().secret_key_file).encode(),
        aid.encode(),
        hashlib.sha256,
    ).hexdigest()[:24]
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(403, "Кнопка недействительна")
    action = db.scalar(select(Entity).where(Entity.id == aid).with_for_update())
    if (
        not action
        or action.kind != "callback_action"
        or action.status != "ACTIVE"
        or aware(datetime.fromisoformat(action.data["expires_at"])) <= now()
    ):
        raise HTTPException(409, "Срок действия кнопки истёк")
    current = current_employee(db, user_id)
    if not current or not employee or current.id != employee.id:
        raise HTTPException(403, "Кнопка недействительна для этой привязки")
    if action.data.get("episode_id") != employee.data.get("episode_id", employee.id):
        raise HTTPException(409, "Кнопка относится к прежнему периоду найма")
    if (
        action.data["recipient_kind"] != "EMPLOYEE"
        or not employee
        or action.data["recipient_id"] != employee.id
    ):
        raise HTTPException(403, "Кнопка адресована другому пользователю")
    return action.data["payload"]


def service_menu(db, employee, conv, event_id):
    from .basic_formulations import basic_prompts_for

    menu = basic_prompts_for(db, employee)
    buttons = [
        {"text": p["label"], "payload": p["action"]} for p in menu.get("items", [])
    ]
    buttons += [
        {"text": "Помощь", "payload": {"action": "HELP"}},
        {"text": "Еда", "payload": {"action": "FOOD"}},
        {"text": "Мой этап", "payload": {"action": "MY_STAGE"}},
        {"text": "Продолжить опрос", "payload": {"action": "CONTINUE_SURVEY"}},
    ]
    queue_message(
        db,
        "EMPLOYEE",
        employee.id,
        "Напишите вопрос своими словами или выберите тему.",
        f"menu:{event_id}",
        buttons=buttons,
    )


def import_staged_image(db, employee, conv, staged):
    """Only connector-created opaque IDs in a separate inbound volume are accepted."""
    sid = str(staged.get("id", ""))
    if not re.fullmatch("[0-9a-f]{64}", sid):
        return None
    inbound = Path(os.getenv("INBOUND_DIR", "/inbound")).resolve()
    path = inbound / sid
    if path.is_symlink() or not path.resolve().is_relative_to(inbound):
        return None
    if not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
        return None
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != staged.get("sha256"):
        return None
    from PIL import Image
    import io

    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.width * image.height > 32_000_000 or image.format not in {
                "JPEG",
                "PNG",
                "WEBP",
            }:
                return None
            image.verify()
            fmt = image.format
    except Exception:
        return None
    aid = new_id()
    dest = Path(get_settings().blob_root) / "employee" / employee.id / aid
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    dest.chmod(0o600)
    save_entity(
        db,
        "asset",
        {
            "employee_id": employee.id,
            "conversation_id": conv.id,
            "filename": "photo." + {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}[fmt],
            "content_type": {
                "JPEG": "image/jpeg",
                "PNG": "image/png",
                "WEBP": "image/webp",
            }[fmt],
            "storage_path": str(dest),
            "sha256": hashlib.sha256(content).hexdigest(),
            "purpose": "EMPLOYEE_PHOTO",
            "validated": True,
        },
        status="READY",
        parent_id=conv.id,
        owner_id=employee.id,
        id=aid,
    )
    return aid


def handle_callback(db, employee, conv, payload, event_id):
    from .domain import handle_domain_callback

    actor = actor_for(employee)
    action = payload.get("action") or payload.get("kind")
    if action == "HELP":
        service_menu(db, employee, conv, event_id)
        return
    if action == "SCENARIO_FORMULATION":
        from .domain import resolve_simple_formulation

        question = resolve_simple_formulation(db, employee, payload)
        submit_turn(
            db,
            actor,
            conv,
            {"kind": "RESOLVED_FORMULATION", "text": question},
            str(uuid.uuid5(uuid.NAMESPACE_URL, event_id)),
        )
        return
    if action == "FOOD":
        menu = quick_prompts_for(db, conv)
        item = next(
            (p for p in menu.get("items", []) if p.get("prompt_key") == "food"), None
        )
        question = (
            item["query_text"] if item else "Где можно пообедать на моей площадке?"
        )
        submit_turn(
            db,
            actor,
            conv,
            {"kind": "RESOLVED_FORMULATION", "text": question},
            str(uuid.uuid5(uuid.NAMESPACE_URL, event_id)),
        )
        return
    if action == "CONTACT_RECRUITER":
        from .domain import verified_hr_contact

        contact = verified_hr_contact(db, employee)
        if contact.get("available"):
            queue_message(
                db,
                "EMPLOYEE",
                employee.id,
                "Ваш назначенный рекрутер: "
                + contact["label"]
                + ". По кнопке вы перейдёте в его личный MAX-диалог. Открытие ссылки не подтверждает, что разговор состоялся.",
                f"recruiter-contact:{event_id}",
                buttons=[
                    {"text": "Написать рекрутеру", "url": contact["url"]},
                    {
                        "text": "Обращение через Корсика",
                        "payload": {"action": "CONTACT_HR"},
                    },
                ],
                data={
                    "contact_id": contact["contact_id"],
                    "contact_version": contact["contact_version"],
                    "required_assigned_hr_id": contact["user_id"],
                },
            )
        else:
            handoff(db, actor, conv, "VERIFIED_RECRUITER_CONTACT_UNAVAILABLE")
        return
    if action in {"CONTACT_HR", "HANDOFF"}:
        handoff(db, actor, conv, "EMPLOYEE_REQUEST")
        return
    if action == "MY_STAGE":
        journeys = [
            j
            for j in list_kind(db, "journey", parent_id=employee.id)
            if j.status != "CANCELLED"
        ]
        text = "Ваш этап: " + str(employee.data.get("employment_stage", "PREBOARDING"))
        if journeys:
            j = journeys[-1]
            text += (
                "\nВыполнено шагов: "
                + str(len(j.data.get("completed_steps", [])))
                + ". Следующие сообщения придут по вашему расписанию."
            )
        queue_message(db, "EMPLOYEE", employee.id, text, f"stage:{event_id}")
        return
    if action == "REQUEST_SOURCE":
        from .assets import queue_original_source

        return queue_original_source(db, employee, conv, payload, event_id)
    if action == "RAG_QUERY":
        menu_input = {
            **payload,
            "kind": "RAG_QUERY",
            "prompt_key": payload.get("prompt_key") or payload.get("item_id"),
        }
        submit_turn(
            db, actor, conv, menu_input, str(uuid.uuid5(uuid.NAMESPACE_URL, event_id))
        )
        return
    result = handle_domain_callback(db, conv, payload)
    # Domain records can contain a `question` definition dict. Only an explicit
    # conversion of a human capture draft is a conversational question.
    if (
        action == "CAPTURE_TO_QUESTION"
        and isinstance(result, dict)
        and isinstance(result.get("capture"), dict)
        and result["capture"].get("draft_type") == "TEXT"
        and isinstance(result.get("question"), str)
        and result["question"].strip()
    ):
        submit_turn(
            db,
            actor,
            conv,
            {"kind": "TEXT", "text": result["question"].strip()},
            str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    "korsik:capture-question:" + payload["capture_id"],
                )
            ),
        )
    elif (
        isinstance(result, dict)
        and isinstance(result.get("text"), str)
        and result["text"].strip()
    ):
        queue_message(
            db, "EMPLOYEE", employee.id, result["text"], f"action-result:{event_id}"
        )
    elif result is None:
        raise HTTPException(
            409, "Действие больше недоступно. Откройте меню командой /start."
        )


def process_max_update(db, job):
    inbox = db.scalar(
        select(TransportInbox)
        .where(TransportInbox.id == job.payload["inbox_id"])
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if not inbox or inbox.status == "DONE":
        return
    event = inbox.payload
    kind = event.get("update_type")
    message = event.get("message") or {}
    callback = event.get("callback") or {}
    user = callback.get("user") or event.get("user") or message.get("sender") or {}
    uid = user.get("user_id")
    event_id = inbox.id
    if not uid or user.get("is_bot"):
        inbox.status = "DONE"
        return
    recipient = message.get("recipient") or {}
    chat_id = event.get("chat_id") or recipient.get("chat_id")
    if recipient.get("chat_type") in {"chat", "channel"}:
        inbox.status = "DONE"
        return
    text = (message.get("body") or {}).get("text") or ""
    payload = (
        str(event.get("payload") or "")
        if kind == "bot_started"
        else (text.partition(" ")[2].strip() if text.startswith("/start ") else "")
    )
    if payload and redeem_invitation(db, user, uid, chat_id, payload, event_id):
        inbox.status = "DONE"
        return
    employee = current_employee(db, uid)
    if kind in {"bot_stopped", "dialog_removed"}:
        if employee:
            b = db.get(Entity, employee.data["binding_id"])
            b.status = "SUSPENDED"
            employee.data = {**employee.data, "max_binding_status": "SUSPENDED"}
        inbox.status = "DONE"
        return
    if not employee:
        disabled = any(
            b.status == "ACTIVE"
            and str(b.data.get("max_user_id")) == str(uid)
            and (candidate := db.get(Entity, b.parent_id))
            and not employee_is_enabled(candidate)
            for b in list_kind(db, "max_binding")
        )
        direct_notice(
            db,
            uid,
            (
                "Доступ к Корсику временно выключен. Обратитесь к HR."
                if disabled
                else "Здравствуйте! Я Корсик — помощник по адаптации. Для доступа попросите HR прислать личную ссылку-приглашение. Если вы HR или руководитель, подключите MAX в своём веб-кабинете."
            ),
            event_id,
        )
        inbox.status = "DONE"
        return
    conv = conversation_for(db, employee)
    conv = get_entity(db, conv.id, "conversation", lock=True)
    from .chat import continue_unclaimed_bot_search

    continue_unclaimed_bot_search(db, conv)
    actor = actor_for(employee)
    try:
        if kind == "message_callback":
            action = callback_payload(db, callback.get("payload"), uid, employee)
            handle_callback(db, employee, conv, action, event_id)
        elif kind == "bot_started" or text.strip().lower() in {
            "/start",
            "/help",
            "/menu",
            "меню",
        }:
            from .domain import notify_simple_bot_started

            started_scenarios = notify_simple_bot_started(db, employee)
            from .chat import has_immediate_simple_greeting

            if not has_immediate_simple_greeting(db, employee, started_scenarios):
                service_menu(db, employee, conv, event_id)
        elif kind == "message_created" and text.strip().lower() in {
            "/hr",
            "/food",
            "/stage",
            "/continue",
        }:
            action = {
                "/hr": "CONTACT_HR",
                "/food": "FOOD",
                "/stage": "MY_STAGE",
                "/continue": "CONTINUE_SURVEY",
            }[text.strip().lower()]
            if action == "FOOD":
                for capture in list_kind(db, "input_capture", parent_id=employee.id):
                    if capture.status in {"OPEN", "DRAFT"}:
                        capture.status = "CANCELLED"
            handle_callback(db, employee, conv, {"action": action}, event_id)
        elif kind == "message_created":
            captures = [
                c
                for c in list_kind(db, "input_capture", parent_id=employee.id)
                if c.status in {"OPEN", "DRAFT"}
                and aware(datetime.fromisoformat(c.data["expires_at"])) > now()
            ]
            cap = max(captures, key=lambda c: aware(c.created_at)) if captures else None
            staged = event.get("_staged_attachments") or []
            from .simple_scenarios import pending_free_answer

            free_capture = pending_free_answer(db, employee, conv)
            if text.strip() and free_capture:
                submit_turn(
                    db, actor, conv, {"kind": "TEXT", "text": text.strip()},
                    str(uuid.uuid5(uuid.NAMESPACE_URL, event_id)),
                )
            elif cap and conv.data.get("mode", "BOT") == "BOT":
                from .domain import capture_draft

                asset_id = (
                    import_staged_image(db, employee, conv, staged[0])
                    if staged
                    else None
                )
                if asset_id:
                    capture_draft(db, actor, cap.id, {"attachment_id": asset_id})
                elif text.strip():
                    capture_draft(db, actor, cap.id, {"text": text})
                else:
                    queue_message(
                        db,
                        "EMPLOYEE",
                        employee.id,
                        "Не удалось прочитать фото. Отправьте JPEG или PNG размером до 10 МБ.",
                        f"attachment-error:{event_id}",
                    )
            elif text.strip():
                submit_turn(
                    db,
                    actor,
                    conv,
                    {"kind": "TEXT", "text": text.strip()},
                    str(uuid.uuid5(uuid.NAMESPACE_URL, event_id)),
                )
            else:
                queue_message(
                    db,
                    "EMPLOYEE",
                    employee.id,
                    "Для вопроса напишите текст. Чтобы ответить фото в анкете, сначала нажмите кнопку ввода у конкретного вопроса.",
                    f"attachment-help:{event_id}",
                )
    except HTTPException as error:
        # The event is consumed once; a stale action is a user-visible business outcome.
        detail = (
            error.detail
            if isinstance(error.detail, str)
            else error.detail.get(
                "message", "Действие устарело. Откройте меню командой /start."
            )
        )
        buttons = []
        if isinstance(error.detail, dict) and error.detail.get("code") == "stale_action":
            from .basic_formulations import simple_basic_prompt_buttons

            # Reject the obsolete revision; offer a newly signed current menu.
            buttons = simple_basic_prompt_buttons(db, employee)
        queue_message(
            db, "EMPLOYEE", employee.id, detail[:1000], f"event-rejected:{event_id}",
            buttons=buttons,
        )
    inbox.status = "DONE"
