"""Final-answer completion gate.

Structural and deterministic: unfinished plans, in-flight tools, and claimed
writes that are missing from disk. It runs after the model has already streamed
a candidate answer, and only decides whether the agent loop may stop. A
rejection is fed back so the agent can keep using its own tools.

There is no second-model reviewer. Claims like "tests passed" are the main
agent's job to substantiate with tool results.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass

from .session import Session


@dataclass(frozen=True)
class VerificationIssue:
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class VerificationResult:
    approved: bool
    issues: tuple[VerificationIssue, ...] = ()

    @classmethod
    def approve(cls) -> VerificationResult:
        return cls(True)

    @classmethod
    def reject(
        cls, issues: Iterable[VerificationIssue]
    ) -> VerificationResult:
        normalized = tuple(issues)
        if not normalized:
            normalized = (
                VerificationIssue("incomplete", "验证器未说明拒绝原因"),
            )
        return cls(False, normalized)

    def feedback_message(self) -> dict:
        return {
            "role": "user",
            "content": json.dumps(
                {
                    "verification_feedback": {
                        "approved": self.approved,
                        "issues": [issue.to_dict() for issue in self.issues],
                        "instruction": (
                            "不要重复同一个最终答案。请使用现有工具补齐证据或工作，"
                            "更新计划后再给出最终回答。"
                        ),
                    }
                },
                ensure_ascii=False,
            ),
        }


class Verifier:
    """Deterministic completion checks. No LLM."""

    def verify(
        self, session: Session, final_answer: str = "",
    ) -> VerificationResult:
        del final_answer
        hard_issues = self._structural_issues(session)
        if hard_issues:
            return VerificationResult.reject(hard_issues)
        return VerificationResult.approve()

    @staticmethod
    def _structural_issues(session: Session) -> list[VerificationIssue]:
        issues: list[VerificationIssue] = []
        plan = session.plan_manager
        if plan.has_plan and plan.status != "completed":
            issues.append(VerificationIssue(
                "plan_incomplete",
                f"当前计划状态是 {plan.status}，请先完成、跳过或重新规划未收口步骤",
            ))

        unfinished = [
            execution.call.id
            for execution in session.tool_executions.values()
            if execution.step > session.active_turn_start_step
            and execution.status in {"pending", "running"}
        ]
        if unfinished:
            issues.append(VerificationIssue(
                "tool_execution_unfinished",
                f"仍有未完成的工具调用: {', '.join(unfinished)}",
            ))
        issues.extend(Verifier._artifact_issues(session))
        return issues

    @staticmethod
    def _artifact_issues(session: Session) -> list[VerificationIssue]:
        """Re-stat files that successful first-party write tools claim to have made."""
        issues: list[VerificationIssue] = []
        workspace = session.workspace_dir.resolve()
        for execution in session.tool_executions.values():
            if (
                execution.step <= session.active_turn_start_step
                or execution.status != "succeeded"
                or execution.call.name not in {"write_file", "edit_file"}
            ):
                continue
            raw_path = execution.call.arguments.get("file")
            if not isinstance(raw_path, str) or not raw_path:
                continue
            path = (workspace / raw_path).resolve()
            if not path.is_relative_to(workspace) or not path.is_file():
                issues.append(VerificationIssue(
                    "artifact_missing",
                    f"工具曾报告写入成功，但文件当前不存在: {raw_path}",
                ))
        return issues
