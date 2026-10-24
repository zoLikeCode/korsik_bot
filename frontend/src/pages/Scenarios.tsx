import { ScenarioMaterials } from "../ScenarioMaterials";
import RussiaTimeZoneField from "../RussiaTimeZoneField";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  Controls,
  type Node,
  type Edge,
  type ReactFlowInstance,
  Position,
  MarkerType,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import {
  Plus,
  Play,
  Send,
  GitBranch,
  ChevronLeft,
  Trash2,
  GripVertical,
  SlidersHorizontal,
  X,
  Ellipsis,
} from "lucide-react";
import { api, items, errorText, request } from "../api";
import { useActor } from "../App";
import {
  statusLabels,
  PageTitle,
  useResource,
  ErrorBox,
  Success,
  Loading,
  Empty,
  Status,
  Modal,
  Field,
  Notice,
  JSONView,
  date,
} from "../ui";
import {
  patchScenarioStep,
  answerWithLabel,
  simulationAnswerValue,
  simulationAnswerChoice,
  type Graph,
  type Step,
  newStep,
  insertStep,
  removeStep,
  connectSteps,
  removeTransition,
  moveStep,
  checkGraph,
  nodeNames,
  scenarioHasPublishedVersion,
  scenarioDraftPayload,
} from "../scenario";
import {
  LegacyStepInspector,
  legacyNodeKinds,
  legacyNewStep,
} from "../LegacyScenarioInspector";
import {
  rememberFlowNodeMeasurements,
  type FlowNodeMeasurements,
} from "../scenarioFlowMeasurements";

