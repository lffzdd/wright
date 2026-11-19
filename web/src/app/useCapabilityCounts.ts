import { useEffect, useState } from "react";
import { api } from "../api";

export type CapabilityCounts = { memory: number | null; rules: number | null; schedules: number | null };

/**
 * Counts shown next to Memory, Rules and Schedules in the sidebar. They belong
 * to the workspace, so they show even with no task open; they reload when a
 * capability page closes, since it may have changed them.
 */
export function useCapabilityCounts(projectId: string, pageOpen: boolean): CapabilityCounts {
  const [counts, setCounts] = useState<CapabilityCounts>({ memory: null, rules: null, schedules: null });
  useEffect(() => {
    if (pageOpen) return;
    if (!projectId) {
      setCounts({ memory: null, rules: null, schedules: null });
      return;
    }
    let current = true;
    const loadCounts = async () => {
      const [memoryResult, rulesResult, schedulesResult] = await Promise.allSettled([
        api.memory(projectId),
        api.rules(projectId),
        api.projectSchedules(projectId),
      ]);
      if (!current) return;
      const memory = memoryResult.status === "fulfilled" ? memoryResult.value : null;
      const facts = Array.isArray(memory?.facts) ? memory.facts as Array<Record<string, unknown>> : [];
      const commitments = Array.isArray(memory?.commitments) ? memory.commitments as Array<Record<string, unknown>> : [];
      setCounts({
        memory: memoryResult.status === "fulfilled"
          ? facts.filter((fact) => fact.status === "active" || fact.status === "quarantined").length + commitments.filter((item) => item.status === "open").length
          : null,
        rules: rulesResult.status === "fulfilled"
          ? (rulesResult.value.rules ?? []).filter((rule) => typeof rule.id === "string" && rule.id.length > 0).length
          : null,
        schedules: schedulesResult.status === "fulfilled" ? schedulesResult.value.length : null,
      });
    };
    void loadCounts();
    return () => { current = false; };
  }, [projectId, pageOpen]);
  return counts;
}
