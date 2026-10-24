import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  Message,
  conversationNeedsReply,
  postDialogMessage,
} from "./pages/Chat";
import { DialogCount } from "./DialogNavBadge";
afterEach(() => vi.unstubAllGlobals());
const message = (author_type = "EMPLOYEE", more = {}) => ({
  id: "question",
  seq: 12,
  author_type,
  text: "Где кабинет?",
  created_at: "2026-10-24T10:00:00Z",
  ...more,
});
describe("question-level dialogs", () => {
  it("places the employee left/green and every Korsik response right/blue without HR labels", () => {
    const employee = renderToStaticMarkup(
      <Message message={message()} zone="Europe/Moscow" />,
    );
    expect(employee).toContain("employee-message");
    expect(employee).toContain("Сотрудник");
    for (const author of ["BOT", "HR", "SYSTEM"]) {
      const response = renderToStaticMarkup(
        <Message message={message(author)} zone="Europe/Moscow" />,
      );
      expect(response).toContain("korsik-message");
      expect(response).toContain("Корсик");
      expect(response).not.toContain("Команда поддержки");
    }
  });
  it("marks only an unresolved employee question for HR and keeps the employee view neutral", () => {
    const failed = message("EMPLOYEE", { support_issue: { status: "OPEN" } });
    expect(
      renderToStaticMarkup(
        <Message message={failed} showProblems zone="Europe/Moscow" />,
      ),
    ).toContain("Корсик не знает ответа");
    expect(
      renderToStaticMarkup(<Message message={failed} zone="Europe/Moscow" />),
    ).not.toContain("Корсик не знает ответа");
    for (const [author, status] of [
      ["EMPLOYEE", "RESOLVED"],
      ["BOT", "OPEN"],
    ])
      expect(
        renderToStaticMarkup(
          <Message
            message={message(author, { support_issue: { status } })}
            showProblems
            zone="Europe/Moscow"
          />,
        ),
      ).not.toContain("Корсик не знает ответа");
  });
  it("renders the authorized quote with escaped content and a message selection action", () => {
    const html = renderToStaticMarkup(
      <Message
        message={message("HR", {
          reply_to: {
            author_type: "EMPLOYEE",
            text: "<script>private</script>",
          },
        })}
        onReply={() => {}}
        zone="Europe/Moscow"
      />,
    );
    expect(html).toContain("dialog-message-quote");
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain("<script>");
    expect(html).toContain('aria-label="Ответить на сообщение №12"');
  });
  it("uses the exact unresolved count even when the old handoff remains open", () => {
    expect(
      conversationNeedsReply({
        mode: "HUMAN",
        active_handoff_case_id: "old",
        unresolved_question_count: 0,
      }),
    ).toBe(false);
    expect(
      conversationNeedsReply({ mode: "BOT", unresolved_question_count: 2 }),
    ).toBe(true);
    expect(renderToStaticMarkup(<DialogCount count={2} />)).toContain(
      "Диалогов с вопросами без ответа: 2",
    );
    expect(renderToStaticMarkup(<DialogCount count={0} />)).toBe("");
    expect(renderToStaticMarkup(<DialogCount count={null} />)).toBe("");
  });
  it("sends HR directly with selected message and stable client UUID without extra claim calls", async () => {
    vi.stubGlobal("document", { cookie: "korsik_csrf=synthetic" });
    const fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ message_id: "sent" }), {
          status: 201,
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetch);
    await postDialogMessage(
      "conv",
      false,
      { kind: "TEXT", text: "Вот ответ" },
      "stable-client",
      "original-question",
      '"conv:5"',
    );
    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, options] = fetch.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    expect(url).toBe("/api/v1/conversations/conv/messages");
    expect(JSON.parse(String(options.body))).toEqual({
      text: "Вот ответ",
      client_message_id: "stable-client",
      reply_to_message_id: "original-question",
    });
    expect(new Headers(options.headers).get("If-Match")).toBe('"conv:5"');
  });
  it("keeps employee quote context in the common turn protocol", async () => {
    vi.stubGlobal("document", { cookie: "" });
    const fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ dispatch: "BOT_ANSWER" }), {
          status: 202,
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetch);
    await postDialogMessage(
      "conv",
      true,
      { kind: "TEXT", text: "Уточнение" },
      "employee-client",
      "bot-message",
    );
    const [url, options] = fetch.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    expect(url).toBe("/api/v1/conversations/conv/turns");
    expect(JSON.parse(String(options.body))).toEqual({
      input: { kind: "TEXT", text: "Уточнение" },
      client_message_id: "employee-client",
      reply_to_message_id: "bot-message",
    });
  });
});
