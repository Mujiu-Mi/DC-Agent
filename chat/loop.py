"""
统一的聊天循环

不管消息来自 /dscat HTTP、/dscat WebSocket 还是 /qq，都走这个 chat_loop。
它是一个"循环"：
  1. 调 AI 拿流式回复（chat/stream.py 的 stream_events）
  2. 如果 AI 没调工具 -> 发 done 事件，本轮结束
  3. 如果 AI 调了工具：
     a. 先把 "report_step" 剥出来（进度汇报，只通知前端不真执行）
     b. 过审计 / 权限决定哪些工具可用：
        - Model.auto_audit=True 时走 AI 二次审核（默认关）
        - 走 request_permission 回调让前端弹窗询问（WebSocket 路径）
        - 另外 safety.py 的规则拦截在 tools/builtin_server.py 里始终生效
     c. 执行工具，把结果加回上下文
     d. 回到第 1 步，让 AI 带着工具结果继续想（最多 MAX_TOOL_ROUNDS 轮）
"""
import json
import asyncio
import re

from utils import logger
from core import auditor
from memory import memory_manager as mm
from core.MangerConfig import Config
from tools.tool_handler import if_tool, _FakeMsg
from core.session_context import SessionContext
from core.prompts import read_txt
from core.summary import call_summary, summarize_context_overflow, summarize_10rounds
from chat.stream import stream_events

Model = Config("Config/Model.json")

# 全局 token 用量统计（key=model_id, value={"tokens": int}），给 /usage_stats 接口用
usage_stats = {}

# 工具最多连续调用几轮：读 -> 改 -> 验证这种多步任务需要多次调工具，
# 设上限防止模型因异常结果无限循环
MAX_TOOL_ROUNDS = 6

# AI 说"已保存文件"但实际没执行写文件时的识别（中文常见表述）
_FILE_SAVE_CLAIM = re.compile(
    r"(?:已|已经|我已|我已经).{0,12}(?:保存|写入|创建).{0,12}(?:文件|代码|网页|页面)?|"
    r"(?:保存|写入|创建)(?:好了|完成|成功)",
)


def _claims_file_saved(text):
    """判断 AI 是否宣称已保存文件（用来做校验提示）。"""
    return bool(_FILE_SAVE_CLAIM.search(text or ""))


def after_dialog(session):
    """每轮对话结束后跑：检查是否需要压缩上下文 / 提取长期记忆。"""
    session.increment_dialog()
    try:
        if session.is_context_overflow():
            summarize_context_overflow(session)
        if session.should_summarize_memory():
            summarize_10rounds(session)
    except Exception as e:
        logger.error("AFTER_DIALOG", f"对话后处理异常: {e}")


