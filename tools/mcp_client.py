"""
MCP 工具聚合器：统一管理「内置工具」+「外部 MCP server」的连接。

对上层（chat/loop.py）只暴露两个同步接口：
  - list_all_tools() -> list[dict]    返回 OpenAI tools schema 格式的工具清单
  - call_tool(name, args, ...) -> str 执行工具并返回文本结果

复杂的地方在于：MCP 的连接管理（握手、initialize）是异步的，
但我们的聊天循环是同步代码，所以 MCPHub 自己起了一个后台事件循环线程，
所有异步操作都用 run_coroutine_threadsafe 扔进去，再同步等结果。

工具名前缀约定：
  - 内置工具直接用原名（read_file、run_cmd ...）
  - 外部 server 的工具用 "<server名>__<工具名>"，避免和内置重名

外部 MCP 配置写在 Config/mcp_servers.json：
  [
    {"name": "weather", "transport": "stdio", "command": "python", "args": ["m", "weather_server.py"]},
    {"name": "myapi",   "transport": "sse",   "url": "http://localhost:8080/sse"},
    {"name": "remote",  "transport": "http",  "url": "http://1.2.3.4:9000/mcp"}
  ]
"""
import os
import json
import asyncio
import threading

from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client

from tools.builtin_server import builtin_server, run_builtin_tool, TOOL_FUNCS
from core.permission_manager import PermissionManager
from utils import logger

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MCP_CONFIG = os.path.join(_BASE_DIR, "Config", "mcp_servers.json")


