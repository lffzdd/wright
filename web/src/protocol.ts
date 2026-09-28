import type { UiEvent } from "./types";
import contract from "./ui-event-contract.json";

const EVENT_TYPES = new Set(contract.types);

export type ParseResult =
  | { ok: true; event: UiEvent }
  | { ok: false; reason: "malformed" | "version" | "session" | "stream" | "type" };

export function parseEvent(
  value: unknown,
  expected: { sessionId: string; streamId: string },
): ParseResult {
  if (!value || typeof value !== "object" || Array.isArray(value)) return { ok: false, reason: "malformed" };
  const event = value as Record<string, unknown>;
  if (event.type === "snapshot_required") return { ok: false, reason: "malformed" };
  if (typeof event.version !== "number" || typeof event.seq !== "number" || typeof event.event_id !== "string") {
    return { ok: false, reason: "malformed" };
  }
  if (typeof event.stream_id !== "string" || typeof event.session_id !== "string" || typeof event.type !== "string") {
    return { ok: false, reason: "malformed" };
  }
  if (!event.payload || typeof event.payload !== "object" || Array.isArray(event.payload)) {
    return { ok: false, reason: "malformed" };
  }
  if (event.version !== contract.version) return { ok: false, reason: "version" };
  if (!EVENT_TYPES.has(event.type)) return { ok: false, reason: "type" };
  if (event.session_id !== expected.sessionId) return { ok: false, reason: "session" };
  if (expected.streamId && event.stream_id !== expected.streamId) return { ok: false, reason: "stream" };
  return { ok: true, event: event as unknown as UiEvent };
}

export type CommandPhase = "idle" | "awaiting" | "accepted" | "rejected" | "unknown";

export function commandAfter(
  outcome: "sent" | "accepted" | "rejected" | "disconnected" | "missing" | "unknown-status",
): { phase: CommandPhase; keepId: boolean } {
  if (outcome === "accepted") return { phase: "accepted", keepId: false };
  if (outcome === "rejected") return { phase: "rejected", keepId: false };
  if (outcome === "sent") return { phase: "awaiting", keepId: true };
  return { phase: "unknown", keepId: true };
}
