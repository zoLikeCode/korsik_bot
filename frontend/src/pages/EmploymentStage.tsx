import { useEffect, useRef, useState } from "react";
import { CalendarClock, RefreshCcw } from "lucide-react";
import { api, errorText, items } from "../api";
import { stageTimeCandidates, stageTimeLabel } from "../employmentTime";
import {
  ErrorBox,
  Field,
  Loading,
  Notice,
  Status,
  Success,
  useResource,
} from "../ui";

const stageLabels: Record<string, string> = {
  PREBOARDING: "До выхода на работу",
  ACTIVE: "После выхода на работу",
};
const outcomeLabels: Record<string, string> = {
  SUPERSEDED: "Заменено новым изменением",
  SKIPPED: "Не применено: условия изменились",
  STALE: "Не применено: данные изменились",
  BLOCKED: "Нужна проверка HR",
};
const reasonLabels: Record<string, string> = {
  SUPERSEDED_BY_NEW_STAGE_COMMAND: "Заменено новым расписанием",
  SUPERSEDED_BY_IMMEDIATE_STAGE: "Этап подтверждён немедленно",
  HR_CANCELLED: "Отменено HR",
  EMPLOYMENT_ENDED: "Период найма завершён",
  EMPLOYMENT_REHIRED: "Оформлен новый период найма",
  EMPLOYMENT_EPISODE_ENDED_OR_REPLACED: "Период найма завершён или заменён",
  AUTHOR_ACCESS_CHANGED: "Изменились права автора расписания",
  AUTHOR_EMPLOYEE_SCOPE_REVOKED: "Автор больше не отвечает за эту карточку",
  STALE_EPISODE_OR_STAGE_COMMAND: "Карточка или подтверждённый этап изменились",
};
export default function EmploymentStage({
  employee,
  onDone,
}: {
  employee: any;
  onDone: () => void;
}) {
  const episodeId = employee.episode_id;
  const episode = useResource(
    episodeId ? `/employment-episodes/${episodeId}` : null,
  );
  const history = useResource(
    episodeId ? `/employment-episodes/${episodeId}/stage-changes` : null,
  );
  const [stage, setStage] = useState(
    employee.employment_stage === "ACTIVE" ? "PREBOARDING" : "ACTIVE",
  );
  const [timing, setTiming] = useState("NOW");
  const [localTime, setLocalTime] = useState("");
  const [chosenInstant, setChosenInstant] = useState("");
  const [reason, setReason] = useState("");
  const [cancelReason, setCancelReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const deviceZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const employeeZone = employee.timezone || "Europe/Moscow";
  const candidates =
    timing === "SCHEDULE" ? stageTimeCandidates(localTime, deviceZone) : [];
  const effectiveAt =
    candidates.length === 1
      ? candidates[0]
      : candidates.includes(chosenInstant)
        ? chosenInstant
        : "";
  const ended =
    employee.employment_stage === "ENDED" || episode.data?.status === "ENDED";
  const rows = items(history.data);
  const activeId = history.data?.active_operation_id;
  const pending =
    !!activeId ||
    rows.some((row) =>
      ["SCHEDULED", "PENDING", "QUEUED", "RUNNING"].includes(row.status),
    );
  const previousActive = useRef<string | null>(null);
  useEffect(() => {
    if (pending) {
      const timer = setInterval(history.reload, 5000);
      return () => clearInterval(timer);
    }
  }, [pending]);
  useEffect(() => {
    if (!history.data) return;
    if (previousActive.current && !activeId) {
      episode.reload();
      onDone();
    }
    previousActive.current = activeId || null;
  }, [history.data]);
  function changed() {
    setConfirmed(false);
    setSuccess("");
  }
  if (!episodeId) return null;
  return (
    <section className="panel inset">
      <div className="panel-head">
        <h3>
          <CalendarClock size={18} /> Этап адаптации
        </h3>
        <Status value={employee.employment_stage || episode.data?.status} />
      </div>
      <p className="muted">
        Подтвердите фактический этап сейчас или задайте будущий момент
        переключения. Это изменение текущего периода найма.
      </p>
      <p className="muted">
        Если до назначенного момента изменятся период найма, данные карточки или
        права автора, старое расписание не применится. Результат будет виден в
        истории.
      </p>
      <ErrorBox error={error || episode.error || history.error} />
      {success && <Success>{success}</Success>}
      {episode.loading && <Loading />}
      {ended ? (
        <Notice>
          Период найма завершён. Для новой адаптации оформите повторный найм.
        </Notice>
      ) : (
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            setError("");
            setSuccess("");
            if (
              timing === "SCHEDULE" &&
              (!effectiveAt || Date.parse(effectiveAt) < Date.now() + 60_000)
            ) {
              setError(
                "Для планирования выберите однозначный момент хотя бы на минуту позже текущего времени.",
              );
              return;
            }
            setBusy(true);
            try {
              const response = await api.post(
                `/employment-episodes/${episodeId}/stage`,
                {
                  expected_episode_id: episodeId,
                  expected_state_version: episode.data.version,
                  stage,
                  ...(timing === "SCHEDULE"
                    ? { effective_at: effectiveAt }
                    : {}),
                  reason: reason.trim(),
                },
                episode.etag || episode.data.etag,
              );
              setSuccess(
                response.data.operation_id ||
                  response.data.type === "EMPLOYMENT_STAGE_CHANGE"
                  ? "Расписание сохранено. Проверяйте результат выполнения в истории."
                  : "Этап адаптации применён.",
              );
              setConfirmed(false);
              setReason("");
              episode.reload();
              history.reload();
              onDone();
            } catch (cause) {
              setError(errorText(cause));
              episode.reload();
              history.reload();
            } finally {
              setBusy(false);
            }
          }}
        >
          <fieldset
            disabled={
              busy || episode.loading || !episode.data || !!episode.error
            }
          >
            <div className="form-grid">
              <Field label="Новый этап адаптации">
                <select
                  value={stage}
                  onChange={(event) => {
                    setStage(event.target.value);
                    changed();
                  }}
                >
                  <option value="PREBOARDING">До выхода на работу</option>
                  <option value="ACTIVE">После выхода на работу</option>
                </select>
              </Field>
              <Field label="Когда применить этап">
                <select
                  value={timing}
                  onChange={(event) => {
                    setTiming(event.target.value);
                    changed();
                  }}
                >
                  <option value="NOW">Сейчас</option>
                  <option value="SCHEDULE">Запланировать</option>
                </select>
              </Field>
            </div>
            {timing === "SCHEDULE" && (
              <>
                <Field
                  label={`Дата и время в поясе устройства: ${deviceZone}`}
                  hint="Время вводится в часовом поясе вашего устройства, даже если сотрудник работает в другом поясе. Выберите момент хотя бы на минуту позже текущего."
                >
                  <input
                    type="datetime-local"
                    required
                    value={localTime}
                    onChange={(event) => {
                      setLocalTime(event.target.value);
                      setChosenInstant("");
                      changed();
                    }}
                  />
                </Field>
                {localTime && !candidates.length && (
                  <ErrorBox error="Такой даты или местного времени не существует. Проверьте ввод; при переводе часов некоторые часы пропускаются." />
                )}
                {candidates.length > 1 && (
                  <Field label="Уточните момент: это время повторяется при переводе часов">
                    <select
                      required
                      value={chosenInstant}
                      onChange={(event) => {
                        setChosenInstant(event.target.value);
                        changed();
                      }}
                    >
                      <option value="">Выберите смещение часов</option>
                      {candidates.map((instant) => (
                        <option value={instant} key={instant}>
                          {stageTimeLabel(instant, deviceZone)}
                        </option>
                      ))}
                    </select>
                  </Field>
                )}
                {effectiveAt && (
                  <div className="resolution">
                    <strong>Один и тот же момент переключения</strong>
                    <p>
                      На вашем устройстве ({deviceZone}):{" "}
                      {stageTimeLabel(effectiveAt, deviceZone)}
                    </p>
                    <p>
                      У сотрудника ({employeeZone}):{" "}
                      {stageTimeLabel(effectiveAt, employeeZone)}
                    </p>
                    <p className="muted">
                      В UTC: {stageTimeLabel(effectiveAt, "UTC")}
                    </p>
                  </div>
                )}
              </>
            )}
            {activeId && (
              <Notice>
                {timing === "NOW"
                  ? "Применение сейчас отменит ранее запланированное переключение."
                  : "Новое расписание заменит ранее запланированное переключение этого периода."}
              </Notice>
            )}
            <Field label="Основание изменения этапа">
              <textarea
                required
                minLength={3}
                value={reason}
                onChange={(event) => {
                  setReason(event.target.value);
                  changed();
                }}
                rows={2}
              />
            </Field>
            <label className="check-field">
              <input
                type="checkbox"
                required
                checked={confirmed}
                onChange={(event) => setConfirmed(event.target.checked)}
              />
              Подтверждаю этап и момент применения для этого сотрудника.
            </label>
            <button
              className="button secondary"
              disabled={
                busy ||
                !episode.data ||
                !!episode.error ||
                !confirmed ||
                reason.trim().length < 3 ||
                (timing === "SCHEDULE" && !effectiveAt)
              }
            >
              {busy
                ? "Сохраняем…"
                : timing === "SCHEDULE"
                  ? "Запланировать изменение этапа"
                  : "Применить этап сейчас"}
            </button>
          </fieldset>
        </form>
      )}
      <div className="panel-head">
        <h4>Запланированные изменения и история</h4>
        <button
          type="button"
          className="text-button"
          onClick={() => {
            history.reload();
            episode.reload();
          }}
        >
          <RefreshCcw size={14} /> Обновить этап и историю
        </button>
      </div>
      {history.loading && !history.data ? (
        <Loading />
      ) : !rows.length ? (
        <p className="muted">Изменений этапа по расписанию ещё нет.</p>
      ) : (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Этап</th>
                  <th>Момент на вашем устройстве · {deviceZone}</th>
                  <th>Момент у сотрудника · {employeeZone}</th>
                  <th>Результат</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id || row.operation_id}>
                    <td>{stageLabels[row.target_stage] || "Этап адаптации"}</td>
                    <td>{stageTimeLabel(row.effective_at, deviceZone)}</td>
                    <td>{stageTimeLabel(row.effective_at, employeeZone)}</td>
                    <td>
                      {outcomeLabels[row.status] || (
                        <Status value={row.status} />
                      )}
                      {row.reason_code && (
                        <p className="muted">
                          {reasonLabels[row.reason_code] ||
                            "Условия изменились; проверьте карточку и назначьте новое время."}
                        </p>
                      )}
                      {row.applied_at && (
                        <p className="muted">
                          Фактически применено:{" "}
                          {stageTimeLabel(row.applied_at, employeeZone)}
                        </p>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {activeId && !ended && (
            <div>
              <Field label="Почему отменяется запланированное изменение этапа">
                <textarea
                  value={cancelReason}
                  minLength={3}
                  rows={2}
                  onChange={(event) => setCancelReason(event.target.value)}
                />
              </Field>
              <button
                type="button"
                className="button secondary"
                disabled={busy || cancelReason.trim().length < 3}
                onClick={async () => {
                  setBusy(true);
                  setError("");
                  setSuccess("");
                  try {
                    const current = await api.get(`/operations/${activeId}`);
                    await api.post(
                      `/operations/${activeId}/cancel`,
                      { reason: cancelReason.trim() },
                      current.etag || current.data.etag,
                    );
                    setCancelReason("");
                    setSuccess(
                      "Запланированное изменение отменено. Текущий этап сохранён.",
                    );
                    history.reload();
                    episode.reload();
                    onDone();
                  } catch (cause) {
                    setError(errorText(cause));
                    history.reload();
                    episode.reload();
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                Отменить запланированное изменение этапа
              </button>
            </div>
          )}
        </>
      )}
    </section>
  );
}
