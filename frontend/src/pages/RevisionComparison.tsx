import { useEffect, useState } from "react";
import { GitCompareArrows } from "lucide-react";
import { api, errorText, items } from "../api";
import { ErrorBox, Field, Loading, Notice, date, useResource } from "../ui";

type Pin = { revision_id: string; extraction_revision_id?: string | null };
type Comparison = {
  base: Pin;
  target: Pin;
  metadata?: {
    field: string;
    before: unknown;
    after: unknown;
    status?: string;
    historical_available?: { base: boolean; target: boolean };
  }[];
  metadata_history_unavailable?: string[];
  current_document_metadata?: Record<string, unknown>;
  limitations?: string[];
  truncation_reasons?: string[];
  pages?: any[];
  comparison?: { alignment?: string; semantic_diff?: boolean };
  summary?: Record<string, unknown>;
  limits?: Record<string, unknown>;
  truncated?: boolean;
};
const changeLabels: Record<string, string> = {
  ADDED: "Добавлено",
  REMOVED: "Удалено",
  CHANGED: "Изменено",
  MODIFIED: "Изменено",
  UNCHANGED: "Без изменений",
  MISSING: "Нет распознанного содержимого",
  UNAVAILABLE: "Сравнение недоступно",
  HISTORY_UNAVAILABLE: "История свойств не сохранена",
};
const metadataLabels: Record<string, string> = {
  filename: "Имя файла",
  file_name: "Имя файла",
  content_kind: "Тип материала",
  effective_from: "Действует с",
  effective_to: "Действует до",
  scope: "Аудитория",
  document_category: "Категория",
  page_count: "Количество страниц",
  title: "Название",
  source_dependencies: "Основания инструкции",
  sha256: "Отпечаток исходного файла",
  revision_kind: "Вид редакции",
  article_kind: "Вид статьи",
  scope_fingerprint: "Отпечаток аудитории",
};
function valueText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
function revisionName(revision: any, index: number) {
  return `${revision.filename || revision.file_name || `Версия ${revision.revision_no || index + 1}`} · ${date(revision.created_at)}`;
}
const categoryLabels: Record<string, string> = {
  REGULATION: "Регламент",
  INSTRUCTION: "Памятка / инструкция",
  OVERVIEW: "Обзор",
  MAP: "Карта / схема",
  OTHER: "Другое",
};
const audienceLabels: Record<string, string> = {
  OFFICE: "Офис",
  PRODUCTION: "Производство",
  OTHER: "Другие категории",
  PREBOARDING: "До выхода на работу",
  ACTIVE: "После выхода на работу",
  EMPLOYEE: "Сотрудники",
  HR_ADMIN: "Администраторы HR",
  HR_SPECIALIST: "Специалисты HR",
  MANAGER: "Руководители подразделений",
  HEAD_TALENT: "Руководитель подбора персонала",
};
function humanDate(value: unknown) {
  if (!value) return "—";
  const parsed = new Date(String(value));
  if (Number.isNaN(parsed.getTime())) return valueText(value);
  if (/^\d{4}-\d{2}-\d{2}$/.test(String(value)))
    return parsed.toLocaleDateString("ru-RU", {
      timeZone: "Europe/Moscow",
      dateStyle: "medium",
    });
  return (
    parsed.toLocaleString("ru-RU", {
      timeZone: "Europe/Moscow",
      dateStyle: "medium",
      timeStyle: "short",
    }) + " (МСК)"
  );
}
function Grid({ grid }: { grid: unknown }) {
  if (!Array.isArray(grid) || !grid.length)
    return <p className="muted">Нет таблицы</p>;
  return (
    <div className="table-wrap">
      <table>
        <tbody>
          {grid.map((row: unknown, i: number) => (
            <tr key={i}>
              {(Array.isArray(row) ? row : [row]).map(
                (cell: unknown, j: number) => (
                  <td key={j}>{valueText(cell)}</td>
                ),
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
function BeforeAfter({ before, after }: { before: unknown; after: unknown }) {
  return (
    <div className="revision-diff-columns">
      <div>
        <h5>Базовая редакция</h5>
        <div className="preserve-lines">{valueText(before)}</div>
      </div>
      <div>
        <h5>Выбранная версия</h5>
        <div className="preserve-lines">{valueText(after)}</div>
      </div>
    </div>
  );
}
export default function RevisionComparison({
  targetId,
  activeRevisionId,
  revisions,
}: {
  targetId: string;
  activeRevisionId?: string;
  revisions: any[];
}) {
  const available = revisions.filter((revision) => revision.id !== targetId);
  const [baseId, setBaseId] = useState("");
  const [result, setResult] = useState<Comparison | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [showUnchanged, setShowUnchanged] = useState(false);
  const scopesShown = !!result?.metadata?.some(
    (change) => change.field === "scope",
  );
  const sites = useResource(scopesShown ? "/sites" : null);
  const legalEntities = useResource(scopesShown ? "/legal-entities" : null);
  function humanMetadata(field: string, value: unknown): string {
    if (value === null || value === undefined || value === "") return "—";
    if (field === "effective_from" || field === "effective_to")
      return humanDate(value);
    if (field === "document_category")
      return categoryLabels[String(value)] || valueText(value);
    if (field === "revision_kind")
      return (
        (
          { FILE: "Загруженный файл", ARTICLE: "Текстовая статья" } as Record<
            string,
            string
          >
        )[String(value)] || valueText(value)
      );
    if (field === "article_kind")
      return (
        (
          {
            GENERAL: "Информационная статья",
            APPROVED_INSTRUCTION: "Согласованная инструкция",
          } as Record<string, string>
        )[String(value)] || valueText(value)
      );
    if (field !== "scope" || typeof value !== "object") return valueText(value);
    const scope = value as Record<string, any>;
    const names = (ids: string[] = [], rows: any[]) =>
      ids
        .map(
          (id) =>
            rows.find((row) => row.id === id)?.name || `Идентификатор ${id}`,
        )
        .join(", ");
    const labels = (values: string[] = []) =>
      values.map((entry) => audienceLabels[entry] || entry).join(", ");
    const legalIds = scope.legal_entity_ids?.length
      ? scope.legal_entity_ids
      : scope.legal_entity_id
        ? [scope.legal_entity_id]
        : [];
    return [
      `Площадки: ${names(scope.site_ids || [], items(sites.data)) || "все"}`,
      `Юридические лица: ${names(legalIds, items(legalEntities.data)) || "без дополнительного ограничения"}`,
      `Категории сотрудников: ${labels(scope.employment_types) || "все"}`,
      `Этапы адаптации: ${labels(scope.employment_stages) || "все"}`,
      `Роли: ${labels(scope.audience_roles) || "без дополнительного ограничения"}`,
      ...(scope.sensitivity
        ? [
            `Доступ: ${({ STANDARD: "обычный", CONFIDENTIAL: "ограниченный", RESTRICTED: "ограниченный", HR_ONLY: "только HR" } as Record<string, string>)[scope.sensitivity] || scope.sensitivity}`,
          ]
        : []),
    ].join("\n");
  }
  useEffect(() => {
    setBaseId(
      available.some((revision) => revision.id === activeRevisionId)
        ? activeRevisionId!
        : available[0]?.id || "",
    );
    setResult(null);
    setError("");
  }, [targetId, activeRevisionId]);
  async function compare(pinned = false) {
    setBusy(true);
    setError("");
    try {
      const query = new URLSearchParams({ base_revision_id: baseId });
      if (
        pinned &&
        result?.base.revision_id === baseId &&
        result?.target.revision_id === targetId
      ) {
        if (result.target.extraction_revision_id)
          query.set(
            "extraction_revision_id",
            result.target.extraction_revision_id,
          );
        if (result.base.extraction_revision_id)
          query.set(
            "base_extraction_revision_id",
            result.base.extraction_revision_id,
          );
      }
      const response = await api.get<Comparison>(
        `/knowledge/revisions/${targetId}/compare?${query}`,
      );
      setResult(response.data);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  if (!available.length)
    return (
      <section className="panel inset">
        <h3>Сравнение редакций</h3>
        <p className="muted">
          У документа пока одна версия. После загрузки новой редакции здесь
          можно будет сравнить текст, таблицы и изображения с предыдущей.
        </p>
      </section>
    );
  const allPages = result?.pages || [];
  const metadata = (result?.metadata || []).filter(
    (change) => change.status !== "UNCHANGED",
  );
  const visiblePages = allPages.filter(
    (page) => showUnchanged || page.status !== "UNCHANGED",
  );
  return (
    <section className="panel inset revision-comparison">
      <div className="panel-head">
        <h3>
          <GitCompareArrows size={19} /> Что изменилось в документе
        </h3>
      </div>
      <p className="muted">
        Сравните выбранную сверху версию с другой редакцией перед проверкой и
        публикацией.
      </p>
      <div className="button-row wrap">
        <Field label="Сравнить с редакцией">
          <select
            value={baseId}
            onChange={(event) => {
              setBaseId(event.target.value);
              setResult(null);
              setError("");
            }}
          >
            <option value="">Выберите прежнюю редакцию</option>
            {available.map((revision, index) => (
              <option value={revision.id} key={revision.id}>
                {revisionName(revision, index)}
                {revision.id === activeRevisionId ? " · Действующая" : ""}
              </option>
            ))}
          </select>
        </Field>
        <button
          type="button"
          className="button secondary"
          disabled={!baseId || busy}
          onClick={() => compare(false)}
        >
          Сравнить версии
        </button>
        {result && (
          <button
            type="button"
            className="text-button"
            disabled={busy}
            onClick={() => compare(true)}
          >
            Повторить с теми же распознаваниями
          </button>
        )}
      </div>
      <ErrorBox error={error} />
      {busy && <Loading />}
      {result && !busy && (
        <>
          <Notice>
            Сравнение сопоставляет физические номера страниц и виды блоков.
            Перенос текста на другую страницу может выглядеть как удаление и
            добавление. Это проверка содержимого, а смысл изменений оценивает
            HR.
          </Notice>
          {result.truncated && (
            <div className="notice warning">
              Результат сокращён сервером: часть страниц, текста или таблиц не
              показана. Откройте обе версии целиком перед согласованием.
            </div>
          )}
          {!!result.metadata_history_unavailable?.length && (
            <div className="notice warning">
              Для одной из редакций не сохранена история названия, аудитории или
              категории. Отсутствие снимка не означает отсутствие изменений;
              текущие свойства доступны отдельно в подробностях.
            </div>
          )}
          {!!metadata.length && (
            <div>
              <h4>Свойства документа</h4>
              {metadata.map((change, index) => (
                <details
                  key={`${change.field}-${index}`}
                  open={!["sha256", "scope_fingerprint"].includes(change.field)}
                >
                  <summary>
                    {metadataLabels[change.field] || change.field} ·{" "}
                    {changeLabels[change.status || "CHANGED"] || change.status}
                  </summary>
                  <BeforeAfter
                    before={
                      change.historical_available?.base === false
                        ? "История не сохранена"
                        : humanMetadata(change.field, change.before)
                    }
                    after={
                      change.historical_available?.target === false
                        ? "История не сохранена"
                        : humanMetadata(change.field, change.after)
                    }
                  />
                </details>
              ))}
            </div>
          )}
          <div className="button-row wrap">
            <h4>
              Страницы:{" "}
              {Number(result.summary?.pages_added || 0) +
                Number(result.summary?.pages_removed || 0) +
                Number(result.summary?.pages_changed || 0)}{" "}
              с изменениями из {allPages.length} показанных
            </h4>
            <label className="check-field">
              <input
                type="checkbox"
                checked={showUnchanged}
                onChange={(event) => setShowUnchanged(event.target.checked)}
              />
              Показывать страницы без изменений
            </label>
          </div>
          {!!result.summary?.unavailable_pages && (
            <div className="notice warning">
              Не удалось сравнить страниц:{" "}
              {String(result.summary.unavailable_pages)}. Сначала завершите их
              распознавание или проверьте оригиналы вручную.
            </div>
          )}
          {!visiblePages.length && (
            <p className="muted">
              В показанном объёме распознанного содержимого изменений нет.
            </p>
          )}
          {visiblePages.map((page) => (
            <details key={page.page_no} open={page.status !== "UNCHANGED"}>
              <summary>
                Страница {page.page_no} ·{" "}
                {changeLabels[page.status] || page.status}
              </summary>
              {page.before_render_sha256 !== page.after_render_sha256 && (
                <p className="muted">
                  Вид страницы изменился. Сверьте полные оригиналы обеих
                  редакций; одинаковый распознанный текст не исключает изменений
                  на изображении.
                </p>
              )}
              {page.comparison_complete === false && (
                <p className="muted">
                  Есть нераспознанная страница: отсутствие изменений не
                  подтверждено.
                </p>
              )}
              {page.text?.changes?.some(
                (change: any) =>
                  change.contains_numbers || change.contains_negation,
              ) && (
                <div className="notice warning">
                  В изменённых фрагментах есть числа или отрицания. Проверьте
                  сроки, суммы и запреты особенно внимательно.
                </div>
              )}
              {page.text && page.text.status !== "UNCHANGED" && (
                <div>
                  <h4>
                    Текст · {changeLabels[page.text.status] || page.text.status}
                  </h4>
                  <BeforeAfter
                    before={page.text.before}
                    after={page.text.after}
                  />
                </div>
              )}
              {(page.tables || [])
                .filter(
                  (table: any) => showUnchanged || table.status !== "UNCHANGED",
                )
                .map((table: any, index: number) => (
                  <div key={index}>
                    <h4>
                      Таблица {table.position || index + 1} ·{" "}
                      {changeLabels[table.status] || table.status}
                    </h4>
                    {table.alignment_ambiguous && (
                      <p className="muted">
                        Количество таблиц различается: пары сопоставлены по
                        порядку, сверьте оригиналы.
                      </p>
                    )}
                    <div className="revision-diff-columns">
                      <div>
                        <h5>Базовая редакция</h5>
                        <Grid grid={table.before_grid} />
                      </div>
                      <div>
                        <h5>Выбранная версия</h5>
                        <Grid grid={table.after_grid} />
                      </div>
                    </div>
                  </div>
                ))}
              {(page.figures || [])
                .filter(
                  (figure: any) =>
                    showUnchanged || figure.status !== "UNCHANGED",
                )
                .map((figure: any, index: number) => (
                  <div key={index}>
                    <h4>
                      Изображение {figure.position || index + 1} ·{" "}
                      {changeLabels[figure.status] || figure.status}
                    </h4>
                    <p className="muted">
                      {figure.alignment_ambiguous
                        ? "Количество изображений различается; пары сопоставлены по порядку. "
                        : ""}
                      Сопоставление по содержимому файла и описанию; визуальный
                      смысл нужно проверить на оригиналах страниц.
                    </p>
                    {(figure.before_description ||
                      figure.after_description) && (
                      <BeforeAfter
                        before={figure.before_description}
                        after={figure.after_description}
                      />
                    )}
                    <details>
                      <summary>Отпечатки изображений</summary>
                      <BeforeAfter
                        before={figure.before_image_sha256}
                        after={figure.after_image_sha256}
                      />
                    </details>
                  </div>
                ))}
              {page.status === "UNCHANGED" && (
                <p className="muted">Сопоставленные блоки совпадают.</p>
              )}
            </details>
          ))}
          <details>
            <summary>Закреплённые версии и ограничения сравнения</summary>
            <dl>
              <dt>Базовая редакция / распознавание</dt>
              <dd>
                {result.base.revision_id} /{" "}
                {result.base.extraction_revision_id || "Нет"}
              </dd>
              <dt>Выбранная редакция / распознавание</dt>
              <dd>
                {result.target.revision_id} /{" "}
                {result.target.extraction_revision_id || "Нет"}
              </dd>
            </dl>
            {!!result.current_document_metadata && (
              <>
                <h4>Текущие свойства документа (не исторический снимок)</h4>
                <dl>
                  {Object.entries(result.current_document_metadata)
                    .filter(([key]) => key !== "is_historical")
                    .map(([key, value]) => (
                      <div key={key}>
                        <dt>{metadataLabels[key] || key}</dt>
                        <dd className="preserve-lines">
                          {humanMetadata(key, value)}
                        </dd>
                      </div>
                    ))}
                </dl>
              </>
            )}
            {result.limitations?.map((limitation) => (
              <p key={limitation}>{limitation}</p>
            ))}
            <pre>
              {JSON.stringify(
                {
                  summary: result.summary,
                  limits: result.limits,
                  truncation_reasons: result.truncation_reasons,
                },
                null,
                2,
              )}
            </pre>
          </details>
        </>
      )}
    </section>
  );
}
