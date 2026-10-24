import type { NodeChange } from "@xyflow/react";

export type FlowNodeMeasurements = Map<
  string,
  { width: number; height: number }
>;

// React Flow reports DOM dimensions separately from the scenario graph. Carry
// them into the next controlled-node projection: omitting `measured` makes
// React Flow discard its handle bounds and hide the node until it is measured
// again. Measurements are view state; recording them must not dirty/autosave
// the scenario or prevent measurement updates while an action is busy.
export function rememberFlowNodeMeasurements(
  measurements: FlowNodeMeasurements,
  changes: NodeChange[],
): void {
  for (const change of changes) {
    if (change.type === "remove") {
      measurements.delete(change.id);
      continue;
    }
    if (change.type !== "dimensions" || !change.dimensions) continue;
    const { width, height } = change.dimensions;
    if (
      Number.isFinite(width) &&
      Number.isFinite(height) &&
      width > 0 &&
      height > 0
    )
      measurements.set(change.id, { width, height });
  }
}
