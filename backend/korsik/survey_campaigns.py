"""Versioned, voluntary identified surveys; SQL scheduling and existing typed answers.

No diagnoses, personality scores, anonymous claims or model-based profiling.
All helpers participate in the caller's transaction. HTTP handlers alone commit.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from .auth import get_actor, user_actor
from .common import (
    actor_can_employee,
    employee_is_enabled,
    audit,
    can_delegate_scope,
    check_etag,
    commit,
    enqueue,
    etag,
    fingerprint,
    get_entity,
    list_kind,
    require_role,
    save_entity,
    serialize,
)
from .db import get_db, now, aware
from .models import Entity, Job, User

router = APIRouter()
HR = ("HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT")
READ = (*HR, "MANAGER")
DELIVER = "DOMAIN_SURVEY_CAMPAIGN_DELIVER"
REMIND = "DOMAIN_SURVEY_CAMPAIGN_REMINDER"
PRIVACY = (
    "Участие добровольное. Можно пропустить любой вопрос или отказаться от опроса. "
    "Это не анонимный опрос: ответы связаны с вашей карточкой и доступны HR в пределах его полномочий. "
    "Руководителям доступны только сводки по группам от 5 участников, без личных ответов. "
    "Опрос не является диагностикой и не рассчитывает психологический профиль."
)
TERMINAL = {"COMPLETED", "DECLINED", "EXPIRED", "CANCELLED"}


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Scope(Input):
    all: bool = False
    site_ids: list[str] = Field(default_factory=list, max_length=100)
    legal_entity_ids: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def constrained(self):
        if self.all and (self.site_ids or self.legal_entity_ids):
            raise ValueError("Общую аудиторию нельзя смешивать с ограничениями")
        if not self.all and not (self.site_ids or self.legal_entity_ids):
            raise ValueError("Укажите площадку или юрлицо")
        for values in (self.site_ids, self.legal_entity_ids):
            if any(not x or len(x) > 100 for x in values) or len(set(values)) != len(
                values
            ):
                raise ValueError("Идентификаторы должны быть непустыми и уникальными")
        return self


class Option(Input):
    value: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    label: str = Field(min_length=1, max_length=120)


class Question(Input):
    question_key: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,40}$")
    prompt: str = Field(min_length=3, max_length=1500)
    type: Literal["LIKERT", "SINGLE_CHOICE", "MULTI_CHOICE", "TEXT"]
    options: list[Option] = Field(default_factory=list, max_length=10)
    minimum: int = Field(default=1, ge=1, le=10)
    maximum: int = Field(default=5, ge=2, le=10)
    max_selected: int | None = Field(default=None, ge=1, le=10)
    max_text_length: int = Field(default=2000, ge=1, le=6000)

    @model_validator(mode="after")
    def type_rules(self):
        if self.type in {"SINGLE_CHOICE", "MULTI_CHOICE"}:
            if len(self.options) < 2 or len({o.value for o in self.options}) != len(
                self.options
            ):
                raise ValueError("Нужны минимум два уникальных варианта")
            if self.max_selected and self.max_selected > len(self.options):
                raise ValueError("Лимит выбора превышает число вариантов")
        elif self.options:
            raise ValueError("У этого типа вопроса нет вариантов")
        if self.type == "LIKERT" and self.minimum >= self.maximum:
            raise ValueError("Начало шкалы должно быть меньше конца")
        return self


class Draft(Input):
    title: str = Field(min_length=3, max_length=160)
    description: str = Field(default="", max_length=2000)
    purpose: Literal["WELLBEING", "ADAPTATION", "CUSTOM"] = "WELLBEING"
    questions: list[Question] = Field(min_length=1, max_length=25)
    thank_you: str = Field(
        default="Спасибо! Ваши ответы сохранены.", min_length=3, max_length=1000
    )
    reason: str = Field(min_length=3, max_length=2000)

    @model_validator(mode="after")
    def unique_questions(self):
        if len({q.question_key for q in self.questions}) != len(self.questions):
            raise ValueError("Идентификаторы вопросов повторяются")
        if (
            len(self.title + self.description + PRIVACY + self.questions[0].prompt)
            > 3400
        ):
            raise ValueError(
                "Первое сообщение слишком длинное: сократите пояснение или вопрос"
            )
        return self


class TemplateCreate(Draft):
    scope: Scope | None = None


class Reason(Input):
    reason: str = Field(min_length=3, max_length=2000)


class VersionCreate(Reason):
    base_version_id: str | None = Field(default=None, min_length=1, max_length=36)


class Review(Reason):
    decision: Literal["APPROVE", "REJECT"]


class Audience(Input):
    employee_ids: list[str] = Field(default_factory=list, max_length=1000)
    site_ids: list[str] = Field(default_factory=list, max_length=100)
    employment_stages: list[Literal["PREBOARDING", "ACTIVE"]] = Field(
        default_factory=lambda: ["PREBOARDING", "ACTIVE"], min_length=1
    )
    employment_types: list[Literal["OFFICE", "PRODUCTION"]] = Field(
        default_factory=list
    )
    user_roles: list[
        Literal["EMPLOYEE", "MANAGER", "HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"]
    ] = Field(default_factory=lambda: ["EMPLOYEE"], min_length=1)

    @model_validator(mode="after")
    def unique_values(self):
        for values in (
            self.employee_ids,
            self.site_ids,
            self.employment_stages,
            self.employment_types,
            self.user_roles,
        ):
            if len(values) != len(set(values)) or any(not x for x in values):
                raise ValueError("Аудитория содержит пустые или повторённые значения")
        return self


class Schedule(Input):
    timezone: str = Field(default="Europe/Moscow", max_length=100)
    starts_at: datetime
    ends_at: datetime
    response_hours: int = Field(default=168, ge=1, le=720)
    reminder_hours: list[int] = Field(default_factory=lambda: [24, 48], max_length=3)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def explicit_offset(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Укажите дату с часовым поясом")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def valid_period(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Неизвестный часовой пояс")
        if self.ends_at <= self.starts_at or self.ends_at - self.starts_at > timedelta(
            days=90
        ):
            raise ValueError("Период должен быть положительным и не превышать 90 дней")
        if any(
            type(x) is not int or x < 1 or x >= self.response_hours
            for x in self.reminder_hours
        ) or len(set(self.reminder_hours)) != len(self.reminder_hours):
            raise ValueError(
                "Напоминания — уникальные часы от получения первого вопроса, раньше срока ответа"
            )
        return self


class CampaignInput(Input):
    name: str = Field(min_length=3, max_length=160)
    version_id: str = Field(min_length=1, max_length=36)
    audience: Audience
    schedule: Schedule
    reason: str = Field(min_length=3, max_length=2000)
    preview_hash: str | None = Field(default=None, max_length=64)


def _reply(entity, status=200):
    return JSONResponse(
        serialize(entity), status_code=status, headers={"ETag": etag(entity)}
    )


def _scope_matches(scope, employee):
    return bool(
        scope.get("all")
        or (
            (
                not scope.get("site_ids")
                or employee.data.get("site_id") in scope["site_ids"]
            )
            and (
                not scope.get("legal_entity_ids")
                or employee.data.get("legal_entity_id") in scope["legal_entity_ids"]
            )
            and (scope.get("site_ids") or scope.get("legal_entity_ids"))
        )
    )


def _template(db, actor, template_id, lock=False):
    require_role(actor, *HR)
    template = get_entity(db, template_id, "survey_template", lock=lock)
    if not can_delegate_scope(actor.scope, template.data["scope"]):
        raise HTTPException(404, "Опрос недоступен")
    return template


def _version(db, actor, version_id, lock=False):
    version = get_entity(db, version_id, "survey_template_version", lock=lock)
    _template(db, actor, version.parent_id)
    return version


def _actor_current(db, actor_id):
    user = db.scalar(
        select(User)
        .where(User.id == actor_id)
        .execution_options(populate_existing=True)
    )
    return user_actor(user) if user and user.enabled and user.role in HR else None


def _audience_matches(db, audience, employee):
    if (
        not employee_is_enabled(employee)
        or employee.data.get("employment_stage") not in audience["employment_stages"]
    ):
        return False
    for field, key in (
        ("employee_ids", "id"),
        ("site_ids", "site_id"),
        ("employment_types", "employment_type"),
    ):
        value = employee.id if key == "id" else employee.data.get(key)
        if audience.get(field) and value not in audience[field]:
            return False
    user_id = employee.data.get("user_id")
    user = db.get(User, user_id) if user_id else None
    if user and (not user.enabled or user.employee_id != employee.id):
        return False
    role = (
        user.role
        if user and user.enabled and user.employee_id == employee.id
        else "EMPLOYEE"
    )
    return role in audience["user_roles"]


def _definition(version):
    source = version.data
    questions = []
    for index, q in enumerate(source["questions"]):
        prompt = q["prompt"]
        if index == 0:
            prompt = (
                source["title"]
                + "\n\n"
                + PRIVACY
                + ("\n\n" + source["description"] if source.get("description") else "")
                + "\n\n"
                + prompt
            )
        item = {
            "question_key": "CUSTOM-" + q["question_key"],
            "type": "NUMERIC" if q["type"] == "LIKERT" else q["type"],
            "prompt": {"text_ty": prompt, "text_vy": prompt},
            "required": False,
            "authored_prompt": q["prompt"],
            "minimum": q["minimum"],
            "maximum": q["maximum"],
            "max_text_length": q["max_text_length"],
            "next_question_key": (
                "CUSTOM-" + source["questions"][index + 1]["question_key"]
                if index + 1 < len(source["questions"])
                else None
            ),
        }
        if q["options"]:
            item["options"] = [
                {
                    "value": o["value"],
                    "label": {"text_ty": o["label"], "text_vy": o["label"]},
                }
                for o in q["options"]
            ]
            item.update(
                min_selected=1, max_selected=q.get("max_selected") or len(q["options"])
            )
        questions.append(item)
    return {
        "survey_key": "CUSTOM-" + version.id,
        "questions": questions,
        "start_question_key": questions[0]["question_key"],
        "thank_you": {"text_ty": source["thank_you"], "text_vy": source["thank_you"]},
        "expiry_days": 7,
        "privacy_notice": PRIVACY,
        "participation": "VOLUNTARY_IDENTIFIED",
        "analysis_policy": "NO_AUTOMATIC_PSYCHOLOGICAL_SCORING",
    }


@router.get("/survey-templates")
def templates(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR)
    return {
        "items": [
            serialize(t)
            for t in list_kind(db, "survey_template")
            if can_delegate_scope(actor.scope, t.data["scope"])
        ]
    }


@router.post("/survey-templates", status_code=201)
def create_template(body: TemplateCreate, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *HR)
    scope = (
        body.scope.model_dump()
        if body.scope
        else {
            k: v
            for k, v in actor.scope.items()
            if k in {"all", "site_ids", "legal_entity_ids"}
        }
    )
    try:
        scope = Scope.model_validate(scope).model_dump()
    except ValueError:
        raise HTTPException(
            403, "Для конструктора требуется область площадок или юрлиц"
        )
    if not can_delegate_scope(actor.scope, scope):
        raise HTTPException(403, "Аудитория за пределами ваших полномочий")
    for field, kind in (("site_ids", "site"), ("legal_entity_ids", "organization")):
        for id in scope.get(field, []):
            get_entity(db, id, kind)
    template = save_entity(
        db,
        "survey_template",
        {"title": body.title, "scope": scope, "published_version_id": None},
        owner_id=actor.id,
    )
    version = save_entity(
        db,
        "survey_template_version",
        body.model_dump(exclude={"scope", "reason"}),
        status="DRAFT",
        parent_id=template.id,
        owner_id=actor.id,
    )
    audit(
        db,
        actor,
        "survey.template.create",
        template.id,
        {"version_id": version.id},
        reason=body.reason,
    )
    commit(db)
    return _reply(template, 201)


@router.get("/survey-templates/{template_id}")
def template_get(template_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    template = _template(db, actor, template_id)
    return {
        **serialize(template),
        "versions": [
            serialize(v)
            for v in list_kind(db, "survey_template_version", parent_id=template.id)
        ],
        "privacy_notice": PRIVACY,
    }


@router.post("/survey-templates/{template_id}/versions", status_code=201)
def new_version(
    template_id: str,
    body: VersionCreate,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    template = _template(db, actor, template_id, lock=True)
    check_etag(request, template)
    versions = list_kind(db, "survey_template_version", parent_id=template.id)
    if any(v.status == "DRAFT" for v in versions):
        raise HTTPException(409, "Сначала завершите существующий черновик")
    base_id = getattr(body, "base_version_id", None) or template.data.get("published_version_id")
    base = _version(db, actor, base_id) if base_id else (versions[-1] if versions else None)
    if not base or base.parent_id != template.id:
        raise HTTPException(404, "Исходная версия опроса недоступна")
    version = save_entity(
        db,
        "survey_template_version",
        {
            k: deepcopy(base.data[k])
            for k in ("title", "description", "purpose", "questions", "thank_you")
        },
        status="DRAFT",
        parent_id=template.id,
        owner_id=actor.id,
    )
    template.data = {**template.data, "draft_created_at": now().isoformat()}
    audit(db, actor, "survey.version.create", version.id, {"base_version_id": base.id}, reason=body.reason)
    commit(db)
    return _reply(version, 201)


@router.get("/survey-template-versions/{version_id}")
def version_get(version_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    return _reply(_version(db, actor, version_id))


@router.patch("/survey-template-versions/{version_id}")
def edit_version(
    version_id: str,
    body: Draft,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    version = _version(db, actor, version_id, lock=True)
    check_etag(request, version)
    if version.status != "DRAFT":
        raise HTTPException(
            409, "Изменять можно только черновик; создайте новую версию"
        )
    version.data = body.model_dump(exclude={"reason"})
    audit(db, actor, "survey.version.edit", version.id, reason=body.reason)
    commit(db)
    return _reply(version)


@router.post("/survey-template-versions/{version_id}/preview")
def preview_version(version_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    version = _version(db, actor, version_id)
    return {
        "definition": _definition(version),
        "privacy_notice": PRIVACY,
        "outbound_messages_created": 0,
        "simulation_only": True,
    }


@router.post("/survey-template-versions/{version_id}/submit-review")
def submit_review(
    version_id: str,
    body: Reason,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    version = _version(db, actor, version_id, lock=True)
    check_etag(request, version)
    if version.status != "DRAFT":
        raise HTTPException(409, "На проверку отправляется черновик")
    Draft.model_validate(
        {
            **{
                k: version.data[k]
                for k in ("title", "description", "purpose", "questions", "thank_you")
            },
            "reason": body.reason,
        }
    )
    version.status = "REVIEW_REQUIRED"
    version.data = {**version.data, "content_fingerprint": fingerprint(version.data)}
    audit(db, actor, "survey.version.submit", version.id, reason=body.reason)
    commit(db)
    return _reply(version)


@router.post("/survey-template-versions/{version_id}/review")
def review_version(
    version_id: str,
    body: Review,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    version = _version(db, actor, version_id, lock=True)
    check_etag(request, version)
    if version.status != "REVIEW_REQUIRED":
        raise HTTPException(409, "Версия не ожидает проверки")
    version.status = "APPROVED" if body.decision == "APPROVE" else "DRAFT"
    version.data = {
        **version.data,
        "reviewed_by": actor.id,
        "reviewed_at": now().isoformat(),
    }
    audit(
        db,
        actor,
        "survey.version.review",
        version.id,
        {"decision": body.decision},
        reason=body.reason,
    )
    commit(db)
    return _reply(version)


@router.post("/survey-template-versions/{version_id}/publish")
def publish_version(
    version_id: str,
    body: Reason,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    require_role(actor, *HR)
    # Serialize publication/new-draft commands on the template before locking
    # its version. The version ETag still fences concurrent content edits.
    visible = _version(db, actor, version_id)
    template = _template(db, actor, visible.parent_id, lock=True)
    version = get_entity(db, version_id, "survey_template_version", lock=True)
    check_etag(request, version)
    if version.status not in {"DRAFT", "REVIEW_REQUIRED", "APPROVED"}:
        raise HTTPException(409, "Эта версия уже опубликована или недоступна для публикации")
    content = {k: deepcopy(version.data[k]) for k in ("title", "description", "purpose", "questions", "thank_you")}
    Draft.model_validate({**content, "reason": body.reason})
    approved_from = version.status
    content_hash = fingerprint(content)
    version.status = "PUBLISHED"
    version.data = {
        **version.data,
        "published_by": actor.id,
        "published_at": now().isoformat(),
        # A publish click is the responsible HR's approval of this exact
        # version, not a claim that another person reviewed it beforehand.
        "publication_approval": {
            "actor_id": actor.id,
            "content_fingerprint": content_hash,
            "from_status": approved_from,
            "method": "EXPLICIT_PUBLISH",
        },
    }
    template.data = {
        **template.data,
        "title": version.data["title"],
        "published_version_id": version.id,
    }
    audit(db, actor, "survey.version.publish", version.id, {"content_fingerprint": content_hash, "approved_from_status": approved_from, "approval_method": "EXPLICIT_PUBLISH"}, reason=body.reason)
    commit(db)
    return _reply(version)


def _campaign(db, actor, campaign_id, lock=False):
    require_role(actor, *HR)
    campaign = get_entity(db, campaign_id, "survey_campaign", lock=lock)
    _template(db, actor, campaign.data["template_id"])
    return campaign


def _preview(db, actor, body):
    version = _version(db, actor, body.version_id)
    template = _template(db, actor, version.parent_id)
    if version.status != "PUBLISHED":
        raise HTTPException(409, "Для рассылки нужна опубликованная версия")
    if body.schedule.ends_at <= now() or body.schedule.starts_at > now() + timedelta(
        days=365
    ):
        raise HTTPException(422, "Срок завершился либо начало позже чем через год")
    for id in body.audience.employee_ids:
        employee = get_entity(db, id, "employee")
        if not actor_can_employee(db, actor, id) or not _scope_matches(
            template.data["scope"], employee
        ):
            raise HTTPException(404, "Сотрудник недоступен для этой кампании")
    for id in body.audience.site_ids:
        get_entity(db, id, "site")
        if (
            template.data["scope"].get("site_ids")
            and id not in template.data["scope"]["site_ids"]
        ):
            raise HTTPException(403, "Площадка вне аудитории опроса")
    people = [
        e
        for e in list_kind(db, "employee")
        if actor_can_employee(db, actor, e.id)
        and _scope_matches(template.data["scope"], e)
        and _audience_matches(db, body.audience.model_dump(), e)
    ]
    # Freeze exact identities and episodes only after the HR saw the preview.
    recipients = [
        {
            "employee_id": e.id,
            "episode_id": e.data.get("episode_id", e.id),
            "name": e.data.get("name", ""),
            "site_id": e.data.get("site_id"),
        }
        for e in people
    ]
    payload = body.model_dump(mode="json", exclude={"preview_hash", "reason"})
    pin = fingerprint(
        {
            "payload": payload,
            "version_fingerprint": fingerprint(version.data),
            "scope": template.data["scope"],
            "recipients": recipients,
        }
    )
    return {
        "preview_hash": pin,
        "recipient_count": len(recipients),
        "recipients": recipients,
        "schedule": payload["schedule"],
        "privacy_notice": PRIVACY,
        "outbound_messages_created": 0,
    }


@router.post("/survey-campaigns/preview")
def preview_campaign(body: CampaignInput, db=Depends(get_db), actor=Depends(get_actor)):
    return _preview(db, actor, body)


@router.post("/survey-campaigns", status_code=201)
def create_campaign(body: CampaignInput, db=Depends(get_db), actor=Depends(get_actor)):
    preview = _preview(db, actor, body)
    if not body.preview_hash or body.preview_hash != preview["preview_hash"]:
        raise HTTPException(
            409,
            "Аудитория или настройки изменились. Повторите предварительный просмотр",
        )
    if not preview["recipient_count"]:
        raise HTTPException(422, "В аудитории нет действующих сотрудников")
    version = _version(db, actor, body.version_id)
    campaign = save_entity(
        db,
        "survey_campaign",
        {
            **body.model_dump(mode="json", exclude={"reason", "preview_hash"}),
            "template_id": version.parent_id,
            "scope": deepcopy(_template(db, actor, version.parent_id).data["scope"]),
            "generation": 1,
            "created_by": actor.id,
            "creator_scope": deepcopy(actor.scope),
            "recipient_count": preview["recipient_count"],
            "privacy_notice": PRIVACY,
        },
        status="SCHEDULED",
        owner_id=actor.id,
    )
    for recipient in preview["recipients"]:
        save_entity(
            db,
            "survey_campaign_enrollment",
            {
                "campaign_id": campaign.id,
                "employee_id": recipient["employee_id"],
                "episode_id": recipient["episode_id"],
                "version_id": version.id,
                "generation": 1,
                "run_id": None,
            },
            status="WAITING",
            parent_id=campaign.id,
        )
    audit(
        db,
        actor,
        "survey.campaign.create",
        campaign.id,
        {"recipient_count": preview["recipient_count"]},
        reason=body.reason,
    )
    commit(db)
    return _reply(campaign, 201)


@router.patch("/survey-campaigns/{campaign_id}")
def replan_campaign(
    campaign_id: str,
    body: CampaignInput,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    campaign = _campaign(db, actor, campaign_id, lock=True)
    check_etag(request, campaign)
    rows = list_kind(db, "survey_campaign_enrollment", parent_id=campaign.id)
    if campaign.status != "SCHEDULED" or any(row.data.get("run_id") for row in rows):
        raise HTTPException(
            409, "Начавшийся опрос не перезаписывается. Создайте отдельную кампанию"
        )
    preview = _preview(db, actor, body)
    if body.preview_hash != preview["preview_hash"] or not preview["recipient_count"]:
        raise HTTPException(409, "Повторите предварительный просмотр аудитории")
    if _version(db, actor, body.version_id).parent_id != campaign.data["template_id"]:
        raise HTTPException(422, "Перепланирование не меняет опрос")
    generation = campaign.data["generation"] + 1
    for row in rows:
        row.status = "CANCELLED"
    campaign.data = {
        **campaign.data,
        **body.model_dump(mode="json", exclude={"reason", "preview_hash"}),
        "generation": generation,
        "created_by": actor.id,
        "creator_scope": deepcopy(actor.scope),
        "recipient_count": preview["recipient_count"],
    }
    for recipient in preview["recipients"]:
        save_entity(
            db,
            "survey_campaign_enrollment",
            {
                "campaign_id": campaign.id,
                "employee_id": recipient["employee_id"],
                "episode_id": recipient["episode_id"],
                "version_id": body.version_id,
                "generation": generation,
                "run_id": None,
            },
            status="WAITING",
            parent_id=campaign.id,
        )
    audit(
        db,
        actor,
        "survey.campaign.replan",
        campaign.id,
        {"generation": generation},
        reason=body.reason,
    )
    commit(db)
    return _reply(campaign)


@router.post("/survey-campaigns/{campaign_id}/cancel")
def cancel_campaign(
    campaign_id: str,
    body: Reason,
    request: Request,
    db=Depends(get_db),
    actor=Depends(get_actor),
):
    campaign = _campaign(db, actor, campaign_id, lock=True)
    check_etag(request, campaign)
    if campaign.status not in {"SCHEDULED", "ACTIVE"}:
        raise HTTPException(409, "Кампания уже завершена")
    campaign.status = "CANCELLED"
    campaign.data = {**campaign.data, "cancelled_at": now().isoformat()}
    _reconcile(db, campaign)
    audit(db, actor, "survey.campaign.cancel", campaign.id, reason=body.reason)
    commit(db)
    return _reply(campaign)


def _visible_rows(db, actor, campaign):
    return [
        row
        for row in list_kind(db, "survey_campaign_enrollment", parent_id=campaign.id)
        if row.data.get("generation") == campaign.data["generation"]
        and actor_can_employee(db, actor, row.data["employee_id"])
    ]


@router.get("/survey-campaigns")
def campaigns(db=Depends(get_db), actor=Depends(get_actor), template_id: str | None = None):
    require_role(actor, *READ)
    if template_id is not None:
        _template(db, actor, template_id)
    rows = []
    for campaign in list_kind(db, "survey_campaign"):
        if template_id is not None and campaign.data.get("template_id") != template_id:
            continue
        if actor.role in HR and can_delegate_scope(actor.scope, campaign.data["scope"]):
            rows.append(serialize(campaign))
        elif actor.role not in HR and _visible_rows(db, actor, campaign):
            rows.append(
                {
                    "id": campaign.id,
                    "name": campaign.data["name"],
                    "status": campaign.status,
                    "schedule": campaign.data["schedule"],
                    "privacy_notice": PRIVACY,
                }
            )
    return {"items": rows}


@router.get("/survey-campaigns/{campaign_id}")
def campaign_get(campaign_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    campaign = _campaign(db, actor, campaign_id)
    return _reply(campaign)


def _present_answers(run):
    questions = {q["question_key"]: q for q in run.data["definition"]["questions"]}
    result = deepcopy(run.data.get("answers", {}))
    for key, answer in result.items():
        question = questions.get(key, {})
        options = {
            o["value"]: o["label"].get("text_vy", "")
            for o in question.get("options", [])
        }
        value = answer.get("value")
        answer["question_prompt"] = question.get("authored_prompt") or question.get(
            "prompt", {}
        ).get("text_vy", key)
        answer["display_value"] = (
            ", ".join(options.get(v, v) for v in value)
            if isinstance(value, list)
            else options.get(value, value) if isinstance(value, str) else value
        )
    return result


@router.get("/survey-campaigns/{campaign_id}/responses")
def campaign_responses(campaign_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    campaign = _campaign(db, actor, campaign_id)
    rows = []
    for enrollment in _visible_rows(db, actor, campaign):
        employee = get_entity(db, enrollment.data["employee_id"], "employee")
        run = (
            db.get(Entity, enrollment.data["run_id"])
            if enrollment.data.get("run_id")
            else None
        )
        rows.append(
            {
                "employee_id": employee.id,
                "employee_name": employee.data.get("name"),
                "episode_id": enrollment.data["episode_id"],
                "status": run.status if run else enrollment.status,
                "run_id": run.id if run else None,
                "answers": _present_answers(run) if run else {},
            }
        )
    return {"items": rows, "privacy_notice": PRIVACY}


@router.get("/survey-campaigns/{campaign_id}/analytics")
def analytics(campaign_id: str, db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, *READ)
    campaign = get_entity(db, campaign_id, "survey_campaign")
    rows = _visible_rows(db, actor, campaign)
    if not rows:
        raise HTTPException(404, "Кампания недоступна")
    result = {
        "campaign_id": campaign.id,
        "name": campaign.data["name"],
        "minimum_group_size": 5,
        "suppressed": len({r.data["employee_id"] for r in rows}) < 5,
        "individual_scores": False,
        "clinical_interpretation": False,
        "questions": [],
    }
    if result["suppressed"]:
        return {
            **result,
            "eligible": None,
            "completed": None,
            "declined": None,
            "missing": None,
        }
    runs = [db.get(Entity, r.data["run_id"]) for r in rows if r.data.get("run_id")]
    version = get_entity(db, campaign.data["version_id"], "survey_template_version")
    for q in version.data["questions"]:
        answers = [
            r.data.get("answers", {}).get("CUSTOM-" + q["question_key"]) for r in runs
        ]
        valid = [a["value"] for a in answers if a and a.get("outcome") == "ANSWERED"]
        # No text excerpts; categories and numeric means require five distinct
        # respondents for THIS question. Never publish a small complementary cell.
        item = {
            "question_key": q["question_key"],
            "prompt": q["prompt"],
            "type": q["type"],
            "suppressed": len(valid) < 5,
            "answered": len(valid) if len(valid) >= 5 else None,
            "mean": None,
            "distribution": None,
        }
        if len(valid) >= 5 and q["type"] == "LIKERT":
            item["mean"] = round(sum(valid) / len(valid), 3)
        elif len(valid) >= 5 and q["type"] in {"SINGLE_CHOICE", "MULTI_CHOICE"}:
            counts = {
                o["value"]: sum(
                    (o["value"] in v if isinstance(v, list) else o["value"] == v)
                    for v in valid
                )
                for o in q["options"]
            }
            item["distribution"] = (
                [
                    {"label": o["label"], "count": counts[o["value"]]}
                    for o in q["options"]
                ]
                if all(n == 0 or n >= 5 for n in counts.values())
                else None
            )
        result["questions"].append(item)
    # Participation counts can expose a lone refusal; only expose all counters
    # if each non-zero cell is at least five, otherwise leave the whole set null.
    counts = {
        "completed": sum(r.status == "COMPLETED" for r in runs),
        "declined": sum(r.status == "DECLINED" for r in runs),
    }
    counts["missing"] = len(rows) - counts["completed"] - counts["declined"]
    safe = all(n == 0 or n >= 5 for n in counts.values())
    return {
        **result,
        "eligible": len(rows),
        **{k: v if safe else None for k, v in counts.items()},
    }


@router.get("/me/custom-surveys")
def my_surveys(db=Depends(get_db), actor=Depends(get_actor)):
    require_role(actor, "EMPLOYEE")
    employee = get_entity(db, actor.employee_id, "employee")
    if not employee_is_enabled(employee) or employee.data.get("employment_stage") == "ENDED":
        return {"items": []}
    from .domain import survey_view

    return {
        "items": [
            survey_view(r)
            for r in list_kind(db, "survey_run")
            if r.data.get("campaign_id")
            and r.data.get("employee_id") == employee.id
            and r.data.get("episode_id") == employee.data.get("episode_id")
        ]
    }


def delivery_allowed(db, run):
    campaign = db.get(Entity, run.data.get("campaign_id"))
    if (
        not campaign
        or campaign.kind != "survey_campaign"
        or campaign.status not in {"ACTIVE", "SCHEDULED"}
    ):
        return False, "CAMPAIGN_INACTIVE"
    if run.data.get("campaign_generation") != campaign.data[
        "generation"
    ] or now() >= aware(datetime.fromisoformat(campaign.data["schedule"]["ends_at"])):
        return False, "CAMPAIGN_EXPIRED_OR_REPLANNED"
    employee = db.scalar(
        select(Entity).where(Entity.id == run.data["employee_id"])
        .execution_options(populate_existing=True)
    )
    actor = _actor_current(db, campaign.data["created_by"])
    template = db.get(Entity, campaign.data["template_id"])
    if (
        not actor
        or not template
        or not can_delegate_scope(actor.scope, campaign.data["scope"])
        or not employee_is_enabled(employee)
        or employee.data.get("episode_id", employee.id) != run.data.get("episode_id")
    ):
        return False, "CAMPAIGN_ACCESS_REVOKED"
    if (
        not actor_can_employee(db, actor, employee.id)
        or not _scope_matches(template.data["scope"], employee)
        or not _audience_matches(db, campaign.data["audience"], employee)
    ):
        return False, "CAMPAIGN_AUDIENCE_CHANGED"
    return True, "OK"


def _reconcile(db, campaign):
    expired = now() >= aware(
        datetime.fromisoformat(campaign.data["schedule"]["ends_at"])
    )
    cancelled = campaign.status == "CANCELLED"
    for row in list_kind(db, "survey_campaign_enrollment", parent_id=campaign.id):
        if row.data.get("generation") != campaign.data["generation"]:
            continue
        run = db.get(Entity, row.data["run_id"]) if row.data.get("run_id") else None
        allowed, reason = delivery_allowed(db, run) if run else (True, "OK")
        if run and run.status not in TERMINAL and (expired or cancelled or not allowed):
            run.status = "EXPIRED" if expired and not cancelled else "CANCELLED"
            run.data = {
                **run.data,
                "closed_at": now().isoformat(),
                "reason_code": reason,
            }
        if run and run.status in TERMINAL:
            row.status = run.status
            # Closed voluntary questionnaires must not keep a stale capture in
            # the employee's single input lane until its independent TTL.
            for capture in list_kind(
                db, "input_capture", parent_id=run.data["employee_id"]
            ):
                if capture.data.get("survey_run_id") == run.id and capture.status in {
                    "OPEN",
                    "DRAFT",
                    "READY_TO_CONFIRM",
                }:
                    capture.status = "CANCELLED"
        if not run and (expired or cancelled):
            row.status = "CANCELLED" if cancelled else "SKIPPED"
    if expired and not cancelled:
        campaign.status = "COMPLETED"


def tick(db):
    count = 0
    for campaign in db.scalars(
        select(Entity)
        .where(
            Entity.kind == "survey_campaign", Entity.status.in_(["ACTIVE", "SCHEDULED"])
        )
        .order_by(Entity.id)
        .with_for_update(skip_locked=True)
    ):
        _reconcile(db, campaign)
        if (
            campaign.status not in {"ACTIVE", "SCHEDULED"}
            or aware(datetime.fromisoformat(campaign.data["schedule"]["starts_at"]))
            > now()
        ):
            continue
        campaign.status = "ACTIVE"
        for row in list_kind(db, "survey_campaign_enrollment", parent_id=campaign.id):
            if (
                row.status == "WAITING"
                and row.data["generation"] == campaign.data["generation"]
            ):
                enqueue(
                    db,
                    DELIVER,
                    {"enrollment_id": row.id, "generation": row.data["generation"]},
                    f"custom-survey:{campaign.id}:{row.data['generation']}:{row.id}",
                )
                count += 1
    return count


def on_opening_accepted(db, run, accepted):
    if run.data.get("first_question_accepted_at"):
        return
    campaign = get_entity(db, run.data["campaign_id"], "survey_campaign")
    schedule = campaign.data["schedule"]
    expires = min(
        accepted + timedelta(hours=schedule["response_hours"]),
        aware(datetime.fromisoformat(schedule["ends_at"])),
    )
    run.data = {
        **run.data,
        "first_question_accepted_at": accepted.isoformat(),
        "expires_at": expires.isoformat(),
    }
    if run.status in TERMINAL or not delivery_allowed(db, run)[0]:
        return
    for number, hours in enumerate(schedule["reminder_hours"], 1):
        due = accepted + timedelta(hours=hours)
        if due < expires:
            enqueue(
                db,
                REMIND,
                {"survey_run_id": run.id, "number": number},
                f"custom-reminder:{run.id}:{number}",
                due,
            )


def process_job(db, job):
    if job.kind not in {DELIVER, REMIND}:
        return False
    # Fence before business changes, including stale executors whose old lease
    # expired without a replacement claim. The outer worker final check remains.
    expected_token = job.payload.get("_lease_token")
    fresh = db.scalar(
        select(Job)
        .where(Job.id == job.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        not fresh
        or fresh.status != "PROCESSING"
        or fresh.payload.get("_lease_token") != expected_token
        or not fresh.lease_until
        or aware(fresh.lease_until) <= now()
    ):
        return True
    from . import domain as d

    if job.kind == REMIND:
        run = get_entity(db, job.payload["survey_run_id"], "survey_run", lock=True)
        if run.status != "IN_PROGRESS" or not delivery_allowed(db, run)[0]:
            return True
        if (
            run.data.get("expires_at")
            and aware(datetime.fromisoformat(run.data["expires_at"])) <= now()
        ):
            run.status = "EXPIRED"
            return True
        conv = d.conversation_for(db, run.data["employee_id"])
        if conv and conv.data.get("mode") in {"HUMAN", "WAITING_HR"}:
            return True
        d.emit(
            db,
            run.data["employee_id"],
            "Добровольный опрос ещё открыт. Можно продолжить, пропустить вопросы или отказаться.",
            [
                {
                    "text": "Продолжить опрос",
                    "payload": {"action": "SURVEY_RESUME", "survey_run_id": run.id},
                },
                {
                    "text": "Отказаться",
                    "payload": {"action": "SURVEY_DECLINE", "survey_run_id": run.id},
                },
            ],
            f"custom-reminder-send:{run.id}:{job.payload['number']}",
            {"survey_run_id": run.id},
        )
        return True
    # Lock order is Job -> campaign -> employee -> enrollment. Cancel/replan
    # own the campaign first; no executor holds a recipient while waiting for it.
    row = get_entity(db, job.payload["enrollment_id"], "survey_campaign_enrollment")
    campaign = get_entity(db, row.parent_id, "survey_campaign", lock=True)
    employee = get_entity(db, row.data["employee_id"], "employee", lock=True)
    row = get_entity(db, row.id, "survey_campaign_enrollment", lock=True)
    if (
        row.status != "WAITING"
        or job.payload["generation"] != campaign.data["generation"]
    ):
        return True
    probe = Entity(
        kind="survey_run",
        data={**row.data, "campaign_generation": row.data["generation"]},
    )
    if not delivery_allowed(db, probe)[0]:
        row.status = "SKIPPED"
        row.data = {**row.data, "reason_code": delivery_allowed(db, probe)[1]}
        return True
    if aware(datetime.fromisoformat(campaign.data["schedule"]["starts_at"])) > now():
        return True
    conv = d.conversation_for(db, employee.id)
    busy = conv and conv.data.get("mode") in {"HUMAN", "WAITING_HR"}
    busy = busy or any(
        r.data.get("employee_id") == employee.id
        and r.data.get("episode_id") == row.data["episode_id"]
        and r.status not in TERMINAL
        for r in list_kind(db, "survey_run")
    )
    if busy:
        # Each deferred minute has one durable successor key; current job can
        # complete without either losing the campaign or flooding retries.
        due = now() + timedelta(minutes=5)
        enqueue(
            db,
            DELIVER,
            {"enrollment_id": row.id, "generation": row.data["generation"]},
            f"custom-defer:{row.id}:{int(due.timestamp()//300)}",
            due,
        )
        return True
    version = get_entity(db, row.data["version_id"], "survey_template_version")
    if version.status != "PUBLISHED":
        row.status = "SKIPPED"
        return True
    step = {"step_key": "custom-" + campaign.id, "survey": _definition(version)}
    run = d.open_survey(db, row, step, employee)
    if run:
        run.data = {
            **run.data,
            "campaign_id": campaign.id,
            "campaign_generation": campaign.data["generation"],
            "privacy_notice": PRIVACY,
            "exclude_from_general_feedback_analysis": True,
            "expires_at": campaign.data["schedule"]["ends_at"],
        }
        row.status = "OPENED"
        row.data = {**row.data, "run_id": run.id, "opened_at": now().isoformat()}
    return True
