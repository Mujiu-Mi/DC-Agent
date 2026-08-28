"""
/dscat 路由：桌面客户端（DC Client）用的接口

  - POST /dscat : 非流式 SSE，客户端发一条消息，服务端流式返回所有事件
  - WebSocket /dscat : 全双工，流式回复 + 工具权限交互

工作目录：客户端在 init 消息里传过来，决定工具能访问的根目录。
"""
import json
import asyncio
import os
import sys
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from utils import logger
from core.MangerConfig import Config
from core.permission_manager import PermissionManager
from core.session_context import SessionContext
from core.difficulty import assess_difficulty
from chat.loop import chat_loop
from routes.common_ws import ask_permission, start_receiver

router = APIRouter()
Model = Config("Config/Model.json")

# WebSocket 空闲超时（秒）：5 分钟没消息就断开
IDLE_TIMEOUT = 300


def _restart_server():
    """延迟 0.5 秒后重启服务进程（原地替换，PID 不变）。"""
    time.sleep(0.5)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)
    os.execv(sys.executable, [sys.executable, os.path.join(root, "main.py")])


class Item(BaseModel):
    """POST /dscat 的请求体"""
    text: str
    workdir: str = ""


@router.post("/dscat")
async def dscat_post(item: Item):
    """
    HTTP POST 路径：SSE 流式返回。
    客户端发一条消息，所有事件（chunk/think/tool/done）依次刷出去。
    """
    user_input = item.text
    thinking_type, reasoning_effort, model_id = await assess_difficulty(user_input)
    workdir = item.workdir or Model.root_dir
    perm_mgr = PermissionManager(workdir)
    session = SessionContext(perm_mgr=perm_mgr)
    queue = asyncio.Queue()

    async def _sse_send(data):
        await queue.put(data)

    chat_task = None

    async def event_stream():
        """SSE 生成器：从队列拿事件，转成 SSE 格式发出去。"""
        nonlocal chat_task
        chat_task = asyncio.create_task(
            chat_loop(_sse_send, user_input, thinking_type, reasoning_effort, model_id,
                      session, workdir=workdir)
        )
        try:
            while True:
                data = await queue.get()
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                if data.get("type") == "done":
                    break
        except asyncio.CancelledError:
            if chat_task and not chat_task.done():
                chat_task.cancel()
            raise
        await chat_task

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.websocket("/dscat")
async def dscat_ws(websocket: WebSocket):
    """
    WebSocket 路径：全双工对话。

    流程：
      1. 客户端发 init（含 workdir 和可选的历史恢复），服务端回 init_ack
      2. 之后每收到一条消息就跑一轮 chat_loop
      3. 期间客户端可回答 permission_response 决定工具能不能用
      4. /clear 清上下文，/restart 重启服务端，ping 保活
    """
    await websocket.accept()
    perm_mgr = PermissionManager(Model.root_dir)
    session = SessionContext(perm_mgr=perm_mgr)
    logger.info("WS", f"新连接 session={session.session_id}")

    # ━━━━ 握手：等客户端发 init ━━━━
    client_workdir = ""
    user_input = None
    try:
        first = await asyncio.wait_for(websocket.receive_text(), timeout=IDLE_TIMEOUT)
        # 如果第一条不是合法 JSON 的 init，就当它是普通用户输入
        try:
            init_data = json.loads(first)
        except (json.JSONDecodeError, ValueError):
            user_input = first
        else:
            if isinstance(init_data, dict) and init_data.get("type") == "init":
                client_workdir = init_data.get("workdir", "") or ""
                if client_workdir:
                    perm_mgr.set_root(client_workdir)
                    logger.info("WS", f"工作目录设置: {client_workdir}")

                # 客户端恢复历史时的还原（只收最近 200 条，每条限 20000 字）
                restored = 0
                history = init_data.get("history", [])
                if isinstance(history, list):
                    for item in history[-200:]:
                        if not isinstance(item, dict):
                            continue
                        identity = item.get("identity")
                        text = item.get("text")
                        if identity not in ("User", "Model") or not isinstance(text, str) or not text.strip():
                            continue
                        session.add_context(identity, text[:20000])
                        restored += 1
                await websocket.send_json({"type": "init_ack", "workdir": client_workdir, "restored": restored})
    except asyncio.TimeoutError:
        await _send_done_and_close(websocket, "连接超时了喵~")
        return

    # ━━━━ 主循环：收消息 -> 跑对话 ━━━━
    try:
        while True:
            if user_input is None:
                user_input = await _receive_message(websocket)
                if user_input is None:
                    return  # 连接已断开或超时

            # 命令：清空上下文
            if user_input.strip().lower() == "/clear":
                session.clear_context()
                await websocket.send_json({"type": "done", "r": "上下文已清除", "cmd": "clear"})
                user_input = None
                continue

            # 命令：重启服务端
            if user_input.strip().lower() == "/restart":
                try:
                    await websocket.send_json({"type": "done", "r": "收到，服务端正在重启喵~", "cmd": "restart"})
                except Exception:
                    pass
                try:
                    await websocket.close()
                except Exception:
                    pass
                asyncio.create_task(asyncio.to_thread(_restart_server))
                return

            pending_inputs = []
            perm_resp_q = asyncio.Queue(maxsize=1)

            async def _ws_send(data):
                await websocket.send_json(data)

            async def _request_permission(name, args):
                return await ask_permission(websocket, session, perm_resp_q, name, args)

            receiver_task = start_receiver(websocket, pending_inputs, perm_resp_q)
            try:
                thinking_type, reasoning_effort, model_id = await assess_difficulty(user_input)
                await chat_loop(_ws_send, user_input, thinking_type, reasoning_effort, model_id,
                                session, request_permission=_request_permission, workdir=client_workdir)
            except Exception as e:
                logger.error("WS", f"Chat 异常: {e}")
                try:
                    await websocket.send_json({"type": "done", "r": f"对话处理出错: {e}"})
                except Exception:
                    pass
            finally:
                receiver_task.cancel()
                try:
                    await receiver_task
                except asyncio.CancelledError:
                    pass

            # 上一条消息处理完了，看有没有攒着的新消息
            user_input = pending_inputs.pop(0) if pending_inputs else None
    except WebSocketDisconnect:
        perm_mgr.reset()
        logger.info("WS", f"连接断开 session={session.session_id}")
    except Exception as e:
        logger.error("WS", f"WebSocket 异常: {e}")


async def _receive_message(websocket):
    """等下一条消息。返回文本；连接超时/断开时返回 None。"""
    while True:
        try:
            msg = await asyncio.wait_for(websocket.receive_text(), timeout=IDLE_TIMEOUT)
        except asyncio.TimeoutError:
            await _send_done_and_close(websocket, "连接超时了喵~")
            return None
        except WebSocketDisconnect:
            return None
        try:
            msg_data = json.loads(msg)
        except (json.JSONDecodeError, ValueError):
            return msg  # 纯文本消息，直接当用户输入
        if isinstance(msg_data, dict) and msg_data.get("type") == "ping":
            continue  # 保活消息，忽略
        return msg


async def _send_done_and_close(websocket, text):
    """发一个 done 通知然后断开连接。"""
    try:
        await websocket.send_json({"type": "done", "r": text})
    except Exception:
        pass
    try:
        await websocket.close()
    except Exception:
        pass
