export type ExecutionMode = "sandbox" | "host";

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
  project_id?: string;
  project_root?: string;
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
  execution_mode?: ExecutionMode;
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
  /** Index of the model step whose text and reasoning are being streamed. */
  segment?: number;
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
  execution_mode?: ExecutionMode;
  sandbox_readable?: string[];
  sandbox_writable?: string[];
  choices?: Array<{
    id: string;
    label: string;
    scope: string;
    persistence: string;
    lifetime?: string; resource_kind?: string; operations?: string[]; resource?: PermissionResource;
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
  kind: "text" | "reasoning" | "tool" | "shell" | "edit" | "approval" | "status";
  status?: string;
  run_id?: string;
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
  /** Estimated before any request was assembled. */
  preview?: boolean;
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
  additions?: number;
  deletions?: number;
  binary?: boolean;
  truncated?: boolean;
  can_revert?: boolean;
  reason?: string;
  committed?: boolean;
};

export type GitChangeScope = "working" | "unstaged" | "staged";
export type ChangeScope = GitChangeScope | "session";
export type GitChange = {
  path: string;
  old_path?: string;
  status: string;
  untracked?: boolean;
  staged?: boolean;
  unstaged?: boolean;
  additions?: number;
  deletions?: number;
  binary?: boolean;
  truncated?: boolean;
  size?: number;
  reason?: string;
  capabilities?: { stage?: boolean; unstage?: boolean; discard?: boolean; hunk?: boolean };
};
export type ChangesSummary = {
  scope?: GitChangeScope;
  baseline: string;
  local_warning: boolean;
  git?: boolean;
  revision?: string;
  changes: GitChange[];
};
export type ChangePatch = {
  path: string;
  patch: string;
  revision?: string;
  truncated: boolean;
  binary: boolean;
  reason?: string;
  unavailable?: boolean;
};
export type ChangeAction = "stage" | "unstage" | "discard" | "commit";
export type ChangeActionResult = { applied: boolean; results?: Array<Record<string, string>>; error?: string };
export type ReviewSummary = { changes: ReviewChange[]; revision?: string; semantics?: Record<string, string> };

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
/** Server-supplied rule facts, displayed without deriving new authority. */
export type PermissionResource = Record<string, unknown> & {
  kind?: string; command_match?: string; argv?: string[]; programs?: string[]; cwd?: string; sandbox_key?: string;
  execution_mode?: ExecutionMode;
  dialect?: string; path?: string; path_match?: string; scheme?: string; host?: string;
  port?: number; methods?: string[]; tool_name?: string; operations?: string[];
};
export type Grants = {
  version: string;
  grants: Array<{ id: string; source: string; lifetime: string; resource_kind: string; target: string; http_methods?: string[]; operations: string[]; recursive: boolean; cwd?: string; tool?: string; version?: string; project_id?: string; resource?: PermissionResource; command_match?: string; argv?: string[]; sandbox_key?: string; dialect?: string; path?: string; path_match?: string }>;
  sandbox?: SandboxStatus | null;
  permission_mode?: string; interaction_mode?: string; execution_mode?: ExecutionMode;
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

export type FolderListing = {
  path: string;
  parent: string | null;
  home: string;
  git: boolean;
  entries: Array<{ name: string; path: string; git: boolean }>;
  truncated: boolean;
};
