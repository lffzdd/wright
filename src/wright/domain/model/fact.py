"""Domain models, types, and constants for factual and semantic memory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ...base.value_object import ValueObject

MemoryType = Literal["user", "feedback", "project", "reference"]
MEMORY_TYPES: tuple[MemoryType, ...] = ("user", "feedback", "project", "reference")

FactScope = Literal["GLOBAL", "PROJECT"]
FactCategory = Literal["USER_PREFERENCE", "PROJECT_FACT", "LESSON_LEARNED", "REFERENCE"]


def parse_memory_type(raw: object) -> MemoryType | None:
    """把 frontmatter 里的原始 type 值归一成合法 MemoryType。"""
    if isinstance(raw, str) and raw in MEMORY_TYPES:
        return raw  # type: ignore[return-value]
    return None


class MemoryStoreError(ValueError):
    """Semantic memory input or state is invalid."""


class MemoryAlreadyExistsError(MemoryStoreError):
    pass


class MemoryNotFoundError(MemoryStoreError):
    pass


@dataclass(frozen=True)
class Fact(ValueObject):
    """Structured immutable fact memory item."""

    id: str
    content: str
    key: str = ""
    scope: FactScope = "PROJECT"
    category: FactCategory = "PROJECT_FACT"
    importance: float = 1.0
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class MemoryHeader:
    id: str
    filename: str
    path: Any
    mtime: float
    name: str
    description: str | None
    type: MemoryType | None
    created_at: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    name: str
    description: str
    type: MemoryType
    content: str
    created_at: str
    updated_at: str
    path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "type": self.type,
            "content": self.content,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "file": self.path.name,
        }

    def to_fact(self, scope: FactScope = "PROJECT") -> Fact:
        """Convert a file-based MemoryRecord into a domain Fact value object."""
        category_map: dict[MemoryType, FactCategory] = {
            "user": "USER_PREFERENCE",
            "feedback": "LESSON_LEARNED",
            "project": "PROJECT_FACT",
            "reference": "REFERENCE",
        }
        return Fact(
            id=self.id,
            key=self.name,
            content=self.content,
            scope=scope,
            category=category_map.get(self.type, "PROJECT_FACT"),
            created_at=self.created_at,
            updated_at=self.updated_at,
        )


TYPES_SECTION = """## 记忆的类型

你的记忆分为以下四种,只能用这四种,不要自创:

<types>
<type>
  <name>user</name>
  <desc>关于用户的角色、目标、职责、知识背景。好的 user 记忆让你能按用户的水平和
  偏好调整后续行为——对一个资深工程师和一个第一次写代码的学生,协作方式应该不同。
  目标是搞清「用户是谁、怎样对他最有帮助」。避免写带负面评判、或与协作无关的内容。</desc>
  <when>当你了解到用户的角色、偏好、职责或知识背景时。</when>
  <how>当你的工作应当被用户画像影响时。比如解释代码,要用最贴合他既有心智模型的方式讲。</how>
  <example>用户:我是数据科学家,在排查我们有哪些日志 → [存 user 记忆:用户是数据科学家,当前关注可观测性/日志]</example>
</type>
<type>
  <name>feedback</name>
  <desc>用户给你的「该怎么做事」的指导——既包括纠正,也包括确认。这是非常重要的一类:
  它让你在项目里保持连贯、不重复犯错。失败和成功都要记:只记纠正会让你越来越畏手畏脚,
  漂离那些用户已经认可的做法。</desc>
  <when>用户纠正你(「不对」「别这样」「停下来别做 X」)或确认某个不显然的做法有效
  (「对就这样」「保持」、默默接受一个不寻常的选择)时。纠正好察觉,确认更安静、要留意。
  记下对未来仍适用、尤其是意外或代码里看不出来的部分,并附上【为什么】以便日后判断边界。</when>
  <how>让这些指导直接影响你的行为,使用户不必把同样的话说第二遍。</how>
  <body>正文先写规则本身,再跟一行 **Why:**(用户给的理由,常是某次事故或强偏好)和一行
  **How to apply:**(这条在什么时候/什么地方生效)。知道为什么,才能判断边界而非死守。</body>
  <example>用户:这些测试别 mock 数据库,上季度 mock 测试过了但生产迁移挂了 →
  [存 feedback 记忆:集成测试必须连真库不要 mock。Why:此前 mock 与生产偏差掩盖了坏迁移]</example>
