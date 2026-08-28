#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DeepSeek Code - 终端客户端 (TUI)
连接 DC Server 的 WebSocket 接口，提供带主题与动画的美观终端界面。
启动时将当前目录作为工作目录发送给服务端。
"""

import os
import sys
import json
import asyncio
import subprocess
import time
import uuid
import re
from datetime import datetime
import urllib.request

import websockets
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK

from textual.app import App, ComposeResult
from textual.widgets import TextArea, Static, OptionList
from textual.widgets.option_list import Option
from textual.containers import VerticalScroll, Container
from textual.reactive import reactive
from textual.binding import Binding
from textual.screen import ModalScreen
from textual import work
from textual.theme import Theme
from textual.color import Color
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from rich.panel import Panel
from rich.console import Group
from rich.table import Table


APP_NAME = "DeepSeek"
VERSION = "2.0.0"


COMMANDS = [
    ("/help",       "显示帮助"),
    ("/clear",      "清除对话上下文"),
    ("/model",      "查看当前模型"),
    ("/config",     "查看/修改配置"),
    ("/settings",   "打开外观设置"),
    ("/theme",      "快速切换主题"),
    ("/approve",    "当前对话自动批准工具请求"),
    ("/allow",      "开启/关闭自动审核"),
    ("/think",      "调整 AI 思考模式"),
    ("/mcp",        "管理外部 MCP 服务器"),
    ("/reconnect",  "重新连接服务器"),
    ("/restart",    "重启服务端"),
    ("/sessions",   "列出历史对话"),
    ("/resume",     "恢复历史对话"),
    ("/new",        "新建对话"),
    ("/quit",       "退出程序"),
]

ACTIVITY_STATES = [
    "🧠 AI 正在头脑风暴",
    "✍️ AI 正在努力编写",
    "🔎 AI 正在翻找线索",
    "🧩 AI 正在拼接思路",
    "🧪 AI 正在验证方案",
    "📚 AI 正在整理答案",
    "✨ AI 正在润色回复",
    "🚀 AI 正在准备输出",
]

ACTIVITY_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


LOGO = r"""
██████╗ ███████╗███████╗██████╗ ███████╗███████╗███████╗██╗  ██╗
██╔══██╗██╔════╝██╔════╝██╔══██╗██╔════╝██╔════╝██╔════╝██║ ██╔╝
██║  ██║█████╗  █████╗  ██████╔╝███████╗█████╗  █████╗  █████╔╝ 
██║  ██║██╔══╝  ██╔══╝  ██╔═══╝ ╚════██║██╔══╝  ██╔══╝  ██╔═██╗ 
██████╔╝███████╗███████╗██║     ███████║███████╗███████╗██║  ██╗
╚═════╝ ╚══════╝╚══════╝╚═╝     ╚══════╝╚══════╝╚══════╝╚═╝  ╚═╝
""".strip("\n")


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def _now():
    return datetime.now().strftime("%H:%M")


def _client_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _server_dir():
    return os.path.abspath(os.path.join(_client_dir(), "..", "DC Server"))


CONFIG_PATH = os.path.join(_client_dir(), "config.json")
CONVERSATIONS_PATH = os.path.join(_client_dir(), "conversations.json")
MAX_CONVERSATIONS = 50
MAX_CONVERSATION_MESSAGES = 200

_STARTED_SERVER_PID = None

DEFAULT_APPEARANCE = {
    "theme": "ocean",
    "animations": True,
    "animation_speed": "normal",
    "accent": None,
    "user_color": None,
    "assistant_color": None,
    "think_color": None,
    "error_color": None,
    "tool_color": None,
    "logo_color": None,
}

DEFAULT_CONFIG = {
    "server_host": "127.0.0.1",
    "server_port": 8520,
    "auto_start_local_server": True,
    "appearance": dict(DEFAULT_APPEARANCE),
}


def load_config():
    """读取客户端 ``config.json``，并与 ``DEFAULT_CONFIG`` 深度合并后返回。"""
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in DEFAULT_CONFIG:
                if isinstance(DEFAULT_CONFIG[k], dict) and isinstance(data.get(k), dict):
                    merged = dict(DEFAULT_CONFIG[k])
                    merged.update({kk: vv for kk, vv in data[k].items() if vv is not None})
                    cfg[k] = merged
                elif k in data:
                    cfg[k] = data[k]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return cfg


def save_config(cfg):
    """持久化客户端连接和外观设置；成功返回 ``True``，失败返回 ``False``。"""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


def load_conversations():
    """读取本地历史会话；文件不存在或格式异常时返回空列表。"""
    try:
        with open(CONVERSATIONS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return []


def save_conversations(conversations):
    """保存最近 ``MAX_CONVERSATIONS`` 个会话；历史保存失败不阻断聊天。"""
    try:
        with open(CONVERSATIONS_PATH, "w", encoding="utf-8") as f:
            json.dump(conversations[:MAX_CONVERSATIONS], f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def new_conversation():
    """创建尚未写入服务端的本地会话数据结构。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    return {
        "id": uuid.uuid4().hex,
        "title": "新对话",
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }


def add_conversation_message(conversation, identity, text):
    """追加 User/Model 消息、更新标题，并按上限截断会话历史。"""
    text = str(text or "").strip()
    if not text or identity not in ("User", "Model"):
        return
    messages = conversation.setdefault("messages", [])
    messages.append({"identity": identity, "text": text})
    if len(messages) > MAX_CONVERSATION_MESSAGES:
        del messages[:-MAX_CONVERSATION_MESSAGES]
    if identity == "User" and conversation.get("title") == "新对话":
        conversation["title"] = text.replace("\n", " ")[:40]
    conversation["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")


def server_history(conversation):
    """转换本地会话为发送给 Server 的 ``identity/text`` 历史消息列表。"""
    return [
        {"identity": item["identity"], "text": item["text"]}
        for item in conversation.get("messages", [])[-MAX_CONVERSATION_MESSAGES:]
        if item.get("identity") in ("User", "Model") and isinstance(item.get("text"), str)
    ]


def session_choices(conversations, current_id):
    items = [
        item for item in conversations
        if item.get("id") != current_id and item.get("messages")
    ]
    return sorted(items, key=lambda item: item.get("updated_at", ""), reverse=True)


def session_list_text(conversations, current_id):
    items = session_choices(conversations, current_id)
    if not items:
        return "暂无可恢复的历史对话。"
    lines = ["历史对话："]
    for index, item in enumerate(items, 1):
        title = item.get("title") or "未命名对话"
        updated = item.get("updated_at", "未知时间")
        count = len(item.get("messages", []))
        lines.append(f"  {index}. {title}  [{updated}，{count} 条消息]")
    lines.append("使用 /resume <编号> 恢复对话。")
    return "\n".join(lines)


def detect_server_url():
    """优先读取 ``DCCAT_SERVER``，否则用客户端配置拼出 `/dscat` WebSocket 地址。"""
    env = os.environ.get("DCCAT_SERVER")
    if env:
        if not env.startswith(("ws://", "wss://")):
            env = "ws://" + env
        return env
    cfg = load_config()
    host = cfg.get("server_host", "127.0.0.1")
    port = cfg.get("server_port", 8520)
    return f"ws://{host}:{port}/dscat"


def _http_url(ws_url):
    """把 WebSocket 基础地址转换为同主机的 HTTP 基础地址。"""
    return ws_url.replace("ws://", "http://").replace("wss://", "https://")


THINK_EFFORTS = {"low", "medium", "high", "max"}


def get_thinking_config(server_url):
    """读取服务端当前的思考配置。"""
    url = _http_url(server_url).replace("/dscat", "/config")
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def set_thinking_config(server_url, payload):
    """调用 ``POST /config/thinking``，返回服务端确认后的思考配置。"""
    url = _http_url(server_url).replace("/dscat", "/config/thinking")
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_think_command(text):
    """解析 /think 指令。payload=None 表示只查询当前配置。"""
    parts = text.lower().split()
    if len(parts) == 1 or (len(parts) == 2 and parts[1] == "status"):
        return None, None
    action = parts[1]
    if action == "auto" and len(parts) == 2:
        return {"auto_think": True}, None
    if action == "off" and len(parts) == 2:
        return {"auto_think": False, "mode": "disabled"}, None
    if action == "on":
        if len(parts) == 2:
            return {"auto_think": False, "mode": "enabled"}, None
        if len(parts) == 3 and parts[2] in THINK_EFFORTS:
            return {"auto_think": False, "mode": "enabled", "effort": parts[2]}, None
    if action == "effort" and len(parts) == 3 and parts[2] in THINK_EFFORTS:
        return {"auto_think": False, "mode": "enabled", "effort": parts[2]}, None
    return None, "用法: /think | /think auto | /think on [low|medium|high|max] | /think off | /think effort <强度>"


def _mcp_http_url(server_url, path):
    """拼出外部 MCP 管理 API 的 HTTP URL。"""
    return _http_url(server_url).replace("/dscat", path)


