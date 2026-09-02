# Wright

A coding agent for the terminal. Point it at a workspace, give it a task, and it
uses files, shell, web, MCP tools, skills, and sub-agents to get the work done.

Wright is the extracted, installable form of a learning implementation that grew
inside a larger LLM monorepo. The loop is still ReAct-shaped today (the model
returns a JSON turn with either tool calls or a final answer). Native tool
calling is the next protocol change; the runtime around it — permissions,
concurrent tools, checkpoints, memory, autonomy — stays.

## Install

```bash
git clone git@github.com:lffzdd/wright.git
cd wright
uv sync --group dev
```

Copy `.env.example` to `.env` and set an OpenAI-compatible endpoint:

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
Resume with `wright --continue` or `wright --resume`.

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
