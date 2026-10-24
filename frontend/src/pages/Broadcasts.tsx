import { useEffect, useRef, useState } from "react";
import { Plus, AtSign, Send } from "lucide-react";
import { useActor } from "../App";
import { api, errorText, items } from "../api";
import {
  Empty,
  ErrorBox,
  Field,
  Loading,
  Modal,
  PageTitle,
  useResource,
} from "../ui";
import RussiaTimeZoneField, {
  russiaTimeZoneLabel,
} from "../RussiaTimeZoneField";
import {
  broadcastAudience,
  broadcastMentionPeople,
  broadcastStatus,
  reconcileBroadcastSelection,
  editBroadcastText,
  encodeBroadcastText,
  insertBroadcastMention,
  type BroadcastText,
} from "../broadcastText";
import "./Broadcasts.css";
const roles = ["HR_ADMIN", "HR_SPECIALIST", "HEAD_TALENT"];
const unitTypes: Record<string, string> = {
  DIRECTION: "Направление",
  DEPARTMENT: "Отдел",
  TEAM: "Команда",
};
export function BroadcastStatus({ item }: { item: any }) {
  const s = broadcastStatus(item);
  return (
    <div className="broadcast-state">
      <span className={`badge ${s.tone}`}>{s.label}</span>
      {s.detail && <small>{s.detail}</small>}
    </div>
  );
}
export default function Broadcasts() {
  const actor = useActor(),
    permitted = roles.includes(actor.role),
    list = useResource(permitted ? "/broadcasts" : null),
    directory = useResource(permitted ? "/broadcasts/recipients" : null);
  const [creating, setCreating] = useState(false),
    [creationBusy, setCreationBusy] = useState(false),
    [selected, setSelected] = useState("");
  useEffect(() => {
    if (!permitted) return;
    const timer = setInterval(list.reload, 6000);
    return () => clearInterval(timer);
  }, [permitted]);
  if (!permitted)
    return (
      <>
        <PageTitle title="Рассылки" />
        <ErrorBox error="Рассылки доступны HR и руководителю управления по подбору персонала" />
      </>
    );
  return (
    <>
      <PageTitle
        title="Рассылки"
        action={
          <button className="button primary" onClick={() => setCreating(true)}>
            <Plus size={17} />
            Создать рассылку
          </button>
        }
      />
      <section className="broadcasts-page">
        <ErrorBox error={list.error} />
        {list.error ? null : list.loading && !list.data ? (
          <Loading />
        ) : !items(list.data).length ? (
          <Empty title="Рассылок пока нет" />
        ) : (
          <div className="broadcast-list">
            {items(list.data).map((item) => (
              <button
                className="panel broadcast-card"
                key={item.id}
                onClick={() => setSelected(item.id)}
              >
                <BroadcastStatus item={item} />
                <strong>{item.text}</strong>
                <span>
                  {item.scheduled_date} · {item.scheduled_time} ·{" "}
                  {russiaTimeZoneLabel(item.timezone)}
                </span>
                <small>
                  Принято MAX: {item.counts.accepted} из {item.counts.eligible}
                  {item.counts.skipped
                    ? ` · Пропущено: ${item.counts.skipped}`
                    : ""}
                  {item.counts.failed ? ` · Ошибок: ${item.counts.failed}` : ""}
                  {item.counts.unknown
                    ? ` · Не подтверждено: ${item.counts.unknown}`
                    : ""}
                </small>
              </button>
            ))}
          </div>
        )}
      </section>
      {creating && (
        <Modal
          title="Создать рассылку"
          onClose={() => {
            if (!creationBusy) setCreating(false);
          }}
          wide
        >
          <ErrorBox error={directory.error} />
          {directory.loading && !directory.data ? (
            <Loading />
          ) : (
            directory.data && (
              <BroadcastForm
                employees={directory.data.employees || []}
                units={directory.data.units || []}
                limit={directory.data.max_recipients || 5000}
                onRefresh={directory.reload}
                onBusy={setCreationBusy}
                onDone={(id) => {
                  setCreating(false);
                  setSelected(id);
                  list.reload();
                }}
              />
            )
          )}
        </Modal>
      )}
      {selected && (
        <BroadcastDetail id={selected} onClose={() => setSelected("")} />
      )}
    </>
  );
}
export function BroadcastForm({
  employees,
  units,
  limit = 5000,
  onDone,
  onRefresh,
  onBusy,
}: {
  employees: any[];
  units: any[];
  limit?: number;
  onDone: (id: string) => void;
  onRefresh: () => void;
  onBusy?: (busy: boolean) => void;
}) {
  const [selected, setSelected] = useState<string[]>([]),
    [groups, setGroups] = useState<string[]>([]),
    [value, setValue] = useState<BroadcastText>({ text: "", mentions: [] });
  const [search, setSearch] = useState(""),
    [mentionSearch, setMentionSearch] = useState(""),
    [mentionUnit, setMentionUnit] = useState(""),
    [date, setDate] = useState(""),
    [time, setTime] = useState(""),
    [zone, setZone] = useState("Europe/Moscow");
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [requestId] = useState(() => crypto.randomUUID()),
    editor = useRef<HTMLTextAreaElement>(null);
  const audience = broadcastAudience(employees, selected, groups),
    eligible = audience.filter((p) => p.available),
    repaired = reconcileBroadcastSelection(employees, units, selected, groups);
  function toggle(id: string, list: string[], change: (v: string[]) => void) {
    change(list.includes(id) ? list.filter((k) => k !== id) : [...list, id]);
  }
  function mention(person: any) {
    const start = editor.current?.selectionStart ?? value.text.length,
      end = editor.current?.selectionEnd ?? start,
      updated = insertBroadcastMention(value, person, start, end);
    setValue(updated);
    requestAnimationFrame(() => {
      editor.current?.focus();
      const pos = updated.mentions.find((m) => m.start === start)?.end ?? start;
      editor.current?.setSelectionRange(pos, pos);
    });
  }
  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (busy) return;
    setError("");
    if (repaired.removed) {
      setError(
        "Состав доступа изменился. Уберите недоступных получателей из выбора.",
      );
      return;
    }
    if (!eligible.length || audience.length > limit) {
      setError(`Выберите от 1 до ${limit} получателей с подключённым MAX`);
      return;
    }
    setBusy(true);
    onBusy?.(true);
    try {
      const r = await api.post("/broadcasts", {
        client_request_id: requestId,
        text: encodeBroadcastText(value),
        recipient_ids: selected,
        org_unit_ids: groups,
        scheduled_date: date,
        scheduled_time: time,
        timezone: zone,
      });
      onDone(r.data.id);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
      onBusy?.(false);
    }
  }
  return (
    <form className="broadcast-compose" onSubmit={submit}>
      <fieldset disabled={busy} className="broadcast-content">
        <div className="broadcast-audience">
          <section>
            <h3>Сотрудники</h3>
            <input
              aria-label="Поиск получателей"
              placeholder="Найти сотрудника"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            <div className="broadcast-picker">
              {employees
                .filter((p) =>
                  `${p.name} ${p.department || ""}`
                    .toLowerCase()
                    .includes(search.toLowerCase()),
                )
                .map((p) => (
                  <label className="broadcast-person" key={p.id}>
                    <input
                      type="checkbox"
                      checked={selected.includes(p.id)}
                      disabled={!p.available}
                      onChange={() => toggle(p.id, selected, setSelected)}
                    />
                    <span>
                      <strong>{p.name}</strong>
                      <small>
                        {p.department || "Без подразделения"}
                        {!p.available &&
                          ` · ${p.unavailable_reason || "MAX не подключён"}`}
                      </small>
                    </span>
                  </label>
                ))}
              {!employees.length && (
                <p className="muted">В вашей области доступа нет сотрудников</p>
              )}
            </div>
          </section>
          <section>
            <h3>Подразделения</h3>
            <div className="broadcast-picker">
              {units.map((u) => (
                <label className="broadcast-person" key={u.id}>
                  <input
                    type="checkbox"
                    checked={groups.includes(u.id)}
                    onChange={() => toggle(u.id, groups, setGroups)}
                  />
                  <span>
                    <strong>{u.title}</strong>
                    <small>{unitTypes[u.type] || "Подразделение"}</small>
                  </span>
                </label>
              ))}
              {!units.length && (
                <p className="muted">Подразделения не созданы</p>
              )}
            </div>
          </section>
        </div>
        <p className="broadcast-count" role="status">
          Получателей: {eligible.length} · Пропущено без доступа или MAX:{" "}
          {audience.length - eligible.length}. Лимит: {limit}.
        </p>
        {repaired.removed > 0 && (
          <div className="broadcast-selection-warning" role="alert">
            <span>
              Недоступных в выборе: {repaired.removed}. Текст сообщения
              сохранён.
            </span>
            <button
              type="button"
              className="button secondary"
              onClick={() => {
                setSelected(repaired.recipients);
                setGroups(repaired.units);
              }}
            >
              Убрать недоступных из выбора
            </button>
          </div>
        )}
        <Field label="Текст сообщения">
          <textarea
            ref={editor}
            rows={7}
            required
            maxLength={3900}
            value={value.text}
            onChange={(e) => setValue(editBroadcastText(value, e.target.value))}
          />
        </Field>
        <small className="muted">
          {value.text.length} / 3900 · Упоминания: {value.mentions.length}
        </small>
        <div className="broadcast-schedule">
          <Field label="Дата">
            <input
              type="date"
              required
              value={date}
              onChange={(e) => setDate(e.target.value)}
            />
          </Field>
          <Field label="Время">
            <input
              type="time"
              required
              value={time}
              onChange={(e) => setTime(e.target.value)}
            />
          </Field>
          <RussiaTimeZoneField value={zone} onChange={setZone} />
        </div>
        <ErrorBox error={error} />
        <div className="broadcast-actions">
          <button
            type="submit"
            className="button primary"
            disabled={
              busy ||
              !eligible.length ||
              audience.length > limit ||
              repaired.removed > 0
            }
          >
            <Send size={16} />
            {busy ? "Сохраняем…" : "Запланировать"}
          </button>
          <button
            type="button"
            className="button secondary"
            onClick={onRefresh}
          >
            Обновить получателей
          </button>
        </div>
      </fieldset>
      <aside className="broadcast-mentions">
        <h3>
          <AtSign size={18} />
          Упоминания
        </h3>
        <Field label="Подразделение для упоминания">
          <select
            value={mentionUnit}
            onChange={(e) => setMentionUnit(e.target.value)}
          >
            <option value="">Все подразделения</option>
            {units.map((unit) => (
              <option key={unit.id} value={unit.id}>
                {unit.title}
              </option>
            ))}
          </select>
        </Field>
        <input
          aria-label="Поиск для упоминания"
          placeholder="Найти сотрудника"
          value={mentionSearch}
          onChange={(e) => setMentionSearch(e.target.value)}
        />
        <div className="broadcast-mention-list">
          {broadcastMentionPeople(employees, mentionUnit, mentionSearch).map(
            (p) => (
              <button
                type="button"
                disabled={busy}
                className="broadcast-mention"
                key={p.id}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => mention(p)}
              >
                <AtSign size={14} />
                <span>
                  {p.name}
                  <small>{p.department || "Без подразделения"}</small>
                </span>
              </button>
            ),
          )}
        </div>
      </aside>
    </form>
  );
}
function BroadcastDetail({ id, onClose }: { id: string; onClose: () => void }) {
  const detail = useResource(`/broadcasts/${id}`);
  useEffect(() => {
    const t = setInterval(detail.reload, 5000);
    return () => clearInterval(t);
  }, [id]);
  const item: any = detail.data;
  return (
    <Modal title="Рассылка" onClose={onClose} wide>
      <ErrorBox error={detail.error} />
      {detail.loading && !item ? (
        <Loading />
      ) : (
        item &&
        !detail.error && (
          <div className="broadcast-detail">
            <BroadcastStatus item={item} />
            <p className="broadcast-message">{item.text}</p>
            <p>
              {item.scheduled_date} · {item.scheduled_time} ·{" "}
              {russiaTimeZoneLabel(item.timezone)}
            </p>
            <p>
              Принято MAX: {item.counts.accepted} из {item.counts.eligible} · В
              ожидании: {item.counts.pending} · Ошибок: {item.counts.failed} ·
              Пропущено: {item.counts.skipped}
            </p>
            <div className="broadcast-deliveries">
              {item.recipients.map((p: any) => (
                <div key={p.employee_id}>
                  <strong>{p.name}</strong>
                  <span>
                    {p.status === "ACCEPTED"
                      ? "Принято MAX"
                      : p.status === "SKIPPED"
                        ? "Пропущено"
                        : p.status === "UNKNOWN_DELIVERY"
                          ? "Результат не подтверждён"
                          : p.error
                            ? "Ошибка доставки"
                            : "В ожидании"}
                  </span>
                  {p.error && <small role="status">{p.error}</small>}
                </div>
              ))}
            </div>
          </div>
        )
      )}
    </Modal>
  );
}
