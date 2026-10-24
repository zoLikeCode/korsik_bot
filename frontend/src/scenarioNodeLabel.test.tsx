import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { ScenarioNodeLabel, scenarioNodeTiming } from "./pages/Scenarios";
import type { Step } from "./scenario";

function message(trigger?: any, text = "Добро пожаловать в команду"): Step {
  return {
    step_key: "welcome",
    kind: "MESSAGE",
    mandatory: true,
    sequence_no: 1,
    eligibility: {},
    delivery_policy: {},
    trigger,
    content: { text_ty: text },
  };
}

describe("scenario card schedule labels", () => {
  it("separates the calendar offset from the actual launch time", () => {
    const trigger = {
      type: "ANCHOR_TIME",
      anchor: "START_DATE",
      day_basis: "CALENDAR",
      offset_days: -3,
      local_time: "09:30",
    };
    expect(scenarioNodeTiming(trigger)).toEqual({
      description: "От выхода -3 кал. дней",
      value: "09:30",
      clock: true,
    });
    const html = renderToStaticMarkup(
      <ScenarioNodeLabel step={message(trigger)} />,
    );
    expect(html).toContain('class="flow-node-offset"');
    expect(html).toContain('title="От выхода -3 кал. дней"');
    expect(html).toMatch(/<time\b[^>]*dateTime="09:30"[^>]*>09:30<\/time>/i);
    expect(html.indexOf("flow-node-offset")).toBeLessThan(
      html.indexOf("<time"),
    );
  });

  it("retains the workday number and probation-end anchor", () => {
    expect(
      scenarioNodeTiming({
        type: "ANCHOR_TIME",
        anchor: "PROBATION_END",
        day_basis: "WORKING",
        workday_number: 2,
        local_time: "14:35",
      }),
    ).toEqual({
      description: "Рабочий день № 2 от конца ИС",
      value: "14:35",
      clock: true,
    });
  });

  it("retains relative months and working days without mixing in the clock", () => {
    expect(
      scenarioNodeTiming({
        type: "ANCHOR_TIME",
        anchor: "PROBATION_END",
        day_basis: "WORKING",
        offset_months: -1,
        offset_days: 3,
        local_time: "16:20",
      }),
    ).toEqual({
      description: "От конца ИС -1 мес. +3 раб. дней",
      value: "16:20",
      clock: true,
    });
  });

  it("renders an after-step delay as a duration, never as a clock time", () => {
    const trigger = {
      type: "AFTER_STEP",
      after_step_key: "intro",
      offset_minutes: 45,
    };
    expect(scenarioNodeTiming(trigger)).toEqual({
      description: "После шага intro",
      value: "+45 мин.",
      clock: false,
    });
    const html = renderToStaticMarkup(
      <ScenarioNodeLabel step={message(trigger)} />,
    );
    expect(html).toContain('class="flow-node-time">+45 мин.</span>');
    expect(html).not.toMatch(/<time\b/i);
    expect(html).not.toContain("10:00");
  });

  it.each([
    [undefined, "Переход по условию"],
    [
      { type: "SIGNAL", signal_code: "HR_APPROVAL" },
      "После подтверждения: HR_APPROVAL",
    ],
  ])("does not invent a launch time for %j", (trigger, description) => {
    expect(scenarioNodeTiming(trigger)).toEqual({
      description,
      value: null,
      clock: false,
    });
    const html = renderToStaticMarkup(
      <ScenarioNodeLabel step={message(trigger)} />,
    );
    expect(html).toContain(description);
    expect(html).not.toContain("flow-node-time");
    expect(html).not.toMatch(/<time\b/i);
    expect(html).not.toContain("10:00");
  });

  it("keeps the complete card text available in its title", () => {
    const text =
      "Для того, чтобы ты пришёл подготовленным в свой первый рабочий день, мы заранее отправим полную инструкцию по доступу в офис и необходимые контакты.";
    const html = renderToStaticMarkup(
      <ScenarioNodeLabel step={message(undefined, text)} />,
    );
    expect(html).toContain(`<strong title="${text}">${text}</strong>`);
    expect(html).toContain('title="Сообщение"');
    expect(html).not.toContain("обязательный");
  });
});
