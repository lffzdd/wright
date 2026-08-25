"""MCP(Model Context Protocol)接入层:把外部 MCP server 暴露的工具,
翻译成本系统的 `Tool`,并入工具池——本 agent 在这里扮演 MCP host/client。

为什么能"零改动下游":executor / 权限 / 协议 / 渲染全只认识 `Tool`(见 base.py),
所以接入 = 多产出几个 `Tool` 对象。MCP 的 inputSchema 与 Tool.parameters 同为
JSON Schema,映射 1:1。

本版范围(刻意收窄):只做 client、配置走 .mcp.json,只接 tools
(不接 resources/prompts,不做动态 list_changed,不做 server 方向)。
transport 支持三种(对标 Claude Code):stdio(子进程)、sse、http(streamable HTTP)。

—— async↔sync 桥(本模块的核心机关)——
官方 mcp SDK 是 asyncio 原生,而本系统的 ToolExecutor 是同步 + 线程池,tool.call 必须
同步返回。McpManager 因此在一条【常驻后台线程】里跑一个事件循环,所有 MCP session 都活
在这个循环里;同步世界(工作线程里的 tool.call)用 run_coroutine_threadsafe 把协程投递
过去再阻塞等结果。这样 executor 那套线程池/超时/保序逻辑一行都不用改——它看到的就是一个
普通的、会阻塞一会儿的同步工具。

session 是有状态长连接(stdio 还带子进程),所以"启动时连一次、发现一次、整段运行期保持、
退出时统一关闭",绝不每次调用现连现关。

—— 为什么整个生命周期挤在一个 _serve 协程里 ——
MCP SDK 的 stdio_client / ClientSession 是 anyio 上下文管理器,内部用 task-bound 的
cancel scope:进入(__aenter__)和退出(__aexit__)必须在【同一个 anyio 任务】里,否则
报 "exit cancel scope in a different task"。而 run_coroutine_threadsafe 每次投递都是一个
独立任务——若在 A 任务里 open、在 B 任务里 close 必炸。所以 _serve 在一个任务里把
"打开所有 session → 交出工具 → park 等关闭信号 → 退出上下文" 一气呵成。工具调用
(call_tool)从工作线程另起任务投递是安全的(只是收发流,不碰 cancel scope)。
"""

import asyncio
import json
import threading
from concurrent.futures import Future
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

from ..logger import get_logger
from ..permission import PermissionCheckResult
from .base import Tool, ToolResult, ToolRuntime

logger = get_logger(__name__)


@dataclass
class McpServerConfig:
    """一个 MCP server 的接入参数(对标 .mcp.json 里 mcpServers 的一条)。

    transport 决定用到哪些字段:
      - "stdio":command(必填)/ args / env —— 起子进程。
      - "sse" / "http":url(必填)/ headers —— 连远端 HTTP 端点。
    """

    name: str
    transport: str = "stdio"
    # stdio 用:
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    # sse / http 用:
    url: str | None = None
    headers: dict[str, str] | None = None


def load_mcp_config(path: Path) -> list[McpServerConfig]:
    """读 .mcp.json,解析出所有 server 配置(stdio / sse / http 三种 transport)。

    格式对标 Claude Code:{"mcpServers": {"<name>": {...}}},条目里:
      - stdio:{"command", "args"?, "env"?}(或显式 "type": "stdio")。
      - sse  :{"type": "sse",  "url", "headers"?}。
      - http :{"type": "http", "url", "headers"?}("streamable-http" 同义)。
    transport 优先看 type;缺省时由字段推断(有 command → stdio,有 url → http)。
    缺关键字段(stdio 无 command / 远端无 url)→ 跳过并 warn。
    文件不存在 → 返回空列表(没配 MCP 是正常情况,不报错)。
    """
    if not path.exists():
        return []

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("读取 MCP 配置失败 %s: %s", path, e)
        return []

    servers = raw.get("mcpServers")
    if not isinstance(servers, dict):
        return []

    configs: list[McpServerConfig] = []
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            logger.warning("跳过 MCP server %r:配置不是对象", name)
            continue

        # transport:显式 type 优先(streamable-http 归一为 http),否则按字段推断。
        transport = entry.get("type")
        if transport == "streamable-http":
            transport = "http"
        if transport is None:
            transport = "stdio" if entry.get("command") else "http" if entry.get("url") else None

        if transport == "stdio":
            command = entry.get("command")
            if not command:
                logger.warning("跳过 MCP server %r:stdio 需要 command 字段", name)
                continue
            configs.append(
                McpServerConfig(
                    name=name,
                    transport="stdio",
                    command=command,
                    args=list(entry.get("args") or []),
                    env=entry.get("env"),
                )
            )
        elif transport in ("sse", "http"):
            url = entry.get("url")
            if not url:
                logger.warning("跳过 MCP server %r:%s 需要 url 字段", name, transport)
                continue
            configs.append(
                McpServerConfig(
                    name=name,
                    transport=transport,
                    url=url,
                    headers=entry.get("headers"),
                )
            )
        else:
            logger.warning(
                "跳过 MCP server %r:无法识别 transport(type=%r,且缺 command/url)",
                name,
                entry.get("type"),
            )
    return configs