def _call_mcp_api(server_url, text):
    """
    解析 /mcp 指令并调用服务端的 /config/mcp* API。
    返回给用户看的文本消息。

    指令格式：
      /mcp                                  列出已配置的外部 MCP server
      /mcp add <name> stdio <command> [args...]  添加 stdio 类型
      /mcp add <name> sse <url>             添加 sse 类型
      /mcp add <name> http <url>            添加 http 类型
      /mcp remove <name>                    移除
      /mcp reload                           重新连接所有外部 MCP server
    """
    parts = text.split()
    if len(parts) == 1:
        # /mcp -> 列出
        try:
            with urllib.request.urlopen(_mcp_http_url(server_url, "/config/mcp"), timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
            servers = data.get("servers", [])
            if not servers:
                return "当前没有配置外部 MCP 服务器。\n用 /mcp add <名称> stdio <命令> [参数...] 添加"
            lines = ["外部 MCP 服务器:"]
            for s in servers:
                t = s.get("transport", "?")
                name = s.get("name", "?")
                if t == "stdio":
                    detail = f"{s.get('command', '')} {' '.join(s.get('args', []))}".strip()
                else:
                    detail = s.get("url", "")
                lines.append(f"  • {name} [{t}] {detail}")
            lines.append("\n修改后用 /mcp reload 重新连接。")
            return "\n".join(lines)
        except Exception as e:
            return f"[错误] 查询外部 MCP 失败: {e}"

    action = parts[1].lower()

    if action == "reload":
        try:
            req = urllib.request.Request(_mcp_http_url(server_url, "/config/mcp/reload"), method="POST")
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode("utf-8"))
            if data.get("status") == "ok":
                connected = data.get("connected", [])
                failed = data.get("failed", [])
                msg = f"已重新连接 {len(connected)} 个外部 MCP 服务器"
                if connected:
                    msg += "：" + "、".join(connected)
                if failed:
                    msg += f"\n连接失败：{'、'.join(failed)}"
                return msg
            return f"[错误] {data.get('error', '重连失败')}"
        except Exception as e:
            return f"[错误] 重连失败: {e}"

    if action == "add":
        if len(parts) < 5:
            return ("用法:\n"
                    "  /mcp add <名称> stdio <命令> [参数...]\n"
                    "  /mcp add <名称> sse <url>\n"
                    "  /mcp add <名称> http <url>")
        name = parts[2]
        transport = parts[3].lower()
        cfg = {"name": name, "transport": transport}
        if transport == "stdio":
            if len(parts) < 5:
                return "[错误] stdio 类型需要提供 command"
            cfg["command"] = parts[4]
            cfg["args"] = parts[5:] if len(parts) > 5 else []
        elif transport in ("sse", "http", "streamable_http"):
            if len(parts) < 5:
                return f"[错误] {transport} 类型需要提供 url"
            cfg["url"] = parts[4]
        else:
            return f"[错误] 不支持的 transport: {transport}（可选 stdio / sse / http）"
        try:
            req = urllib.request.Request(
                _mcp_http_url(server_url, "/config/mcp"),
                data=json.dumps(cfg).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode("utf-8"))
            if data.get("status") == "ok":
                return data.get("message", f"已添加外部 MCP: {name}")
            return f"[错误] {data.get('error', '添加失败')}"
        except Exception as e:
            return f"[错误] 添加外部 MCP 失败: {e}"

    if action == "remove":
        if len(parts) < 3:
            return "用法: /mcp remove <名称>"
        name = parts[2]
        try:
            req = urllib.request.Request(
                _mcp_http_url(server_url, "/config/mcp"),
                data=json.dumps({"name": name}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="DELETE",
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                data = json.loads(r.read().decode("utf-8"))
            if data.get("status") == "ok":
                return data.get("message", f"已移除外部 MCP: {name}")
            return f"[错误] {data.get('error', '移除失败')}"
        except Exception as e:
            return f"[错误] 移除外部 MCP 失败: {e}"

    return ("用法:\n"
            "  /mcp                                列出外部 MCP 服务器\n"
            "  /mcp add <名称> stdio <命令> [参数...]  添加 stdio 类型\n"
            "  /mcp add <名称> sse <url>           添加 sse 类型\n"
            "  /mcp add <名称> http <url>          添加 http 类型\n"
            "  /mcp remove <名称>                  移除\n"
            "  /mcp reload                         重新连接所有")


def _server_process():
    """返回相邻 DC Server 的工作目录、虚拟环境 Python 和 main.py；缺失时全为 ``None``。"""
    sd = _server_dir()
    py = os.path.join(sd, ".venv", "Scripts", "python.exe")
    main = os.path.join(sd, "main.py")
    if os.path.exists(py) and os.path.exists(main):
        return sd, py, main
    return None, None, None


def _http_up(ws_url, timeout=2):
    try:
        with urllib.request.urlopen(_http_url(ws_url), timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def ensure_server(ws_url):
    """在启用自动启动时拉起本地 DC Server。

    服务已可访问或配置关闭自动启动时返回 ``False``；只有本次成功创建并等到
    Server 就绪时返回 ``True``。调用方可用 ``_STARTED_SERVER_PID`` 区分归属。
    """
    global _STARTED_SERVER_PID
    if _http_up(ws_url):
        return False
    cfg = load_config()
    if not cfg.get("auto_start_local_server", True):
        return False
    sd, py, main = _server_process()
    if not py:
        return False
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        [py, main],
        cwd=sd,
        creationflags=flags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _STARTED_SERVER_PID = proc.pid
    for _ in range(40):
        time.sleep(0.5)
        if _http_up(ws_url):
            return True
    return False


_UNTAGGED_CODE_FENCE = re.compile(r"```[ \t]*\n(.*?)```", re.DOTALL)


def _guess_code_language(code):
    """为 AI 未标注语言的 fenced code block 推断常见语言，供语法高亮使用。"""
    sample = code.lstrip().lower()
    if sample.startswith(("<!doctype html", "<html", "<div", "<section", "<main")):
        return "html"
    if sample.startswith(("{", "[")):
        return "json"
    if "def " in sample or "import " in sample or "print(" in sample:
        return "python"
    if "function " in sample or "const " in sample or "let " in sample or "=>" in sample:
        return "javascript"
    if "{" in sample and ("color:" in sample or "display:" in sample or "margin:" in sample):
        return "css"
    if sample.startswith(("select ", "insert ", "update ", "create table")):
        return "sql"
    if sample.startswith(("<", "#include", "package ")):
        return "text"
    return "text"


def _prepare_ai_markdown(text):
    """补全无语言 fenced code block 的语言标签，使 AI 代码获得稳定语法高亮。"""
    def add_language(match):
        code = match.group(1)
        return f"```{_guess_code_language(code)}\n{code}```"

    return _UNTAGGED_CODE_FENCE.sub(add_language, text)


def _safe_markdown(text, code_theme="dracula"):
    try:
        return RichMarkdown(_prepare_ai_markdown(text), code_theme=code_theme)
    except Exception:
        return Text(text)


def _valid_hex(color):
    if not isinstance(color, str):
        return False
    c = color.strip().lstrip("#")
    return bool(re.fullmatch(r"[0-9a-fA-F]{3}|[0-9a-fA-F]{6}", c))


def _norm_hex(color):
    c = color.strip()
    if c.startswith("#"):
        c = c[1:]
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return "#" + c.lower()


def _rgb(color):
    c = color.lstrip("#")
    if len(c) == 3:
        c = "".join(ch * 2 for ch in c)
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _ansi_fg(color):
    r, g, b = _rgb(color)
    return f"\033[38;2;{r};{g};{b}m"


# ━━━━━ 主题系统 ━━━━━

THEME_DEFS = {
    "ocean": {
        "label": "淡蓝 · 默认",
        "dark": True,
        "background": "#000000",
        "surface": "#0b1016",
        "panel": "#0e141c",
        "boost": "#131b25",
        "primary": "#6bc2ff",
        "secondary": "#3fa9f5",
        "accent": "#7dd3fc",
        "text": "#b9dcf8",
        "text_muted": "#6d8aa6",
        "user": "#7dd3fc",
        "ai": "#b9dcf8",
        "topbar": "#7dd3fc",
        "topbar_text": "#062a4a",
        "success": "#58d68d",
        "warning": "#e5b94c",
        "error": "#e05252",
        "think": "#d4b06a",
        "think_border": "#6e5a33",
        "tool": "#e5b94c",
        "err_bright": "#ff6b6b",
        "ok": "#58d68d",
        "progress_run": "#6bc2ff",
        "progress_ok": "#58d68d",
        "progress_fail": "#ff6b6b",
        "progress_pending": "#5d7b9c",
        "logo_high": "#d6f1ff",
        "logo_mid": "#6bc2ff",
        "logo_low": "#2a7fd6",
        "logo_base": "#2c4a66",
    },
    "ocean-dark": {
        "label": "深海夜色",
        "dark": True,
        "background": "#0a1626",
        "surface": "#10233c",
        "panel": "#0d1e33",
        "boost": "#14304f",
        "primary": "#4fb4f4",
        "secondary": "#2cc9ee",
        "accent": "#7dd3fc",
        "text": "#d3e9fb",
        "text_muted": "#7fa6c8",
        "user": "#7dd3fc",
        "ai": "#d3e9fb",
        "topbar": "#4fb4f4",
        "topbar_text": "#062a4a",
        "success": "#58d68d",
        "warning": "#e5b94c",
        "error": "#e05252",
        "think": "#c9a84f",
        "think_border": "#7a6432",
        "tool": "#e5b94c",
        "err_bright": "#ff7b72",
        "ok": "#58d68d",
        "progress_run": "#4fb4f4",
        "progress_ok": "#58d68d",
        "progress_fail": "#ff7b72",
        "progress_pending": "#5d7b9c",
        "logo_high": "#e6f9ff",
        "logo_mid": "#6ec4ff",
        "logo_low": "#2a7fd6",
        "logo_base": "#3f6b95",
    },
    "midnight": {
        "label": "午夜星辉",
        "dark": True,
        "background": "#141126",
        "surface": "#1c1738",
        "panel": "#1a1435",
        "boost": "#28204e",
        "primary": "#a78bfa",
        "secondary": "#818cf8",
        "accent": "#c4b5fd",
        "text": "#eae8ff",
        "text_muted": "#9b94c9",
        "user": "#c4b5fd",
        "ai": "#eae8ff",
        "topbar": "#a78bfa",
        "topbar_text": "#191242",
        "success": "#34d399",
        "warning": "#fbbf24",
        "error": "#f87171",
        "think": "#c4aef7",
        "think_border": "#6d59a8",
        "tool": "#f5c56b",
        "err_bright": "#ff8a8a",
        "ok": "#34d399",
        "progress_run": "#a78bfa",
        "progress_ok": "#34d399",
        "progress_fail": "#ff8a8a",
        "progress_pending": "#7d76b3",
        "logo_high": "#e9e2ff",
        "logo_mid": "#a78bfa",
        "logo_low": "#6d55c8",
        "logo_base": "#7a71ab",
    },
    "forest": {
        "label": "清风森林",
        "dark": True,
        "background": "#0a1812",
        "surface": "#10241a",
        "panel": "#0d2118",
        "boost": "#163a28",
        "primary": "#4ade80",
        "secondary": "#34d399",
        "accent": "#86efac",
        "text": "#ddf8e9",
        "text_muted": "#7fb89a",
        "user": "#86efac",
        "ai": "#ddf8e9",
        "topbar": "#4ade80",
        "topbar_text": "#062812",
        "success": "#4ade80",
        "warning": "#fbbf24",
        "error": "#f87171",
        "think": "#a7e08c",
        "think_border": "#5c8a66",
        "tool": "#f5c56b",
        "err_bright": "#ff8a8a",
        "ok": "#4ade80",
        "progress_run": "#4ade80",
        "progress_ok": "#4ade80",
        "progress_fail": "#ff8a8a",
        "progress_pending": "#5c9b78",
        "logo_high": "#e6ffe8",
        "logo_mid": "#6ee7a0",
        "logo_low": "#2f9e5f",
        "logo_base": "#4f8a66",
    },
    "sunset": {
        "label": "暖霞落日",
        "dark": True,
        "background": "#1c1020",
        "surface": "#271425",
        "panel": "#241223",
        "boost": "#331d2f",
        "primary": "#fb7185",
        "secondary": "#f472b6",
        "accent": "#fda4af",
        "text": "#ffe4ee",
        "text_muted": "#c08ba0",
        "user": "#fda4af",
        "ai": "#ffe4ee",
        "topbar": "#fb7185",
        "topbar_text": "#2a0a16",
        "success": "#4ade80",
        "warning": "#fbbf24",
        "error": "#f87171",
        "think": "#f4c889",
        "think_border": "#a06641",
        "tool": "#f5c56b",
        "err_bright": "#ff9a9a",
        "ok": "#4ade80",
        "progress_run": "#fb7185",
        "progress_ok": "#4ade80",
        "progress_fail": "#ff9a9a",
        "progress_pending": "#a9778f",
        "logo_high": "#ffe9ef",
        "logo_mid": "#fb7185",
        "logo_low": "#c24a6e",
        "logo_base": "#9c5f77",
    },
    "noir": {
        "label": "极简灰黑",
        "dark": True,
        "background": "#0a0a0b",
        "surface": "#141416",
        "panel": "#111113",
        "boost": "#1d1d1f",
        "primary": "#d4d4d8",
        "secondary": "#a1a1aa",
        "accent": "#ffffff",
        "text": "#ececee",
        "text_muted": "#9d9da3",
        "user": "#ffffff",
        "ai": "#ececee",
        "topbar": "#d4d4d8",
        "topbar_text": "#0a0a0b",
        "success": "#8be3a5",
        "warning": "#e5b94c",
        "error": "#ee6868",
        "think": "#b9b9c0",
        "think_border": "#5b5b63",
        "tool": "#e5c86b",
        "err_bright": "#ff7b72",
        "ok": "#8be3a5",
        "progress_run": "#d4d4d8",
        "progress_ok": "#8be3a5",
        "progress_fail": "#ff7b72",
        "progress_pending": "#6c6c72",
        "logo_high": "#ffffff",
        "logo_mid": "#c0c0c8",
        "logo_low": "#82828a",
        "logo_base": "#5c5c63",
    },
}

THEME_NODE_KEYS = {
    "accent": "accent",
    "user_color": "user",
    "assistant_color": "ai",
    "think_color": "think",
    "error_color": "err_bright",
    "tool_color": "tool",
    "logo_color": "logo_high",
}

COLOR_KEYS = {
    "accent": "强调色",
    "user_color": "用户消息颜色",
    "assistant_color": "AI 消息颜色",
    "think_color": "思考内容颜色",
    "error_color": "报错颜色",
    "tool_color": "工具调用颜色",
    "logo_color": "LOGO 高亮颜色",
}

THEME_CHOICES = [
    ("ocean", "淡蓝 · 默认"),
    ("ocean-dark", "深海夜色"),
    ("midnight", "午夜星辉"),
    ("forest", "清风森林"),
    ("sunset", "暖霞落日"),
    ("noir", "极简灰黑"),
]

SPEED_CHOICES = [
    ("fast", "轻盈 · 快速"),
    ("normal", "标准 · 流畅"),
    ("slow", "细腻 · 舒缓"),
]

PRESET_COLORS = [
    ("default", "默认（跟随主题）"),
    ("#7dd3fc", "浅蓝 #7dd3fc"),
    ("#2d9ee8", "海蓝 #2d9ee8"),
    ("#1556a8", "深蓝 #1556a8"),
    ("#2cc9e2", "亮青 #2cc9e2"),
    ("#34d399", "翠绿 #34d399"),
    ("#86efac", "薄荷 #86efac"),
    ("#fbbf24", "琥珀 #fbbf24"),
    ("#f97316", "橙红 #f97316"),
    ("#fb7185", "玫红 #fb7185"),
    ("#a78bfa", "紫罗兰 #a78bfa"),
    ("#ec4899", "品红 #ec4899"),
    ("#94a3b8", "银灰 #94a3b8"),
    ("CUSTOM", "自定义颜色…"),
]

SETTINGS_MENU = [
    ("theme", "主题选择"),
    ("color_accent", "强调色"),
    ("color_user_color", "用户消息颜色"),
    ("color_assistant_color", "AI 消息颜色"),
    ("color_think_color", "思考内容颜色"),
    ("color_error_color", "报错颜色"),
    ("color_tool_color", "工具调用颜色"),
    ("color_logo_color", "LOGO 高亮颜色"),
    ("animations", "动画效果"),
    ("speed", "动画速度"),
    ("restore", "恢复默认外观"),
    ("close", "关闭设置"),
]


def resolve_appearance(conf_appearance):
    app = conf_appearance or {}
    theme_name = app.get("theme")
    if theme_name not in THEME_DEFS:
        theme_name = "ocean"
    base = dict(THEME_DEFS[theme_name])
    for cfg_key, node_key in THEME_NODE_KEYS.items():
        val = app.get(cfg_key)
        if val and _valid_hex(val):
            base[node_key] = _norm_hex(val)
    base["dark"] = bool(base.get("dark", True))
    base["theme"] = theme_name
    base["animations"] = bool(app.get("animations", True))
    speed = app.get("animation_speed", "normal")
    if speed not in ("fast", "normal", "slow"):
        speed = "normal"
    base["animation_speed"] = speed
    base["primary"] = base["accent"]
    return base


def build_theme(appearance):
    v = appearance
    return Theme(
        name="dcc",
        primary=v["accent"],
        secondary=v["primary"],
        background=v["background"],
        foreground=v["text"],
        surface=v["surface"],
        panel=v["panel"],
        boost=v["boost"],
        accent=v["accent"],
        success=v["success"],
        warning=v["warning"],
        error=v["error"],
        dark=v["dark"],
        variables={
            "text": v["text"],
            "text-muted": v["text_muted"],
            "primary": v["primary"],
            "secondary": v["secondary"],
            "accent": v["accent"],
            "success": v["success"],
            "warning": v["warning"],
            "error": v["error"],
            "background": v["background"],
            "surface": v["surface"],
            "panel": v["panel"],
            "boost": v["boost"],
            "user": v["user"],
            "ai": v["ai"],
            "think": v["think"],
            "think-border": v["think_border"],
            "tool": v["tool"],
            "err-bright": v["err_bright"],
            "ok": v["ok"],
            "progress-run": v["progress_run"],
            "progress-ok": v["progress_ok"],
            "progress-fail": v["progress_fail"],
            "progress-pending": v["progress_pending"],
            "logo-high": v["logo_high"],
            "logo-mid": v["logo_mid"],
            "logo-low": v["logo_low"],
            "logo-base": v["logo_base"],
            "topbar": v["topbar"],
            "topbar-text": v["topbar_text"],
        },
    )


def format_appearance(config):
    app = config.get("appearance") or DEFAULT_APPEARANCE
    label = THEME_DEFS.get(app.get("theme"), {}).get("label", app.get("theme", "?"))
    lines = [
        "外观配置:",
        f"  主题: {app.get('theme')}（{label}）",
        f"  动画: {'开' if app.get('animations') else '关'}",
        f"  动画速度: {app.get('animation_speed')}",
    ]
    for key, label_name in COLOR_KEYS.items():
        val = app.get(key)
        lines.append(f"  {label_name}: {'默认' if not val else val}")
    return "\n".join(lines)


def handle_config_text(text, server_url):
    parts = text.split()
    cfg = load_config()
    appearance_changed = False

    if len(parts) == 1:
        msg = (
            "客户端配置:\n"
            f"  配置文件: {CONFIG_PATH}\n"
            f"  服务器地址: {cfg.get('server_host')}:{cfg.get('server_port')}\n"
            f"  当前连接: {server_url}\n"
            f"  自动启动本地服务端: {'开' if cfg.get('auto_start_local_server') else '关'}\n"
            + format_appearance(cfg) + "\n"
            "\n服务器修改命令:\n"
            "  /config set host <IP>          设置服务器地址\n"
            "  /config set port <端口>        设置服务器端口\n"
            "  /config set autostart on|off   开关自动启动本地服务端\n"
            "\n外观修改命令:\n"
            "  /config set theme <名称>        切换主题（ocean / ocean-dark / midnight / forest / sunset / noir）\n"
            "  /config set color <项> <色值>   设置颜色项为 #RRGGBB 或 default\n"
            "      颜色项: accent / user / assistant / think / error / tool / logo\n"
            "  /config set animations on|off   开关动画\n"
            "  /config set speed fast|normal|slow  动画速度\n"
            "  /config reset                  全部恢复默认\n"
            "  /config save                   保存当前连接为配置"
        )
        return True, msg, False

    sub = parts[1].lower()
    if sub == "set" and len(parts) >= 4:
        key = parts[2].lower()
        val = parts[3]
        if key == "host":
            cfg["server_host"] = val
        elif key == "port":
            try:
                cfg["server_port"] = int(val)
            except ValueError:
                return True, "[错误] 端口必须是数字", False
        elif key == "autostart":
            cfg["auto_start_local_server"] = val.lower() in ("on", "true", "1", "开")
        elif key == "theme":
            theme_name = val.strip()
            if theme_name not in THEME_DEFS:
                return True, f"[错误] 未知主题: {theme_name}（可用: {', '.join(THEME_DEFS)}）", False
            appearance = cfg.setdefault("appearance", dict(DEFAULT_APPEARANCE))
            appearance["theme"] = theme_name
            appearance_changed = True
        elif key == "animations":
            appearance = cfg.setdefault("appearance", dict(DEFAULT_APPEARANCE))
            appearance["animations"] = val.lower() in ("on", "true", "1", "开")
            appearance_changed = True
        elif key in ("speed", "animspeed"):
            speed_map = {"fast": "fast", "normal": "normal", "slow": "slow",
                         "轻盈": "fast", "标准": "normal", "细腻": "slow"}
            speed = speed_map.get(val.lower())
            if not speed:
                return True, "[错误] 速度可选 fast / normal / slow", False
            appearance = cfg.setdefault("appearance", dict(DEFAULT_APPEARANCE))
            appearance["animation_speed"] = speed
            appearance_changed = True
        elif key == "color" and len(parts) >= 5:
            color_key = parts[3].lower()
            color_val = parts[4]
            short_map = {
                "accent": "accent",
                "user": "user_color",
                "assistant": "assistant_color",
                "think": "think_color",
                "error": "error_color",
                "tool": "tool_color",
                "logo": "logo_color",
            }
            cfg_key = short_map.get(color_key)
            if not cfg_key:
                return True, "[错误] 颜色项可选 accent/user/assistant/think/error/tool/logo", False
            appearance = cfg.setdefault("appearance", dict(DEFAULT_APPEARANCE))
            if color_val.lower() in ("default", "默认", "reset", "none"):
                appearance[cfg_key] = None
            elif _valid_hex(color_val):
                appearance[cfg_key] = _norm_hex(color_val)
            else:
                return True, f"[错误] 无效颜色: {color_val}（用 #RRGGBB 或 default）", False
            appearance_changed = True
        else:
            return True, f"[错误] 未知配置项: {key}（可选 host/port/autostart/theme/color/animations/speed）", False
        if save_config(cfg):
            if key == "color":
                label = {"accent": "强调色", "user": "用户消息", "assistant": "AI 消息",
                         "think": "思考内容", "error": "报错", "tool": "工具调用",
                         "logo": "LOGO"}.get(color_key, color_key)
                return True, f"[已保存] {label} = {parts[4]}（已实时生效）", appearance_changed
            return True, f"[已保存] {key} = {val}（输入 /reconnect 应用服务器新配置）", appearance_changed
        return True, "[错误] 保存失败", appearance_changed

    if sub == "reset":
        if save_config(dict(DEFAULT_CONFIG)):
            return True, "[已恢复默认配置]（外观与服务器均已重置，已实时生效）", True
        return True, "[错误] 保存失败", False

    if sub == "save":
        host = server_url
        host = host.replace("ws://", "").replace("wss://", "").replace("/dscat", "")
        if ":" in host:
            h, p = host.rsplit(":", 1)
            try:
                cfg["server_host"] = h
                cfg["server_port"] = int(p)
            except ValueError:
                pass
        if save_config(cfg):
            return True, f"[已保存当前连接] {cfg['server_host']}:{cfg['server_port']}", False
        return True, "[错误] 保存失败", False

    return True, f"[错误] 未知子命令: {sub}（可用 set / reset / save）", False


# ━━━━━ LOGO 动画组件 ━━━━━

class LogoAccent(Static):
    """DeepSeek LOGO，带从左到右循环扫过的灯光高亮效果。"""

    def __init__(self):
        super().__init__("")
        self._logo_lines = LOGO.split("\n")
        self._width = max(len(l) for l in self._logo_lines)
        self._light = -2.5
        self._dir = 1
        self._speed = 0.6
        self._timer = None

    def _palette(self):
        return self.app.appearance_palette()

    def on_mount(self):
        self._timer = self.set_interval(1 / 20, self._tick)

    def on_unmount(self):
        if self._timer is not None:
            try:
                self._timer.stop()
            except Exception:
                pass

    def _tick(self):
        if not getattr(self.app, "animations_enabled", True):
            return
        self._light += self._dir * self._speed
        if self._light >= self._width + 3:
            self._light = self._width + 3
            self._dir = -1
        elif self._light <= -3:
            self._light = -3
            self._dir = 1
        self.refresh()

    def render(self):
        from rich.console import Group as RGroup
        p = self._palette()
        hi, mid, low, base = p["logo_high"], p["logo_mid"], p["logo_low"], p["logo_base"]
        rendered = []
        for line in self._logo_lines:
            t = Text()
            for x, ch in enumerate(line):
                if ch == " ":
                    t.append(" ")
                    continue
                d = abs(x - self._light)
                if d < 1.2:
                    t.append(ch, style=f"bold {hi}")
                elif d < 3.2:
                    t.append(ch, style=mid)
                elif d < 6.0:
                    t.append(ch, style=low)
                else:
                    t.append(ch, style=base)
            rendered.append(t)
        return RGroup(*rendered)


# ━━━━━ 消息组件 ━━━━━

class RecolorMixin:
    def rebuild(self):
        method = getattr(self, "_rebuilt", None)
        if method is not None:
            method()


class UserMessage(Static, RecolorMixin):
    def __init__(self, text):
        super().__init__()
        self.text = text
        self.ts = _now()
        self._rebuilt()

    def _palette(self):
        return self.app.appearance_palette()

    def _rebuilt(self):
        p = self._palette()
        self.update(Group(
            Text(f" 你  {self.ts}", style=f"bold {p['user']}"),
            Text(self.text),
            Text(""),
        ))

    def get_selection(self, selection):
        return selection.extract("\n" + self.text), "\n"


class AiMessage(Static, RecolorMixin):
    SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def __init__(self):
        super().__init__("")
        self.full_text = ""
        self.ts = _now()
        self._spin_index = 0
        self._spinner = None
        self.finished = False

    def on_mount(self):
        if not self.full_text and not self.finished:
            self._start_spinner()

    def on_unmount(self):
        self._stop_spinner()

    def _start_spinner(self):
        if self._spinner is not None:
            return
        self._spinner = self.set_interval(1 / 8, self._spin_tick)

    def _stop_spinner(self):
        if self._spinner is not None:
            try:
                self._spinner.stop()
            except Exception:
                pass
            self._spinner = None

    def _spin_tick(self):
        if self.full_text or self.finished:
            self._stop_spinner()
            return
        self._spin_index = (self._spin_index + 1) % len(self.SPINNER)
        self.refresh()

    def append_chunk(self, chunk):
        self.full_text += chunk
        self._stop_spinner()
        self._rebuilt()

    def finish(self):
        self.finished = True
        self._stop_spinner()
        self._rebuilt()

    def _palette(self):
        return self.app.appearance_palette()

    def on_click(self, event=None):
        offset = getattr(event, "offset", None)
        if self.full_text and offset is not None and offset.y == 0 and offset.x <= 3:
            self.app.copy_to_clipboard(self.full_text)
            self.app._set_status("已复制完整 AI 回复")
            event.stop()

    def get_selection(self, selection):
        return selection.extract("\n" + self.full_text), "\n"

    def _rebuilt(self):
        p = self._palette()
        header = Text(f" ⧉  {APP_NAME}  {self.ts}", style=f"bold {p['ai']}")
        self.tooltip = "点击左侧 ⧉ 复制完整回复"
        if self.full_text:
            # Dracula 接近 JetBrains/PyCharm 的深色编辑器配色；浅色主题保留友好配色。
            code_theme = "dracula" if p["dark"] else "friendly"
            body = _safe_markdown(self.full_text, code_theme)
        elif self.finished:
            body = Text("（空回复）", style="dim italic")
        else:
            frame = self.SPINNER[self._spin_index % len(self.SPINNER)]
            body = Text(f"{frame} 思考中…", style=f"italic {p['think']}")
        self.update(Group(header, body, Text("")))


class ThinkToggle(Static):
    def on_click(self, event=None):
        owner = self.parent
        if isinstance(owner, ThinkBlock):
            owner.toggle()
        if event is not None:
            event.stop()


class ThinkBlock(Container, RecolorMixin):
    STAR_FRAMES = ["✦", "✧", "⋆", "✧"]

    def __init__(self):
        super().__init__()
        self.full_text = ""
        self.ts = _now()
        self.collapsed = True
        self.finished = False
        self._star_index = 0
        self._star_timer = None

    def compose(self):
        yield ThinkToggle("", classes="think-toggle")
        yield Static("", classes="think-body")

    def on_mount(self):
        self._rebuilt()
        self._star_timer = self.set_interval(0.18, self._star_tick)

    def on_unmount(self):
        self._stop_star()

    def _stop_star(self):
        if self._star_timer is not None:
            try:
                self._star_timer.stop()
            except Exception:
                pass
            self._star_timer = None

    def _star_tick(self):
        if self.finished:
            self._stop_star()
            return
        if not getattr(self.app, "animations_enabled", True):
            return
        self._star_index = (self._star_index + 1) % len(self.STAR_FRAMES)
        self._rebuilt()

    def append(self, chunk):
        self.full_text += chunk
        self._rebuilt()

    def finish(self):
        self.finished = True
        self.collapsed = bool(self.full_text)
        self._stop_star()
        self._rebuilt()

    def toggle(self):
        if not self.full_text:
            return
        self.collapsed = not self.collapsed
        self._rebuilt()

    def _palette(self):
        return self.app.appearance_palette()

    def _rebuilt(self):
        p = self._palette()
        star = self.STAR_FRAMES[self._star_index] if not self.finished else "✦"
        arrow = "›" if self.collapsed else "⌄"
        state = "已完成" if self.finished else "正在思考"
        try:
            toggle = self.query_one(".think-toggle", ThinkToggle)
            body = self.query_one(".think-body", Static)
        except Exception:
            return
        toggle.update(Text(f"{star} Thinking  {arrow} {state}  {self.ts}", style=f"italic {p['think']}"))
        toggle.tooltip = "点击展开思考过程" if self.collapsed else "点击收起思考过程"
        body.update(Text(self.full_text, style=f"italic {p['think']}") if self.full_text else Text("…", style="dim"))
        self.set_class(self.collapsed, "-collapsed")


class ToolCallWidget(Static, RecolorMixin):
    def __init__(self, tool_name, args):
        super().__init__("")
        self.tool_name = tool_name
        self.args = args
        self.result = None
        self.ts = _now()
        self._done = False

    def set_result(self, result):
        self.result = result
        self._done = True
        self._rebuilt()

    def _palette(self):
        return self.app.appearance_palette()

    def _copy_text(self):
        args = json.dumps(self.args, ensure_ascii=False) if self.args else ""
        parts = [self.tool_name]
        if args:
            parts.append(args)
        if self.result is not None:
            parts.append(str(self.result))
        return "\n".join(parts)

    def on_click(self, event=None):
        offset = getattr(event, "offset", None)
        if offset is not None and offset.x <= 4 and offset.y <= 2:
            self.app.copy_to_clipboard(self._copy_text())
            self.app._set_status("已复制完整工具信息")
            event.stop()

    def get_selection(self, selection):
        return selection.extract("\n" + self._copy_text()), "\n"

    def _rebuilt(self):
        p = self._palette()
        args_str = json.dumps(self.args, ensure_ascii=False) if self.args else ""
        lines = [Text(f" ⧉ {self.tool_name}  {self.ts}", style=f"bold {p['tool']}")]
        if args_str:
            lines.append(Text(args_str, style="dim"))
        if self.result is not None:
            lines.append(Text("─" * 48, style="dim"))
            lines.append(Text(str(self.result), style=p["text"]))
        spinner = "" if self._done else " ⏳"
        self.update(Panel(
            Group(*lines),
            border_style=p["tool"],
            title=f"工具调用{spinner}",
            title_align="left",
        ))
        self.tooltip = "点击左侧 ⧉ 复制完整工具信息"


class SystemNote(Static):
    def __init__(self, text, tone="dim"):
        super().__init__("")
        self.text = text
        self.ts = _now()
        self.tone = tone

    def _palette(self):
        return self.app.appearance_palette()

    def render(self):
        p = self._palette()
        if self.tone == "error":
            style = f"bold {p['err_bright']}"
        elif self.tone == "ok":
            style = p["ok"]
        elif self.tone == "warn":
            style = p["tool"]
        elif self.tone == "info":
            style = p["accent"]
        else:
            style = "dim"
        return Text(f" [{self.ts}] {self.text}", style=style)


class StepProgress(Static):
    def __init__(self, **kwargs):
        super().__init__("", **kwargs)
        self._steps = {}

    def set_step(self, step, title, status, detail=""):
        self._steps[step] = {"title": title, "status": status, "detail": detail}
        self.visible = True
        self.refresh()

    def clear(self):
        self._steps.clear()
        self.visible = False
        self.refresh()

    def _palette(self):
        return self.app.appearance_palette()

    def render(self):
        p = self._palette()
        if not self._steps:
            return Text("")
        t = Table.grid(padding=(0, 1), expand=True)
        t.add_column(ratio=1)
        for num in sorted(self._steps):
            s = self._steps[num]
            if s["status"] == "started":
                icon, style = "●", f"bold {p['progress_run']}"
            elif s["status"] == "completed":
                icon, style = "✓", p["progress_ok"]
            elif s["status"] == "failed":
                icon, style = "✗", f"bold {p['progress_fail']}"
            else:
                icon, style = "○", "dim"
            label = Text(f"{icon} 步骤{num}: {s['title']}", style=style)
            if s.get("detail"):
                label.append(Text(f" — {s['detail']}", style="dim"))
            t.add_row(label)
        return Panel(t, title="任务清单", title_align="left", border_style=p["progress_run"], padding=(0, 1))


# ━━━━━ 输入框 ━━━━━

class ChatInput(TextArea):
    BINDINGS = [
        Binding("enter", "submit", "发送", show=False, priority=True),
        Binding("shift+enter", "newline", "换行", show=False, priority=True),
    ]

    def on_mount(self):
        self._ime_composing = False
        self._ime_timer = None

    def on_unmount(self):
        self._end_ime_composition()

    def _begin_ime_composition(self):
        self._ime_composing = True
        self.show_cursor = False
        if self._ime_timer is not None:
            try:
                self._ime_timer.stop()
            except Exception:
                pass
        self._ime_timer = self.set_timer(5.0, self._end_ime_composition)

    def _end_ime_composition(self):
        self._ime_composing = False
        self.show_cursor = True
        if self._ime_timer is not None:
            try:
                self._ime_timer.stop()
            except Exception:
                pass
            self._ime_timer = None

    def _detect_ime_preedit(self, previous_text):
        if self.has_focus and self.text == previous_text:
            self._begin_ime_composition()

    def _after_key(self, previous_text, event):
        character = getattr(event, "character", None)
        if character and character.isprintable():
            self.call_after_refresh(self._detect_ime_preedit, previous_text)
        elif self._ime_composing and event.key in ("escape", "enter", "left", "right", "up", "down"):
            self._end_ime_composition()

    def action_submit(self):
        if self.app._completion_visible():
            self.app._accept_completion(send=True)
            return
        self.app.submit_input()

    def action_newline(self):
        if self.app._completion_visible():
            return
        try:
            self.insert("\n")
        except Exception:
            super().action_newline()

    async def _on_key(self, event):
        try:
            if self.app._completion_visible():
                key = event.key
                if key in ("up", "left"):
                    self.app._cycle_completion(-1)
                    event.prevent_default()
                    event.stop()
                    return
                elif key in ("down", "right"):
                    self.app._cycle_completion(1)
                    event.prevent_default()
                    event.stop()
                    return
                elif key == "tab":
                    self.app._accept_completion(send=False)
                    event.prevent_default()
                    event.stop()
                    return
                elif key == "escape":
                    self.app._hide_completion()
                    event.prevent_default()
                    event.stop()
                    return
        except Exception:
            self.app._hide_completion()
            event.prevent_default()
            event.stop()
            return
        previous_text = self.text
        await super()._on_key(event)
        self._after_key(previous_text, event)

    def on_text_area_changed(self, event):
        if self._ime_composing:
            self._end_ime_composition()
        self.app._on_input_changed(self.text)

    def on_focus(self, event=None):
        self.app._set_composer_focus(True)

    def on_blur(self, event=None):
        self.app._set_composer_focus(False)


# ━━━━━ 权限弹窗 ━━━━━

class PermissionScreen(ModalScreen):
    CSS = """
    PermissionScreen {
        align: center bottom;
    }
    #perm-dialog {
        width: 72;
        height: auto;
        padding: 1 2;
        border: solid $warning;
        background: $surface;
    }
    #perm-title {
        text-style: bold;
        color: $warning;
        margin-bottom: 1;
    }
    #perm-path {
        color: $accent;
        text-style: bold;
        margin-bottom: 1;
    }
    #perm-options {
        height: auto;
        max-height: 10;
        border: none;
        background: transparent;
        margin-bottom: 0;
    }
    #perm-options > .option-list--option {
        padding: 0 1;
    }
    #perm-hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "deny", "拒绝", show=False, priority=True),
    ]

    def __init__(self, path):
        super().__init__()
        self.path = path

    def on_mount(self):
        self.query_one("#perm-options", OptionList).focus()
        self.set_timer(60, self._timeout)

    def _timeout(self):
        self.dismiss("deny")

    def compose(self):
        yield Container(
            Static("⚠ 权限请求", id="perm-title"),
            Static("AI 想要访问以下路径:", id="perm-label"),
            Static(self.path, id="perm-path"),
            OptionList(
                Option(" 本次允许", id="allow_once"),
                Option(" 始终允许", id="allow_always"),
                Option(" 拒绝", id="deny"),
                id="perm-options",
            ),
            Static("↑↓ 选择  Enter 确认  Esc 拒绝", id="perm-hint"),
            id="perm-dialog",
        )

    def action_deny(self):
        self.dismiss("deny")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.dismiss(event.option.id)


# ━━━━━ 颜色输入弹窗 ━━━━━

class HexInputScreen(ModalScreen):
    CSS = """
    HexInputScreen {
        align: center middle;
    }
    #hex-dialog {
        width: 48;
        height: auto;
        padding: 1 2;
        border: solid $accent;
        background: $surface;
    }
    #hex-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #hex-input {
        height: 3;
        background: $panel;
    }
    #hex-hint {
        color: $text-muted;
        margin-top: 1;
    }
    #hex-error {
        color: $err-bright;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("enter", "submit", "确认", show=False, priority=True),
        Binding("escape", "cancel", "取消", show=False, priority=True),
    ]

    def __init__(self, title):
        super().__init__()
        self.title_text = title

    def on_mount(self):
        self.query_one("#hex-input", TextArea).focus()

    def compose(self):
        yield Container(
            Static(f"自定义{self.title_text}", id="hex-title"),
            Static("输入 #RRGGBB 颜色值（如 #38bdf8），留空则恢复默认：", id="hex-label"),
            TextArea("", id="hex-input"),
            Static("Enter 确认    Esc 取消", id="hex-hint"),
            Static("", id="hex-error"),
            id="hex-dialog",
        )

    def action_cancel(self):
        self.dismiss(None)

    def action_submit(self):
        value = self.query_one("#hex-input", TextArea).text.strip()
        if not value:
            self.dismiss(None)
            return
        if not _valid_hex(value):
            self.query_one("#hex-error", Static).update(" 无效的颜色值，请使用 #RRGGBB 格式")
            return
        self.dismiss(_norm_hex(value))


