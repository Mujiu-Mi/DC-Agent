"""
AI 流式输出核心：把模型的流式回复转成"事件"，一个接一个 yield 给上层。

为什么需要线程 + 队列？
  openai/anthropic 的 SDK 都是同步函数，调它会等模型把所有内容返回，
  但我们要的是"模型出一段就立刻发给客户端"。
  做法：
    1. 在线程池里起一个"生产者线程"：它同步地消费模型流，
       每来一段内容就塞进 queue.Queue（put 是线程安全的）
    2. 主协程作为"消费者"：用 asyncio.to_thread 反复从队列取，
       边取边 yield（这样事件循环不阻塞，还能跳到别的会话去干活）
  queue 就是线程和协程之间的"转交站"。

事件类型（dict）：
  {"type": "chunk", "c": "..."}     - 正文片段
  {"type": "think", "c": "..."}     - AI 思考过程片段
  {"type": "_result", ...}          - 一轮流结束（含完整文本/工具调用/用量/思考统计）
"""
import json
import queue
import asyncio
import concurrent.futures

from core.MangerConfig import Config
from core.prompts import build_system_prompt
from core.clients import get_client
from utils import logger
from tools.mcp_client import hub as mcp_hub

# 全局配置（启动时读一次，改配置要调 Model.reload()）
Model = Config("Config/Model.json")

# 生产线程池：所有模型的流式调用都扔这里同步执行
_executor = concurrent.futures.ThreadPoolExecutor(max_workers=64, thread_name_prefix="producer")


def select_tools(user_msg):
    """返回全部可用工具（内置 + 外部 MCP），不按关键词筛选。"""
    return mcp_hub.list_all_tools()


def _convert_tools_for_anthropic(selected_tools):
    """把 OpenAI 的 tools schema 翻译成 Anthropic 格式（字段名不同）。"""
    result = []
    for t in selected_tools:
        f = t["function"]
        result.append({
            "name": f["name"],
            "description": f["description"],
            "input_schema": {k: v for k, v in f["parameters"].items() if k != "additionalProperties"},
        })
    return result


# ============================================================
# 生产者：在后台线程里"生产"事件
# ============================================================

