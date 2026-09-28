import { Brain, Clock, Folder, Gear, Scroll } from "@phosphor-icons/react";
import { setLocale } from "../i18n";
import { Composer, Inspector, SessionRail, Timeline, emptyDraft } from "./widgets";
import type { SessionSummary, ViewState } from "../types";

const session: SessionSummary = {
  session_id: "task104ab",
  status: "waiting_for_input",
  execution: "waiting_for_input",
  user_goal: "Implement atomic Redis leaky-bucket rate limiting in auth middleware",
  model: "deterministic-preview",
  environment: "local",
  branch_name: "feat/rate-limit-redis",
  active: true,
  interaction_mode: "agent",
  permission_mode: "default",
};

const state: ViewState = {
  stream_id: "visual",
  last_seq: 0,
  session,
  history: [],
  active_turn: null,
  plan: {
    objective: "Harden rate limiter against burst race conditions by integrating Redis Lua atomicity and compiling native bindings.",
    steps: [
      { id: "1", title: "Inspect rate limit config and codebase", status: "completed", note: "Completed" },
      { id: "2", title: "Run baseline tests to reproduce the 429 bug", status: "completed", note: "Exit 1" },
      { id: "3", title: "Implement atomic Lua token bucket", status: "completed", note: "+38 -14" },
      { id: "4", title: "Rebuild native bindings", status: "in_progress", note: "Awaiting permission" },
      { id: "5", title: "Verify burst tests and benchmark latency", status: "pending", note: "Pending step 4" },
    ],
  },
  pending_interactions: [{
    request_id: "perm-visual",
    kind: "permission",
    tool_name: "execute_command",
    subject: "build",
    reason: "Clean generated build caches and compile native bindings.",
    grant_summary: "./build, ./dist",
    preview: "rm -rf ./build ./dist && pnpm run build:native",
    command: "rm -rf ./build ./dist && pnpm run build:native",
    risk_flags: ["recursive_delete"],
    choices: [
      { id: "allow_once", label: "Allow once", scope: "This invocation only", persistence: "No save" },
      { id: "allow_session", label: "Allow for this session", scope: "This session", persistence: "Save on this session only" },
      { id: "allow_persistent", label: "Allow permanently", scope: "Later sessions", persistence: "Save in user permissions" },
      { id: "deny", label: "Deny", scope: "No execution", persistence: "No save" },
    ],
  }],
  notices: [],
  queued_commands: [],
  queue_depth: 0,
  usage: {
    prompt_tokens: null, completion_tokens: null, total_tokens: null,
    request_prompt_tokens: null, request_completion_tokens: null, request_total_tokens: null,
    context_tokens: 48620, context_limit: 128000,
  },
  context_breakdown: {
    kind: "tokenizer_estimate",
    exact: false,
    total: 48620,
    limit: 128000,
    categories: [
      { id: "system_prompt", tokens: 14200, share: 0.11 },
      { id: "history", tokens: 22400, share: 0.17 },
      { id: "tool_output", tokens: 8620, share: 0.07 },
      { id: "instructions", tokens: 3400, share: 0.03 },
    ],
    note: "Estimated with the local character tokenizer. Provider billing usage is separate.",
  },
  timeline: [
    { id: "user", kind: "text", role: "user", text: "We're seeing intermittent 429 bypasses during flash traffic spikes. Replace the current in-memory counter in src/middleware/rate-limiter.ts with an atomic Redis leaky-bucket implementation using Lua scripting. Confirm the bug with tests and rebuild native bindings." },
    { id: "reason", kind: "text", role: "assistant", text: "Identified a concurrency race in separate GET/SET calls. Designed an atomic Lua token-bucket script." },
    { id: "read", kind: "tool", name: "read_directory", phase: "succeeded", arguments: { path: "src/middleware/" }, duration_ms: 24, result: { output: "auth.ts\nrate-limiter.ts\nsession.ts\ntelemetry.ts" } },
    { id: "shell", kind: "shell", name: "execute_command", phase: "failed", arguments: { command: "pnpm test -- --grep rate-limiter" }, duration_ms: 1400, result: { returncode: 1, output: "tests/rate-limiter.test.ts > RateLimiter > atomic burst concurrency test (380ms)\nAssertionError: expected status 429, received 200 OK (50 requests leaked)\nTests: 1 failed, 2 passed (3 total)" } },
    { id: "edit", kind: "edit", name: "write_file", phase: "succeeded", arguments: { path: "src/middleware/rate-limiter.ts" }, result: { output: "- const count = await this.redis.get(`rate:${clientId}`); // Race condition\n+ const LUA_TOKEN_BUCKET = `local cur = redis.call('get', KEYS[1]); if not cur or tonumber(cur)>0 then redis.call('decr', KEYS[1]); return 1; end; return 0;`;\n+ const allowed = await this.redis.eval(LUA_TOKEN_BUCKET, 1, `rate:${clientId}`);" } },
    { id: "perm-visual", kind: "approval", phase: "pending", interaction: undefined },
  ],
  subagents: [{ task_id: "worker-1", status: "idle", task: "test-runner-worker" }],
  accessed_files: [
    { path: "src/middleware/rate-limiter.ts", access: "+38 -14", call_id: "edit" },
    { path: "src/middleware/auth.ts", access: "READ", call_id: "read" },
    { path: "tests/rate-limiter.test.ts", access: "READ", call_id: "test" },
  ],
  seen: [],
  connection: "connected",
  agents: [],
  resync: false,
};

