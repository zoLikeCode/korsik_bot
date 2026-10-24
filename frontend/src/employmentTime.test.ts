import { describe, expect, it } from "vitest";
import { stageTimeCandidates } from "./employmentTime";

describe("explicit employment-stage scheduling time", () => {
  it("uses the input zone instead of silently using the employee zone", () => {
    expect(stageTimeCandidates("2026-10-24T09:00", "Europe/Moscow")).toEqual([
      "2026-10-24T06:00:00.000Z",
    ]);
    expect(
      stageTimeCandidates("2026-10-24T09:00", "Asia/Yekaterinburg"),
    ).toEqual(["2026-10-24T04:00:00.000Z"]);
  });
  it("does not normalize a nonexistent daylight-saving local time", () => {
    expect(stageTimeCandidates("2026-03-08T02:30", "America/New_York")).toEqual(
      [],
    );
  });
  it("returns both distinct moments when a local clock time is repeated", () => {
    expect(stageTimeCandidates("2026-11-01T01:30", "America/New_York")).toEqual(
      ["2026-11-01T05:30:00.000Z", "2026-11-01T06:30:00.000Z"],
    );
  });
  it("rejects invalid calendar dates and unrecognized time zones", () => {
    expect(stageTimeCandidates("2026-02-31T09:00", "Europe/Moscow")).toEqual(
      [],
    );
    expect(stageTimeCandidates("2026-10-24T09:00", "Invalid/Zone")).toEqual([]);
  });
});
