import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import {
  Send,
  MessageCircle,
  ArrowUpRight,
  BookOpen,
  ChevronRight,
  ClipboardList,
  Pause,
  Play,
  Reply,
  X,
} from "lucide-react";
import { useActor } from "../App";
import { api, items, errorText, apiSource } from "../api";
import EmployeeAvatar from "../EmployeeAvatar";
import "./Chat.css";
import {
  PageTitle,
  useResource,
  Loading,
  ErrorBox,
  Empty,
  Status,
  Modal,
  Field,
  Notice,
  time,
  SearchInput,
} from "../ui";
export function conversationNeedsReply(conversation: any): boolean {
  if (Number.isFinite(conversation.unresolved_question_count))
    return conversation.unresolved_question_count > 0;
  return (
    conversation.mode === "WAITING_HR" ||
    Boolean(conversation.active_handoff_case_id)
  );
}
export async function postDialogMessage(
  id: string,
  employee: boolean,
  input: any,
  clientId: string,
  replyToId?: string,
  etag?: string,
) {
  const reply = replyToId ? { reply_to_message_id: replyToId } : {};
  return api.post(
    employee ? `/conversations/${id}/turns` : `/conversations/${id}/messages`,
    employee
      ? { input, client_message_id: clientId, ...reply }
      : { text: input.text, client_message_id: clientId, ...reply },
    employee ? undefined : etag,
  );
}

