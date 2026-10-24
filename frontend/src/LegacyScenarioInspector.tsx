import { ScenarioMaterials } from "./ScenarioMaterials";
// Native editor for existing calendar-v1 drafts. No conversion or source-field loss.
import { Plus, Trash2 } from "lucide-react";
import { Field } from "./ui";
import { type Step, type Graph, nodeNames } from "./scenario";
export const legacyNodeKinds = [
  "MESSAGE",
  "QUESTION",
  "SURVEY",
  "WAIT_SIGNAL",
  "HR_TASK",
  "BRANCH",
  "END",
];
export function legacyNewStep(kind: string, after?: Step): Step {
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
  if (!["BRANCH", "END"].includes(kind))
    s.trigger = after
      ? {
          type: "AFTER_STEP",
          after_step_key: after.step_key,
          after_event: "STEP_COMPLETED",
          offset_minutes: 0,
        }
      : {
          type: "ANCHOR_TIME",
          anchor: "START_DATE",
          day_basis: "CALENDAR",
          offset_days: 0,
          offset_months: 0,
          local_time: "10:00",
          calendar_binding: { mode: "SITE_AT_ENROLLMENT" },
        };
  if (kind === "MESSAGE")
    s.content = {
      text_ty: "Новое сообщение",
      text_vy: "Новое сообщение",
      asset_ids: [],
      buttons: [],
    };
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
  if (kind === "SURVEY")
    s.survey = {
      survey_key: `survey_${step_key}`,
      start_question_key: `q_${step_key}`,
      expiry_days: 7,
      thank_you: {
        text_ty: "Спасибо за ответы!",
        text_vy: "Спасибо за ответы!",
      },
      questions: [
        {
          question_key: `q_${step_key}`,
          type: "NUMERIC",
          prompt: {
            text_ty: "Как проходит адаптация?",
            text_vy: "Как проходит адаптация?",
          },
          required: true,
          source_requiredness: "REQUIRED",
          allow_decline: true,
          minimum: 1,
          maximum: 10,
        },
      ],
      reminders: {
        anchor: "FIRST_QUESTION_ACCEPTED",
        offset_local_days: [1, 2],
        suppressed_in_human: true,
        suppressed_on_pause: true,
        suppressed_on_terminal: true,
      },
    };
  return s;
}
function ConditionEditor({
  condition: c,
  graph,
  sites,
  onChange,
  depth = 0,
}: {
  condition: any;
  graph: Graph;
  sites: any[];
  onChange: (c: any) => void;
  depth?: number;
}) {
  const questions = graph.steps.flatMap((s) =>
    s.question ? [s.question] : s.survey?.questions || [],
  );
  const kinds = [
    ["ALWAYS", "Всегда"],
    ["EMPLOYEE", "Атрибут сотрудника"],
    ["ANSWER", "Ответ на вопрос"],
    ["SIGNAL", "Подтверждение HR"],
    ...(depth < 3
      ? [
          ["ALL", "Все условия"],
          ["ANY", "Хотя бы одно"],
        ]
      : []),
  ];
  const set = (p: any) => onChange({ ...c, ...p });
  return (
    <div className="condition-editor">
      <Field label="Условие перехода">
        <select
          value={c.kind}
          onChange={(e) => {
            const kind = e.target.value;
            onChange(
              kind === "ALWAYS"
                ? { kind }
                : kind === "EMPLOYEE"
                  ? {
                      kind,
                      field: "EMPLOYMENT_TYPE",
                      operator: "EQ",
                      value: "OFFICE",
                    }
                  : kind === "ANSWER"
                    ? {
                        kind,
                        question_key: questions[0]?.question_key || "",
                        operator: "EQ",
                        value: "",
                      }
                    : kind === "SIGNAL"
                      ? { kind, signal_code: "HR_APPROVAL" }
                      : { kind, conditions: [{ kind: "ALWAYS" }] },
            );
          }}
        >
          {kinds.map(([v, label]) => (
            <option value={v} key={v}>
              {label}
            </option>
          ))}
        </select>
      </Field>
      {c.kind === "EMPLOYEE" && (
        <Field label="Атрибут">
          <select
            value={c.field}
            onChange={(e) => set({ field: e.target.value, value: "" })}
          >
            <option value="EMPLOYMENT_TYPE">Тип сотрудника</option>
            <option value="SITE_ID">Площадка</option>
            <option value="EMPLOYMENT_STAGE">Стадия найма</option>
            <option value="ADDRESS_FORM">Обращение</option>
          </select>
        </Field>
      )}
      {c.kind === "ANSWER" && (
        <Field label="Вопрос">
          <select
            value={c.question_key}
            onChange={(e) => set({ question_key: e.target.value })}
          >
            {questions.map((q) => (
              <option key={q.question_key} value={q.question_key}>
                {q.prompt?.text_ty?.slice(0, 60) || q.question_key}
              </option>
            ))}
          </select>
        </Field>
      )}
      {["ANSWER", "EMPLOYEE"].includes(c.kind) && (
        <>
          <Field label="Проверка">
            <select
              value={c.operator}
              onChange={(e) => {
                const operator = e.target.value;
                set({
                  operator,
                  ...(["IS_EMPTY", "IS_NOT_EMPTY"].includes(operator)
                    ? { value: undefined }
                    : ["LT", "LTE", "GT", "GTE"].includes(operator)
                      ? { value: Number(c.value) || 8 }
                      : { value: String(c.value ?? "") }),
                });
              }}
            >
              {[
                ["EQ", "Равно"],
                ["NE", "Не равно"],
                ...(c.kind === "ANSWER"
                  ? [
                      ["LT", "Меньше"],
                      ["LTE", "Не больше"],
                      ["GT", "Больше"],
                      ["GTE", "Не меньше"],
                      ["IS_EMPTY", "Нет ответа"],
                      ["IS_NOT_EMPTY", "Ответ получен"],
                    ]
                  : []),
              ].map(([v, label]) => (
                <option value={v} key={v}>
                  {label}
                </option>
              ))}
            </select>
          </Field>
          {!["IS_EMPTY", "IS_NOT_EMPTY"].includes(c.operator) && (
            <Field label="Значение">
              {c.kind === "EMPLOYEE" && c.field === "SITE_ID" ? (
                <select
                  value={c.value}
                  onChange={(e) => set({ value: e.target.value })}
                >
                  <option value="">Выберите площадку</option>
                  {sites.map((s) => (
                    <option key={s.id} value={s.id}>
                      {s.name}
                    </option>
                  ))}
                </select>
              ) : c.kind === "EMPLOYEE" ? (
                <select
                  value={c.value}
                  onChange={(e) => set({ value: e.target.value })}
                >
                  {(c.field === "EMPLOYMENT_TYPE"
                    ? [
                        ["OFFICE", "Офис"],
                        ["PRODUCTION", "Производство"],
                      ]
                    : c.field === "EMPLOYMENT_STAGE"
                      ? [
                          ["PREBOARDING", "До выхода"],
                          ["ACTIVE", "Работает"],
                        ]
                      : [
                          ["TY", "Ты"],
                          ["VY", "Вы"],
                        ]
                  ).map(([v, label]) => (
                    <option key={v} value={v}>
                      {label}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  type={
                    ["LT", "LTE", "GT", "GTE"].includes(c.operator)
                      ? "number"
                      : "text"
                  }
                  value={c.value ?? ""}
                  onChange={(e) =>
                    set({
                      value: ["LT", "LTE", "GT", "GTE"].includes(c.operator)
                        ? Number(e.target.value)
                        : e.target.value,
                    })
                  }
                />
              )}
            </Field>
          )}
        </>
      )}
      {c.kind === "SIGNAL" && (
        <Field label="Сигнал">
          <input
            value={c.signal_code || ""}
            onChange={(e) => set({ signal_code: e.target.value })}
          />
        </Field>
      )}
      {["ALL", "ANY"].includes(c.kind) && (
        <>
          {(c.conditions || []).map((child: any, i: number) => (
            <ConditionEditor
              key={i}
              condition={child}
              graph={graph}
              sites={sites}
              depth={depth + 1}
              onChange={(value) =>
                set({
                  conditions: c.conditions.map((v: any, j: number) =>
                    i === j ? value : v,
                  ),
                })
              }
            />
          ))}
          <button
            className="text-button"
            onClick={() =>
              set({ conditions: [...(c.conditions || []), { kind: "ALWAYS" }] })
            }
          >
            <Plus size={13} />
            Добавить условие
          </button>
        </>
      )}
    </div>
  );
}
export function LegacyStepInspector({
  step: s,
  graph,
  sites,
  locked,
  onChange,
  onDelete,
}: {
  step: Step;
  graph: Graph;
  sites: any[];
  locked: boolean;
  onChange: (s: Partial<Step>) => void;
  onDelete: () => void;
}) {
  const t = s.trigger;
  const question = s.question;
  const editQuestion = (q: any) =>
    onChange({ question: { ...question, ...q } });
  return (
    <div className="step-properties">
      <div className="inspector-title">
        <h3>{nodeNames[s.kind]}</h3>
        <span className="code-key">ID: {s.step_key}</span>
      </div>
      <fieldset disabled={locked}>
        {s.content && (
          <>
            <Field label="Текст · обращение на «ты»">
              <textarea
                rows={5}
                value={s.content.text_ty || ""}
                onChange={(e) =>
                  onChange({
                    content: { ...s.content, text_ty: e.target.value },
                  })
                }
              />
            </Field>
            <Field label="Текст · обращение на «вы»">
              <textarea
                rows={5}
                value={s.content.text_vy || ""}
                onChange={(e) =>
                  onChange({
                    content: { ...s.content, text_vy: e.target.value },
                  })
                }
              />
            </Field>
            <ScenarioMaterials
              assetIds={s.content.asset_ids || []}
              locked={locked}
              onChange={(asset_ids) =>
                onChange({ content: { asset_ids } })
              }
            />
          </>
        )}
        {question && (
          <QuestionFields question={question} onChange={editQuestion} />
        )}{" "}
        {s.survey && (
          <>
            <Field label="Срок ответа, календарных дней">
              <input
                type="number"
                min={1}
                max={90}
                value={s.survey.expiry_days}
                onChange={(e) =>
                  onChange({
                    survey: {
                      ...s.survey,
                      expiry_days: Number(e.target.value),
                    },
                  })
                }
              />
            </Field>
            <h4>Вопросы опроса</h4>
            {s.survey.questions.map((q: any, index: number) => (
              <details className="survey-question" key={q.question_key}>
                <summary>
                  {index + 1}. {q.prompt?.text_ty || q.question_key}
                </summary>
                <QuestionFields
                  question={q}
                  onChange={(patch) =>
                    onChange({
                      survey: {
                        ...s.survey,
                        questions: s.survey.questions.map(
                          (v: any, i: number) =>
                            i === index ? { ...v, ...patch } : v,
                        ),
                      },
                    })
                  }
                />
              </details>
            ))}
            <button
              className="button secondary compact"
              onClick={() => {
                const q = legacyNewStep("QUESTION").question;
                const previous = s.survey.questions.at(-1);
                const questions = [
                  ...s.survey.questions.map((v: any) =>
                    v === previous
                      ? { ...v, next_question_key: q.question_key }
                      : v,
                  ),
                  q,
                ];
                onChange({ survey: { ...s.survey, questions } });
              }}
            >
              <Plus size={15} />
              Добавить вопрос
            </button>
          </>
        )}
        {s.task && (
          <>
            <Field label="Задача ответственному">
              <textarea
                value={s.task.summary}
                onChange={(e) =>
                  onChange({ task: { ...s.task, summary: e.target.value } })
                }
              />
            </Field>
            <Field label="Кому назначить">
              <select
                value={s.task.assignee_kind}
                onChange={(e) =>
                  onChange({
                    task: { ...s.task, assignee_kind: e.target.value },
                  })
                }
              >
                <option value="ASSIGNED_HR">Назначенный HR</option>
                <option value="EMPLOYEE_MANAGER">
                  Руководитель сотрудника
                </option>
                <option value="RECRUITER">Рекрутер</option>
              </select>
            </Field>
            <Field label="Срок, дней">
              <input
                type="number"
                min={0}
                value={s.task.due_after_days}
                onChange={(e) =>
                  onChange({
                    task: { ...s.task, due_after_days: Number(e.target.value) },
                  })
                }
              />
            </Field>
          </>
        )}
        {t && (
          <>
            <h4>Когда отправить</h4>
            {s.kind !== "WAIT_SIGNAL" && (
              <Field label="Условие запуска">
                <select
                  value={t.type}
                  onChange={(e) =>
                    onChange({
                      trigger:
                        e.target.value === "AFTER_STEP"
                          ? {
                              type: "AFTER_STEP",
                              after_step_key:
                                graph.steps.find(
                                  (v) => v.step_key !== s.step_key,
                                )?.step_key || "",
                              after_event: "STEP_COMPLETED",
                              offset_minutes: 0,
                            }
                          : {
                              type: "ANCHOR_TIME",
                              anchor: "START_DATE",
                              day_basis: "CALENDAR",
                              offset_days: 0,
                              offset_months: 0,
                              local_time: "10:00",
                              calendar_binding: { mode: "SITE_AT_ENROLLMENT" },
                            },
                    })
                  }
                >
                  <option value="ANCHOR_TIME">От даты выхода / конца ИС</option>
                  <option value="AFTER_STEP">После другого шага</option>
                </select>
              </Field>
            )}
            {t.type === "ANCHOR_TIME" && (
              <>
                <Field label="Опорная дата">
                  <select
                    value={t.anchor}
                    onChange={(e) =>
                      onChange({ trigger: { ...t, anchor: e.target.value } })
                    }
                  >
                    <option value="START_DATE">Дата выхода</option>
                    <option value="PROBATION_END">
                      Конец испытательного срока
                    </option>
                  </select>
                </Field>
                <div className="form-grid">
                  <Field
                    label={
                      t.workday_number
                        ? "Порядковый рабочий день (выход — первый)"
                        : "Сдвиг, дней"
                    }
                  >
                    <input
                      type="number"
                      min={t.workday_number ? 1 : -3650}
                      max={3650}
                      value={t.workday_number || t.offset_days || 0}
                      onChange={(e) =>
                        onChange({
                          trigger: {
                            ...t,
                            ...(t.workday_number
                              ? { workday_number: Number(e.target.value) }
                              : { offset_days: Number(e.target.value) }),
                          },
                        })
                      }
                    />
                  </Field>
                  <Field label="Время">
                    <input
                      type="time"
                      value={t.local_time || "10:00"}
                      onChange={(e) =>
                        onChange({
                          trigger: { ...t, local_time: e.target.value },
                        })
                      }
                    />
                  </Field>
                </div>
                {!t.workday_number && (
                  <Field label="Сдвиг, месяцев (перед сдвигом дней)">
                    <input
                      type="number"
                      min={-120}
                      max={120}
                      value={t.offset_months || 0}
                      onChange={(e) =>
                        onChange({
                          trigger: {
                            ...t,
                            offset_months: Number(e.target.value),
                          },
                        })
                      }
                    />
                  </Field>
                )}
                {t.day_basis === "WORKING" && (
                  <label className="check-field">
                    <input
                      type="checkbox"
                      checked={!!t.workday_number}
                      onChange={(e) => {
                        const next = { ...t };
                        if (e.target.checked) {
                          next.workday_number = 1;
                          delete next.offset_days;
                          delete next.offset_months;
                        } else {
                          delete next.workday_number;
                          next.offset_days = 0;
                          next.offset_months = 0;
                        }
                        onChange({ trigger: next });
                      }}
                    />
                    Указать порядковый рабочий день
                  </label>
                )}
                <Field label="Считать дни">
                  <select
                    value={t.day_basis}
                    onChange={(e) => {
                      const next = { ...t, day_basis: e.target.value };
                      if (
                        next.day_basis === "CALENDAR" &&
                        next.workday_number
                      ) {
                        delete next.workday_number;
                        next.offset_days = 0;
                        next.offset_months = 0;
                      }
                      onChange({ trigger: next });
                    }}
                  >
                    <option value="CALENDAR">По календарю</option>
                    <option value="WORKING">По рабочему календарю</option>
                  </select>
                </Field>
              </>
            )}
            {t.type === "AFTER_STEP" && (
              <>
                <Field label="Предшествующий шаг">
                  <select
                    value={t.after_step_key}
                    onChange={(e) =>
                      onChange({
                        trigger: { ...t, after_step_key: e.target.value },
                      })
                    }
                  >
                    {graph.steps
                      .filter((v) => v.step_key !== s.step_key)
                      .map((v) => (
                        <option value={v.step_key} key={v.step_key}>
                          {v.step_key}
                        </option>
                      ))}
                  </select>
                </Field>
                <Field label="Событие">
                  <select
                    value={t.after_event}
                    onChange={(e) =>
                      onChange({
                        trigger: { ...t, after_event: e.target.value },
                      })
                    }
                  >
                    <option value="STEP_COMPLETED">Шаг завершён</option>
                    <option value="ACTION_ACCEPTED">
                      Сообщение принято MAX
                    </option>
                    <option value="SURVEY_COMPLETED">Анкета завершена</option>
                  </select>
                </Field>
                <Field label="Задержка, минут">
                  <input
                    type="number"
                    min={0}
                    max={10080}
                    value={t.offset_minutes || 0}
                    onChange={(e) =>
                      onChange({
                        trigger: {
                          ...t,
                          offset_minutes: Number(e.target.value),
                        },
                      })
                    }
                  />
                </Field>
              </>
            )}
            {t.type === "SIGNAL" && (
              <Field label="Код подтверждения">
                <input
                  value={t.signal_code}
                  onChange={(e) =>
                    onChange({ trigger: { ...t, signal_code: e.target.value } })
                  }
                />
              </Field>
            )}
          </>
        )}
        <details className="advanced">
          <summary>Условия переходов</summary>
          {(s.transitions || []).map((tr: any, i: number) => (
            <div className="transition-row" key={i}>
              <Field label={`Переход ${i + 1}`}>
                <select
                  value={tr.target_step_key}
                  onChange={(e) =>
                    onChange({
                      transitions: s.transitions?.map((v: any, j: number) =>
                        i === j ? { ...v, target_step_key: e.target.value } : v,
                      ),
                    })
                  }
                >
                  {graph.steps
                    .filter((v) => v.step_key !== s.step_key)
                    .map((v) => (
                      <option key={v.step_key} value={v.step_key}>
                        {v.step_key}
                      </option>
                    ))}
                </select>
              </Field>
              <button
                type="button"
                className="text-button danger"
                onClick={() =>
                  onChange({
                    transitions: s.transitions?.filter(
                      (_: any, index: number) => index !== i,
                    ),
                  })
                }
              >
                Удалить связь
              </button>
              <ConditionEditor
                condition={tr.condition || { kind: "ALWAYS" }}
                graph={graph}
                sites={sites}
                onChange={(condition) =>
                  onChange({
                    transitions: s.transitions?.map((v: any, j: number) =>
                      i === j ? { ...v, condition } : v,
                    ),
                  })
                }
              />
            </div>
          ))}
        </details>
        <button
          type="button"
          className="button danger compact"
          onClick={onDelete}
        >
          <Trash2 size={15} />
          Удалить шаг
        </button>
      </fieldset>
    </div>
  );
}
function QuestionFields({
  question: q,
  onChange,
}: {
  question: any;
  onChange: (p: any) => void;
}) {
  return (
    <>
      <Field label="Тип ответа">
        <select
          value={q.type}
          onChange={(e) => {
            const type = e.target.value;
            const next: any = {
              question_key: q.question_key,
              type,
              prompt: q.prompt,
              required: q.required,
              source_requiredness: q.source_requiredness || "OPTIONAL",
              allow_decline: true,
            };
            if (type === "TEXT") next.max_text_length = 2000;
            if (type === "NUMERIC") {
              next.minimum = 1;
              next.maximum = 10;
            }
            if (["YES_NO", "SINGLE_CHOICE", "MULTI_CHOICE"].includes(type))
              next.options =
                type === "YES_NO"
                  ? [
                      {
                        value: "yes",
                        label: { text_ty: "Да", text_vy: "Да" },
                        exclusive: false,
                      },
                      {
                        value: "no",
                        label: { text_ty: "Нет", text_vy: "Нет" },
                        exclusive: false,
                      },
                    ]
                  : [
                      {
                        value: "option_1",
                        label: {
                          text_ty: "Первый вариант",
                          text_vy: "Первый вариант",
                        },
                        exclusive: false,
                      },
                    ];
            if (type === "MULTI_CHOICE") {
              next.min_selected = 1;
              next.max_selected = 10;
            }
            if (type === "PHOTO") next.attachment_purpose = "EMPLOYEE_PHOTO";
            Object.keys(q).forEach((k) => {
              if (!(k in next)) next[k] = undefined;
            });
            onChange(next);
          }}
        >
          <option value="TEXT">Свободный текст</option>
          <option value="NUMERIC">Оценка 1–10</option>
          <option value="YES_NO">Да / нет</option>
          <option value="SINGLE_CHOICE">Один вариант</option>
          <option value="MULTI_CHOICE">Несколько вариантов</option>
          <option value="PHOTO">Фотография</option>
        </select>
      </Field>
      <Field label="Формулировка · ты">
        <textarea
          value={q.prompt?.text_ty || ""}
          onChange={(e) =>
            onChange({ prompt: { ...q.prompt, text_ty: e.target.value } })
          }
        />
      </Field>
      <Field label="Формулировка · вы">
        <textarea
          value={q.prompt?.text_vy || ""}
          onChange={(e) =>
            onChange({ prompt: { ...q.prompt, text_vy: e.target.value } })
          }
        />
      </Field>
      <label className="check-field">
        <input
          type="checkbox"
          checked={q.required}
          onChange={(e) => onChange({ required: e.target.checked })}
        />
        Обязательный вопрос
      </label>
      <div className="muted tiny">
        Тип:{" "}
        {(
          {
            TEXT: "текст",
            NUMERIC: "оценка 1–10",
            YES_NO: "да / нет",
            SINGLE_CHOICE: "один вариант",
            MULTI_CHOICE: "несколько вариантов",
            PHOTO: "фотография",
          } as any
        )[q.type] || q.type}
        . Отказ всегда доступен.
      </div>
      {q.options?.map((o: any, i: number) => (
        <Field key={o.value || i} label={`Вариант ${i + 1}`}>
          <input
            value={o.label?.text_ty || o.label_ty || o.label || ""}
            onChange={(e) =>
              onChange({
                options: q.options.map((v: any, j: number) =>
                  i === j
                    ? {
                        ...v,
                        label: {
                          text_ty: e.target.value,
                          text_vy: e.target.value,
                        },
                      }
                    : v,
                ),
              })
            }
          />
        </Field>
      ))}
      {q.options && q.type !== "YES_NO" && (
        <button
          className="button secondary compact"
          onClick={() =>
            onChange({
              options: [
                ...q.options,
                {
                  value: `option_${crypto.randomUUID().slice(0, 6)}`,
                  label: { text_ty: "Новый вариант", text_vy: "Новый вариант" },
                  exclusive: false,
                },
              ],
            })
          }
        >
          <Plus size={14} />
          Добавить вариант
        </button>
      )}
    </>
  );
}
