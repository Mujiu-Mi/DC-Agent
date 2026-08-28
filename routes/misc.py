"""
杂项路由：健康检查 / 模型列表 / 用量统计 / 上下文压缩 / 记忆检索

主对话流程不用这些，是给前端辅助界面用的：
  - GET  /models            : 列所有模型
  - GET  /usage_stats       : 查 token 用量统计
  - POST /compress_context  : 压缩全局上下文
  - POST /search_memory     : 搜索记忆
"""
from fastapi import APIRouter
from pydantic import BaseModel

from core.MangerConfig import Config
from memory import memory_manager as mm
from core.summary import call_summary
from chat.loop import usage_stats

router = APIRouter()
Model = Config("Config/Model.json")


@router.get("/dscat")
async def dscat_get():
    """健康检查端点（GET /dscat 返回运行状态）。"""
    return "正常运行！！"


@router.get("/models")
async def list_models():
    """列所有模型（不返回 api_key）。"""
    return {
        "model": Model.model,
        "models": {
            mid: {
                "接口": e.get("接口", ""),
                "model_name": e.get("model_name", ""),
                "url": e.get("url", ""),
            }
            for mid, e in Model.models.items()
        },
    }


@router.get("/usage_stats")
async def get_usage_stats():
    """查 token 用量统计（按模型 ID 汇总）。"""
    return usage_stats


@router.post("/compress_context")
async def compress_context():
    """压缩全局上下文（先用 AI 总结，再归档）。"""
    ctx = mm.get_context()
    if not ctx.strip():
        return {"ok": False, "note": "上下文为空"}
    ok = mm.compress_context(call_summary)
    return {"ok": ok, "note": "上下文已压缩并归档" if ok else "压缩失败"}


@router.post("/summarize_context")
async def summarize_context():
    """总结全局上下文：先试压缩（含归档），失败就只做摘要。"""
    ctx = mm.get_context()
    if not ctx.strip():
        return {"summary": "", "note": "上下文为空"}
    if mm.compress_context(call_summary):
        return {"summary": "上下文已压缩", "note": "压缩完成（含归档）"}
    summary = call_summary(
        "以下是一段 AI 对话的上下文记录。请将其总结为一段简洁的摘要，保留关键信息：\n\n" + ctx[-5000:]
    )
    if summary:
        mm.clear_context()
        mm.append_context("System", "[上下文整理摘要]" + summary)
        return {"summary": summary, "note": "ok"}
    return {"summary": "", "note": "总结失败"}


class _SearchItem(BaseModel):
    """搜索记忆的请求体"""
    query: str
    max_results: int = 5


@router.post("/search_memory")
async def search_memory(item: _SearchItem):
    """搜索记忆（长期/每日/归档）。"""
    return {"results": mm.search_memory(item.query, item.max_results)}
