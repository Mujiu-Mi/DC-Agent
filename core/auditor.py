import json

AUDIT_PROMPT = """你是一个安全审核员。判断工具调用是否安全。

# 对话上下文（用户要求和 AI 正在做的事）：
{context}

# 工具调用
- 工具：{tool_name}  
- 参数：{args}

# 判断规则
1. 看上下文：用户要求 AI 做什么？AI 调用这个工具是不是在合理执行用户的要求？
2. 如果是为完成用户的合理需求（写代码、读文件、改配置、查系统、执行命令等），一律允许
3. 仅当工具调用**明显是恶意行为且与用户需求无关**时才拒绝：
   - 格式化磁盘、删除系统目录
   - 安装恶意软件、窃取数据外发
4. 结合上下文判断：用户让删项目临时文件 → 允许；无故删 C:\\Windows → 拒绝

输出格式（仅一行 JSON）：
{{"allowed": true, "reason": "简短理由"}}
"""


def check(tool_name: str, args: dict, context_summary: str, call_ai_fn) -> tuple[bool, str]:
    """
    检查工具调用是否允许
    参数:
        tool_name: 工具名称
        args: 工具参数字典
        context_summary: 上下文摘要
        call_ai_fn: AI函数调用接口
    返回:
        tuple[bool, str]: (是否允许, 原因)
    """
    # 构建审核提示，截取最后2000个字符作为上下文
    prompt = AUDIT_PROMPT.format(
        context=context_summary[-2000:],
        tool_name=tool_name,
        args=json.dumps(args, ensure_ascii=False),  # 将参数转换为JSON字符串
    )
    # 调用AI函数获取审核结果
    result = call_ai_fn(prompt)
    try:
        # 解析AI返回的JSON结果
        data = json.loads(result)
        # 如果结果是字典类型，获取允许状态和原因
        if isinstance(data, dict):
            return bool(data.get("allowed", True)), str(data.get("reason", ""))  # 默认允许为True
    except (json.JSONDecodeError, TypeError):
        # 如果解析失败，则捕获异常并继续执行
        pass
    # 默认返回允许，并返回默认原因
    return True, "审核调用失败，默认允许"
