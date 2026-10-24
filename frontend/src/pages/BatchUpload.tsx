import { useEffect, useState } from "react";
import { Upload } from "lucide-react";
import { useActor } from "../App";
import { api, errorText, explainIssue } from "../api";
import { KnowledgeScopeFields } from "./KnowledgeAuthoring";
import { ErrorBox, Field, Modal, Notice, Status, useResource } from "../ui";
type BatchRow = {
  id: string;
  file: File;
  status: string;
  document_id?: string;
  operation_id?: string;
  error?: string;
};
export default function BatchUpload({
  onClose,
  onChange,
  onOpen,
}: {
  onClose: () => void;
  onChange: () => void;
  onOpen: (id: string) => void;
}) {
  const actor = useActor(),
    [rows, setRows] = useState<BatchRow[]>([]),
    [scope, setScope] = useState<any>({
      site_ids: actor.scope?.site_ids || [],
      ...(actor.scope?.legal_entity_ids
        ? { legal_entity_ids: actor.scope.legal_entity_ids }
        : {}),
      employment_types: ["OFFICE", "PRODUCTION", "OTHER"],
      employment_stages: ["ACTIVE"],
    }),
    [category, setCategory] = useState("REGULATION"),
    [reason, setReason] = useState("Пакетная загрузка документов на проверку"),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  function update(id: string, patch: Partial<BatchRow>) {
    setRows((current) =>
      current.map((r) => (r.id === id ? { ...r, ...patch } : r)),
    );
  }
  async function upload(row: BatchRow) {
    if (!/\.(pdf|pptx|txt|md|png|jpe?g)$/i.test(row.file.name)) {
      update(row.id, {
        status: "FAILED",
        error:
          "Формат не поддерживается. Выберите PDF, PPTX, TXT, MD, PNG или JPG.",
      });
      return;
    }
    if (row.file.size > 50 * 1024 * 1024) {
      update(row.id, {
        status: "FAILED",
        error: "Размер файла превышает 50 МБ.",
      });
      return;
    }
    update(row.id, { status: "UPLOADING", error: "" });
    try {
      let id = row.document_id;
      if (!id) {
        const doc = await api.post("/knowledge/documents", {
          title: row.file.name.replace(/\.[^.]+$/, ""),
          scope,
          document_category: category,
        });
        id = doc.data.id;
        update(row.id, { document_id: id });
      }
      const current = await api.get(`/knowledge/documents/${id}`);
      const file = new FormData();
      file.append("file", row.file);
      file.append("reason", reason);
      const result = await api.upload(
        `/knowledge/documents/${id}/revisions`,
        file,
        current.etag || current.data.etag,
      );
      update(row.id, {
        status: "UPLOADED",
        operation_id: result.data.operation?.id,
      });
      onChange();
    } catch (e) {
      update(row.id, { status: "FAILED", error: errorText(e) });
    }
  }
  async function submit() {
    setBusy(true);
    setError("");
    try {
      for (const row of rows.filter((r) =>
        ["PENDING", "FAILED"].includes(r.status),
      ))
        await upload(row);
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="Пакетная загрузка документов" onClose={onClose} wide>
      <ErrorBox error={error} />
      <Notice>
        Каждый файл получает собственную карточку и отдельный результат
        обработки. Ошибка одного файла не отменяет остальные. Публикации в этом
        окне нет: затем HR проверяет каждый документ.
      </Notice>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        <Field label="Файлы для загрузки — до 20 за один раз">
          <input
            type="file"
            multiple
            accept=".pdf,.pptx,.txt,.md,.png,.jpg,.jpeg"
            disabled={busy || rows.some((r) => r.document_id)}
            onChange={(e) => {
              const chosen = Array.from(e.target.files || []);
              if (chosen.length > 20) {
                setError("Выберите не больше 20 файлов. Пакет не запущен.");
                return;
              }
              setError("");
              setRows(
                chosen.map((file) => ({
                  id: crypto.randomUUID(),
                  file,
                  status: "PENDING",
                })),
              );
            }}
          />
        </Field>
        <fieldset disabled={busy || rows.some((r) => r.document_id)}>
          <KnowledgeScopeFields value={scope} onChange={setScope} />
          <Field label="Общая категория документов">
            <select
              value={category}
              onChange={(e) => setCategory(e.target.value)}
            >
              <option value="REGULATION">Регламенты</option>
              <option value="INSTRUCTION">Инструкции</option>
              <option value="OVERVIEW">Обзорные материалы</option>
              <option value="MAP">Карты и схемы</option>
              <option value="OTHER">Другое</option>
            </select>
          </Field>
        </fieldset>
        <Field label="Основание загрузки">
          <input
            required
            minLength={3}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
          />
        </Field>
        <button
          className="button primary"
          disabled={
            busy ||
            !rows.some((r) => ["PENDING", "FAILED"].includes(r.status)) ||
            !scope.employment_types.length ||
            !scope.employment_stages.length
          }
        >
          <Upload size={16} />
          {busy ? "Загружаем файлы…" : "Загрузить / повторить неудачные"}
        </button>
      </form>
      {rows.length > 0 && (
        <section className="panel inset">
          <h3>Результат каждого файла</h3>
          {rows.map((row) => (
            <BatchResult
              key={row.id}
              row={row}
              onOpen={onOpen}
              onReplace={(file) =>
                update(row.id, {
                  file,
                  status: "PENDING",
                  error: "",
                  operation_id: undefined,
                })
              }
            />
          ))}
          <p className="muted">
            Получено файлов:{" "}
            {rows.filter((r) => r.status === "UPLOADED").length} из{" "}
            {rows.length}. Карточки и серверные операции сохранятся после
            закрытия вкладки.
          </p>
        </section>
      )}
    </Modal>
  );
}
function BatchResult({
  row,
  onOpen,
  onReplace,
}: {
  row: BatchRow;
  onOpen: (id: string) => void;
  onReplace: (file: File) => void;
}) {
  const operation = useResource(
      row.operation_id ? `/operations/${row.operation_id}` : null,
    ),
    pending =
      !!row.operation_id &&
      (!operation.data ||
        ["PENDING", "RUNNING", "QUEUED"].includes(operation.data.status));
  useEffect(() => {
    if (pending) {
      const timer = setInterval(operation.reload, 3000);
      return () => clearInterval(timer);
    }
  }, [pending, row.operation_id]);
  return (
    <div className="list-row">
      <div>
        <strong>{row.file.name}</strong>
        <p>{(row.file.size / 1024 / 1024).toFixed(1)} МБ</p>
        <ErrorBox
          error={
            row.error ||
            operation.error ||
            (operation.data?.error?.code
              ? explainIssue(operation.data.error.code)
              : operation.data?.error)
          }
        />
      </div>
      {row.status === "FAILED" && (
        <Field label={`Заменить неудачный файл ${row.file.name}`}>
          <input
            type="file"
            accept=".pdf,.pptx,.txt,.md,.png,.jpg,.jpeg"
            onChange={(e) => {
              const file = e.target.files?.[0];
              if (file) onReplace(file);
            }}
          />
        </Field>
      )}
      <Status value={operation.data?.status || row.status} />
      {row.document_id && (
        <button
          className="text-button"
          onClick={() => onOpen(row.document_id!)}
        >
          Открыть карточку
        </button>
      )}
    </div>
  );
}
