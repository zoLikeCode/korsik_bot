export type Step = {
  step_key: string;
  kind: string;
  mandatory: boolean;
  sequence_no: number;
  eligibility: any;
  delivery_policy: any;
  trigger?: any;
  content?: any;
  transitions?: any[];
  question?: any;
  survey?: any;
  task?: any;
  answer?: { label_ty: string; label_vy: string; free_text?: boolean };
  survey_template_version_id?: string;
  survey_template_title?: string;
};
export type Graph = {
  schema_version: string;
  algorithm_version: string;
  start_step_key: string;
  steps: Step[];
  source_mapping: any[];
  editor_layout?: any;
  editor_mode?: "SIMPLE";
  start_policy?: {
    condition: "ON_BOT_START" | "START_DATE" | "SCHEDULED" | "RECURRING";
    every_days?: number;
    date?: string;
    time?: string;
    timezone: string;
  };
  resume_policy?: { reminder_hours: number; max_reminders?: number };
};
// A delayed attachment operation owns one block; it never owns a graph snapshot.
export function patchScenarioStep(graph: Graph, stepKey: string, patch: Partial<Step>): Graph {
  if (!graph.steps.some(step => step.step_key === stepKey)) return graph;
  return {...graph, steps: graph.steps.map(step => step.step_key === stepKey ? {
    ...step, ...patch,
    ...(patch.content ? {content: {...step.content, ...patch.content}} : {}),
  } : step)};
}
export const nodeNames: Record<string, string> = {
  MESSAGE: "Сообщение",
  ANSWER: "Ответ",
  QUESTION: "Вопрос",
  SURVEY: "Опрос",
  WAIT_SIGNAL: "Подтверждение HR",
  BRANCH: "Условие",
  HR_TASK: "Задача",
  END: "Завершение",
};
export function newStep(kind: string, after?: Step): Step {
  const step_key = `hr_${crypto.randomUUID().slice(0, 8)}`;
  const s: Step = {
    step_key,
    kind,
    mandatory: false,
    sequence_no: after ? after.sequence_no + 1 : 0,
    eligibility: {
      site_ids: [],
      employment_types: ["OFFICE", "PRODUCTION", "OTHER"],
      employment_stages: ["PREBOARDING", "ACTIVE"],
    },
    delivery_policy: {
      late_policy: "SEND_ONCE_ON_JOIN",
      non_working_day_policy: "KEEP_CALENDAR_DATE",
      quiet_hours_policy: "DEFER_TO_ALLOWED_TIME",
      relevance_window_days: 7,
      max_wait_days: 7,
    },
    transitions: [],
  };
  if (!["BRANCH", "END", "ANSWER"].includes(kind))
    s.trigger = after
      ? {
          type: "AFTER_STEP",
          after_step_key: after.step_key,
          after_event: "STEP_COMPLETED",
          offset_minutes: 0,
        }
      : { type: "AFTER_PREVIOUS" };
  if (kind === "MESSAGE")
    s.content = {
      text_ty: "Новое сообщение",
      text_vy: "Новое сообщение",
      asset_ids: [],
      buttons: [],
    };
  if (kind === "ANSWER")
    s.answer = { label_ty: "Новый ответ", label_vy: "Новый ответ" };
  if (kind === "WAIT_SIGNAL")
    s.trigger = { type: "SIGNAL", signal_code: "HR_APPROVAL" };
  if (kind === "HR_TASK")
    s.task = {
      task_type: "FOLLOW_UP",
      assignee_kind: "ASSIGNED_HR",
      due_after_days: 1,
      day_basis: "CALENDAR",
      summary: "Связаться с сотрудником",
    };
  if (kind === "QUESTION")
    s.question = {
      question_key: `q_${step_key}`,
      type: "TEXT",
      prompt: { text_ty: "Ваш вопрос", text_vy: "Ваш вопрос" },
      required: false,
      source_requiredness: "OPTIONAL",
      allow_decline: true,
      max_text_length: 2000,
    };
  return s;
}
export function insertStep(
  graph: Graph,
  step: Step,
  afterKey?: string,
  before = false,
): Graph {
  const copy: Graph = structuredClone(graph);
  if (!afterKey) {
    if (!copy.steps.length) copy.start_step_key = step.step_key;
    copy.steps.push(step);
    return copy;
  }
  const anchor = copy.steps.find((s) => s.step_key === afterKey);
  if (!anchor) throw new Error("Шаг для вставки не найден");
  if (before) {
    for (const s of copy.steps) {
      s.transitions = (s.transitions || []).map((t) =>
        t.target_step_key === afterKey
          ? { ...t, target_step_key: step.step_key }
          : t,
      );
    }
    step.transitions = [
      { target_step_key: afterKey, condition: { kind: "ALWAYS" } },
    ];
    if (copy.start_step_key === afterKey) copy.start_step_key = step.step_key;
    const index = copy.steps.indexOf(anchor);
    copy.steps.splice(index, 0, step);
  } else {
    step.transitions = structuredClone(anchor.transitions || []);
    anchor.transitions = [
      { target_step_key: step.step_key, condition: { kind: "ALWAYS" } },
    ];
    copy.steps.splice(copy.steps.indexOf(anchor) + 1, 0, step);
  }
  copy.steps = copy.steps.map((s, i) => ({ ...s, sequence_no: i }));
  return copy;
}
export function removeStep(graph: Graph, key: string): Graph {
  const copy: Graph = structuredClone(graph);
  const target = copy.steps.find((s) => s.step_key === key);
  if (!target) return copy;
  if (
    copy.steps.some(
      (s) =>
        s.step_key !== key &&
        s.trigger?.type === "AFTER_STEP" &&
        s.trigger.after_step_key === key,
    )
  )
    throw new Error(
      "Другой шаг ожидает завершения этого шага. Сначала измените его время запуска.",
    );
  if ((target.transitions || []).length > 1)
    throw new Error(
      "У шага несколько вариантов перехода. Сначала измените переходы, чтобы удаление не потеряло ветку сценария.",
    );
  const next =
    target.kind === "ANSWER"
      ? undefined
      : (target.transitions || [])[0]?.target_step_key;
  copy.steps = copy.steps
    .filter((s) => s.step_key !== key)
    .map((s) => ({
      ...s,
      transitions: (s.transitions || []).flatMap((t) =>
        t.target_step_key === key
          ? next
            ? [{ ...t, target_step_key: next }]
            : []
          : [t],
      ),
    }));
  if (copy.start_step_key === key)
    copy.start_step_key = next || copy.steps[0]?.step_key || "";
  if (copy.editor_layout)
    copy.editor_layout.nodes = (copy.editor_layout.nodes || []).filter(
      (n: any) => n.step_key !== key,
    );
  return copy;
}
export function checkGraph(graph: Graph): string[] {
  const errors: string[] = [];
  const ids = new Set(graph.steps.map((s) => s.step_key));
  if (ids.size !== graph.steps.length)
    errors.push("Идентификаторы шагов должны быть уникальны");
  if (!ids.has(graph.start_step_key)) errors.push("Не указан начальный шаг");
  for (const s of graph.steps) {
    const answerChoices =
      s.kind === "MESSAGE" &&
      (s.transitions || []).length > 0 &&
      (s.transitions || []).every(
        (t) =>
          graph.steps.find((row) => row.step_key === t.target_step_key)
            ?.kind === "ANSWER",
      );
    if (
      !answerChoices &&
      (s.transitions || []).filter(
        (t) => !t.condition || t.condition.kind === "ALWAYS",
      ).length > 1
    )
      errors.push(
        `Шаг ${s.step_key}: только одна безусловная связь допустима; остальные должны иметь условия`,
      );
    for (const t of s.transitions || [])
      if (!ids.has(t.target_step_key))
        errors.push(`Шаг ${s.step_key}: переход ссылается на неизвестный шаг`);
    if (s.trigger?.type === "AFTER_STEP" && !ids.has(s.trigger.after_step_key))
      errors.push(`Шаг ${s.step_key}: неизвестный предшествующий шаг`);
  }
  const visiting = new Set<string>(),
    visited = new Set<string>();
  let cyclic = false;
  const visit = (key: string) => {
    if (visiting.has(key)) {
      cyclic = true;
      return;
    }
    if (visited.has(key)) return;
    visiting.add(key);
    for (const t of graph.steps.find((s) => s.step_key === key)?.transitions ||
      [])
      if (ids.has(t.target_step_key)) visit(t.target_step_key);
    visiting.delete(key);
    visited.add(key);
  };
  graph.steps.forEach((s) => visit(s.step_key));
  if (cyclic) errors.push("Связи образуют цикл. Удалите обратную связь.");
  return errors;
}

