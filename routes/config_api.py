"""
配置管理 API：/config 系列

前端用这些接口读写 Config/Model.json：
  - GET  /config             : 查当前配置
  - POST /config             : 改激活模型 / 思考模式
  - POST /config/models      : 加一个模型配置
  - DELETE /config/models    : 删一个模型配置
  - POST /config/auto_audit  : 开关 auto_audit（AI 二次审核工具）
  - GET|POST|DELETE /config/mcp, /config/mcp/reload : 外部 MCP server 管理
"""
import json

from fastapi import APIRouter
from pydantic import BaseModel

from utils import logger
from core.MangerConfig import Config
from chat.loop import usage_stats
from tools.mcp_client import hub as mcp_hub

router = APIRouter()
Model = Config("Config/Model.json")

CONFIG_FILE = "Config/Model.json"


def _update_config_file(patch):
    """
    把 patch 合并进 Model.json（保留其他字段），写回文件并刷新内存配置。

    patch 形如：{"model": "model_2"} 或 {"thinking": {"mode": "disabled"}}
    """
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data.update(patch)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    Model.reload()


class _ConfigUpdate(BaseModel):
    """POST /config 的请求体"""
    model: str | None = None
    auto_think: bool | None = None
    think_mode: str | None = None
    think_effort: str | None = None


class _ThinkingUpdate(BaseModel):
    """POST /config/thinking 的请求体。三个字段至少提供一个。"""
    auto_think: bool | None = None
    mode: str | None = None
    effort: str | None = None


class _ModelEntry(BaseModel):
    """加/删模型配置的请求体"""
    model_id: str
    接口: str = "openai"
    model_name: str = ""
    api_key: str = ""
    url: str = ""


class _AutoAuditUpdate(BaseModel):
    """开关 auto_audit 的请求体"""
    enabled: bool


@router.get("/config")
async def get_config():
    """查当前配置（api_key 不返回明文，只告诉前端"配没配"）。"""
    return {
        "model": Model.model,
        "models": dict(Model.models),
        "model_ids": Model.model_ids,
        "auto_think": Model.auto_think,
        "think_mode": Model.think_mode,
        "think_effort": Model.think_effort,
        "api_key_configured": bool(Model.active_api_key),
        "auto_audit": Model.auto_audit,
    }


@router.post("/config")
async def set_config(cfg: _ConfigUpdate):
    """改激活模型 / 思考模式，写回 Model.json。"""
    patch = {}
    if cfg.model is not None:
        patch["model"] = cfg.model
    if cfg.auto_think is not None:
        patch["auto_think"] = cfg.auto_think
    if cfg.think_mode is not None or cfg.think_effort is not None:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                thinking = json.load(f).get("thinking", {})
        except Exception:
            thinking = {}
        if cfg.think_mode is not None:
            thinking["mode"] = cfg.think_mode
        if cfg.think_effort is not None:
            thinking["effort"] = cfg.think_effort
        patch["thinking"] = thinking
    _update_config_file(patch)
    return {"status": "ok", "model": Model.model}


@router.post("/config/models")
async def add_model_entry(item: _ModelEntry):
    """加一个模型配置。"""
    Model.set_model_entry(item.model_id, {
        "接口": item.接口,
        "model_name": item.model_name,
        "api_key": item.api_key,
        "url": item.url,
    })
    usage_stats[item.model_id] = {"tokens": 0}
    return {"status": "ok", "model_id": item.model_id}


@router.delete("/config/models")
async def remove_model_entry(item: _ModelEntry):
    """删一个模型配置。"""
    Model.remove_model_entry(item.model_id)
    usage_stats.pop(item.model_id, None)
    return {"status": "ok", "model_id": item.model_id}


@router.post("/config/auto_audit")
async def set_auto_audit(cfg: _AutoAuditUpdate):
    """开关 auto_audit（AI 二次审核工具调用）。"""
    _update_config_file({"auto_audit": cfg.enabled})
    logger.info("AUDIT", f"自动审核 {'开启' if cfg.enabled else '关闭'}")
    return {"status": "ok", "auto_audit": cfg.enabled}


@router.post("/config/thinking")
async def set_thinking(cfg: _ThinkingUpdate):
    """
    调整 AI 思考策略。

    - auto_think=true：服务端按消息复杂度自动决定思考开关
    - auto_think=false + mode=enabled/disabled：手动开关思考
    - effort：思考强度，支持 low/medium/high/max
    """
    valid_modes = {"enabled", "disabled"}
    valid_efforts = {"low", "medium", "high", "max"}
    if cfg.mode is not None and cfg.mode not in valid_modes:
        return {"status": "error", "error": "mode 只能是 enabled 或 disabled"}
    if cfg.effort is not None and cfg.effort not in valid_efforts:
        return {"status": "error", "error": "effort 只能是 low、medium、high 或 max"}
    if cfg.auto_think is None and cfg.mode is None and cfg.effort is None:
        return {"status": "error", "error": "至少提供 auto_think、mode 或 effort 之一"}

    patch = {}
    if cfg.auto_think is not None:
        patch["auto_think"] = cfg.auto_think
    if cfg.mode is not None or cfg.effort is not None:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                thinking = json.load(f).get("thinking", {})
        except Exception:
            thinking = {}
        if cfg.mode is not None:
            thinking["mode"] = cfg.mode
        if cfg.effort is not None:
            thinking["effort"] = cfg.effort
        patch["thinking"] = thinking
    _update_config_file(patch)

    logger.info("THINKING",
                f"自动={'开' if Model.auto_think else '关'} mode={Model.think_mode} effort={Model.think_effort}")
    return {
        "status": "ok",
        "auto_think": Model.auto_think,
        "mode": Model.think_mode,
        "effort": Model.think_effort,
    }


# ============================================================
# 外部 MCP server 管理（配置文件是 Config/mcp_servers.json）
# ============================================================

class _McpServerEntry(BaseModel):
    """单个外部 MCP server 配置。"""
    name: str
    transport: str
    command: str | None = None
    args: list[str] | None = None
    env: dict | None = None
    url: str | None = None


class _McpRemoveEntry(BaseModel):
    """删除外部 MCP server 的请求体。"""
    name: str


@router.get("/config/mcp")
async def list_mcp_servers():
    """列出配置的全部外部 MCP server。"""
    return {"servers": mcp_hub.list_external()}


@router.post("/config/mcp")
async def add_mcp_server(entry: _McpServerEntry):
    """新增一个外部 MCP server 配置并立即连接。"""
    cfg = {"name": entry.name, "transport": entry.transport}
    if entry.command is not None:
        cfg["command"] = entry.command
    if entry.args is not None:
        cfg["args"] = entry.args
    if entry.env is not None:
        cfg["env"] = entry.env
    if entry.url is not None:
        cfg["url"] = entry.url
    return mcp_hub.add_external(cfg)


@router.delete("/config/mcp")
async def remove_mcp_server(entry: _McpRemoveEntry):
    """删除一个外部 MCP server 配置并断开连接。"""
    return mcp_hub.remove_external(entry.name)


@router.post("/config/mcp/reload")
async def reload_mcp_servers():
    """断开所有外部 MCP 连接，重新读配置并重连。"""
    return mcp_hub.reload_external()
