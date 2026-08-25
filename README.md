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

The agent’s workspace is `src/wright/workspace`. Resume a session with
`wright --continue` or `wright --resume`.

## Tests

```bash
uv run pytest -q
uv run python -m wright.test
uv run python -m wright.test_subagent
```

Knowledge search is off unless `WRIGHT_KNOWLEDGE_ENABLED=1`. It is an optional
adapter: set `WRIGHT_RAG_DIR` / `WRIGHT_KNOWLEDGE_INDEX` if you have an external
RAG package. Wright does not vendor that stack.

## Layout

| Path | Role |
|------|------|
| `src/wright/` | Agent runtime, tools, permissions, memory, skills |
| `src/wright/tests/` | pytest suite |
| `docs/` | Design notes for the control plane, autonomy, skills, hooks |

## License

MIT
