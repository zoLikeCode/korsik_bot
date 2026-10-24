import { useEffect, useState } from "react";
import { api, errorText, items } from "../api";
import { ErrorBox, Loading, Notice, Status, useResource } from "../ui";
export async function revisionPublicationPins(ids: string[]) {
  return Promise.all(
    ids.map(async (id) => {
      const result = await api.get(`/knowledge/revisions/${id}`);
      return {
        revision_id: id,
        expected_extraction_revision_id:
          result.data.latest_extraction_revision_id,
        expected_content_fingerprint: result.data.content_fingerprint,
        expected_review_fingerprint: result.data.review_fingerprint,
      };
    }),
  );
}
export default function JointKnowledge({
  revision,
  selected,
  onChange,
  memberIds,
  onOpen,
  refreshToken,
  busy,
}: {
  revision: any;
  selected: string[];
  onChange: (ids: string[]) => void;
  memberIds: string[];
  onOpen: (id: string) => void;
  refreshToken: unknown;
  busy: boolean;
}) {
  const documents = useResource("/knowledge/documents"),
    [available, setAvailable] = useState<any[]>([]),
    [members, setMembers] = useState<any[]>([]),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(false);
  const memberKey = memberIds.filter((id) => id !== revision.id).join(",");
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    const ids = Array.from(
      new Set<string>([
        ...items(documents.data)
          .filter(
            (d) =>
              d.id !== revision.document_id &&
              d.latest_revision_id &&
              !["PURGED", "PURGE_PENDING"].includes(d.status),
          )
          .map((d) => d.latest_revision_id),
        ...memberKey.split(",").filter(Boolean),
      ]),
    );
    Promise.all(
      ids.map(async (id) => {
        const result = await api.get(`/knowledge/revisions/${id}`);
        return result.data;
      }),
    )
      .then((rows) => {
        if (active) {
          setAvailable(
            rows.filter((r) =>
              ["REVIEW_REQUIRED", "APPROVED"].includes(r.status),
            ),
          );
          setMembers(rows.filter((r) => memberKey.split(",").includes(r.id)));
        }
      })
      .catch((e) => active && setError(errorText(e)))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [documents.data, memberKey, refreshToken]);
  const names = new Map(items(documents.data).map((d) => [d.id, d.title]));
  if (!available.length && !memberKey && !error) return null;
  return (
    <details className="advanced" open={!!memberKey}>
      <summary>Совместная проверка источника и пояснений</summary>
      <ErrorBox error={error || documents.error} />
      <Notice>
        Выберите связанные черновики, например новую карту и пояснение HR.
        Сначала проверьте страницы каждого материала. Общая сборка и QA
        охватывают весь набор; согласование выполняется для каждой версии, а
        публикация — одной атомарной операцией.
      </Notice>
      {loading ? (
        <Loading />
      ) : (
        available.map((r) => (
          <label className="check-field" key={r.id}>
            <input
              type="checkbox"
              checked={selected.includes(r.id)}
              disabled={busy || revision.status === "PUBLISHED"}
              onChange={(e) =>
                onChange(
                  e.target.checked
                    ? [...selected, r.id]
                    : selected.filter((id) => id !== r.id),
                )
              }
            />
            {names.get(r.document_id) || r.filename || "Связанный материал"}{" "}
            <Status value={r.status} />
          </label>
        ))
      )}
      {members.length > 0 && (
        <section className="panel inset">
          <h3>В текущем наборе публикации</h3>
          <p>
            Сначала согласуйте текстовые пояснения, затем исходный файл с
            изображениями.
          </p>
          {members.map((r) => (
            <div className="list-row" key={r.id}>
              <div>
                <strong>
                  {names.get(r.document_id) || r.filename || "Материал набора"}
                </strong>
                <small>
                  {" "}
                  {r.revision_kind === "ARTICLE"
                    ? "Статья / пояснение"
                    : "Исходный файл"}
                </small>
              </div>
              <Status value={r.status} />
              <button className="text-button" onClick={() => onOpen(r.id)}>
                Открыть проверку
              </button>
            </div>
          ))}
        </section>
      )}
    </details>
  );
}
