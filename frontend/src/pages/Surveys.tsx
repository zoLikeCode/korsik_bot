import { useEffect, useState } from "react";
import {
  publishSurvey,
  saveSurveyDraft,
  surveyPublicationLabel,
} from "../surveyLifecycle";
import "./Surveys.css";
import { ArrowUp, ArrowDown } from "lucide-react";
import {
  moveSurveyQuestion,
  surveyActionReason,
  surveyDraftBody,
} from "../surveyEditing";
import { useActor } from "../App";
import { ApiError, api, errorText, items } from "../api";
import { stageTimeCandidates, stageTimeLabel } from "../employmentTime";
import {
  Empty,
  ErrorBox,
  Field,
  Loading,
  Notice,
  PageTitle,
  Status,
  Success,
  useResource,
} from "../ui";

type Question = {
  question_key: string;
  prompt: string;
  type: string;
  options: { value: string; label: string }[];
  minimum: number;
  maximum: number;
  max_selected: number | null;
  max_text_length: number;
};
const newQuestion = (): Question => ({
  question_key: `q_${crypto.randomUUID().slice(0, 8)}`,
  prompt: "",
  type: "LIKERT",
  options: [],
  minimum: 1,
  maximum: 5,
  max_selected: null,
  max_text_length: 2000,
});
const privacy =
  "Участие добровольное. Опрос не анонимный: ответы связаны с карточкой сотрудника и доступны HR в пределах полномочий. Руководители видят только сводки групп от 5 участников. Диагнозы и психологические профили не рассчитываются.";
const versionStatuses: Record<string, string> = {
  DRAFT: "Черновик",
  REVIEW_REQUIRED: "На проверке",
  APPROVED: "Согласована",
  PUBLISHED: "Опубликована",
  ARCHIVED: "Архив",
};
const types: Record<string, string> = {
  LIKERT: "Шкала",
  SINGLE_CHOICE: "Один вариант",
  MULTI_CHOICE: "Несколько вариантов",
  TEXT: "Свободный ответ",
};

