import { useEffect, useRef, useState } from "react";
import { Cpu, FlaskConical, Play, Plus } from "lucide-react";
import { useActor } from "../App";
import { api, errorText, items } from "../api";
import {
  Badge,
  Empty,
  ErrorBox,
  Field,
  Loading,
  Notice,
  Status,
  Success,
  useResource,
} from "../ui";
const roleNames: Record<string, string> = {
  embedder: "Энкодер поиска",
  reranker: "Модель переранжирования",
  generator: "Модель ответа",
};
const checkNames: Record<string, string> = {
  embedder_identity: "Версия энкодера совпала",
  reranker_identity: "Версия переранжирования совпала",
  generator_identity: "Версия генератора совпала",
  russian_embedding: "Русский текст преобразуется в вектор",
  russian_reranking: "Релевантный фрагмент получает более высокий ранг",
  semantic_evaluation_report: "Есть проверенный отчёт о качестве модели",
  grounded_generation_1: "Контроль времени обеда",
  grounded_generation_2: "Контроль адресата обращения",
  grounded_generation_3: "Контроль срока уведомления",
};

export default function LocalAi() {
  const actor = useActor();
  const canEdit =
    actor.role === "PLATFORM_ADMIN" ||
    (actor.role === "HR_ADMIN" && actor.scope?.all === true);
  const active = useResource("/local-ai/active-profile"),
    models = useResource("/local-ai/models"),
    profiles = useResource("/local-ai/profiles");
  const [profileId, setProfileId] = useState(""),
    [validationId, setValidationId] = useState(""),
    [name, setName] = useState("Локальный профиль ответов"),
    [policy, setPolicy] = useState("LOCAL_GENERATION"),
    [chosen, setChosen] = useState<Record<string, string>>({}),
    [reason, setReason] = useState(
      "Настройка локальных моделей администратором",
    ),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [busy, setBusy] = useState(false);
  const detail = useResource(
    profileId ? `/local-ai/profiles/${profileId}` : null,
  );
  const current = detail.data?.id === profileId ? detail.data : null;
  const vid =
    validationId || current?.latest_validation_id || current?.validation_id;
  const validation = useResource(vid ? `/local-ai/validations/${vid}` : null);
  const pending =
    !!vid &&
    (!validation.data ||
      ["PENDING", "QUEUED", "RUNNING", "PROCESSING"].includes(
        validation.data.status,
      ));
  const finished = useRef("");
  useEffect(() => {
    if (active.data?.deployment_mode === "FULL") setPolicy("LOCAL_GENERATION");
  }, [active.data?.deployment_mode]);
  useEffect(() => {
    if (!profileId) {
      const id = active.data?.active_profile?.id || items(profiles.data)[0]?.id;
      if (id) setProfileId(id);
    }
  }, [active.data, profiles.data]);
  useEffect(() => {
    setChosen((previous) => {
      const next = { ...previous };
      for (const role of Object.keys(roleNames)) {
        if (!next[role])
          next[role] =
            items(models.data).find(
              (m) => m.role === role && m.deployment_selected,
            )?.id ||
            items(models.data).find((m) => m.role === role)?.id ||
            "";
      }
      return next;
    });
  }, [models.data]);
  useEffect(() => {
    if (pending) {
      const timer = setInterval(validation.reload, 2500);
      return () => clearInterval(timer);
    }
  }, [vid, pending]);
  useEffect(() => {
    if (
      vid &&
      validation.data &&
      ["PASSED", "FAILED", "CANCELLED"].includes(validation.data.status) &&
      finished.current !== vid
    ) {
      finished.current = vid;
      detail.reload();
      profiles.reload();
    }
  }, [validation.data, vid]);
  async function run(task: () => Promise<void>) {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await task();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const required =
    policy === "LOCAL_GENERATION"
      ? ["embedder", "reranker", "generator"]
      : ["embedder", "reranker"];
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>
            <Cpu size={20} /> Локальные модели
          </h2>
        </div>
        <Badge tone={active.data?.active_profile ? "green" : "amber"}>
          {active.data?.active_profile ? "Профиль активен" : "Нужна настройка"}
        </Badge>
      </div>
      <ErrorBox
        error={
          error ||
          active.error ||
          models.error ||
          profiles.error ||
          detail.error ||
          validation.error
        }
      />
      {success && <Success>{success}</Success>}
      {!canEdit && (
        <Notice>
          Общая конфигурация моделей доступна для чтения. Проверку и
          переключение выполняет центральный HR или администратор платформы.
        </Notice>
      )}
      {active.loading ? (
        <Loading />
      ) : (
        <p>
          {active.data?.active_profile
            ? `Активен профиль «${active.data.active_profile.name}». Политика: ${active.data.active_profile.configuration?.generation_policy === "LOCAL_GENERATION" ? "локальная генерация с проверкой источников" : "ответы выдержками из документов"}.`
            : "Генерация пока не активирована. Ответы по доступным документам продолжают работать без внешнего AI API."}
        </p>
      )}
      {canEdit && (
        <details className="advanced">
          <summary>Создать профиль из установленных моделей</summary>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              run(async () => {
                const response = await api.post("/local-ai/profiles", {
                  name,
                  models: Object.fromEntries(
                    required.map((role) => [role, chosen[role]]),
                  ),
                  generation_policy: policy,
                  retrieval_profile: "cpu",
                  reason,
                });
                setProfileId(response.data.id);
                setValidationId("");
                profiles.reload();
                setSuccess(
                  "Черновик профиля создан. Выполните технические проверки перед активацией.",
                );
              });
            }}
          >
            <Field label="Название профиля">
              <input
                required
                minLength={1}
                maxLength={160}
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </Field>
            <Field label="Как формировать ответ">
              <select
                value={policy}
                onChange={(e) => setPolicy(e.target.value)}
              >
                <option value="LOCAL_GENERATION">
                  Локальная LLM + проверяемые источники
                </option>
                {active.data?.deployment_mode !== "FULL" && (
                  <option value="EXTRACTIVE_ONLY">
                    Точные выдержки из документов
                  </option>
                )}
              </select>
            </Field>
            <div className="form-grid">
              {required.map((role) => (
                <Field key={role} label={roleNames[role]}>
                  <select
                    required
                    value={chosen[role] || ""}
                    onChange={(e) =>
                      setChosen({ ...chosen, [role]: e.target.value })
                    }
                  >
                    <option value="">Выберите установленную модель</option>
                    {items(models.data)
                      .filter((m) => m.role === role)
                      .map((m) => (
                        <option key={m.id} value={m.id}>
                          {m.repository || m.bundle_subdir} · {m.bundle_subdir}
                        </option>
                      ))}
                  </select>
                </Field>
              ))}
            </div>
            <Notice>
              Доступны только установленные и зафиксированные артефакты.
              Retrieval работает на CPU. Пределы текущего runtime: контекст 4096
              токенов, вход 3840, ответ 256, thinking выключен. Эти ограничения
              задаются сервером.
            </Notice>
            <button
              className="button secondary"
              disabled={
                busy ||
                reason.trim().length < 3 ||
                required.some((role) => !chosen[role])
              }
            >
              <Plus size={16} />
              Создать черновик
            </button>
          </form>
        </details>
      )}
      {!items(models.data).length && !models.loading && (
        <Empty title="Локальные артефакты ещё не установлены">
          Администратор должен перенести подготовленный пакет моделей во
          внутренний контур. Runtime не скачивает модели из интернета.
        </Empty>
      )}
      {!!items(profiles.data).length && (
        <>
          <Field label="Профиль для проверки">
            <select
              value={profileId}
              onChange={(e) => {
                setProfileId(e.target.value);
                setValidationId("");
              }}
            >
              {items(profiles.data).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} · {p.status}
                </option>
              ))}
            </select>
          </Field>
          {current && (
            <>
              <div className="button-row wrap">
                <button
                  className="button secondary"
                  disabled={
                    !canEdit ||
                    busy ||
                    pending ||
                    detail.loading ||
                    reason.trim().length < 3
                  }
                  onClick={() =>
                    run(async () => {
                      const response = await api.post(
                        `/local-ai/profiles/${profileId}/tests`,
                        { reason },
                        detail.etag || detail.data.etag,
                      );
                      setValidationId(
                        response.data.validation_id || response.data.id,
                      );
                      setSuccess(
                        "Проверка запущена на локальном runtime; результат сохранится после перезагрузки.",
                      );
                    })
                  }
                >
                  <FlaskConical size={16} />
                  {pending ? "Идёт проверка…" : "Проверить runtime"}
                </button>
                <button
                  className="button primary"
                  disabled={
                    !canEdit ||
                    busy ||
                    pending ||
                    detail.loading ||
                    reason.trim().length < 3 ||
                    validation.data?.status !== "PASSED" ||
                    validation.data?.profile_id !== profileId ||
                    detail.data.status === "ACTIVE"
                  }
                  onClick={() =>
                    run(async () => {
                      await api.post(
                        `/local-ai/profiles/${profileId}/activate`,
                        { reason },
                        detail.etag || detail.data.etag,
                      );
                      active.reload();
                      profiles.reload();
                      detail.reload();
                      setSuccess("Локальный профиль активирован");
                    })
                  }
                >
                  <Play size={16} />
                  Активировать проверенный профиль
                </button>
              </div>
              <Field label="Причина настройки / переключения">
                <input
                  required
                  minLength={3}
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
              </Field>
            </>
          )}
          {vid && (
            <section className="panel inset">
              <div className="panel-head">
                <h3>Техническая проверка</h3>
                <Status value={validation.data?.status || "QUEUED"} />
              </div>
              {pending && <Loading />}
              {(validation.data?.checks || []).map((c: any) => (
                <div className="panel-head" key={c.name}>
                  <span>
                    {checkNames[c.name] || c.name}
                    {typeof c.seconds === "number" && (
                      <small className="muted">
                        {" "}
                        · {c.seconds.toFixed(2)}с
                      </small>
                    )}
                    {c.error_type && (
                      <small className="error-text"> · {c.error_type}</small>
                    )}
                  </span>
                  <Status value={c.status} />
                </div>
              ))}
              <Notice>
                Это проверка работоспособности моделей на технических примерах.
                Корпоративные ответы оцениваются отдельно по контрольным
                вопросам и критериям HR.
              </Notice>
            </section>
          )}
        </>
      )}
    </section>
  );
}
