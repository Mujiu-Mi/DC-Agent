"""
/qq 路由：QQ 机器人接口

  - POST /qq : 一次请求一条消息，返回完整回复（非流式）
  - WebSocket /qq : 长连接，多条消息复用

和 /dscat 的区别：
  1. 走 QQ 会话池（跨请求保留上下文，按发送人/群隔离）
  2. 私信默认在沙盒目录里工作（避免 AI 乱动项目文件）
  3. 群聊禁用工具（disable_tools=True），私信只允许沙盒内工具
  4. 消息会包一层"来源 + 格式 + 长度要求"之后再发给 AI（QQ 不支持 Markdown）
"""
import os
import json
import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from utils import logger
from core.MangerConfig import Config
from core.difficulty import assess_difficulty
from chat.loop import chat_loop
from chat.session_pool import get_qq_session, qq_session_key, clear_qq_session_by_key
from routes.common_ws import ask_permission, start_receiver

router = APIRouter()
Model = Config("Config/Model.json")

# WebSocket 空闲超时（秒）
IDLE_TIMEOUT = 300

# QQ 私信默认沙盒目录：AI 在私信里调工具只能在这个目录里操作
_QQ_SANDBOX_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "qq_sandbox")
)
os.makedirs(_QQ_SANDBOX_DIR, exist_ok=True)


class QQItem(BaseModel):
    """POST /qq 的请求体"""
    sender: str
    message: str
    chat_type: str = "private"  # private=私信  group=群消息
    sender_qq: str = ""         # 发送人 QQ 号
    group_qq: str = ""          # 群消息时的群号
    group_name: str = ""        # chat_type=group 时建议带上群名
    workdir: str = ""


def _qq_source_identity(sender, chat_type="private", group_name="", sender_qq="", group_qq=""):
    """
    生成上下文里的身份标签。
    例：QQ私信-张三(123456) / QQ群消息-李四(789)@猫猫群(群123)
    AI 看上下文时能知道每条消息是谁、在哪个场景发的。
    """
    sq = f"({sender_qq})" if sender_qq else ""
    if chat_type == "group":
        gn = group_name.strip()
        gq = group_qq.strip()
        if gn and gq:
            gtag = f"{gn}({gq})"
        elif gq:
            gtag = f"群{gq}"
        else:
            gtag = gn or "群"
        return f"QQ群消息-{sender}{sq}@{gtag}"
    return f"QQ私信-{sender}{sq}"


def _qq_current_message(sender, message, chat_type="private", group_name="", sender_qq="", group_qq=""):
    """
    生成真正发给 AI 的"当前消息"（带来源/格式/长度要求）。

    AI 回复聊天消息时用这套格式，群里还要明确告知：禁止调工具。
    """
    sq = f" QQ号:{sender_qq}" if sender_qq else ""
    if chat_type == "group":
        gn, gq = group_name.strip(), group_qq.strip()
        if gn and gq:
            loc = f"QQ群聊-{gn}(群号:{gq})"
        elif gn:
            loc = f"QQ群聊-{gn}"
        elif gq:
            loc = f"QQ群聊(群号:{gq})"
        else:
            loc = "QQ群聊"
        tool_note = ("说明：当前为QQ群聊消息。群聊场景下【禁止调用任何工具】，请直接用文字回复；"
                     "如确需工具才能回答，请明确告知用户该需求请在私信中提出。")
    else:
        loc = "QQ私信"
        tool_note = ""

    out = (f"[来源:QQ即时通讯] [会话类型:{loc}] [发送人:{sender}{sq}] [内容:{message}]\n"
           f"说明：以上是一条来自 {loc} 的 QQ 消息，发送人名字与 QQ 号已标注。"
           f"请在回复时体现你已知晓消息来源场景（群聊/私信）与发送人，并自然称呼。")
    if tool_note:
        out += "\n" + tool_note
    out += ("\n格式要求：QQ 不支持 Markdown 渲染，回复时【禁止使用 Markdown 格式】"
            "（不要用 #标题、**加粗**、`代码`、```代码块```、- 列表、>引用、|表格|、--- 分隔线 等），"
            "请用纯文本+换行+普通标点排版。")
    out += ("\n长度要求：QQ 消息回复必须简洁，能少字绝不多字。"
            "直奔答案，不寒暄不啰嗦，不用长篇解释，尽量一两句话说完。")
    return out


