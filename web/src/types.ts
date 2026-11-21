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
  // Interface-only payload from ToolResult.display (e.g. the plan checklist).
  display?: unknown;
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
  /** A user message sent while its turn was already running. */
  interjection?: boolean;
  attachments?: Attachment[];
  output?: string;
  kind: "text" | "reasoning" | "tool" | "shell" | "edit" | "approval" | "status";
  status?: string;
  /** Set on a resolved approval record in the transcript. */
  decision?: "allow" | "always" | "deny" | "abort";
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
  /**
   * Turn totals on the turn's closing status item. Not sent by the server yet;
   * the turn footer shows each one once it arrives.
   */
  turn_elapsed_ms?: number | null;
  turn_usage?: { input_tokens?: number | null; output_tokens?: number | null; cache_read_tokens?: number | null; cost_usd?: number | null } | null;
  /** Follow-up prompts the agent suggests after a turn; not sent by the server yet. */
  suggestions?: string[];
};

/** One thing inside a context category, e.g. a single tool result or one MCP server's tools. */
export type ContextItem = {
  label: string;
  tokens: number;
  /** Fixed label to translate instead of `label`. */
  key?: string;
  detail?: string;
  count?: number;
  folded?: boolean;
  loaded?: boolean;
  call_id?: string;
  mcp?: string;
  /** Natural-language text (a message or a paragraph), shown in the UI font. */
  prose?: boolean;
  /** Stable across the snapshot and the detail view. */
  id?: string;
  /** Turn the item belongs to; absent for what every request carries. */
  turn?: number;
  /** Detail view only: position in the request, the full content, its category. */
  seq?: number;
  content?: string;
  truncated?: boolean;
  category?: string;
};

/** Tokens one turn adds, split by kind. */
export type ContextTurnParts = { text: number; reasoning: number; tool_calls: number; tool_results: number };

/** One user turn: everything from a person's message to the next one. */
export type ContextTurn = { index: number; label: string; record_id?: string; tokens: number; parts: ContextTurnParts };

export type ContextDetailQuery = { category: string } | { turn: number };

/** Every item of one category or turn, fetched on demand. */
export type ContextDetail = { items: ContextItem[]; tokens: number; category?: ContextCategory; turn?: ContextTurn };

/** The next request whole and in order, as it would be assembled now. */
export type ContextRequest = {
  items: ContextItem[];
  tokens: number;
  used?: number;
  /** The ring's total: the provider's last count plus estimates for what was added since. */
  calibrated_used?: number | null;
  limit?: number | null;
  turns?: ContextTurn[];
  /** Only when asked for: the messages and tools as they go on the wire. */
  request?: { messages: unknown[]; tools: unknown[] };
};

/** A skill on disk and whether the model is offered it. */
export type SkillView = { id: string; name: string; description: string; enabled: boolean };

/** One tool's share of a tool category: how many calls and their tokens. */
export type ContextToolSum = { label: string; count: number; tokens: number };

export type ContextGroup = "fixed" | "conversation" | "reserve";

export type ContextCategory = {
  id: string;
  tokens: number;
  share: number;
  /** Older snapshots have no group; the panel infers it from the id. */
  group?: ContextGroup;
  count?: number;
  /** The largest items, biggest first. */
  items?: ContextItem[];
  /** Tool categories only: items summed per tool, biggest first. */
  by_tool?: ContextToolSum[];
};

/** One request: the local estimate and, once it returned, the provider's prompt count. */
export type ContextPoint = { estimated: number; actual?: number };

export type ContextBreakdown = {
  kind?: string;
  exact?: boolean;
  total?: number;
  /** Tokens the request itself takes; `total` also counts the reply reserve. */
  used?: number;
  limit?: number | null;
  /** Older tool results folded for this request and the tokens that saved. */
  folded?: { count: number; tokens: number };
  system_prompt_contains_memory?: boolean;
  categories?: ContextCategory[];
  note?: string;
  /** Estimated before any request was assembled. */
  preview?: boolean;
  /** Request size (same scale as `used`) past which older tool results fold. */
  fold_at?: number;
  /** Recent requests, oldest first. */
  history?: ContextPoint[];
  /** `used` anchored on the provider's count for the last request. */
  calibrated_used?: number;
  /** The request the total is anchored on. */
  anchor?: { actual: number; estimated: number };
  /** Conversation turns, oldest first; the oldest may be left out. */
  turns?: ContextTurn[];
  turns_omitted?: number;
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
  rules: Array<{ effect: "allow" | "ask" | "deny"; scope: "session" | "project" | "user"; rule: Record<string, unknown>; target: string; resource_kind: string; operations: string[]; tool: string; description: string; conditional: boolean; locked?: boolean }>;
  constraints: string[]; precedence: string[];
};
export type SandboxStatus = { platform: string; provider: string; available: boolean; state: "ready" | "setup_required" | "initializing" | "failed"; detail?: string };
/** Server-supplied rule facts, displayed without deriving new authority. */
export type PermissionResource = Record<string, unknown> & {
  kind?: string; command_match?: string; argv?: string[]; programs?: string[]; cwd?: string; sandbox_key?: string;
  execution_mode?: ExecutionMode;
  dialect?: string; path?: string; path_match?: string; scheme?: string; host?: string;
  port?: number; methods?: string[]; tool_name?: string; operations?: string[];
  host_match?: string; allow_private?: boolean;
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
  accessed_files?: Array<{ path: string; access?: string; tool?: string; call_id?: string; count?: number; tools?: Record<string, number>; last_calls?: Record<string, string> }>;
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
    /** Session totals not sent by the server yet; the context tab shows "—" until they arrive. */
    cache_read_tokens?: number | null;
    cost_usd?: number | null;
  };
  /** Configured MCP servers; not sent by the server yet. */
  mcp_servers?: McpServerView[];
};