# ━━━━━ 设置弹窗 ━━━━━

class SettingsScreen(ModalScreen):
    CSS = """
    SettingsScreen {
        align: center middle;
    }
    #settings-dialog {
        width: 78;
        height: 36;
        padding: 1 2;
        border: solid $accent;
        background: $surface;
    }
    #settings-header {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #settings-hint {
        color: $text-muted;
        margin-bottom: 1;
    }
    #settings-menu-wrap, #settings-picker-wrap {
        height: 13;
        border: none;
        margin-bottom: 1;
    }
    #settings-menu, #settings-picker {
        height: 100%;
        border: none;
        background: transparent;
    }
    #settings-preview {
        height: 11;
        padding: 1;
        border: round $panel;
        background: $panel;
        margin-bottom: 1;
        overflow-y: auto;
    }
    #settings-toast {
        height: 1;
        color: $success;
    }
    """

    BINDINGS = [
        Binding("escape", "quit_settings", "关闭", show=False),
    ]

    def __init__(self):
        super().__init__()
        self._in_picker = False
        self._picker = None

    def compose(self):
        yield Container(
            Static("⚙ 外观与设置", id="settings-header"),
            Static("  ↑↓ 移动选择，Enter 确认，Esc 返回或关闭", id="settings-hint"),
            Container(OptionList(id="settings-menu"), id="settings-menu-wrap"),
            Container(OptionList(id="settings-picker"), id="settings-picker-wrap", classes="hidden"),
            Static("", id="settings-preview"),
            Static("", id="settings-toast"),
            id="settings-dialog",
        )

    def on_mount(self):
        self._fill_menu()
        self._refresh_preview()
        menu = self.query_one("#settings-menu", OptionList)
        menu.focus()
        try:
            menu.highlighted = 0
        except Exception:
            pass

    def _appearance(self):
        return self.app.appearance_settings

    def _fill_menu(self):
        ol = self.query_one("#settings-menu", OptionList)
        ol.clear_options()
        appearance = self._appearance()
        theme_label = THEME_DEFS.get(appearance.get("theme"), {}).get("label", appearance.get("theme", "?"))
        for key, label in SETTINGS_MENU:
            detail = ""
            if key == "theme":
                detail = f"　当前：{theme_label}"
            elif key.startswith("color_"):
                ck = key[6:]
                val = appearance.get(ck)
                detail = f"　当前：{'默认' if not val else val}"
            elif key == "animations":
                detail = "　当前：" + ("开" if appearance.get("animations", True) else "关")
            elif key == "speed":
                speed_label = dict(SPEED_CHOICES).get(appearance.get("animation_speed"), "标准")
                detail = f"　当前：{speed_label}"
            ol.add_option(Option(f"  {label}{detail}", id=key))

    def _fill_picker(self):
        ol = self.query_one("#settings-picker", OptionList)
        ol.clear_options()
        key = self._picker
        if key == "theme":
            for name, label in THEME_CHOICES:
                ol.add_option(Option(f"  {label}", id=f"theme:{name}"))
        elif key == "animations":
            ol.add_option(Option("  开启动画", id="anim:on"))
            ol.add_option(Option("  关闭动画", id="anim:off"))
        elif key == "speed":
            for name, label in SPEED_CHOICES:
                ol.add_option(Option(f"  {label}", id=f"speed:{name}"))
        elif key and key.startswith("color_"):
            ck = key[6:]
            current = self.app.appearance_settings.get(ck)
            for value, label in PRESET_COLORS:
                mark = ""
                if value == "CUSTOM":
                    pass
                elif value == "default":
                    if not current:
                        mark = "　← 当前"
                elif current is not None and str(current).lower() == value:
                    mark = "　← 当前"
                ol.add_option(Option(f"  {label + mark}", id=f"color:{ck}:{value}"))
        ol.add_option(Option("  ← 返回设置菜单", id="back"))

    def _refresh_preview(self):
        try:
            preview = self.query_one("#settings-preview")
        except Exception:
            return
        p = self.app.appearance_palette()
        g = Group(
            Text(f"  你  09:41", style=f"bold {p['user']}"),
            Text("  帮我写一个深色模式切换"),
            Text(""),
            Text(f"  {APP_NAME}  09:41", style=f"bold {p['ai']}"),
            Text("  好的！你可以这样实现："),
            Text(f"  ⠋ 思考中…", style=f"italic {p['think']}"),
            Panel(Text("  先分析依赖，再逐步实现……", style=f"italic {p['think']}"),
                  border_style=p["think_border"], title="思考过程", title_align="left"),
            Panel(Text(f"  read_file  09:42", style=f"bold {p['tool']}"),
                  border_style=p["tool"], title="工具调用 ⏳", title_align="left"),
            Text(f"  ✓ 全部完成　✗ 有一个步骤失败", style=p["ok"]),
            Text(f"  [09:43] 连接失败: 服务端未响应", style=f"bold {p['err_bright']}"),
        )
        preview.update(g)
        preview.refresh()

    def _toast(self, msg):
        try:
            self.query_one("#settings-toast", Static).update("  " + msg)
        except Exception:
            pass

    def _show_menu(self):
        self.query_one("#settings-menu-wrap").remove_class("hidden")
        self.query_one("#settings-picker-wrap").add_class("hidden")
        self._fill_menu()
        menu = self.query_one("#settings-menu", OptionList)
        menu.focus()
        try:
            menu.highlighted = 0
        except Exception:
            pass

    def _highlight_current_picker(self):
        ol = self.query_one("#settings-picker", OptionList)
        appearance = self._appearance() or {}
        target = None
        key = self._picker
        if key == "theme":
            target = f"theme:{appearance.get('theme', 'ocean')}"
        elif key == "animations":
            target = "anim:on" if appearance.get("animations", True) else "anim:off"
        elif key == "speed":
            target = f"speed:{appearance.get('animation_speed', 'normal')}"
        elif key and key.startswith("color_"):
            ck = key[6:]
            cur = appearance.get(ck)
            if cur:
                target = f"color:{ck}:{str(cur).lower()}"
            else:
                target = f"color:{ck}:default"
        if target:
            for i in range(len(ol.options)):
                try:
                    if ol.get_option_at_index(i).id == target:
                        ol.highlighted = i
                        return
                except Exception:
                    pass
        try:
            ol.highlighted = 0
        except Exception:
            pass

    def _show_picker(self):
        self.query_one("#settings-menu-wrap").add_class("hidden")
        self.query_one("#settings-picker-wrap").remove_class("hidden")
        self._fill_picker()
        ol = self.query_one("#settings-picker", OptionList)
        ol.focus()
        self._highlight_current_picker()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        if event.option.id is None:
            return
        oid = event.option.id
        if not self._in_picker:
            if oid == "close":
                self.action_quit_settings()
                return
            if oid == "restore":
                self.app.appearance_restore_default()
                self._fill_menu()
                self._refresh_preview()
                self._toast("已恢复默认外观")
                return
            self._picker = oid
            self._in_picker = True
            self._show_picker()
            return
        if oid == "back":
            self._in_picker = False
            self._show_menu()
            return
        self._apply_choice(oid)

    def _apply_choice(self, oid):
        if oid.startswith("theme:"):
            name = oid.split(":", 1)[1]
            self.app.appearance_set_theme(name)
            self._toast(f"主题已切换为 {THEME_DEFS.get(name, {}).get('label', name)}")
        elif oid.startswith("anim:"):
            on = oid.split(":", 1)[1] == "on"
            self.app.appearance_set_animations(on)
            self._toast("动画已开启" if on else "动画已关闭")
        elif oid.startswith("speed:"):
            name = oid.split(":", 1)[1]
            self.app.appearance_set_speed(name)
            self._toast(f"动画速度已设为 {dict(SPEED_CHOICES).get(name, name)}")
        elif oid.startswith("color:"):
            _, ck, value = oid.split(":", 2)
            if value == "CUSTOM":
                self._prompt_custom(ck)
                return
            if value == "default":
                self.app.appearance_set_color(ck, None)
            else:
                self.app.appearance_set_color(ck, value)
            label = COLOR_KEYS.get(ck, ck)
            self._toast(f"{label} 已更新")
        self._in_picker = False
        self._show_menu()
        self._refresh_preview()

    def _prompt_custom(self, ck):
        label = COLOR_KEYS.get(ck, ck)

        def on_value(value):
            if value:
                self.app.appearance_set_color(ck, value)
                self._toast(f"{label} 已更新为 {value}")
            self._fill_picker()
            self.query_one("#settings-picker", OptionList).focus()
            self._refresh_preview()

        self.app.push_screen(HexInputScreen(label), callback=on_value)

    def action_quit_settings(self):
        self.dismiss()


