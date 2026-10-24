import { useEffect, useState } from "react";
import { Plus, ShieldCheck } from "lucide-react";
import { api, errorText, items, explainIssue } from "../api";
import {
  Badge,
  Empty,
  ErrorBox,
  Field,
  Loading,
  Modal,
  Notice,
  Status,
  Success,
  useResource,
} from "../ui";

const modes: Record<string, string> = {
  EXTRACT: "Цитата из документа",
  APPROVED_CARD: "Согласованная инструкция",
  CLARIFY: "Уточняющий вопрос",
  HANDOFF: "Передать HR",
  LOCAL_GENERATED: "Ответ локальной модели",
};
const lines = (v: string) =>
  v
    .split("\n")
    .map((x) => x.trim())
    .filter(Boolean);

export function ControlQuestions() {
  const list = useResource("/knowledge/control-questions");
  const [edit, setEdit] = useState<any>(null);
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>Контрольные вопросы</h2>
        </div>
        <button className="button secondary" onClick={() => setEdit({})}>
          <Plus size={16} />
          Добавить вопрос
        </button>
      </div>
      <p className="muted">
        Зафиксируйте ожидаемый ответ и профиль сотрудника. При обновлении
        документа система повторит поиск по кандидату и проверит источники,
        точные фразы и режим ответа. Смысл ответа оценивает HR.
      </p>
      <ErrorBox error={list.error} />
      {list.loading ? (
        <Loading />
      ) : items(list.data).length ? (
        <div className="stack">
          {items(list.data).map((q) => (
            <button
              key={q.control_question_id}
              className="list-row"
              onClick={() => setEdit(q)}
            >
              <div>
                <strong>{q.question}</strong>
                <p>
                  {modes[q.expectation.mode] || q.expectation.mode} · версия{" "}
                  {q.version_no} ·{" "}
                  {q.expectation.critical ? "критический" : "обычный"}
                </p>
              </div>
              <Badge>
                {q.preview_profile.employment_type === "OFFICE"
                  ? "Офис"
                  : "Производство"}
              </Badge>
            </button>
          ))}
        </div>
      ) : (
        <Empty title="Проверьте важные вопросы до публикации">
          Например: точное время обеда, получение доступов, различия между
          площадками и безопасная передача HR при отсутствии источника.
        </Empty>
      )}
      {edit && (
        <ControlQuestionForm
          initial={edit}
          onClose={() => setEdit(null)}
          onDone={() => {
            setEdit(null);
            list.reload();
          }}
        />
      )}
    </section>
  );
}