/**
 * One configured MCP server. `tokens` is what its tool definitions take in the
 * context; `toggleable` is set only when the server accepts an on/off change.
 */
export type McpServerView = {
  name: string;
  status: "connected" | "failed" | "auth_required" | "disabled";
  tool_count: number;
  tokens?: number | null;
  error?: string | null;
  toggleable?: boolean;
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

export type SettingScope = "user" | "project";

/** One typed runtime setting, as GET /settings describes it. */
export type RuntimeSetting = {
  key: string;
  kind: "str" | "int" | "bool" | "choice" | "path" | "list";
  choices: string[];
  minimum: number | null;
  description: string;
  default: unknown;
  value: unknown;
  source: "default" | SettingScope | "env";
  user_value: unknown;
  project_value: unknown;
  env: string[];
  env_override: boolean;
  scopes: SettingScope[];
};

export type ProviderStatus = {
  id: string;
  name: string;
  base_url: string | null;
  key_env: string;
  key_set: boolean;
  reachable: boolean;
};

export type ModelPriceInfo = { input: number; output: number; cache_read: number | null };

export type CatalogModelInfo = {
  name: string;
  provider: string;
  context_window: number | null;
  preset: boolean;
  price: ModelPriceInfo | null;
};

export type ModelOverview = {
  /** Why a new session would not start with the effective llm.model or llm.memory_model. */
  problems?: Record<string, string>;
  endpoint: { base_url: string; key_env: string; key_set: boolean; provider: string };
  providers: ProviderStatus[];
  models: CatalogModelInfo[];
};

export type RuntimeSettingsView = {
  paths: { user: string; project: string | null };
  settings: RuntimeSetting[];
  warnings: string[];
  /** Absent from servers older than the provider overview. */
  models?: ModelOverview;
  /** Process switches that stay environment-only, shown read-only. */
  environment?: Array<{ name: string; value: string | null; description: string }>;
};

/** Where an API key comes from; its value is never sent to the page. */
export type SecretSource = "file" | "env" | "missing";
export type SecretsView = {
  path: string;
  secrets: Array<{ name: string; group: "models" | "search" | "knowledge"; source: SecretSource }>;
};

export type PermissionRuleRow = {
  id: string;
  effect: "allow" | "ask" | "deny";
  scope: "session" | SettingScope;
  text: string;
  description: string;
  locked: boolean;
};
export type PermissionDefaultsView = {
  mode: string;
  modes: string[];
  path: string;
  project: boolean;
  rules: PermissionRuleRow[];
};

export type SkillSwitchesView = {
  skills: Array<{ id: string; name: string; description: string; enabled: boolean }>;
  errors: string[];
  /** WRIGHT_DISABLED_SKILLS decides; the switches cannot change it. */
  locked: boolean;
};

export type McpTransport = "stdio" | "http" | "sse";
export type McpServerRow = {
  name: string;
  scope: SettingScope;
  transport: McpTransport | "unknown";
  command: string;
  args: string[];
  url: string;
  /** Names only; values stay on disk. */
  env: string[];
  headers: string[];
  problem: string | null;
  shadowed: boolean;
  active: boolean;
};
export type McpConfigView = {
  project_trusted: boolean;
  files: Partial<Record<SettingScope, { path: string; error: string | null }>>;
  servers: McpServerRow[];
};
/** A null value keeps the stored one. */
export type McpServerForm = {
  scope: SettingScope;
  transport: McpTransport;
  command?: string;
  args?: string[];
  url?: string;
  env?: Record<string, string | null>;
  headers?: Record<string, string | null>;
  previous?: string;
};

export type HooksView = {
  path: string;
  error: string | null;
  hooks: Array<{ event: string; name: string; matcher: string; command: string[]; timeout: number }>;
};

/** Where the conversation should scroll: an exact row, else the turn's prompt. */
export type MessageTarget = { contextId?: string; tocId?: string };
