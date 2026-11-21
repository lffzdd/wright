# Settings

Wright keeps non-secret runtime settings in typed JSON files. Secrets stay in
the environment or a `.env` file.

## Where settings come from

From lowest to highest precedence:

1. Built-in defaults.
2. User file: `~/.wright/settings.json` (under `WRIGHT_HOME`).
3. Project file: `{project}/.wright/settings.json`, project-safe keys only.
4. Environment variables, including those loaded from `~/.wright/.env` and the
   allowlisted keys of a project `.env`.
5. Command-line flags (`--model`, `--transport`, `--with-rag`) for that run.

A model chosen inside a session (`/model`) is stored with the session and wins
over the settings when that session is resumed; `--model` still wins over it.

Files are nested JSON objects:

```json
{
  "llm": { "model": "gpt-4.1", "models": ["gpt-4.1", "gpt-4.1-mini"] },
  "session": { "auto_title": false }
}
```

Settings are read when a session runtime is assembled, so edits apply to new
sessions.

## Keys

| Key | Type | Default | Env override | Project file |
|---|---|---|---|---|
| `llm.model` | string | empty | `OPENAI_MODEL` | yes |
| `llm.context_limit` | integer ≥ 0 | 0 (catalog, then 128000) | `OPENAI_CONTEXT_LIMIT` | yes |
| `llm.context_limits` | list of `model=tokens` | empty | `WRIGHT_CONTEXT_LIMITS` (comma-separated) | yes |
| `llm.memory_model` | string | empty (uses `llm.model`) | `OPENAI_MEMORY_MODEL` | yes |
| `llm.models` | list of strings | empty (catalog presets) | `WRIGHT_MODELS` (comma-separated) | no |
| `llm.prices` | list of `model=input/output[/cache_read]` | empty | `WRIGHT_MODEL_PRICES` (comma-separated) | no |
| `llm.transport` | `auto` \| `chat` \| `responses` \| `messages` | `auto` | `WRIGHT_LLM_TRANSPORT` | no |
| `llm.base_url` | string | empty (the model's provider) | `OPENAI_BASE_URL` | no |
| `session.auto_title` | boolean | true | `WRIGHT_AUTO_TITLE` | no |
| `skills.disabled` | list of skill ids | empty | `WRIGHT_DISABLED_SKILLS` (comma-separated) | yes |
| `memory.enabled` | boolean | true | `WRIGHT_MEMORY_ENABLED` | no |
| `memory.recall` | boolean | true | `WRIGHT_MEMORY_RECALL` | no |
| `memory.auto_extract` | boolean | true | `WRIGHT_MEMORY_AUTO_EXTRACT` | no |
| `logging.level` | `DEBUG` … `CRITICAL` | `WARNING` | `WRIGHT_LOG_LEVEL` | no |
| `knowledge.enabled` | boolean | false | `WRIGHT_KNOWLEDGE_ENABLED` | no |
| `knowledge.retriever` | `dense` \| `hybrid` | `dense` | `WRIGHT_KNOWLEDGE_RETRIEVER` | no |
| `knowledge.reranker` | boolean | false | `WRIGHT_KNOWLEDGE_RERANKER` | no |
| `knowledge.rag_dir` | path | empty | `WRIGHT_RAG_DIR` | no |
| `knowledge.index` | path | `simple_index.json` in `rag_dir` | `WRIGHT_KNOWLEDGE_INDEX` | no |
| `tools.web_search` | `auto` \| `tavily` \| `brave` \| `off` | `auto` (first provider with a key) | `WRIGHT_SEARCH_PROVIDER` | no |
| `mcp.trust_project` | boolean | false | `WRIGHT_TRUST_PROJECT_MCP` | no |
| `sandbox.default_execution` | `sandbox` \| `host` | `sandbox` | `WRIGHT_DEFAULT_EXECUTION` | no |
| `sandbox.egress` | `proxy` \| `legacy` | `proxy` | `WRIGHT_SANDBOX_EGRESS` | no |
| `hooks.config` | path | empty (no hooks) | `WRIGHT_HOOKS_CONFIG` | no |

The schema lives in `domain/model/settings.py` (`SETTING_SPECS`); this table
follows it. `skills.disabled` drops those skills from the catalog the model
sees and makes `load_skill` refuse them; it is read on every request, so the
Context tab's switches take effect on the next one. A body loaded before the
switch stays in the conversation. The `memory.*` switches are described in [memory.md](memory.md#配置); a
project file cannot set them, so a cloned repository cannot turn memory back
on. `mcp.trust_project`, `sandbox.*` and `hooks.config` are about trust and
code execution, so only the user file or the environment may set them; see
[permissions.md](permissions.md). `--trust-project-mcp`, `--execution-mode` and
`--hooks-config` still win for one run. An empty environment variable counts as unset. Booleans in the
environment accept `1/true/yes/on` and `0/false/no/off`.

## Model catalog

Wright knows common models from Anthropic, OpenAI, Google, DeepSeek, Zhipu and
Qwen (`domain/model/llm/catalog.py`, with the date it was checked and the
pages the values came from). For a model in the catalog, setting `llm.model`
and its provider's API key is enough; the endpoint, context window and model
picker follow from it. Model names match the way prices do: a gateway prefix
such as `openai/` and a dated snapshot suffix still find the family.

**Endpoint and key.** With `llm.base_url` set, every model goes to that
endpoint with `OPENAI_API_KEY`, as before the catalog. Without it, a catalog
model goes to its provider's documented OpenAI-compatible endpoint with that
provider's own key:

| Provider | Endpoint | Key |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `OPENAI_API_KEY` |
| Anthropic | `https://api.anthropic.com/v1/` | `ANTHROPIC_API_KEY` |
| Google | `https://generativelanguage.googleapis.com/v1beta/openai/` | `GEMINI_API_KEY` |
| DeepSeek | `https://api.deepseek.com` | `DEEPSEEK_API_KEY` |
| Zhipu | `https://api.z.ai/api/paas/v4/` (international) | `ZAI_API_KEY` |
| Qwen | depends on the account: set `llm.base_url` | `OPENAI_API_KEY` |

A missing key stops with an error that names it; Wright never sends one
provider's key to another. A model outside the catalog needs `llm.base_url`.
Zhipu's China endpoint (`open.bigmodel.cn`) and Qwen's workspace endpoints
are set through `llm.base_url`.

**Switching models.** Choosing a model of another provider in a session moves
the session to that provider's endpoint and transport. A conversation whose
history needs the Responses API (OpenAI) cannot continue on a Chat endpoint;
Wright refuses that switch and keeps the current model.

**Transport.** `auto` picks `responses` for `api.openai.com`, `messages` (the
native Anthropic Messages API) for `api.anthropic.com`, and `chat` for every
other endpoint. A session keeps the transport it started with, so sessions
created before `messages` existed stay on `chat`. Anthropic and Chat histories
can continue on each other's endpoint.

**Context window.** The first of these that applies:

1. `llm.context_limits`, entries `model=tokens` for single models;
2. `llm.context_limit`, when not 0, for every model;
3. the catalog's value for the model;
4. 128000.

Where a provider publishes an input limit below the context window, the
catalog uses the input limit. A malformed `llm.context_limits` entry is
skipped with a warning.

**Model picker.** `llm.models` replaces the picker's list. When it is empty
the picker offers a few current catalog models of each provider you can
reach: those with a key set, or with `llm.base_url` set, the provider that
serves that host. A gateway or other unknown host offers no presets.

## Model prices

The web console shows an estimated cost for each session. Wright ships list
prices for common models from Anthropic, OpenAI, Google, DeepSeek, Zhipu and
Qwen (`domain/model/llm/pricing.py`, with the date they were checked and the
pages they came from). A gateway prefix such as `openai/` and a dated snapshot
suffix such as `-2025-08-07` still find the family's price.

`llm.prices` adds a model the list lacks or replaces a list price. Each entry
is `model=input/output[/cache_read]` in USD per million tokens; without a
cache price, cached tokens are billed as input:

```json
{"llm": {"prices": ["kimi-k3=3/15/0.3", "my-gateway-model=0.5/2"]}}
```

The estimate leaves out cache writes, batch and priority tiers, and most
long-context surcharges. While any request in a session used a model with no
known price, the session cost shows as unknown rather than a partial sum. A
malformed entry is skipped with a warning. Prices are user-only: a cloned
repository should not decide what a session appears to cost.

## What a project file may set

A project file can arrive with a cloned repository, so it may only choose the
model: `llm.model`, `llm.context_limit`, `llm.context_limits` and
`llm.memory_model`, the same non-secret keys a project `.env` may set. Endpoints, paths Wright
imports code from, logging and every other key are user-only. Wright ignores
them in a project file and logs a warning naming the file and key.

## Validation

- Unknown keys and invalid values are ignored with a warning; the lower layer
  stands.
- Keys that look like credentials (`api_key`, `token`, `secret`, `password`)
  are rejected in any file. Put secrets in the environment or `.env`.
- An unreadable user file, or an invalid `llm.base_url`, `knowledge.rag_dir`,
  `knowledge.index` or `hooks.config` in the user file, stops startup instead
  of falling back. A fallback could send the API key to the default endpoint,
  load code from somewhere else, or drop the hooks that deny tool calls. An
  unreadable project file is only a warning.
- `sandbox.default_execution` and `sandbox.egress` fall back to their default
  on an invalid value, because the default (`sandbox`, `proxy`) is the
  stricter choice.

## Not in settings

These stay environment-only:

- `WRIGHT_HOME`, `WRIGHT_MEMORY_DIR` and `WRIGHT_PERMISSION_CONFIG`, which
  locate state, including the settings file itself, and
  `WRIGHT_PERMISSION_INTERACTIVE`, a process switch for tests and scripts. The
  web console shows their values under Advanced.
- Interface preferences (language, theme, how thinking is shown) in `~/.wright/preferences.json`.

API keys are not settings either. They live in `~/.wright/.env` or the
environment and are never written to a settings file. The web console can
set and remove the keys it knows (see below); `LLM_API_KEY` and the RAG
embedder's other variables are still edited by hand.

## API keys

The keys Wright knows are `OPENAI_API_KEY` and the provider keys of the model
catalog (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`,
`ZAI_API_KEY`, `DASHSCOPE_API_KEY`), `TAVILY_API_KEY`, `BRAVE_API_KEY` and
`SILICONFLOW_API_KEY`. For each the console reports only where it comes from:

- `file`: set in `~/.wright/.env`;
- `env`: set outside that file (the shell or a project `.env`), which wins at
  startup, so the console leaves it alone;
- `missing`.

Writing a key replaces its line in `~/.wright/.env`, keeps every other line,
writes the file atomically with mode 0600, and updates the running process,
so the next session uses it without a restart. Values are one line of
printable text without quotes or backslashes. No response ever contains a
value.

## Editing settings

**Web console:** Settings has eight sections: General, Models, Context & cost,
Permissions, Tools, Memory, Knowledge search and Advanced, and a search across
all of them.

- Each row edits the user file. For a project-safe key, the row's menu offers
  "Set for this project only", which switches that row to the project file;
  "Use the value for all projects" removes the project value. A row says
  which file is in effect when it edits the other one.
- A key set by an environment variable is locked, with a lock icon on the
  row and one summary at the top of the page listing every such key.
- Models first asks how models are reached: provider keys (each catalog
  provider with its key and whether it is ready) or one custom endpoint (its
  URL, `OPENAI_API_KEY` and the API). Leaving the endpoint removes
  `llm.base_url` after asking. When the effective `llm.model` or
  `llm.memory_model` cannot be routed, the row shows the error a new session
  would stop with. `llm.prices` and `llm.context_limits` are edited as tables.
- Permissions edits the default permission mode, where commands run,
  sandbox networking and project MCP trust, and lists the stored rules; see
  [permissions.md](permissions.md#在设置页修改默认值). Loosening values ask first.
- Tools edits the search provider and its keys, the MCP servers in
  `~/.wright/mcp.json` and the project's `.wright/mcp.json`, the skill
  switches (`skills.disabled`) and the hooks file, whose hooks are listed
  read-only.
- API key fields are write-only: set, replace or remove.

**CLI and TUI:**

```
/settings                                 list keys, values and sources
/settings set [--project] KEY VALUE       write to the user (or project) file
/settings unset [--project] KEY           remove the key from that file
```

Values are parsed like environment variables: `true/false`, numbers, and
comma-separated lists.

**HTTP API:** `GET /api/v1/workspaces/{id}/settings` describes every key for
that project: effective value, source layer (`default`, `user`, `project`,
`env`), the raw value in each file, whether an environment variable
overrides it, and which files may hold it. Its `models` field lists the
catalog providers with whether each key is set and reachable, the custom
endpoint, every catalog model with its context window and price, and
`problems`: the routing error for `llm.model` or `llm.memory_model`, if any. `PUT` with `{"scope": "user" |
"project", "key": "llm.model", "value": ...}` writes one key, and
`"value": null` removes it. `/api/v1/settings` does the same with no project
(user file only). Writes follow the same rules as loading: a project-scope
write of a user-only key, a secret or an invalid value is refused with 400.
The same response lists the environment-only variables under `environment`.

The Settings page also uses these routes, each under `/api/v1` (user layer
only) and `/api/v1/workspaces/{id}` (with that project):

| Route | Does |
|---|---|
| `GET /settings/permissions` | default mode, the user and project rules, built-in rules marked `locked` |
| `PUT /settings/permissions/mode` | `{"mode": ...}` sets the default mode in the user permission file |
| `POST /settings/permissions/rules` | `{"effect": "deny" \| "ask", "scope", "text"}` adds a rule |
| `DELETE /settings/permissions/rules/{scope}/{id}` | removes a rule; built-in rules are refused |
| `GET /settings/skills`, `PUT /settings/skills/{id}` | skill switches without a session |
| `GET /settings/mcp`, `PUT /settings/mcp/{name}`, `DELETE /settings/mcp/{scope}/{name}` | MCP servers; `env` and `headers` values are write-only, and a `null` value keeps the stored one |

User-only routes: `GET /api/v1/settings/secrets`, `PUT` or `DELETE
/api/v1/settings/secrets/{name}` (`{"value": ...}`), and `GET
/api/v1/settings/hooks`, which parses the file `hooks.config` names.

## Adding a key

1. Add a `SettingSpec` to `SETTING_SPECS` and a field to the matching section
   dataclass in `domain/model/settings.py`; an import-time check keeps the two
   in step.
2. Keep it user-only unless it can only choose among harmless options. Mark
   endpoints and code-loading paths `fail_closed`.
3. Read it from `WrightSettings` where the runtime is assembled and pass the
   value in. Don't read the environment variable directly.
4. Update the table above and `.env.example`.

## Code layout

| Layer | Module | Owns |
|---|---|---|
| domain | `model/settings.py` | schema, defaults, value coercion |
| domain | `policy/settings.py` | layer merge, project allowlist, secret rejection |
| domain | `gateway/settings.py` | `SettingsStore` protocol |
| infrastructure | `config/settings_store.py` | JSON files, locked atomic writes |
| application | `settings/service.py` | resolve, describe, validated update |
| application | `settings/models.py` | provider key status and catalog models for the editor |
| application | `composition/settings.py` | binds the stores; `load_settings(project_root)`, `sandbox_egress()` |
| domain | `gateway/secrets.py`, `gateway/mcp_config.py` | `SecretStore`, `McpConfigStore` protocols |
| infrastructure | `config/secret_store.py`, `config/mcp_config_store.py` | `~/.wright/.env` and `mcp.json` edits |
| application | `settings/secrets.py` | which keys may be written, and where each one comes from |
| application | `permission/defaults.py` | default mode and stored rules, without a session |
| application | `workspace/mcp_config.py` | MCP servers as configured, values hidden |
| interfaces | `web/settings_routes.py` | the HTTP routes above |