const connectionLineStyle = {
  stroke: "var(--scenario-edge-selected)",
  strokeWidth: 2.5,
};
const defaultEdgeOptions = {
  interactionWidth: 24,
  labelBgPadding: [6, 4] as [number, number],
  labelBgBorderRadius: 5,
  labelBgStyle: { fill: "var(--scenario-edge-label-bg)" },
  labelStyle: {
    fill: "var(--scenario-edge-label)",
    fontSize: 12,
    fontWeight: 600,
  },
};
export default function Scenarios() {
  const state = useResource("/scenarios");
  const [scenario, setScenario] = useState<any>(null),
    [error, setError] = useState(""),
    [creating, setCreating] = useState(false),
    [name, setName] = useState(""),
    [management, setManagement] = useState<{
      scenario: any;
      action: "rename" | "delete";
    } | null>(null);
  return (
    <div className="scenario-page">
      <PageTitle
        title="Конструктор сценариев"
        action={
          <button
            className="button primary"
            onClick={() => {
              setName("");
              setCreating(true);
            }}
          >
            <Plus size={18} />
            Новый сценарий
          </button>
        }
      />
      <ErrorBox error={state.error || error} />
      {state.loading ? (
        <Loading />
      ) : scenario ? (
        <>
          <button
            className="text-button scenario-list-back"
            onClick={() => {
              setScenario(null);
              state.reload();
            }}
          >
            <ChevronLeft size={17} />
            Все сценарии
          </button>
          <Program key={scenario.id} scenario={scenario} />
        </>
      ) : items(state.data).length ? (
        <div className="document-grid scenario-card-grid">
          {items(state.data).map((s) => (
            <ScenarioCard
              key={s.id}
              scenario={s}
              onOpen={() => setScenario(s)}
              onRename={() => setManagement({ scenario: s, action: "rename" })}
              onDelete={() => setManagement({ scenario: s, action: "delete" })}
            />
          ))}
        </div>
      ) : (
        <Empty title="Создайте первый сценарий" />
      )}
      {management && (
        <ScenarioManagement
          scenario={management.scenario}
          action={management.action}
          onClose={() => setManagement(null)}
          onDone={() => {
            setManagement(null);
            state.reload();
          }}
        />
      )}
      {creating && (
        <Modal title="Новый сценарий" onClose={() => setCreating(false)}>
          <form
            onSubmit={async (e) => {
              e.preventDefault();
              setError("");
              try {
                const r = await api.post("/scenarios", { name });
                state.reload();
                setScenario(r.data);
                setCreating(false);
              } catch (e) {
                setError(errorText(e));
              }
            }}
          >
            <Field label="Название сценария">
              <input
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="Например, первый рабочий день"
              />
            </Field>
            <div className="modal-actions">
              <button className="button primary">Создать сценарий</button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}
export function ScenarioCard({
  scenario,
  onOpen,
  onRename,
  onDelete,
}: {
  scenario: any;
  onOpen: () => void;
  onRename?: () => void;
  onDelete?: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const actions = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!menuOpen) return;
    const close = (event: PointerEvent) => {
      if (!actions.current?.contains(event.target as globalThis.Node))
        setMenuOpen(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [menuOpen]);
  const condition = scenario.start_policy?.condition;
  const launch =
    (
      {
        ON_BOT_START: "При запуске",
        START_DATE: "Дата выхода",
        SCHEDULED: "По выбранному времени",
        RECURRING: "Повторять",
      } as Record<string, string>
    )[condition] || "Задано в версии";
  const published = scenarioHasPublishedVersion(scenario);
  return (
    <article className="document-card scenario-card">
      <button
        type="button"
        className="scenario-card-main"
        aria-label={`Сценарий: ${scenario.name}`}
        onClick={onOpen}
      >
        <div className="document-top">
          <span className="document-icon">
            <GitBranch size={25} />
          </span>
        </div>
        <span className={`badge ${published ? "green" : "gray"}`}>
          {published ? "Опубликован" : "Не опубликован"}
        </span>
        <h3>{scenario.name}</h3>
        <p>Условие запуска: {launch}</p>
        <div className="document-bottom document-updated">
          <span>
            Обновлён {date(scenario.updated_at || scenario.created_at)}
          </span>
        </div>
      </button>
      {scenario.can_edit !== false && (onRename || onDelete) && (
        <div
          className="scenario-card-actions"
          ref={actions}
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              setMenuOpen(false);
              event.stopPropagation();
            }
          }}
        >
          <button
            type="button"
            className="icon-button"
            aria-label={`Действия со сценарием: ${scenario.name}`}
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen(!menuOpen)}
          >
            <Ellipsis size={20} />
          </button>
          {menuOpen && (
            <ScenarioCardMenu
              onRename={onRename}
              onDelete={onDelete}
              onClose={() => setMenuOpen(false)}
            />
          )}
        </div>
      )}
    </article>
  );
}
export function ScenarioCardMenu({
  onRename,
  onDelete,
  onClose,
}: {
  onRename?: () => void;
  onDelete?: () => void;
  onClose: () => void;
}) {
  return (
    <div
      className="scenario-card-menu"
      role="group"
      aria-label="Действия со сценарием"
    >
      {onRename && (
        <button
          type="button"
          onClick={() => {
            onClose();
            onRename();
          }}
        >
          Переименовать
        </button>
      )}
      {onDelete && (
        <button
          type="button"
          className="danger"
          onClick={() => {
            onClose();
            onDelete();
          }}
        >
          Удалить
        </button>
      )}
    </div>
  );
}
export async function deleteScenarioResource(
  path: string,
  etag: string | null,
  transport = request,
) {
  return transport(path, { method: "DELETE" }, etag);
}
function ScenarioManagement({
  scenario,
  action,
  onClose,
  onDone,
}: {
  scenario: any;
  action: "rename" | "delete";
  onClose: () => void;
  onDone: () => void;
}) {
  const [name, setName] = useState(scenario.name),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  return (
    <Modal
      title={
        action === "rename" ? "Переименовать сценарий" : "Удалить сценарий"
      }
      onClose={onClose}
    >
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          setBusy(true);
          setError("");
          try {
            const etag =
              scenario.etag ||
              (await api.get(`/scenarios/${scenario.id}`)).etag;
            if (action === "rename")
              await api.patch(
                `/scenarios/${scenario.id}`,
                { name: name.trim() },
                etag,
              );
            else
              await deleteScenarioResource(`/scenarios/${scenario.id}`, etag);
            onDone();
          } catch (e) {
            setError(errorText(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        {action === "rename" ? (
          <Field label="Название сценария">
            <input
              required
              maxLength={160}
              value={name}
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
        ) : (
          <p>
            Удалить сценарий «{scenario.name}» из списка? Уже назначенные
            сотрудникам версии и история сохранятся.
          </p>
        )}
        <ErrorBox error={error} />
        <div className="modal-actions">
          <button
            type="button"
            className="button secondary"
            disabled={busy}
            onClick={onClose}
          >
            Отмена
          </button>
          <button
            className={`button ${action === "delete" ? "danger" : "primary"}`}
            disabled={
              busy ||
              (action === "rename" &&
                (!name.trim() || name.trim() === scenario.name))
            }
          >
            {action === "rename" ? "Сохранить название" : "Удалить сценарий"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
function Program({ scenario }: { scenario: any }) {
  const versions = useResource(`/scenarios/${scenario.id}/versions`);
  const [selected, setSelected] = useState(""),
    [settingsOpen, setSettingsOpen] = useState(false),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  useEffect(() => {
    const list = items(versions.data);
    if (list.length && !list.some((x) => x.id === selected))
      setSelected(
        list.find((x) => x.status === "DRAFT")?.id || list[list.length - 1].id,
      );
  }, [versions.data]);
  return (
    <>
      <h2 className="scenario-program-name">{scenario.name}</h2>
      {scenario.can_edit === false && (
        <Notice>Редактирование этой схемы доступно центральному HR.</Notice>
      )}
      <div className="version-bar">
        <div className="scenario-version-controls">
          <select
            aria-label="Версия схемы"
            value={selected}
            onChange={(e) => setSelected(e.target.value)}
          >
            {items(versions.data).map((v: any) => (
              <option key={v.id} value={v.id}>
                {v.name ||
                  `Версия ${v.version_number || v.number || v.id.slice(0, 6)}`}{" "}
                · {statusLabels[v.status] || v.status}
              </option>
            ))}
          </select>
          <button
            className="button secondary"
            disabled={!selected}
            onClick={() => setSettingsOpen(true)}
          >
            <SlidersHorizontal size={17} />
            Настройки сценария
          </button>
        </div>
        <button
          className="button secondary"
          disabled={busy || scenario.can_edit === false}
          onClick={async () => {
            setBusy(true);
            setError("");
            try {
              const r = await api.post(
                `/scenarios/${scenario.id}/versions`,
                scenarioDraftPayload(
                  items(versions.data).find(
                    (version) => version.id === selected,
                  ),
                ),
              );
              versions.reload();
              setSelected(r.data.id);
            } catch (e) {
              setError(errorText(e));
            } finally {
              setBusy(false);
            }
          }}
        >
          <Plus size={17} />
          {selected ? "Новый черновик" : "Создать первый черновик"}
        </button>
      </div>
      <ErrorBox error={error || versions.error} />
      {selected ? (
        <ScenarioEditor
          key={selected}
          id={selected}
          canEdit={scenario.can_edit !== false}
          onPublished={() => versions.reload()}
          onDeleted={() => {
            setSettingsOpen(false);
            setSelected("");
            versions.reload();
          }}
          settingsOpen={settingsOpen}
          onSettingsClose={() => setSettingsOpen(false)}
        />
      ) : (
        <Empty title="У схемы пока нет версии">
          Создайте черновик и добавьте первые блоки.
        </Empty>
      )}
    </>
  );
}
function ScenarioEditor({
  id,
  onPublished,
  onDeleted,
  canEdit,
  settingsOpen,
  onSettingsClose,
}: {
  id: string;
  canEdit: boolean;
  onPublished: () => void;
  onDeleted: () => void;
  settingsOpen: boolean;
  onSettingsClose: () => void;
}) {
  const canPublish = useActor().role === "HR_ADMIN";
  const [sourceReview, setSourceReview] = useState<any>(null);
  const [sourceAck, setSourceAck] = useState(false);
  const [sourceReason, setSourceReason] = useState("");
  const state = useResource(`/scenario-versions/${id}`),
    sites = useResource("/sites");
  const [graph, setGraph] = useState<Graph | null>(null),
    [selected, setSelected] = useState(""),
    [dirty, setDirty] = useState(false),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [result, setResult] = useState<any>(null),
    [simulation, setSimulation] = useState(false),
    [versionName, setVersionName] = useState(""),
    [versionStatus, setVersionStatus] = useState(""),
    [deleteConfirm, setDeleteConfirm] = useState(false),
    [settingsGraph, setSettingsGraph] = useState<Graph | null>(null);
  const draftRef = useRef<ScenarioDraftState>({
    graph: null,
    dirty: false,
    etag: null,
    pending: null,
  });
  const [flow, setFlow] = useState<ReactFlowInstance | null>(null);
  const nodeMeasurements = useRef<FlowNodeMeasurements>(new Map());
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const inspectorButton = useRef<HTMLButtonElement>(null);
  const inspectorPanel = useRef<HTMLElement>(null);
  const inspectorId = `scenario-inspector-${id}`;
  function closeInspector() {
    setInspectorOpen(false);
    inspectorButton.current?.focus({ preventScroll: true });
  }
  useEffect(() => {
    if (inspectorOpen) inspectorPanel.current?.focus({ preventScroll: true });
  }, [inspectorOpen]);
  const [selectedEdge, setSelectedEdge] = useState<{
    source: string;
    index: number;
  } | null>(null);
  const status = versionStatus || state.data?.status;
  const locked =
    busy || !canEdit || !["DRAFT", "IN_REVIEW", "APPROVED"].includes(status);
  useEffect(() => {
    if (state.data) {
      setVersionName(state.data.name || "Новая редакция");
      setVersionStatus(state.data.status);
      const g = state.data.graph || state.data.definition;
      draftRef.current.graph = g;
      draftRef.current.dirty = false;
      draftRef.current.etag = state.etag;
      setGraph(g);
      setSelected(g?.steps?.[0]?.step_key || "");
      setDirty(false);
    }
  }, [state.data, state.etag]);
  const active = graph?.steps.find((s) => s.step_key === selected);
  const Inspector =
    graph?.editor_mode === "SIMPLE" ? StepInspector : LegacyStepInspector;
  const update = (g: Graph) => {
    draftRef.current.graph = g;
    draftRef.current.dirty = true;
    setGraph(g);
    setDirty(true);
    setSuccess("");
    setResult(null);
  };
  const updateStep = (stepKey: string, patch: Partial<Step>) => {
    const current = draftRef.current.graph;
    if (!current || locked) return;
    const next = patchScenarioStep(current, stepKey, patch);
    if (next !== current) update(next);
  };
  const nodes: Node[] = useMemo(
    () =>
      graph?.steps.map((s, i) => ({
        id: s.step_key,
        measured: nodeMeasurements.current.get(s.step_key),
        position: graph.editor_layout?.nodes?.find(
          (n: any) => n.step_key === s.step_key,
        ) || { x: (i % 3) * 320, y: Math.floor(i / 3) * 180 },
        data: {
          label: <ScenarioNodeLabel step={s} />,
        },
        selected: selected === s.step_key,
        sourcePosition: Position.Right,
        targetPosition: Position.Left,
        className: `scenario-flow-node kind-${s.kind} ${selected === s.step_key ? "selected" : ""}`,
        draggable: !locked,
      })) || [],
    [graph, selected, locked],
  );
  const edges: Edge[] = useMemo(
    () =>
      graph?.steps.flatMap((s) =>
        (s.transitions || []).map((t: any, i: number) => ({
          id: `${s.step_key}-${i}`,
          source: s.step_key,
          target: t.target_step_key,
          animated: false,
          selected:
            selectedEdge?.source === s.step_key && selectedEdge.index === i,
          data: { transitionIndex: i },
          markerEnd: {
            type: MarkerType.ArrowClosed,
            width: 18,
            height: 18,
            color:
              selectedEdge?.source === s.step_key && selectedEdge.index === i
                ? "var(--scenario-edge-selected)"
                : "var(--scenario-edge)",
          },
          label:
            graph.editor_mode === "SIMPLE"
              ? undefined
              : (
                  {
                    ALWAYS: "Всегда",
                    ANSWER: "Ответ",
                    EMPLOYEE: "Сотрудник",
                    SIGNAL: "Подтверждение",
                    ALL: "Все условия",
                    ANY: "Любое условие",
                    NOT: "Не выполняется",
                  } as Record<string, string>
                )[t.condition?.kind || "ALWAYS"],
          type: "smoothstep",
        })),
      ) || [],
    [graph, selectedEdge],
  );
  function addNode(kind: string, position?: { x: number; y: number }) {
    if (locked || !graph) return;
    const step =
      graph.editor_mode === "SIMPLE" ? newStep(kind) : legacyNewStep(kind);
    step.sequence_no =
      Math.max(0, ...graph.steps.map((row) => row.sequence_no)) + 1;
    const selectedPosition = graph.editor_layout?.nodes?.find(
      (row: any) => row.step_key === selected,
    );
    const placed = position || {
      x: (selectedPosition?.x || 0) + 340,
      y: (selectedPosition?.y || 0) + 120,
    };
    update(moveStep(insertStep(graph, step), step.step_key, placed));
    setSelected(step.step_key);
    setSelectedEdge(null);
  }
  async function persistDraft() {
    await persistScenarioDraft(id, draftRef.current, api, () => {
      setDirty(false);
      setVersionStatus("DRAFT");
      setError("");
    });
  }
  useEffect(() => {
    if (!dirty || locked) return;
    const timer = window.setTimeout(() => {
      persistDraft().catch((e) => setError(errorText(e)));
    }, 700);
    return () => window.clearTimeout(timer);
  }, [graph, dirty, locked]);
  useEffect(() => {
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (draftRef.current.dirty || draftRef.current.pending) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", beforeUnload);
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      // Flush internal navigation that happens before the debounce expires.
      persistDraft().catch(() => undefined);
    };
  }, []);
  useEffect(() => {
    if (settingsOpen && graph) setSettingsGraph(structuredClone(graph));
  }, [settingsOpen]);
  async function saveSettings(name: string) {
    if (!settingsGraph) return;
    setBusy(true);
    setError("");
    try {
      await persistDraft();
      const response = await api.put(`/scenario-versions/${id}`, { name: name.trim(), graph: settingsGraph }, draftRef.current.etag);
      draftRef.current = { ...draftRef.current, graph: settingsGraph, dirty: false, etag: response.etag };
      setGraph(settingsGraph);
      setDirty(false);
      setVersionName(response.data.name || name.trim());
      setVersionStatus(response.data.status || "DRAFT");
      onSettingsClose();
      onPublished();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function deleteDraft() {
    setBusy(true);
    setError("");
    try {
      await persistDraft();
      await deleteScenarioResource(
        `/scenario-versions/${id}`,
        draftRef.current.etag,
      );
      draftRef.current.dirty = false;
      setDirty(false);
      onSettingsClose();
      onDeleted();
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function openSimulation() {
    setBusy(true);
    setError("");
    try {
      await persistDraft();
      setSimulation(true);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  async function publish(acknowledgement?: {
    fingerprint: string;
    reason: string;
  }) {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      const local = checkGraph(draftRef.current.graph!);
      if (local.length) {
        setError(local.join("; "));
        return;
      }
      await persistDraft();
      const result = await publishScenario(id, acknowledgement);
      if (result.kind === "invalid") {
        setResult(result.validation);
        return;
      }
      if (result.kind === "source_review") {
        setSourceReview(result.validation);
        setSourceAck(false);
        setSourceReason("");
        return;
      }
      setSourceReview(null);
      setResult(null);
      setVersionStatus("PUBLISHED");
      state.reload();
      onPublished();
      setSuccess("Программа опубликована для новых назначений");
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  if (state.loading || !graph)
    return (
      <>
        <ErrorBox error={state.error} />
        <Loading />
      </>
    );
  return (
    <>
      <ScenarioActions
        busy={busy}
        canPublish={
          canEdit &&
          canPublish &&
          ["DRAFT", "IN_REVIEW", "APPROVED"].includes(status)
        }
        onSimulation={openSimulation}
        onPublish={() => publish()}
      />
      <ErrorBox error={error || state.error} />
      {success && <Success>{success}</Success>}
      {settingsOpen && (
        <Modal
          title="Настройки сценария"
          onClose={() => {
            setDeleteConfirm(false);
            onSettingsClose();
          }}
        >
          <ScenarioVersionSettings
            name={versionName}
            status={status}
            busy={busy}
            canEdit={canEdit}
            onRename={saveSettings}
            onDelete={() => setDeleteConfirm(true)}
          >
            {settingsGraph?.editor_mode === "SIMPLE" && <ScenarioSettings graph={settingsGraph} locked={locked} onChange={setSettingsGraph} />}
          </ScenarioVersionSettings>
          {deleteConfirm && (
            <section className="scenario-delete-confirm">
              <p>
                Удалить черновик «{versionName}»? Опубликованные версии
                сохранятся.
              </p>
              <div className="button-row">
                <button
                  type="button"
                  className="button secondary"
                  disabled={busy}
                  onClick={() => setDeleteConfirm(false)}
                >
                  Отмена
                </button>
                <button
                  type="button"
                  className="button danger"
                  disabled={busy}
                  onClick={deleteDraft}
                >
                  Удалить черновик
                </button>
              </div>
            </section>
          )}
          <ErrorBox error={error} />
        </Modal>
      )}
      <div className="scenario-editor">
        <button
          ref={inspectorButton}
          type="button"
          className="button secondary scenario-inspector-toggle"
          aria-expanded={inspectorOpen}
          aria-controls={inspectorId}
          aria-label={
            inspectorOpen
              ? "Скрыть панель редактирования"
              : "Открыть панель редактирования"
          }
          onClick={() =>
            inspectorOpen ? closeInspector() : setInspectorOpen(true)
          }
        >
          <SlidersHorizontal size={18} />
          Параметры
        </button>
        <div className="editor-stage">
          <div
            className="flow-canvas"
            aria-label="Схема сценария"
            onDragOver={(event) => {
              if (!locked) {
                event.preventDefault();
                event.dataTransfer.dropEffect = "copy";
              }
            }}
            onDrop={(event) => {
              event.preventDefault();
              const kind = event.dataTransfer.getData("korsik/node");
              if (nodeNames[kind] && flow)
                addNode(
                  kind,
                  flow.screenToFlowPosition({
                    x: event.clientX,
                    y: event.clientY,
                  }),
                );
            }}
          >
            <ReactFlow
              proOptions={{ hideAttribution: true }}
              nodes={nodes}
              edges={edges}
              onInit={setFlow}
              onNodeClick={(_, node) => {
                setSelected(node.id);
                setSelectedEdge(null);
              }}
              onPaneClick={() => setSelectedEdge(null)}
              onNodesChange={(changes) => {
                rememberFlowNodeMeasurements(nodeMeasurements.current, changes);
                if (locked) return;
                const positions = changes.filter(
                  (change) => change.type === "position" && change.position,
                );
                if (!positions.length) return;
                let next = graph;
                for (const change of positions)
                  if (change.type === "position" && change.position)
                    next = moveStep(next, change.id, change.position);
                update(next);
              }}
              onConnect={(connection) => {
                if (locked || !connection.source || !connection.target) return;
                try {
                  update(
                    connectSteps(graph, connection.source, connection.target),
                  );
                  setSelectedEdge(null);
                  setError("");
                } catch (e) {
                  setError(errorText(e));
                }
              }}
              onEdgeClick={(_, edge) => {
                setSelected(edge.source);
                setSelectedEdge({
                  source: edge.source,
                  index: edge.data?.transitionIndex as number,
                });
              }}
              fitView
              defaultEdgeOptions={defaultEdgeOptions}
              connectionLineStyle={connectionLineStyle}
              minZoom={0.1}
              maxZoom={2}
              nodesConnectable={!locked}
              nodesDraggable={!locked}
              edgesReconnectable={false}
              deleteKeyCode={null}
              connectOnClick={!locked}
              ariaLabelConfig={{
                "controls.zoomIn.ariaLabel": "Приблизить",
                "controls.zoomOut.ariaLabel": "Отдалить",
                "controls.fitView.ariaLabel": "Показать всю схему",
              }}
            >
              <Background
                variant={BackgroundVariant.Dots}
                gap={22}
                size={1.3}
              />
              <Controls showInteractive={false} />
            </ReactFlow>
          </div>
        </div>
        <aside
          ref={inspectorPanel}
          id={inspectorId}
          className="editor-inspector scenario-inspector-panel"
          aria-label="Параметры схемы"
          hidden={!inspectorOpen}
          tabIndex={-1}
          onKeyDown={(event) => {
            if (event.key === "Escape" && !event.defaultPrevented) {
              event.preventDefault();
              event.stopPropagation();
              closeInspector();
            }
          }}
        >
          <div className="scenario-inspector-heading">
            <strong>Шаги и параметры</strong>
            <button
              type="button"
              className="icon-button"
              aria-label="Закрыть панель редактирования"
              onClick={closeInspector}
            >
              <X size={19} />
            </button>
          </div>
          <div className="scenario-inspector-body">
            {!locked && (
              <>
                <h3>Добавить шаг</h3>
                <div className="node-palette">
                  {(graph.editor_mode === "SIMPLE"
                    ? ["MESSAGE", "ANSWER", "SURVEY", "END"]
                    : legacyNodeKinds
                  ).map((kind) => (
                    <button
                      key={kind}
                      draggable
                      onDragStart={(e) =>
                        e.dataTransfer.setData("korsik/node", kind)
                      }
                      onClick={() => addNode(kind)}
                    >
                      <GripVertical size={13} />
                      {nodeNames[kind]}
                      <Plus size={13} />
                    </button>
                  ))}
                </div>
              </>
            )}
            {selectedEdge &&
              (() => {
                const source = graph.steps.find(
                  (row) => row.step_key === selectedEdge.source,
                );
                const transition = source?.transitions?.[selectedEdge.index];
                if (!transition) return null;
                return (
                  <section className="edge-inspector">
                    <h3>Связь</h3>
                    <p className="muted">
                      {source?.step_key} → {transition.target_step_key}
                    </p>
                    <button
                      className="button danger compact"
                      disabled={locked}
                      onClick={() => {
                        update(
                          removeTransition(
                            graph,
                            selectedEdge.source,
                            selectedEdge.index,
                          ),
                        );
                        setSelectedEdge(null);
                      }}
                    >
                      Удалить связь
                    </button>
                  </section>
                );
              })()}
            {active && (
              <Inspector
                key={`${id}:${active.step_key}`}
                step={active}
                graph={graph}
                sites={items(sites.data)}
                locked={locked}
                onChange={(patch) => updateStep(active.step_key, patch)}
                onDelete={() => {
                  try {
                    update(removeStep(graph, active.step_key));
                    setSelected(
                      graph.steps.find((s) => s.step_key !== active.step_key)
                        ?.step_key || "",
                    );
                  } catch (e) {
                    setError(errorText(e));
                  }
                }}
              />
            )}
          </div>
        </aside>
      </div>
      {result && (
        <section className="panel">
          <div className="panel-head">
            <h3>Результат проверки</h3>
            <button className="text-button" onClick={() => setResult(null)}>
              Скрыть
            </button>
          </div>
          <ResultView data={result} />
        </section>
      )}
      {sourceReview && (
        <Modal
          title="Изменения исходной программы адаптации"
          onClose={() => setSourceReview(null)}
          wide
        >
          <Notice>
            В этой версии изменены обязательные шаги или привязки к исходному
            ТЗ. Перед согласованием проверьте каждое изменение. Новые сотрудники
            получат эту программу после публикации.
          </Notice>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Шаг</th>
                  <th>Изменение</th>
                  <th>Поля</th>
                </tr>
              </thead>
              <tbody>
                {sourceReview.source_changes.map((change: any, i: number) => (
                  <tr key={i}>
                    <td>
                      <strong>
                        {graph.steps
                          .find((step) => step.step_key === change.step_key)
                          ?.content?.text_ty?.slice(0, 100) ||
                          graph.steps
                            .find((step) => step.step_key === change.step_key)
                            ?.question?.prompt?.text_ty?.slice(0, 100) ||
                          "Шаг исходной программы"}
                      </strong>
                      <small className="muted">{change.step_key}</small>
                    </td>
                    <td>
                      {{
                        REMOVED_MANDATORY_STEP: "Удалён обязательный шаг",
                        CHANGED_MANDATORY_STEP: "Изменён обязательный шаг",
                        CHANGED_SOURCE_MAPPING: "Изменена связь с исходным ТЗ",
                      }[change.change as string] || change.change}
                    </td>
                    <td>
                      {(change.fields || [])
                        .map(
                          (field: string) =>
                            (
                              ({
                                mandatory: "Обязательность",
                                eligibility: "Аудитория",
                                trigger: "Условие и время запуска",
                                question: "Вопрос и допустимые ответы",
                                survey: "Анкета",
                                transitions: "Связи и условия переходов",
                                content: "Текст или материалы",
                                source_refs: "Связь с исходным ТЗ",
                                source_mapping: "Соответствие исходному ТЗ",
                                required_signal: "Подтверждение HR",
                              }) as Record<string, string>
                            )[field] || field,
                        )
                        .join(", ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Field label="Почему изменения нужны">
            <textarea
              value={sourceReason}
              onChange={(e) => setSourceReason(e.target.value)}
              rows={3}
            />
          </Field>
          <label className="checkbox-row">
            <input
              type="checkbox"
              checked={sourceAck}
              onChange={(e) => setSourceAck(e.target.checked)}
            />
            Я проверил изменения обязательных шагов и подтверждаю их
            согласование
          </label>
          <ErrorBox error={error} />
          <button
            className="button primary"
            disabled={busy || !sourceAck || sourceReason.trim().length < 3}
            onClick={() =>
              publish({
                fingerprint: sourceReview.source_change_fingerprint,
                reason: sourceReason.trim(),
              })
            }
          >
            Подтвердить и опубликовать
          </button>
        </Modal>
      )}
      {simulation && (
        <Simulation
          id={id}
          graph={graph}
          sites={items(sites.data)}
          onClose={() => setSimulation(false)}
        />
      )}
    </>
  );
}
export type ScenarioDraftState = {
  graph: Graph | null;
  dirty: boolean;
  etag: string | null;
  pending: Promise<void> | null;
};
// Autosave serializes writes and carries forward each reply's version token.
// A graph edited while a request is pending remains dirty until its own save.
export async function persistScenarioDraft(
  id: string,
  draft: ScenarioDraftState,
  client = api,
  onSaved: () => void = () => {},
): Promise<void> {
  while (draft.pending) await draft.pending;
  if (!draft.dirty || !draft.graph) return;
  const snapshot = draft.graph;
  const request = (async () => {
    const response = await client.put(
      `/scenario-versions/${id}`,
      { graph: snapshot },
      draft.etag,
    );
    draft.etag = response.etag;
    if (draft.graph === snapshot) {
      draft.dirty = false;
      onSaved();
    }
  })();
  draft.pending = request;
  try {
    await request;
  } finally {
    if (draft.pending === request) draft.pending = null;
  }
  if (draft.dirty) await persistScenarioDraft(id, draft, client, onSaved);
}
type SourceAcknowledgement = { fingerprint: string; reason: string };
export async function publishScenario(
  id: string,
  acknowledgement?: SourceAcknowledgement,
  client = api,
) {
  const path = `/scenario-versions/${id}`;
  const validation = await client.post(`${path}/validate`, {});
  if (!validation.data.valid)
    return { kind: "invalid" as const, validation: validation.data };
  const changed = validation.data.source_changes?.length > 0;
  if (
    changed &&
    (!acknowledgement ||
      acknowledgement.fingerprint !==
        validation.data.source_change_fingerprint ||
      acknowledgement.reason.trim().length < 3)
  )
    return { kind: "source_review" as const, validation: validation.data };
  // Validation changes the entity version; acquire its fresh ETag before review.
  const current = await client.get(path);
  const reviewed = await client.post(
    `${path}/review`,
    {
      action: "APPROVE",
      ...(changed && acknowledgement
        ? {
            source_change_acknowledged: true,
            source_change_fingerprint: acknowledgement.fingerprint,
            source_change_reason: acknowledgement.reason.trim(),
          }
        : {}),
    },
    current.etag,
  );
  const published = await client.post(
    `${path}/publish`,
    {
      migration_policy: "NEW_ENROLLMENTS_ONLY",
      reason:
        changed && acknowledgement
          ? acknowledgement.reason.trim()
          : "Публикация схемы адаптации из редактора",
    },
    reviewed.etag,
  );
  return { kind: "published" as const, response: published };
}
export function ScenarioActions({
  busy,
  canPublish,
  onSimulation,
  onPublish,
}: {
  busy: boolean;
  canPublish: boolean;
  onSimulation: () => void;
  onPublish: () => void;
}) {
  return (
    <div className="editor-toolbar" aria-label="Действия со схемой">
      <button
        className="button secondary"
        disabled={busy}
        onClick={onSimulation}
      >
        <Play size={16} />
        Симуляция
      </button>
      <button
        className="button primary"
        disabled={busy || !canPublish}
        onClick={onPublish}
      >
        <Send size={16} />
        Опубликовать
      </button>
    </div>
  );
}
export function scenarioNodeTiming(trigger: any): {
  description: string;
  value: string | null;
  clock: boolean;
} {
  if (trigger?.type === "AFTER_PREVIOUS")
    return {
      description: "После выполнения предыдущего",
      value: null,
      clock: false,
    };
  if (!trigger || trigger.type === "SIGNAL")
    return { description: describeTrigger(trigger), value: null, clock: false };
  if (trigger.type === "AFTER_STEP") {
    const minutes = trigger.offset_minutes || 0;
    return {
      description: `После шага ${trigger.after_step_key}`,
      value: `${minutes > 0 ? "+" : ""}${minutes} мин.`,
      clock: false,
    };
  }
  const time = trigger.local_time || "10:00";
  const text = describeTrigger(trigger);
  return {
    description: text.slice(0, text.lastIndexOf(" · ")),
    value: time,
    clock: true,
  };
}

export function ScenarioNodeLabel({ step }: { step: Step }) {
  const timing = scenarioNodeTiming(step.trigger);
  const text =
    step.answer?.label_ty ||
    step.content?.text_ty ||
    step.survey_template_title ||
    step.survey?.survey_key ||
    step.question?.prompt?.text_ty ||
    step.task?.summary ||
    nodeNames[step.kind] ||
    step.step_key;
  return (
    <div className="flow-node-content">
      <span
        className="flow-node-kind"
        title={nodeNames[step.kind] || step.kind}
      >
        {nodeNames[step.kind] || step.kind}
      </span>
      <strong title={text}>{text}</strong>
      {!["ANSWER", "END"].includes(step.kind) && (
        <div className="flow-node-schedule">
          <span className="flow-node-offset" title={timing.description}>
            {timing.description}
          </span>
          {timing.value &&
            (timing.clock ? (
              <time className="flow-node-time" dateTime={timing.value}>
                {timing.value}
              </time>
            ) : (
              <span className="flow-node-time">{timing.value}</span>
            ))}
        </div>
      )}
    </div>
  );
}

function describeTrigger(t: any) {
  if (!t) return "Переход по условию";
  if (t.type === "AFTER_PREVIOUS") return "После выполнения предыдущего";
  if (t.type === "SIGNAL") return `После подтверждения: ${t.signal_code}`;
  if (t.type === "AFTER_STEP")
    return `После шага ${t.after_step_key} · ${t.offset_minutes || 0} мин.`;
  const anchor = t.anchor === "PROBATION_END" ? "конца ИС" : "выхода";
  if (t.workday_number)
    return `Рабочий день № ${t.workday_number} от ${anchor} · ${t.local_time || "10:00"}`;
  return `От ${anchor}${t.offset_months ? ` ${t.offset_months > 0 ? "+" : ""}${t.offset_months} мес.` : ""} ${(t.offset_days || 0) >= 0 ? "+" : ""}${t.offset_days || 0} ${t.day_basis === "WORKING" ? "раб." : "кал."} дней · ${t.local_time || "10:00"}`;
}
export function ScenarioVersionSettings({
  name,
  status,
  busy,
  canEdit,
  onRename,
  onDelete,
  children,
}: {
  name: string;
  status: string;
  busy: boolean;
  canEdit: boolean;
  onRename: (name: string) => void;
  onDelete: () => void;
  children?: React.ReactNode;
}) {
  const [value, setValue] = useState(name);
  useEffect(() => setValue(name), [name]);
  const editable =
    canEdit && ["DRAFT", "IN_REVIEW", "APPROVED"].includes(status);
  return (
    <section className="scenario-version-settings">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          if (editable && value.trim())
            onRename(value.trim());
        }}
      >
        <Field label="Название версии">
          <input
            required
            maxLength={160}
            disabled={!editable || busy}
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
        </Field>
        {children}
        <div className="scenario-settings-footer">
          {canEdit && status === "DRAFT" && <button type="button" className="button danger" disabled={busy} onClick={onDelete}>Удалить</button>}
          {editable && <button className="button" disabled={busy || !value.trim()}>Сохранить изменения</button>}
        </div>
      </form>
    </section>
  );
}
export function ScenarioSettings({
  graph,
  locked,
  onChange,
}: {
  graph: Graph;
  locked: boolean;
  onChange: (graph: Graph) => void;
}) {
  const policy = graph.start_policy || {
    condition: "ON_BOT_START",
    timezone: "Europe/Moscow",
  };
  const scheduled = policy.condition === "SCHEDULED";
  const recurring = policy.condition === "RECURRING";
  return (
    <fieldset disabled={locked} className="scenario-settings-fields">
      <Field label="Условие запуска">
        <select
          value={policy.condition}
          onChange={(e) => {
            const condition = e.target.value as typeof policy.condition;
            onChange({
              ...graph,
              start_policy:
                condition === "SCHEDULED"
                  ? {
                      condition,
                      date: new Date().toISOString().slice(0, 10),
                      time: "10:00",
                      timezone: "Europe/Moscow",
                    }
                  : condition === "RECURRING"
                    ? { condition, every_days: 1, time: "10:00", timezone: policy.timezone || "Europe/Moscow" }
                    : { condition, timezone: "Europe/Moscow" },
            });
          }}
        >
          <option value="ON_BOT_START">При запуске</option>
          <option value="START_DATE">Дата выхода</option>
          <option value="SCHEDULED">По выбранному времени</option>
          <option value="RECURRING">Повторять</option>
        </select>
      </Field>
      {scheduled && (
        <div className="form-grid">
          <Field label="День календаря">
            <input
              type="date"
              required
              value={policy.date || ""}
              onChange={(e) =>
                onChange({
                  ...graph,
                  start_policy: { ...policy, date: e.target.value },
                })
              }
            />
          </Field>
          <Field label="Время запуска">
            <input
              type="time"
              required
              value={policy.time || "10:00"}
              onChange={(e) =>
                onChange({
                  ...graph,
                  start_policy: { ...policy, time: e.target.value },
                })
              }
            />
          </Field>
        </div>
      )}
      {recurring && <>
        <div className="form-grid">
          <Field label="Повторять весь сценарий каждые, дней"><input type="number" min={1} max={365} required value={policy.every_days ?? 1} onChange={e => onChange({...graph, start_policy: {...policy, every_days: Math.max(1, Math.min(365, Number(e.target.value) || 1))}})} /></Field>
          <Field label="Время запуска"><input type="time" required value={policy.time || "10:00"} onChange={e => onChange({...graph, start_policy: {...policy, time: e.target.value}})} /></Field>
        </div>
        <RussiaTimeZoneField value={policy.timezone} onChange={timezone => onChange({...graph, start_policy: {...policy, timezone}})} />
        <p className="muted tiny">Первый запуск — сегодня, если время ещё не наступило, иначе через выбранное число дней. Каждый новый запуск начинает сценарий заново; предыдущий незавершённый запуск заменяется.</p>
      </>}
      {scheduled && <p className="muted tiny">Время Москвы (UTC+3)</p>}
      <Field label="Повторить незавершённый шаг через, часов">
        <input
          type="number"
          min={1}
          max={168}
          value={graph.resume_policy?.reminder_hours ?? 24}
          onChange={(e) =>
            onChange({
              ...graph,
              resume_policy: {
                ...graph.resume_policy,
                reminder_hours: Math.max(
                  1,
                  Math.min(168, Number(e.target.value) || 1),
                ),
              },
            })
          }
        />
      </Field>
      {!scheduled && (
        <Field label="Количество напоминаний">
          <input
            type="number"
            min={0}
            max={100}
            placeholder={
              graph.resume_policy?.max_reminders === undefined
                ? "Без ограничения"
                : undefined
            }
            value={graph.resume_policy?.max_reminders ?? ""}
            onChange={(event) => {
              if (event.target.value === "") return;
              onChange({
                ...graph,
                resume_policy: {
                  reminder_hours: graph.resume_policy?.reminder_hours ?? 24,
                  ...graph.resume_policy,
                  max_reminders: Math.max(
                    0,
                    Math.min(100, Math.trunc(Number(event.target.value) || 0)),
                  ),
                },
              });
            }}
          />
        </Field>
      )}
    </fieldset>
  );
}
export function CustomFormulations({
  value,
  locked,
  onChange,
}: {
  value: any[];
  locked: boolean;
  onChange: (value: any[]) => void;
}) {
  return (
    <section className="custom-formulations">
      <h4>Свои формулировки</h4>
      {value.map((prompt, index) => (
        <div className="custom-formulation" key={prompt.id}>
          <Field label="Текст кнопки">
            <input
              disabled={locked}
              maxLength={80}
              value={prompt.label || ""}
              onChange={(e) =>
                onChange(
                  value.map((row, i) =>
                    i === index ? { ...row, label: e.target.value } : row,
                  ),
                )
              }
            />
          </Field>
          <Field label="Вопрос для поиска">
            <textarea
              disabled={locked}
              rows={2}
              maxLength={8000}
              value={prompt.query_text || ""}
              onChange={(e) =>
                onChange(
                  value.map((row, i) =>
                    i === index ? { ...row, query_text: e.target.value } : row,
                  ),
                )
              }
            />
          </Field>
          <button
            type="button"
            className="text-button danger"
            disabled={locked}
            onClick={() => onChange(value.filter((_, i) => i !== index))}
          >
            <Trash2 size={14} />
            Удалить формулировку
          </button>
        </div>
      ))}
      {!locked && (
        <button
          type="button"
          className="button secondary compact"
          disabled={value.length >= 12}
          onClick={() =>
            onChange([
              ...value,
              {
                id: `prompt_${crypto.randomUUID().slice(0, 8)}`,
                label: "Новый вопрос",
                query_text: "",
              },
            ])
          }
        >
          <Plus size={14} />
          Добавить формулировку
        </button>
      )}
    </section>
  );
}
export function StepScheduleFields({
  trigger,
  locked,
  onChange,
}: {
  trigger: any;
  locked: boolean;
  onChange: (trigger: any) => void;
}) {
  const after =
    !trigger ||
    trigger.type === "AFTER_PREVIOUS" ||
    trigger.type === "AFTER_STEP";
  return (
    <div className="step-schedule-fields">
      <Field label="Условие">
        <select
          disabled={locked}
          value={after ? "AFTER_PREVIOUS" : "ANCHOR_TIME"}
          onChange={(e) =>
            onChange(
              e.target.value === "AFTER_PREVIOUS"
                ? { type: "AFTER_PREVIOUS" }
                : {
                    type: "ANCHOR_TIME",
                    anchor: "START_DATE",
                    day_basis: "CALENDAR",
                    offset_days: 0,
                    local_time: "10:00",
                    calendar_binding: { mode: "SITE_AT_ENROLLMENT" },
                  },
            )
          }
        >
          <option value="AFTER_PREVIOUS">После выполнения предыдущего</option>
          <option value="ANCHOR_TIME">По сдвигу</option>
        </select>
      </Field>
      <div className="form-grid">
        <Field label="Сдвиг по дням">
          <input
            type="number"
            min={-365}
            max={365}
            disabled={locked || after}
            value={after ? "" : trigger.offset_days || 0}
            onChange={(e) =>
              onChange({ ...trigger, offset_days: Number(e.target.value) })
            }
          />
        </Field>
        <Field label="Время">
          <input
            type="time"
            disabled={locked || after}
            value={after ? "" : trigger.local_time || "10:00"}
            onChange={(e) =>
              onChange({ ...trigger, local_time: e.target.value })
            }
          />
        </Field>
      </div>
    </div>
  );
}
export function StepInspector({
  step: s,
  graph,
  locked,
  onChange,
  onDelete,
}: {
  step: Step;
  graph: Graph;
  locked: boolean;
  onChange: (s: Partial<Step>) => void;
  onDelete: () => void;
}) {
  const templates = useResource(
    s.kind === "SURVEY" ? "/survey-templates" : null,
  );
  const simple = graph.editor_mode === "SIMPLE";
  return (
    <div className="step-properties">
      <div className="inspector-title">
        <h3>{nodeNames[s.kind] || s.kind}</h3>
        <span className="code-key">ID: {s.step_key}</span>
      </div>
      <fieldset disabled={locked}>
        {s.kind === "MESSAGE" && (
          <>
            <Field
              label={simple ? "Текст сообщения" : "Текст · обращение на «ты»"}
            >
              <textarea
                rows={5}
                maxLength={simple ? 3900 : undefined}
                value={s.content?.text_ty || ""}
                onChange={(e) =>
                  onChange({
                    content: {
                      ...s.content,
                      text_ty: e.target.value,
                      ...(simple ? { text_vy: e.target.value } : {}),
                    },
                  })
                }
              />
            </Field>
            {!simple && (
              <Field label="Текст · обращение на «вы»">
                <textarea
                  rows={5}
                  value={s.content?.text_vy || ""}
                  onChange={(e) =>
                    onChange({
                      content: { ...s.content, text_vy: e.target.value },
                    })
                  }
                />
              </Field>
            )}
            <ScenarioMaterials
              assetIds={s.content?.asset_ids || []}
              locked={locked}
              onChange={(asset_ids) =>
                onChange({ content: { asset_ids } })
              }
            />
            <CustomFormulations
              value={s.content?.custom_prompts || []}
              locked={locked}
              onChange={(custom_prompts) =>
                onChange({ content: { ...s.content, custom_prompts } })
              }
            />
          </>
        )}
        {s.kind === "ANSWER" && (
          <>
            <Field label="Текст ответа">
              <input
                maxLength={80}
                value={s.answer?.label_ty || ""}
                onChange={(e) =>
                  onChange({
                    answer: answerWithLabel(s.answer, e.target.value),
                  })
                }
              />
            </Field>
            <label className="check">
              <input
                type="checkbox"
                checked={s.answer?.free_text === true}
                onChange={(e) =>
                  onChange({
                    answer: {
                      ...answerWithLabel(s.answer, s.answer?.label_ty || ""),
                      free_text: e.target.checked,
                    },
                  })
                }
              />
              Свободный ответ
            </label>
          </>
        )}
        {s.kind === "SURVEY" && (
          <>
            <Field label="Опрос">
              <select
                value={s.survey_template_version_id || ""}
                onChange={(e) =>
                  onChange({
                    survey_template_version_id: e.target.value || undefined,
                    survey: undefined,
                  })
                }
              >
                <option value="">Выберите опубликованный опрос</option>
                {items(templates.data)
                  .filter((row) => row.published_version_id)
                  .map((row) => (
                    <option key={row.id} value={row.published_version_id}>
                      {row.title || row.name}
                    </option>
                  ))}
                {s.survey_template_version_id &&
                  !items(templates.data).some(
                    (row) =>
                      row.published_version_id === s.survey_template_version_id,
                  ) && (
                    <option value={s.survey_template_version_id}>
                      Закреплённая версия опроса
                    </option>
                  )}
              </select>
            </Field>
            {s.survey?.questions?.length > 0 && (
              <p className="muted tiny">
                Вопросов: {s.survey.questions.length}. Опрос запускается
                целиком.
              </p>
            )}
            <ErrorBox error={templates.error} />
          </>
        )}
        {!["MESSAGE", "ANSWER", "SURVEY", "END"].includes(s.kind) && (
          <p className="muted">
            Шаг предыдущей версии:{" "}
            {s.question?.prompt?.text_ty ||
              s.task?.summary ||
              nodeNames[s.kind]}
            . Содержание сохранено.
          </p>
        )}
        {!["ANSWER", "END"].includes(s.kind) && (
          <StepScheduleFields
            trigger={s.trigger}
            locked={locked}
            onChange={(trigger) => onChange({ trigger })}
          />
        )}
        <button
          type="button"
          className="button danger compact"
          onClick={onDelete}
        >
          <Trash2 size={15} />
          Удалить блок
        </button>
      </fieldset>
    </div>
  );
}
export function scenarioSimulationChoices(graph: Graph) {
  return graph.steps
    .filter((step) => step.kind === "MESSAGE")
    .map((message) => ({
      message,
      answers: (message.transitions || [])
        .map((t) =>
          graph.steps.find((step) => step.step_key === t.target_step_key),
        )
        .filter((step): step is Step => step?.kind === "ANSWER"),
    }))
    .filter((group) => group.answers.length > 0);
}
function Simulation({
  id,
  graph,
  sites,
  onClose,
}: {
  id: string;
  graph: Graph;
  sites: any[];
  onClose: () => void;
}) {
  const [answers, setAnswers] = useState<Record<string, any>>({});
  const choices = scenarioSimulationChoices(graph);
  const [profile, setProfile] = useState({
      site_id: sites[0]?.id || "",
      start_date: new Date().toISOString().slice(0, 10),
      employment_type: "OFFICE",
      timezone: "Europe/Moscow",
    }),
    [result, setResult] = useState<any>(null),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  return (
    <Modal title="Виртуальный сотрудник" onClose={onClose} wide>
      <Notice>
        Симуляция не отправляет сообщения в MAX и не изменяет реальные маршруты.
      </Notice>
      <form
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setError("");
          try {
            const r = await api.post(`/scenario-versions/${id}/simulations`, {
              employee: profile,
              answers,
            });
            setResult(r.data);
          } catch (e) {
            setError(errorText(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="form-grid">
          <Field label="Площадка">
            <select
              value={profile.site_id}
              onChange={(e) =>
                setProfile({ ...profile, site_id: e.target.value })
              }
            >
              {sites.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Дата выхода">
            <input
              type="date"
              required
              value={profile.start_date}
              onChange={(e) =>
                setProfile({ ...profile, start_date: e.target.value })
              }
            />
          </Field>
          <Field label="Тип сотрудника">
            <select
              value={profile.employment_type}
              onChange={(e) =>
                setProfile({ ...profile, employment_type: e.target.value })
              }
            >
              <option value="OFFICE">Офис</option>
              <option value="PRODUCTION">Производство</option>
            </select>
          </Field>
          <Field label="Часовой пояс">
            <input
              value={profile.timezone}
              onChange={(e) =>
                setProfile({ ...profile, timezone: e.target.value })
              }
            />
          </Field>
        </div>
        {choices.length > 0 && (
          <section className="simulation-choices">
            <h3>Ответы сотрудника</h3>
            {choices.map(({ message, answers: options }) => (
              <Field
                key={message.step_key}
                label={message.content?.text_ty || message.step_key}
              >
                <select
                  value={simulationAnswerValue(
                    answers[`choice:${message.step_key}`],
                  )}
                  onChange={(e) =>
                    setAnswers({
                      ...answers,
                      [`choice:${message.step_key}`]: simulationAnswerChoice(
                        options.find(
                          (option) => option.step_key === e.target.value,
                        ),
                      ),
                    })
                  }
                >
                  <option value="">Остановиться и ждать ответа</option>
                  {options.map((answer) => (
                    <option key={answer.step_key} value={answer.step_key}>
                      {answer.answer?.label_ty || answer.step_key}
                    </option>
                  ))}
                </select>
                {options.find(
                  (option) =>
                    option.step_key ===
                    simulationAnswerValue(
                      answers[`choice:${message.step_key}`],
                    ),
                )?.answer?.free_text && (
                  <label className="field">
                    <span>Свободный ответ сотрудника</span>
                    <textarea
                      maxLength={8000}
                      value={
                        answers[`choice:${message.step_key}`]?.free_text || ""
                      }
                      onChange={(e) =>
                        setAnswers({
                          ...answers,
                          [`choice:${message.step_key}`]: {
                            ...answers[`choice:${message.step_key}`],
                            free_text: e.target.value,
                          },
                        })
                      }
                    />
                  </label>
                )}
              </Field>
            ))}
          </section>
        )}
        <ErrorBox error={error} />
        <button className="button primary" disabled={busy}>
          <Play size={16} />
          {busy ? "Рассчитываем…" : "Построить маршрут"}
        </button>
      </form>
      {result && <ResultView data={result} />}
    </Modal>
  );
}
export function ResultView({ data }: { data: any }) {
  const hasValidation = typeof data.valid === "boolean";
  const hasTimeline = Array.isArray(data.timeline);
  const rows = items(
    hasTimeline
      ? data.timeline
      : data.steps || data.plan || data.occurrences || data.events,
  );
  const zone = data.employee?.timezone || "Europe/Moscow";
  function scheduledAt(value: string): string {
    try {
      return new Intl.DateTimeFormat("ru-RU", {
        dateStyle: "medium",
        timeStyle: "short",
        timeZone: zone,
      }).format(new Date(value));
    } catch {
      return value;
    }
  }
  return (
    <>
      {hasValidation &&
        (data.valid ? (
          <Success>Программа прошла серверную проверку.</Success>
        ) : (
          <ErrorBox error="В программе есть ошибки. Исправьте указанные шаги перед публикацией." />
        ))}
      {(data.errors || []).map((error: any, index: number) => (
        <ErrorBox
          key={index}
          error={
            typeof error === "string"
              ? error
              : `${error.path || "Шаг"}: ${error.message || "Проверьте настройки"}`
          }
        />
      ))}
      {data.warnings?.length > 0 && (
        <details className="advanced">
          <summary>Замечания к содержимому: {data.warnings.length}</summary>
          {data.warnings.map((warning: any, index: number) => (
            <p className="muted" key={index}>
              {typeof warning === "string"
                ? warning
                : warning.message || "Проверьте настройки шага"}
            </p>
          ))}
        </details>
      )}
      {rows.length > 0 && (
        <>
          <p className="muted">
            Блоков в схеме: {rows.length}. Часовой пояс: {zone}.
          </p>
          <div className="timeline">
            {rows.map((row: any, index: number) => {
              const dueAt = row.due_at || row.scheduled_at || row.run_at;
              const reason =
                row.reason && typeof row.reason === "object"
                  ? describeTrigger(row.reason)
                  : row.reason || row.text || row.message || row.explanation;
              return (
                <div className="timeline-item" key={row.step_key || index}>
                  <span className="timeline-dot" />
                  <div>
                    <strong>
                      {row.title ||
                        `${nodeNames[row.kind] || row.kind || "Шаг"} · ${row.step_key || index + 1}`}
                    </strong>
                    {reason && <p>{reason}</p>}
                    {dueAt ? (
                      <time dateTime={dueAt}>{scheduledAt(dueAt)}</time>
                    ) : (
                      <small>Время зависит от события или ответа</small>
                    )}
                    {row.missing_assets?.length > 0 && (
                      <p className="muted">
                        Неприкреплённых материалов: {row.missing_assets.length}
                      </p>
                    )}
                    {row.questions?.length > 0 && (
                      <ul>
                        {row.questions.map((question: any) => (
                          <li key={question.question_key}>
                            Вопрос {question.question_key}
                            {question.comment_requested
                              ? " · запрошен комментарий"
                              : ""}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                  {row.status === "WAITING_FREE_TEXT" ? (
                    <span className="badge amber">
                      Ожидается свободный ответ
                    </span>
                  ) : (
                    <Status value={row.status || "READY"} />
                  )}
                </div>
              );
            })}
          </div>
        </>
      )}
      {hasTimeline && !rows.length && (
        <Empty
          title={
            data.valid === false
              ? "Маршрут не построен"
              : "Для указанных условий нет подходящих шагов"
          }
        />
      )}
      {!hasValidation && !hasTimeline && !rows.length && (
        <JSONView data={data} />
      )}
    </>
  );
}
function MenuEditor({ scenario }: { scenario: any }) {
  const canPublish = useActor().role === "HR_ADMIN";
  const [reason, setReason] = useState("");
  const menus = useResource(`/assistant-menus?scenario_id=${scenario.id}`);
  const [menuId, setMenuId] = useState(""),
    [version, setVersion] = useState<any>(null),
    [etag, setEtag] = useState<string | null>(null),
    [rows, setRows] = useState<any[]>([]),
    [error, setError] = useState(""),
    [success, setSuccess] = useState(""),
    [busy, setBusy] = useState(false),
    [dirty, setDirty] = useState(false);
  useEffect(() => {
    const list = items(menus.data).filter(
      (m: any) => m.scenario_id === scenario.id,
    );
    if (list.length) setMenuId(list[0].id);
  }, [menus.data]);
  const load = async (id: string) => {
    const r = await api.get(`/assistant-menu-versions/${id}`);
    setVersion(r.data);
    setEtag(r.etag);
    setRows(r.data.items || []);
    setDirty(false);
  };
  useEffect(() => {
    if (menuId)
      api
        .get(`/assistant-menus/${menuId}`)
        .then((r) => {
          const v =
            r.data.draft_version_id ||
            r.data.versions?.find((v: any) => v.status === "DRAFT")?.id ||
            r.data.current_version_id ||
            r.data.published_version_id;
          return v ? load(v) : null;
        })
        .catch((e) => setError(errorText(e)));
  }, [menuId]);
  const run = async (fn: () => Promise<any>, message: string) => {
    setBusy(true);
    setError("");
    setSuccess("");
    try {
      await fn();
      setSuccess(message);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="panel">
      <div className="panel-head">
        <div>
          <h2>Меню Корсика</h2>
        </div>
        {version && <Status value={version.status} />}
      </div>
      <p className="muted">
        Кнопка запускает тот же поиск по базе знаний, что обычный вопрос. Меню
        обновляется независимо от сроков анкет.
      </p>
      <ErrorBox error={error} />
      {success && <Success>{success}</Success>}
      <div className="button-row">
        {!menuId && (
          <button
            className="button primary"
            disabled={busy || scenario.can_edit === false}
            onClick={() =>
              run(async () => {
                const r = await api.post("/assistant-menus", {
                  name: `Меню · ${scenario.name}`,
                  scenario_id: scenario.id,
                });
                setMenuId(r.data.id);
                menus.reload();
              }, "Меню создано")
            }
          >
            Создать меню
          </button>
        )}
        {menuId && (
          <button
            className="button secondary"
            disabled={busy || scenario.can_edit === false}
            onClick={() =>
              run(async () => {
                const r = await api.post(
                  `/assistant-menus/${menuId}/versions`,
                  { base_version_id: version?.id },
                );
                await load(r.data.id);
              }, "Черновик меню создан")
            }
          >
            <Plus size={16} />
            Создать черновик
          </button>
        )}
      </div>
      {version ? (
        <>
          <div className="menu-rows">
            {rows.map((r, i) => (
              <div className="menu-edit-row" key={r.prompt_key || r.id}>
                <span className="row-number">{i + 1}</span>
                <Field label="Название кнопки · ты">
                  <input
                    disabled={
                      scenario.can_edit === false || version.status !== "DRAFT"
                    }
                    value={r.label_ty || ""}
                    onChange={(e) => {
                      setRows(
                        rows.map((v, j) =>
                          i === j
                            ? {
                                ...v,
                                label_ty: e.target.value,
                                label_vy: v.label_vy || e.target.value,
                              }
                            : v,
                        ),
                      );
                      setDirty(true);
                    }}
                  />
                </Field>
                <Field label="Название кнопки · вы">
                  <input
                    disabled={
                      scenario.can_edit === false || version.status !== "DRAFT"
                    }
                    value={r.label_vy || ""}
                    onChange={(e) => {
                      setRows(
                        rows.map((v, j) =>
                          i === j ? { ...v, label_vy: e.target.value } : v,
                        ),
                      );
                      setDirty(true);
                    }}
                  />
                </Field>
                <Field label="Вопрос для поиска">
                  <textarea
                    disabled={
                      scenario.can_edit === false || version.status !== "DRAFT"
                    }
                    value={r.query_text || ""}
                    onChange={(e) => {
                      setRows(
                        rows.map((v, j) =>
                          i === j ? { ...v, query_text: e.target.value } : v,
                        ),
                      );
                      setDirty(true);
                    }}
                  />
                </Field>
                {scenario.can_edit !== false && version.status === "DRAFT" && (
                  <button
                    className="icon-button danger"
                    aria-label="Удалить кнопку"
                    onClick={() => {
                      setRows(rows.filter((_, j) => i !== j));
                      setDirty(true);
                    }}
                  >
                    <Trash2 size={17} />
                  </button>
                )}
              </div>
            ))}
          </div>
          {scenario.can_edit !== false && version.status === "DRAFT" && (
            <button
              className="button secondary"
              onClick={() => {
                setRows([
                  ...rows,
                  {
                    prompt_key: `prompt_${crypto.randomUUID().slice(0, 8)}`,
                    label_ty: "Новый вопрос",
                    label_vy: "Новый вопрос",
                    query_text: "",
                    enabled: true,
                    order: rows.length,
                    eligibility: {
                      site_ids: [],
                      employment_types: ["OFFICE", "PRODUCTION", "OTHER"],
                      employment_stages: ["PREBOARDING", "ACTIVE"],
                    },
                  },
                ]);
                setDirty(true);
              }}
            >
              <Plus size={16} />
              Добавить кнопку
            </button>
          )}
          {scenario.can_edit !== false &&
            ["DRAFT", "APPROVED"].includes(version.status) && (
              <Field label="Основание согласования / публикации меню">
                <textarea
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  minLength={3}
                  rows={2}
                  placeholder="Что проверено и зачем обновляется меню"
                />
              </Field>
            )}
          <div className="modal-actions">
            <button
              className="button secondary"
              disabled={
                scenario.can_edit === false ||
                busy ||
                !dirty ||
                version.status !== "DRAFT"
              }
              onClick={() =>
                run(async () => {
                  await api.put(
                    `/assistant-menu-versions/${version.id}`,
                    {
                      items: rows.map((row) => ({
                        ...row,
                        id: row.id || row.prompt_key,
                        prompt_key: row.prompt_key || row.id,
                      })),
                    },
                    etag,
                  );
                  await load(version.id);
                }, "Меню сохранено")
              }
            >
              Сохранить
            </button>
            <button
              className="button secondary"
              disabled={
                scenario.can_edit === false ||
                busy ||
                dirty ||
                reason.trim().length < 3 ||
                version.status !== "DRAFT"
              }
              onClick={() =>
                run(async () => {
                  await api.post(
                    `/assistant-menu-versions/${version.id}/review`,
                    { action: "APPROVE", reason: reason.trim() },
                    etag,
                  );
                  await load(version.id);
                }, "Меню согласовано")
              }
            >
              Согласовать
            </button>
            <button
              className="button primary"
              disabled={
                scenario.can_edit === false ||
                !canPublish ||
                busy ||
                dirty ||
                reason.trim().length < 3 ||
                version.status !== "APPROVED"
              }
              onClick={() =>
                run(async () => {
                  await api.post(
                    `/assistant-menu-versions/${version.id}/publish`,
                    { reason: reason.trim() },
                    etag,
                  );
                  await load(version.id);
                }, "Меню опубликовано")
              }
            >
              Опубликовать
            </button>
          </div>
        </>
      ) : (
        <Empty title="Меню ещё не опубликовано">
          Создайте черновик и добавьте вопросы, которые сотрудники задают чаще
          всего.
        </Empty>
      )}
    </section>
  );
}