async def chat_loop(send_event, user_input, thinking_type, reasoning_effort, model_id,
                    session, request_permission=None, workdir="",
                    source_identity="User", current_msg=None, disable_tools=False):
    """
    主循环：调 AI -> 收工具调用 -> 执行工具 -> 结果喂回去 -> 直到 AI 出纯文本。

    参数：
      send_event:      async 回调，把事件 dict 发给客户端（WS send / SSE yield / QQ 收 chunks）
      user_input:      用户这一轮的原始输入
      thinking_type:   "enabled" / "disabled"
      reasoning_effort: "high" / "max" 等
      model_id:        模型 ID
      session:         本会话的上下文管理器（每会话一个）
      request_permission: async 回调，询问工具权限；None 表示不询问（走 auto_audit）
      workdir:         当前工作目录（工具只能在这个目录内操作）
      source_identity: 上下文里记录的身份标签（如 "QQ私信-张三(123)"）
      current_msg:     真正发给 AI 的"当前消息"（含来源/格式要求，QQ 场景用）；None 时用 user_input
      disable_tools:   True = 禁止调用任何工具（QQ 群聊用）

    返回：
      最终答复的纯文本
    """
    logger.info("CHAT", f"model_id={model_id} session={session.session_id} "
                        f"workdir={workdir} source={source_identity} disable_tools={disable_tools}")

    # 工作目录只在首次出现或变化时写入，保持历史提示词可被缓存
    session.set_workdir(workdir)
    session.add_context(source_identity, user_input)

    total_usage = {"p": 0, "c": 0, "h": 0, "m": 0}   # 本轮 token 用量合计
    total_think = 0            # 思考片段数
    total_think_chars = 0      # 思考总字数
    first_round = True         # 是否第一轮（第一轮才拼"当前消息"）
    tool_round = 0             # 已经执行了几轮工具
    reply_text = ""            # AI 最终文本
    successful_file_writes = []  # 本轮真的写成功的文件名（用于校验）

    while True:
        # ── 1. 调 AI，收流式事件 ──
        allow_tools = (not disable_tools) and tool_round < MAX_TOOL_ROUNDS
        tool_calls = None
        async for evt in stream_events(
            read_txt("Prompt/myself.md"),                                  # 人设
            (current_msg if current_msg is not None else user_input) if first_round else "",  # 当前消息
            session.get_context(),                                         # 历史上下文
            mm.get_forever_memory(),                                       # 长期记忆
            model_id, thinking_type, reasoning_effort,
            use_tools=allow_tools,
            tool_request_text=user_input,
        ):
            if evt.get("type") == "_result":
                # 流结束：累计用量 / 思考统计，记录工具调用
                for key in total_usage:
                    total_usage[key] += evt["usage"][key]
                total_think += evt["think_seg"]
                total_think_chars += evt.get("think_chars", 0)
                tool_calls = evt["tools"]
                reply_text = evt["text"]
                break
            await send_event(evt)

        # ── 2. AI 没调工具：发 done，本轮结束 ──
        if not tool_calls:
            # AI 说保存了但实际没执行写文件 -> 提醒一下
            if _claims_file_saved(reply_text) and not successful_file_writes:
                reply_text += "\n\n[系统校验：本轮没有成功执行 write_file 或 edit_file，文件尚未保存。]"
            if reply_text:
                session.add_context("Model", reply_text)
            hit_rate = (total_usage["h"] / total_usage["p"] * 100) if total_usage["p"] else 0
            if model_id in usage_stats:
                usage_stats[model_id]["tokens"] += total_usage["p"] + total_usage["c"]
            entry = Config("Config/Model.json").get_entry(model_id)  # 拿最新的模型配置
            await send_event({
                "type": "done",
                "p": total_usage["p"], "c": total_usage["c"],
                "h": total_usage["h"], "mh": total_usage["m"],
                "hr": f"{hit_rate:.1f}%",
                "th": "on" if thinking_type == "enabled" else "off",
                "te": reasoning_effort if thinking_type == "enabled" else "",
                "ts": total_think, "tc": total_think_chars,
                "model_id": model_id,
                "model_name": entry.get("model_name", ""),
                "接口": entry.get("接口", ""),
            })
            if session.perm_mgr:
                session.perm_mgr.end_turn()
            asyncio.create_task(asyncio.to_thread(after_dialog, session))  # 后台收尾，不阻塞回复
            return reply_text

        # ── 3. AI 调了工具：先剥出 report_step（进度通知，不执行） ──
        tool_results = []

        def _collect(identity, ctx):
            session.add_context(identity, ctx)
            tool_results.append((identity, ctx))

        step_reports = []
        for tc in tool_calls:
            if tc["function"]["name"] == "report_step":
                try:
                    sa = json.loads(tc["function"]["arguments"])
                except json.JSONDecodeError:
                    sa = {}
                try:
                    sa["step"] = int(sa.get("step", 0))
                except (TypeError, ValueError):
                    sa["step"] = 0
                await send_event({"type": "step_progress", "step": sa["step"],
                                  "title": sa.get("title", ""), "status": sa.get("status", "started"),
                                  "detail": sa.get("detail", "")})
                step_reports.append((tc, sa))
        for tc, sa in step_reports:
            tool_calls.remove(tc)
            msg = f"[步骤 {sa.get('step', '?')}] {sa.get('title', '')} - {sa.get('status', '')}"
            if sa.get("detail"):
                msg += f" ({sa['detail']})"
            session.add_context("进度汇报", msg)

        # ── 4. 审计 / 权限：决定哪些工具允许执行 ──
        approved_tools = []
        denied_reasons = []   # [(工具名, 拒绝原因), ...]
        for tc in tool_calls:
            try:
                args = json.loads(tc["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}
            name = tc["function"]["name"]
            logger.info("CHAT", f"执行工具: {name}")

            skip = False
            if Model.auto_audit:
                # AI 二次审核：让模型判断这次调用是否安全
                allowed, reason = auditor.check(name, args, session.get_recent_context(10), call_summary)
                if allowed:
                    logger.info("AUDIT", f"通过 {name}: {reason}")
                else:
                    logger.info("AUDIT", f"拒绝 {name}: {reason}")
                    denied_reasons.append((name, reason))
                    skip = True
            elif request_permission:
                # 客户端弹窗询问（WebSocket 路径用）
                mode = await request_permission(name, args)
                if mode == "deny":
                    skip = True

            if not skip:
                approved_tools.append(tc)

        for name, reason in denied_reasons:
            session.add_context(name, f"[安全审核拒绝] {reason}")

        # 工具全被拒 -> 回循环顶部让 AI 重新决定
        if not approved_tools:
            tool_round += 1
            continue

        # ── 5. 通知前端"要执行哪些工具"，然后执行 ──
        for tc in approved_tools:
            name = tc["function"]["name"]
            await send_event({"type": "tool", "n": name, "a": json.loads(tc["function"]["arguments"])})

        # if_tool 是同步的（执行工具可能要跑命令/读文件），扔线程池跑
        await asyncio.to_thread(if_tool, _FakeMsg(approved_tools), _collect, session.perm_mgr, workdir)

        # ── 6. 把工具结果发回前端（长结果截断到 500 字）──
        for name, result in tool_results:
            full = str(result)
            logger.info("CHAT", f"工具结果: {name}: {full[:300]}")
            if name == "write_file" and full.startswith("文件已成功写入"):
                successful_file_writes.append(full)
            elif name == "edit_file" and full.startswith("文件已精确修改"):
                successful_file_writes.append(full)
            payload = {"type": "tool_result", "n": name, "r": full[:500]}
            if len(full) > 500:
                payload["truncated"] = True
            await send_event(payload)

        # 工具执行完，回到第 1 步：带着结果继续调 AI
        first_round = False
        tool_round += 1
