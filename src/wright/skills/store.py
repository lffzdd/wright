"""扫描、解析和原子写入 workspace/skills/<id>/SKILL.md。"""

from __future__ import annotations

import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .types import (
    MAX_SKILL_BODY_CHARS,
    MAX_SKILL_DESCRIPTION_CHARS,
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_ID_CHARS,
    MAX_SKILL_NAME_CHARS,
    MAX_SKILLS,
    SAFE_SKILL_ID_RE,
    SkillDefinition,
    SkillMeta,
    SkillNotFoundError,
    SkillStoreError,
)


_FRONTMATTER_RE = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?(.*)\Z",
    re.DOTALL,
)


def normalize_skill_id(skill_id: str) -> str:
    value = str(skill_id).strip()
    if value.endswith("/SKILL.md"):
        value = value[: -len("/SKILL.md")]
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


def _single_line(value: object, field: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise SkillStoreError(f"{field} 必须是字符串")
    cleaned = " ".join(value.splitlines()).strip()
    if not cleaned:
        raise SkillStoreError(f"skill frontmatter 缺少 {field}")
    if len(cleaned) > max_chars:
        raise SkillStoreError(f"{field} 不能超过 {max_chars} 个字符")
    return cleaned


def _parse_scalar(raw: str) -> Any:
    text = raw.strip()
    if not text:
        return ""
    if (text.startswith('"') and text.endswith('"')) or (
        text.startswith("'") and text.endswith("'")
    ):
        return text[1:-1]
    if text.startswith("[") and text.endswith("]"):
        inner = text[1:-1].strip()
        if not inner:
            return []
        items = []
        for part in inner.split(","):
            item = _parse_scalar(part)
            if item != "":
                items.append(item)
        return items
    lowered = text.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    return text


def parse_skill_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        raise SkillStoreError("SKILL.md 必须包含 YAML frontmatter（以 --- 包围）")
    raw_fm, body = match.group(1), match.group(2)
    data: dict[str, Any] = {}
    lines = raw_fm.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            index += 1
            continue
        if ":" not in line:
            raise SkillStoreError(f"无法解析 frontmatter 行: {line}")
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        if not key:
            raise SkillStoreError("frontmatter 键不能为空")
        if rest:
            data[key] = _parse_scalar(rest)
            index += 1
            continue
        items: list[Any] = []
        index += 1
        while index < len(lines):
            nested = lines[index]
            nested_stripped = nested.strip()
            if nested_stripped.startswith("- "):
                items.append(_parse_scalar(nested_stripped[2:]))
                index += 1
                continue
            if not nested_stripped:
                index += 1
                continue
            break
        data[key] = items
    return data, body


def _allowed_tools(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = _parse_scalar(value) if value.startswith("[") else [
            item.strip() for item in value.split(",") if item.strip()
        ]
    if not isinstance(value, list):
        raise SkillStoreError("allowed_tools 必须是字符串列表")
    tools: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise SkillStoreError("allowed_tools 的每一项必须是非空字符串")
        name = item.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
            raise SkillStoreError(f"非法工具名: {name}")
        if name not in tools:
            tools.append(name)
    return tuple(tools)


def parse_skill_markdown(skill_id: str, text: str, path: Path) -> SkillDefinition:
    frontmatter, body = parse_skill_frontmatter(text)
    name = _single_line(frontmatter.get("name"), "name", MAX_SKILL_NAME_CHARS)
    description = _single_line(
        frontmatter.get("description"), "description", MAX_SKILL_DESCRIPTION_CHARS
    )
    allowed = _allowed_tools(frontmatter.get("allowed_tools"))
    cleaned_body = body.strip()
    if len(cleaned_body) > MAX_SKILL_BODY_CHARS:
        raise SkillStoreError(
            f"skill 正文不能超过 {MAX_SKILL_BODY_CHARS} 个字符"
        )
    return SkillDefinition(
        meta=SkillMeta(
            id=skill_id,
            name=name,
            description=description,
            allowed_tools=allowed,
            path=path,
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
    text = path.read_text(encoding="utf-8")
    resolved_id = skill_id or path.parent.name
    return parse_skill_markdown(normalize_skill_id(resolved_id), text, path)


def dump_skill_markdown(
    name: str,
    description: str,
    body: str,
    allowed_tools: list[str] | None = None,
) -> str:
    lines = ["---", f"name: {name}", f"description: {description}"]
    if allowed_tools:
        joined = ", ".join(allowed_tools)
        lines.append(f"allowed_tools: [{joined}]")
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
    children = sorted(
        (child for child in directory.iterdir()),
        key=lambda path: path.name,
    )
    for child in children:
        if child.is_symlink():
            errors.append(f"跳过符号链接目录: {child.name}")
            continue
        if not child.is_dir():
            continue
        try:
            skill_id = normalize_skill_id(child.name)
        except SkillStoreError as exc:
            errors.append(f"跳过非法 skill id {child.name!r}: {exc}")
            continue
        skill_path = child / "SKILL.md"
        if skill_path.is_symlink():
            errors.append(f"跳过符号链接: {skill_id}")
            continue
        if not skill_path.is_file():
            errors.append(f"跳过缺少 SKILL.md 的目录: {skill_id}")
            continue
        if len(definitions) >= MAX_SKILLS:
            errors.append(
                f"已达到 skill 数量上限 {MAX_SKILLS}，忽略 {skill_id}"
            )
            continue
        try:
            definitions.append(load_skill_file(skill_path, skill_id))
        except (SkillStoreError, OSError) as exc:
            errors.append(f"跳过损坏的 skill {skill_id}: {exc}")
    return definitions, errors