class McpManager:
    """持有一条常驻事件循环线程 + 所有 MCP session,负责连接、工具发现与关闭。

    用法:
        mgr = McpManager(load_mcp_config(workspace / ".mcp.json"))
        mcp_tools = mgr.start()          # 连接 + 发现,返回已包装好的 Tool 列表
        ...                              # 整段 agent 运行期 session 保持存活
        mgr.shutdown()                   # 关闭所有 session / 子进程
    """

    def __init__(
        self,
        configs: list[McpServerConfig],
        call_timeout: float = 30.0,
        startup_timeout: float = 30.0,
    ):
        self.configs = configs
        # 单次工具调用超时:应与外层 executor 的 tool_timeout 对齐。
        self.call_timeout = call_timeout
        # 启动(连接 + 发现)整体超时:某个 server 卡在 initialize 时不至于永久挂起。
        self.startup_timeout = startup_timeout
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self.loop.run_forever, name="mcp-loop", daemon=True
        )
        self.sessions: dict[str, ClientSession] = {}
        self._started = False
        self._serve_future: Future | None = None  # _serve 任务句柄(线程安全)
        self._ready: Future | None = None  # _serve 交回工具列表的通道
        self._shutdown_event: asyncio.Event | None = None  # 在 loop 上创建

    def _run(self, coro, timeout: float | None = None):
        """把协程投递到事件循环线程并【同步阻塞】等结果——同步世界↔async 的桥。"""
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return fut.result(timeout=timeout)

    def start(self) -> list[Tool]:
        """起事件循环线程,连接所有 server 并发现工具。返回包装好的 Tool 列表。

        没有任何 server 配置时直接返回空列表(连线程都不必起)。
        """
        if not self.configs:
            return []
        self._thread.start()
        self._started = True
        self._ready = Future()
        # _serve 是一个长生命周期任务:open → 交回工具 → park → close 全在它一个任务里。
        self._serve_future = asyncio.run_coroutine_threadsafe(self._serve(), self.loop)
        # 阻塞等 _serve 完成"连接+发现"阶段,拿回工具列表(park 之前 set_result)。
        return self._ready.result(timeout=self.startup_timeout)

    async def _serve(self) -> None:
        """在单一任务内完成 session 的 open → 服务 → close,绕开 anyio 的任务绑定限制。"""
        self._shutdown_event = asyncio.Event()
        try:
            async with AsyncExitStack() as stack:
                tools: list[Tool] = []
                for cfg in self.configs:
                    try:
                        session = await self._connect_one(stack, cfg)
                    except Exception as e:
                        # 单个 server 连不上不拖垮整体:log 后跳过,其余照常。
                        logger.warning("MCP server %r 连接失败,已跳过:%s", cfg.name, e)
                        continue
                    self.sessions[cfg.name] = session
                    listed = await session.list_tools()
                    for mcp_tool in listed.tools:
                        tools.append(self._wrap(cfg.name, session, mcp_tool))
                    logger.info(
                        "MCP server %r 已连接,发现 %d 个工具",
                        cfg.name,
                        len(listed.tools),
                    )
                # 交回工具列表让 start() 返回;本任务继续 park 住,session 保持存活。
                assert self._ready is not None
                self._ready.set_result(tools)
                await self._shutdown_event.wait()
            # AsyncExitStack 在此退出——与进入它的是【同一个任务】,不触发 cancel scope 报错。
        except Exception as e:
            # 还没交回工具(连接阶段就崩)→ 把异常抛给 start();否则只能 log。
            if self._ready is not None and not self._ready.done():
                self._ready.set_exception(e)
            else:
                logger.warning("MCP serve 任务异常:%s", e)

    async def _connect_one(
        self, stack: AsyncExitStack, cfg: McpServerConfig
    ) -> ClientSession:
        # 按 transport 建立 read/write 流;子进程/连接的生命周期挂在 stack 上,
        # _serve 退出时一次性按相反顺序拆掉。三种 transport 之后的 session 处理完全一致。
        if cfg.transport == "stdio":
            assert cfg.command is not None  # load_mcp_config 已保证 stdio 必有 command
            params = StdioServerParameters(
                command=cfg.command, args=cfg.args, env=cfg.env
            )
            read, write = await stack.enter_async_context(stdio_client(params))
        elif cfg.transport == "sse":
            assert cfg.url is not None  # 远端 transport 必有 url
            read, write = await stack.enter_async_context(
                sse_client(url=cfg.url, headers=cfg.headers)
            )
        elif cfg.transport == "http":
            assert cfg.url is not None
            # streamable http 产出 3 元组,第三个是 get_session_id,本系统用不到。
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(url=cfg.url, headers=cfg.headers)
            )
        else:
            raise ValueError(f"未知 transport: {cfg.transport!r}")

        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return session

    def _wrap(self, server: str, session: ClientSession, mcp_tool) -> Tool:
        """把一个 MCP tool 包装成本系统的 Tool。"""
        # 命名空间防撞名:既避免与内置工具撞,也避免多 server 之间撞(对标 Claude Code)。
        full_name = f"mcp__{server}__{mcp_tool.name}"

        def call(args: dict[str, Any], runtime: ToolRuntime) -> ToolResult:
            try:
                result = self._run(
                    session.call_tool(
                        mcp_tool.name,
                        args,
                        # MCP 协议层的读超时:让远端调用自己到点放弃(协程内部抛错),
                        # 比纯靠下面的 future 超时更干净(不留孤儿协程)。
                        read_timeout_seconds=timedelta(seconds=self.call_timeout),
                    ),
                    # future 超时只作兜底,给协议层超时留点 headroom 让它先触发。
                    timeout=self.call_timeout + 5,
                )
            except Exception as e:
                return ToolResult.fail(f"{type(e).__name__}: {e}")
            return _to_tool_result(result)

        def check_permission(
            args: dict[str, Any], runtime: ToolRuntime
        ) -> PermissionCheckResult:
            # 外部工具默认走审批(对标 web 工具的 _ask_http_request)。
            return PermissionCheckResult(
                "ask",
                f"{runtime.tool_name}: external MCP tool from server {server!r}",
                ("external_mcp",),
                source="tool",
            )

        return Tool(
            name=full_name,
            description=mcp_tool.description or "",
            parameters=mcp_tool.inputSchema,  # JSON Schema 同构,直接透传
            call=call,
            check_permission=check_permission,
            # is_concurrency_safe 不传:沿用默认(排他执行)。远程副作用未知,延续
            # base.py 的保守默认最稳妥。
            # timeout_owner="tool":MCP 调用用 read_timeout_seconds 自带超时语义,
            # 不需要 executor 再叠一层 deadline 计时器去取消一个本就被 future 阻塞的调用。
            timeout_owner="tool",
        )

    def shutdown(self) -> None:
        """关闭所有 session / 子进程,并停掉事件循环线程。可重复调用。"""
        if not self._started:
            return
        try:
            # 给 park 中的 _serve 发关闭信号,它会在自己的任务里退出 AsyncExitStack。
            if self._shutdown_event is not None:
                self.loop.call_soon_threadsafe(self._shutdown_event.set)
            if self._serve_future is not None:
                self._serve_future.result(timeout=5)  # 等 session 干净关闭
        except Exception as e:
            logger.warning("关闭 MCP session 时出错:%s", e)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(timeout=5)
            self._started = False


def _to_tool_result(result) -> ToolResult:
    """把 MCP 的 CallToolResult 翻译成本系统的 ToolResult。

    content 是类型化块的列表(TextContent / ImageContent / ...):文本块拼接,
    非文本块降级成类型占位描述。isError=True → fail。
    """
    parts: list[str] = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(f"[{getattr(block, 'type', 'unknown')} content]")
    text = "\n".join(parts)

    if getattr(result, "isError", False):
        return ToolResult.fail(text or "MCP tool returned an error")
    return ToolResult.success({"content": text})