def _load_external_config():
    """读取 Config/mcp_servers.json；文件缺失或格式错时返回空列表。"""
    if not os.path.exists(_MCP_CONFIG):
        return []
    try:
        with open(_MCP_CONFIG, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        logger.error("MCP_CLIENT", "mcp_servers.json 顶层应为数组，已忽略外部 MCP 配置")
        return []
    except Exception as e:
        logger.error("MCP_CLIENT", f"读取 mcp_servers.json 失败: {e}")
        return []


def _save_external_config(configs):
    """写入 Config/mcp_servers.json。"""
    try:
        os.makedirs(os.path.dirname(_MCP_CONFIG), exist_ok=True)
        with open(_MCP_CONFIG, "w", encoding="utf-8") as f:
            json.dump(configs, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error("MCP_CLIENT", f"写入 mcp_servers.json 失败: {e}")
        return False


class MCPHub:
    """
    单例聚合器：所有工具连接都归它管。

    背景自己起一个后台事件循环线程，所有异步的 MCP 操作都送过去执行，
    我们这里只需要"同步等结果"。
    """

    def __init__(self):
        self._loop = None                 # 后台事件循环
        self._loop_thread = None          # 后台事件循环线程
        # 可重入锁：_ensure_initialized 持锁期间 _start_loop 还要拿同一把锁
        self._init_lock = threading.RLock()
        self._initialized = False
        self._external_clients = {}       # server名 -> Client（已建立的连接）
        self._external_tools_cache = {}   # server名 -> 工具列表缓存

    # ─────────── 后台事件循环 ───────────

    def _start_loop(self):
        """确保后台事件循环已启动（线程已存在就什么都不做）。"""
        if self._loop is not None and self._loop.is_running():
            return
        with self._init_lock:
            if self._loop is not None and self._loop.is_running():
                return
            self._loop = asyncio.new_event_loop()

            def _run():
                asyncio.set_event_loop(self._loop)
                self._loop.run_forever()

            self._loop_thread = threading.Thread(target=_run, daemon=True, name="mcp-hub-loop")
            self._loop_thread.start()

    def _run_async(self, coro, timeout=60.0):
        """把异步任务扔进后台事件循环，同步等它执行完。"""
        self._start_loop()
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    # ─────────── 连接外部 server ───────────

    async def _build_client(self, cfg):
        """按 transport 类型建立连接，进入连接上下文（握手 + initialize）。"""
        transport = cfg.get("transport", "").lower()
        if transport == "stdio":
            params = StdioServerParameters(
                command=cfg["command"],
                args=cfg.get("args", []),
                env=cfg.get("env"),
            )
            client = Client(server=stdio_client(params))
        elif transport in ("sse", "http", "streamable_http"):
            url = cfg.get("url", "")
            if not url:
                logger.error("MCP_CLIENT", f"外部 MCP {cfg.get('name')} 缺少 url")
                return None
            client = Client(server=url)
        else:
            logger.error("MCP_CLIENT", f"未知 transport: {transport} (配置: {cfg.get('name')})")
            return None
        await client.__aenter__()  # 连接失败会抛异常，由调用方处理
        return client

    async def _connect_one(self, cfg):
        """连接单个外部 server。"""
        name = cfg.get("name", "")
        if name in self._external_clients:
            return
        client = await self._build_client(cfg)
        if client is not None:
            self._external_clients[name] = client
            logger.info("MCP_CLIENT", f"外部 MCP 已连接: {name}")

    async def _init_external(self):
        """逐个连接配置文件里的外部 server，失败跳过不影响内置工具。"""
        for cfg in _load_external_config():
            name = cfg.get("name", "")
            if not name:
                logger.error("MCP_CLIENT", f"外部 MCP 配置缺少 name 字段，跳过: {cfg}")
                continue
            if name in self._external_clients:
                continue
            try:
                await self._connect_one(cfg)
            except Exception as e:
                logger.error("MCP_CLIENT", f"连接外部 MCP {name} 失败: {e}")

    def _ensure_initialized(self):
        """首次使用时读配置并连接外部 server；失败不阻塞内置工具。"""
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            try:
                self._run_async(self._init_external(), timeout=120.0)
            except Exception as e:
                logger.error("MCP_CLIENT", f"初始化外部 MCP 连接失败: {e}")
            self._initialized = True

    # ─────────── 列出所有工具 ───────────

    async def _list_all_async(self):
        """收集内置 + 外部的所有工具，统一转成 OpenAI tools schema。"""
        result = []

        # 内置工具：读 MCPServer 的注册清单
        try:
            mcp_tools = await builtin_server.list_tools()
            for t in mcp_tools:
                result.append(_mcp_tool_to_openai(t.name, t.description or "", t.input_schema))
        except Exception as e:
            logger.error("MCP_CLIENT", f"列出内置工具失败: {e}")

        # 外部工具：每个 server 的名字加前缀
        for sname, client in list(self._external_clients.items()):
            try:
                lr = await client.list_tools()
                tools = getattr(lr, "tools", [])
                self._external_tools_cache[sname] = tools
                for t in tools:
                    full_name = f"{sname}__{t.name}"
                    result.append(_mcp_tool_to_openai(full_name, t.description or "", t.input_schema))
            except Exception as e:
                logger.error("MCP_CLIENT", f"列出外部 MCP {sname} 工具失败: {e}")

        return result

    def list_all_tools(self):
        """对外接口：返回全部可用工具（OpenAI tools schema 格式）。"""
        self._ensure_initialized()
        try:
            return self._run_async(self._list_all_async(), timeout=30.0)
        except Exception as e:
            logger.error("MCP_CLIENT", f"list_all_tools 失败，降级返回空列表: {e}")
            return []

    # ─────────── 执行工具 ───────────

    async def _call_async(self, name, args, perm_mgr=None, workdir=""):
        # 内置工具：走带安全闸门的本地执行
        if name in TOOL_FUNCS:
            return run_builtin_tool(name, args, perm_mgr, workdir)

        # 外部工具：解析 "<server>__<tool>" 前缀，找对应连接
        if "__" in name:
            sname, tname = name.split("__", 1)
            client = self._external_clients.get(sname)
            if client is None:
                return f"错误：未连接的外部 MCP server「{sname}」（工具 {tname} 无法调用）"
            try:
                cr = await client.call_tool(tname, args)
                return _call_result_to_text(cr)
            except Exception as e:
                return f"工具执行异常（外部 MCP {sname}）: {e}"

        return f"错误：未知工具 {name}"

    def call_tool(self, name, args, perm_mgr=None, workdir=""):
        """对外接口：执行工具，返回文本结果。"""
        self._ensure_initialized()
        try:
            return self._run_async(self._call_async(name, args, perm_mgr, workdir), timeout=120.0)
        except Exception as e:
            logger.error("MCP_CLIENT", f"call_tool {name} 失败: {e}")
            return f"工具执行异常: {e}"

    # ─────────── 清理与外部 MCP 管理 ───────────

    async def _close_all(self):
        """断开所有外部连接。（跨 task 退出可能报 cancel scope 错误，忽略即可，
        底层 transport/子进程会在连接断开时自动清理。）"""
        for sname, client in list(self._external_clients.items()):
            try:
                await client.__aexit__(None, None, None)
            except Exception:
                pass
        self._external_clients.clear()

    def shutdown(self):
        """进程退出前的清理。"""
        if not self._initialized or self._loop is None:
            return
        try:
            self._run_async(self._close_all(), timeout=10.0)
        except Exception:
            pass

    # ---- 供 routes/config_api.py 调用的管理接口 ----

    def list_external(self):
        """返回持久化的外部 MCP 配置（不会建立新连接）。"""
        return _load_external_config()

    def add_external(self, cfg):
        """保存配置并尝试连接；连接失败时配置仍会保留。

        必需字段：stdio 用 name/transport/command，http/sse 用 name/transport/url。
        """
        name = cfg.get("name", "").strip()
        if not name:
            return {"status": "error", "error": "缺少 name 字段"}
        transport = cfg.get("transport", "").lower()
        if transport not in ("stdio", "sse", "http", "streamable_http"):
            return {"status": "error", "error": "transport 必须是 stdio / sse / http"}
        if transport == "stdio" and not cfg.get("command"):
            return {"status": "error", "error": "stdio 类型需要 command 字段"}
        if transport in ("sse", "http", "streamable_http") and not cfg.get("url"):
            return {"status": "error", "error": f"{transport} 类型需要 url 字段"}

        configs = _load_external_config()
        if any(c.get("name") == name for c in configs):
            return {"status": "error", "error": f"已存在名为 {name} 的 MCP server"}
        configs.append(cfg)
        if not _save_external_config(configs):
            return {"status": "error", "error": "写入 mcp_servers.json 失败"}

        self._ensure_initialized()
        try:
            self._run_async(self._connect_one(cfg), timeout=60.0)
            return {"status": "ok", "message": f"已连接外部 MCP: {name}"}
        except Exception as e:
            return {"status": "error", "error": f"配置已保存但连接失败: {e}"}

    def remove_external(self, name):
        """从配置和连接池中移除名为 name 的外部 server。"""
        configs = _load_external_config()
        new_configs = [c for c in configs if c.get("name") != name]
        if len(new_configs) == len(configs):
            return {"status": "error", "error": f"未找到名为 {name} 的 MCP server"}
        _save_external_config(new_configs)

        client = self._external_clients.pop(name, None)
        if client is not None:
            try:
                self._run_async(client.__aexit__(None, None, None), timeout=10.0)
            except Exception:
                pass
        self._external_tools_cache.pop(name, None)
        return {"status": "ok", "message": f"已移除外部 MCP: {name}"}

    def reload_external(self):
        """断开后按 mcp_servers.json 重连，返回成功/失败名称列表。"""
        self._ensure_initialized()
        try:
            self._run_async(self._close_all(), timeout=15.0)
        except Exception:
            pass
        self._initialized = False
        self._ensure_initialized()
        connected = list(self._external_clients.keys())
        all_names = [c.get("name", "") for c in _load_external_config()]
        failed = [n for n in all_names if n not in connected]
        return {"status": "ok", "connected": connected, "failed": failed}


# ============================================================
# 工具函数
# ============================================================

def _mcp_tool_to_openai(name, description, input_schema):
    """把 MCP 的 Tool 转成 OpenAI chat completions 的 tools schema。"""
    schema = dict(input_schema) if isinstance(input_schema, dict) else {"type": "object", "properties": {}}
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    schema["additionalProperties"] = False
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": schema,
        },
    }


def _call_result_to_text(cr):
    """把 MCP 的 CallToolResult 转成纯文本。"""
    parts = []
    for block in getattr(cr, "content", []) or []:
        t = getattr(block, "type", "")
        if t == "text":
            parts.append(getattr(block, "text", ""))
        else:
            parts.append(f"[{t} content]")  # 图片/音频等非文本内容给个占位
    text = "\n".join(p for p in parts if p)
    if getattr(cr, "is_error", False):
        text = f"[工具返回错误] {text}"
    return text or "(工具执行完毕，无输出)"


# 进程级单例：整个服务共享一个 hub
hub = MCPHub()
