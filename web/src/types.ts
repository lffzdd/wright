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
  kind: "text" | "tool" | "shell" | "edit" | "approval";
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

export type Snapshot = {
  stream_id: string;
  last_seq: number;
  session: SessionSummary;
  history: HistoryTurn[];
  active_turn: ActiveTurn | null;
  agents?: AgentView[];
  plan: { objective?: string; status?: string; steps?: Array<{ id: string; title: string; status: string; note?: string }> };
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
  seen: string[];
  connection: "connecting" | "connected" | "reconnecting" | "disconnected" | "closed";
  resync?: boolean;
};
