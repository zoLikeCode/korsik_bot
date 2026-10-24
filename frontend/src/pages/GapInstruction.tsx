import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Plus } from "lucide-react";
import SourceDependencies from "../components/SourceDependencies";
import { api, errorText } from "../api";
import {
  ErrorBox,
  Field,
  Modal,
  Notice,
  Status,
  Success,
  useResource,
} from "../ui";

export default function GapInstruction({
  gap,
  onClose,
  onChange,
}: {
  gap: any;
  onClose: () => void;
  onChange: () => void;
}) {
  const state = useResource(`/knowledge/gaps/${gap.id}`);
  const [title, setTitle] = useState(""),
    [question, setQuestion] = useState(""),
    [text, setText] = useState(""),
    [variants, setVariants] = useState(""),
    [anonymous, setAnonymous] = useState(false),
    [dependencies, setDependencies] = useState<any[]>([]),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [busy, setBusy] = useState(false),
    [created, setCreated] = useState<any>(null),
    [drafts, setDrafts] = useState<any[]>([]);
  const data: any = state.data || gap;
  useEffect(() => {
    let active = true;
    Promise.all(
      (data.instruction_revision_ids || []).map((id: string) =>
        api.get(`/knowledge/revisions/${id}`).then((r) => r.data),
      ),
    )
      .then((rows) => active && setDrafts(rows))
      .catch((e) => active && setError(errorText(e)));
    return () => {
      active = false;
    };
  }, [state.data, created]);
  async function submit() {
    setBusy(true);
    setError("");
    try {
      const result = await api.post(
        `/knowledge/gaps/${gap.id}/instruction-drafts`,
        {
          title,
          instruction: {
            question,
            question_variants: variants
              .split("\n")
              .map((v) => v.trim())
              .filter(Boolean),
            answer_text: text,
          },
          dependencies,
          anonymization_confirmed: anonymous,
          reason:
            "Обезличенная инструкция HR на основании опубликованных документов",
        },
        state.etag,
      );
      setCreated(result.data);
      setSuccess(
        "Черновик создан. Проверьте его страницу, соберите индекс и опубликуйте в базе знаний. Пробел пока открыт.",
      );
      state.reload();
      onChange();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="Закрыть пробел в знаниях" onClose={onClose} wide>
      <div className="resolution">
        <h3>{data.question || data.title || data.query_text}</h3>
        <p>{data.description}</p>
      </div>
      <ErrorBox error={error || state.error} />
      {success && <Success>{success}</Success>}
      {!!drafts.length && (
        <section className="panel inset">
          <h3>Подготовленные инструкции</h3>
          {drafts.map((r) => (
            <div className="panel-head" key={r.id}>
              <div>
                <strong>{r.instruction?.question || "Инструкция HR"}</strong>
                <p>
                  <Status value={r.status} />
                </p>
              </div>
              {r.status === "PUBLISHED" && data.status !== "RESOLVED" && (
                <button
                  className="button secondary"
                  disabled={busy}
                  onClick={async () => {
                    setBusy(true);
                    setError("");
                    try {
                      await api.post(`/knowledge/gaps/${gap.id}/resolve`, {
                        instruction_revision_id: r.id,
                      });
                      state.reload();
                      onChange();
                      setSuccess("Пробел закрыт опубликованной инструкцией");
                    } catch (e) {
                      setError(errorText(e));
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  Закрыть этим ответом
                </button>
              )}
            </div>
          ))}
          <Link className="button secondary" to="/knowledge" onClick={onClose}>
            Открыть базу знаний
          </Link>
        </section>
      )}
      {data.status !== "RESOLVED" && !created && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <Notice>
            Напишите общий обезличенный вопрос и подтверждённый ответ.
            Персональная ситуация сотрудника останется в обращении; инструкция
            не публикуется автоматически.
          </Notice>
          <Field label="Название инструкции">
            <input
              required
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </Field>
          <Field label="Общий вопрос без персональных данных">
            <textarea
              required
              minLength={3}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
            />
          </Field>
          <Field label="Другие формулировки этого вопроса (с новой строки)">
            <textarea
              value={variants}
              onChange={(e) => setVariants(e.target.value)}
            />
          </Field>
          <Field label="Проверенный ответ и условия применения">
            <textarea
              required
              rows={7}
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
          </Field>
          <SourceDependencies
            allowDraft
            value={dependencies}
            onChange={setDependencies}
          />
          <label className="check-field">
            <input
              type="checkbox"
              required
              checked={anonymous}
              onChange={(e) => setAnonymous(e.target.checked)}
            />
            Проверено: в инструкции нет ФИО, контактов, ответов анкеты и других
            персональных сведений
          </label>
          <button
            className="button primary"
            disabled={busy || !anonymous || !dependencies.length}
          >
            <Plus size={16} />
            {busy ? "Создаём…" : "Создать черновик инструкции"}
          </button>
        </form>
      )}
      {created && (
        <Link className="button primary" to="/knowledge" onClick={onClose}>
          Проверить черновик в базе знаний
        </Link>
      )}
    </Modal>
  );
}