@router.post("/qq")
async def qq_post(item: QQItem):
    """
    HTTP POST 路径：QQ 机器人把一条消息转过来，返回 AI 的完整回复。
    内部仍是流式的，只是把所有 chunk 收集完再一次性返回。
    """
    sender = item.sender.strip() or "未知发送人"
    message = item.message
    chat_type = item.chat_type if item.chat_type in ("private", "group") else "private"
    thinking_type, reasoning_effort, model_id = await assess_difficulty(message)

    # 私信默认走沙盒目录；收到消息时也接受外部传的 workdir
    qq_workdir = _QQ_SANDBOX_DIR
    if item.workdir and item.workdir.strip():
        qq_workdir = item.workdir.strip()

    # 从会话池拿会话（私信按发送人、群聊按群隔离）
    session = await get_qq_session(chat_type, item.sender_qq, item.group_qq, qq_workdir)

    # 收集事件：chunk -> 回复文本；tool/step/tool_result/done -> 结构化返回
    chunks = []
    tool_events = []
    step_events = []
    tool_result_events = []
    done_event = {}

    async def _collect(data):
        t = data.get("type")
        if t == "chunk":
            c = data.get("c", "")
            if c:
                chunks.append(c)
        elif t == "tool":
            tool_events.append({"n": data.get("n"), "a": data.get("a")})
        elif t == "step_progress":
            step_events.append({"step": data.get("step"), "title": data.get("title", ""),
                                "status": data.get("status", ""), "detail": data.get("detail", "")})
        elif t == "tool_result":
            payload = {"n": data.get("n"), "r": data.get("r", "")}
            if data.get("truncated"):
                payload["truncated"] = True
            tool_result_events.append(payload)
        elif t == "done":
            nonlocal done_event
            done_event = data

    try:
        await chat_loop(
            _collect, message, thinking_type, reasoning_effort, model_id, session,
            workdir=qq_workdir,
            source_identity=_qq_source_identity(sender, chat_type, item.group_name,
                                                item.sender_qq, item.group_qq),
            current_msg=_qq_current_message(sender, message, chat_type, item.group_name,
                                            item.sender_qq, item.group_qq),
            disable_tools=(chat_type == "group"),
        )
    except Exception as e:
        logger.error("QQ", f"Chat 异常: {e}")
        return {"type": "done", "r": "", "reply": "".join(chunks),
                "error": f"对话处理出错: {e}",
                "tools": tool_events, "steps": step_events, "tool_results": tool_result_events}

    return {
        "type": "done",
        "reply": "".join(chunks),
        "r": done_event.get("r", "".join(chunks)),
        "tools": tool_events,
        "steps": step_events,
        "tool_results": tool_result_events,
        "p": done_event.get("p", 0), "c": done_event.get("c", 0),
        "h": done_event.get("h", 0), "mh": done_event.get("mh", 0),
        "hr": done_event.get("hr", "0.0%"),
        "th": done_event.get("th", "off"),
        "te": done_event.get("te", ""),
        "ts": done_event.get("ts", 0), "tc": done_event.get("tc", 0),
        "model_id": done_event.get("model_id", model_id),
        "model_name": done_event.get("model_name", ""),
        "接口": done_event.get("接口", ""),
    }


