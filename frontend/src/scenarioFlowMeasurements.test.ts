import { describe, expect, it } from "vitest";
import type { InternalNode, Node, NodeChange } from "@xyflow/react";
import { adoptUserNodes } from "@xyflow/system";
import {
  rememberFlowNodeMeasurements,
  type FlowNodeMeasurements,
} from "./scenarioFlowMeasurements";

function projection(
  measurements: FlowNodeMeasurements,
  options: { label?: string; selected?: boolean; x?: number } = {},
): Node[] {
  return ["welcome", "finish"].map((id, index) => ({
    id,
    position: { x: options.x ?? index * 340, y: 0 },
    data: { label: options.label ?? id },
    selected: options.selected ?? false,
    measured: measurements.get(id),
  }));
}

function measuredFlow() {
  const measurements: FlowNodeMeasurements = new Map();
  const lookup = new Map<string, InternalNode>();
  const parents = new Map<string, Map<string, InternalNode>>();
  adoptUserNodes(projection(measurements), lookup, parents);
  const changes: NodeChange[] = [
    {
      id: "welcome",
      type: "dimensions",
      dimensions: { width: 280, height: 154 },
    },
    {
      id: "finish",
      type: "dimensions",
      dimensions: { width: 280, height: 114 },
    },
  ];
  // Model React Flow's initial ResizeObserver result and its public callback.
  for (const change of changes) {
    if (change.type !== "dimensions") continue;
    const internal = lookup.get(change.id)!;
    internal.measured = { ...change.dimensions };
    internal.internals.handleBounds = { source: [], target: [] };
  }
  rememberFlowNodeMeasurements(measurements, changes);
  return { measurements, lookup, parents };
}

describe("controlled scenario node measurements", () => {
  it("reproduces the visibility reset when a fresh node drops its dimensions", () => {
    const { lookup, parents } = measuredFlow();
    const result = adoptUserNodes(
      projection(new Map(), { label: "Изменённый текст" }),
      lookup,
      parents,
    );
    expect(result.nodesInitialized).toBe(false);
    expect(lookup.get("welcome")?.measured.width).toBeUndefined();
    expect(lookup.get("welcome")?.internals.handleBounds).toBeUndefined();
  });

  it.each([
    { label: "Изменённый текст без мигания" },
    { selected: true },
    { x: 184 },
  ])("keeps nodes and connection handles initialized after %j", (options) => {
    const { measurements, lookup, parents } = measuredFlow();
    const originalBounds = [...lookup.values()].map(
      (node) => node.internals.handleBounds,
    );
    const result = adoptUserNodes(
      projection(measurements, options),
      lookup,
      parents,
    );
    expect(result.nodesInitialized).toBe(true);
    expect(lookup.get("welcome")?.measured).toEqual({
      width: 280,
      height: 154,
    });
    expect(lookup.get("finish")?.measured).toEqual({ width: 280, height: 114 });
    expect(lookup.get("welcome")?.internals.handleBounds).toBe(
      originalBounds[0],
    );
    expect(lookup.get("finish")?.internals.handleBounds).toBe(
      originalBounds[1],
    );
  });

  it("carries forward a genuine DOM height change on the next edit", () => {
    const { measurements, lookup, parents } = measuredFlow();
    rememberFlowNodeMeasurements(measurements, [
      {
        id: "welcome",
        type: "dimensions",
        dimensions: { width: 280, height: 198 },
      },
    ]);
    const result = adoptUserNodes(
      projection(measurements, { label: "Текст в несколько строк" }),
      lookup,
      parents,
    );
    expect(result.nodesInitialized).toBe(true);
    expect(lookup.get("welcome")?.measured).toEqual({
      width: 280,
      height: 198,
    });
    expect(lookup.get("finish")?.measured.height).toBe(114);
  });

  it("does not copy dimensions to newly added nodes or treat movement as measurement", () => {
    const { measurements } = measuredFlow();
    const before = [...measurements];
    rememberFlowNodeMeasurements(measurements, [
      { id: "welcome", type: "position", position: { x: 42, y: 56 } },
      { id: "welcome", type: "select", selected: true },
      { type: "add", item: { id: "new", position: { x: 0, y: 0 }, data: {} } },
    ]);
    expect([...measurements]).toEqual(before);
    expect(measurements.has("new")).toBe(false);
  });

  it("ignores missing, partial, non-finite and non-positive measurements", () => {
    const { measurements } = measuredFlow();
    for (const dimensions of [
      undefined,
      { width: 280 },
      { width: NaN, height: 100 },
      { width: 280, height: Infinity },
      { width: 0, height: 100 },
      { width: 280, height: -1 },
    ]) {
      rememberFlowNodeMeasurements(measurements, [
        { id: "welcome", type: "dimensions", dimensions } as NodeChange,
      ]);
      expect(measurements.get("welcome")).toEqual({ width: 280, height: 154 });
    }
  });

  it("forgets a removed node's old dimensions before an id can be reused", () => {
    const { measurements } = measuredFlow();
    rememberFlowNodeMeasurements(measurements, [
      { id: "welcome", type: "remove" },
    ]);
    expect(measurements.has("welcome")).toBe(false);
    expect(measurements.has("finish")).toBe(true);
  });
});
