"""
内置 MCP Server：把 tools/tool.py 里的 26 个函数注册给 MCP 框架。

这里分两层看：
  1. builtin_server（MCPServer 实例）：只负责"介绍工具"。
     MCP 框架自动读函数签名和 docstring，生成 JSON Schema，
     这样 AI 模型就能完整看到每个工具的格式。
  2. TOOL_FUNCS（字典）：name -> 真正执行的函数。
     执行时走 run_builtin_tool()（带安全 + 权限闸门），
     而不是直接调 MCP 框架，因为安全闸门需要每轮会话的权限状态。

执行链路：
  chat/loop.py -> tools/mcp_client.py hub.call_tool()
             -> run_builtin_tool(name, args, perm_mgr, workdir)
             -> 安全检查 -> TOOL_FUNCS[name](**args)

新增工具只需两步：
  1. 在 tools/tool.py 写一个普通函数
  2. 在本文件用 @register 装饰它（函数名 = 工具名）
"""
import inspect
import json

from mcp.server.mcpserver import MCPServer

from tools import tool
from tools import safety
from memory import memory_manager as mm
from core.permission_manager import PermissionManager, extract_tool_path, PATH_TOOLS

# MCP 框架的注册入口（供 list_all_tools 生成工具清单用）
builtin_server = MCPServer("dc-builtin")

# 工具注册表：函数名 -> 函数本身（@register 自动收集）
TOOL_FUNCS = {}


def register(func):
    """把函数同时注册给 MCP 框架（生成 schema）和工具注册表（用于执行）。"""
    builtin_server.tool()(func)
    TOOL_FUNCS[func.__name__] = func
    return func


# ============================================================
# 工具注册（函数名 = 工具名，docstring = 给 AI 的说明）
# ============================================================

@register
def report_step(step: int, title: str, status: str = "started", detail: str = "") -> str:
    """向用户报告当前执行进度。仅在任务至少包含三个步骤、预计耗时较长、或用户明确要求进度时调用；不要为简单任务制造无意义的进度消息。"""
    return tool.report_step(step=step, title=title, status=status, detail=detail)


@register
def edit_file(path: str, old_text: str, new_text: str) -> str:
    """精确修改已有文件中的一段文本。必须先用 read_file 读取文件，再提供完全一致且只出现一次的 old_text；工具只替换这一处，适合小范围代码修改。若要创建新文件或整体重写文件，请用 write_file。"""
    return tool.edit_file(path=path, old_text=old_text, new_text=new_text)


@register
def get_current_time() -> str:
    """获取当前的日期和时间，精确到秒。无需任何参数。"""
    return tool.get_current_time()


@register
def read_file(path: str) -> str:
    """读取电脑上的本地文件内容，返回文件文本。【重要】当用户让你读文件/查看文件/打开文件时，必须调用本工具，不要用 python_exec 写代码读文件。"""
    return tool.read_file(path=path)


@register
def write_file(path: str, content: str) -> str:
    """将内容写入本地文件。此操作会覆盖目标文件的全部原内容，并会自动创建父目录；修改已有文件前先用 read_file 查看原内容。"""
    return tool.write_file(path=path, content=content)


@register
def list_dir(path: str = ".") -> str:
    """列出指定文件夹中的文件和子目录。path 省略时列出当前工作目录。"""
    return tool.list_dir(path=path)


@register
def create_dir(path: str) -> str:
    """创建一个新的文件夹。"""
    return tool.create_dir(path=path)


@register
def delete_file(path: str) -> str:
    """删除一个文件，操作不可恢复。仅在用户明确要求删除指定文件时调用；不要把清理、覆盖或移动误当作删除。"""
    return tool.delete_file(path=path)


@register
def move_file(src: str, dst: str) -> str:
    """移动或重命名文件/文件夹。"""
    return tool.move_file(src=src, dst=dst)


@register
def run_cmd(command: str, timeout: int = 30, cwd: str | None = None) -> str:
    """在系统终端中执行命令。【重要】当用户让你执行 shell/命令行/终端命令时，必须调用本工具，不要用 python_exec 调 subprocess 模拟。"""
    return tool.run_cmd(command=command, timeout=timeout, cwd=cwd)


@register
def get_system_info() -> str:
    """获取当前系统的详细信息。无需任何参数。"""
    return tool.get_system_info()


@register
def python_exec(code: str, timeout: int = 15, cwd: str | None = None) -> str:
    """执行受限的 Python 代码，返回输出结果。仅用于纯计算、数据处理、文本或 JSON 解析、算法验证；不要用于文件读写、网络请求、系统命令或安装依赖。"""
    return tool.python_exec(code=code, timeout=timeout, cwd=cwd)


@register
def http_get(url: str, headers: dict | None = None) -> str:
    """向已知 URL 发送 HTTP GET 请求，适合调用公开 API 或检查指定接口；不能用于搜索网页，搜索请用 web_search。"""
    return tool.http_get(url=url, headers=headers or {})


@register
def http_post(url: str, data: str | None = None, json_data: dict | None = None, headers: dict | None = None) -> str:
    """向已知 URL 发送 HTTP POST 请求，适合调用用户指定或已知的 API；不能用于搜索网页。"""
    return tool.http_post(url=url, data=data, json_data=json_data, headers=headers or {})


@register
def read_memory(target: str = "forever", date: str | None = None) -> str:
    """读取长期记忆或每日记忆。target 可选 forever 或 daily；读 daily 时可指定 date(YYYY-MM-DD)。"""
    return tool.read_memory(target=target, date=date)


