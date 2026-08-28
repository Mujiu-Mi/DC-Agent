"""
/dscat 和 /qq 两只 WebSocket 路由共用的辅助：
  1. ask_permission：工具权限询问（发 permission_request，等 permission_response）
  2. start_receiver：后台任务持续收消息，别的消息塞进待处理列表
"""
import asyncio
import json

from fastapi import WebSocketDisconnect

from core.permission_manager import extract_tool_path


async def ask_permission(websocket, session, perm_resp_q, name, args):
    """
    向客户端询问工具权限。

    返回：
      "deny"  - 拒绝
      "once"  - 本轮允许
      "always" - 本会话都允许（同时写入权限管理器，下次不再问）

    客户端 60 秒内没回答就按拒绝处理。
    """
    target = extract_tool_path(name, args) or name
    await websocket.send_json({
        "type": "permission_request", "tool": name, "args": args, "path": target,
    })
    try:
        resp = await asyncio.wait_for(perm_resp_q.get(), timeout=60)
        mode = resp.get("mode", "deny")
    except asyncio.TimeoutError:
        mode = "deny"

    # 客户端答的是 allow_once / allow_always，转成我们的术语
    if mode == "allow_once":
        mode = "once"
    elif mode == "allow_always":
        mode = "always"
    else:
        mode = "deny"

    if mode in ("once", "always"):
        session.perm_mgr.grant(target, mode)
    return mode


def start_receiver(websocket, pending_inputs, perm_resp_q):
    """
    开一个后台收消息任务：
      - permission_response -> 放进权限响应队列
      - ping -> 忽略（保活）
      - 其他 -> 放进 pending_inputs 列表（主循环跑完上一条消息后再处理）
    """
    async def _loop():
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    pending_inputs.append(raw)
                    continue
                if msg.get("type") == "permission_response":
                    try:
                        perm_resp_q.put_nowait(msg)
                    except asyncio.QueueFull:
                        pass  # 队列满了说明客户端发太快，忽略多余的回答
                elif msg.get("type") == "ping":
                    pass
                else:
                    pending_inputs.append(raw)
        except (WebSocketDisconnect, RuntimeError):
            pass  # 连接断开就自然退出

    return asyncio.create_task(_loop())