state.timeline = state.timeline?.map((item) => item.id === "perm-visual" ? { ...item, interaction: state.pending_interactions[0] } : item);

const sessions: SessionSummary[] = [
  session,
  { session_id: "task101zz", status: "idle", execution: "idle", user_goal: "Benchmark gRPC pool", active: true, environment: "local" },
  { session_id: "done01", status: "idle", user_goal: "Fix Redis connection leak", active: false },
  { session_id: "fail01", status: "failed", user_goal: "Docker compose e2e cluster", active: false },
];

export function VisualFixture() {
  setLocale("en");
  return <div className="app-viewport">
    <header className="topbar">
      <div className="brand"><span className="brand-mark">W</span><strong>Wright</strong><span className="sep">/</span><span className="crumb">api-gateway</span></div>
      <div className="top-center">
        <div className="branch-pill"><span>feat/rate-limit-redis</span><span className="dirty">3 uncommitted</span></div>
        <button type="button" className="search-trigger"><span>Search files, tasks, symbols...</span></button>
      </div>
      <div className="top-status"><span className="active-count">2/4 active</span></div>
    </header>
    <div className="workspace-grid">
      <SessionRail sessions={sessions} selected={session.session_id} onSelect={() => undefined} onCreate={() => undefined} open={false} footer="deterministic-preview · connected">
        <div>
          <div className="section-label"><span>Workspaces</span></div>
          <button type="button" className="nav-row selected"><Folder size={14} />api-gateway</button>
          <button type="button" className="nav-row"><Folder size={14} />auth-service</button>
        </div>
        <div>
          <div className="section-label"><span>Capabilities</span></div>
          <button type="button" className="nav-row"><Brain size={14} />Memory</button>
          <button type="button" className="nav-row"><Scroll size={14} />Rules</button>
          <button type="button" className="nav-row"><Clock size={14} />Schedules</button>
          <button type="button" className="nav-row"><Gear size={14} />Settings</button>
        </div>
      </SessionRail>
      <main className="conversation">
        <div className="conversation-head">
          <div className="task-heading"><span className="task-id-pill">task104</span><h1>{session.user_goal}</h1></div>
          <div className="task-status">
            <span className="status-badge waiting"><span className="dot pulse" />Waiting for input</span>
            <div className="context-meter"><span className="figures"><span>48620 / 128000</span><span>38%</span></span><span className="meter slim"><i style={{ width: "38%" }} /></span></div>
          </div>
        </div>
        <Timeline state={state} respond={() => false} cancelQueued={() => undefined} />
        <div className="composer-wrap">
          <Composer
            sessionId={session.session_id}
            connection="connected"
            draft={{ ...emptyDraft(), documents: [{ id: "rate", filename: "src/middleware/rate-limiter.ts" }] }}
            updateDraft={() => undefined}
            running
            models={["deterministic-preview"]}
            currentModel="deterministic-preview"
            interactionMode="agent"
            permissionMode="default"
            submit={async () => "rejected"}
            cancel={async () => undefined}
          />
        </div>
      </main>
      <Inspector
        state={state}
        sessionId={session.session_id}
        open
        close={() => undefined}
        staticData={{
          files: [
            { name: "rate-limiter.ts", path: "src/middleware/rate-limiter.ts", kind: "file" },
            { name: "auth.ts", path: "src/middleware/auth.ts", kind: "file" },
          ],
          changes: [{ path: "src/middleware/rate-limiter.ts", status: "M" }],
          review: [],
        }}
      />
    </div>
  </div>;
}
