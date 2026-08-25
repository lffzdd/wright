"""Immutable episodic memory: one compact execution record per user turn."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Literal

from .paths import memory_dir


EpisodeStatus = Literal["completed", "failed", "max_steps"]
EPISODES_DIRECTORY = "episodes"
MAX_EPISODES = 500
MAX_EPISODE_GOAL_CHARS = 2_000
MAX_EPISODE_OUTCOME_CHARS = 4_000
MAX_EPISODE_TOOLS = 100
MAX_EPISODE_VERIFICATIONS = 100
MAX_EPISODE_AGENTS = 64
MAX_EPISODE_FILE_BYTES = 256_000
_EPISODE_ID_RE = re.compile(r"ep-[A-Za-z0-9_-]{1,180}")
_locks_guard = threading.Lock()
_store_locks: dict[Path, threading.RLock] = {}


class EpisodeStoreError(ValueError):
    pass


class EpisodeNotFoundError(EpisodeStoreError):
    pass


@dataclass(frozen=True)
class EpisodeRecord:
    id: str
    session_id: str
    goal: str
    status: EpisodeStatus
    outcome: str
    started_step: int
    ended_step: int
    created_at: str
    plan: dict[str, Any]
    tools: tuple[dict[str, Any], ...]
    agents: tuple[dict[str, Any], ...]
    verification: tuple[dict[str, Any], ...]
    usage: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "goal": self.goal,
            "status": self.status,
            "outcome": self.outcome,
            "started_step": self.started_step,
            "ended_step": self.ended_step,
            "created_at": self.created_at,
            "plan": self.plan,
            "tools": list(self.tools),
            "agents": list(self.agents),
            "verification": list(self.verification),
            "usage": self.usage,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "EpisodeRecord":
        if not isinstance(value, dict):
            raise EpisodeStoreError("episode 必须是对象")
        episode_id = value.get("id")
        if not isinstance(episode_id, str) or _EPISODE_ID_RE.fullmatch(episode_id) is None:
            raise EpisodeStoreError("episode id 非法")
        status = value.get("status")
        if status not in {"completed", "failed", "max_steps"}:
            raise EpisodeStoreError("episode status 非法")
        tools = value.get("tools", [])
        agents = value.get("agents", [])
        verification = value.get("verification", [])
        plan = value.get("plan", {})
        usage = value.get("usage", {})
        if not isinstance(tools, list) or not all(isinstance(item, dict) for item in tools):
            raise EpisodeStoreError("episode tools 非法")
        if len(tools) > MAX_EPISODE_TOOLS:
            raise EpisodeStoreError("episode tools 超出上限")
        if not isinstance(agents, list) or not all(
            isinstance(item, dict) for item in agents
        ):
            raise EpisodeStoreError("episode agents 非法")
        if len(agents) > MAX_EPISODE_AGENTS:
            raise EpisodeStoreError("episode agents 超出上限")
        if not isinstance(verification, list) or not all(
            isinstance(item, dict) for item in verification
        ):
            raise EpisodeStoreError("episode verification 非法")
        if len(verification) > MAX_EPISODE_VERIFICATIONS:
            raise EpisodeStoreError("episode verification 超出上限")
        if not isinstance(plan, dict) or not isinstance(usage, dict):
            raise EpisodeStoreError("episode plan/usage 非法")
        started_step = _nonnegative_int(value.get("started_step"), "started_step")
        ended_step = _nonnegative_int(value.get("ended_step"), "ended_step")
        if ended_step < started_step:
            raise EpisodeStoreError("ended_step 不能小于 started_step")
        return cls(
            id=episode_id,
            session_id=_bounded_string(value.get("session_id"), "session_id", 128),
            goal=_bounded_string(
                value.get("goal"), "goal", MAX_EPISODE_GOAL_CHARS, allow_empty=True
            ),
            status=status,
            outcome=_bounded_string(
                value.get("outcome"),
                "outcome",
                MAX_EPISODE_OUTCOME_CHARS,
                allow_empty=True,
            ),
            started_step=started_step,
            ended_step=ended_step,
            created_at=_bounded_string(value.get("created_at"), "created_at", 100),
            plan=plan,
            tools=tuple(tools),
            agents=tuple(agents),
            verification=tuple(verification),
            usage={
                "prompt_tokens": _nonnegative_int(
                    usage.get("prompt_tokens", 0), "usage.prompt_tokens"
                ),
                "completion_tokens": _nonnegative_int(
                    usage.get("completion_tokens", 0), "usage.completion_tokens"
                ),
                "total_tokens": _nonnegative_int(
                    usage.get("total_tokens", 0), "usage.total_tokens"
                ),
            },
        )


class EpisodeStore:
    def __init__(self, memory_directory: Path | None = None) -> None:
        self.root = (memory_directory or memory_dir()).expanduser().resolve()
        self.directory = self.root / EPISODES_DIRECTORY

    def save(self, episode: EpisodeRecord) -> EpisodeRecord:
        path = self.path_for(episode.id)
        with _lock_for(self.directory):
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            if path.exists():
                return self.get(episode.id)
            _atomic_write(path, json.dumps(
                episode.to_dict(), ensure_ascii=False, indent=2
            ) + "\n")
            self._prune_unlocked()
        return episode

    def get(self, episode_id: str) -> EpisodeRecord:
        path = self.path_for(episode_id)
        try:
            if path.is_symlink():
                raise EpisodeStoreError("拒绝读取符号链接 episode")
            if path.stat().st_size > MAX_EPISODE_FILE_BYTES:
                raise EpisodeStoreError("episode 文件超出大小上限")
            return EpisodeRecord.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except FileNotFoundError as exc:
            raise EpisodeNotFoundError(f"episode 不存在: {episode_id}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise EpisodeStoreError(f"episode 无法读取: {exc}") from exc

    def delete(self, episode_id: str) -> EpisodeRecord:
        with _lock_for(self.directory):
            episode = self.get(episode_id)
            try:
                self.path_for(episode_id).unlink()
            except OSError as exc:
                raise EpisodeStoreError(f"episode 删除失败: {exc}") from exc
        return episode

    def list(self, limit: int = 100) -> list[EpisodeRecord]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_EPISODES:
            raise EpisodeStoreError(f"limit 必须是 1..{MAX_EPISODES} 的整数")
        if not self.directory.is_dir():
            return []
        paths = sorted(
            (
                path
                for path in self.directory.glob("ep-*.json")
                if path.is_file() and not path.is_symlink()
            ),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        episodes: list[EpisodeRecord] = []
        for path in paths:
            try:
                episodes.append(self.get(path.stem))
            except EpisodeStoreError:
                continue
            if len(episodes) >= limit:
                break
        return episodes

    def search(
        self,
        query: str = "",
        *,
        status: EpisodeStatus | None = None,
        limit: int = 20,
    ) -> list[EpisodeRecord]:
        if status is not None and status not in {"completed", "failed", "max_steps"}:
            raise EpisodeStoreError("episode status 非法")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise EpisodeStoreError("limit 必须是 1..100 的整数")
        query_text = str(query).strip().casefold()
        terms = [term for term in re.split(r"\s+", query_text) if term]
        scored: list[tuple[int, str, EpisodeRecord]] = []
        for episode in self.list(MAX_EPISODES):
            if status is not None and episode.status != status:
                continue
            haystack = "\n".join([
                episode.goal,
                episode.outcome,
                episode.status,
                " ".join(str(tool.get("name", "")) for tool in episode.tools),
            ]).casefold()
            if terms and not all(term in haystack for term in terms):
                continue
            score = sum(haystack.count(term) for term in terms) if terms else 0
            scored.append((score, episode.created_at, episode))
        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return [episode for _, _, episode in scored[:limit]]

    def path_for(self, episode_id: str) -> Path:
        if not isinstance(episode_id, str) or _EPISODE_ID_RE.fullmatch(episode_id) is None:
            raise EpisodeStoreError("episode_id 非法")
        return self.directory / f"{episode_id}.json"

    def _prune_unlocked(self) -> None:
        paths = sorted(
            self.directory.glob("ep-*.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        for path in paths[MAX_EPISODES:]:
            path.unlink(missing_ok=True)


def episode_from_session(session_state: Any, final_answer: str | None) -> EpisodeRecord:
    start = int(getattr(session_state, "active_turn_start_step", 0))
    end = int(getattr(session_state, "step_count", start))
    goal = str(getattr(session_state, "user_goal", ""))[:MAX_EPISODE_GOAL_CHARS]
    status = getattr(session_state, "status", "failed")
    if status not in {"completed", "failed", "max_steps"}:
        raise EpisodeStoreError(f"不能记录未终止的 session status: {status}")
    outcome = (
        str(final_answer)
        if final_answer is not None
        else f"任务以 status={status} 结束，没有可交付 final_answer。"
    )[:MAX_EPISODE_OUTCOME_CHARS]
    digest = hashlib.sha256(goal.encode("utf-8")).hexdigest()[:10]
    message_start = int(
        getattr(session_state, "active_turn_start_message_index", 0)
    )
    # One stable id per user turn. If a process crashes after the episode write
    # but before its checkpoint, replaying finalization remains idempotent.  The
    # message boundary also distinguishes turns cancelled before any new step.
    episode_id = (
        f"ep-{session_state.session_id}-{start}-{message_start}-{digest}"
    )

    executions = sorted(
        (
            execution
            for execution in getattr(session_state, "tool_executions", {}).values()
            if execution.step > start
        ),
        key=lambda execution: execution.step,
    )[:MAX_EPISODE_TOOLS]
    tools = tuple({
        "step": execution.step,
        "name": execution.call.name,
        "status": execution.status,
        "ok": execution.result.ok if execution.result is not None else False,
        "error": (
            execution.result.err[:500]
            if execution.result is not None and execution.result.err
            else ""
        ),
    } for execution in executions)

    current_turns = [
        turn
        for turn in getattr(session_state, "turns", [])
        if turn.step > start
    ]
    verification = tuple(
        {
            "step": turn.step,
            "approved": turn.verification.approved,
            "issues": turn.verification.issues,
        }
        for turn in current_turns
        if turn.verification is not None
    )
    prompt_tokens = sum(
        turn.usage.prompt_tokens for turn in current_turns if turn.usage is not None
    )
    completion_tokens = sum(
        turn.usage.completion_tokens for turn in current_turns if turn.usage is not None
    )
    total_tokens = sum(
        turn.usage.total_tokens for turn in current_turns if turn.usage is not None
    )
    agent_tree = getattr(session_state, "control_plane").tree_summary(
        getattr(session_state, "agent_root_turn_id", "")
    )

    def flatten(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        flattened: list[dict[str, Any]] = []
        for node in nodes:
            children = node.get("children", [])
            flattened.append({
                "id": node.get("id"),
                "parent_id": node.get("parent_id"),
                "depth": node.get("depth"),
                "task": str(node.get("task", ""))[:300],
                "status": node.get("status"),
                "steps_used": node.get("steps_used", 0),
                "total_tokens": node.get("total_tokens", 0),
                "result": str(node.get("result", ""))[:500],
                "error": str(node.get("error", ""))[:500],
            })
            if isinstance(children, list):
                flattened.extend(flatten(children))
        return flattened

    agents = tuple(flatten(agent_tree)[:MAX_EPISODE_AGENTS])
    return EpisodeRecord(
        id=episode_id,
        session_id=str(session_state.session_id),
        goal=goal,
        status=status,
        outcome=outcome,
        started_step=start,
        ended_step=end,
        created_at=datetime.now(timezone.utc).isoformat(),
        plan=getattr(session_state, "plan_manager").snapshot(),
        tools=tools,
        agents=agents,
        verification=verification,
        usage={
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        },
    )


def format_episode_manifest(episodes: list[EpisodeRecord]) -> str:
    return "\n".join(
        f"- {episode.id} [{episode.status}] {episode.created_at}: "
        f"{episode.goal[:180]} -> {episode.outcome[:240]}"
        for episode in episodes
    )


def read_episodes_for_surfacing(episodes: list[EpisodeRecord]) -> str:
    blocks = []
    for episode in episodes:
        tool_line = ", ".join(
            f"{tool.get('name')}:{tool.get('status')}" for tool in episode.tools
        ) or "无"
        agent_line = ", ".join(
            f"{agent.get('id')}:{agent.get('status')}"
            for agent in episode.agents
        ) or "无"
        blocks.append(
            f"### {episode.id}\n"
            f"时间: {episode.created_at}\n"
            f"目标: {episode.goal}\n"
            f"状态: {episode.status}\n"
            f"结果: {episode.outcome}\n"
            f"工具轨迹: {tool_line}\n"
            f"子 Agent: {agent_line}"
        )
    return "\n\n".join(blocks)


def _lock_for(directory: Path) -> threading.RLock:
    with _locks_guard:
        return _store_locks.setdefault(directory, threading.RLock())


def _atomic_write(path: Path, text: str) -> None:
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _string(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise EpisodeStoreError(f"{field} 必须是字符串")
    return value


def _bounded_string(
    value: Any,
    field: str,
    max_chars: int,
    *,
    allow_empty: bool = False,
) -> str:
    text = _string(value, field, allow_empty=allow_empty)
    if len(text) > max_chars:
        raise EpisodeStoreError(f"{field} 不能超过 {max_chars} 个字符")
    return text


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise EpisodeStoreError(f"{field} 必须是非负整数")
    return value
