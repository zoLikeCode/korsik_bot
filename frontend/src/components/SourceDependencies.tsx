import { blockLabels } from "../ui";
import { useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { api, errorText, items } from "../api";
import {
  Badge,
  Empty,
  ErrorBox,
  Field,
  Loading,
  Notice,
  useResource,
} from "../ui";

export async function pinSourceDependency(
  revisionId: string,
  extraction: any,
  blocks: any[],
  rationale: string,
) {
  const source_block_sha256: Record<string, string> = {},
    source_block_image_sha256: Record<string, string> = {};
  for (const block of blocks) {
    const buffer = await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(block.text || ""),
    );
    source_block_sha256[block.id] = Array.from(new Uint8Array(buffer))
      .map((v) => v.toString(16).padStart(2, "0"))
      .join("");
    if (block.kind === "FIGURE" && block.image_sha256)
      source_block_image_sha256[block.id] = block.image_sha256;
  }
  return {
    source_revision_id: revisionId,
    source_extraction_revision_id:
      extraction.extraction_revision_id || extraction.id,
    source_block_ids: blocks.map((b) => b.id),
    source_block_sha256,
    source_block_image_sha256,
    rationale,
  };
}

export default function SourceDependencies({
  value,
  onChange,
  allowDraft = false,
}: {
  value: any[];
  onChange: (v: any[]) => void;
  allowDraft?: boolean;
}) {
  const documents = useResource("/knowledge/documents");
  const [documentId, setDocumentId] = useState(""),
    [useDraft, setUseDraft] = useState(false),
    [selected, setSelected] = useState<string[]>([]),
    [rationale, setRationale] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const doc = items(documents.data).find((d) => d.id === documentId),
    revisionId =
      allowDraft && useDraft
        ? doc?.latest_revision_id
        : doc?.active_revision_id ||
          (allowDraft ? doc?.latest_revision_id : null);
  const extraction = useResource(
    revisionId ? `/knowledge/revisions/${revisionId}/extraction` : null,
  );
  const blocks = items(extraction.data?.blocks);
  async function add() {
    setBusy(true);
    setError("");
    try {
      const pin = await pinSourceDependency(
        revisionId,
        extraction.data,
        blocks.filter((b) => selected.includes(b.id)),
        rationale,
      );
      onChange([...value, pin]);
      setSelected([]);
      setRationale("");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="source-dependencies">
      <h3>На какие документы опирается ответ</h3>
      <ErrorBox error={documents.error || extraction.error || error} />
      {value.map((d, index) => (
        <div className="panel-head" key={`${d.source_revision_id}-${index}`}>
          <div>
            <strong>
              Источник {index + 1} · {d.source_block_ids.length} фрагментов
            </strong>
            <p className="muted">{d.rationale}</p>
          </div>
          <button
            type="button"
            className="icon-button"
            aria-label={`Убрать источник ${index + 1}`}
            onClick={() => onChange(value.filter((_, i) => i !== index))}
          >
            <Trash2 size={16} />
          </button>
        </div>
      ))}
      <Field
        label={
          allowDraft
            ? "Документ-основание"
            : "Опубликованный документ-основание"
        }
      >
        <select
          value={documentId}
          onChange={(e) => {
            setDocumentId(e.target.value);
            setSelected([]);
            setUseDraft(false);
          }}
        >
          <option value="">Выберите источник</option>
          {items(documents.data)
            .filter(
              (d) =>
                (d.active_revision_id && d.status === "ACTIVE") ||
                (allowDraft &&
                  d.latest_revision_id &&
                  !["PURGED", "PURGE_PENDING"].includes(d.status)),
            )
            .map((d) => (
              <option key={d.id} value={d.id}>
                {d.title}
                {!d.active_revision_id ? " · черновик" : ""}
              </option>
            ))}
        </select>
      </Field>
      {allowDraft &&
        doc?.active_revision_id &&
        doc?.latest_revision_id &&
        doc.latest_revision_id !== doc.active_revision_id && (
          <label className="check-field">
            <input
              type="checkbox"
              checked={useDraft}
              onChange={(e) => {
                setUseDraft(e.target.checked);
                setSelected([]);
              }}
            />
            Использовать новую черновую версию этого источника
          </label>
        )}
      {allowDraft && revisionId && revisionId !== doc?.active_revision_id && (
        <Notice>
          Выбрана непубликованная версия. Включите этот источник и статью в
          совместный кандидат, проверьте и опубликуйте их вместе.
        </Notice>
      )}
      {!items(documents.data).some(
        (d) => d.active_revision_id || (allowDraft && d.latest_revision_id),
      ) &&
        !documents.loading && (
          <Empty title="Сначала опубликуйте источник">
            Для инструкции нужны проверяемые основания. Загрузите и согласуйте
            регламент в базе знаний, затем вернитесь к этому вопросу.
          </Empty>
        )}
      {revisionId &&
        (extraction.loading ? (
          <Loading />
        ) : (
          <>
            <div className="source-block-picker">
              {blocks.map((b) => (
                <label className="source-block-option" key={b.id}>
                  <input
                    type="checkbox"
                    checked={selected.includes(b.id)}
                    onChange={(e) =>
                      setSelected(
                        e.target.checked
                          ? [...selected, b.id]
                          : selected.filter((id) => id !== b.id),
                      )
                    }
                  />
                  <div>
                    <Badge>
                      Страница {b.page_no || 1} ·{" "}
                      {blockLabels[b.kind] || "Фрагмент"}
                    </Badge>
                    <p>
                      {b.description || b.text || "Изображение без описания"}
                    </p>
                  </div>
                </label>
              ))}
            </div>
            <Field label="Почему эти фрагменты подтверждают ответ">
              <input
                value={rationale}
                onChange={(e) => setRationale(e.target.value)}
                minLength={3}
              />
            </Field>
            <button
              type="button"
              className="button secondary compact"
              disabled={busy || !selected.length || rationale.trim().length < 3}
              onClick={add}
            >
              <Plus size={15} />
              Добавить выбранные основания
            </button>
          </>
        ))}
      <Notice>
        Сохраняются точные версии источников, фрагменты и их контрольные суммы.
        Обновление основания потребует повторной проверки инструкции.
      </Notice>
    </section>
  );
}