# ━━━━━ 主应用 ━━━━━

class DcCatApp(App):
    CSS = """
    Screen {
        layers: base above;
        layout: vertical;
    }
    #main-area {
        height: 1fr;
        layout: horizontal;
        layer: base;
    }
    #conversation-pane {
        width: 1fr;
        height: 1fr;
        layout: vertical;
    }
    LogoAccent {
        height: 6;
        content-align: center top;
        padding: 0 1;
        layer: base;
    }
    #sidebar {
        width: 34;
        height: 1fr;
        padding: 0 1;
        background: $surface;
        border-left: solid $panel;
        overflow-y: auto;
    }
    #sidebar-brand {
        height: 1;
        color: $accent;
        text-style: bold;
        border-bottom: solid $panel;
        padding: 0;
    }
    #sidebar-connection, #sidebar-project, #sidebar-sync, #sidebar-model, #sidebar-permission, #sidebar-hint {
        height: auto;
        padding: 0;
        border-bottom: solid $panel;
    }
    #sidebar-connection {
        height: 1;
    }
    #sidebar-hint {
        color: $text-muted;
        border-bottom: none;
    }
    #chat {
        height: 1fr;
        padding: 0 1;
        scrollbar-size: 1 1;
        layer: base;
    }
    #input-wrap {
        height: auto;
        max-height: 20;
        padding: 0 1 1 1;
        layer: base;
    }
    #composer {
        height: auto;
        max-height: 18;
        border: round $panel;
        background: $surface;
    }
    #composer.-focused {
        border: round $accent;
    }
    #composer.-busy {
        border: round $tool;
    }
    #composer-row {
        height: auto;
        layout: horizontal;
    }
    #composer-prompt {
        width: 3;
        height: 1;
        content-align: center middle;
        color: $accent;
        text-style: bold;
    }
    ChatInput {
        height: auto;
        min-height: 2;
        max-height: 12;
        border: none;
        background: transparent;
        color: $text;
    }
    ChatInput:focus {
        background: transparent;
    }
    #composer-meta {
        height: 1;
        padding: 0 1 0 3;
        color: $text-muted;
        background: $panel;
    }
    #statusbar {
        height: 1;
        background: $boost;
        color: $text-muted;
        padding: 0 1;
        layer: base;
    }
    #completion-list {
        width: 40;
        max-height: 12;
        margin-bottom: 1;
        border: solid $accent;
        background: $surface;
        padding: 0 1;
        display: none;
    }
    #completion-list.-visible {
        display: block;
    }
    #completion-list > .option-list--option {
        padding: 0 2;
    }
    #step-progress {
        height: auto;
        max-height: 8;
        margin: 0 1;
        layer: base;
    }
    ThinkBlock {
        height: auto;
        border: round $think-border;
        padding: 0 1;
    }
    ThinkBlock > .think-toggle {
        height: 1;
        color: $think;
    }
    ThinkBlock > .think-body {
        height: auto;
        color: $think;
    }
    ThinkBlock.-collapsed > .think-body {
        display: none;
    }
    .hidden {
        display: none;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit", "退出", show=False),
        Binding("ctrl+d", "quit", "退出", show=False),
    ]

    connected = reactive(False)
    busy = reactive(False)

    def __init__(self, server_url, workdir):
        super().__init__()
        self.server_url = server_url
        self.workdir = workdir
        self.ws = None
        self.model_name = ""
        self.server_started_by_us = False
        self._current_assistant = None
        self._current_think = None
        self._tool_queue = []
        self._input_history = []
        self._pending_cycles = 0
        self._scroll_pending = False
        self._streaming = False
        self.auto_approve_tools = False
        self._activity_timer = None
        self._activity_frame = 0
        self._activity_override = None
        self._sync_status = "等待服务端同步"
        self._last_status = "就绪"
        self._appearance = resolve_appearance(load_config().get("appearance") or {})
        self.animations_enabled = True
        self.animation_speed = "normal"
        self.conversations = load_conversations()
        self.conversation = new_conversation()
        self.conversations.insert(0, self.conversation)
        save_conversations(self.conversations)

    # ── 外观 ──

    @property
    def appearance_settings(self):
        return load_config().get("appearance") or dict(DEFAULT_APPEARANCE)

    def get_css_variables(self):
        variables = super().get_css_variables()
        fallback = resolve_appearance(DEFAULT_APPEARANCE)
        for key in ("topbar", "topbar_text", "user", "ai", "think", "think_border",
                    "tool", "err_bright", "ok", "progress_run", "progress_ok",
                    "progress_fail", "progress_pending", "logo_high", "logo_mid",
                    "logo_low", "logo_base"):
            var = key.replace("_", "-")
            if var not in variables:
                variables[var] = fallback[key]
        return variables

    def appearance_palette(self):
        return self._appearance

    def appearance_color_palette(self):
        return self._appearance

    def anim_duration(self, base):
        speed = getattr(self, "animation_speed", "normal")
        if speed == "fast":
            return base * 0.45
        if speed == "slow":
            return base * 1.8
        return base

    def apply_appearance(self, animate=True):
        cfg = load_config()
        appearance = resolve_appearance(cfg.get("appearance") or {})
        self._appearance = appearance
        self.animations_enabled = bool(appearance["animations"])
        self.animation_speed = appearance["animation_speed"]
        try:
            self.register_theme(build_theme(appearance))
            self.theme = "dcc"
        except Exception:
            pass
        self._recolor_chat()
        self._render_sidebar()
        if animate and self.animations_enabled:
            self._animate_transition(self.anim_duration(0.7))

    def appearance_set_theme(self, name):
        if name not in THEME_DEFS:
            return
        cfg = load_config()
        cfg.setdefault("appearance", dict(DEFAULT_APPEARANCE))["theme"] = name
        save_config(cfg)
        self.apply_appearance(animate=True)

    def appearance_set_animations(self, on):
        cfg = load_config()
        cfg.setdefault("appearance", dict(DEFAULT_APPEARANCE))["animations"] = bool(on)
        save_config(cfg)
        self.apply_appearance(animate=False)

    def appearance_set_speed(self, speed):
        cfg = load_config()
        cfg.setdefault("appearance", dict(DEFAULT_APPEARANCE))["animation_speed"] = speed
        save_config(cfg)
        self.apply_appearance(animate=False)

    def appearance_set_color(self, cfg_key, value):
        cfg = load_config()
        appearance = cfg.setdefault("appearance", dict(DEFAULT_APPEARANCE))
        if value is None:
            appearance[cfg_key] = None
        else:
            appearance[cfg_key] = value
        save_config(cfg)
        self.apply_appearance(animate=True)

    def appearance_restore_default(self):
        cfg = load_config()
        cfg["appearance"] = dict(DEFAULT_APPEARANCE)
        save_config(cfg)
        self.apply_appearance(animate=True)

    def _recolor_chat(self):
        try:
            chat = self.query_one("#chat", VerticalScroll)
        except Exception:
            return
        for child in list(chat.children):
            rebuild = getattr(child, "rebuild", None)
            if rebuild is not None:
                try:
                    rebuild()
                except Exception:
                    pass
        try:
            self.query_one(ChatInput).refresh()
        except Exception:
            pass

    def _animate_transition(self, duration):
        p = self._appearance
        try:
            top = self.query_one("#topbar", Static)
            top.styles.animate("background", Color(p["topbar"]), duration=duration)
            top.styles.animate("color", Color(p["topbar_text"]), duration=duration)
        except Exception:
            pass
        try:
            sb = self.query_one("#statusbar", Static)
            sb.styles.animate("background", Color(p["boost"]), duration=duration)
        except Exception:
            pass
        try:
            inp = self.query_one(ChatInput)
            inp.styles.animate("background", Color(p["surface"]), duration=duration)
        except Exception:
            pass
        try:
            self.screen.styles.animate("background", Color(p["background"]), duration=duration)
        except Exception:
            pass

    def _animate_in(self, widget):
        if not self.animations_enabled:
            return
        dur = self.anim_duration(0.35)
        try:
            widget.styles.opacity = 0.0
        except Exception:
            return
        try:
            widget.styles.animate("opacity", 1.0, duration=dur, easing="out_cubic")
        except Exception:
            try:
                widget.styles.opacity = 1.0
            except Exception:
                pass

    # ── 界面组装 ──

    def compose(self):
        with Container(id="main-area"):
            with Container(id="conversation-pane"):
                yield LogoAccent()
                yield VerticalScroll(id="chat")
                yield StepProgress(id="step-progress")
                with Container(id="input-wrap"):
                    yield OptionList(id="completion-list")
                    with Container(id="composer"):
                        with Container(id="composer-row"):
                            yield Static("›", id="composer-prompt")
                            yield ChatInput(
                                id="input",
                            )
                        yield Static("", id="composer-meta")
            with Container(id="sidebar"):
                yield Static("", id="sidebar-brand")
                yield Static("", id="sidebar-connection")
                yield Static("", id="sidebar-project")
                yield Static("", id="sidebar-model")
                yield Static("", id="sidebar-permission")
                yield Static("", id="sidebar-sync")
                yield Static("输入消息开始对话\n输入 / 查看命令", id="sidebar-hint")
        yield Static("", id="statusbar")

    def on_mount(self):
        self.apply_appearance(animate=False)
        self.title = f"{APP_NAME} Code"
        self.sub_title = f"v{VERSION}"
        self._set_status("就绪")
        self._render_composer_meta()
        self._render_sidebar()
        self.query_one(ChatInput).focus()
        self._connect_loop()

    # ── 右侧信息栏 ──

    def _render_sidebar(self):
        try:
            brand = self.query_one("#sidebar-brand", Static)
            connection = self.query_one("#sidebar-connection", Static)
            project = self.query_one("#sidebar-project", Static)
            model = self.query_one("#sidebar-model", Static)
            permission = self.query_one("#sidebar-permission", Static)
            sync = self.query_one("#sidebar-sync", Static)
        except Exception:
            return
        p = self._appearance
        status = "已连接" if self.connected else "未连接"
        status_color = p["ok"] if self.connected else p["err_bright"]
        folder = os.path.basename(os.path.normpath(self.workdir)) or self.workdir
        brand.update(Text(APP_NAME, style=f"bold {p['accent']}"))
        connection.update(Text.assemble(
            Text("● ", style=status_color),
            Text(status, style=f"bold {status_color}"),
            Text(f"  ·  v{VERSION}", style="dim"),
        ))
        project.update(Text.assemble(
            Text("工作目录\n", style=f"bold {p['text']}"),
            Text(f"{self.workdir}\n", style=p["accent"]),
            Text("服务器\n", style=f"bold {p['text']}"),
            Text(self.server_url, style=p["text_muted"]),
        ))
        if self.model_name:
            model.visible = True
            model.update(Text.assemble(
                Text("模型\n", style=f"bold {p['text']}"),
                Text(self.model_name, style=p["secondary"]),
            ))
        else:
            model.visible = False
            model.update(Text(""))
        approve_enabled = getattr(self, "auto_approve_tools", False)
        permission_color = p["ok"] if approve_enabled else p["text_muted"]
        permission.update(Text.assemble(
            Text("工具审核\n", style=f"bold {p['text']}"),
            Text("当前对话自动通过" if approve_enabled else "每次请求确认", style=permission_color),
        ))
        sync_text = getattr(self, "_sync_status", "等待服务端同步")
        sync.update(Text.assemble(
            Text("状态\n", style=f"bold {p['text']}"),
            Text(sync_text, style=p["text_muted"]),
        ))

    def _set_status(self, msg):
        self._last_status = msg
        try:
            bar = self.query_one("#statusbar", Static)
        except Exception:
            pass
        else:
            p = self._appearance
            bar.update(Text(f" {msg}", style=p["text_muted"]))
        self._render_sidebar()

    def _set_composer_focus(self, focused):
        try:
            composer = self.query_one("#composer", Container)
            composer.set_class(focused, "-focused")
        except Exception:
            pass

    def _start_activity_animation(self):
        self._activity_frame = 0
        self._activity_override = None
        if self._activity_timer is None:
            self._activity_timer = self.set_interval(0.14, self._tick_activity_animation)

    def _stop_activity_animation(self):
        self._activity_override = None
        if self._activity_timer is not None:
            try:
                self._activity_timer.stop()
            except Exception:
                pass
            self._activity_timer = None

    def _tick_activity_animation(self):
        if not self.busy:
            self._stop_activity_animation()
            return
        if not self.animations_enabled:
            return
        self._activity_frame += 1
        self._render_composer_meta()

    def _render_composer_meta(self):
        try:
            meta = self.query_one("#composer-meta", Static)
        except Exception:
            return
        p = self._appearance
        if self.busy:
            frame = ACTIVITY_FRAMES[self._activity_frame % len(ACTIVITY_FRAMES)]
            label = self._activity_override or ACTIVITY_STATES[
                (self._activity_frame // 12) % len(ACTIVITY_STATES)
            ]
            folder = os.path.basename(os.path.normpath(self.workdir)) or self.workdir
            meta.update(Text.assemble(
                Text(f"⚙ {frame}  ", style=p["tool"]),
                Text(label, style=p["text"]),
                Text(f"   @{folder}", style="dim"),
            ))
            return
        elif self.connected:
            label, color = "已连接", p["ok"]
        else:
            label, color = "离线", p["err_bright"]
        folder = os.path.basename(os.path.normpath(self.workdir)) or self.workdir
        meta.update(Text.assemble(
            Text("● ", style=color),
            Text(label, style=p["text_muted"]),
            Text(f"   @{folder}", style="dim"),
        ))

    def watch_connected(self, connected):
        self._render_sidebar()
        self._render_composer_meta()

    def watch_busy(self, busy):
        try:
            self.query_one("#composer", Container).set_class(busy, "-busy")
        except Exception:
            pass
        if busy:
            self._start_activity_animation()
        else:
            self._stop_activity_animation()
        self._render_composer_meta()

    # ── 欢迎 / 帮助 ──

    def _add_welcome(self):
        self._render_sidebar()

    def _add_help(self):
        p = self._appearance
        lines = [
            Text("可用命令:", style=f"bold {p['tool']}"),
            Text("  /help        显示此帮助"),
            Text("  /clear       清除对话上下文"),
            Text("  /model       查看当前模型信息"),
            Text("  /config      查看/修改配置（服务器 + 外观）"),
            Text("  /settings    打开外观设置面板"),
            Text("  /theme <名称>  快速切换主题（ocean / ocean-dark / midnight / forest / sunset / noir）"),
            Text("  /approve on|off  当前对话自动通过工具请求"),
            Text("  /allow on|off  开启/关闭自动审核"),
            Text("  /think        查看思考模式"),
            Text("  /think auto   自动按任务复杂度决定"),
            Text("  /think on [low|medium|high|max]  手动开启思考"),
            Text("  /think off    手动关闭思考"),
            Text("  /mcp         查看外部 MCP 服务器"),
            Text("  /mcp add <名称> stdio <命令> [参数...]  添加 stdio 类型"),
            Text("  /mcp add <名称> sse <url>   添加 sse 类型"),
            Text("  /mcp add <名称> http <url>  添加 http 类型"),
            Text("  /mcp remove <名称>  移除外部 MCP 服务器"),
            Text("  /mcp reload  重新连接所有外部 MCP 服务器"),
            Text("  /reconnect   重新连接服务器"),
            Text("  /restart     重启服务端"),
            Text("  /sessions    列出历史对话"),
            Text("  /resume <编号>  恢复历史对话"),
            Text("  /new         新建对话"),
            Text("  /quit        退出程序"),
            Text(""),
            Text("外观设置示例:", style="bold"),
            Text("  /config set theme ocean-dark"),
            Text("  /config set color think #c9a84f"),
            Text("  /config set animations on"),
            Text(""),
        ]
        note = Static(Group(*lines))
        self.query_one("#chat").mount(note)
        self._animate_in(note)

    # ── 本地会话历史 ──

    def _save_conversations(self):
        save_conversations(self.conversations)

    def _record_message(self, identity, text):
        add_conversation_message(self.conversation, identity, text)
        self._save_conversations()

    def _show_sessions(self):
        self._add_note(session_list_text(self.conversations, self.conversation["id"]), "info")

    def _select_session(self, raw_index):
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            return False, "用法: /resume <编号>。先输入 /sessions 查看编号。"
        choices = session_choices(self.conversations, self.conversation["id"])
        if index < 1 or index > len(choices):
            return False, "会话编号不存在。先输入 /sessions 查看编号。"
        if not self.conversation.get("messages"):
            self.conversations = [item for item in self.conversations if item.get("id") != self.conversation["id"]]
        self.conversation = choices[index - 1]
        self.auto_approve_tools = False
        self._save_conversations()
        self._render_sidebar()
        return True, f"已恢复对话: {self.conversation.get('title', '未命名对话')}"

    def _new_conversation(self):
        self.conversation = new_conversation()
        self.conversations.insert(0, self.conversation)
        self.auto_approve_tools = False
        self._save_conversations()
        self._render_sidebar()

    def _clear_current_conversation(self):
        self.conversation["messages"] = []
        self.conversation["title"] = "新对话"
        self.conversation["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        self._save_conversations()

    async def _render_saved_conversation(self, note):
        chat = self.query_one("#chat", VerticalScroll)
        for child in list(chat.children):
            child.remove()
        for item in self.conversation.get("messages", []):
            if item.get("identity") == "User":
                await self._mount_chat(UserMessage(item.get("text", "")))
            elif item.get("identity") == "Model":
                widget = AiMessage()
                widget.full_text = item.get("text", "")
                widget.finish()
                await self._mount_chat(widget)
        self._add_note(note, "ok")
        self._scroll_end()

    # ── 连接 ──

    @work(exclusive=True)
    async def _connect_loop(self):
        url = self.server_url
        self._set_status(f"检查服务器 {url} ...")
        started = await asyncio.to_thread(ensure_server, url)
        if started:
            self.server_started_by_us = True
            self._set_status("已自动启动服务端，连接中...")
        if not await asyncio.to_thread(_http_up, url):
            self._set_status("无法连接服务端，请手动启动 DC Server 或用 /reconnect 重试")
            self._add_note("连接失败: 服务端未响应。请确保 DC Server 已启动。", "error")
            return
        self._set_status("连接中...")
        try:
            async with websockets.connect(url, max_size=2 ** 24, ping_interval=20) as ws:
                self.ws = ws
                await ws.send(json.dumps({
                    "type": "init",
                    "workdir": self.workdir,
                    "history": server_history(self.conversation),
                }, ensure_ascii=False))
                self.connected = True
                self._set_status("已连接，等待输入...")
                ping_task = asyncio.create_task(self._ping_loop(ws))
                try:
                    async for raw in ws:
                        await self._handle_raw(raw)
                        await asyncio.sleep(0)
                finally:
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass
        except ConnectionClosedOK:
            pass
        except ConnectionClosed as e:
            self._add_note(f"连接关闭: {e}", "error")
        except Exception as e:
            self._add_note(f"连接异常: {e}", "error")
        finally:
            self.ws = None
            self.connected = False
            try:
                self._end_busy()
            except Exception:
                pass
            self._set_status("已断开连接 (输入 /reconnect 重连)")

    def _reconnect(self):
        if self.ws:
            try:
                asyncio.ensure_future(self.ws.close())
            except Exception:
                pass
            self.ws = None
        self.connected = False
        try:
            self._end_busy()
        except Exception:
            pass
        self.server_url = detect_server_url()
        self._connect_loop()

    async def _restart_server(self):
        url = self.server_url
        if not self.ws or not self.connected:
            self._add_note("未连接到服务端，无法重启", "error")
            return
        self._start_busy()
        self._set_status("发送重启指令...")
        try:
            await self.ws.send("/restart")
        except Exception as e:
            self._add_note(f"发送重启指令失败: {e}", "error")
            self._end_busy()
            return
        self._add_note("已发送重启指令，等待服务端重启...", "warn")
        self._set_status("服务端重启中...")
        # 先等服务端下线，再等服务端恢复
        for _ in range(40):
            await asyncio.sleep(0.5)
            if not _http_up(url):
                break
        for _ in range(120):
            await asyncio.sleep(0.5)
            if _http_up(url):
                self._end_busy()
                self._add_note("服务端已恢复，重新连接中...", "ok")
                self._reconnect()
                return
        self._end_busy()
        self._add_note("等待服务端恢复超时，请手动检查服务端或用 /reconnect", "error")
        self._set_status("已断开连接 (输入 /reconnect 重连)")

    # ── 接收处理 ──

    async def _ping_loop(self, ws):
        try:
            while True:
                await asyncio.sleep(60)
                try:
                    await ws.send(json.dumps({"type": "ping"}))
                except Exception:
                    return
        except asyncio.CancelledError:
            pass

    async def _handle_raw(self, raw):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        t = data.get("type")
        try:
            if t == "init_ack":
                wd = data.get("workdir", "")
                restored = data.get("restored", 0)
                suffix = f"，已恢复 {restored} 条历史消息" if restored else ""
                self._sync_status = f"工作目录已同步\n{wd}{suffix}"
                self._render_sidebar()
                return
            if t == "chunk":
                c = data.get("c", "")
                if c:
                    self._streaming = True
                    await self._append_assistant(c)
                return
            if t == "think":
                c = data.get("c", "")
                if c:
                    await self._append_think(c)
                return
            if t == "tool":
                await self._add_tool_call(data.get("n", ""), data.get("a", {}))
                return
            if t == "tool_result":
                self._set_tool_result(data.get("n", ""), data.get("r", ""))
                return
            if t == "permission_request":
                path = data.get("path", "") or data.get("tool", "")
                await self._handle_permission(path)
                return
            if t == "step_progress":
                self._step_update(
                    data.get("step", 0),
                    data.get("title", ""),
                    data.get("status", "started"),
                    data.get("detail", ""),
                )
                return
            if t == "done":
                self._streaming = False
                if data.get("cmd") == "clear":
                    self._add_note(data.get("r", "上下文已清除"))
                    self._current_assistant = None
                    self._current_think = None
                    self._tool_queue = []
                    self._clear_steps()
                elif "r" in data:
                    self._add_note(data["r"])
                else:
                    self._finish_turn(data)
                self._end_busy()
                return
        except Exception as e:
            self._add_note(f"[处理消息错误] type={t} {e}", "error")

    def _finish_turn(self, data):
        p = data.get("p", 0)
        c = data.get("c", 0)
        hr = data.get("hr", "0%")
        model = data.get("model_name", "")
        model_id = data.get("model_id", "")
        th = data.get("th", "off")
        te = data.get("te", "")
        self.model_name = model
        self._render_sidebar()
        think_str = f"{te}" if (th == "on" and te) else th
        self._set_status(
            f"模型: {model}" + (f" ({model_id})" if model_id else "") +
            f" | 输入: {p} 输出: {c} | 缓存命中: {hr} | 思考: {think_str}"
        )
        if self._current_assistant:
            self._record_message("Model", self._current_assistant.full_text)
            self._current_assistant.finish()
            self._current_assistant = None
        if self._current_think:
            self._current_think.finish()
        self._current_think = None

    # ── 流式追加 ──

    async def _append_assistant(self, chunk):
        if self._current_assistant is None:
            self._current_assistant = AiMessage()
            await self._mount_chat(self._current_assistant)
        self._current_assistant.append_chunk(chunk)
        self._scroll_end()

    async def _append_think(self, chunk):
        if self._current_think is None:
            self._current_think = ThinkBlock()
            await self._mount_chat(self._current_think)
        self._current_think.append(chunk)
        self._scroll_end()

    async def _add_tool_call(self, name, args):
        self._current_assistant = None
        if self._current_think:
            self._current_think.finish()
        self._current_think = None
        self._activity_override = f"🛠️ AI 正在调用 {name}"
        self._render_composer_meta()
        widget = ToolCallWidget(name, args)
        self._tool_queue.append(widget)
        await self._mount_chat(widget)
        self._scroll_end()

    def _set_tool_result(self, name, result):
        widget = None
        for i, w in enumerate(self._tool_queue):
            if getattr(w, "tool_name", None) == name:
                widget = self._tool_queue.pop(i)
                break
        if widget is None and self._tool_queue:
            widget = self._tool_queue.pop(0)
        if widget is not None:
            widget.set_result(result)
        self._activity_override = None
        self._render_composer_meta()
        self._scroll_end()

    def _step_update(self, step, title, status, detail=""):
        try:
            step = int(step)
        except (TypeError, ValueError):
            step = 0
        try:
            w = self.query_one("#step-progress", StepProgress)
            w.set_step(step, title, status, detail)
            self._scroll_end()
        except Exception:
            pass

    def _clear_steps(self):
        try:
            self.query_one("#step-progress", StepProgress).clear()
        except Exception:
            pass

    async def _mount_chat(self, widget):
        chat = self.query_one("#chat", VerticalScroll)
        await chat.mount(widget)
        self._animate_in(widget)

    def _scroll_end(self):
        if self._scroll_pending:
            return
        self._scroll_pending = True
        self.call_after_refresh(self._do_scroll)

    def _do_scroll(self):
        self._scroll_pending = False
        try:
            chat = self.query_one("#chat", VerticalScroll)
            animated = self.animations_enabled and not self._streaming
            if animated:
                chat.scroll_end(animate=True, duration=self.anim_duration(0.45))
            else:
                chat.scroll_end(animate=False)
        except Exception:
            pass

    def _add_note(self, text, tone="dim"):
        note = SystemNote(text, tone=tone)
        self.query_one("#chat").mount(note)
        self._animate_in(note)
        self._scroll_end()

    # ━━━━━ 自动补全 ━━━━━

    def _completion_visible(self):
        try:
            ol = self.query_one("#completion-list", OptionList)
            return "-visible" in ol.classes
        except Exception:
            return False

    def _on_input_changed(self, text):
        if self._pending_cycles > 0:
            self._pending_cycles -= 1
            return
        try:
            ol = self.query_one("#completion-list", OptionList)
        except Exception:
            return
        first_line = text.split("\n", 1)[0]
        if not first_line.startswith("/"):
            self._hide_completion()
            return
        cmd_part = first_line.strip().split()[0] if first_line.strip() else "/"
        if not cmd_part:
            self._hide_completion()
            return
        matches = [(c, d) for c, d in COMMANDS if c.startswith(cmd_part)]
        if not matches or (len(matches) == 1 and matches[0][0] == cmd_part):
            self._hide_completion()
            return
        ol.clear_options()
        for c, d in matches:
            ol.add_option(Option(f" {c}  —  {d}", id=c))
        ol.add_class("-visible")
        try:
            ol.highlighted = 0
        except Exception:
            pass

    def _hide_completion(self):
        try:
            ol = self.query_one("#completion-list", OptionList)
            ol.remove_class("-visible")
            ol.clear_options()
        except Exception:
            pass

    def _cycle_completion(self, delta):
        try:
            ol = self.query_one("#completion-list", OptionList)
        except Exception:
            return
        count = len(ol.options)
        if count == 0:
            self._hide_completion()
            return
        cur = ol.highlighted or 0
        new = (cur + delta) % count
        try:
            ol.highlighted = new
        except Exception:
            pass
        try:
            opt = ol.get_option_at_index(new)
        except Exception:
            opt = None
        if opt is not None and opt.id is not None:
            self._pending_cycles += 1
            try:
                inp = self.query_one(ChatInput)
                inp.text = str(opt.id)
                inp.cursor_location = (0, len(str(opt.id)))
            except Exception:
                self._pending_cycles -= 1

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        if not self._completion_visible():
            return
        event.stop()
        opt = event.option
        if opt.id is None:
            return
        self._hide_completion()
        try:
            inp = self.query_one(ChatInput)
            inp.text = str(opt.id)
            inp.cursor_location = (0, len(str(opt.id)))
            inp.focus()
        except Exception:
            pass

    def _accept_completion(self, send=False):
        try:
            ol = self.query_one("#completion-list", OptionList)
            opt = ol.highlighted_option
        except Exception:
            opt = None
        self._hide_completion()
        if opt is None:
            return
        cmd = opt.id
        if cmd is None:
            return
        inp = self.query_one(ChatInput)
        if send:
            inp.text = str(cmd)
            self.submit_input()
        else:
            inp.text = str(cmd)
            inp.cursor_location = (0, len(str(cmd)))
            inp.focus()

    # ── 权限 ──

    async def _handle_permission(self, path):
        if self.auto_approve_tools:
            mode = "once"
            self._set_status(f"已自动通过工具请求: {path}")
            if self.ws:
                try:
                    await self.ws.send(json.dumps({"type": "permission_response", "mode": mode}))
                except Exception:
                    pass
            return
        try:
            mode = await self.push_screen_wait(PermissionScreen(path))
        except Exception:
            mode = "deny"
        if mode is None:
            mode = "deny"
        if self.ws:
            try:
                await self.ws.send(json.dumps({"type": "permission_response", "mode": mode}))
            except Exception:
                pass

    # ── 发送 ──

    def _start_busy(self):
        self.busy = True
        self._hide_completion()
        self.query_one(ChatInput).disabled = True

    def _end_busy(self):
        self.busy = False
        self.query_one(ChatInput).disabled = False
        self.query_one(ChatInput).focus()

    def submit_input(self):
        inp = self.query_one(ChatInput)
        text = inp.text.strip()
        if not text:
            return
        cmd = text.lower()

        if cmd == "/help":
            inp.text = ""
            self._add_help()
            return
        if cmd == "/settings":
            inp.text = ""
            self.push_screen(SettingsScreen())
            return
        if cmd == "/theme" or cmd.startswith("/theme "):
            inp.text = ""
            parts = text.split(maxsplit=1)
            name = parts[1].strip() if len(parts) == 2 else ""
            if not name:
                self._add_note("用法: /theme <名称>。可用: " + " / ".join(THEME_DEFS), "warn")
                return
            if name not in THEME_DEFS:
                self._add_note(f"未知主题: {name}（可用: {', '.join(THEME_DEFS)}）", "warn")
                return
            self.appearance_set_theme(name)
            self._add_note(f"主题已切换为 {THEME_DEFS[name]['label']}", "ok")
            return
        if cmd == "/approve" or cmd.startswith("/approve "):
            inp.text = ""
            self._handle_approve_command(text)
            return
        if cmd in ("/quit", "/exit"):
            self.exit()
            return
        if cmd == "/reconnect":
            inp.text = ""
            self._add_note("重新连接中...", "warn")
            self._reconnect()
            return
        if cmd == "/restart":
            inp.text = ""
            asyncio.create_task(self._restart_server())
            return
        if cmd == "/sessions":
            inp.text = ""
            self._show_sessions()
            return
        if cmd.startswith("/resume"):
            inp.text = ""
            parts = text.split(maxsplit=1)
            ok, note = self._select_session(parts[1] if len(parts) == 2 else "")
            if not ok:
                self._add_note(note, "warn")
                return
            asyncio.create_task(self._render_saved_conversation(note))
            self._reconnect()
            return
        if cmd == "/new":
            inp.text = ""
            self._new_conversation()
            self._clear_chat()
            self._add_note("已新建对话", "ok")
            self._reconnect()
            return
        if cmd == "/model":
            inp.text = ""
            asyncio.create_task(self._fetch_model_info())
            return
        if cmd == "/config" or cmd.startswith("/config "):
            handled = self._handle_config_command(text)
            if handled:
                inp.text = ""
                return
        if cmd == "/allow" or cmd.startswith("/allow "):
            inp.text = ""
            asyncio.create_task(self._handle_allow_command(text))
            return
        if cmd == "/think" or cmd.startswith("/think "):
            inp.text = ""
            asyncio.create_task(self._handle_think_command(text))
            return
        if cmd == "/mcp" or cmd.startswith("/mcp "):
            inp.text = ""
            asyncio.create_task(self._handle_mcp_command(text))
            return
        if cmd == "/clear":
            inp.text = ""
            self._clear_current_conversation()
            self._clear_chat()
            if not self.ws or not self.connected:
                self._add_note("未连接到服务端", "error")
                return
            self._start_busy()
            asyncio.create_task(self._do_send("/clear"))
            return

        if not self.ws or not self.connected:
            self._add_note("未连接到服务端，无法发送", "error")
            return
        if self.busy:
            return

        inp.text = ""
        self._record_message("User", text)
        self.query_one("#chat").mount(UserMessage(text))
        self._clear_steps()
        self._scroll_end()
        self._start_busy()
        asyncio.create_task(self._do_send(text))

    async def _do_send(self, text):
        if not self.ws:
            self._add_note("发送失败: 未连接", "error")
            self._end_busy()
            return
        try:
            await self.ws.send(text)
        except Exception as e:
            self._add_note(f"发送失败: {e}", "error")
            self._end_busy()

    def _handle_approve_command(self, text):
        parts = text.lower().split()
        if len(parts) < 2 or parts[1] not in ("on", "off", "开", "关"):
            self._add_note("用法: /approve on  |  /approve off", "warn")
            return
        self.auto_approve_tools = parts[1] in ("on", "开")
        self._render_sidebar()
        if self.auto_approve_tools:
            self._add_note("当前对话已开启工具自动通过；新建或切换会话后会自动关闭", "ok")
        else:
            self._add_note("当前对话已恢复为逐项确认工具请求", "info")

    def _handle_config_command(self, text):
        handled, msg, appearance_changed = handle_config_text(text, self.server_url)
        if appearance_changed:
            self.apply_appearance(animate=True)
        if handled and msg:
            self._add_note(msg, "info")
        return True

    async def _handle_allow_command(self, text):
        parts = text.lower().split()
        enable = None
        if len(parts) >= 2:
            v = parts[1]
            if v in ("on", "true", "1", "开"):
                enable = True
            elif v in ("off", "false", "0", "关"):
                enable = False
        if enable is None:
            self._add_note("用法: /allow on  |  /allow off", "warn")
            return
        http_url = _http_url(self.server_url).replace("/dscat", "/config/auto_audit")
        try:
            req = urllib.request.Request(
                http_url,
                data=json.dumps({"enabled": enable}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                resp = json.loads(r.read().decode("utf-8"))
            ok = resp.get("auto_audit", False) == enable
            if ok:
                self._add_note(f"自动审核已{'开启' if enable else '关闭'}", "ok")
            else:
                self._add_note("设置失败，请检查服务端配置", "error")
        except Exception as e:
            self._add_note(f"自动审核设置失败: {e}", "error")

    async def _handle_think_command(self, text):
        """处理 TUI 的 /think 指令。"""
        payload, error = parse_think_command(text)
        if error:
            self._add_note(error, "warn")
            return
        try:
            if payload is None:
                result = await asyncio.to_thread(get_thinking_config, self.server_url)
            else:
                result = await asyncio.to_thread(set_thinking_config, self.server_url, payload)
            if result.get("status") == "error":
                self._add_note(f"思考设置失败: {result.get('error', '未知错误')}", "error")
                return
            mode = "自动" if result.get("auto_think") else (
                "手动开启" if result.get("think_mode", result.get("mode")) == "enabled" else "手动关闭"
            )
            effort = result.get("think_effort", result.get("effort", "high"))
            message = f"思考模式: {mode} | 强度: {effort}"
            if payload is None:
                message += "\n可用: /think auto | /think on [low|medium|high|max] | /think off | /think effort <强度>"
            self._add_note(message, "ok")
        except Exception as e:
            self._add_note(f"思考设置失败: {e}", "error")

    async def _handle_mcp_command(self, text):
        """处理 TUI 的 /mcp 指令：管理外部 MCP 服务器。"""
        result = await asyncio.to_thread(_call_mcp_api, self.server_url, text)
        self._add_note(result, "info" if not result.startswith("[错误]") else "error")

    async def _fetch_model_info(self):
        http_url = _http_url(self.server_url).replace("/dscat", "/config")
        try:
            with urllib.request.urlopen(http_url, timeout=5) as r:
                data = json.loads(r.read().decode("utf-8"))
            model = data.get("model", "?")
            models = data.get("models", {})
            entry = models.get(model, {})
            name = entry.get("model_name", "?")
            iface = entry.get("接口", "?")
            auto = data.get("auto_think", False)
            p = self._appearance
            lines = [
                Text("当前模型信息:", style=f"bold {p['accent']}"),
                Text(f"  模型ID: {model}"),
                Text(f"  模型名: {name}"),
                Text(f"  接口:   {iface}"),
                Text(f"  自动思考: {'开' if auto else '关'}"),
                Text(""),
            ]
            note = Static(Group(*lines))
            self.query_one("#chat").mount(note)
            self._animate_in(note)
            self._scroll_end()
        except Exception as e:
            self._add_note(f"获取模型信息失败: {e}", "error")

    def _clear_chat(self):
        chat = self.query_one("#chat", VerticalScroll)
        for child in list(chat.children):
            child.remove()
        self._current_assistant = None
        self._current_think = None
        self._tool_queue = []
        self._clear_steps()
        self._add_welcome()

    # ── 退出清理 ──

    def on_unmount(self):
        self._stop_activity_animation()
        if self.server_started_by_us and _STARTED_SERVER_PID:
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(_STARTED_SERVER_PID), "/F", "/T"],
                        capture_output=True, timeout=5,
                    )
                else:
                    import signal
                    try:
                        os.kill(_STARTED_SERVER_PID, signal.SIGTERM)
                    except Exception:
                        pass
            except Exception:
                pass


# ━━━━━ 纯文本模式客户端 ━━━━━

class PlainClient:
    """非 TTY 环境下的纯文本行模式客户端（PyCharm 运行控制台等）。"""

    HELP = """\
