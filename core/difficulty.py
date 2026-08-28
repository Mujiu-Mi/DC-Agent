"""
对话难度评估：根据用户输入决定 AI 的思考策略

难度分三档，对应不同思考模式：
  - 简单（"你好"、"几点了"）    -> 关闭思考，省 token、响应快
  - 中等（一般问题）            -> 开启思考 + high
  - 复杂（"设计架构"、"重构"） -> 开启思考 + max effort，质量优先

先用 AI 判断；AI 判断失败时回退到关键词启发式，保证总能返回结果。
返回的 (think_mode, think_effort, model_id) 会传给 chat/stream.py。
"""
import asyncio

from core.MangerConfig import Config
from core.clients import get_client
from utils import logger

Model = Config("Config/Model.json")

# 难度 -> (think_mode, think_effort)
_DIFFICULTY_MAP = {
    "simple": ("disabled", "high"),
    "medium": ("enabled", "high"),
    "complex": ("enabled", "max"),
}

# 给 AI 判断难度用的提示词（要求它只输出一个英文单词）
_JUDGE_SYSTEM = (
    "你是任务难度评估助手。根据用户请求判断它属于哪一档难度，"
    "只输出一个英文单词，不要输出任何解释或标点：\n"
    "simple（问候闲聊、一句话简单查询）\n"
    "medium（一般问题，需要一定知识或写作）\n"
    "complex（架构设计、重构、调试、复杂分析、多步骤长任务）"
)


async def assess_difficulty(user_input):
    """
    用 AI 评估用户输入的难度。

    返回 (think_mode, think_effort, model_id) 三元组。
    空输入直接返回当前配置的思考参数。
    """
    # 先用配置里的默认思考参数兜底
    model_id = Model.model
    think_t = Model.think_mode
    think_e = Model.think_effort
    if not user_input or not user_input.strip():
        return (think_t, think_e, model_id)

    text = user_input.strip()

    # AI 判断难度（同步调用，放到线程池里避免阻塞事件循环）
    result = ""
    if Model.is_valid:
        try:
            result = await asyncio.to_thread(_judge_sync, text)
        except Exception as e:
            logger.error("DIFFICULTY", f"AI 难度判断异常: {e}")

    # 判断失败就用关键词启发式兜底
    if not result:
        result = _fallback_heuristic(text)

    if result == "simple":
        return ("disabled", "high", model_id)

    # 中等 / 复杂才开思考，并且受 auto_think 开关控制
    if result in ("medium", "complex"):
        if Model.auto_think:
            return _DIFFICULTY_MAP[result] + (model_id,)
        return (think_t, think_e, model_id)

    return (think_t, think_e, model_id)


def _judge_sync(text):
    """
    同步调用 AI 判断难度（在 asyncio.to_thread 里跑）。
    返回 "simple" / "medium" / "complex"；失败返回 "" 交给上层兜底。
    """
    entry = Model.active_entry
    fmt = entry.get("接口", "openai")
    model_name = entry.get("model_name", "")
    if not Model.is_valid or not model_name:
        return ""

    client = get_client(Model.model)
    user_prompt = (
        "请判断下面这条用户请求的难度，只回 simple / medium / complex 之一：\n"
        f"用户请求：{text[:500]}"
    )
    try:
        if fmt == "anthropic":
            resp = client.messages.create(
                model=model_name,
                max_tokens=8,
                system=_JUDGE_SYSTEM,
                messages=[{"role": "user", "content": user_prompt}],
            )
            reply = "".join(
                b.text for b in resp.content if getattr(b, "type", "") == "text"
            )
        else:
            resp = client.chat.completions.create(
                model=model_name,
                max_tokens=8,
                temperature=0,
                messages=[
                    {"role": "system", "content": _JUDGE_SYSTEM},
                    {"role": "user", "content": user_prompt},
                ],
            )
            reply = resp.choices[0].message.content or ""
    except Exception as e:
        logger.error("DIFFICULTY", f"AI 难度判断调用失败: {e}")
        return ""

    return _normalize(reply)


def _normalize(reply):
    """把 AI 回复的单词规整成难度分类，认不出就返回空串。"""
    r = (reply or "").strip().lower()
    if any(kw in r for kw in ("complex", "困难", "复杂", "难")):
        return "complex"
    if any(kw in r for kw in ("simple", "easy", "简单")):
        return "simple"
    if any(kw in r for kw in ("medium", "moderate", "中等", "一般", "普通", "正常")):
        return "medium"
    return ""


def _fallback_heuristic(text):
    """关键词启发式兜底：AI 不可用时保证难度分类稳定。"""
    # 简单问题：短消息 + 命中问候/闲聊类关键词
    simple_kw = ["你好", "hi", "hello", "再见", "拜拜", "谢谢", "thank",
                 "你是谁", "你叫什么", "几点", "几号", "日期", "今天",
                 "天气", "星期", "现在", "时间"]
    if len(text) < 15:
        low = text.lower()
        if any(kw in low for kw in simple_kw):
            return "simple"

    # 复杂问题：命中规划/分析类关键词或文本很长
    complex_kw = ["设计", "架构", "优化", "重构", "调试", "分析", "对比",
                  "方案", "规划", "策略", "复杂", "性能", "安全", "分布式",
                  "实现一个", "写一个", "开发", "架构设计", "系统设计",
                  "review", "审计", "评估", "建议", "最佳实践"]
    if len(text) > 80:
        return "complex"
    low = text.lower()
    if any(kw in low for kw in complex_kw):
        return "complex"

    return "medium"
