import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import RussiaTimeZoneField, {
  RUSSIA_TIME_ZONES,
  russiaTimeZoneLabel,
} from "./RussiaTimeZoneField";
import {
  EmployeeCreateFields,
  EmployeeDetailHeader,
  EmployeeInvitation,
  EmployeePagination,
  EmployeeDeleteConfirmation,
  deleteEmployeeRecord,
} from "./pages/Employees";
import {
  employeeCreatePayload,
  employeeListPath,
  employeeDateChanges,
  employeeDatesDirty,
  employeeInviteUrl,
  employeeEffectiveEnabled,
  moscowTodayISO,
  type EmployeeCreateFields as CreateValues,
} from "./employeeAdministration";
import { ApiError } from "./api";

afterEach(() => vi.unstubAllGlobals());
const form = (): CreateValues => ({
  name: "  Анна Иванова ",
  email: " anna@example.test ",
  manager_id: "manager-user",
  start_date: "2026-10-24",
  probation_end_date: "2027-01-24",
  timezone: "Asia/Vladivostok",
});
describe("compact employee administration", () => {
  it("creates only visible form fields even if a previous form object still contains hidden administration values", () => {
    const stale = {
      ...form(),
      site_id: "old-site",
      employment_type: "PRODUCTION",
      scenario_id: "old-scenario",
      assigned_hr_id: "old-hr",
    };
    expect(employeeCreatePayload(stale)).toEqual({
      name: "Анна Иванова",
      email: "anna@example.test",
      manager_id: "manager-user",
      start_date: "2026-10-24",
      probation_end_date: "2027-01-24",
      timezone: "Asia/Vladivostok",
    });
    const blank = form();
    blank.email = "";
    blank.manager_id = "";
    blank.probation_end_date = "";
    const payload = employeeCreatePayload(blank);
    for (const omitted of [
      "email",
      "manager_id",
      "probation_end_date",
      "site_id",
      "employment_type",
      "scenario_id",
      "assigned_hr_id",
    ])
      expect(payload).not.toHaveProperty(omitted);
  });
  it("renders the requested creation fields and native timezone selection without site, type, program or HR controls", () => {
    const html = renderToStaticMarkup(
      <EmployeeCreateFields
        form={form()}
        managers={[{ id: "manager-user", name: "Руководитель" }]}
        onChange={() => {}}
      />,
    );
    for (const label of [
      "Имя и фамилия",
      "Рабочая почта",
      "Руководитель",
      "Дата выхода",
      "Конец испытательного срока",
      "Часовой пояс",
    ])
      expect(html).toContain(label);
    for (const removed of [
      "Площадка",
      "Тип сотрудника",
      "Программа адаптации",
      "Ответственный HR",
    ])
      expect(html).not.toContain(removed);
    expect(html.match(/<input[^>]*type="date"/g)).toHaveLength(2);
    expect(html).toContain('value="Asia/Vladivostok" selected=""');
  });
  it("keeps pagination and filters on the server, including names absent from the current page", () => {
    const url = new URL(
      employeeListPath(3, " Анна & Борис ", "ACTIVE"),
      "https://example.test",
    );
    expect(url.pathname).toBe("/employees");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      page: "3",
      page_size: "10",
      search: "Анна & Борис",
      stage: "ACTIVE",
    });
    expect(employeeListPath(1, "", "")).toBe("/employees?page=1&page_size=10");
    const last = renderToStaticMarkup(
      <EmployeePagination
        page={3}
        pages={3}
        total={24}
        busy={false}
        onChange={() => {}}
      />,
    );
    expect(last).toContain("Страница 3 из 3");
    expect(last).toContain("Всего 24");
    expect(last.match(/disabled=""/g)).toHaveLength(1);
    const empty = renderToStaticMarkup(
      <EmployeePagination
        page={1}
        pages={1}
        total={0}
        busy={false}
        onChange={() => {}}
      />,
    );
    expect(empty.match(/disabled=""/g)).toHaveLength(2);
  });
  it("does not include access, employment stage, binding or other date when saving one changed date", () => {
    const baseline = {
      start_date: "2026-10-24",
      probation_end_date: "2027-01-24",
    };
    expect(employeeDateChanges("2026-10-25", "2027-01-24", baseline)).toEqual({
      start_date: "2026-10-25",
    });
    expect(employeeDateChanges("2026-10-24", "2027-01-25", baseline)).toEqual({
      probation_end_date: "2027-01-25",
    });
    expect(employeeDatesDirty("", baseline.probation_end_date, baseline)).toBe(
      true,
    );
    expect(
      employeeDateChanges("", baseline.probation_end_date, baseline),
    ).toEqual({});
    expect(
      employeeDatesDirty(
        baseline.start_date,
        baseline.probation_end_date,
        baseline,
      ),
    ).toBe(false);
  });
  it("offers reversible enabled access without presenting employment-end actions", () => {
    const header = renderToStaticMarkup(
      <EmployeeDetailHeader
        employee={{ name: "Анна", enabled: false, employment_stage: "ACTIVE" }}
        editable
        busy={false}
        onEnabledChange={() => {}}
      />,
    );
    expect(header).toContain("Активен");
    expect(header).toContain("Выключен");
    expect(header.match(/<option/g)).toHaveLength(2);
    expect(header).toContain('value="false" selected=""');
    expect(header).not.toContain("Завершить найм");
    expect(
      employeeEffectiveEnabled({ enabled: true, employment_stage: "ENDED" }),
    ).toBe(false);
    const ended = renderToStaticMarkup(
      <EmployeeDetailHeader
        employee={{ name: "Анна", enabled: true, employment_stage: "ENDED" }}
        editable
        busy={false}
        onEnabledChange={() => {}}
      />,
    );
    expect(ended).toMatch(/<select[^>]*disabled=""/);
    expect(ended).toContain('value="false" selected=""');
  });
  it("renders a MAX invitation link without exposing raw tokens or requiring extra HR confirmation", () => {
    const invite = {
      invite_url: "https://max.ru/example_bot?start=join_opaque",
      token: "raw-token-never-display",
    };
    const html = renderToStaticMarkup(
      <EmployeeInvitation
        invite={invite}
        onCopied={() => {}}
        onError={() => {}}
      />,
    );
    expect(html).toContain("Скопировать ссылку");
    expect(html).toContain(invite.invite_url);
    expect(html).not.toContain(invite.token);
    expect(html).not.toContain("Подтвердить привязку");
    expect(employeeInviteUrl({ token: "raw-only" })).toBe("");
    expect(employeeInviteUrl({ invite_url: "javascript:alert(1)" })).toBe("");
  });
});
describe("Russian IANA timezone selector", () => {
  it("contains all 26 verified RU zone.tab entries and eleven distinct current UTC offsets", () => {
    expect(RUSSIA_TIME_ZONES).toHaveLength(26);
    expect(new Set(RUSSIA_TIME_ZONES.map((zone) => zone.value)).size).toBe(26);
    expect(new Set(RUSSIA_TIME_ZONES.map((zone) => zone.offset)).size).toBe(11);
    expect(RUSSIA_TIME_ZONES.map((zone) => zone.value)).toContain(
      "Europe/Kaliningrad",
    );
    expect(RUSSIA_TIME_ZONES.map((zone) => zone.value)).toContain(
      "Asia/Anadyr",
    );
    for (const zone of RUSSIA_TIME_ZONES)
      expect(() =>
        new Intl.DateTimeFormat("ru", { timeZone: zone.value }).format(
          new Date(),
        ),
      ).not.toThrow();
  });
  it("uses a native select with Russian labels and IANA values instead of an arbitrary timezone input", () => {
    const html = renderToStaticMarkup(
      <RussiaTimeZoneField value="Europe/Moscow" onChange={() => {}} />,
    );
    expect(html.match(/<select/g)).toHaveLength(1);
    expect(html.match(/<option/g)).toHaveLength(26);
    expect(html).not.toContain("<input");
    expect(html).toContain('value="Europe/Moscow" selected=""');
    expect(russiaTimeZoneLabel("Europe/Moscow")).toBe("UTC+03:00 — Москва");
    expect(russiaTimeZoneLabel("Asia/Anadyr")).toBe("UTC+12:00 — Анадырь");
  });
  it("creates an ISO employment date in Moscow even near a UTC day boundary", () => {
    expect(moscowTodayISO(new Date("2026-10-24T22:15:00Z"))).toBe("2026-10-25");
  });
});