export default function Surveys() {
  const actor = useActor(),
    hr = ["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"].includes(actor.role);
  const list = useResource(hr ? "/survey-templates" : null),
    [selected, setSelected] = useState("");
  function refresh() {
    list.reload();
  }
  if (actor.role === "EMPLOYEE")
    return (
      <>
        <PageTitle title="Опросы" />
        <div className="survey-page">
          <EmployeeSurveys />
        </div>
      </>
    );
  return (
    <>
      <PageTitle
        title="Опросы"
        action={
          hr ? (
            <button
              className="button primary"
              onClick={() => setSelected("new")}
            >
              Создать опрос
            </button>
          ) : undefined
        }
      />
      <section className="panel survey-page">
        <ErrorBox error={list.error} />
        {hr && (
          <>
            <div className="card-grid">
              {items(list.data).map((t) => (
                <button
                  key={t.id}
                  className="card"
                  onClick={() => setSelected(t.id)}
                >
                  <strong>{t.title}</strong>
                  <span
                    className={`survey-publication ${t.published_version_id ? "published" : "unpublished"}`}
                  >
                    {surveyPublicationLabel(t)}
                  </span>
                </button>
              ))}
            </div>
            {selected && (
              <Editor
                key={selected}
                id={selected}
                onDone={refresh}
                onClose={() => setSelected("")}
              />
            )}
          </>
        )}
        {!hr && <CampaignList revision={0} hr={false} />}
      </section>
    </>
  );
}
export function SurveyQuestionOrder({
  index,
  count,
  disabled = false,
  onMove,
}: {
  index: number;
  count: number;
  disabled?: boolean;
  onMove: (direction: -1 | 1) => void;
}) {
  return (
    <>
      <button
        type="button"
        className="button secondary"
        disabled={disabled || index === 0}
        aria-label={`Поднять вопрос ${index + 1}`}
        title="Переместить вопрос выше"
        onClick={() => onMove(-1)}
      >
        <ArrowUp size={18} />
      </button>
      <button
        type="button"
        className="button secondary"
        disabled={disabled || index === count - 1}
        aria-label={`Опустить вопрос ${index + 1}`}
        title="Переместить вопрос ниже"
        onClick={() => onMove(1)}
      >
        <ArrowDown size={18} />
      </button>
    </>
  );
}
export function Editor({
  id,
  onDone,
  onClose,
}: {
  id: string;
  onDone: () => void;
  onClose: () => void;
}) {
  const [templateId, setTemplateId] = useState<string | null>(
    id === "new" ? null : id,
  );
  const resource = useResource(
    templateId ? `/survey-templates/${templateId}` : null,
  );
  const [campaignRevision, setCampaignRevision] = useState(0);
  const [version, setVersion] = useState<any>(null),
    [title, setTitle] = useState(""),
    [description, setDescription] = useState(""),
    [purpose, setPurpose] = useState("WELLBEING"),
    [thanks, setThanks] = useState("Спасибо! Ваши ответы сохранены."),
    [questions, setQuestions] = useState<Question[]>([newQuestion()]),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [busy, setBusy] = useState(false),
    [preview, setPreview] = useState<any>(null),
    [conflict, setConflict] = useState(false);
  function load(v: any) {
    setVersion(v);
    setTitle(v.title);
    setDescription(v.description);
    setPurpose(v.purpose);
    setThanks(v.thank_you);
    setQuestions(v.questions);
    setPreview(null);
    setConflict(false);
  }
  useEffect(() => {
    const vs = resource.data?.versions;
    if (vs?.length)
      load(
        vs.find((v: any) => v.id === version?.id) ||
          vs.find((v: any) => v.status === "DRAFT") ||
          vs[vs.length - 1],
      );
  }, [resource.data]);
  const editable = !templateId || version?.status === "DRAFT";
  const publishable =
    !templateId ||
    ["DRAFT", "REVIEW_REQUIRED", "APPROVED"].includes(version?.status);
  const dirty =
    !!version &&
    (title !== version.title ||
      description !== version.description ||
      purpose !== version.purpose ||
      thanks !== version.thank_you ||
      JSON.stringify(questions) !== JSON.stringify(version.questions));
  async function act(fn: () => Promise<any>, message: string) {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await fn();
      setSuccess(message);
      onDone();
    } catch (e) {
      setError(errorText(e));
      if (e instanceof ApiError && e.status === 412) setConflict(true);
    } finally {
      setBusy(false);
    }
  }
  function patchQ(i: number, p: Partial<Question>) {
    setQuestions((q) => q.map((v, n) => (n === i ? { ...v, ...p } : v)));
    setPreview(null);
  }
  const body = surveyDraftBody(
    { title, purpose, questions },
    { description, thank_you: thanks },
  );
  if (templateId && resource.loading && !resource.data && !version)
    return <Loading />;
  return (
    <div className="card survey-editor">
      <div className="panel-head">
        <h3>{!templateId ? "Новый опрос" : title}</h3>
        <button className="button secondary" onClick={onClose}>
          Закрыть
        </button>
      </div>
      <ErrorBox error={error || resource.error} />
      {resource.error && (
        <button
          className="button secondary"
          disabled={busy}
          onClick={resource.reload}
        >
          Повторить загрузку
        </button>
      )}
      {conflict && (
        <button
          className="button secondary"
          disabled={busy}
          onClick={() => {
            setError("");
            resource.reload();
          }}
        >
          Загрузить актуальную версию
        </button>
      )}
      {success && <Success>{success}</Success>}
      {version && (
        <>
          <Status value={version.status} />
          <div className="survey-version-toolbar">
            <Field label="Версия">
              <select
                disabled={busy || !resource.data}
                value={version.id}
                onChange={(e) =>
                  load(
                    resource.data.versions.find(
                      (v: any) => v.id === e.target.value,
                    ),
                  )
                }
              >
                {resource.data?.versions.map((v: any, i: number) => (
                  <option key={v.id} value={v.id}>
                    Версия {i + 1} · {versionStatuses[v.status] || v.status}
                  </option>
                ))}
              </select>
            </Field>
            <button
              className="button secondary"
              disabled={
                busy ||
                !resource.data ||
                resource.data.versions.some((v: any) => v.status === "DRAFT")
              }
              onClick={() =>
                act(async () => {
                  const result = await api.post(
                    `/survey-templates/${templateId}/versions`,
                    {
                      reason: surveyActionReason("version"),
                      base_version_id: version.id,
                    },
                    resource.data.etag,
                  );
                  load(result.data);
                  resource.reload();
                }, "Черновик создан из выбранной версии")
              }
            >
              Создать черновик из версии
            </button>
            <button
              className="button secondary"
              disabled={busy || dirty}
              onClick={() =>
                act(
                  async () =>
                    setPreview(
                      (
                        await api.post(
                          `/survey-template-versions/${version.id}/preview`,
                        )
                      ).data,
                    ),
                  "Предпросмотр сохранённой версии",
                )
              }
            >
              Как увидит сотрудник
            </button>
          </div>
        </>
      )}
      {!editable && (
        <Notice>
          Изменения вносятся в новом черновике. Эта версия защищена от
          редактирования.
        </Notice>
      )}
      <Field label="Название">
        <input
          maxLength={160}
          disabled={!editable || busy}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
      </Field>
      <Field label="Цель опроса">
        <select
          disabled={!editable || busy}
          value={purpose}
          onChange={(e) => setPurpose(e.target.value)}
        >
          <option value="WELLBEING">Самочувствие и поддержка</option>
          <option value="ADAPTATION">Адаптация</option>
          <option value="CUSTOM">Другой рабочий опрос</option>
        </select>
      </Field>
      {questions.map((q, i) => (
        <div className="card" key={q.question_key}>
          <div className="panel-head">
            <h4>Вопрос {i + 1}</h4>
            {editable && (
              <div className="toolbar">
                <SurveyQuestionOrder
                  index={i}
                  count={questions.length}
                  disabled={busy}
                  onMove={(direction) => {
                    setQuestions((v) => moveSurveyQuestion(v, i, direction));
                    setPreview(null);
                  }}
                />
                <button
                  className="button secondary"
                  disabled={busy || questions.length === 1}
                  onClick={() =>
                    setQuestions((v) => v.filter((_, n) => n !== i))
                  }
                >
                  Удалить
                </button>
              </div>
            )}
          </div>
          <Field label="Текст вопроса">
            <textarea
              disabled={!editable || busy}
              maxLength={1500}
              value={q.prompt}
              onChange={(e) => patchQ(i, { prompt: e.target.value })}
            />
          </Field>
          <Field label="Как отвечать">
            <select
              disabled={!editable || busy}
              value={q.type}
              onChange={(e) =>
                patchQ(i, {
                  type: e.target.value,
                  options: ["SINGLE_CHOICE", "MULTI_CHOICE"].includes(
                    e.target.value,
                  )
                    ? [
                        { value: "v1", label: "Первый вариант" },
                        { value: "v2", label: "Второй вариант" },
                      ]
                    : [],
                  max_selected: null,
                })
              }
            >
              {Object.entries(types).map(([v, label]) => (
                <option key={v} value={v}>
                  {label}
                </option>
              ))}
            </select>
          </Field>
          {q.type === "LIKERT" && (
            <div className="form-grid">
              <Field label="От">
                <input
                  type="number"
                  min={1}
                  max={9}
                  disabled={!editable || busy}
                  value={q.minimum}
                  onChange={(e) =>
                    patchQ(i, { minimum: Number(e.target.value) })
                  }
                />
              </Field>
              <Field label="До">
                <input
                  type="number"
                  min={2}
                  max={10}
                  disabled={!editable || busy}
                  value={q.maximum}
                  onChange={(e) =>
                    patchQ(i, { maximum: Number(e.target.value) })
                  }
                />
              </Field>
            </div>
          )}
          {["SINGLE_CHOICE", "MULTI_CHOICE"].includes(q.type) && (
            <Field label="Варианты — каждый с новой строки (от 2 до 10)">
              <textarea
                disabled={!editable || busy}
                value={q.options.map((o) => o.label).join("\n")}
                onChange={(e) =>
                  patchQ(i, {
                    options: e.target.value
                      .split("\n")
                      .map((label, n) => ({ value: `v${n + 1}`, label })),
                    max_selected: null,
                  })
                }
              />
            </Field>
          )}
          {q.type === "MULTI_CHOICE" && (
            <Field label="Максимум выбранных вариантов">
              <input
                type="number"
                min={1}
                max={q.options.length}
                disabled={!editable || busy}
                value={q.max_selected || q.options.length}
                onChange={(e) =>
                  patchQ(i, { max_selected: Number(e.target.value) })
                }
              />
            </Field>
          )}
          <p>Любой вопрос можно пропустить.</p>
        </div>
      ))}
      {editable && (
        <button
          className="button secondary"
          disabled={busy || questions.length >= 25}
          onClick={() => setQuestions((q) => [...q, newQuestion()])}
        >
          Добавить вопрос
        </button>
      )}
      {dirty && (
        <Notice>
          Есть несохранённые изменения. Публикация сохранит и опубликует текущие
          вопросы.
        </Notice>
      )}
      <div className="toolbar survey-editor-actions">
        <button
          className="button secondary"
          disabled={busy || !editable}
          onClick={() =>
            act(async () => {
              const saved = await saveSurveyDraft({
                templateId,
                version,
                body,
                onCreatedTemplate: (createdId) => {
                  setTemplateId(createdId);
                  onDone();
                },
              });
              load(saved.version);
              resource.reload();
            }, "Опрос сохранён")
          }
        >
          Сохранить
        </button>
        <button
          className="button primary"
          disabled={busy || !publishable}
          onClick={() =>
            act(async () => {
              const published = await publishSurvey({
                templateId,
                version,
                body,
                dirty,
                reason: surveyActionReason("publish"),
                onCreatedTemplate: (createdId) => {
                  setTemplateId(createdId);
                  onDone();
                },
                onSavedVersion: load,
              });
              load(published.version);
              resource.reload();
            }, "Опрос опубликован и доступен в сценариях")
          }
        >
          Опубликовать
        </button>
      </div>
      {preview && (
        <div className="card">
          <h4>Предпросмотр</h4>
          <Notice>{preview.privacy_notice}</Notice>
          {preview.definition.questions.map((q: any, i: number) => (
            <div key={q.question_key}>
              <strong>
                {i + 1}. {q.prompt.text_vy}
              </strong>
              {q.options?.map((o: any) => (
                <p key={o.value}>○ {o.label.text_vy}</p>
              ))}
              {q.type === "NUMERIC" && (
                <p>
                  Шкала от {q.minimum} до {q.maximum}
                </p>
              )}
              <p>Пропустить вопрос · Отказаться от опроса</p>
            </div>
          ))}
        </div>
      )}
      {version?.status === "PUBLISHED" && (
        <CampaignForm
          versionId={version.id}
          onDone={() => {
            setCampaignRevision((v) => v + 1);
            onDone();
          }}
        />
      )}
      {templateId && (
        <CampaignList hr revision={campaignRevision} templateId={templateId} />
      )}
    </div>
  );
}
function CampaignForm({
  versionId,
  onDone,
  campaign,
}: {
  versionId: string;
  onDone: () => void;
  campaign?: any;
}) {
  const employees = useResource("/employees"),
    sites = useResource("/sites");
  const [name, setName] = useState(campaign?.name || ""),
    [zone, setZone] = useState(campaign?.schedule.timezone || "Europe/Moscow"),
    [start, setStart] = useState(""),
    [end, setEnd] = useState(""),
    [startChoice, setStartChoice] = useState(""),
    [endChoice, setEndChoice] = useState("");
  const [ids, setIds] = useState<string[]>(
      campaign?.audience.employee_ids || [],
    ),
    [siteIds, setSiteIds] = useState<string[]>(
      campaign?.audience.site_ids || [],
    ),
    [stages, setStages] = useState<string[]>(
      campaign?.audience.employment_stages || ["PREBOARDING", "ACTIVE"],
    ),
    [kinds, setKinds] = useState<string[]>(
      campaign?.audience.employment_types || [],
    ),
    [roles, setRoles] = useState<string[]>(
      campaign?.audience.user_roles || ["EMPLOYEE"],
    );
  const [hours, setHours] = useState(campaign?.schedule.response_hours || 168),
    [reminders, setReminders] = useState<string>(
      (campaign?.schedule.reminder_hours || [24, 48]).join(", "),
    ),
    [reason, setReason] = useState(""),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [busy, setBusy] = useState(false),
    [preview, setPreview] = useState<any>(null);
  const starts = stageTimeCandidates(start, zone),
    ends = stageTimeCandidates(end, zone),
    startsAt =
      starts.length === 1
        ? starts[0]
        : starts.includes(startChoice)
          ? startChoice
          : "",
    endsAt =
      ends.length === 1 ? ends[0] : ends.includes(endChoice) ? endChoice : "";
  const body = {
    name,
    version_id: versionId,
    audience: {
      employee_ids: ids,
      site_ids: siteIds,
      employment_stages: stages,
      employment_types: kinds,
      user_roles: roles,
    },
    schedule: {
      timezone: zone,
      starts_at: startsAt,
      ends_at: endsAt,
      response_hours: hours,
      reminder_hours: reminders.trim()
        ? reminders.split(",").map((v) => Number(v.trim()))
        : [],
    },
    reason,
  };
  const key = JSON.stringify(body),
    current = preview?.key === key;
  function toggle(
    values: string[],
    value: string,
    setter: (v: string[]) => void,
  ) {
    setter(
      values.includes(value)
        ? values.filter((v) => v !== value)
        : [...values, value],
    );
  }
  async function act(save: boolean) {
    setBusy(true);
    setError("");
    try {
      if (!startsAt || !endsAt)
        throw new Error(
          "Проверьте даты и часовой пояс; для повторяющегося времени выберите смещение.",
        );
      if (save) {
        if (!current)
          throw new Error("Повторите предпросмотр после изменения настроек");
        const payload = { ...body, preview_hash: preview.result.preview_hash };
        if (campaign)
          await api.patch(
            `/survey-campaigns/${campaign.id}`,
            payload,
            campaign.etag,
          );
        else await api.post("/survey-campaigns", payload);
        setSuccess(
          "Расписание сохранено. Получатели и права будут проверены перед отправкой.",
        );
        setPreview(null);
        onDone();
      } else
        setPreview({
          key,
          result: (await api.post("/survey-campaigns/preview", body)).data,
        });
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="card">
      <h3>{campaign ? "Изменить будущую рассылку" : "Запланировать опрос"}</h3>
      <ErrorBox error={error || employees.error || sites.error} />
      {success && <Success>{success}</Success>}
      <Field label="Название рассылки">
        <input
          maxLength={160}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </Field>
      <Field label="Часовой пояс расписания">
        <input
          value={zone}
          onChange={(e) => setZone(e.target.value)}
          placeholder="Europe/Moscow"
        />
      </Field>
      {[
        {
          label: "Начало",
          value: start,
          set: setStart,
          candidates: starts,
          choice: startChoice,
          choose: setStartChoice,
        },
        {
          label: "Окончание",
          value: end,
          set: setEnd,
          candidates: ends,
          choice: endChoice,
          choose: setEndChoice,
        },
      ].map((r) => (
        <div key={r.label}>
          <Field label={r.label + " в выбранном часовом поясе"}>
            <input
              type="datetime-local"
              value={r.value}
              onChange={(e) => r.set(e.target.value)}
            />
          </Field>
          {r.candidates.length > 1 && (
            <Field label="Время повторяется: выберите смещение">
              <select
                value={r.choice}
                onChange={(e) => r.choose(e.target.value)}
              >
                <option value="">Выберите время</option>
                {r.candidates.map((v) => (
                  <option key={v} value={v}>
                    {stageTimeLabel(v, zone)} · {v}
                  </option>
                ))}
              </select>
            </Field>
          )}
          {r.value && !r.candidates.length && (
            <ErrorBox error="Такого времени нет в выбранном часовом поясе" />
          )}
        </div>
      ))}
      {campaign && (
        <p>
          Прежний период: {stageTimeLabel(campaign.schedule.starts_at, zone)} —{" "}
          {stageTimeLabel(campaign.schedule.ends_at, zone)}. Для
          перепланирования введите новые даты.
        </p>
      )}
      <Field label="Срок ответа после подтверждённой доставки, часов">
        <input
          type="number"
          min={1}
          max={720}
          value={hours}
          onChange={(e) => setHours(Number(e.target.value))}
        />
      </Field>
      <Field label="Напомнить через часы (до трёх, через запятую)">
        <input
          value={reminders}
          onChange={(e) => setReminders(e.target.value)}
          placeholder="24, 48"
        />
      </Field>
      <p>
        Пустое поле отключает напоминания. После окончания периода они не
        отправляются.
      </p>
      {[
        {
          label: "Этапы",
          values: stages,
          set: setStages,
          choices: [
            ["PREBOARDING", "До выхода"],
            ["ACTIVE", "После выхода"],
          ],
        },
        {
          label: "Категории — пусто означает все",
          values: kinds,
          set: setKinds,
          choices: [
            ["OFFICE", "Офис"],
            ["PRODUCTION", "Производство"],
          ],
        },
        {
          label: "Роли с карточкой сотрудника",
          values: roles,
          set: setRoles,
          choices: [
            ["EMPLOYEE", "Сотрудники"],
            ["MANAGER", "Руководители"],
            ["HR_SPECIALIST", "HR-менеджеры"],
            ["HR_ADMIN", "HR-администраторы"],
            ["HEAD_TALENT", "Руководитель по подбору персонала"],
          ],
        },
      ].map((r) => (
        <fieldset key={r.label}>
          <legend>{r.label}</legend>
          {r.choices.map(([v, l]) => (
            <label key={v}>
              <input
                type="checkbox"
                checked={r.values.includes(v)}
                onChange={() => toggle(r.values, v, r.set)}
              />
              {l}{" "}
            </label>
          ))}
        </fieldset>
      ))}
      <fieldset>
        <legend>Площадки — пусто означает все доступные</legend>
        {items(sites.data).map((s) => (
          <label key={s.id}>
            <input
              type="checkbox"
              checked={siteIds.includes(s.id)}
              onChange={() => toggle(siteIds, s.id, setSiteIds)}
            />
            {s.name}{" "}
          </label>
        ))}
      </fieldset>
      <details>
        <summary>
          Конкретные сотрудники — иначе вся подходящая аудитория
        </summary>
        {items(employees.data)
          .filter((e) => e.employment_stage !== "ENDED")
          .map((e) => (
            <label key={e.id} style={{ display: "block" }}>
              <input
                type="checkbox"
                checked={ids.includes(e.id)}
                onChange={() => toggle(ids, e.id, setIds)}
              />
              {e.name}
            </label>
          ))}
      </details>
      <Field label="Основание рассылки">
        <textarea
          maxLength={2000}
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
      </Field>
      <div className="toolbar">
        <button
          className="button secondary"
          disabled={busy || reason.trim().length < 3}
          onClick={() => act(false)}
        >
          Проверить аудиторию и расписание
        </button>
        <button
          className="button secondary"
          disabled={busy || !current}
          onClick={() => act(true)}
        >
          {campaign ? "Сохранить расписание" : "Запланировать"}
        </button>
      </div>
      {current && (
        <div className="card">
          <strong>Получателей: {preview.result.recipient_count}</strong>
          <p>
            {stageTimeLabel(startsAt, zone)} — {stageTimeLabel(endsAt, zone)}
          </p>
          <Notice>
            Получатели фиксируются при сохранении. Перед отправкой проверяются
            их текущие права, площадка и период найма. Начатые и отвеченные
            опросы не перезаписываются.
          </Notice>
          <ul>
            {preview.result.recipients.map((p: any) => (
              <li key={p.employee_id}>{p.name}</li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
export function CampaignList({
  hr,
  revision,
  templateId,
}: {
  hr: boolean;
  revision: number;
  templateId?: string;
}) {
  const resource = useResource(
      templateId
        ? `/survey-campaigns?template_id=${encodeURIComponent(templateId)}`
        : "/survey-campaigns",
    ),
    [selected, setSelected] = useState<any>(null),
    [reason, setReason] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const report = useResource(
      selected ? `/survey-campaigns/${selected.id}/analytics` : null,
    ),
    responses = useResource(
      hr && selected ? `/survey-campaigns/${selected.id}/responses` : null,
    );
  useEffect(() => {
    if (resource.data && selected) {
      const current = items(resource.data).find(
        (row) => row.id === selected.id,
      );
      setSelected(current || null);
    }
  }, [resource.data]);
  useEffect(() => {
    if (revision > 0) {
      resource.reload();
      report.reload();
      if (hr) responses.reload();
    }
  }, [revision]);
  useEffect(() => {
    if (selected) {
      const t = setInterval(() => {
        report.reload();
        if (hr) responses.reload();
      }, 10000);
      return () => clearInterval(t);
    }
  }, [selected?.id, hr]);
  async function cancel() {
    setBusy(true);
    setError("");
    try {
      const c = (await api.get(`/survey-campaigns/${selected.id}`)).data;
      setSelected(
        (
          await api.post(
            `/survey-campaigns/${selected.id}/cancel`,
            { reason },
            c.etag,
          )
        ).data,
      );
      resource.reload();
      responses.reload();
      report.reload();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  if (
    !resource.loading &&
    !resource.error &&
    !error &&
    items(resource.data).length === 0
  )
    return null;
  return (
    <section className="survey-results">
      <ErrorBox error={resource.error || error} />
      {resource.loading ? (
        <Loading />
      ) : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Название</th>
                <th>Состояние</th>
                <th>Начало</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {items(resource.data).map((c) => (
                <tr key={c.id}>
                  <td>{c.name}</td>
                  <td>
                    <Status value={c.status} />
                  </td>
                  <td>
                    {stageTimeLabel(c.schedule.starts_at, c.schedule.timezone)}
                  </td>
                  <td>
                    <button
                      className="button secondary"
                      onClick={() => setSelected(c)}
                    >
                      Открыть
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {selected && (
        <div className="card">
          <div className="panel-head">
            <h3>{selected.name}</h3>
            <button
              className="button secondary"
              onClick={() => setSelected(null)}
            >
              Закрыть
            </button>
          </div>
          <ErrorBox error={report.error || (hr ? responses.error : "")} />
          {report.data?.suppressed ? (
            <Notice>
              В доступной группе меньше 5 участников. Сводка скрыта; отдельные
              показатели также скрываются, если ответили менее 5 человек.
            </Notice>
          ) : (
            report.data && (
              <>
                <p>
                  Приглашено: {report.data.eligible}. Завершили:{" "}
                  {report.data.completed ?? "скрыто"}. Отказались:{" "}
                  {report.data.declined ?? "скрыто"}. Не завершили:{" "}
                  {report.data.missing ?? "скрыто"}.
                </p>
                {report.data.questions.map((q: any) => (
                  <div className="card" key={q.question_key}>
                    <strong>{q.prompt}</strong>
                    <p>
                      {q.suppressed
                        ? "Меньше 5 ответов — показатель скрыт"
                        : `Ответили: ${q.answered}`}
                    </p>
                    {q.mean !== null && <p>Среднее по вопросу: {q.mean}</p>}
                    {q.distribution && (
                      <ul>
                        {q.distribution.map((o: any) => (
                          <li key={o.label}>
                            {o.label}: {o.count}
                          </li>
                        ))}
                      </ul>
                    )}
                    {q.type === "TEXT" && (
                      <p>
                        Личные тексты доступны только HR в пределах полномочий.
                      </p>
                    )}
                  </div>
                ))}
              </>
            )
          )}
          {hr && (
            <>
              <details>
                <summary>Личные ответы — только HR</summary>
                <Notice>{privacy}</Notice>
                {items(responses.data).map((r) => (
                  <div className="card" key={r.employee_id}>
                    <strong>{r.employee_name}</strong>{" "}
                    <Status value={r.status} />
                    {Object.values(r.answers || {}).map((a: any) => (
                      <p key={a.question_key}>
                        {a.question_prompt || a.question_key}:{" "}
                        {a.outcome === "DECLINED"
                          ? "Вопрос пропущен"
                          : String(a.display_value ?? a.value)}
                      </p>
                    ))}
                  </div>
                ))}
              </details>
              {["ACTIVE", "SCHEDULED"].includes(selected.status) && (
                <>
                  <Field label="Основание отмены">
                    <textarea
                      value={reason}
                      maxLength={2000}
                      onChange={(e) => setReason(e.target.value)}
                    />
                  </Field>
                  <button
                    className="button secondary"
                    disabled={busy || reason.trim().length < 3}
                    onClick={cancel}
                  >
                    Отменить оставшиеся отправки
                  </button>
                </>
              )}
              {selected.status === "SCHEDULED" && (
                <details>
                  <summary>Изменить будущее расписание</summary>
                  <CampaignForm
                    key={selected.id}
                    versionId={selected.version_id}
                    campaign={selected}
                    onDone={() => {
                      resource.reload();
                      setSelected(null);
                    }}
                  />
                </details>
              )}
            </>
          )}
        </div>
      )}
    </section>
  );
}
function EmployeeSurveys() {
  const resource = useResource("/me/custom-surveys"),
    [selected, setSelected] = useState("");
  useEffect(() => {
    const t = setInterval(resource.reload, 10000);
    return () => clearInterval(t);
  }, []);
  return (
    <section className="panel">
      <div className="panel-head">
        <button className="button secondary" onClick={resource.reload}>
          Обновить
        </button>
      </div>
      <Notice>{privacy}</Notice>
      <ErrorBox error={resource.error} />
      {resource.loading ? (
        <Loading />
      ) : items(resource.data).length === 0 ? (
        <Empty title="Назначенных опросов пока нет" />
      ) : (
        items(resource.data).map((r) => (
          <div className="card" key={r.id}>
            <strong>
              {r.definition.questions[0].prompt.text_vy.split("\n")[0]}
            </strong>{" "}
            <Status value={r.status} />
            <p>
              Отвечено {r.progress.answered} из {r.progress.total}
            </p>
            <button
              className="button secondary"
              onClick={() => setSelected(r.id)}
            >
              Открыть
            </button>
          </div>
        ))
      )}
      {selected && (
        <EmployeeRun key={selected} id={selected} onDone={resource.reload} />
      )}
    </section>
  );
}
function EmployeeRun({ id, onDone }: { id: string; onDone: () => void }) {
  const resource = useResource(`/survey-runs/${id}`),
    r = resource.data,
    q = r?.current_question;
  const [text, setText] = useState(""),
    [values, setValues] = useState<string[]>([]),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    setText("");
    setValues([]);
  }, [q?.question_key]);
  async function send(value: any, skip = false) {
    setBusy(true);
    setError("");
    try {
      await api.post(`/survey-runs/${id}/answers`, {
        question_id: q.question_key,
        state_revision: r.state_seq,
        ...(skip ? { outcome: "DECLINED" } : { value }),
      });
      resource.reload();
      onDone();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function command(action: string) {
    setBusy(true);
    setError("");
    try {
      await api.post(`/survey-runs/${id}/commands`, { action });
      resource.reload();
      onDone();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  if (resource.loading) return <Loading />;
  return (
    <div className="card">
      <ErrorBox error={resource.error || error} />
      {r && (
        <>
          <Status value={r.status} />
          {r.status === "IN_PROGRESS" && q && (
            <>
              <h3>{q.prompt.text_vy || q.prompt.text_ty}</h3>
              <div className="toolbar">
                {q.type === "SINGLE_CHOICE" &&
                  q.options.map((o: any) => (
                    <button
                      className="button secondary"
                      key={o.value}
                      disabled={busy}
                      onClick={() => send(o.value)}
                    >
                      {o.label.text_vy || o.label.text_ty}
                    </button>
                  ))}
                {q.type === "NUMERIC" &&
                  Array.from(
                    { length: q.maximum - q.minimum + 1 },
                    (_, n) => n + q.minimum,
                  ).map((n) => (
                    <button
                      className="button secondary"
                      key={n}
                      disabled={busy}
                      onClick={() => send(n)}
                    >
                      {n}
                    </button>
                  ))}
              </div>
              {q.type === "MULTI_CHOICE" && (
                <>
                  {q.options.map((o: any) => (
                    <label key={o.value} style={{ display: "block" }}>
                      <input
                        type="checkbox"
                        checked={values.includes(o.value)}
                        onChange={() =>
                          setValues((v) =>
                            v.includes(o.value)
                              ? v.filter((x) => x !== o.value)
                              : [...v, o.value],
                          )
                        }
                      />
                      {o.label.text_vy || o.label.text_ty}
                    </label>
                  ))}
                  <button
                    className="button secondary"
                    disabled={busy || values.length === 0}
                    onClick={() => send(values)}
                  >
                    Сохранить выбор
                  </button>
                </>
              )}
              {q.type === "TEXT" && (
                <>
                  <Field label="Ваш ответ">
                    <textarea
                      value={text}
                      maxLength={q.max_text_length || 2000}
                      onChange={(e) => setText(e.target.value)}
                    />
                  </Field>
                  <button
                    className="button secondary"
                    disabled={busy || !text.trim()}
                    onClick={() => send(text)}
                  >
                    Сохранить ответ
                  </button>
                </>
              )}
              <button
                className="button secondary"
                disabled={busy}
                onClick={() => send(null, true)}
              >
                Пропустить вопрос
              </button>
            </>
          )}
          {r.awaiting_completion_confirmation && r.status === "IN_PROGRESS" && (
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => command("FINISH")}
            >
              Завершить опрос
            </button>
          )}
          {r.status === "PAUSED" && (
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => command("RESUME")}
            >
              Продолжить
            </button>
          )}
          {r.status === "IN_PROGRESS" && (
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => command("PAUSE")}
            >
              Пауза
            </button>
          )}
          {["IN_PROGRESS", "PAUSED"].includes(r.status) && (
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => command("DECLINE")}
            >
              Отказаться от опроса
            </button>
          )}
        </>
      )}
    </div>
  );
}