</type>
<type>
  <name>project</name>
  <desc>你了解到的、关于这个项目里正在进行的工作、目标、计划、bug、事故等背景,且这些
  无法从代码或 git 历史推导。project 记忆帮你理解用户请求背后的动机与全局语境。</desc>
  <when>当你了解到「谁在做什么、为什么、什么时候之前」。这类状态变化较快,尽量保持更新。
  保存时务必把相对日期换算成绝对日期(「周四」→「2026-03-05」),以免日后无法解读。</when>
  <how>用它更全面地理解请求的细节与微妙之处,做出更知情的建议。</how>
  <body>正文先写事实/决定,再跟 **Why:**(动机,常是约束、截止日、干系人诉求)和
  **How to apply:**(它该如何影响你的建议)。project 记忆衰减快,Why 帮未来的你判断它是否还成立。</body>
  <example>用户:周四之后冻结所有非关键合并,移动端要切发布分支 →
  [存 project 记忆:2026-03-05 起进入合并冻结(移动端发布切分支),此后排期的非关键 PR 要提醒]</example>
</type>
<type>
  <name>reference</name>
  <desc>指向外部系统里信息所在位置的指针。让你记住「去哪里找项目目录之外的最新信息」。</desc>
  <when>当你了解到外部系统资源及其用途。比如 bug 跟踪在某个 Linear 项目、反馈在某个 Slack 频道。</when>
  <how>当用户提到某个外部系统、或所需信息可能在外部系统里时。</how>
  <example>用户:管线的 bug 都在 Linear 的 INGEST 项目里跟踪 →
  [存 reference 记忆:管线 bug 跟踪于 Linear 项目 "INGEST"]</example>
</type>
</types>
"""

WHAT_NOT_TO_SAVE = """## 不要存进记忆的内容

- 代码模式、约定、架构、文件路径、项目结构——读当前项目状态就能得到。
- git 历史、近期改动、谁改了什么——`git log` / `git blame` 才是权威。
- 调试解法、修 bug 配方——修复在代码里,来龙去脉在 commit message 里。
- 任何已经写在 CLAUDE.md / README 等项目文档里的内容。
- 临时性任务细节:进行中的工作、临时状态、当前对话上下文。

即便用户明确要你保存,这些排除项依然适用。如果用户让你存一份 PR 列表或活动总结,
反问其中【意外或不显然】的部分是什么——那才是值得留下的。"""

WHEN_TO_ACCESS = """## 何时存取记忆

- 当记忆看起来相关、或用户提及过往对话里的工作时,主动调用记忆。
- 当用户明确要你检查、回忆、记住某事时,你【必须】存取记忆。
- 如果用户说【忽略】或【不要用】记忆:就当 MEMORY.md 是空的——不要套用、不要引用、
  不要对比、也不要提及任何记忆内容。
- 记忆会随时间过期。把记忆当作「某个时间点为真」的上下文。在据此回答或做假设之前,
  先读当前文件/资源核实它是否仍然正确。若记忆与当前情况冲突,相信你现在观察到的,
  并更新或删除那条陈旧记忆,而不是照着它行动。"""

TRUSTING_RECALL = """## 据记忆推荐之前

一条提到具体函数、文件或开关的记忆,只是在说它【写入记忆的那一刻】存在过。它可能已被
改名、删除,或从未合并。在据此推荐之前:

- 记忆提到文件路径:确认文件存在。
- 记忆提到函数或开关:grep 一下。
- 如果用户即将照你的推荐行动(而不只是问历史),先核实。

「记忆说 X 存在」不等于「X 现在存在」。

一条概括仓库状态的记忆(活动日志、架构快照)是时间冻结的。若用户问的是【近期/当前】
状态,优先用 `git log` 或直接读代码,而不是回忆那份快照。"""

FRONTMATTER_EXAMPLE = """```markdown
---
name: {{记忆名,短横线 kebab-case}}
description: {{一句话描述——未来据此判断是否相关,写具体些}}
type: {{user, feedback, project, reference 之一}}
---

{{记忆正文——feedback/project 类型请按:规则/事实,然后 **Why:** 和 **How to apply:** 两行}}
```"""

__all__ = [
    "FRONTMATTER_EXAMPLE",
    "Fact",
    "FactCategory",
    "FactScope",
    "MEMORY_TYPES",
    "MemoryAlreadyExistsError",
    "MemoryHeader",
    "MemoryNotFoundError",
    "MemoryRecord",
    "MemoryStoreError",
    "MemoryType",
    "TRUSTING_RECALL",
    "TYPES_SECTION",
    "WHAT_NOT_TO_SAVE",
    "WHEN_TO_ACCESS",
    "parse_memory_type",
]
