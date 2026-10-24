import { useEffect, useState } from "react";
import { Plus, Trash2, Clock3 } from "lucide-react";
import { useActor } from "../App";
import { api, errorText, items } from "../api";
import {
  Empty,
  ErrorBox,
  Field,
  Loading,
  Notice,
  Success,
  useResource,
} from "../ui";

export default function SlaPolicy() {
  const actor = useActor(),
    canEdit = actor.role === "HR_ADMIN" && actor.scope?.all === true;
  const state = useResource("/settings/sla-policy"),
    calendars = useResource("/work-calendars"),
    sites = useResource("/sites");
  const [rules, setRules] = useState<any[]>([]),
    [reason, setReason] = useState("Настройка срока первого ответа HR"),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [success, setSuccess] = useState("");
  useEffect(() => {
    if (state.data) setRules(state.data.queue_rules || []);
  }, [state.data]);
  const versions = items(calendars.data)
    .flatMap((c) =>
      (c.versions || []).map((v: any) => ({
        ...v,
        label: `${c.name} · версия ${v.version_no || v.version} · ${v.timezone}`,
        active: c.active_version_id === v.id,
      })),
    )
    .filter((v) => v.active);
  const change = (index: number, patch: any) =>
    setRules(rules.map((r, i) => (i === index ? { ...r, ...patch } : r)));
  async function save() {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await api.patch(
        "/settings/sla-policy",
        { queue_rules: rules, apply_to_existing_open_cases: false, reason },
        state.etag || state.data?.etag,
      );
      setSuccess("Новая версия SLA сохранена для будущих обращений");
      state.reload();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel">
      {!canEdit && (
        <Notice>
          Общая политика сроков доступна для чтения. Изменения согласует
          центральный HR.
        </Notice>
      )}
      <div className="panel-head">
        <h2>
          <Clock3 size={20} /> Срок первого ответа HR
        </h2>
        {canEdit && (
          <button
            className="button secondary compact"
            onClick={() =>
              setRules([
                ...rules,
                {
                  scope: { site_ids: [] },
                  calendar_version_id: "",
                  first_response_working_minutes: 60,
                },
              ])
            }
          >
            <Plus size={16} />
            Правило
          </button>
        )}
      </div>
      <ErrorBox error={state.error || calendars.error || error} />
      {success && <Success>{success}</Success>}
      {state.loading ? (
        <Loading />
      ) : !rules.length ? (
        <Empty title="Срок ответа пока не настроен">
          Добавьте правило, выберите рабочий календарь и длительность в рабочих
          минутах.
        </Empty>
      ) : (
        rules.map((r, index) => (
          <fieldset
            key={index}
            disabled={!canEdit || busy}
            className="sla-rule"
          >
            <div className="panel-head">
              <strong>Правило {index + 1}</strong>
              <button
                className="icon-button"
                aria-label={`Удалить правило ${index + 1}`}
                onClick={() => setRules(rules.filter((_, i) => i !== index))}
              >
                <Trash2 size={16} />
              </button>
            </div>
            <div className="form-grid">
              <Field label="Площадка">
                <select
                  value={r.scope?.site_ids?.[0] || ""}
                  onChange={(e) =>
                    change(index, {
                      scope: {
                        ...r.scope,
                        site_ids: e.target.value ? [e.target.value] : [],
                      },
                    })
                  }
                >
                  <option value="">Все площадки</option>
                  {items(sites.data).map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Рабочий календарь">
                <select
                  required
                  value={r.calendar_version_id || ""}
                  onChange={(e) =>
                    change(index, { calendar_version_id: e.target.value })
                  }
                >
                  <option value="">Выберите активный календарь</option>
                  {r.calendar_version_id &&
                    !versions.some((v) => v.id === r.calendar_version_id) && (
                      <option value={r.calendar_version_id}>
                        Ранее закреплённая версия календаря
                      </option>
                    )}
                  {versions.map((v) => (
                    <option key={v.id} value={v.id}>
                      {v.label}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Первый ответ, рабочих минут">
                <input
                  type="number"
                  min={1}
                  max={525600}
                  value={r.first_response_working_minutes}
                  onChange={(e) =>
                    change(index, {
                      first_response_working_minutes: Number(e.target.value),
                    })
                  }
                />
              </Field>
            </div>
          </fieldset>
        ))
      )}
      <Notice>
        Правила применяются по порядку до первого совпадения. Время считается
        только по явно заданным рабочим интервалам. Уже открытые обращения
        сохраняют свою версию SLA и календаря.
      </Notice>
      {canEdit && (
        <>
          <Field label="Причина изменения">
            <input
              minLength={3}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>
          <button
            className="button primary"
            disabled={
              busy ||
              reason.length < 3 ||
              rules.some(
                (r) =>
                  !r.calendar_version_id ||
                  r.first_response_working_minutes < 1,
              )
            }
            onClick={save}
          >
            {busy ? "Сохраняем…" : "Сохранить новую версию SLA"}
          </button>
        </>
      )}
    </section>
  );
}