export default function Chat() {
  const actor = useActor(),
    employee = actor.role === "EMPLOYEE";
  const conversations = useResource("/conversations"),
    [params] = useSearchParams();
  const [id, setId] = useState(params.get("conversation") || ""),
    [query, setQuery] = useState("");
  useEffect(() => {
    const rows = items(conversations.data);
    if (!id && rows.length) setId(rows[0].id);
  }, [conversations.data]);
  useEffect(() => {
    const refresh = () => conversations.reload();
    const timer = setInterval(refresh, 8000);
    window.addEventListener("korsik:dialog-problems-updated", refresh);
    return () => {
      clearInterval(timer);
      window.removeEventListener("korsik:dialog-problems-updated", refresh);
    };
  }, []);
  const rows = items(conversations.data).filter((c) =>
    `${c.employee_name || c.name || ""}`
      .toLowerCase()
      .includes(query.toLowerCase()),
  );
  return (
    <>
      <PageTitle title={employee ? "Диалог" : "Диалоги"}></PageTitle>
      <ErrorBox error={conversations.error} />
      <div
        className={`chat-workspace dialog-workspace ${employee ? "employee-chat" : ""}`}
      >
        {!employee && (
          <aside className="conversation-list">
            <SearchInput
              value={query}
              onChange={setQuery}
              placeholder="Найти сотрудника"
            />
            {rows.map((c) => (
              <button
                key={c.id}
                className={
                  id === c.id ? "conversation-card active" : "conversation-card"
                }
                onClick={() => setId(c.id)}
              >
                <EmployeeAvatar
                  name={c.employee_name || c.name || "Сотрудник"}
                  src={c.employee_photo_url}
                  small
                />
                <div>
                  <strong>
                    {c.employee_name ||
                      c.name ||
                      `Сотрудник ${c.employee_id?.slice(0, 6)}`}
                  </strong>
                  <small>
                    {conversationNeedsReply(c)
                      ? "Нужен ответ"
                      : c.mode === "HUMAN"
                        ? "Диалог"
                        : "Диалог с Корсиком"}
                  </small>
                </div>
                {c.unresolved_question_count > 0 && (
                  <span
                    className="dialog-problem-count"
                    aria-label={`Вопросов без ответа: ${c.unresolved_question_count}`}
                  >
                    {c.unresolved_question_count}
                  </span>
                )}
              </button>
            ))}
          </aside>
        )}
        {id ? (
          <Conversation key={id} id={id} employee={employee} />
        ) : conversations.loading ? (
          <Loading />
        ) : (
          <Empty title="Диалог пока не создан">
            {employee
              ? "После подтверждения приглашения здесь появится ваш диалог с Корсиком."
              : "Сотрудники появятся после подключения к MAX."}
          </Empty>
        )}
      </div>
    </>
  );
}
function Conversation({ id, employee }: { id: string; employee: boolean }) {
  const state = useResource(`/conversations/${id}`),
    messages = useResource(`/conversations/${id}/messages`),
    prompts = useResource(
      employee ? `/conversations/${id}/quick-prompts` : null,
    );
  const [value, setValue] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [pending, setPending] = useState(false),
    [surveyId, setSurveyId] = useState(""),
    [replyTarget, setReplyTarget] = useState<any>(null);
  const command = useRef<{ key: string; id: string } | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const answerTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const c: any = state.data || {};
  const surveyList = useResource(
    employee && c.employee_id ? `/employees/${c.employee_id}/surveys` : null,
    [c.employee_id],
  );
  useEffect(() => {
    const interval = setInterval(() => {
      messages.reload();
      state.reload();
      surveyList.reload();
    }, 4000);
    return () => clearInterval(interval);
  }, [id]);
  useEffect(
    () => () => {
      if (answerTimer.current) clearInterval(answerTimer.current);
    },
    [id],
  );
  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [items(messages.data).slice(-1)[0]?.id]);
  useEffect(() => {
    const active = items(surveyList.data).find((s) =>
      ["ACTIVE", "OPEN", "IN_PROGRESS", "AWAITING_ANSWER", "PAUSED"].includes(
        s.status,
      ),
    );
    setSurveyId(c.active_survey_run_id || active?.id || "");
  }, [c.active_survey_run_id, surveyList.data]);
  async function send(input: any) {
    if (busy) return;
    setError("");
    setBusy(true);
    try {
      const key = JSON.stringify({ input, reply: replyTarget?.id || null });
      if (command.current?.key !== key)
        command.current = { key, id: crypto.randomUUID() };
      const r = await postDialogMessage(
        id,
        employee,
        input,
        command.current.id,
        replyTarget?.id,
        state.etag ?? undefined,
      );
      setValue("");
      setReplyTarget(null);
      command.current = null;
      window.dispatchEvent(new Event("korsik:dialog-problems-updated"));
      setPending(employee && r.data.dispatch === "BOT_ANSWER");
      messages.reload();
      state.reload();
      prompts.reload();
      if (r.data.dispatch === "BUSY")
        setError(
          r.data.message ||
            "Предыдущий вопрос ещё обрабатывается. Попробуйте чуть позже.",
        );
      if (r.data.answer_id) {
        let attempts = 0;
        const timer = setInterval(async () => {
          attempts++;
          try {
            const answer = await api.get(`/answers/${r.data.answer_id}`);
            if (
              [
                "COMPLETED",
                "ANSWERED",
                "ABSTAINED",
                "FAILED",
                "READY",
                "SUCCEEDED",
                "CANCELLED",
                "EXPIRED",
              ].includes(answer.data.status) ||
              attempts > 90
            ) {
              clearInterval(timer);
              setPending(false);
              messages.reload();
              window.dispatchEvent(new Event("korsik:dialog-problems-updated"));
              if (answer.data.status === "FAILED")
                setError("Не удалось получить ответ. Сообщение сохранено.");
            }
          } catch {
            clearInterval(timer);
            setPending(false);
          }
        }, 2000);
        answerTimer.current = timer;
      }
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="chat-panel">
      <header className="chat-header">
        {employee ? (
          <span className="bot-avatar">к.</span>
        ) : (
          <EmployeeAvatar
            name={c.employee_name || "Сотрудник"}
            src={c.employee_photo_url}
          />
        )}
        <div>
          <strong>
            {employee ? "Корсик" : c.employee_name || "Сотрудник"}
          </strong>
          <small>
            {employee
              ? c.mode === "HUMAN" || c.mode === "WAITING_HR"
                ? "Вопрос в работе"
                : "Корсик"
              : c.mode === "HUMAN"
                ? "Команда поддержки"
                : conversationNeedsReply(c)
                  ? "Нужен ответ"
                  : "Корсик"}
          </small>
        </div>
        {!employee && (
          <Status
            value={
              c.unresolved_question_count > 0
                ? `Вопросов без ответа: ${c.unresolved_question_count}`
                : "Нет вопросов без ответа"
            }
          />
        )}
        {!employee && c.mode === "HUMAN" && (
          <button
            className="button secondary compact"
            onClick={async () => {
              try {
                await api.post(
                  `/conversations/${id}/resume-bot`,
                  {},
                  state.etag,
                );
                state.reload();
                messages.reload();
              } catch (e) {
                setError(errorText(e));
              }
            }}
          >
            Вернуть Корсику
          </button>
        )}
      </header>
      <div className="message-scroll">
        {messages.loading && !messages.data ? (
          <Loading />
        ) : items(messages.data).length ? (
          items(messages.data).map((m) => (
            <Message
              key={m.id}
              message={m}
              zone={c.employee_timezone || c.timezone || "Europe/Moscow"}
              showProblems={!employee}
              onReply={() => setReplyTarget(m)}
            />
          ))
        ) : (
          <div className="chat-empty">
            <span className="bot-avatar large">к.</span>
            <h2>Нет сообщений</h2>
            <p>Напишите сообщение.</p>
          </div>
        )}
        {pending && (
          <div className="message assistant">
            <span className="typing-dots">
              <i />
              <i />
              <i />
            </span>
            <small>Проверяю источники…</small>
          </div>
        )}
        <div ref={bottom} />
      </div>
      {employee &&
        (c.scenario_questions || []).some((q: any) => q.status === "OPEN") && (
          <ScenarioQuestionBanner
            conversationId={id}
            item={c.scenario_questions.find((q: any) => q.status === "OPEN")}
            existingCapture={c.active_input_capture}
            onUpdated={() => {
              state.reload();
              messages.reload();
            }}
          />
        )}
      {employee && surveyId && (
        <SurveyBanner
          conversationId={id}
          id={surveyId}
          onUpdated={() => {
            surveyList.reload();
            messages.reload();
          }}
        />
      )}
      {employee && items(prompts.data).length > 0 && (
        <div className="quick-prompts">
          {items(prompts.data).map((p: any) => (
            <button
              key={p.prompt_key}
              disabled={busy || pending}
              onClick={() =>
                send({
                  kind: "RAG_QUERY",
                  menu_id: p.menu_id || prompts.data?.menu_id,
                  menu_version_id:
                    p.menu_version_id || prompts.data?.menu_version_id,
                  prompt_key: p.prompt_key,
                })
              }
            >
              <MessageCircle size={14} />
              {p.label || p.label_ty || p.label_vy}
              <ArrowUpRight size={13} />
            </button>
          ))}
        </div>
      )}
      {replyTarget && (
        <div
          className="dialog-compose-quote"
          aria-label="Выбранное сообщение для ответа"
        >
          <div>
            <strong>В ответ на сообщение</strong>
            <p>{replyTarget.text || replyTarget.content}</p>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label="Отменить ответ с цитатой"
            onClick={() => setReplyTarget(null)}
          >
            <X size={18} />
          </button>
        </div>
      )}
      <ErrorBox error={error || messages.error} />
      <form
        className="chat-compose"
        onSubmit={(e) => {
          e.preventDefault();
          if (value.trim()) send({ kind: "TEXT", text: value.trim() });
        }}
      >
        <textarea
          aria-label={employee ? "Ваш вопрос" : "Сообщение сотруднику"}
          rows={1}
          maxLength={replyTarget ? 3300 : employee ? 8000 : 3900}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={
            employee ? "Напишите вопрос Корсику…" : "Напишите сотруднику…"
          }
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              if (value.trim()) send({ kind: "TEXT", text: value.trim() });
            }
          }}
        />
        <button
          aria-label="Отправить сообщение"
          className="button primary"
          disabled={busy || pending || !value.trim()}
        >
          <Send size={19} />
        </button>
      </form>
    </section>
  );
}
export function Message({
  message: m,
  zone,
  showProblems = false,
  onReply,
}: {
  message: any;
  zone: string;
  showProblems?: boolean;
  onReply?: () => void;
}) {
  const mine = ["EMPLOYEE", "USER", "employee", "user"].includes(
      m.author_type || m.role,
    ),
    citations = m.data?.citations || m.citations || [];
  return (
    <article
      className={`message ${mine ? "mine employee-message" : "assistant korsik-message"}`}
      data-message-id={m.id}
    >
      <div className="message-author">
        {mine ? "Сотрудник" : "Корсик"}
        <time>{time(m.created_at, zone)}</time>
      </div>
      {m.reply_to && (
        <blockquote className="dialog-message-quote">
          <span>
            {["EMPLOYEE", "USER"].includes(m.reply_to.author_type)
              ? "Сотрудник"
              : "Корсик"}
          </span>
          {m.reply_to.text}
        </blockquote>
      )}
      <div className="message-text">{m.text || m.content}</div>
      {showProblems && mine && m.support_issue?.status === "OPEN" && (
        <span className="dialog-unanswered-badge">Корсик не знает ответа</span>
      )}
      {onReply && (
        <button
          type="button"
          className="dialog-reply-action"
          aria-label={`Ответить на сообщение №${m.seq}`}
          onClick={onReply}
        >
          <Reply size={14} />
          Ответить с цитатой
        </button>
      )}
      {citations.length > 0 && (
        <div className="citations">
          <span>
            <BookOpen size={13} />
            Источники ответа
          </span>
          {citations.map((c: any, i: number) => (
            <details key={c.id || c.citation_id || i}>
              <summary>
                {c.title || c.document_title || `Источник ${i + 1}`}
                {c.page_no ? ` · стр. ${c.page_no}` : ""}
                <ChevronRight size={14} />
              </summary>
              {c.quote && <blockquote>{c.quote}</blockquote>}
              <a
                target="_blank"
                rel="noreferrer"
                href={apiSource(
                  c.source_url,
                  `/api/v1/citations/${c.citation_id || c.id}/source`,
                )}
              >
                Открыть источник
                <ArrowUpRight size={13} />
              </a>
            </details>
          ))}
        </div>
      )}
      {m.data?.mode &&
        ["EXTRACTIVE", "EXTRACT", "CLARIFY", "HANDOFF"].includes(
          m.data.mode,
        ) && (
          <small className="answer-mode">
            {["EXTRACTIVE", "EXTRACT"].includes(m.data.mode)
              ? "Цитата из документа"
              : m.data.mode === "CLARIFY"
                ? "Требуется уточнение"
                : "Вопрос в работе"}
          </small>
        )}
    </article>
  );
}
function ScenarioQuestionBanner({
  conversationId,
  item,
  existingCapture,
  onUpdated,
}: {
  conversationId: string;
  item: any;
  existingCapture: any;
  onUpdated: () => void;
}) {
  const [open, setOpen] = useState(false),
    [value, setValue] = useState<any>(""),
    [capture, setCapture] = useState<any>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [confirm, setConfirm] = useState(false);
  const q = item.question || {},
    questionId = item.id || item.scenario_question_id || item.question_id;
  useEffect(() => {
    const cap = existingCapture;
    if (
      cap &&
      (cap.scenario_question_id === questionId ||
        cap.target?.scenario_question_id === questionId) &&
      ["OPEN", "DRAFT", "READY_TO_CONFIRM"].includes(cap.status || cap.state)
    ) {
      setCapture(cap);
      setValue(cap.draft || cap.value?.text || cap.value?.asset_id || "");
      setConfirm(Boolean(cap.draft_fingerprint));
    }
  }, [existingCapture?.id, existingCapture?.capture_id]);
  async function run(fn: () => Promise<any>) {
    setBusy(true);
    setError("");
    try {
      await fn();
      onUpdated();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function save() {
    await run(async () => {
      if (!["TEXT", "PHOTO"].includes(q.type)) {
        await api.post(`/scenario-questions/${questionId}/answers`, {
          question_id: q.question_key,
          value,
          outcome: "ANSWERED",
        });
        setOpen(false);
        return;
      }
      if (!confirm) {
        let cap = capture;
        if (!cap) {
          cap = (
            await api.post("/input-captures", {
              scenario_question_id: questionId,
              question_id: q.question_key,
              capture_kind: "ANSWER",
            })
          ).data;
        }
        const payload =
          q.type === "PHOTO"
            ? {
                attachment_id: value,
                value: { kind: "ATTACHMENT", asset_id: value },
              }
            : { text: value, value: { kind: "TEXT", text: value } };
        const r = await api.put(
          `/input-captures/${cap.id || cap.capture_id}/draft`,
          payload,
          cap.etag,
        );
        setCapture({ ...r.data, etag: r.etag });
        setConfirm(true);
        return;
      }
      const capId = capture.id || capture.capture_id;
      const latest = await api.get(`/input-captures/${capId}`);
      await api.post(
        `/input-captures/${capId}/confirm`,
        { confirm: true, draft_fingerprint: latest.data.draft_fingerprint },
        latest.etag,
      );
      setOpen(false);
    });
  }
  async function cancel() {
    await run(async () => {
      const cap = capture || existingCapture;
      if (
        cap &&
        ["OPEN", "DRAFT", "READY_TO_CONFIRM"].includes(cap.status || cap.state)
      ) {
        const r = await api.get(`/input-captures/${cap.id || cap.capture_id}`);
        await api.post(
          `/input-captures/${cap.id || cap.capture_id}/cancel`,
          { cancel: true },
          r.etag,
        );
      }
      setCapture(null);
      setConfirm(false);
      setOpen(false);
    });
  }
  return (
    <>
      <div className="survey-banner">
        <MessageCircle size={19} />
        <div>
          <strong>{q.prompt?.text_ty || "Вопрос для знакомства"}</strong>
          <small>
            Это отдельный шаг адаптации, ответ сохраняется после вашего
            действия.
          </small>
        </div>
        <button
          className="button secondary compact"
          onClick={() => setOpen(true)}
        >
          Ответить
          <ChevronRight size={15} />
        </button>
      </div>
      {open && (
        <Modal title="Шаг адаптации" onClose={() => setOpen(false)}>
          <h3 className="survey-prompt">
            {q.prompt?.text_ty || q.question_key}
          </h3>
          <ErrorBox error={error} />
          {q.options ? (
            <div className="survey-options">
              {q.options.map((o: any) => (
                <button
                  key={o.value || o.option_key}
                  className={
                    value === (o.value || o.option_key) ? "selected" : ""
                  }
                  onClick={() => setValue(o.value || o.option_key)}
                >
                  {o.label?.text_ty || o.value || o.option_key}
                </button>
              ))}
            </div>
          ) : q.type === "PHOTO" ? (
            <Field label="Ваше фото">
              <input
                type="file"
                accept="image/png,image/jpeg"
                onChange={async (e) => {
                  const f = e.target.files?.[0];
                  if (!f) return;
                  const data = new FormData();
                  data.append("file", f);
                  await run(async () => {
                    const r = await api.upload(
                      `/conversations/${conversationId}/attachments`,
                      data,
                    );
                    setValue(r.data.asset_id || r.data.id);
                    setConfirm(false);
                  });
                }}
              />
            </Field>
          ) : (
            <Field label="Ваш ответ">
              <textarea
                maxLength={q.max_text_length || 12000}
                rows={5}
                value={value}
                onChange={(e) => {
                  setValue(e.target.value);
                  setConfirm(false);
                }}
              />
            </Field>
          )}
          {confirm && (
            <Notice>
              Сохранить как ответ на этот шаг? Текст не станет вопросом к
              ассистенту. Фото и биография не публикуются в корпоративных
              материалах автоматически.
            </Notice>
          )}
          <div className="button-row wrap">
            <button
              className="button primary"
              disabled={busy || !value}
              onClick={save}
            >
              {confirm
                ? "Подтвердить сохранение"
                : ["TEXT", "PHOTO"].includes(q.type)
                  ? "Проверить ответ"
                  : "Сохранить выбор"}
            </button>
            <button
              className="button secondary"
              disabled={busy}
              onClick={cancel}
            >
              Отменить ввод
            </button>
            <button
              className="text-button"
              disabled={busy}
              onClick={() =>
                run(async () => {
                  await api.post(`/scenario-questions/${questionId}/answers`, {
                    question_id: q.question_key,
                    outcome: "DECLINED",
                    answer_outcome: "DECLINED",
                  });
                  setOpen(false);
                })
              }
            >
              Не хочу отвечать
            </button>
          </div>
        </Modal>
      )}
    </>
  );
}
function SurveyBanner({
  id,
  onUpdated,
  conversationId,
}: {
  id: string;
  onUpdated: () => void;
  conversationId: string;
}) {
  const survey = useResource(`/survey-runs/${id}`),
    [open, setOpen] = useState(false);
  const s: any = survey.data || {};
  return (
    <>
      <div className="survey-banner">
        <ClipboardList size={19} />
        <div>
          <strong>{s.title || s.survey_key || "Ваша анкета адаптации"}</strong>
          <small>
            {s.status === "PAUSED"
              ? "Пауза. Можно продолжить, пока срок не истёк."
              : "Есть незавершённая анкета."}
          </small>
        </div>
        <button
          className="button secondary compact"
          onClick={() => setOpen(true)}
        >
          Продолжить
          <ChevronRight size={15} />
        </button>
      </div>
      {open && (
        <SurveyForm
          conversationId={conversationId}
          id={id}
          onClose={() => setOpen(false)}
          onUpdated={() => {
            survey.reload();
            onUpdated();
          }}
        />
      )}
    </>
  );
}
function SurveyForm({
  id,
  onClose,
  onUpdated,
  conversationId,
}: {
  id: string;
  onClose: () => void;
  onUpdated: () => void;
  conversationId: string;
}) {
  const state = useResource(`/survey-runs/${id}`),
    [value, setValue] = useState<any>(""),
    [comment, setComment] = useState(""),
    [confirm, setConfirm] = useState(false),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const s: any = state.data || {},
    q =
      s.current_question ||
      s.question ||
      s.questions?.find(
        (v: any) => v.question_key === s.current_question_key,
      ) ||
      {};
  useEffect(() => {
    setValue("");
    setComment("");
    setConfirm(false);
  }, [q.question_key, q.id]);
  useEffect(() => {
    if (s.pending_answer && value === "") setValue(s.pending_answer.value);
  }, [s.pending_answer]);
  async function answer(decline = false, declineComment = false) {
    setBusy(true);
    setError("");
    try {
      await api.post(
        `/survey-runs/${id}/answers`,
        {
          question_id: q.question_id || q.id || q.question_key,
          expected_state_seq: s.state_seq ?? s.version ?? 0,
          state_revision: s.question_revision ?? 1,
          ...(decline
            ? { answer_outcome: "DECLINED", outcome: "DECLINED" }
            : {
                answer_outcome: "ANSWERED",
                outcome: "ANSWERED",
                value,
                ...(declineComment
                  ? { comment_outcome: "DECLINED" }
                  : comment
                    ? { comment, comment_outcome: "PROVIDED" }
                    : { comment_outcome: "NOT_REQUESTED" }),
              }),
        },
        state.etag ?? undefined,
      );
      state.reload();
      onUpdated();
      setConfirm(false);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function command(command: string, extra: any = {}) {
    try {
      await api.post(
        `/survey-runs/${id}/commands`,
        {
          action: command,
          command,
          expected_state_seq: s.state_seq ?? s.version ?? 0,
          ...extra,
        },
        state.etag ?? undefined,
      );
      state.reload();
      onUpdated();
      if (command === "PAUSE") onClose();
    } catch (e) {
      setError(errorText(e));
    }
  }
  return (
    <Modal title="Анкета адаптации" onClose={onClose}>
      <ErrorBox error={error || state.error} />
      {state.loading ? (
        <Loading />
      ) : ["COMPLETED", "DECLINED", "EXPIRED", "CANCELLED"].includes(
          s.status,
        ) ? (
        <Empty
          title={
            s.status === "COMPLETED" ? "Спасибо за ответы!" : "Анкета закрыта"
          }
        >
          {s.status === "EXPIRED"
            ? "Срок ответа истёк. Повторное открытие согласуется с командой поддержки."
            : "Ваш результат сохранён."}
        </Empty>
      ) : s.awaiting_completion_confirmation ? (
        <>
          <Notice>
            Все ответы сохранены. Проверьте их перед завершением анкеты.
          </Notice>
          <div className="survey-review">
            {Object.entries(s.answers || {}).map(([key, a]: any) => (
              <div className="list-row" key={key}>
                <div>
                  <strong>
                    {s.definition?.questions?.find(
                      (q: any) => q.question_key === key,
                    )?.prompt?.text_ty || key}
                  </strong>
                  <p>
                    {a.outcome === "DECLINED"
                      ? "Отказ от ответа"
                      : Array.isArray(a.value)
                        ? a.value.join(", ")
                        : String(a.value ?? "")}
                  </p>
                </div>
                <button
                  className="text-button"
                  onClick={() => command("EDIT_ANSWER", { question_id: key })}
                >
                  Исправить
                </button>
              </div>
            ))}
          </div>
          <div className="modal-actions">
            <button
              className="button primary"
              onClick={() => command("FINISH")}
            >
              Завершить анкету
            </button>
          </div>
        </>
      ) : s.status === "PAUSED" ? (
        <>
          <Notice>Анкета на паузе. Срок ответа продолжает идти.</Notice>
          <button className="button primary" onClick={() => command("RESUME")}>
            <Play size={16} />
            Продолжить
          </button>
        </>
      ) : (
        <>
          <div className="section-label">{s.survey_key || "Опрос"}</div>
          <h3 className="survey-prompt">
            {q.prompt?.text_ty || q.text || q.title || "Вопрос загружается…"}
          </h3>
          {q.type === "PHOTO" ? (
            <Field label="Прикрепить фото">
              <input
                type="file"
                accept="image/jpeg,image/png"
                onChange={async (e) => {
                  const f = e.target.files?.[0];
                  if (!f) return;
                  const data = new FormData();
                  data.append("file", f);
                  try {
                    const r = await api.upload(
                      `/conversations/${conversationId}/attachments`,
                      data,
                    );
                    setValue(r.data.asset_id || r.data.id);
                    setConfirm(false);
                  } catch (e) {
                    setError(errorText(e));
                  }
                }}
              />
            </Field>
          ) : q.type === "NUMERIC" || q.answer_type === "SCALE" ? (
            <div className="scale-options">
              {Array.from({ length: 10 }, (_, i) => i + 1).map((v) => (
                <button
                  key={v}
                  className={value === v ? "selected" : ""}
                  onClick={() => setValue(v)}
                >
                  {v}
                </button>
              ))}
            </div>
          ) : q.options ? (
            <div className="survey-options">
              {q.options.map((o: any) => (
                <button
                  key={o.value}
                  className={
                    (
                      q.type === "MULTI_CHOICE"
                        ? Array.isArray(value) && value.includes(o.value)
                        : value === o.value
                    )
                      ? "selected"
                      : ""
                  }
                  onClick={() =>
                    setValue(
                      q.type === "MULTI_CHOICE"
                        ? Array.isArray(value) && value.includes(o.value)
                          ? value.filter((v: any) => v !== o.value)
                          : [...(Array.isArray(value) ? value : []), o.value]
                        : o.value,
                    )
                  }
                >
                  {o.label?.text_ty || o.label_ty || o.label || o.value}
                </button>
              ))}
            </div>
          ) : (
            <Field label="Ваш ответ">
              <textarea
                rows={5}
                value={value}
                onChange={(e) => {
                  setValue(e.target.value);
                  setConfirm(false);
                }}
                maxLength={q.max_text_length || 10000}
              />
            </Field>
          )}
          {s.pending_answer && (
            <Notice>
              Для этой оценки можно пояснить причину или явно отказаться от
              комментария.
            </Notice>
          )}
          {q.comment_when && (
            <Field label="Комментарий, если хотите пояснить">
              <textarea
                value={comment}
                onChange={(e) => setComment(e.target.value)}
              />
            </Field>
          )}
          {confirm && (
            <Notice>
              Сохранить этот текст как ответ на вопрос анкеты? Он не будет
              отправлен ассистенту как консультация.
            </Notice>
          )}
          <div className="button-row wrap">
            <button
              className="button primary"
              disabled={busy || value === ""}
              onClick={() =>
                q.type === "TEXT" && !confirm ? setConfirm(true) : answer()
              }
            >
              {q.type === "TEXT" && !confirm
                ? "Проверить ответ"
                : "Сохранить ответ"}
            </button>
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => answer(true)}
            >
              Не отвечать на вопрос
            </button>
            {s.pending_answer && (
              <button
                className="button secondary"
                disabled={busy}
                onClick={() => answer(false, true)}
              >
                Не раскрывать причину
              </button>
            )}
          </div>
          <div className="modal-actions">
            <button className="text-button" onClick={() => command("PAUSE")}>
              <Pause size={15} />
              Продолжить позже
            </button>
            <button className="text-button" onClick={onClose}>
              Задать вопрос Корсику
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}