/** Edges are the executable first-match transitions, not editor-only decoration. */
export function connectSteps(
  graph: Graph,
  source: string,
  target: string,
  condition: any = { kind: "ALWAYS" },
): Graph {
  const from = graph.steps.find((s) => s.step_key === source);
  if (!from || !graph.steps.some((s) => s.step_key === target))
    throw new Error("Шаг связи не найден.");
  if (source === target) throw new Error("Нельзя соединить шаг с самим собой.");
  if (from.kind === "END")
    throw new Error("У завершения не может быть следующего шага.");
  if ((from.transitions || []).some((t) => t.target_step_key === target))
    throw new Error("Эта связь уже существует.");
  const targetStep = graph.steps.find((s) => s.step_key === target)!;
  const answerChoice = from.kind === "MESSAGE" && targetStep.kind === "ANSWER";
  if (answerChoice && (from.transitions || []).length >= 10)
    throw new Error("Под сообщением может быть не больше 10 ответов.");
  const existingAnswers = (from.transitions || []).some(
    (t) =>
      graph.steps.find((row) => row.step_key === t.target_step_key)?.kind ===
      "ANSWER",
  );
  if (
    (answerChoice &&
      (from.transitions || []).some(
        (t) =>
          graph.steps.find((row) => row.step_key === t.target_step_key)
            ?.kind !== "ANSWER",
      )) ||
    (!answerChoice && existingAnswers)
  )
    throw new Error(
      "Сообщение с вариантами ответа соединяется только с блоками «Ответ».",
    );
  if (targetStep.kind === "ANSWER" && from.kind !== "MESSAGE")
    throw new Error("Блок «Ответ» можно присоединить только к сообщению.");
  if (
    !answerChoice &&
    condition.kind === "ALWAYS" &&
    (from.transitions || []).some(
      (t) => !t.condition || t.condition.kind === "ALWAYS",
    )
  )
    throw new Error(
      "У шага уже есть безусловная связь. Удалите её или настройте условие перед добавлением новой.",
    );
  const copy = structuredClone(graph),
    row = copy.steps.find((s) => s.step_key === source)!;
  const transitions = row.transitions || [];
  const fallback = transitions.findIndex(
    (t) => !t.condition || t.condition.kind === "ALWAYS",
  );
  const edge = {
    target_step_key: target,
    condition: structuredClone(condition),
  };
  if (condition.kind !== "ALWAYS" && fallback >= 0)
    transitions.splice(fallback, 0, edge);
  else transitions.push(edge);
  row.transitions = transitions;
  if (checkGraph(copy).some((message) => message.includes("цикл")))
    throw new Error("Эта связь создаёт цикл.");
  return copy;
}

