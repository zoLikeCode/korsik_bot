import { useEffect, useRef, useState } from "react";
import {
  Upload,
  Download,
  FileText,
  CheckCheck,
  ChevronLeft,
  ChevronRight,
  RefreshCcw,
} from "lucide-react";
import { useActor } from "../App";
import { api, ApiError, items, errorText, explainIssue } from "../api";
import {
  Modal,
  Field,
  ErrorBox,
  Success,
  Status,
  Loading,
  Empty,
  Badge,
  blockLabels,
  statusLabels,
  useResource,
} from "../ui";
import "./KnowledgeSimple.css";

const supported = /\.(pdf|pptx|txt|md|png|jpe?g)$/i;
export function documentFileError(
  files: ArrayLike<Pick<File, "name" | "size">>,
): string {
  if (files.length !== 1)
    return "Выберите один файл. Для следующего документа используйте отдельную загрузку.";
  if (!supported.test(files[0].name))
    return "Поддерживаются PDF, PPTX, TXT, MD, PNG и JPG.";
  if (files[0].size > 50 * 1024 * 1024)
    return "Файл должен быть не больше 50 МБ";
  if (files[0].size === 0) return "Файл пустой. Выберите другой файл.";
  return "";
}
export async function uploadDocumentRevision(
  documentId: string,
  file: File,
): Promise<string> {
  const data = new FormData();
  data.append("file", file);
  data.append("simple_workflow", "true");
  const latest = await api.get(`/knowledge/documents/${documentId}`);
  const result = await api.upload(
    `/knowledge/documents/${documentId}/revisions`,
    data,
    latest.etag,
  );
  const revisionId = result.data?.revision?.id;
  if (typeof revisionId !== "string" || !revisionId)
    throw new Error(
      "Сервер не вернул версию документа. Обновите список документов.",
    );
  if (
    result.data.duplicate &&
    ["REVIEW_REQUIRED", "FAILED"].includes(result.data.revision.status)
  ) {
    const revision = await api.get(`/knowledge/revisions/${revisionId}`);
    if (!revision.etag) throw new Error("Дождитесь загрузки версии документа");
    await api.post(
      `/knowledge/revisions/${revisionId}/simple-prepare`,
      {},
      revision.etag,
    );
  }
  return revisionId;
}

