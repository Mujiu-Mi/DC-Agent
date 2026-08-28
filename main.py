"""
DC Server 主入口

本文件只做三件事：
  1. 创建 FastAPI 应用实例 + 注册生命周期（启动调度器/关闭调度器）
  2. 挂载所有路由（dscat / qq / config / misc）
  3. 启动 uvicorn

所有具体业务逻辑都在子包里：
  - core/    : 基础能力（客户端/提示词/难度评估/总结）
  - chat/    : 对话核心（流式/聊天循环/会话池）
  - routes/  : HTTP/WebSocket 路由

看这个文件就能知道整个服务的"目录结构"和"路由分布"。
"""
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core.MangerConfig import Config
from routes.dscat import router as dscat_router
from routes.qq import router as qq_router
from routes.config_api import router as config_router
from routes.misc import router as misc_router
from core.summary import job_daily_summary
from tools.mcp_client import hub as mcp_hub

# 全局配置实例（所有子模块各自 new 自己的 Config，每次 new 都会 load 文件，状态一致）
Model = Config("Config/Model.json")

# 定时任务调度器：每天 23:30 跑一次当日对话总结
scheduler = AsyncIOScheduler()


@asynccontextmanager
async def lifespan(app):
    """
    FastAPI 生命周期钩子。
    启动时：注册每日 23:30 的记忆总结任务；预热 MCP hub（连接外部 MCP server）
    关闭时：停调度器；关闭 MCP hub 的外部连接
    """
    scheduler.add_job(job_daily_summary, "cron", hour=23, minute=30)
    scheduler.start()
    print("[Scheduler] 每日 23:30 记忆总结已启动")
    # 预热 MCP hub：连接 Config/mcp_servers.json 里的外部 MCP server
    # 失败不阻塞启动，内置工具始终可用
    try:
        mcp_hub.list_all_tools()
        print("[MCP] 内置 + 外部工具已就绪")
    except Exception as e:
        print(f"[MCP] 外部 MCP 初始化失败（内置工具仍可用）: {e}")
    yield
    scheduler.shutdown()
    mcp_hub.shutdown()


# 创建 FastAPI 应用，挂载生命周期
app = FastAPI(lifespan=lifespan)

# 挂载所有路由
app.include_router(dscat_router)      # /dscat HTTP + WebSocket
app.include_router(qq_router)         # /qq HTTP + WebSocket
app.include_router(config_router)     # /config 系列
app.include_router(misc_router)       # /models /usage_stats /compress_context 等

# 挂前端静态资源（如果有 ../client 目录）
# 注意：必须放在最后，因为 StaticFiles mount "/" 会吃掉所有未匹配的路径
_frontend_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "client")
if os.path.exists(_frontend_dir):
    app.mount("/", StaticFiles(directory=_frontend_dir, html=True), name="frontend")


if __name__ == "__main__":
    _host = Model.server_host
    _port = Model.server_port
    uvicorn.run("main:app", host=_host, port=_port, reload=False, access_log=False)
