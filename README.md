# Wright

A coding agent for the terminal. Point it at a workspace, give it a task, and it
uses files, shell, web, MCP tools, skills, and sub-agents to get the work done.

Wright uses native function calling through the OpenAI-compatible Chat Completions
API. The model returns tool calls or a normal text answer; Wright runs the tools
and returns each result with its original call ID. Permissions, concurrent tools,
checkpoints, memory, and autonomy remain in the runtime.

The original custom JSON ReAct implementation is preserved at the
`legacy-json-react` Git tag. See [the migration and study guide](docs/native-tool-calling.md)
for the reading order, execution records, and how to open the old version.

## Install

```bash
git clone git@github.com:lffzdd/wright.git
cd wright
uv sync --group dev
```

Install the optional local Web console dependencies when needed:

```bash
uv sync --group dev --extra web
```

Copy `.env.example` to `.env` and set an OpenAI-compatible endpoint and model
that support native function calling:

```bash
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4.1
# Optional comma-separated choices offered by TUI /model.
WRIGHT_MODELS=gpt-4.1,gpt-4.1-mini
```

## Run

From the repo root:

```bash
uv run wright
```

or:

```bash
uv run python -m wright.main
```

The agent edits the current directory (or `--workspace DIR`). Sessions, traces,
and the task database go to `~/.wright/projects/<id>/`, not into your repo.
Resume with `wright --continue` or `wright --resume`. Native sessions use checkpoint
version 2; version 1 sessions must be opened with `legacy-json-react`. Start a new
session after upgrading; existing checkpoint files are not converted or deleted.

The TUI also offers `/new`, `/resume`, and `/model`. `/new` and `/resume` rebuild
the session runtime cleanly; `/model` switches the active OpenAI-compatible model
for subsequent requests. Configure optional model choices with `WRIGHT_MODELS`.

```bash
cd /path/to/your/project
uv run --directory /path/to/wright wright
# or, after `uv tool install`:
wright
wright --workspace /path/to/your/project
```

### Local Web console

The TUI remains the default. Start the optional, localhost-only Web console
explicitly:

```bash
wright --ui web --workspace /path/to/your/project
wright --ui web --web-port 8765 --web-capacity 4 --no-open
```

Wright binds only to `127.0.0.1`. At startup it creates a one-time bootstrap
token in the URL fragment, exchanges it for an HttpOnly SameSite cookie, and
removes the fragment in the browser.

For Git projects, new Web sessions use independent worktrees by default under
`~/.wright/worktrees/<project-id>/<session-id>` on branches named
`wright/<session-id>`. They start from the current checkout's `HEAD`; uncommitted
checkout changes are not copied. Choose **Current checkout** explicitly to use
those existing changes. Only one local-checkout session can be active at once.
Non-Git projects use that local mode automatically.

Closing a session saves its checkpoint and stops its runtime while retaining
the execution directory. Archiving removes a worktree only when it is clean and
has no commits after its recorded base; otherwise Wright retains the branch and
path for manual review. The Changes inspector is read-only and Git-only.

### Durable automations and recovery

`wright --ui headless --workspace <project>` explicitly starts the local
ApplicationHost for persisted automations; it does not install a service or
leave a background process behind. Closing a chat session does not cancel an
already accepted durable run, while stopping the host stops new scheduling and
waits for workers before closing MCP connections and SQLite.

Interactive command IDs are stored with their payload and state. Accepted but
not started input is re-queued after a restart; a command interrupted while it
was running is recorded as `unknown` and is not automatically replayed. This
prevents duplicate side effects but is not an exactly-once guarantee for shell,
MCP, or other external work.

Pending permission and ask-user requests are also recorded with their Run
scope. The current protocol deliberately fails them closed on process restart:
the old in-memory waiter no longer exists, so a late reply cannot restart a
tool call. A future resumable interaction transport must create a new explicit
request rather than reuse a historic approval.

Delivered MCP images are copied to managed project artifacts instead of being
kept as base64 in history/checkpoints. The authenticated Web endpoint is
`/api/v1/sessions/<session>/artifacts/<artifact>`; it serves only references
recorded by that session's tool history. PNG/JPEG/WebP content is decoded and
validated against the declared MIME type using the same limits as uploaded
attachments. Invalid images are reported explicitly and are not registered.
Chat and Responses requests resolve image references only at the provider
boundary, after the corresponding batch of tool results. Missing or damaged
artifacts produce an explicit unavailable-image note; base64 never enters the
checkpoint. Context estimates reserve an image budget for these references.

Web sessions sharing an execution directory reuse one ApplicationHost while
retaining separate automation records. Their schedulers share a single dispatch
slot. A tool whose result cannot be committed makes its durable Run `unknown`;
that Run is never automatically retried, even with a retry policy. Whole-run
retries are limited to failures before any tool executed.

## Tests

```bash
uv run pytest -q
uv run python scripts/smoke_agent.py
uv run python scripts/smoke_subagent.py
```

Knowledge search is off unless `WRIGHT_KNOWLEDGE_ENABLED=1`. It is an optional
adapter: set `WRIGHT_RAG_DIR` / `WRIGHT_KNOWLEDGE_INDEX` if you have an external
RAG package. Wright does not vendor that stack.

## Layout

| Path | Role |
|------|------|
| cwd / `--workspace` | Project files the agent may edit |
| `~/.wright/projects/<id>/` | Sessions, traces, autonomy task DB |
| `~/.wright/worktrees/<id>/<session-id>/` | Isolated Web-session worktrees |
| `~/.wright/memory/` | Long-term memory |
| `~/.wright/mcp.json` | User MCP servers |
| `{project}/.wright/mcp.json` | Project MCP servers (override user on the same name) |
| `{project}/.wright/skills/` | Project skills (override user skills) |
| `~/.wright/skills/` | User skills |
| `src/wright/` | Agent runtime |
| `web/` | React + TypeScript Web console source |
| `src/wright/web/static/` | Prebuilt Web assets included in the Python package |
| `src/wright/tests/` | pytest suite |
| `examples/` | Sample skills and an `mcp.json` template |
| `docs/` | Design notes |

Copy `examples/skills/` into `~/.wright/skills` or a project’s `.wright/skills`.
Copy `examples/mcp.json` to `~/.wright/mcp.json` or `{project}/.wright/mcp.json`
when you actually want MCP servers — Wright does not seed a dummy config.

## License

MIT