function ControlQuestionForm({
  initial,
  onClose,
  onDone,
}: {
  initial: any;
  onClose: () => void;
  onDone: () => void;
}) {
  const sites = useResource("/sites"),
    legal = useResource("/legal-entities"),
    docs = useResource("/knowledge/documents");
  const [text, setText] = useState(initial.question || ""),
    [siteId, setSiteId] = useState(initial.preview_profile?.site_id || ""),
    [legalId, setLegalId] = useState(
      initial.preview_profile?.legal_entity_id || "",
    );
  const [employmentType, setEmploymentType] = useState(
      initial.preview_profile?.employment_type || "OFFICE",
    ),
    [stage, setStage] = useState(
      initial.preview_profile?.employment_stage || "ACTIVE",
    );
  const [asOf, setAsOf] = useState(
    initial.preview_profile?.as_of?.slice(0, 10) ||
      new Date().toLocaleDateString("en-CA", { timeZone: "Europe/Moscow" }),
  );
  const [mode, setMode] = useState(initial.expectation?.mode || "EXTRACT"),
    [include, setInclude] = useState(
      (initial.expectation?.must_include_literals || []).join("\n"),
    ),
    [exclude, setExclude] = useState(
      (initial.expectation?.must_exclude_literals || []).join("\n"),
    );
  const [critical, setCritical] = useState(
      initial.expectation?.critical ?? true,
    ),
    [rubric, setRubric] = useState(
      initial.expectation?.review_rubric ||
        "Сверить смысл ответа с источником, проверить числа и условия применения.",
    );
  const [docId, setDocId] = useState(""),
    [evidenceIds, setEvidenceIds] = useState<string[]>(
      initial.expectation?.evidence_revision_ids || [],
    ),
    [evidenceDetails, setEvidenceDetails] = useState<Record<string, any>>({});
  const revisions = useResource(
    docId ? `/knowledge/documents/${docId}/revisions` : null,
  );
  const [reason, setReason] = useState(
      initial.control_question_id
        ? "Уточнение ожиданий контрольного вопроса"
        : "Добавление контрольного вопроса HR",
    ),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  useEffect(() => {
    const site = items(sites.data).find((s) => s.id === siteId);
    if (site?.legal_entity_id) setLegalId(site.legal_entity_id);
  }, [siteId, sites.data]);
  useEffect(() => {
    let active = true;
    Promise.all(
      evidenceIds.map((id) =>
        api.get(`/knowledge/revisions/${id}`).then((r) => r.data),
      ),
    )
      .then(
        (rows) =>
          active &&
          setEvidenceDetails(Object.fromEntries(rows.map((r) => [r.id, r]))),
      )
      .catch((e) => active && setError(errorText(e)));
    return () => {
      active = false;
    };
  }, [evidenceIds.join(",")]);
  async function save() {
    setBusy(true);
    setError("");
    try {
      const payload = {
        question: text,
        preview_profile: {
          legal_entity_id: legalId,
          site_id: siteId,
          employment_type: employmentType,
          employment_stage: stage,
          as_of: `${asOf}T12:00:00+03:00`,
        },
        expectation: {
          mode,
          evidence_revision_ids: evidenceIds,
          evidence_block_ids:
            evidenceIds.length ===
              (initial.expectation?.evidence_revision_ids || []).length &&
            evidenceIds.every((id) =>
              (initial.expectation?.evidence_revision_ids || []).includes(id),
            )
              ? initial.expectation?.evidence_block_ids || []
              : [],
          instruction_revision_id: evidenceIds.includes(
            initial.expectation?.instruction_revision_id,
          )
            ? initial.expectation.instruction_revision_id
            : null,
          must_include_literals: lines(include),
          must_exclude_literals: lines(exclude),
          critical,
          review_rubric: rubric,
        },
        source_dependencies: initial.source_dependencies || [],
        reason,
      };
      if (initial.control_question_id) {
        await api.post(
          `/knowledge/control-questions/${initial.control_question_id}/versions`,
          payload,
          initial.etag,
        );
      } else await api.post("/knowledge/control-questions", payload);
      onDone();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title={
        initial.control_question_id
          ? "Новая версия контрольного вопроса"
          : "Контрольный вопрос"
      }
      onClose={onClose}
      wide
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        <Field label="Вопрос сотрудника">
          <textarea
            required
            minLength={3}
            maxLength={2000}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </Field>
        <div className="form-grid">
          <Field label="Площадка">
            <select
              required
              value={siteId}
              onChange={(e) => setSiteId(e.target.value)}
            >
              <option value="">Выберите площадку</option>
              {items(sites.data).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Юридическое лицо">
            <select
              required
              value={legalId}
              onChange={(e) => setLegalId(e.target.value)}
            >
              <option value="">Выберите юрлицо</option>
              {items(legal.data).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Тип сотрудника">
            <select
              value={employmentType}
              onChange={(e) => setEmploymentType(e.target.value)}
            >
              <option value="OFFICE">Офис</option>
              <option value="PRODUCTION">Производство</option>
              <option value="OTHER">Другое</option>
            </select>
          </Field>
          <Field label="Этап">
            <select value={stage} onChange={(e) => setStage(e.target.value)}>
              <option value="ACTIVE">После выхода</option>
              <option value="PREBOARDING">До выхода</option>
            </select>
          </Field>
          <Field label="Дата проверки действительности правил">
            <input
              required
              type="date"
              value={asOf}
              onChange={(e) => setAsOf(e.target.value)}
            />
          </Field>
          <Field label="Ожидаемый способ ответа">
            <select
              value={mode}
              onChange={(e) => {
                setMode(e.target.value);
                if (["HANDOFF", "CLARIFY"].includes(e.target.value))
                  setEvidenceIds([]);
              }}
            >
              {Object.entries(modes).map(([v, label]) => (
                <option key={v} value={v}>
                  {label}
                </option>
              ))}
            </select>
          </Field>
        </div>
        {!["HANDOFF", "CLARIFY"].includes(mode) && (
          <div>
            <Field label="Документ-основание">
              <select
                value={docId}
                onChange={(e) => {
                  setDocId(e.target.value);
                }}
              >
                <option value="">Без закреплённого документа</option>
                {items(docs.data).map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.title}
                  </option>
                ))}
              </select>
            </Field>
            {docId && (
              <div
                className="recipient-list"
                role="group"
                aria-label="Ожидаемые версии источников"
              >
                {items(revisions.data).map((r) => (
                  <label className="check-field" key={r.id}>
                    <input
                      type="checkbox"
                      checked={evidenceIds.includes(r.id)}
                      onChange={(e) =>
                        setEvidenceIds(
                          e.target.checked
                            ? [...evidenceIds, r.id]
                            : evidenceIds.filter((id) => id !== r.id),
                        )
                      }
                    />
                    {r.filename || r.instruction?.question || "Инструкция HR"} ·{" "}
                    <Status value={r.status} />
                  </label>
                ))}
              </div>
            )}
            {!!evidenceIds.length && (
              <div className="stack">
                {evidenceIds.map((id, index) => {
                  const row = evidenceDetails[id];
                  return (
                    <div className="panel-head" key={id}>
                      <span>
                        {items(docs.data).find((d) => d.id === row?.document_id)
                          ?.title || `Источник ${index + 1}`}{" "}
                        ·{" "}
                        {row?.filename ||
                          row?.instruction?.question ||
                          "Версия закреплена"}
                      </span>
                      <button
                        type="button"
                        className="text-button"
                        onClick={() =>
                          setEvidenceIds(evidenceIds.filter((v) => v !== id))
                        }
                      >
                        Убрать
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
            <small className="muted">
              Можно закрепить несколько источников. Без выбора проверяются режим
              ответа, точные фразы и доступность всех использованных документов.
            </small>
          </div>
        )}
        <div className="form-grid">
          <Field label="Обязательные точные фразы (каждая с новой строки)">
            <textarea
              value={include}
              onChange={(e) => setInclude(e.target.value)}
              placeholder="12:00"
            />
          </Field>
          <Field label="Недопустимые фразы (каждая с новой строки)">
            <textarea
              value={exclude}
              onChange={(e) => setExclude(e.target.value)}
              placeholder="14:00"
            />
          </Field>
        </div>
        <Field label="Что HR должен оценить в ответе">
          <textarea
            required
            minLength={10}
            value={rubric}
            onChange={(e) => setRubric(e.target.value)}
          />
        </Field>
        <label className="check-field">
          <input
            type="checkbox"
            checked={critical}
            onChange={(e) => setCritical(e.target.checked)}
          />
          Критическая проверка
        </label>
        <Field label="Причина изменения">
          <input
            required
            minLength={10}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </Field>
        <Notice>
          Ожидания сохраняются новой неизменяемой версией. Замена документа
          запускает проверки и требует пересмотреть закреплённые источники.
          Ручная оценка не отменяет провал машинных проверок.
        </Notice>
        <ErrorBox
          error={error || sites.error || legal.error || revisions.error}
        />
        <div className="modal-actions">
          <button type="button" className="button secondary" onClick={onClose}>
            Отмена
          </button>
          <button className="button primary" disabled={busy}>
            {busy ? "Сохраняем…" : "Сохранить ожидания"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

export function QaArtifactReview({
  id,
  onDone,
  readOnly = false,
}: {
  id: string;
  onDone: () => void;
  readOnly?: boolean;
}) {
  const state = useResource(`/knowledge/qa-artifacts/${id}`);
  const [checks, setChecks] = useState<Record<string, string>>({}),
    [reason, setReason] = useState(
      "Ответы сверены с источниками по критериям HR",
    ),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [success, setSuccess] = useState("");
  const qa: any = state.data || {},
    results = qa.control_suite?.results || [];
  const required = results.length
    ? results.map((r: any) => ({
        id: r.manual_check_id,
        label: r.review_rubric,
        result: r,
      }))
    : [
        { id: "OCR_NUMBERS", label: "Числа, даты и условия распознаны верно" },
        { id: "CITATIONS", label: "Ссылки указывают на нужные фрагменты" },
        { id: "ANSWER_MEANING", label: "Смысл ответа соответствует документу" },
      ];
  useEffect(() => {
    setChecks(
      Object.fromEntries(
        (qa.manual_checks || []).map((c: any) => [c.check_id, c.status]),
      ),
    );
  }, [state.data]);
  async function save() {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await api.post(
        `/knowledge/qa-artifacts/${id}/review`,
        {
          checks: required.map((c: any) => ({
            check_id: c.id,
            status: checks[c.id],
          })),
          reason,
        },
        state.etag || qa.etag,
      );
      setSuccess("Ручная оценка сохранена");
      state.reload();
      onDone();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel qa-review">
      <div className="panel-head">
        <h3>
          <ShieldCheck size={18} /> Проверка ответов
        </h3>
        <Status value={qa.status} />
      </div>
      <ErrorBox error={state.error || error} />
      {success && <Success>{success}</Success>}
      {state.loading ? (
        <Loading />
      ) : (
        <>
          <p className="muted">
            Машинные проверки:{" "}
            {Object.entries(qa.checks || {})
              .map(
                ([k, v]) =>
                  `${({ physical_coverage: "все страницы проверены", exact_evidence_links: "ссылки на источники", index_ack: "индекс готов", control_questions: "контрольные ответы" } as any)[k] || k} — ${v ? "пройдено" : "ошибка"}`,
              )
              .join("; ")}
            . Смысл и применимость ответа подтверждает HR.
          </p>
          {qa.blockers?.length > 0 && (
            <ErrorBox error={qa.blockers.map(explainIssue).join("; ")} />
          )}
          {required.map((c: any) => (
            <article className="extraction-block" key={c.id}>
              {c.result && (
                <>
                  <div className="block-meta">
                    <Badge tone={c.result.passed ? "green" : "red"}>
                      {c.result.passed
                        ? "Проверки пройдены"
                        : "Проверки не пройдены"}
                    </Badge>
                    <span>
                      {modes[c.result.actual_mode] || c.result.actual_mode}
                    </span>
                  </div>
                  <p>{c.result.actual_text || "Ответ без текста"}</p>
                  <small className="muted">
                    Источников: {c.result.citation_revision_ids?.length || 0}.{" "}
                    {Object.entries(c.result.checks || {})
                      .filter(([, ok]) => !ok)
                      .map(([key]) => explainIssue(key))
                      .join("; ")}
                  </small>
                </>
              )}
              <Field label={c.label}>
                <select
                  required
                  disabled={readOnly}
                  value={checks[c.id] || ""}
                  onChange={(e) =>
                    setChecks({ ...checks, [c.id]: e.target.value })
                  }
                >
                  <option value="">Оцените результат</option>
                  <option value="PASS">Соответствует</option>
                  <option value="FAIL">Нужна доработка</option>
                </select>
              </Field>
            </article>
          ))}
          <Field label="Комментарий проверяющего">
            <input
              value={reason}
              disabled={readOnly}
              minLength={3}
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>
          <button
            className="button secondary"
            disabled={
              readOnly ||
              busy ||
              qa.status !== "PASSED" ||
              required.some((c: any) => !checks[c.id]) ||
              reason.length < 3
            }
            onClick={save}
          >
            {busy ? "Сохраняем…" : "Сохранить ручную оценку"}
          </button>
          {qa.status === "FAILED" && (
            <Notice>
              Исправьте источник или ожидания и повторите проверку. Неуспешный
              машинный результат нельзя согласовать вручную.
            </Notice>
          )}
        </>
      )}
    </section>
  );
}
