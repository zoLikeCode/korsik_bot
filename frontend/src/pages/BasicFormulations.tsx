import { useEffect, useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { api, errorText } from "../api";
import { ErrorBox, Field, Loading, Success, useResource } from "../ui";

type Formulation = { id: string; label: string; query_text: string };

export default function BasicFormulations() {
  const state = useResource("/knowledge/basic-formulations");
  const [rows, setRows] = useState<Formulation[]>([]);
  const [etag, setEtag] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  useEffect(() => {
    if (state.data && !dirty) {
      setRows(state.data.items);
      setEtag(state.etag);
    }
  }, [state.data, state.etag]);
  const canEdit = state.data?.can_edit === true;
  function update(next: Formulation[]) {
    setRows(next);
    setDirty(true);
    setSuccess("");
  }
  if (state.loading) return <Loading />;
  return (
    <section className="panel basic-formulations">
      <div className="panel-heading">
        <h2>Основные формулировки</h2>
        {canEdit && (
          <button
            className="button secondary"
            disabled={busy || rows.length >= 8}
            onClick={() =>
              update([
                ...rows,
                {
                  id: `p_${crypto.randomUUID().replaceAll("-", "")}`,
                  label: "",
                  query_text: "",
                },
              ])
            }
          >
            <Plus size={16} /> Добавить формулировку
          </button>
        )}
      </div>
      <ErrorBox error={error || state.error} />
      {success && <Success>{success}</Success>}
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          if (!canEdit || busy) return;
          setBusy(true);
          setError("");
          setSuccess("");
          try {
            const result = await api.put(
              "/knowledge/basic-formulations",
              { items: rows },
              etag,
            );
            setRows(result.data.items);
            setEtag(result.etag);
            setDirty(false);
            setSuccess("Основные формулировки сохранены");
          } catch (reason) {
            setError(errorText(reason));
          } finally {
            setBusy(false);
          }
        }}
      >
        <fieldset disabled={!canEdit || busy}>
          {rows.map((row, index) => (
            <div className="formulation-row" key={row.id}>
              <Field label={`Кнопка ${index + 1}`}>
                <input
                  required
                  maxLength={60}
                  value={row.label}
                  onChange={(event) =>
                    update(
                      rows.map((item) =>
                        item.id === row.id
                          ? { ...item, label: event.target.value }
                          : item,
                      ),
                    )
                  }
                />
              </Field>
              <Field label={`Вопрос для поиска ${index + 1}`}>
                <textarea
                  required
                  maxLength={2000}
                  rows={2}
                  value={row.query_text}
                  onChange={(event) =>
                    update(
                      rows.map((item) =>
                        item.id === row.id
                          ? { ...item, query_text: event.target.value }
                          : item,
                      ),
                    )
                  }
                />
              </Field>
              {canEdit && (
                <button
                  type="button"
                  className="icon-button danger"
                  aria-label={`Удалить формулировку ${index + 1}`}
                  disabled={rows.length <= 1}
                  onClick={() =>
                    update(rows.filter((item) => item.id !== row.id))
                  }
                >
                  <Trash2 size={17} />
                </button>
              )}
            </div>
          ))}
        </fieldset>
        {canEdit && (
          <div className="modal-actions">
            <button
              className="button primary"
              disabled={busy || (!dirty && state.data?.configured)}
            >
              {busy ? "Сохраняем…" : "Сохранить"}
            </button>
          </div>
        )}
      </form>
    </section>
  );
}
