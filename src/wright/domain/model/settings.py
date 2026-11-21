"""Typed runtime settings: the schema, its defaults and per-key validation.

Settings are non-secret tunables. They come from layered sources (built-in
default, user file, project file, environment); this module only describes
each key and turns a raw value into a typed one. Merging the layers is a rule
in ``domain.policy.settings``; reading files is an infrastructure concern.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from typing import Any, Literal

SettingScope = Literal["user", "project"]
SettingSource = Literal["default", "user", "project", "env"]
SettingKind = Literal["str", "int", "bool", "choice", "path", "list"]

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


class SettingsError(ValueError):
    """A settings file or value that cannot be used."""


@dataclass(frozen=True)
class SettingSpec:
    """One key: its type, default and where it may be set.

    ``project_safe`` keys may come from ``{project}/.wright/settings.json``. A
    project file can arrive with a cloned repository, so endpoints, paths that
    load code, and anything touching trust stay user-only. ``fail_closed``
    keys make an invalid value in a trusted layer an error instead of falling
    back, because the fallback could send data somewhere else.
    """

    key: str
    kind: SettingKind
    default: Any
    env: tuple[str, ...] = ()
    project_safe: bool = False
    fail_closed: bool = False
    choices: tuple[str, ...] = ()
    minimum: int | None = None
    description: str = ""

    def coerce(self, raw: object) -> Any:
        """Return the typed value; raise SettingsError when it does not fit."""
        if self.kind == "list":
            return self._coerce_list(raw)
        if self.kind == "bool":
            return self._coerce_bool(raw)
        if self.kind == "int":
            return self._coerce_int(raw)
        if not isinstance(raw, str):
            raise SettingsError(f"{self.key} must be a string")
        text = raw.strip()
        if self.kind == "choice":
            for choice in self.choices:
                if choice.lower() == text.lower():
                    return choice
            raise SettingsError(f"{self.key} must be one of: {', '.join(self.choices)}")
        if self.kind == "path" and not text:
            raise SettingsError(f"{self.key} must not be empty")
        return text

    def _coerce_list(self, raw: object) -> tuple[str, ...]:
        if isinstance(raw, str):
            items: list[object] = list(raw.split(","))
        elif isinstance(raw, list):
            items = raw
        else:
            raise SettingsError(f"{self.key} must be a list of strings")
        if not all(isinstance(item, str) for item in items):
            raise SettingsError(f"{self.key} must be a list of strings")
        cleaned = (str(item).strip() for item in items)
        return tuple(dict.fromkeys(item for item in cleaned if item))

    def _coerce_bool(self, raw: object) -> bool:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            text = raw.strip().lower()
            if text in _TRUE:
                return True
            if text in _FALSE:
                return False
        raise SettingsError(f"{self.key} must be true or false")

    def _coerce_int(self, raw: object) -> int:
        if isinstance(raw, bool):
            raise SettingsError(f"{self.key} must be an integer")
        if isinstance(raw, int):
            value = raw
        elif isinstance(raw, str) and raw.strip().lstrip("-").isdigit():
            value = int(raw.strip())
        else:
            raise SettingsError(f"{self.key} must be an integer")
        if self.minimum is not None and value < self.minimum:
            raise SettingsError(f"{self.key} must be at least {self.minimum}")
        return value

    def to_json(self, value: Any) -> Any:
        return list(value) if self.kind == "list" else value


SETTING_SPECS: tuple[SettingSpec, ...] = (
    SettingSpec(
        "llm.model", "str", "", env=("OPENAI_MODEL",), project_safe=True,
        description="Model for new sessions; --model overrides it.",
    ),
    SettingSpec(
        "llm.models", "list", (), env=("WRIGHT_MODELS",),
        description="Models offered by the model picker; empty offers catalog models whose provider is reachable.",
    ),
    SettingSpec(
        "llm.memory_model", "str", "", env=("OPENAI_MEMORY_MODEL",), project_safe=True,
        description="Cheaper model for memory recall, fact extraction, the memory egress check and session titles; empty uses llm.model.",
    ),
    SettingSpec(
        "llm.context_limit", "int", 0, env=("OPENAI_CONTEXT_LIMIT",),
        project_safe=True, minimum=0,
        description="Context window for every model, in tokens; 0 uses the built-in catalog, then 128000.",
    ),
    SettingSpec(
        "llm.context_limits", "list", (), env=("WRIGHT_CONTEXT_LIMITS",), project_safe=True,
        description="Context windows of single models, as model=tokens; they take precedence over llm.context_limit.",
    ),
    SettingSpec(
        "llm.prices", "list", (), env=("WRIGHT_MODEL_PRICES",),
        description="Prices for models the built-in list lacks or prices differently, as model=input/output[/cache_read] in USD per million tokens.",
    ),
    SettingSpec(
        "llm.transport", "choice", "auto", env=("WRIGHT_LLM_TRANSPORT",),
        choices=("auto", "chat", "responses", "messages"),
        description="Model API: chat completions, responses, Anthropic messages, or chosen from the endpoint.",
    ),
    SettingSpec(
        "llm.base_url", "str", "", env=("OPENAI_BASE_URL",), fail_closed=True,
        description="OpenAI-compatible endpoint for every model, sent with OPENAI_API_KEY; empty picks the endpoint of a catalog model's provider.",
    ),
    SettingSpec(
        "session.auto_title", "bool", True, env=("WRIGHT_AUTO_TITLE",),
        description="Name each session with one short model call after its first turn.",
    ),
    SettingSpec(
        "skills.disabled", "list", (), env=("WRIGHT_DISABLED_SKILLS",), project_safe=True,
        description="Skill ids left out of the catalog; load_skill refuses them.",
    ),
    SettingSpec(
        "memory.enabled", "bool", True, env=("WRIGHT_MEMORY_ENABLED",),
        description="Long-term memory: the resident block, recall, memory tools and turn records. The egress check stays on either way.",
    ),
    SettingSpec(
        "memory.recall", "bool", True, env=("WRIGHT_MEMORY_RECALL",),
        description="Pick relevant facts and past turns with one memory-model call per user turn; off keeps only the resident block.",
    ),
    SettingSpec(
        "memory.auto_extract", "bool", True, env=("WRIGHT_MEMORY_AUTO_EXTRACT",),
        description="Save inferred facts from finished turns without being asked; off keeps only facts written on purpose.",
    ),
    SettingSpec(
        "logging.level", "choice", "WARNING", env=("WRIGHT_LOG_LEVEL",),
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        description="Lowest level shown in the terminal; the log file always keeps INFO.",
    ),
    SettingSpec(
        "knowledge.enabled", "bool", False, env=("WRIGHT_KNOWLEDGE_ENABLED",),
        description="Offer the knowledge_search tool.",
    ),
    SettingSpec(
        "knowledge.retriever", "choice", "dense", env=("WRIGHT_KNOWLEDGE_RETRIEVER",),
        choices=("dense", "hybrid"),
        description="Retriever used by knowledge_search.",
    ),
    SettingSpec(
        "knowledge.reranker", "bool", False, env=("WRIGHT_KNOWLEDGE_RERANKER",),
        description="Rerank knowledge_search results.",
    ),
    SettingSpec(
        "knowledge.rag_dir", "path", "", env=("WRIGHT_RAG_DIR",), fail_closed=True,
        description="Directory containing rag_chain.py; Wright imports Python from it.",
    ),
    SettingSpec(
        "knowledge.index", "path", "", env=("WRIGHT_KNOWLEDGE_INDEX",), fail_closed=True,
        description="Knowledge index file; defaults to simple_index.json in knowledge.rag_dir.",
    ),
    SettingSpec(
        "tools.web_search", "choice", "auto", env=("WRIGHT_SEARCH_PROVIDER",),
        choices=("auto", "tavily", "brave", "off"),
        description="Search provider for web_search; auto uses the first one with a key, off removes the tool.",
    ),
    # Trust and sandbox keys stay user-only. Their invalid values fall back to
    # the default, which is the strictest choice, so they need not fail closed.
    SettingSpec(
        "mcp.trust_project", "bool", False, env=("WRIGHT_TRUST_PROJECT_MCP",),
        description="Also start the MCP servers in the project's .wright/mcp.json; they run commands from the repository.",
    ),
    SettingSpec(
        "sandbox.default_execution", "choice", "sandbox", env=("WRIGHT_DEFAULT_EXECUTION",),
        choices=("sandbox", "host"),
        description="Where shell commands of new sessions run; host runs them without the system sandbox.",
    ),
    SettingSpec(
        "sandbox.egress", "choice", "proxy", env=("WRIGHT_SANDBOX_EGRESS",),
        choices=("proxy", "legacy"),
        description="Sandboxed network: proxy decides each host; legacy opens or closes all of it per command. Windows always uses legacy.",
    ),
    # Losing a hooks file would also lose the hooks that deny tool calls.
    SettingSpec(
        "hooks.config", "path", "", env=("WRIGHT_HOOKS_CONFIG",), fail_closed=True,
        description="Lifecycle hooks file; --hooks-config overrides it.",
    ),
)

SPECS_BY_KEY: Mapping[str, SettingSpec] = {spec.key: spec for spec in SETTING_SPECS}


@dataclass(frozen=True)
class LlmSettings:
    model: str = ""
    models: tuple[str, ...] = ()
    memory_model: str = ""
    context_limit: int = 0
    context_limits: tuple[str, ...] = ()
    prices: tuple[str, ...] = ()
    transport: str = "auto"
    base_url: str = ""


@dataclass(frozen=True)
class SessionSettings:
    auto_title: bool = True


@dataclass(frozen=True)
class SkillsSettings:
    disabled: tuple[str, ...] = ()


@dataclass(frozen=True)
class MemorySettings:
    enabled: bool = True
    recall: bool = True
    auto_extract: bool = True


@dataclass(frozen=True)
class LoggingSettings:
    level: str = "WARNING"


@dataclass(frozen=True)
class KnowledgeSettings:
    enabled: bool = False
    retriever: str = "dense"
    reranker: bool = False
    rag_dir: str = ""
    index: str = ""


@dataclass(frozen=True)
class ToolsSettings:
    web_search: str = "auto"


@dataclass(frozen=True)
class McpSettings:
    trust_project: bool = False


@dataclass(frozen=True)
class SandboxSettings:
    default_execution: str = "sandbox"
    egress: str = "proxy"


@dataclass(frozen=True)
class HooksSettings:
    config: str = ""


_SECTIONS: Mapping[str, type] = {
    "llm": LlmSettings,
    "session": SessionSettings,
    "skills": SkillsSettings,
    "memory": MemorySettings,
    "logging": LoggingSettings,
    "knowledge": KnowledgeSettings,
    "tools": ToolsSettings,
    "mcp": McpSettings,
    "sandbox": SandboxSettings,
    "hooks": HooksSettings,
}


@dataclass(frozen=True)
class WrightSettings:
    """Effective settings after merging every layer.

    ``sources`` records which layer supplied each key, for display only.
    """

    llm: LlmSettings = field(default_factory=LlmSettings)
    session: SessionSettings = field(default_factory=SessionSettings)
    skills: SkillsSettings = field(default_factory=SkillsSettings)
    memory: MemorySettings = field(default_factory=MemorySettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    knowledge: KnowledgeSettings = field(default_factory=KnowledgeSettings)
    tools: ToolsSettings = field(default_factory=ToolsSettings)
    mcp: McpSettings = field(default_factory=McpSettings)
    sandbox: SandboxSettings = field(default_factory=SandboxSettings)
    hooks: HooksSettings = field(default_factory=HooksSettings)
    sources: Mapping[str, SettingSource] = field(default_factory=dict, compare=False)

    @classmethod
    def from_values(
        cls,
        values: Mapping[str, Any],
        sources: Mapping[str, SettingSource] | None = None,
    ) -> WrightSettings:
        sections = {}
        for name, section_cls in _SECTIONS.items():
            sections[name] = section_cls(**{
                item.name: values[f"{name}.{item.name}"]
                for item in fields(section_cls)
                if f"{name}.{item.name}" in values
            })
        return cls(**sections, sources=dict(sources or {}))

    def value(self, key: str) -> Any:
        section, _, name = key.partition(".")
        return getattr(getattr(self, section), name)


def _check_schema() -> None:
    """The section dataclasses and the spec table must describe the same keys."""
    declared = {
        f"{name}.{item.name}": item.default
        for name, section_cls in _SECTIONS.items()
        for item in fields(section_cls)
    }
    if set(declared) != set(SPECS_BY_KEY):
        raise RuntimeError("settings sections and SETTING_SPECS disagree")
    for key, default in declared.items():
        if SPECS_BY_KEY[key].default != default:
            raise RuntimeError(f"default for {key} disagrees")


_check_schema()


__all__ = [
    "SETTING_SPECS",
    "SPECS_BY_KEY",
    "HooksSettings",
    "KnowledgeSettings",
    "LlmSettings",
    "LoggingSettings",
    "McpSettings",
    "MemorySettings",
    "SandboxSettings",
    "SessionSettings",
    "SettingKind",
    "SettingScope",
    "SettingSource",
    "SettingSpec",
    "SettingsError",
    "SkillsSettings",
    "ToolsSettings",
    "WrightSettings",
]
