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

Copy `.env.example` to `.env` and set an OpenAI-compatible endpoint and model
that support native function calling:

```bash
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4.1
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

```bash
cd /path/to/your/project
uv run --directory /path/to/wright wright
# or, after `uv tool install`:
wright
wright --workspace /path/to/your/project
```

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
| `~/.wright/memory/` | Long-term memory |
| `~/.wright/mcp.json` | User MCP servers |
| `{project}/.wright/mcp.json` | Project MCP servers (override user on the same name) |
| `{project}/.wright/skills/` | Project skills (override user skills) |
| `~/.wright/skills/` | User skills |
| `src/wright/` | Agent runtime |
| `src/wright/tests/` | pytest suite |
| `examples/` | Sample skills and an `mcp.json` template |
| `docs/` | Design notes |

Copy `examples/skills/` into `~/.wright/skills` or a project’s `.wright/skills`.
Copy `examples/mcp.json` to `~/.wright/mcp.json` or `{project}/.wright/mcp.json`
when you actually want MCP servers — Wright does not seed a dummy config.

## License

MIT