@register
def write_memory(content: str) -> str:
    """将信息写入长期记忆。用户明确要求记住、记录、保存偏好或写入记忆时必须调用它。仅记录稳定、长期有效且确实会帮助后续对话的偏好或事实。"""
    return tool.write_memory(content=content)


@register
def web_search(input_text: str, mode: str = "auto", max_results: int = 5,
               fetch_content: bool = False, max_len: int | None = None,
               max_links: int = 20) -> str:
    """网页搜索与浏览工具：可搜索关键词、抓取指定网页正文、提取页面链接。【重要】用户让你搜资料、查公开信息、抓网页或提取链接时，必须调用本工具；调用已知 API 才用 http_get/http_post。"""
    return tool.web_search(
        input_text=input_text, mode=mode, max_results=max_results,
        fetch_content=fetch_content, max_len=max_len, max_links=max_links,
    )


@register
def get_location() -> str:
    """获取当前所在的城市和地区位置信息。基于 IP 地址定位，返回国家、地区、城市。无需任何参数。"""
    return tool.get_location()


@register
def search_code(query: str, path: str = ".", include: str | None = None, max_results: int = 30) -> str:
    """在项目文件中搜索关键词（类似 IDE 的全局搜索）。用于定位代码、查找函数定义、搜索错误信息出现位置；不支持正则，完整阅读请用 read_file 或 batch_read。"""
    return tool.search_code(query=query, path=path, include=include, max_results=max_results)


@register
def batch_read(paths: str) -> str:
    """一次性读取多个文件的内容，每个文件内容用分隔线隔开。paths 用英文逗号分隔，如 'src/main.py,src/utils.py'。"""
    return tool.batch_read(paths=paths)


@register
def get_env_info() -> str:
    """获取当前开发环境和已安装工具的信息（操作系统、Python/Node/npm/Git/CUDA 版本等）。无需任何参数。"""
    return tool.get_env_info()


@register
def ssh_connect(host: str, port: int = 22, username: str = "root",
                password: str = "", key_path: str = "") -> str:
    """SSH 连接到远程服务器，返回连接 ID。执行远程命令前必须先调用本工具；随后使用返回的 conn_id 调 ssh_exec，完成后应 ssh_disconnect。支持密码和密钥认证。"""
    return tool.ssh_connect(host=host, port=port, username=username,
                            password=password, key_path=key_path)


@register
def ssh_exec(conn_id: str, command: str, timeout: int = 30) -> str:
    """在已建立的 SSH 连接上执行命令，返回命令输出和退出码。必须先调用 ssh_connect 获取连接ID。"""
    return tool.ssh_exec(conn_id=conn_id, command=command, timeout=timeout)


@register
def ssh_disconnect(conn_id: str) -> str:
    """断开指定的 SSH 连接，释放资源。连接闲置10分钟也会自动断开。"""
    return tool.ssh_disconnect(conn_id=conn_id)


@register
def ssh_list() -> str:
    """列出当前所有活跃的 SSH 连接及其空闲时间。无需任何参数。"""
    return tool.ssh_list()


@register
def search_memory(query: str, max_results: int = 5) -> str:
    """搜索AI的记忆系统，在长期记忆、每日记忆、归档会话中查找相关信息。可以帮助回忆之前的对话内容、用户偏好、事实信息等。"""
    return tool.search_memory(query=query, max_results=max_results)


# ============================================================
# 统一执行入口：安全检查 + 权限检查，然后真的执行
# ============================================================

# 需要从 workdir 注入 cwd 的工具（执行子进程时限制目录）
_CWD_TOOLS = {"run_cmd", "python_exec"}


def run_builtin_tool(name: str, args: dict, perm_mgr: PermissionManager | None = None,
                     workdir: str = "") -> str:
    """
    执行内置工具，带安全 + 权限闸门，返回结果文本。

    安全/权限拦截时返回 "[安全拦截] ..." / "[权限拒绝] ..." 文本，
    AI 看到后会调整自己的行为。
    """
    # 1. 规则拦截（危险命令、系统路径、内网地址等）
    safe, reason = safety.check_tool(name, args)
    if not safe:
        return f"[安全拦截] 工具 {name} 被阻止：{reason}"

    # 2. 工作目录越界检查（用户授权的范围之外不许动文件）
    if perm_mgr and perm_mgr.has_root and name in PATH_TOOLS:
        path_to_check = extract_tool_path(name, args)
        if path_to_check and not perm_mgr.is_allowed(path_to_check):
            return f"[权限拒绝] 路径「{path_to_check}」不在允许的工作目录内"

    # 3. 需要工作目录的工具：手动把目录塞进参数
    if name in _CWD_TOOLS and workdir:
        args = dict(args, cwd=workdir)

    # 4. 找到函数，只传递签名里声明的参数（多余的参数直接丢弃）
    func = TOOL_FUNCS.get(name)
    if func is None:
        return f"错误：未知内置工具 {name}"
    allowed_params = inspect.signature(func).parameters.keys()
    kwargs = {k: v for k, v in args.items() if k in allowed_params}

    # 5. 执行；结果如果是列表/字典，转成 JSON 文本让 AI 能读懂
    try:
        result = func(**kwargs)
    except TypeError as e:
        return f"错误：参数不匹配 - {e}"
    except Exception as e:
        return f"错误：工具执行异常 - {type(e).__name__}: {e}"
    if isinstance(result, (list, dict)):
        return json.dumps(result, ensure_ascii=False, indent=2)
    return str(result)