def _producer_openai(t_queue, model_name, prompt, selected_tools, think_type, think_effort, client):
    """
    OpenAI 兼容接口的流式生产线程。

    把模型返回的每个 chunk 塞进 t_queue，最后塞一个 {"_done": True, ...} 表示结束。
    过程中累计：正文 / 思考 / 工具调用 / token 用量。
    """
    local_full = ""                       # 正文拼接
    local_tool = {}                       # {index: {"id":.., "function": {"name":.., "arguments":..}}}
    local_usage = {"p": 0, "c": 0, "h": 0, "m": 0}   # prompt/completion/cache_hit/cache_miss
    local_think = 0                       # 思考行数（当作"思考片段数"）
    local_think_chars = 0                 # 思考总字数
    is_deepseek = "deepseek" in str(client.base_url).lower()

    # 构造请求
    try:
        kwargs = dict(
            model=model_name,
            messages=[
                {"role": "system", "content": build_system_prompt()},
                {"role": "user", "content": prompt},
            ],
            tools=selected_tools or None,
            tool_choice="auto" if selected_tools else None,
            stream=True,
        )
        # DeepSeek 的思考模式是特例：要走 reasoning_effort + thinking 两个参数
        if is_deepseek and think_type == "enabled":
            kwargs["reasoning_effort"] = think_effort
            kwargs["extra_body"] = {"thinking": {"type": think_type}}
        stream = client.chat.completions.create(**kwargs)
    except Exception as e:
        t_queue.put({"error": str(e)})
        return

    # 消费流：逐 chunk 解析
    for chunk in stream:
        try:
            # 用量字段只在某些 chunk 出现，重复出现就覆盖
            if hasattr(chunk, "usage") and chunk.usage:
                u = chunk.usage
                local_usage["p"] = getattr(u, "prompt_tokens", 0) or 0
                local_usage["c"] = getattr(u, "completion_tokens", 0) or 0
                local_usage["h"] = getattr(u, "prompt_cache_hit_tokens", 0) or 0
                local_usage["m"] = getattr(u, "prompt_cache_miss_tokens", 0) or 0
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            # 思考内容（DeepSeek 的 reasoning_content 字段）
            reasoning = getattr(delta, "reasoning_content", None) or ""
            if reasoning:
                local_think_chars += len(reasoning)
                lines = [line for line in reasoning.split("\n") if line.strip()]
                local_think += len(lines)
                t_queue.put({"type": "think", "c": reasoning})

            # 正文
            text = delta.content or ""
            if text:
                local_full += text
                t_queue.put({"type": "chunk", "c": text})

            # 工具调用：按 index 累积，name 和 arguments 会分多片到达
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in local_tool:
                        local_tool[idx] = {"id": "", "function": {"name": "", "arguments": ""}}
                    if tc.id:
                        local_tool[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            local_tool[idx]["function"]["name"] += tc.function.name
                        if tc.function.arguments:
                            local_tool[idx]["function"]["arguments"] += tc.function.arguments
        except Exception as e:
            logger.error("STREAM", f"处理 chunk 异常: {e}")
            continue

    # 流结束：组装最终结果
    tool_list = [local_tool[i] for i in sorted(local_tool) if local_tool[i]["function"]["name"]] if local_tool else []
    t_queue.put({
        "_done": True,
        "text": local_full,
        "tools": tool_list,
        "usage": local_usage,
        "think_seg": local_think if local_think_chars else 0,
        "think_chars": local_think_chars,
    })


def _producer_anthropic(t_queue, model_name, prompt, selected_tools, think_type, client):
    """
    Anthropic Claude 的流式生产线程。

    逻辑和 OpenAI 路径一样，只是 Claude 的事件类型完全不同：
    message_start / content_block_start / content_block_delta / message_delta。
    """
    local_full = ""
    local_tool_map = {}
    local_usage = {"p": 0, "c": 0, "h": 0, "m": 0}
    local_think = 0
    local_think_chars = 0

    try:
        kwargs = dict(
            model=model_name,
            max_tokens=8192,
            system=build_system_prompt(),
            messages=[{"role": "user", "content": prompt}],
            stream=True,
        )
        if selected_tools:
            kwargs["tools"] = _convert_tools_for_anthropic(selected_tools)
            kwargs["tool_choice"] = {"type": "auto"}
        if think_type == "enabled":
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": 4096}
        stream = client.messages.create(**kwargs)
    except Exception as e:
        t_queue.put({"error": str(e)})
        return

    for event in stream:
        try:
            if event.type == "message_start":
                # 输入 token 数在这里出现
                if hasattr(event, "message") and hasattr(event.message, "usage") and event.message.usage:
                    local_usage["p"] = getattr(event.message.usage, "input_tokens", 0) or 0

            elif event.type == "content_block_start":
                block = event.content_block
                t = getattr(block, "type", "")
                if t == "thinking":
                    txt = getattr(block, "thinking", "") or ""
                    local_think_chars += len(txt)
                    local_think += len([l for l in txt.split("\n") if l.strip()])
                    t_queue.put({"type": "think", "c": txt})
                elif t == "tool_use":
                    local_tool_map[event.index] = {
                        "id": block.id, "name": block.name, "input_text": "",
                    }

            elif event.type == "content_block_delta":
                delta = event.delta
                dt = getattr(delta, "type", "")
                if dt == "thinking_delta":
                    txt = getattr(delta, "thinking", "") or ""
                    local_think_chars += len(txt)
                    local_think += len([l for l in txt.split("\n") if l.strip()])
                    t_queue.put({"type": "think", "c": txt})
                elif dt == "input_json_delta":
                    # 工具调用的 arguments 分片到达
                    idx = event.index
                    if idx in local_tool_map:
                        local_tool_map[idx]["input_text"] += (getattr(delta, "partial_json", "") or "")
                elif dt == "text_delta":
                    text = getattr(delta, "text", "") or ""
                    if text:
                        local_full += text
                        t_queue.put({"type": "chunk", "c": text})

            elif event.type == "message_delta":
                # 输出 token 数在这里出现
                if hasattr(event, "usage") and event.usage:
                    local_usage["c"] = getattr(event.usage, "output_tokens", 0) or 0
        except Exception:
            continue

    # 组装工具调用结果（Claude 拿到的 arguments 是一段无效 JSON 文本，先拼好再解析）
    tool_list = []
    for idx in sorted(local_tool_map):
        t = local_tool_map[idx]
        try:
            input_obj = json.loads(t["input_text"]) if t["input_text"] else {}
        except json.JSONDecodeError:
            input_obj = {}
        tool_list.append({
            "id": t["id"],
            "function": {"name": t["name"], "arguments": json.dumps(input_obj)},
        })
    t_queue.put({
        "_done": True,
        "text": local_full,
        "tools": tool_list,
        "usage": local_usage,
        "think_seg": local_think if local_think_chars else 0,
        "think_chars": local_think_chars,
    })


# ============================================================
# 对外接口：流式生成器
# ============================================================

async def stream_events(myself, user_msg, context, forever_mem,
                        model_id, think_type, think_effort, use_tools=True, tool_request_text=None):
    """
    异步生成器：每次 yield 一个事件 dict。

    参数：
      myself:      Prompt/myself.md 的内容（AI 的人设）
      user_msg:    当前用户消息（拼在 prompt 末尾；工具回灌轮次可能为空）
      context:     本会话累积的历史上下文
      forever_mem: 长期记忆
      model_id:    模型 ID（Config/Model.json 里的 key）
      think_type:  "enabled" / "disabled"
      think_effort: "high" / "max" 等
      use_tools:   本轮是否允许 AI 调工具
      tool_request_text: 工具回灌轮 user_msg 为空时，仍用它来取工具清单
    """
    entry = Model.get_entry(model_id)
    model_name = entry.get("model_name", "")
    fmt = entry.get("接口", "openai")
    selected_tools = select_tools(tool_request_text or user_msg) if use_tools else []
    selected_names = [tool_def["function"]["name"] for tool_def in selected_tools]
    logger.info("STREAM",
                f"开始流式: model_id={model_id} fmt={fmt} model={model_name} tools={selected_names}")

    # 把"长期记忆 + 人设 + 历史上下文 + 当前消息"拼成一次调用的 prompt
    prompt = f"//长期记忆内容:{forever_mem} //myself:{myself}  //上下文//:{context}"
    if user_msg:
        prompt += f"  //当前对话内容//:{user_msg}"

    t_queue = queue.Queue()
    loop = asyncio.get_running_loop()

    # 生产者：扔线程池同步调用 AI 的流式接口
    def _producer():
        try:
            client = get_client(model_id)
            if fmt == "anthropic":
                _producer_anthropic(t_queue, model_name, prompt, selected_tools, think_type, client)
            else:
                _producer_openai(t_queue, model_name, prompt, selected_tools, think_type, think_effort, client)
        except Exception as e:
            logger.error("STREAM", f"生产者线程异常: {e}")
            t_queue.put({"error": f"生产者异常: {e}"})

    loop.run_in_executor(_executor, _producer)

    # 消费者：从队列拿事件，一个一个 yield 出去
    while True:
        data = await asyncio.to_thread(_read_event, t_queue)
        if data == "TIMEOUT":
            logger.error("STREAM", "读取队列超时（120s）")
            yield {"type": "chunk", "c": "[错误: AI 响应超时]"}
            yield {"type": "_result", "text": "", "tools": [],
                   "usage": {"p": 0, "c": 0, "h": 0, "m": 0}, "think_seg": 0}
            return
        if data is None:
            break  # 队列正常关闭
        if isinstance(data, dict) and data.get("error"):
            yield {"type": "chunk", "c": f"[API 请求失败: {data['error']}]"}
            yield {"type": "_result", "text": "", "tools": [],
                   "usage": {"p": 0, "c": 0, "h": 0, "m": 0}, "think_seg": 0}
            return
        if isinstance(data, dict) and data.get("_done"):
            yield {"type": "_result", "text": data["text"], "tools": data["tools"],
                   "usage": data["usage"], "think_seg": data["think_seg"],
                   "think_chars": data.get("think_chars", 0)}
            return
        yield data


def _read_event(t_queue):
    """从队列取一个事件（最多等 120 秒），超时返回字符串 'TIMEOUT'。"""
    try:
        return t_queue.get(timeout=120)
    except queue.Empty:
        return "TIMEOUT"
