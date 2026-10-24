import { useState } from "react";
import { Download, Upload } from "lucide-react";
import { api, errorText, items } from "../api";
import { ErrorBox, Field, Modal, Notice, Success, Status } from "../ui";

export default function EmployeeImport({
  sites,
  onClose,
  onDone,
}: {
  sites: any[];
  onClose: () => void;
  onDone: () => void;
}) {
  const [text, setText] = useState(""),
    [filename, setFilename] = useState(""),
    [mode, setMode] = useState("CREATE"),
    [partial, setPartial] = useState(false),
    [preview, setPreview] = useState<any>(null),
    [result, setResult] = useState<any>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  function template() {
    const blob = new Blob(
      [
        "\uFEFFexternal_employee_id,name,site_id,start_date,probation_end_date,employment_type,timezone,assigned_hr_id,manager_id,email\n",
      ],
      { type: "text/csv;charset=utf-8" },
    );
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "employees-template.csv";
    a.click();
    URL.revokeObjectURL(url);
  }
  async function run(apply = false) {
    setBusy(true);
    setError("");
    try {
      const response = await api.post("/employees/imports", {
        csv_text: text,
        mode,
        validate_only: !apply,
        allow_partial: partial,
      });
      if (apply) {
        setResult(response.data);
        setPreview(null);
        onDone();
      } else setPreview(response.data);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal title="Импорт сотрудников из CSV" onClose={onClose} wide>
      <div className="button-row wrap">
        <label className="button secondary">
          <Upload size={16} />
          Выбрать CSV
          <input
            type="file"
            accept=".csv,text/csv"
            hidden
            onChange={async (e) => {
              const file = e.target.files?.[0];
              if (!file) return;
              if (file.size > 5 * 1024 * 1024) {
                setError("CSV должен быть не больше 5 МБ");
                return;
              }
              setFilename(file.name);
              setText(await file.text());
              setPreview(null);
              setResult(null);
            }}
          />
        </label>
        <button className="button secondary" onClick={template}>
          <Download size={16} />
          Скачать шаблон
        </button>
        <span className="muted">
          {filename || "UTF-8 · до 1000 строк · разделитель — запятая"}
        </span>
      </div>
      <Field label="Режим импорта">
        <select
          value={mode}
          onChange={(e) => {
            setMode(e.target.value);
            setPreview(null);
          }}
        >
          <option value="CREATE">Создать новые карточки</option>
          <option value="UPDATE">Обновить по внешнему ID</option>
          <option value="UPSERT">Создать новые и обновить существующие</option>
        </select>
      </Field>
      <Field label="Содержимое CSV">
        <textarea
          rows={7}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
            setPreview(null);
            setResult(null);
          }}
          placeholder="external_employee_id,name,site_id,start_date,employment_type,timezone"
        />
      </Field>
      <Notice>
        Обязательны name, site_id, start_date (ГГГГ-ММ-ДД). Тип сотрудника —
        OFFICE или PRODUCTION. Для обновления нужен external_employee_id. Импорт
        проверяет доступ к площадкам и назначениям; приглашения в MAX
        отправляются отдельно из карточек.
      </Notice>
      <details>
        <summary>Идентификаторы доступных площадок</summary>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Площадка</th>
                <th>site_id</th>
              </tr>
            </thead>
            <tbody>
              {sites.map((s) => (
                <tr key={s.id}>
                  <td>{s.name}</td>
                  <td>
                    <code>{s.id}</code>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
      <ErrorBox error={error} />
      {result && (
        <Success>
          Создано: {result.created}. Обновлено: {result.updated}. Приглашения не
          отправлены автоматически.
        </Success>
      )}
      {preview && (
        <>
          <div className="panel-head">
            <strong>
              {preview.valid
                ? "Все строки прошли проверку"
                : "Есть строки с ошибками"}
            </strong>
            <span className="muted">Проверено: {items(preview).length}</span>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Строка</th>
                  <th>Сотрудник</th>
                  <th>Действие</th>
                  <th>Проверка</th>
                </tr>
              </thead>
              <tbody>
                {items(preview).map((r) => (
                  <tr key={r.row}>
                    <td>{r.row}</td>
                    <td>{r.data?.name || "—"}</td>
                    <td>
                      {r.operation === "UPDATE" ? "Обновление" : "Создание"}
                    </td>
                    <td>
                      {r.valid ? (
                        <Status value="PASSED" />
                      ) : (
                        <span className="error-text">
                          {r.errors.join("; ")}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!preview.valid && (
            <label className="check-field">
              <input
                type="checkbox"
                checked={partial}
                onChange={(e) => setPartial(e.target.checked)}
              />
              Импортировать только проверенные строки; остальные пропустить
            </label>
          )}
        </>
      )}
      <div className="modal-actions">
        <button className="button secondary" onClick={onClose}>
          Закрыть
        </button>
        <button
          className="button secondary"
          disabled={busy || !text.trim()}
          onClick={() => run()}
        >
          {busy ? "Проверяем…" : "Проверить файл"}
        </button>
        {preview && (
          <button
            className="button primary"
            disabled={
              busy ||
              !items(preview).some((r) => r.valid) ||
              (!preview.valid && !partial)
            }
            onClick={() => run(true)}
          >
            Применить импорт
          </button>
        )}
      </div>
    </Modal>
  );
}