export function revisionSelection(
  selected: string,
  revisions: any[],
  pending: string,
) {
  if (pending)
    return {
      selected: pending,
      pending: revisions.some((row) => row.id === pending) ? "" : pending,
    };
  if (!revisions.length || revisions.some((row) => row.id === selected))
    return { selected, pending: "" };
  return { selected: revisions[0].id as string, pending: "" };
}
export type ExtractionEditSnapshot = {
  block: any;
  extractionId: string;
  etag: string;
};
export function extractionEditSnapshot(
  block: any,
  extractionId: string,
  etag: string | null,
): ExtractionEditSnapshot {
  if (!extractionId || !etag)
    throw new Error("Дождитесь загрузки распознанного содержимого");
  return { block: structuredClone(block), extractionId, etag };
}
export function extractionEditRequest(
  snapshot: ExtractionEditSnapshot,
  text: string,
) {
  return {
    blockId: snapshot.block.block_id || snapshot.block.id,
    etag: snapshot.etag,
    body: {
      ...(snapshot.block.kind === "TABLE"
        ? {
            patch_type: "TABLE",
            grid: text.split("\n").map((row) => row.split("\t")),
          }
        : snapshot.block.kind === "FIGURE"
          ? { patch_type: "FIGURE_DESCRIPTION", description: text }
          : { patch_type: "TEXT", text }),
      reason: "Распознавание исправлено HR по оригиналу",
      expected_extraction_revision_id: snapshot.extractionId,
    },
  };
}
export function extractionResourceError(error: string, processing: boolean) {
  if (error !== "extraction_not_ready") return error;
  return processing
    ? ""
    : "Распознанное содержимое ещё не готово. Обновите статус документа.";
}
export function RecognizedContentStatus({
  revision,
  page,
}: {
  revision: any;
  page: any;
}) {
  if (
    revision?.status === "PUBLISHED" &&
    revision.simple_approval?.method === "EXPLICIT_WHOLE_REVISION_APPROVAL"
  )
    return <Badge tone="green">Версия подтверждена</Badge>;
  const status = page?.review_status || page?.status || "REVIEW_REQUIRED";
  return <Status value={status === "PENDING" ? "REVIEW_REQUIRED" : status} />;
}
export function isRevisionLocked(status: string) {
  return [
    "PUBLISHED",
    "ARCHIVED",
    "PURGED",
    "PURGE_PENDING",
    "REJECTED",
    "CANCELLED",
  ].includes(status);
}
export function isExtractionConflict(error: unknown) {
  return (
    error instanceof ApiError &&
    ([412, 414].includes(error.status) ||
      (error.status === 409 &&
        typeof error.detail === "string" &&
        error.detail.startsWith("stale_")))
  );
}
export function extractionPagePath(base: string, page: number) {
  return `${base}/extraction?page_from=${Math.max(1, Math.trunc(page) || 1)}&limit=2000`;
}
function extractionEditText(block: any) {
  return block.kind === "TABLE"
    ? (block.grid || block.cells || [])
        .map((row: any[]) =>
          row
            .map((cell) => (typeof cell === "object" ? cell?.text || "" : cell))
            .join("\t"),
        )
        .join("\n")
    : block.description || block.text || block.content || "";
}
export function DocumentAudience({
  units,
  all,
  onAll,
  selected,
  onSelected,
  disabled = false,
}: {
  units: any[];
  all: boolean;
  onAll: (value: boolean) => void;
  selected: string[];
  onSelected: (ids: string[]) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="knowledge-audience">
      <label className="check-field">
        <input
          type="checkbox"
          checked={all}
          disabled={disabled}
          onChange={(e) => {
            onAll(e.target.checked);
            setOpen(false);
          }}
        />
        Доступен всем
      </label>
      <button
        type="button"
        className="knowledge-audience-trigger"
        disabled={all || disabled}
        aria-expanded={!all && open}
        aria-controls="knowledge-audience-options"
        onClick={() => setOpen(!open)}
      >
        {selected.length
          ? `Выбрано подразделений: ${selected.length}`
          : "Выберите подразделения"}
        <span aria-hidden="true">⌄</span>
      </button>
      {!all && open && (
        <div
          className="knowledge-audience-options"
          id="knowledge-audience-options"
        >
          {units.length ? (
            units.map((unit) => (
              <label className="check-field" key={unit.id}>
                <input
                  type="checkbox"
                  checked={selected.includes(unit.id)}
                  disabled={disabled}
                  onChange={(e) =>
                    onSelected(
                      e.target.checked
                        ? [...selected, unit.id]
                        : selected.filter((id) => id !== unit.id),
                    )
                  }
                />
                {unit.title || unit.name}
              </label>
            ))
          ) : (
            <p className="muted">Нет доступных подразделений</p>
          )}
        </div>
      )}
    </div>
  );
}
export function UploadDocument({
  onClose,
  onDone,
  existing,
}: {
  onClose: () => void;
  onDone: (doc: any) => void;
  existing?: any;
}) {
  const organization = useResource(existing ? null : "/organization"),
    input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null),
    [title, setTitle] = useState(existing?.title || ""),
    [category, setCategory] = useState("REGULATION"),
    [all, setAll] = useState(true),
    [selected, setSelected] = useState<string[]>([]),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [created, setCreated] = useState<any>(null);
  function choose(files: FileList | null) {
    const issue = documentFileError(files || []);
    setError(issue);
    if (issue) {
      setFile(null);
      if (input.current) input.current.value = "";
      return;
    }
    const value = files![0];
    setFile(value);
    if (!title.trim()) setTitle(value.name.replace(/\.[^.]+$/, ""));
  }
  async function submit() {
    const issue = documentFileError(file ? [file] : []);
    if (issue || !title.trim() || (!existing && !all && !selected.length)) {
      setError(
        issue ||
          (!title.trim()
            ? "Укажите название документа"
            : "Выберите хотя бы одно подразделение"),
      );
      return;
    }
    setBusy(true);
    setError("");
    try {
      let doc = existing || created;
      if (!doc) {
        doc = (
          await api.post("/knowledge/documents", {
            title: title.trim(),
            document_category: category,
            audience_all: all,
            scope: all ? {} : { org_unit_ids: selected },
          })
        ).data;
        setCreated(doc);
      }
      const revisionId = await uploadDocumentRevision(doc.id, file!);
      onDone({ ...doc, selected_revision_id: revisionId });
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal
      title={existing ? "Обновить документ" : "Загрузить документ"}
      onClose={onClose}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <div
          className="dropzone"
          role="button"
          tabIndex={busy ? -1 : 0}
          aria-label="Выбрать один файл"
          aria-disabled={busy}
          onClick={() => !busy && input.current?.click()}
          onKeyDown={(e) => {
            if (!busy && ["Enter", " "].includes(e.key)) {
              e.preventDefault();
              input.current?.click();
            }
          }}
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            if (!busy) choose(e.dataTransfer.files);
          }}
        >
          <Upload size={30} />
          <strong>{file?.name || "Перетащите один файл сюда"}</strong>
          <span>
            {file
              ? `${(file.size / 1024 / 1024).toFixed(1)} МБ`
              : "или нажмите, чтобы выбрать · до 50 МБ"}
          </span>
          <small>PDF, PPTX, TXT, MD, PNG, JPG</small>
        </div>
        <input
          ref={input}
          type="file"
          hidden
          disabled={busy}
          accept=".pdf,.pptx,.txt,.md,.png,.jpg,.jpeg"
          onChange={(e) => choose(e.target.files)}
        />
        <Field label="Название документа">
          <input
            required
            value={title}
            disabled={busy || !!existing || !!created}
            maxLength={200}
            onChange={(e) => setTitle(e.target.value)}
          />
        </Field>
        {!existing && (
          <>
            <Field label="Категория">
              <select
                value={category}
                disabled={busy || !!created}
                onChange={(e) => setCategory(e.target.value)}
              >
                <option value="REGULATION">Регламент</option>
                <option value="INSTRUCTION">Памятка / инструкция</option>
                <option value="OVERVIEW">Презентация</option>
                <option value="OTHER">Другое</option>
              </select>
            </Field>
            <div className="field">
              <span>Кому доступен документ</span>
              <DocumentAudience
                units={(organization.data?.units || []).filter(
                  (u: any) => u.status !== "ARCHIVED",
                )}
                all={all}
                onAll={setAll}
                selected={selected}
                onSelected={setSelected}
                disabled={busy || !!created}
              />
            </div>
            {!all && <ErrorBox error={organization.error} />}
          </>
        )}
        <ErrorBox error={error} />
        <div className="modal-actions">
          <button
            type="button"
            className="button secondary"
            disabled={busy}
            onClick={onClose}
          >
            Отмена
          </button>
          <button className="button primary" disabled={busy || !file}>
            {busy
              ? "Загружаем…"
              : existing
                ? "Обновить документ"
                : "Загрузить документ"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
export function DocumentDetail({
  doc,
  onClose,
  onChange,
}: {
  doc: any;
  onClose: () => void;
  onChange: () => void;
}) {
  const editable = ["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"].includes(
    useActor().role,
  );
  const revisions = useResource(`/knowledge/documents/${doc.id}/revisions`),
    [selected, setSelected] = useState(doc.selected_revision_id || ""),
    [pendingRevision, setPendingRevision] = useState(
      doc.selected_revision_id || "",
    ),
    [replace, setReplace] = useState(false),
    list = items(revisions.data);
  useEffect(() => {
    const next = revisionSelection(selected, list, pendingRevision);
    if (next.selected !== selected) setSelected(next.selected);
    if (next.pending !== pendingRevision) setPendingRevision(next.pending);
  }, [revisions.data, selected, pendingRevision]);
  return (
    <Modal title={doc.title} onClose={onClose} wide>
      <div className="version-bar knowledge-simple-version">
        <Field label="Версия документа">
          <select
            aria-label="Версия документа"
            value={selected}
            onChange={(e) => {
              setPendingRevision("");
              setSelected(e.target.value);
            }}
          >
            {pendingRevision &&
              !list.some((row) => row.id === pendingRevision) && (
                <option value={pendingRevision}>Загруженная версия</option>
              )}
            {list.map((row, i) => (
              <option key={row.id} value={row.id}>
                {row.filename ||
                  row.file_name ||
                  `Версия ${row.revision_no || i + 1}`}{" "}
                · {statusLabels[row.status] || row.status}
              </option>
            ))}
          </select>
        </Field>
        {editable && (
          <button className="button secondary" onClick={() => setReplace(true)}>
            <Upload size={16} />
            Обновить документ
          </button>
        )}
      </div>
      <ErrorBox error={revisions.error} />
      {selected ? (
        <SimpleRevisionReview
          key={selected}
          id={selected}
          onChange={() => {
            revisions.reload();
            onChange();
          }}
        />
      ) : revisions.loading ? (
        <Loading />
      ) : (
        <Empty title="У документа ещё нет файла" />
      )}
      {replace && (
        <UploadDocument
          existing={doc}
          onClose={() => setReplace(false)}
          onDone={(result) => {
            setReplace(false);
            setPendingRevision(result.selected_revision_id || "");
            setSelected(result.selected_revision_id || "");
            revisions.reload();
            onChange();
          }}
        />
      )}
    </Modal>
  );
}
export function publicationBlockers(value: any): string[] {
  return (value?.blockers || []).map((entry: any) =>
    typeof entry === "string"
      ? explainIssue(entry)
      : entry.message || explainIssue(entry.code || "Проверьте документ"),
  );
}
function SimpleRevisionReview({
  id,
  onChange,
}: {
  id: string;
  onChange: () => void;
}) {
  const actor = useActor(),
    editable = ["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"].includes(
      actor.role,
    ),
    base = `/knowledge/revisions/${id}`;
  const [page, setPage] = useState(1);
  const revision = useResource(base),
    extraction = useResource(extractionPagePath(base, page)),
    pages = useResource(`${base}/pages`),
    simple = useResource(`${base}/simple-status`);
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [editing, setEditing] = useState<ExtractionEditSnapshot | null>(null),
    [editConflict, setEditConflict] = useState(false),
    [text, setText] = useState(""),
    [ocr, setOcr] = useState(false),
    [rotation, setRotation] = useState(0),
    [forceOcr, setForceOcr] = useState(false);
  const r: any = revision.data || {},
    ext: any = extraction.data || {},
    state: any = simple.data || {},
    pageRows = items(pages.data),
    pageData = pageRows.find((row) => row.page_no === page) || {},
    blocks = items(ext.blocks).filter(
      (row) => (row.page_no || row.page || 1) === page,
    ),
    pageTruncated = blocks.length >= 2000,
    parseError = r.error || r.processing_error;
  const locked = isRevisionLocked(r.status),
    processing =
      (!revision.data && revision.loading) ||
      !!state.processing ||
      [
        "UPLOADED",
        "QUEUED",
        "PENDING",
        "PARSING",
        "PROCESSING",
        "INDEXING",
      ].includes(r.status);
  const reportChange = useRef(onChange);
  reportChange.current = onChange;
  const reportedStatus = useRef<string | null>(null);
  useEffect(() => {
    if (r.status && reportedStatus.current !== r.status) {
      reportedStatus.current = r.status;
      reportChange.current();
    }
  }, [r.status]);
  function refresh() {
    revision.reload();
    extraction.reload();
    pages.reload();
    simple.reload();
  }
  useEffect(() => {
    if (processing) {
      const timer = setInterval(refresh, 2500);
      return () => clearInterval(timer);
    }
  }, [processing, id]);
  useEffect(() => {
    setPage((current) => Math.min(current, Math.max(1, pageRows.length)));
  }, [pageRows.length]);
  async function mutate(
    path: string,
    body: any,
    patch = false,
    pinnedEtag?: string,
  ) {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const snapshotEtag =
        pinnedEtag ?? (patch ? extraction.etag : revision.etag);
      if (!snapshotEtag) throw new Error("Дождитесь загрузки документа");
      const result = patch
        ? await api.patch(path, body, snapshotEtag)
        : await api.post(path, body, snapshotEtag);
      refresh();
      onChange();
      return result;
    } catch (e) {
      setError(errorText(e));
      if (editing && patch && isExtractionConflict(e)) setEditConflict(true);
      refresh();
      return null;
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="knowledge-simple-review">
      <div className="review-header">
        <Status value={r.status || "PROCESSING"} />
        <span className="muted">{r.filename || r.file_name || "Документ"}</span>
        {r.revision_kind !== "ARTICLE" && (
          <a
            className="text-button"
            href={`/api/v1${base}/source`}
            target="_blank"
            rel="noreferrer"
          >
            <Download size={15} />
            Оригинал
          </a>
        )}
        <button
          className="icon-button"
          aria-label="Обновить статус документа"
          onClick={refresh}
        >
          <RefreshCcw size={16} />
        </button>
      </div>
      <ErrorBox
        error={
          error ||
          revision.error ||
          extractionResourceError(extraction.error, processing) ||
          extractionResourceError(pages.error, processing) ||
          simple.error
        }
      />
      {success && <Success>{success}</Success>}
      {processing && (
        <p className="muted knowledge-processing" role="status">
          {state.message || "Документ обрабатывается…"}
        </p>
      )}
      {parseError && (
        <ErrorBox
          error={
            typeof parseError === "string"
              ? parseError
              : parseError.message || "Не удалось обработать документ"
          }
        />
      )}
      <div className="review-grid">
        <section className="page-preview">
          <div className="page-controls">
            <button
              className="icon-button"
              disabled={page <= 1}
              aria-label="Предыдущая страница"
              onClick={() => setPage(page - 1)}
            >
              <ChevronLeft size={18} />
            </button>
            <span>
              Страница {page} из {pageRows.length || r.page_count || 1}
            </span>
            <button
              className="icon-button"
              disabled={page >= (pageRows.length || r.page_count || 1)}
              aria-label="Следующая страница"
              onClick={() => setPage(page + 1)}
            >
              <ChevronRight size={18} />
            </button>
          </div>
          {editable && !locked && pageRows.length > 0 && (
            <button
              className="text-button"
              disabled={busy || processing || extraction.loading}
              onClick={() => {
                setOcr(true);
                setRotation(0);
                setForceOcr(false);
              }}
            >
              Повторить распознавание страницы {page}
            </button>
          )}
          {pageData.preview_url ? (
            <img
              src={`${pageData.preview_url}${pageData.preview_url.includes("?") ? "&" : "?"}v=${encodeURIComponent(ext.extraction_revision_id || "current")}`}
              alt={`Оригинал страницы ${page}`}
            />
          ) : /\.pdf$/i.test(r.filename || r.file_name || "") ? (
            <iframe
              title={`Оригинал документа, страница ${page}`}
              src={`/api/v1${base}/source#page=${page}`}
            />
          ) : (
            <div className="source-file-placeholder">
              <FileText size={40} />
              <a
                className="button secondary"
                href={`/api/v1${base}/source`}
                target="_blank"
                rel="noreferrer"
              >
                Открыть оригинал
              </a>
            </div>
          )}
        </section>
        <section className="extracted-content">
          <div className="panel-head">
            <h3>Распознанное содержимое</h3>
            <RecognizedContentStatus revision={r} page={pageData} />
          </div>
          {pageTruncated && (
            <ErrorBox error="Показаны первые 2000 фрагментов страницы. Разделите документ на несколько файлов перед подтверждением проверки." />
          )}
          {extraction.loading ? (
            <Loading />
          ) : blocks.length ? (
            blocks.map((block, i) => (
              <article
                className="extraction-block"
                key={block.block_id || block.id || i}
              >
                <div className="block-meta">
                  <Badge>
                    {blockLabels[block.type || block.kind || "TEXT"] ||
                      "Фрагмент"}
                  </Badge>
                  {editable && !locked && (
                    <button
                      className="text-button"
                      disabled={busy || processing || extraction.loading}
                      onClick={() => {
                        try {
                          setEditing(
                            extractionEditSnapshot(
                              block,
                              ext.extraction_revision_id,
                              extraction.etag,
                            ),
                          );
                          setText(extractionEditText(block));
                          setEditConflict(false);
                          setError("");
                        } catch (error) {
                          setError(errorText(error));
                        }
                      }}
                    >
                      Исправить
                    </button>
                  )}
                </div>
                {block.kind === "FIGURE" && editable && !locked && (
                  <button
                    className="text-button"
                    disabled={busy || processing || extraction.loading}
                    onClick={() =>
                      mutate(
                        `${base}/extraction/blocks/${block.block_id || block.id}`,
                        {
                          patch_type: "DISPOSITION",
                          disposition: "REFERENCE_ONLY",
                          reason:
                            "Проверено HR: изображение оставлено для просмотра",
                          expected_extraction_revision_id:
                            ext.extraction_revision_id,
                        },
                        true,
                      )
                    }
                  >
                    Оставить только для просмотра
                  </button>
                )}
                <p className="preserve-lines">
                  {block.description ||
                    block.text ||
                    block.content ||
                    block.caption ||
                    "Нет текстового описания"}
                </p>
                {(block.grid || block.cells) && (
                  <div className="table-wrap">
                    <table>
                      <tbody>
                        {(block.grid || block.cells).map(
                          (row: any[], j: number) => (
                            <tr key={j}>
                              {(Array.isArray(row) ? row : [row]).map(
                                (cell: any, k: number) => (
                                  <td key={k}>
                                    {cell && typeof cell === "object"
                                      ? cell.text
                                      : String(cell ?? "")}
                                  </td>
                                ),
                              )}
                            </tr>
                          ),
                        )}
                      </tbody>
                    </table>
                  </div>
                )}
              </article>
            ))
          ) : (
            <Empty
              title={
                processing
                  ? "Распознавание страницы…"
                  : "На странице нет текста"
              }
            />
          )}
          {editable && !locked && (
            <button
              className="button secondary full"
              disabled={
                busy ||
                processing ||
                extraction.loading ||
                pageTruncated ||
                !ext.extraction_revision_id
              }
              onClick={() =>
                mutate(
                  `${base}/pages/${page}/review`,
                  {
                    action: "REVIEW_CONTENT",
                    reason: "Содержимое страницы проверено HR",
                    expected_extraction_revision_id: ext.extraction_revision_id,
                  },
                  true,
                )
              }
            >
              <CheckCheck size={17} />
              Содержимое страницы проверено
            </button>
          )}
        </section>
      </div>
      <div className="knowledge-simple-publication">
        {!locked && publicationBlockers(state).length > 0 && (
          <div className="knowledge-publication-blockers" role="status">
            {publicationBlockers(state).map((value, i) => (
              <p key={i}>{value}</p>
            ))}
          </div>
        )}
        {!locked && editable && state.requires_prepare && (
          <button
            className="button secondary"
            disabled={busy || processing || !revision.etag}
            onClick={() => mutate(`${base}/simple-prepare`, {})}
          >
            Подготовить документ
          </button>
        )}
        {!locked && (
          <button
            className="button primary"
            disabled={
              !editable ||
              busy ||
              processing ||
              !state.ready_to_publish ||
              !revision.etag ||
              actor.role !== "HR_ADMIN"
            }
            onClick={async () => {
              if (await mutate(`${base}/simple-publish`, {}))
                setSuccess("Документ опубликован");
            }}
          >
            Опубликовать
          </button>
        )}
      </div>
      {editing && (
        <Modal title="Исправить распознавание" onClose={() => setEditing(null)}>
          <Field
            label={
              editing.block.kind === "TABLE"
                ? "Ячейки таблицы: колонки через табуляцию, строки с новой строки"
                : editing.block.kind === "FIGURE"
                  ? "Описание изображения для поиска"
                  : "Точный текст из документа"
            }
          >
            <textarea
              rows={12}
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
          </Field>
          <ErrorBox error={error} />
          {editConflict && (
            <button
              className="button secondary"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  const latest = await api.get(
                    extractionPagePath(base, editing.block.page_no || page),
                  );
                  const blockId = editing.block.block_id || editing.block.id;
                  const block = items(latest.data.blocks).find(
                    (row) => (row.block_id || row.id) === blockId,
                  );
                  if (!block)
                    throw new Error(
                      "Фрагмент изменился или удалён. Закройте редактор и выберите актуальный фрагмент.",
                    );
                  setEditing(
                    extractionEditSnapshot(
                      block,
                      latest.data.extraction_revision_id,
                      latest.etag,
                    ),
                  );
                  setText(extractionEditText(block));
                  setEditConflict(false);
                  setError("");
                  refresh();
                } catch (error) {
                  setError(errorText(error));
                } finally {
                  setBusy(false);
                }
              }}
            >
              Загрузить актуальный текст и заменить мои правки
            </button>
          )}
          <div className="modal-actions">
            <button
              className="button secondary"
              disabled={busy}
              onClick={() => setEditing(null)}
            >
              Отмена
            </button>
            <button
              className="button primary"
              disabled={
                busy ||
                locked ||
                processing ||
                extraction.loading ||
                editConflict
              }
              onClick={async () => {
                const request = extractionEditRequest(editing, text);
                const result = await mutate(
                  `${base}/extraction/blocks/${request.blockId}`,
                  request.body,
                  true,
                  request.etag,
                );
                if (result) setEditing(null);
              }}
            >
              Сохранить исправление
            </button>
          </div>
        </Modal>
      )}
      {ocr && (
        <Modal
          title={`Повторное распознавание страницы ${page}`}
          onClose={() => setOcr(false)}
        >
          {/\.(pdf|png|jpe?g)$/i.test(r.filename || r.file_name || "") && (
            <>
              <Field label="Поворот страницы">
                <select
                  value={rotation}
                  onChange={(e) => setRotation(Number(e.target.value))}
                >
                  <option value={0}>Без поворота</option>
                  <option value={90}>90° по часовой стрелке</option>
                  <option value={180}>180°</option>
                  <option value={270}>270° по часовой стрелке</option>
                </select>
              </Field>
              <label className="check-field">
                <input
                  type="checkbox"
                  checked={forceOcr}
                  onChange={(e) => setForceOcr(e.target.checked)}
                />
                Распознать изображение, даже если есть текстовый слой
              </label>
            </>
          )}
          <ErrorBox error={error} />
          <div className="modal-actions">
            <button className="button secondary" onClick={() => setOcr(false)}>
              Отмена
            </button>
            <button
              className="button primary"
              disabled={busy || processing || extraction.loading}
              onClick={async () => {
                const result = await mutate(`${base}/retry`, {
                  page_numbers: [page],
                  ...(/\.(pdf|png|jpe?g)$/i.test(
                    r.filename || r.file_name || "",
                  )
                    ? {
                        page_options: {
                          [String(page)]: { rotation, force_ocr: forceOcr },
                        },
                      }
                    : {}),
                  reason: "Повторное распознавание страницы по запросу HR",
                });
                if (result) setOcr(false);
              }}
            >
              Распознать страницу
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