describe("logical employee deletion", () => {
  it("places the delete action beside access only for an editable employee", () => {
    const props = {
      employee: { id: "person", name: "Анна", employment_stage: "ACTIVE" },
      busy: false,
      onEnabledChange: () => {},
      onDelete: () => {},
    };
    const editable = renderToStaticMarkup(
      <EmployeeDetailHeader {...props} editable />,
    );
    expect(editable).toContain('aria-label="Удалить сотрудника"');
    expect(editable).toMatch(
      /type="button"[^>]*aria-label="Удалить сотрудника"/,
    );
    expect(
      renderToStaticMarkup(
        <EmployeeDetailHeader {...props} editable={false} />,
      ),
    ).not.toContain('aria-label="Удалить сотрудника"');
  });
  it("names the employee and explains removal/access revocation while preserving history", () => {
    const html = renderToStaticMarkup(
      <EmployeeDeleteConfirmation
        employee={{
          id: "person",
          name: "Анна <Иванова>",
          etag: '\"person:3\"',
        }}
        busy={false}
        error=""
        conflict={false}
        onConfirm={() => {}}
        onCancel={() => {}}
        onReload={() => {}}
      />,
    );
    expect(html).toContain('role="alertdialog"');
    expect(html).toContain("Анна &lt;Иванова&gt;");
    expect(html).toContain("исчезнет из списка");
    expect(html).toContain("доступ к кабинету и боту будет выключен");
    expect(html).toContain("История адаптации и ответов сохранится");
    expect(html).not.toContain("безвозвратно");
  });
  it("blocks confirmation after a stale write and requires an explicit current-record reload", () => {
    const html = renderToStaticMarkup(
      <EmployeeDeleteConfirmation
        employee={{ id: "person", name: "Анна", etag: '\"person:3\"' }}
        busy={false}
        error="Карточка изменена"
        conflict
        onConfirm={() => {}}
        onCancel={() => {}}
        onReload={() => {}}
      />,
    );
    expect(html).toContain("Загрузить актуальные данные");
    expect(html).toMatch(
      /<button[^>]*disabled=""[^>]*>Удалить сотрудника<\/button>/,
    );
  });
  it("uses the displayed employee ETag for a bodyless authenticated DELETE and never auto-retries a conflict", async () => {
    vi.stubGlobal("document", { cookie: "korsik_csrf=csrf-test" });
    const fetch = vi.fn(
      async () =>
        new Response(JSON.stringify({ detail: "stale_etag" }), {
          status: 412,
          headers: { "Content-Type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", fetch);
    await expect(
      deleteEmployeeRecord({
        id: "person",
        name: "Анна",
        etag: '\"person:3\"',
      }),
    ).rejects.toBeInstanceOf(ApiError);
    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, options] = fetch.mock.calls[0] as unknown as [
      string,
      RequestInit,
    ];
    expect(url).toBe("/api/v1/employees/person");
    expect(options.method).toBe("DELETE");
    expect(options.body).toBeUndefined();
    expect(options.credentials).toBe("include");
    expect(new Headers(options.headers).get("If-Match")).toBe('\"person:3\"');
    expect(new Headers(options.headers).get("X-CSRF-Token")).toBe("csrf-test");
    expect(new Headers(options.headers).get("Idempotency-Key")).toMatch(
      /^[0-9a-f-]{36}$/,
    );
  });
  it("returns successful logical deletion only after server acceptance and refuses a missing version", async () => {
    vi.stubGlobal("document", { cookie: "" });
    const fetch = vi.fn(
      async () =>
        new Response(
          JSON.stringify({ id: "person", status: "DELETED", enabled: false }),
          {
            headers: {
              "Content-Type": "application/json",
              ETag: '\"person:4\"',
            },
          },
        ),
    );
    vi.stubGlobal("fetch", fetch);
    const result = await deleteEmployeeRecord({
      id: "person",
      name: "Анна",
      etag: '\"person:3\"',
    });
    expect(result.data).toMatchObject({ status: "DELETED", enabled: false });
    expect(result.etag).toBe('\"person:4\"');
    await expect(
      deleteEmployeeRecord({ id: "person", name: "Анна", etag: "" }),
    ).rejects.toThrow("Дождитесь загрузки");
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