export function removeTransition(
  graph: Graph,
  source: string,
  index: number,
): Graph {
  const copy = structuredClone(graph),
    row = copy.steps.find((s) => s.step_key === source);
  if (!row || !row.transitions?.[index])
    throw new Error("Связь изменилась. Выберите её заново.");
  row.transitions.splice(index, 1);
  return copy;
}

export function moveStep(
  graph: Graph,
  key: string,
  position: { x: number; y: number },
): Graph {
  if (!graph.steps.some((s) => s.step_key === key))
    throw new Error("Шаг не найден.");
  if (
    ![position.x, position.y].every(
      (n) => Number.isFinite(n) && Math.abs(n) <= 1000000,
    )
  )
    throw new Error("Недопустимое положение шага.");
  const layout = graph.editor_layout || {
    schema_version: "korsik-editor-layout-v1",
    nodes: [],
    viewport: { x: 0, y: 0, zoom: 1 },
  };
  return {
    ...graph,
    editor_layout: {
      ...layout,
      nodes: [
        ...(layout.nodes || []).filter((n: any) => n.step_key !== key),
        { step_key: key, ...position },
      ],
    },
  };
}

export function scenarioHasPublishedVersion(scenario: {
  has_published_versions?: boolean;
  current_published_version_id?: string | null;
  published_version_id?: string | null;
  latest_version_status?: string;
}) {
  return (
    scenario.has_published_versions === true ||
    !!scenario.current_published_version_id ||
    !!scenario.published_version_id ||
    scenario.latest_version_status === "PUBLISHED"
  );
}

export function scenarioDraftPayload(base?: {
  id: string;
  graph?: Pick<Graph, "editor_mode">;
}) {
  return {
    ...(base ? { base_version_id: base.id } : {}),
    ...(!base || base.graph?.editor_mode === "SIMPLE"
      ? { editor_mode: "SIMPLE" }
      : {}),
  };
}

export function answerWithLabel(
  answer: Step["answer"],
  label: string,
): NonNullable<Step["answer"]> {
  return { ...answer, label_ty: label, label_vy: label };
}
export function simulationAnswerValue(response: any): string {
  return typeof response === "string" ? response : response?.value || "";
}
export function simulationAnswerChoice(answer: Step | undefined): any {
  if (!answer) return "";
  return answer.answer?.free_text
    ? { value: answer.step_key, free_text: "" }
    : answer.step_key;
}