@router.websocket("/qq")
async def qq_ws(websocket: WebSocket):
    """WebSocket 路径：QQ 机器人保持长连接，一条连接里传多条消息。"""
    await websocket.accept()
    logger.info("QQ-WS", "新连接")

    # ━━━━ 握手：等 init 消息（可选传 workdir） ━━━━
    client_workdir = _QQ_SANDBOX_DIR
    try:
        first = await asyncio.wait_for(websocket.receive_text(), timeout=IDLE_TIMEOUT)
        try:
            init_data = json.loads(first)
        except (json.JSONDecodeError, ValueError):
            await websocket.send_json({"type": "done", "r": "QQ 连接需先发送 init 消息"})
            await websocket.close()
            return
        else:
            if isinstance(init_data, dict) and init_data.get("type") == "init":
                w = (init_data.get("workdir", "") or "").strip()
                if w:
                    client_workdir = w
                logger.info("QQ-WS", f"工作目录设置: {client_workdir}")
                await websocket.send_json({"type": "init_ack", "workdir": client_workdir})
    except asyncio.TimeoutError:
        await _send_done_and_close(websocket)
        return

    # ━━━━ 主循环：收消息 -> 跑对话 ━━━━
    try:
        while True:
            try:
                raw = await asyncio.wait_for(websocket.receive_text(), timeout=IDLE_TIMEOUT)
            except asyncio.TimeoutError:
                await _send_done_and_close(websocket)
                return
            except WebSocketDisconnect:
                return

            try:
                msg_data = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                await websocket.send_json({"type": "done", "r": "消息格式需为 JSON: {sender, message}"})
                continue

            if not isinstance(msg_data, dict):
                continue
            if msg_data.get("type") == "ping":
                continue
            if msg_data.get("type") == "clear":
                # 清指定会话的上下文
                ct = msg_data.get("chat_type", "private")
                if ct not in ("private", "group"):
                    ct = "private"
                ck = qq_session_key(ct, str(msg_data.get("sender_qq", "")), str(msg_data.get("group_qq", "")))
                await clear_qq_session_by_key(ck)
                await websocket.send_json({"type": "done", "r": "上下文已清除", "cmd": "clear"})
                continue

            sender = str(msg_data.get("sender", "")).strip() or "未知发送人"
            message = str(msg_data.get("message", ""))
            chat_type = msg_data.get("chat_type", "private")
            if chat_type not in ("private", "group"):
                chat_type = "private"
            sender_qq = str(msg_data.get("sender_qq", ""))
            group_qq = str(msg_data.get("group_qq", ""))
            group_name = str(msg_data.get("group_name", ""))
            if not message.strip():
                continue

            # 从会话池取会话，保留多轮上下文
            session = await get_qq_session(chat_type, sender_qq, group_qq, client_workdir)

            pending_inputs = []
            perm_resp_q = asyncio.Queue(maxsize=1)

            async def _ws_send(data):
                await websocket.send_json(data)

            async def _request_permission(name, args):
                return await ask_permission(websocket, session, perm_resp_q, name, args)

            receiver_task = start_receiver(websocket, pending_inputs, perm_resp_q)
            try:
                thinking_type, reasoning_effort, model_id = await assess_difficulty(message)
                await chat_loop(_ws_send, message, thinking_type, reasoning_effort, model_id,
                                session, request_permission=_request_permission, workdir=client_workdir,
                                source_identity=_qq_source_identity(sender, chat_type, group_name,
                                                                    sender_qq, group_qq),
                                current_msg=_qq_current_message(sender, message, chat_type, group_name,
                                                                sender_qq, group_qq),
                                disable_tools=(chat_type == "group"))
            except Exception as e:
                logger.error("QQ-WS", f"Chat 异常: {e}")
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

    except WebSocketDisconnect:
        logger.info("QQ-WS", "连接断开")
    except Exception as e:
        logger.error("QQ-WS", f"WebSocket 异常: {e}")


async def _send_done_and_close(websocket, text="连接超时了喵~"):
    try:
        await websocket.send_json({"type": "done", "r": text})
    except Exception:
        pass
    try:
        await websocket.close()
    except Exception:
        pass
