import { useState } from "react";
import { CheckCheck, GitPullRequest } from "lucide-react";
import { api, errorText, items } from "../api";
import { nodeNames } from "../scenario";
import {
  ErrorBox,
  Field,
  Notice,
  Status,
  Success,
  date,
  useResource,
} from "../ui";
const signalNames: Record<string, string> = {
  HR_START_APPROVED: "Выход сотрудника подтверждён",
  RECRUITER_PROFILE_APPROVED: "Профиль сотрудника проверен",
  RECRUITER_PLAN_APPROVED: "План адаптации проверен",
  RECRUITER_PHOTO_APPROVED: "Фотография проверена",
};
export default function JourneyAdvanced({
  journey,
  timeline,
  onDone,
}: {
  journey: any;
  timeline: any[];
  onDone: () => void;
}) {
  const versions = useResource(`/scenarios/${journey.scenario_id}/versions`);
  const [target, setTarget] = useState(""),
    [plan, setPlan] = useState<any>(null),
    [confirmed, setConfirmed] = useState(false),
    [signal, setSignal] = useState(""),
    [reason, setReason] = useState(""),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [busy, setBusy] = useState(false);
  const signalCodes = Array.from(
    new Set<string>(
      timeline
        .filter(
          (t) =>
            t.journey_id === journey.id &&
            (t.step?.trigger?.type === "SIGNAL" || t.step?.required_signal),
        )
        .map((t) => t.step.required_signal || t.step.trigger.signal_code),
    ),
  ).filter((code) => !journey.signals?.[code]);
  const published = items(versions.data).filter(
    (v) => v.status === "PUBLISHED" && v.id !== journey.version_id,
  );
  async function run(action: () => Promise<void>) {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await action();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <ErrorBox error={error || versions.error} />
      {success && <Success>{success}</Success>}
      {signalCodes.length > 0 && (
        <details className="advanced">
          <summary>Подтверждения для продолжения программы</summary>
          <Notice>
            Отмечайте только реально завершённые действия. Публикация биографии
            и фото выполняется отдельно через предварительный просмотр и выбор
            получателей.
          </Notice>
          <Field label="Какое действие выполнено">
            <select value={signal} onChange={(e) => setSignal(e.target.value)}>
              <option value="">Выберите подтверждение</option>
              {signalCodes.map((code) => (
                <option key={code} value={code}>
                  {signalNames[code] || code}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Основание подтверждения">
            <textarea
              required
              minLength={3}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>
          <button
            className="button secondary"
            disabled={busy || !signal || reason.trim().length < 3}
            onClick={() =>
              run(async () => {
                await api.post(`/journeys/${journey.id}/signals`, {
                  signal_code: signal,
                  reason,
                });
                onDone();
                setSignal("");
                setSuccess("Действие подтверждено, будущие шаги пересчитаны.");
              })
            }
          >
            <CheckCheck size={16} />
            Зафиксировать выполнение
          </button>
        </details>
      )}
      {published.length > 0 && (
        <details className="advanced">
          <summary>Перевести на новую версию программы</summary>
          <Notice>
            Сначала изучите пересчитанный план. Перенос выполняется явно;
            активная анкета блокирует смену версии, а ответы и уже принятые
            сообщения сохраняются.
          </Notice>
          <Field label="Основание смены программы">
            <textarea
              required
              minLength={3}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </Field>
          <Field label="Новая опубликованная версия">
            <select
              value={target}
              onChange={(e) => {
                setTarget(e.target.value);
                setPlan(null);
                setConfirmed(false);
              }}
            >
              <option value="">Выберите версию</option>
              {published.map((v) => (
                <option key={v.id} value={v.id}>
                  {v.name || "Программа адаптации"} · версия{" "}
                  {v.version_no || v.version}
                </option>
              ))}
            </select>
          </Field>
          <button
            className="button secondary"
            disabled={busy || !target}
            onClick={() =>
              run(async () => {
                const result = await api.post(`/journeys/${journey.id}/plans`, {
                  version_id: target,
                });
                setPlan(result.data);
                setConfirmed(false);
              })
            }
          >
            <GitPullRequest size={16} />
            Показать новый план
          </button>
          {plan && (
            <>
              <p>
                В новом плане {plan.timeline?.length ?? 0} шагов.
                Предварительный просмотр действует до {date(plan.expires_at)}.
              </p>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Шаг</th>
                      <th>Плановое время</th>
                      <th>Применимость</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(plan.timeline || []).map((t: any, i: number) => (
                      <tr key={t.step_key || i}>
                        <td>
                          {t.title || nodeNames[t.kind] || `Шаг ${i + 1}`}
                        </td>
                        <td>{t.due_at ? date(t.due_at) : "После условия"}</td>
                        <td>
                          <Status
                            value={
                              t.status ||
                              (t.applicable === false
                                ? "NOT_APPLICABLE"
                                : "PLANNED")
                            }
                          />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <label className="check-field">
                <input
                  type="checkbox"
                  checked={confirmed}
                  onChange={(e) => setConfirmed(e.target.checked)}
                />
                План проверен. Перенести текущий маршрут на выбранную версию.
              </label>
              <button
                className="button primary"
                disabled={busy || !confirmed || reason.trim().length < 3}
                onClick={() =>
                  run(async () => {
                    await api.post(`/journeys/${journey.id}/plans/apply`, {
                      plan_id: plan.id || plan.preview_id,
                      preview_hash: plan.preview_hash,
                      reason,
                    });
                    setPlan(null);
                    versions.reload();
                    onDone();
                    setSuccess(
                      "Программа сотрудника обновлена после проверки плана.",
                    );
                  })
                }
              >
                Применить новый план
              </button>
            </>
          )}
        </details>
      )}
    </>
  );
}
