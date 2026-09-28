"""扫描、解析和原子写入 skills/<id>/SKILL.md。"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

import yaml

from ...domain.model.skills import (
    MAX_SKILL_BODY_CHARS,
    MAX_SKILL_COMPATIBILITY_CHARS,
    MAX_SKILL_DESCRIPTION_CHARS,
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_ID_CHARS,
    MAX_SKILL_LICENSE_CHARS,
    MAX_SKILL_NAME_CHARS,
    MAX_SKILLS,
    SAFE_SKILL_ID_RE,
    SkillDefinition,
    SkillMeta,
    SkillNotFoundError,
    SkillStoreError,
)

# Frontmatter keys with behavior or a reserved metadata slot.
# Suggestions come only from allowed-tools. They never grant permission.
_RESERVED_KEYS = frozenset({
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
})


class _UniqueKeyLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_map(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=False)
        if key in mapping:
            raise SkillStoreError(f"frontmatter 键重复: {key}")
        mapping[key] = loader.construct_object(value_node, deep=False)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_map,
)

_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)\Z",
    re.DOTALL,
)


def normalize_skill_id(skill_id: str) -> str:
    value = str(skill_id).strip()
    value = value.removesuffix("/SKILL.md")
    if "/" in value or "\\" in value or value in {".", ".."}:
        raise SkillStoreError("skill_id 必须是安全的目录名，不能包含路径")
    if not value or SAFE_SKILL_ID_RE.fullmatch(value) is None:
        raise SkillStoreError(
            f"skill_id 必须匹配 {SAFE_SKILL_ID_RE.pattern}，不能包含路径"
        )
    if len(value) > MAX_SKILL_ID_CHARS:
        raise SkillStoreError(f"skill_id 不能超过 {MAX_SKILL_ID_CHARS} 个字符")
    return value


def skill_file_path(directory: Path, skill_id: str) -> Path:
    directory = directory.expanduser().resolve()
    normalized = normalize_skill_id(skill_id)
    path = (directory / normalized / "SKILL.md").resolve()
    if path.parent.parent != directory or path.parent.name != normalized:
        raise SkillStoreError("skill path 越界")
    return path


def _required_text(value: object, field: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise SkillStoreError(f"{field} 必须是字符串")
    cleaned = " ".join(value.splitlines()).strip()
    if not cleaned:
        raise SkillStoreError(f"skill frontmatter 缺少 {field}")
    if len(cleaned) > max_chars:
        raise SkillStoreError(
            f"{field} 不能超过 {max_chars} 个字符（当前 {len(cleaned)}）"
        )
    return cleaned


def _optional_text(value: object, field: str, max_chars: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise SkillStoreError(f"{field} 必须是字符串")
    cleaned = " ".join(value.splitlines()).strip()
    if len(cleaned) > max_chars:
        raise SkillStoreError(
            f"{field} 不能超过 {max_chars} 个字符（当前 {len(cleaned)}）"
        )
    return cleaned


def _plain_metadata(value: Any, *, depth: int = 0) -> Any:
    """Keep JSON-like metadata. safe_load already blocks arbitrary objects."""
    if depth > 6:
        raise SkillStoreError("metadata 嵌套过深")
    if value is None or isinstance(value, (str, int, bool)):
        if isinstance(value, str) and len(value) > 4_000:
            raise SkillStoreError("metadata 字符串过长")
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise SkillStoreError("metadata 不能包含 NaN 或无穷大")
        return value
    if isinstance(value, list):
        if len(value) > 50:
            raise SkillStoreError("metadata 列表过长")
        return [_plain_metadata(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > 50:
            raise SkillStoreError("metadata 字段过多")
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise SkillStoreError("metadata 键必须是非空字符串")
            cleaned[key] = _plain_metadata(item, depth=depth + 1)
        return cleaned
    raise SkillStoreError(f"metadata 包含不支持的类型: {type(value).__name__}")


def _metadata_pairs(value: object) -> tuple[tuple[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise SkillStoreError("metadata 必须是映射")
    plain = _plain_metadata(value)
    return tuple(
        (key, json.dumps(item, ensure_ascii=False, sort_keys=True))
        for key, item in plain.items()
    )


def _extra_pairs(frontmatter: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    pairs: list[tuple[str, str]] = []
    for key, value in frontmatter.items():
        if key in _RESERVED_KEYS:
            continue
        if not isinstance(key, str) or not key.strip():
            raise SkillStoreError("frontmatter 键必须是非空字符串")
        plain = _plain_metadata(value)
        pairs.append((key, json.dumps(plain, ensure_ascii=False, sort_keys=True)))
    return tuple(pairs)


def parse_skill_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise SkillStoreError("SKILL.md 必须包含 YAML frontmatter（以 --- 包围）")
    raw_fm, body = match.group(1), match.group(2)
    try:
        loaded = yaml.load(raw_fm, Loader=_UniqueKeyLoader)
    except SkillStoreError:
        raise
    except yaml.YAMLError as exc:
        raise SkillStoreError(f"无法解析 skill frontmatter: {exc}") from exc
    if loaded is None:
        raise SkillStoreError("skill frontmatter 为空")
    if not isinstance(loaded, dict):
        raise SkillStoreError("skill frontmatter 必须是映射")
    return loaded, body


def _allowed_tools(value: object, *, field: str) -> tuple[str, ...]:
    """Normalize the spec string or a YAML sequence to one suggestion tuple."""
    if value is None:
        return ()
    if isinstance(value, str):
        # Spec form is a space-delimited string. A YAML sequence is the same field.
        parts = list(value.split())
    elif isinstance(value, list):
        parts = list(value)
    else:
        raise SkillStoreError(f"{field} 必须是字符串或字符串列表")
    tools: list[str] = []
    for item in parts:
        if not isinstance(item, str) or not item.strip():
            raise SkillStoreError(f"{field} 的每一项必须是非空字符串")
        name = item.strip()
        if len(name) > 200 or any(char.isspace() or ord(char) < 32 for char in name):
            raise SkillStoreError(f"{field} 包含非法工具名: {name!r}")
        if name not in tools:
            tools.append(name)
    return tuple(tools)


def _normalize_allowed_tools(frontmatter: dict[str, Any]) -> tuple[str, ...]:
    if "allowed_tools" in frontmatter:
        raise SkillStoreError("请使用 allowed-tools，不再接受 allowed_tools")
    if "allowed-tools" not in frontmatter:
        return ()
    return _allowed_tools(frontmatter.get("allowed-tools"), field="allowed-tools")


def parse_skill_markdown(skill_id: str, text: str, path: Path) -> SkillDefinition:
    frontmatter, body = parse_skill_frontmatter(text)
    name = _required_text(frontmatter.get("name"), "name", MAX_SKILL_NAME_CHARS)
    if name != skill_id:
        raise SkillStoreError(f"name 必须与目录名一致: {skill_id}")
    description = _required_text(
        frontmatter.get("description"), "description", MAX_SKILL_DESCRIPTION_CHARS
    )
    cleaned_body = body.strip()
    if len(cleaned_body) > MAX_SKILL_BODY_CHARS:
        raise SkillStoreError(
            f"skill 正文不能超过 {MAX_SKILL_BODY_CHARS} 个字符"
            f"（当前 {len(cleaned_body)}）"
        )
    return SkillDefinition(
        meta=SkillMeta(
            id=skill_id,
            name=name,
            description=description,
            allowed_tools=_normalize_allowed_tools(frontmatter),
            path=path,
            license=_optional_text(
                frontmatter.get("license"), "license", MAX_SKILL_LICENSE_CHARS
            ),
            compatibility=_optional_text(
                frontmatter.get("compatibility"),
                "compatibility",
                MAX_SKILL_COMPATIBILITY_CHARS,
            ),
            metadata=_metadata_pairs(frontmatter.get("metadata")),
            extra=_extra_pairs(frontmatter),
        ),
        body=cleaned_body,
    )


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise SkillStoreError(f"拒绝符号链接: {label}")


def load_skill_file(path: Path, skill_id: str | None = None) -> SkillDefinition:
    path = path.resolve()
    _reject_symlink(path, str(path))
    if not path.is_file():
        raise SkillNotFoundError(f"skill 不存在: {path}")
    size = path.stat().st_size
    if size > MAX_SKILL_FILE_BYTES:
        raise SkillStoreError(
            f"skill 文件过大（{size} 字节，上限 {MAX_SKILL_FILE_BYTES}）"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SkillStoreError(
            f"SKILL.md 不是有效的 UTF-8（{path.name}）: {exc.reason}"
        ) from exc
    except OSError as exc:
        raise SkillStoreError(f"无法读取 {path.name}: {exc}") from exc
    resolved_id = skill_id or path.parent.name
    return parse_skill_markdown(normalize_skill_id(resolved_id), text, path)


def _yaml_scalar(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def dump_skill_markdown(
    name: str,
    description: str,
    body: str,
    allowed_tools: list[str] | None = None,
) -> str:
    """Write allowed-tools. name is the skill directory name."""
    lines = [
        "---",
        f"name: {_yaml_scalar(name)}",
        f"description: {_yaml_scalar(description)}",
    ]
    if allowed_tools:
        rendered = ", ".join(_yaml_scalar(item) for item in allowed_tools)
        lines.append(f"allowed-tools: [{rendered}]")
    lines.append("---")
    lines.append("")
    lines.append(body.rstrip())
    lines.append("")
    return "\n".join(lines)


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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


def write_skill(
    directory: Path,
    skill_id: str,
    *,
    name: str,
    description: str,
    body: str,
    allowed_tools: list[str] | None = None,
) -> Path:
    """测试和人工维护用的写入入口；不暴露给模型。"""
    path = skill_file_path(directory, skill_id)
    text = dump_skill_markdown(name, description, body, allowed_tools)
    if len(text.encode("utf-8")) > MAX_SKILL_FILE_BYTES:
        raise SkillStoreError("skill 文件超过大小上限")
    parse_skill_markdown(normalize_skill_id(skill_id), text, path)
    _atomic_write(path, text)
    return path


def scan_skills(directory: Path) -> tuple[list[SkillDefinition], list[str]]:
    """扫描目录。单个坏 skill 只记录错误，不让整个仓库不可用。"""
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        return [], []

    definitions: list[SkillDefinition] = []
    errors: list[str] = []
    try:
        children = sorted(directory.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        return [], [f"无法扫描 skill 目录 {directory}: {exc}"]
    for child in children:
        try:
            if child.is_symlink():
                errors.append(f"跳过符号链接目录: {child.name}")
                continue
            if not child.is_dir():
                continue
        except OSError as exc:
            errors.append(f"跳过无法读取的目录 {child.name}: {exc}")
            continue
        try:
            skill_id = normalize_skill_id(child.name)
        except SkillStoreError as exc:
            errors.append(f"跳过非法 skill id {child.name!r}: {exc}")
            continue
        skill_path = child / "SKILL.md"
        try:
            if skill_path.is_symlink():
                errors.append(f"跳过符号链接: {skill_id}")
                continue
            if not skill_path.is_file():
                errors.append(f"跳过缺少 SKILL.md 的目录: {skill_id}")
                continue
        except OSError as exc:
            errors.append(f"跳过无法读取的 skill {skill_id}: {exc}")
            continue
        if len(definitions) >= MAX_SKILLS:
            errors.append(
                f"已达到 skill 数量上限 {MAX_SKILLS}，忽略 {skill_id}"
            )
            continue
        try:
            definitions.append(load_skill_file(skill_path, skill_id))
        except SkillStoreError as exc:
            errors.append(f"跳过损坏的 skill {skill_id}: {exc}")
    return definitions, errors


__all__ = [
    "dump_skill_markdown",
    "load_skill_file",
    "normalize_skill_id",
    "parse_skill_frontmatter",
    "parse_skill_markdown",
    "scan_skills",
    "skill_file_path",
    "write_skill",
]
