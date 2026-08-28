"""
AI 总结能力

三个场景都用当前激活模型（Model.model）：
  1. summarize_context_overflow : 上下文超 256K 时压缩成摘要
  2. summarize_10rounds         : 每 10 轮对话提取长期记忆
  3. summarize_daily            : 每天 23:30 总结当天对话（调度器触发）

核心是 call_summary(prompt)：非流式调一次 AI，拿文本结果。
"""
from core.clients import get_client
from core.MangerConfig import Config
from memory import memory_manager as mm
from utils import logger

Model = Config("Config/Model.json")


def call_summary(prompt):
    """
    用当前激活模型跑一次非流式调用，返回文本结果；失败返回空串。

    参数:
        prompt: 要给 AI 处理的完整提示词
    """
    mid = Model.model
    entry = Model.get_entry(mid)
    fmt = entry.get("接口", "openai")
    model_name = entry.get("model_name", "")
    try:
        client = get_client(mid)
        if fmt == "anthropic":
            resp = client.messages.create(
                model=model_name,
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text if resp.content else ""
        resp = client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        return resp.choices[0].message.content or ""
    except Exception as e:
        logger.error("SUMMARY", f"总结调用失败: {e}")
        return ""


def summarize_context_overflow(session):
    """上下文超限时：整段对话压成摘要，清空后把摘要塞回去。"""
    ctx = session.get_context()
    if not ctx.strip():
        return
    summary = call_summary("以下是一段 AI 对话的上下文记录。请将其总结为一段简洁的摘要，保留关键信息：\n\n" + ctx)
    if summary:
        session.clear_context()
        session.add_context("System", "[上下文摘要]" + summary)


def summarize_10rounds(session):
    """每 10 轮对话：把值得长期记住的信息（偏好、事实等）写入长期记忆。"""
    recent = session.get_recent_context(30)
    if not recent.strip():
        session.reset_dialog_count()
        return
    result = call_summary("以下是最近的对话。提取值得长期记住的信息（偏好、事实等）：\n\n" + recent)
    if result:
        mm.append_forever_memory(result)
    session.reset_dialog_count()


def summarize_daily():
    """每日总结：把今天的全部对话总结成要点，存到每日记忆。由调度器触发。"""
    dialogs = mm.get_today_dialogs()
    if not dialogs or "暂无" in dialogs:
        return
    summary = call_summary("总结今天对话的主要内容，以要点形式输出：\n\n" + dialogs)
    if summary:
        mm.save_daily_memory(summary)


async def job_daily_summary():
    """apscheduler 调度的异步包装：把同步的 summarize_daily 扔线程池跑。"""
    import asyncio
    await asyncio.get_event_loop().run_in_executor(None, summarize_daily)
