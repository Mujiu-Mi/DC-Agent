"""
工具调用分发器

接收 AI 返回的 tool_calls，逐个调用 MCP hub 执行，把结果通过
add_context 回调加进会话上下文。

工具来源（统一由 MCP hub 管理）：
  - 内置工具：tools/builtin_server.py 注册的 MCPServer，走 run_builtin_tool
    带 safety.py + permission_manager 安全闸门。
  - 外部工具：Config/mcp_servers.json 配置的外部 MCP server，工具名
    形如 "<server>__<tool>"，由 hub 路由到对应连接。

对外接口与旧版一致：
  if_tool(ai_message, add_context, perm_mgr, workdir)
  其中 ai_message.tool_calls 是 OpenAI 格式的工具调用列表。
"""
import json

from tools.mcp_client import hub as mcp_hub
from core.permission_manager import PermissionManager


def if_tool(ai_message, add_context, perm_mgr: PermissionManager | None = None, workdir: str = ""):
    """处理工具调用，将结果加入上下文（通过 add_context(iden, ctx) 回调）。"""
    if not ai_message.tool_calls:
        return None

    for tool_call in ai_message.tool_calls:
        try:
            args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError:
            add_context("tool_error", f"工具 {tool_call.function.name} 参数解析失败")
            continue
        name = tool_call.function.name

        # 统一走 MCP hub：内置工具走带安全闸门的本地执行，
        # 外部工具（含 "__" 前缀）路由到对应 MCP 连接。
        result = mcp_hub.call_tool(name, args, perm_mgr, workdir)
        add_context(name, result)


# ━━━━━ 流式支持：把字典工具调用包装成 if_tool 可识别的对象 ━━━━━

class _FakeFunc:
    __slots__ = ("name", "arguments")
    def __init__(self, d): self.name = d["name"]; self.arguments = d["arguments"]


class _FakeTC:
    __slots__ = ("id", "function", "type")
    def __init__(self, d):
        self.id = d.get("id", ""); self.function = _FakeFunc(d["function"]); self.type = "function"


class _FakeMsg:
    __slots__ = ("tool_calls",)
    def __init__(self, data): self.tool_calls = [_FakeTC(tc) for tc in data]
