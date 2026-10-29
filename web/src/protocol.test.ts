import { describe, expect, it } from "vitest";
import { commandAfter, parseEvent } from "./protocol";
import statusContract from "./session-status-contract.json";

const event = {
  version: 2,
  stream_id: "stream",
  event_id: "event-1",
  seq: 1,
  emitted_at: "now",
  project_id: "project",
  session_id: "abc",
  type: "content.delta",
  payload: { piece: "a" },
};

describe("event protocol", () => {
  it.each([
    ...statusContract.valid.map((item) => ({ ...item, valid: true })),
    ...statusContract.invalid.map((item) => ({ ...item, valid: false })),
  ])("status payload: $name", ({ payload, valid }) => {
    const parsed = parseEvent({ ...event, session_id: "session", type: "session.status_changed", payload }, { sessionId: "session", streamId: "stream" });
    expect(parsed.ok).toBe(valid);
    if (!valid) expect(parsed).toEqual({ ok: false, reason: "payload" });
  });

  it("rejects the removed session.snapshot event", () => {
    expect(parseEvent({ ...event, type: "session.snapshot" }, { sessionId: "abc", streamId: "stream" })).toEqual({ ok: false, reason: "type" });
  });
  it("accepts a version 2 event for the current stream", () => {
    const parsed = parseEvent(event, { sessionId: "abc", streamId: "stream" });
    expect(parsed.ok).toBe(true);
  });

  it("rejects a bad version, another session, another stream, and malformed json objects", () => {
    expect(parseEvent({ ...event, version: 1 }, { sessionId: "abc", streamId: "stream" })).toMatchObject({ ok: false, reason: "version" });
    expect(parseEvent({ ...event, session_id: "other" }, { sessionId: "abc", streamId: "stream" })).toMatchObject({ ok: false, reason: "session" });
    expect(parseEvent({ ...event, stream_id: "old" }, { sessionId: "abc", streamId: "stream" })).toMatchObject({ ok: false, reason: "stream" });
    expect(parseEvent({ ...event, payload: null }, { sessionId: "abc", streamId: "stream" })).toMatchObject({ ok: false, reason: "malformed" });
    expect(parseEvent({ ...event, type: "future.event" }, { sessionId: "abc", streamId: "stream" })).toMatchObject({ ok: false, reason: "type" });
  });

  it("keeps the same command id when the outcome is unknown", () => {
    expect(commandAfter("accepted")).toEqual({ phase: "accepted", keepId: false });
    expect(commandAfter("rejected")).toEqual({ phase: "rejected", keepId: false });
    expect(commandAfter("disconnected").keepId).toBe(true);
    expect(commandAfter("missing")).toEqual({ phase: "unknown", keepId: true });
    expect(commandAfter("unknown-status").phase).toBe("unknown");
  });
});
