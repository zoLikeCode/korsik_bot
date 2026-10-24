import { useEffect, useState } from "react";
import SourceDependencies from "../components/SourceDependencies";
import { useActor } from "../App";
import { api, errorText, items } from "../api";
import {
  ErrorBox,
  Field,
  Loading,
  Modal,
  Notice,
  Success,
  useResource,
} from "../ui";
export function KnowledgeScopeFields({
  value,
  onChange,
}: {
  value: any;
  onChange: (v: any) => void;
}) {
  const sites = useResource("/sites"),
    legal = useResource("/legal-entities");
  function toggle(field: string, key: string, checked: boolean) {
    onChange({
      ...value,
      [field]: checked
        ? [...(value[field] || []), key]
        : (value[field] || []).filter((v: string) => v !== key),
    });
  }
  return (
    <>
      <ErrorBox error={sites.error || legal.error} />
      <div className="form-grid">
        <Field
          label="Площадки, которым доступен документ"
          hint="Для нескольких площадок выберите каждую. Пустой список означает общую область и допускается только при соответствующих правах."
        >
          <div className="scope-options">
            {items(sites.data).map((s) => (
              <label className="check-field" key={s.id}>
                <input
                  type="checkbox"
                  checked={(value.site_ids || []).includes(s.id)}
                  onChange={(e) => toggle("site_ids", s.id, e.target.checked)}
                />
                {s.name}
              </label>
            ))}
          </div>
        </Field>
        <Field label="Юридические лица">
          <div className="scope-options">
            {items(legal.data).map((l) => (
              <label className="check-field" key={l.id}>
                <input
                  type="checkbox"
                  checked={(
                    value.legal_entity_ids ||
                    (value.legal_entity_id ? [value.legal_entity_id] : [])
                  ).includes(l.id)}
                  onChange={(e) => {
                    const ids =
                      value.legal_entity_ids ||
                      (value.legal_entity_id ? [value.legal_entity_id] : []);
                    const next = {
                      ...value,
                      legal_entity_ids: e.target.checked
                        ? [...ids, l.id]
                        : ids.filter((v: string) => v !== l.id),
                    };
                    delete next.legal_entity_id;
                    onChange(next);
                  }}
                />
                {l.name}
              </label>
            ))}
          </div>
        </Field>
      </div>
      <div className="form-grid">
        <div>
          <strong>Категории сотрудников</strong>
          {Object.entries({
            OFFICE: "Офис",
            PRODUCTION: "Производство",
            OTHER: "Другие",
          }).map(([key, name]) => (
            <label className="check-field" key={key}>
              <input
                type="checkbox"
                checked={(value.employment_types || []).includes(key)}
                onChange={(e) =>
                  toggle("employment_types", key, e.target.checked)
                }
              />
              {name}
            </label>
          ))}
        </div>
        <div>
          <strong>Этапы, когда доступен материал</strong>
          {Object.entries({
            PREBOARDING: "До выхода на работу",
            ACTIVE: "После выхода",
          }).map(([key, name]) => (
            <label className="check-field" key={key}>
              <input
                type="checkbox"
                checked={(value.employment_stages || []).includes(key)}
                onChange={(e) =>
                  toggle("employment_stages", key, e.target.checked)
                }
              />
              {name}
            </label>
          ))}
        </div>
      </div>
    </>
  );
}
export function ArticleEditor({
  onClose,
  onCreated,
  existingDocument,
}: {
  onClose: () => void;
  onCreated: (documentId: string) => void | Promise<void>;
  existingDocument?: any;
}) {
  const actor = useActor(),
    [title, setTitle] = useState(existingDocument?.title || ""),
    [kind, setKind] = useState("APPROVED_INSTRUCTION"),
    [question, setQuestion] = useState(""),
    [variants, setVariants] = useState(""),
    [text, setText] = useState(""),
    [effectiveFrom, setEffectiveFrom] = useState(""),
    [effectiveTo, setEffectiveTo] = useState(""),
    [reason, setReason] = useState(""),
    [anonymous, setAnonymous] = useState(false),
    [dependencies, setDependencies] = useState<any[]>([]),
    [scope, setScope] = useState<any>(
      existingDocument?.scope || {
        site_ids: actor.scope?.site_ids || [],
        ...(actor.scope?.legal_entity_ids
          ? { legal_entity_ids: actor.scope.legal_entity_ids }
          : {}),
        employment_types: ["OFFICE", "PRODUCTION", "OTHER"],
        employment_stages: ["ACTIVE"],
      },
    ),
    [createdDoc, setCreatedDoc] = useState<any>(existingDocument || null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  return (
    <Modal title="Создать статью или инструкцию HR" onClose={onClose} wide>
      <ErrorBox error={error} />
      <Notice>
        Новый материал сохраняется черновиком. Страницы, доказательства,
        контрольные ответы и публикация проходят тот же процесс, что и
        загруженные файлы.
      </Notice>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError("");
          try {
            let doc = createdDoc;
            if (!doc) {
              const response = await api.post("/knowledge/documents", {
                title,
                scope,
                document_category: "INSTRUCTION",
              });
              doc = response.data;
              setCreatedDoc(doc);
            }
            const latest = await api.get(`/knowledge/documents/${doc.id}`);
            await api.post(
              `/knowledge/documents/${doc.id}/revisions/article`,
              {
                article_kind: kind,
                text,
                ...(effectiveFrom
                  ? { effective_from: new Date(effectiveFrom).toISOString() }
                  : {}),
                ...(effectiveTo
                  ? { effective_to: new Date(effectiveTo).toISOString() }
                  : {}),
                ...(kind === "APPROVED_INSTRUCTION"
                  ? {
                      instruction: {
                        question,
                        question_variants: variants
                          .split("\n")
                          .map((v) => v.trim())
                          .filter(Boolean),
                        answer_text: text,
                      },
                    }
                  : {}),
                dependencies,
                reason,
              },
              latest.etag || latest.data.etag,
            );
            await onCreated(doc.id);
          } catch (e) {
            setError(errorText(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="form-grid">
          <Field label="Название материала">
            <input
              required
              maxLength={500}
              value={title}
              disabled={!!createdDoc}
              onChange={(e) => setTitle(e.target.value)}
            />
          </Field>
          <Field label="Вид материала">
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="APPROVED_INSTRUCTION">
                Точный ответ HR с документами-основаниями
              </option>
              <option value="GENERAL">Общая статья базы знаний</option>
            </select>
          </Field>
        </div>
        {kind === "APPROVED_INSTRUCTION" && (
          <>
            <Field label="На какой общий вопрос отвечает инструкция">
              <textarea
                required
                minLength={3}
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
              />
            </Field>
            <Field label="Другие формулировки вопроса — по одной на строку">
              <textarea
                value={variants}
                onChange={(e) => setVariants(e.target.value)}
              />
            </Field>
          </>
        )}
        <Field label="Текст и условия применения">
          <textarea
            required
            rows={9}
            maxLength={100000}
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
        </Field>
        <div className="form-grid">
          <Field label="Материал действует с (местное время)">
            <input
              type="datetime-local"
              value={effectiveFrom}
              onChange={(e) => setEffectiveFrom(e.target.value)}
            />
          </Field>
          <Field label="Материал действует до (необязательно)">
            <input
              type="datetime-local"
              min={effectiveFrom || undefined}
              value={effectiveTo}
              onChange={(e) => setEffectiveTo(e.target.value)}
            />
          </Field>
        </div>
        <fieldset disabled={!!createdDoc}>
          <KnowledgeScopeFields value={scope} onChange={setScope} />
        </fieldset>
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
          В тексте нет персональных ответов анкет и закрытых сведений, не
          предназначенных выбранной аудитории
        </label>
        <Field label="Основание создания">
          <textarea
            required
            minLength={3}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </Field>
        <div className="modal-actions">
          <button className="button secondary" type="button" onClick={onClose}>
            Отмена
          </button>
          <button
            className="button primary"
            disabled={
              busy ||
              !anonymous ||
              (kind === "APPROVED_INSTRUCTION" && !dependencies.length) ||
              !scope.employment_types.length ||
              !scope.employment_stages.length
            }
          >
            {busy ? "Сохраняем…" : "Создать и открыть проверку"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
export function DocumentAdministration({
  id,
  onDone,
}: {
  id: string;
  onDone: () => void;
}) {
  const actor = useActor();
  const directory = useResource("/directory");
  const state = useResource(`/knowledge/documents/${id}`),
    [ownerId, setOwnerId] = useState(""),
    [title, setTitle] = useState(""),
    [description, setDescription] = useState(""),
    [category, setCategory] = useState("OTHER"),
    [scope, setScope] = useState<any>({}),
    [confirmed, setConfirmed] = useState(false),
    [reason, setReason] = useState(""),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    if (state.data) {
      setOwnerId(state.data.owner_id || "");
      setTitle(state.data.title || "");
      setDescription(state.data.description || "");
      setCategory(state.data.document_category || "OTHER");
      setScope(state.data.scope || {});
      setConfirmed(false);
    }
  }, [state.data]);
  return (
    <details className="advanced">
      <summary>Свойства, доступ и ответственный HR</summary>
      <ErrorBox error={error || state.error} />
      {success && <Success>{success}</Success>}
      {state.loading ? (
        <Loading />
      ) : (
        <form
          onSubmit={async (e) => {
            e.preventDefault();
            setBusy(true);
            setError("");
            setSuccess("");
            try {
              const patch: Record<string, unknown> = {};
              if (ownerId && ownerId !== state.data.owner_id)
                patch.owner_id = ownerId;
              if (title !== state.data.title) patch.title = title;
              if (description !== (state.data.description || ""))
                patch.description = description;
              if (category !== state.data.document_category)
                patch.document_category = category;
              if (JSON.stringify(scope) !== JSON.stringify(state.data.scope))
                patch.scope = scope;
              if (!Object.keys(patch).length) {
                setSuccess("Изменений нет.");
                return;
              }
              await api.patch(
                `/knowledge/documents/${id}`,
                { ...patch, reason: reason.trim() },
                state.etag || state.data.etag,
              );
              state.reload();
              onDone();
              setSuccess(
                "Свойства сохранены. Назначение ответственного не расширяет доступ к документу; изменения области или категории требуют новой проверки черновиков.",
              );
            } catch (e) {
              setError(errorText(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          <Field label="Название документа">
            <input
              required
              minLength={1}
              maxLength={500}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </Field>
          <Field label="Описание">
            <textarea
              maxLength={4000}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>
          <Field label="Категория">
            <select
              value={category}
              onChange={(e) => setCategory(e.target.value)}
            >
              <option value="REGULATION">Регламент</option>
              <option value="INSTRUCTION">Инструкция</option>
              <option value="OVERVIEW">Обзор</option>
              <option value="MAP">Карта / схема</option>
              <option value="OTHER">Другое</option>
            </select>
          </Field>
          {
            <Field
              label="Ответственный HR"
              hint="Выберите действующего HR с доступом ко всей аудитории документа. Сервер проверит область; назначение не выдаёт новые права."
            >
              <select
                value={ownerId}
                disabled={actor.role !== "HR_ADMIN"}
                onChange={(event) => setOwnerId(event.target.value)}
              >
                {!ownerId && <option value="">Ответственный не указан</option>}
                {ownerId &&
                  !items(directory.data?.users).some(
                    (user) =>
                      user.id === ownerId &&
                      user.enabled !== false &&
                      ["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"].includes(
                        user.role,
                      ),
                  ) && (
                    <option value={ownerId}>
                      {items(directory.data?.users).find(
                        (user) => user.id === ownerId,
                      )?.name ||
                        "Текущий ответственный вне видимого справочника"}{" "}
                      · прежнее назначение
                    </option>
                  )}
                {items(directory.data?.users)
                  .filter(
                    (user) =>
                      user.enabled !== false &&
                      ["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"].includes(
                        user.role,
                      ),
                  )
                  .map((user) => (
                    <option key={user.id} value={user.id}>
                      {user.name} ·{" "}
                      {user.role === "HR_ADMIN"
                        ? "HR · администратор"
                        : "HR · специалист"}
                    </option>
                  ))}
              </select>
            </Field>
          }
          <ErrorBox error={directory.error} />
          <KnowledgeScopeFields value={scope} onChange={setScope} />
          <Notice>
            Для действующего документа можно сузить аудиторию. Для расширения
            сначала архивируйте документ и проверьте новую ревизию. Изменение
            области или категории сбрасывает согласование черновиков.
          </Notice>
          <Field label="Основание изменения документа">
            <textarea
              required
              minLength={3}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              rows={2}
            />
          </Field>
          <label className="check-field">
            <input
              type="checkbox"
              required
              checked={confirmed}
              onChange={(e) => setConfirmed(e.target.checked)}
            />
            Проверено, что выбранная аудитория имеет право на весь оригинальный
            документ
          </label>
          <button
            className="button secondary"
            disabled={
              busy ||
              !confirmed ||
              reason.trim().length < 3 ||
              (JSON.stringify(scope) !== JSON.stringify(state.data.scope) &&
                (!(scope.employment_types || []).length ||
                  !(scope.employment_stages || []).length))
            }
          >
            Сохранить свойства документа
          </button>
        </form>
      )}
    </details>
  );
}
