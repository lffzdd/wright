"""Generate portable Web test data from the real permission resolver."""

from __future__ import annotations

import json
import posixpath
from pathlib import Path, PurePosixPath

from wright.domain.gateway.execution import ExecutionPath
from wright.domain.model.tool import ToolCall
from wright.domain.policy.permission.resolver import (
    PermissionPolicy,
    PermissionResolver,
)
from wright.domain.policy.permission.scope import AccessScope
from wright.domain.policy.permission.settings import PermissionRule, PermissionSettings
from wright.domain.policy.permission.types import InvocationIdentity
from wright.infrastructure.tools.command import execute_command_tool
from wright.infrastructure.tools.file import write_file_tool
from wright.interfaces.i18n.catalog import EN, ZH


def permission_copy() -> dict:
    return {
        locale: {
            key: value
            for key, value in catalog.items()
            if key.startswith("permission.")
        }
        for locale, catalog in (("en", EN), ("zh-CN", ZH))
    }


class FixturePaths:
    def cwd(self) -> ExecutionPath:
        return ExecutionPath("fixture", "/project")

    def resolve_path(
        self, requested: str, *, cwd: ExecutionPath | None = None
    ) -> ExecutionPath:
        return ExecutionPath(
            "fixture",
            posixpath.normpath(posixpath.join((cwd or self.cwd()).value, requested)),
        )


def permission_fixtures() -> dict:
    prompts = {}
    for name, tool, arguments in (
        (
            "file",
            write_file_tool,
            {
                "file": "/external/result.txt",
                "content": "A new file\n" + "preview line\n" * 100,
            },
        ),
        ("shell", execute_command_tool, {"command": "echo hello"}),
    ):
        result = PermissionResolver().resolve(
            ToolCall(tool.name, arguments, f"approval-{name}"),
            tool,
            backend=FixturePaths(),
            scope=AccessScope(PurePosixPath("/project")),
            identity=InvocationIdentity("session-permission"),
        )
        prompts[name] = {"kind": "permission", **result.prompt.to_dict()}
    records = tuple(
        PermissionRule(
            "*",
            kind="file",
            root="/external",
            pattern=f"{lifetime}.txt",
            operations=("file_read", "file_write"),
            exact=True,
            lifetime=lifetime,
            id=lifetime,
        )
        for lifetime in ("session", "project", "user")
    )
    settings = PermissionSettings(allow=list(records[1:]))
    return {
        "prompts": prompts,
        "effective_policy": PermissionPolicy(settings).summarize(
            roots=["/project"], session_rules=records[:1]
        ),
    }


if __name__ == "__main__":
    target = (
        Path(__file__).resolve().parents[1]
        / "web/tests/fixtures/permission-contract.json"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(permission_fixtures(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (target.parents[2] / "src/permission-copy.json").write_text(
        json.dumps(permission_copy(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
