import { describe, expect, it } from "vitest";
import contract from "./notice-contract.json";
import { applyEvent } from "./reducer";
import type { UiEvent, ViewState } from "./types";

const state: ViewState = {
  stream_id: "stream", last_seq: 0, seen: [], connection: "connected",
  session: { session_id: "session", status: "idle", active: true },
  history: [], active_turn: null, plan: {}, pending_interactions: [], notices: [],
  queued_commands: [], queue_depth: 0,
  usage: { prompt_tokens: null, completion_tokens: null, total_tokens: null,
    request_prompt_tokens: null, request_completion_tokens: null, request_total_tokens: null,
    context_tokens: null, context_limit: null },
};

describe("shared snapshot/live notice contract", () => {
  it.each(contract.cases)("$name", ({ type, payload, expected }) => {
    const event: UiEvent = {
      version: 2, stream_id: "stream", seq: 1, event_id: "event-1", emitted_at: "now",
      project_id: "project", session_id: "session", type, payload,
    };
    const notice = applyEvent(state, event).notices.at(-1);
    expect(notice).toBeDefined();
    expect({ text: notice?.text, code: notice?.code ?? "", params: notice?.params ?? {} }).toEqual(expected);
  });
});
