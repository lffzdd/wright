export type SessionSummary = {
  session_id: string;
  status: string;
  agent_status?: string;
  user_goal?: string;
  model?: string;
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
  prompt: string;
  attachments: Attachment[];
  reasoning: string;
  content: string;
  tools: ToolState[];
};

export type Interaction = {
  request_id: string;
  kind: "permission" | "ask_user";
  question?: string;
  context?: string;
  options?: string[];
  tool_name?: string;
  subject?: string;
  reason?: string;
  risk_flags?: string;
  offer_always?: boolean;
  remember_rule?: string;
  remember_persists?: boolean;
  revoke_hint?: string;
};

export type QueuedCommand = { command_id: string; prompt: string; attachments?: Attachment[] };

export type Notice = {
  id: string;
  type: string;
  text: string;
  kind?: string;
};

export type Snapshot = {
  stream_id: string;
  last_seq: number;
  session: SessionSummary;
  history: Array<{
    user: string;
    assistant: string;
    attachments?: Attachment[];
    tools?: ToolState[];
  }>;
  active_turn: ActiveTurn | null;
  plan: { objective?: string; status?: string; steps?: Array<{ id: string; title: string; status: string; note?: string }> };
  pending_interactions: Interaction[];
  notices: Notice[];
  queued_commands: QueuedCommand[];
  queue_depth: number;
  usage: {
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    request_prompt_tokens: number;
    request_completion_tokens: number;
    request_total_tokens: number;
    context_tokens: number;
    context_limit: number;
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

export type ViewState = Snapshot & { seen: string[]; connection: "connecting" | "connected" | "disconnected" };
