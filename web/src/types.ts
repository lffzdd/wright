export type SessionLifecycle = "open" | "closing" | "closed";
export type SessionExecution = "idle" | "running" | "queued" | "waiting_for_input" | "cancelling";

export type FileReference = {
  kind: "file";
  path: string;
  name: string;
  project_id: string;
  external?: boolean;
};

export type SessionSummary = {
  session_id: string;
  status: string;
  lifecycle?: SessionLifecycle;
  execution?: SessionExecution;
  queue_reason?: string;
  agent_status?: string;
  user_goal?: string;
  model?: string;
  interaction_mode?: "agent" | "plan" | "ask";
  permission_mode?: string | null;
  environment?: "local" | "worktree";
  execution_root?: string;
  branch_name?: string;
  pending_interactions?: number;
  active: boolean;
  recoverable?: boolean;
  saved_at?: string;
};

export type ToolState = {
  call_id: string;
  name: string;
  arguments?: Record<string, unknown>;
  output?: string;
  ok?: boolean;
  err?: string;
  data?: unknown;
  phase?: "planned" | "awaiting_approval" | "running" | "succeeded" | "failed";
  artifacts?: ArtifactRef[];
};

export type ArtifactRef = {
  id: string;
  media_type: string;
  name: string;
  size: number;
  run_id?: string;
  call_id?: string;
};

export type Attachment = {
  id: string;
  filename: string;
  media_type: string;
  size: number;
  width: number;
  height: number;
};

export type ActiveTurn = {
  turn_id?: string;
  run_id?: string;
  prompt: string;
  attachments: Attachment[];
  reasoning: string;
  content: string;
  tools: ToolState[];
};

export type HistoryTurn = {
  turn_id?: string;
  run_id?: string;
  status?: string;
  user: string;
  assistant: string;
  attachments?: Attachment[];
  tools?: ToolState[];
};

export type AgentView = {
  task_id: string;
  depth: number;
  content: string;
  tools: ToolState[];
  status: string;
  request_usage?: {
    prompt_tokens: number | null;
    completion_tokens: number | null;
    total_tokens: number | null;
  };
};

export type Interaction = {
  request_id: string;
  kind: "permission" | "ask_user";
  agent_task_id?: string;
  agent_depth?: number;
  question?: string;
  context?: string;
  options?: string[];
  tool_name?: string;
  subject?: string;
  reason?: string;
  reason_code?: string;
  reason_params?: Record<string, string>;
  summary_code?: string;
  summary_params?: Record<string, string>;
  risk_flags?: string[];
  targets?: string[];
  principal?: string;
  operation?: string;
  grant_summary?: string;
  preview?: string;
  preview_truncated?: boolean;
  risk_level?: "review" | "elevated";
  cwd?: string;
  command?: string;
  http_method?: string;
  http_target?: string;
  shell_note?: string;
  choices?: Array<{
    id: string;
    label: string;
    scope: string;
    persistence: string;
    lifetime?: string; resource_kind?: string; operations?: string[]; resource?: Record<string, unknown>;
  }>;
};

export type QueuedCommand = { command_id: string; prompt: string; attachments?: Attachment[] };

export type Notice = {
  id: string;
  type: string;
  text: string;
  kind?: string;
  code?: string;
  params?: Record<string, string>;
};

export type TimelineItem = {
  id: string;
  turn_id?: string;
  attachments?: Attachment[];
  output?: string;
  kind: "text" | "reasoning" | "tool" | "shell" | "edit" | "approval";
  role?: string;
  text?: string;
  name?: string;
  phase?: string;
  call_id?: string;
  arguments?: Record<string, unknown>;
  result?: Record<string, unknown> | null;
  duration_ms?: number | null;
  order?: number;
  interaction?: Interaction;
};

export type ContextCategory = { id: string; tokens: number; share: number };

export type ContextBreakdown = {
  kind?: string;
  exact?: boolean;
  total?: number;
  limit?: number | null;
  system_prompt_contains_core_memory?: boolean;
  categories?: ContextCategory[];
  note?: string;
};

export type ReviewChange = {
  path: string;
  state: string;
  origin?: string;
  kind?: string;
  display_kind?: string;
  rename_with?: string;
  review?: string;
  reversible?: boolean;
  current_sha256?: string | null;
};

export type WaitReason = { kind: "permission" | "ask_user" | "user_input" | "blocked"; request_ids: string[]; detail?: string };
export type PlanStep = {
  id: string; title: string; status: string; note?: string;
  started_at: number | null; ended_at: number | null; elapsed_ms?: number; wait_reason?: WaitReason;
};
export type PlanView = { objective?: string; status?: string; revision?: number; observed_at?: number; steps?: PlanStep[]; wait_reason?: WaitReason };
export type PolicyDefault = { decision: "allow" | "ask" | "deny"; source: string; reason_code: string; reason_params: Record<string, string> };
export type EffectivePolicy = {
  operation: "file_read" | "file_write" | "shell";
  defaults: { in_scope: PolicyDefault; outside_scope: PolicyDefault };
  directories: string[];
  read_only: string[];
  rules: Array<{ effect: "allow" | "ask" | "deny"; scope: "session" | "project" | "user"; rule: Record<string, unknown>; target: string; resource_kind: string; operations: string[]; tool: string; description: string; conditional: boolean }>;
  constraints: string[]; precedence: string[];
};
export type SandboxStatus = { platform: string; provider: string; available: boolean; state: "ready" | "setup_required" | "initializing" | "failed"; detail?: string };
export type Grants = {
  version: string;
  grants: Array<{ id: string; source: string; lifetime: string; resource_kind: string; target: string; http_methods?: string[]; operations: string[]; recursive: boolean; cwd?: string; tool?: string; version?: string; project_id?: string }>;
  sandbox?: SandboxStatus | null;
  permission_mode?: string; interaction_mode?: string;
  boundary_codes?: string[];

  effective_policy: EffectivePolicy[];
};

export type Snapshot = {
  stream_id: string;
  last_seq: number;
  session: SessionSummary;
  history: HistoryTurn[];
  active_turn: ActiveTurn | null;
  agents?: AgentView[];
  plan: PlanView;
  pending_interactions: Interaction[];
  notices: Notice[];
  queued_commands: QueuedCommand[];
  queue_depth: number;
  timeline?: TimelineItem[];
  subagents?: Array<{ task_id?: string; parent_id?: string; status?: string; task?: string; ended_at?: string | null }>;
  accessed_files?: Array<{ path: string; access?: string; tool?: string; call_id?: string }>;
  context_breakdown?: ContextBreakdown;
  usage: {
    prompt_tokens: number | null;
    completion_tokens: number | null;
    total_tokens: number | null;
    request_prompt_tokens: number | null;
    request_completion_tokens: number | null;
    request_total_tokens: number | null;
    context_tokens: number | null;
    context_limit: number | null;
  };
};

export type UiEvent = {
  version: number;
  stream_id: string;
  event_id: string;
  seq: number;
  emitted_at: string;
  project_id: string;
  session_id: string;
  turn_id?: string;
  type: string;
  payload: Record<string, unknown>;
};

export type ViewState = Snapshot & {
  permissions_revision?: number;
  seen: string[];
  connection: "connecting" | "connected" | "reconnecting" | "disconnected" | "closed";
  resync?: boolean;
};