可用命令:
  /help       显示此帮助
  /clear      清除对话上下文
  /model      查看当前模型信息
  /config     查看客户端配置
  /settings   打开外观设置（仅 TUI 模式）
  /theme <名称>  切换主题
  /approve on|off  当前对话自动通过工具请求
  /allow      开启/关闭自动审核（/allow on 或 /allow off）
  /think      查看/调整思考模式（/think auto | on [强度] | off）
  /mcp        管理外部 MCP 服务器（/mcp | /mcp add | /mcp remove | /mcp reload）
  /reconnect  重新连接服务器
  /restart    重启服务端
  /sessions   列出历史对话
  /resume <编号>  恢复历史对话
  /new        新建对话
  /quit       退出程序
直接输入文字回车即可与 AI 对话。"""

    def __init__(self, server_url, workdir):
        self.server_url = server_url
        self.workdir = workdir
        self.ws = None
        self.connected = False
        self.busy = False
        self.auto_approve_tools = False
        self.palette = resolve_appearance(load_config().get("appearance") or {})
        self.conversations = load_conversations()
        self.conversation = new_conversation()
        self.conversations.insert(0, self.conversation)
        self._reconnect_requested = False
        self._current_reply = ""
        save_conversations(self.conversations)

    def _p(self, *args, **kwargs):
        print(*args, **kwargs, flush=True)

    def _record_message(self, identity, text):
        add_conversation_message(self.conversation, identity, text)
        save_conversations(self.conversations)

    def _show_sessions(self):
        self._p(session_list_text(self.conversations, self.conversation["id"]))

    def _select_session(self, raw_index):
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            return False, "用法: /resume <编号>。先输入 /sessions 查看编号。"
        choices = session_choices(self.conversations, self.conversation["id"])
        if index < 1 or index > len(choices):
            return False, "会话编号不存在。先输入 /sessions 查看编号。"
        if not self.conversation.get("messages"):
            self.conversations = [item for item in self.conversations if item.get("id") != self.conversation["id"]]
        self.conversation = choices[index - 1]
        self.auto_approve_tools = False
        save_conversations(self.conversations)
        return True, f"已恢复对话: {self.conversation.get('title', '未命名对话')}"

    def _print_saved_conversation(self):
        for item in self.conversation.get("messages", []):
            name = "你" if item.get("identity") == "User" else APP_NAME
            self._p(f"{name} > {item.get('text', '')}")

    def _clear_current_conversation(self):
        self.conversation["messages"] = []
        self.conversation["title"] = "新对话"
        self.conversation["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        save_conversations(self.conversations)

    async def _ping_loop(self, ws):
        try:
            while True:
                await asyncio.sleep(60)
                try:
                    await ws.send(json.dumps({"type": "ping"}))
                except Exception:
                    return
        except asyncio.CancelledError:
            pass

    async def run(self):
        self._p(f"{APP_NAME} Code v{VERSION} (纯文本模式)")
        self._p(f"工作目录: {self.workdir}")
        while True:
            self._reconnect_requested = False
            url = self.server_url
            self._p(f"服务器: {url}")
            self._p("正在连接服务端...")
            started = await asyncio.to_thread(ensure_server, url)
            if started:
                self._p("已自动启动本地服务端。")
            if not await asyncio.to_thread(_http_up, url):
                self._p(f"{_ansi_fg(self.palette['err_bright'])}[错误]{_RESET} 无法连接服务端。请确认 DC Server 已启动，或用 /config set host <IP> 修改地址。")
                return

            try:
                async with websockets.connect(url, max_size=2 ** 24, ping_interval=20) as ws:
                    self.ws = ws
                    await ws.send(json.dumps({
                        "type": "init",
                        "workdir": self.workdir,
                        "history": server_history(self.conversation),
                    }, ensure_ascii=False))
                    self.connected = True
                    self._p("已连接，输入消息开始对话（Ctrl+C 退出）\n")
                    ping_task = asyncio.create_task(self._ping_loop(ws))
                    try:
                        await self._session_loop()
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
            except (ConnectionClosed, ConnectionClosedOK):
                if not self._reconnect_requested:
                    self._p(f"\n{_ansi_fg(self.palette['err_bright'])}[连接已关闭]{_RESET}")
            except KeyboardInterrupt:
                self._p("\n再见~")
                return
            except Exception as e:
                self._p(f"\n{_ansi_fg(self.palette['err_bright'])}[连接异常]{_RESET} {e}")
            finally:
                self.ws = None
                self.connected = False

            if not self._reconnect_requested:
                return

    async def _session_loop(self):
        receiver = asyncio.create_task(self._receiver_loop())
        try:
            await self._input_loop()
        finally:
            receiver.cancel()
            try:
                await receiver
            except asyncio.CancelledError:
                pass

    async def _input_loop(self, disconnected=False):
        loop = asyncio.get_event_loop()
        while True:
            try:
                line = await loop.run_in_executor(None, input, "你 > " if not disconnected else "> ")
            except EOFError:
                break
            except KeyboardInterrupt:
                self._p("\n再见~")
                raise
            text = line.strip()
            if not text:
                continue
            await self._handle_command_or_send(text)

    async def _handle_command_or_send(self, text):
        cmd = text.lower()
        if cmd == "/help":
            self._p(self.HELP)
            return
        if cmd in ("/quit", "/exit"):
            raise KeyboardInterrupt
        if cmd == "/theme" or cmd.startswith("/theme "):
            parts = text.split(maxsplit=1)
            name = parts[1].strip() if len(parts) == 2 else ""
            if name not in THEME_DEFS:
                self._p("[主题] 可用: " + " / ".join(THEME_DEFS))
                return
            cfg = load_config()
            cfg.setdefault("appearance", dict(DEFAULT_APPEARANCE))["theme"] = name
            save_config(cfg)
            self.palette = resolve_appearance(cfg["appearance"])
            self._p(f"[主题已切换] {THEME_DEFS[name]['label']}")
            return
        if cmd == "/reconnect":
            self._p("重新连接中...")
            self.server_url = detect_server_url()
            self._reconnect_requested = True
            raise ConnectionClosedOK(None, None, None)
        if cmd == "/restart":
            await self._wait_server_restart()
            return
        if cmd == "/sessions":
            self._show_sessions()
            return
        if cmd.startswith("/resume"):
            parts = text.split(maxsplit=1)
            ok, note = self._select_session(parts[1] if len(parts) == 2 else "")
            self._p(note)
            if not ok:
                return
            self._print_saved_conversation()
            self._reconnect_requested = True
            raise ConnectionClosedOK(None, None, None)
        if cmd == "/new":
            self.conversation = new_conversation()
            self.conversations.insert(0, self.conversation)
            self.auto_approve_tools = False
            save_conversations(self.conversations)
            self._p("已新建对话，重新连接中...")
            self._reconnect_requested = True
            raise ConnectionClosedOK(None, None, None)
        if cmd == "/model":
            await self._fetch_model_info()
            return
        if cmd.startswith("/config"):
            handled, msg, _ = handle_config_text(text, self.server_url)
            if msg:
                self._p(msg)
            if handled:
                self.palette = resolve_appearance(load_config().get("appearance") or {})
            return
        if cmd.startswith("/allow"):
            await self._handle_allow(text)
            return
        if cmd == "/approve" or cmd.startswith("/approve "):
            parts = text.lower().split()
            if len(parts) < 2 or parts[1] not in ("on", "off", "开", "关"):
                self._p("用法: /approve on  |  /approve off")
                return
            self.auto_approve_tools = parts[1] in ("on", "开")
            self._p("[工具审核] 当前对话已开启自动通过" if self.auto_approve_tools else "[工具审核] 当前对话已恢复逐项确认")
            return
        if cmd == "/think" or cmd.startswith("/think "):
            await self._handle_think(text)
            return
        if cmd == "/mcp" or cmd.startswith("/mcp "):
            await self._handle_mcp_cli(text)
            return
        if cmd == "/clear":
            if not self.ws:
                self._p("[未连接] 无法执行该命令")
                return
            try:
                self._clear_current_conversation()
                await self.ws.send(text)
            except Exception as e:
                self._p(f"[发送失败] {e}")
            return
        if not self.ws:
            self._p("[未连接] 无法发送消息，输入 /reconnect 重试")
            return
        if self.busy:
            self._p("[忙碌] AI 正在回复，请稍候...")
            return
        try:
            self._p(f"{_ansi_fg(self.palette['ai'])}{APP_NAME} > {_RESET}", end="")
            self.busy = True
            self._record_message("User", text)
            self._current_reply = ""
            await self.ws.send(text)
        except Exception as e:
            self.busy = False
            self._p(f"[发送失败] {e}")

    async def _receiver_loop(self):
        try:
            async for raw in self.ws:
                await self._handle_raw(raw)
        except ConnectionClosedOK:
            pass
        except Exception as e:
            self._p(f"\n[接收异常] {e}")

    async def _wait_server_restart(self):
        url = self.server_url
        if not self.ws or not self.connected:
            self._p("[未连接] 无法重启服务端")
            return
        try:
            await self.ws.send("/restart")
        except Exception as e:
            self._p(f"[发送失败] {e}")
            return
        self._p("[重启] 已发送重启指令，等待服务端恢复...")
        for _ in range(40):
            await asyncio.sleep(0.5)
            if not _http_up(url):
                break
        for _ in range(120):
            await asyncio.sleep(0.5)
            if _http_up(url):
                self._p("[重启完成] 服务端已恢复，重新连接中...")
                self._reconnect_requested = True
                raise ConnectionClosedOK(None, None, None)
        self._p("[重启超时] 服务端未恢复，请手动检查或用 /reconnect")

    async def _handle_allow(self, text):
        parts = text.lower().split()
        enable = None
        if len(parts) >= 2:
            v = parts[1]
            if v in ("on", "true", "1", "开"):
                enable = True
            elif v in ("off", "false", "0", "关"):
                enable = False
        if enable is None:
            self._p("用法: /allow on  |  /allow off")
            return
        http_url = _http_url(self.server_url).replace("/dscat", "/config/auto_audit")
        try:
            req = urllib.request.Request(
                http_url,
                data=json.dumps({"enabled": enable}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                resp = json.loads(r.read().decode("utf-8"))
            ok = resp.get("auto_audit", False) == enable
            self._p(f"自动审核已{'开启' if ok and enable else '关闭' if ok else '设置失败'}")
        except Exception as e:
            self._p(f"[自动审核设置失败] {e}")

    async def _handle_think(self, text):
        payload, error = parse_think_command(text)
        if error:
            self._p(f"[错误] {error}")
            return
        try:
            if payload is None:
                result = await asyncio.to_thread(get_thinking_config, self.server_url)
            else:
                result = await asyncio.to_thread(set_thinking_config, self.server_url, payload)
            if result.get("status") == "error":
                self._p(f"[思考设置失败] {result.get('error', '未知错误')}")
                return
            mode = "自动" if result.get("auto_think") else ("手动开启" if result.get("think_mode", result.get("mode")) == "enabled" else "手动关闭")
            effort = result.get("think_effort", result.get("effort", "high"))
            self._p(f"[思考模式] {mode} | 强度: {effort}")
            if payload is None:
                self._p("用法: /think auto | /think on [low|medium|high|max] | /think off | /think effort <强度>")
        except Exception as e:
            self._p(f"[思考设置失败] {e}")

    async def _handle_mcp_cli(self, text):
        """处理纯终端模式的 /mcp 指令。"""
        result = await asyncio.to_thread(_call_mcp_api, self.server_url, text)
        self._p(result)

    async def _handle_raw(self, raw):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        p = self.palette
        t = data.get("type")
        if t == "init_ack":
            wd = data.get("workdir", "")
            restored = data.get("restored", 0)
            if wd:
                self._p(f"[工作目录已同步] {wd}")
            if restored:
                self._p(f"[已恢复 {restored} 条历史消息]")
            return
        if t == "chunk":
            chunk = data.get("c", "")
            self._current_reply += chunk
            self._p(chunk, end="", flush=True)
            return
        if t == "think":
            self._p(f"{_ansi_fg(p['think'])}{data.get('c', '')}{_RESET}", end="", flush=True)
            return
        if t == "tool":
            name = data.get("n", "")
            args = data.get("a", {})
            self._p(f"\n{_ansi_fg(p['tool'])}[工具] {name} {json.dumps(args, ensure_ascii=False)}{_RESET}", flush=True)
            return
        if t == "tool_result":
            r = data.get("r", "")
            if data.get("truncated"):
                r += "...(结果已截断)"
            self._p(f"\033[2m{r}\033[0m", flush=True)
            return
        if t == "permission_request":
            path = data.get("path", "")
            if self.auto_approve_tools:
                self._p(f"\n[工具审核] 已自动通过: {path}")
                if self.ws:
                    try:
                        await self.ws.send(json.dumps({"type": "permission_response", "mode": "once"}))
                    except Exception:
                        pass
                return
            self._p(f"\n[权限请求] AI 想访问: {path}")
            mode = await self._ask_permission(path)
            if self.ws:
                try:
                    await self.ws.send(json.dumps({"type": "permission_response", "mode": mode}))
                except Exception:
                    pass
            return
        if t == "done":
            self.busy = False
            if data.get("cmd") == "clear":
                self._p(f"\n{data.get('r', '上下文已清除')}")
            elif "r" in data:
                self._p(f"\n{data['r']}")
            else:
                pp = data.get("p", 0)
                c = data.get("c", 0)
                model = data.get("model_name", "")
                self._p(f"\n\n{_ansi_fg(p['ok'])}[完成]{_RESET} {model} | 输入:{pp} 输出:{c}\n")
                self._record_message("Model", self._current_reply)
                self._current_reply = ""
            return

    async def _ask_permission(self, path):
        loop = asyncio.get_event_loop()
        self._p("  [Y]本次允许 / [A]始终允许 / [N]拒绝 (默认 N): ", end="", flush=True)
        try:
            ans = await loop.run_in_executor(None, input)
            ans = ans.strip().lower()
        except Exception:
            ans = ""
        if ans == "y":
            return "once"
        if ans == "a":
            return "always"
        return "deny"

    async def _fetch_model_info(self):
        http_url = _http_url(self.server_url).replace("/dscat", "/config")
        try:
            def _fetch():
                with urllib.request.urlopen(http_url, timeout=5) as r:
                    return json.loads(r.read().decode("utf-8"))
            data = await asyncio.to_thread(_fetch)
            model = data.get("model", "?")
            models = data.get("models", {})
            entry = models.get(model, {})
            self._p("当前模型信息:")
            self._p(f"  模型ID: {model}")
            self._p(f"  模型名: {entry.get('model_name', '?')}")
            self._p(f"  接口:   {entry.get('接口', '?')}")
            self._p(f"  自动思考: {'开' if data.get('auto_think') else '关'}")
        except Exception as e:
            self._p(f"[获取失败] {e}")


_RESET = "\033[0m"


def main():
    workdir = os.getcwd()
    server_url = detect_server_url()

    args = sys.argv[1:]
    if args:
        for i, arg in enumerate(args):
            if arg == "--theme" and i + 1 < len(args):
                cfg = load_config()
                if args[i + 1] in THEME_DEFS:
                    cfg.setdefault("appearance", dict(DEFAULT_APPEARANCE))["theme"] = args[i + 1]
                    save_config(cfg)
            elif arg == "--no-animations":
                cfg = load_config()
                cfg.setdefault("appearance", dict(DEFAULT_APPEARANCE))["animations"] = False
                save_config(cfg)
            elif arg == "--speed" and i + 1 < len(args):
                cfg = load_config()
                if args[i + 1] in ("fast", "normal", "slow"):
                    cfg.setdefault("appearance", dict(DEFAULT_APPEARANCE))["animation_speed"] = args[i + 1]
                    save_config(cfg)

    if not _is_tty():
        print("=" * 56)
        print(f" 检测到非交互式终端（PyCharm 运行控制台/管道重定向）")
        print(" 已自动切换为【纯文本行模式】，避免全屏渲染产生空白。")
        print(" 如需美观的 TUI 界面，请在独立的 cmd / Windows Terminal")
        print(" 窗口中运行: dccode")
        print("=" * 56)
        client = PlainClient(server_url=server_url, workdir=workdir)
        try:
            asyncio.run(client.run())
        except KeyboardInterrupt:
            pass
        return

    app = DcCatApp(server_url=server_url, workdir=workdir)
    app.run()


def _is_tty():
    return sys.stdin.isatty() and sys.stdout.isatty()


if __name__ == "__main__":
    main()
