"""
内置工具库：AI 可以调用的所有工具，一个工具就是一个普通函数。

约定：
  - 函数名 = 工具名（AI 靠名字来调用）
  - 参数 = AI 要传的参数
  - docstring = 给 AI 看的说明（它根据说明决定用什么工具）
  - 返回值用字符串，AI 会拿结果继续思考

调用链：AI 模型 -> chat/loop.py -> tools/builtin_server.py（安全检查）-> 本文件
"""
import ast
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import platform
import shutil
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from ddgs import DDGS

from utils import logger
from memory import memory_manager as mm

# ============================================================
# 网页搜索（ddgs 搜索引擎 + 网页正文提取）
# ============================================================

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def _fetch_html(url, timeout=10):
    """下载网页原始 HTML；失败时返回以"错误:"开头的文本。"""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except Exception as e:
        return f"错误: {e}"


def _get_text(url, max_len=None):
    """提取网页纯文本（去掉 script/style 等干扰标签）。"""
    html = _fetch_html(url)
    if html.startswith("错误"):
        return html
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    text = soup.get_text(separator="\n", strip=True)
    text = "\n".join(line for line in text.splitlines() if line.strip())
    if max_len and len(text) > max_len:
        text = text[:max_len] + "...(截断)"
    return text


def _search_web(query, max_results=5):
    """搜索网页，返回 [{title, link, snippet}, ...]。"""
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "title": r.get("title"),
                    "link": r.get("href"),
                    "snippet": r.get("body"),
                })
    except Exception as e:
        return [{"error": str(e)}]
    return results


def _get_links(url, max_links=20):
    """提取网页里出现的所有链接。"""
    html = _fetch_html(url)
    if html.startswith("错误"):
        return []
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href and not href.startswith(("#", "javascript:")):
            links.append({
                "text": a.get_text(strip=True) or href,
                "href": href,
                "absolute": requests.compat.urljoin(url, href),
            })
        if len(links) >= max_links:
            break
    return links


def web_search(input_text, mode="auto", max_results=5, fetch_content=False,
               max_len=None, max_links=20):
    """网页搜索与浏览工具：可搜索关键词、抓取指定网页正文、提取页面链接。"""
    # mode=auto：看起来是网址就"抓取"，否则"搜索"
    if mode == "auto":
        mode = "fetch" if input_text.startswith(("http://", "https://")) else "search"

    if mode == "search":
        results = _search_web(input_text, max_results)
        if fetch_content and results and not results[0].get("error"):
            for item in results:
                if item.get("link"):
                    item["content"] = _get_text(item["link"], max_len)
        return results
    if mode == "fetch":
        return _get_text(input_text, max_len)
    if mode == "links":
        return _get_links(input_text, max_links)
    return f"未知模式: {mode}，可选 auto/search/fetch/links"


# ============================================================
# 简单工具
# ============================================================

def report_step(step, title, status="started", detail=""):
    """向用户报告当前执行进度（只通知前端，不真正做事）。"""
    return f"[步骤 {step}] {title} — {status}" + (f" ({detail})" if detail else "")


def get_current_time():
    """获取当前的日期和时间，精确到秒。无需任何参数。"""
    n = datetime.now()
    weekdays = "一二三四五六日"
    return f"{n.year}年{n.month}月{n.day}日 {n.hour}时{n.minute}分{n.second}秒 星期{weekdays[n.weekday()]}"


def get_location():
    """获取当前所在的城市和地区位置信息。基于 IP 地址定位。"""
    try:
        resp = requests.get("https://ipapi.co/json/", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return (f"当前IP: {data.get('ip', '未知')}\n"
                    f"国家: {data.get('country_name', '未知')}\n"
                    f"地区: {data.get('region', '未知')}\n"
                    f"城市: {data.get('city', '未知')}")
        resp2 = requests.get("https://ip-api.com/json/?lang=zh-CN", timeout=10)
        if resp2.status_code == 200:
            data = resp2.json()
            return (f"国家: {data.get('country', '未知')}\n"
                    f"城市: {data.get('city', '未知')}\n"
                    f"运营商: {data.get('isp', '未知')}")
        return "定位失败 (HTTP " + str(resp.status_code) + ")"
    except Exception as e:
        return "定位失败：" + str(e)


# ============================================================
# 文件与目录操作
# ============================================================

def read_file(path):
    """读取电脑上的本地文件内容，返回文件文本。"""
    try:
        path = os.path.abspath(path)
        if not os.path.exists(path):
            return f"错误：文件不存在 - {path}"
        if not os.path.isfile(path):
            return f"错误：路径不是文件 - {path}"
        if os.path.getsize(path) > 1024 * 1024:
            return "错误：文件超过 1MB 限制，请指定更小的文件"
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except Exception as e:
        return f"错误：读取文件失败 - {e}"


def write_file(path, content=""):
    """将内容写入本地文件（自动创建父目录），会覆盖原内容。"""
    try:
        path = os.path.abspath(path)
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"文件已成功写入：{path}"
    except Exception as e:
        return f"错误：写入文件失败 - {e}"


def edit_file(path, old_text, new_text):
    """在已有文件中精确替换一段只出现一次的文本。"""
    if not path or not path.strip():
        return "错误：未提供文件路径"
    if not old_text:
        return "错误：old_text 不能为空，避免意外替换整个文件"
    try:
        path = os.path.abspath(path)
        if not os.path.exists(path):
            return f"错误：文件不存在 - {path}"
        if not os.path.isfile(path):
            return f"错误：路径不是文件 - {path}"
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        matches = content.count(old_text)
        if matches == 0:
            return "错误：未找到 old_text；请先读取文件并提供完全一致的原文本"
        if matches > 1:
            return f"错误：old_text 在文件中出现 {matches} 次；请提供更完整且唯一的上下文"
        with open(path, "w", encoding="utf-8") as f:
            f.write(content.replace(old_text, new_text, 1))
        return f"文件已精确修改：{path}"
    except Exception as e:
        return f"错误：修改文件失败 - {e}"


def list_dir(path="."):
    """列出指定文件夹里的文件和子目录。"""
    try:
        path = os.path.abspath(path)
        if not os.path.exists(path):
            return f"错误：路径不存在 - {path}"
        if not os.path.isdir(path):
            return f"错误：路径不是文件夹 - {path}"

        items = []
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            if os.path.isdir(full):
                items.append(f"[DIR]  {name}/")
            else:
                size = os.path.getsize(full)
                if size < 1024:
                    size_str = f"{size} B"
                elif size < 1024 * 1024:
                    size_str = f"{size/1024:.1f} KB"
                else:
                    size_str = f"{size/1024/1024:.1f} MB"
                items.append(f"[FILE] {name}  ({size_str})")

        header = f"{path}\n{'=' * 50}\n"
        summary = f"\n{'=' * 50}\n共 {len(items)} 项"
        return header + "\n".join(items) + summary
    except Exception as e:
        return f"错误：列出目录失败 - {e}"


def create_dir(path):
    """创建一个新的文件夹（自动创建父目录）。"""
    if not path or not path.strip():
        return "错误：未提供文件夹路径"
    try:
        path = os.path.abspath(path)
        os.makedirs(path, exist_ok=True)
        return f"文件夹已创建：{path}"
    except Exception as e:
        return f"错误：创建文件夹失败 - {e}"


def delete_file(path):
    """删除一个文件（不可恢复，只删文件不删文件夹）。"""
    if not path or not path.strip():
        return "错误：未提供文件路径"
    try:
        path = os.path.abspath(path)
        if not os.path.exists(path):
            return f"错误：文件不存在 - {path}"
        if not os.path.isfile(path):
            return f"错误：路径不是文件，不支持删除文件夹 - {path}"
        os.remove(path)
        return f"文件已删除：{path}"
    except Exception as e:
        return f"错误：删除文件失败 - {e}"


def move_file(src, dst):
    """把文件/文件夹从 src 移动到 dst（重命名同一回事）。"""
    if not src or not src.strip():
        return "错误：未提供源路径"
    if not dst or not dst.strip():
        return "错误：未提供目标路径"
    try:
        src = os.path.abspath(src)
        dst = os.path.abspath(dst)
        if not os.path.exists(src):
            return f"错误：源路径不存在 - {src}"
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        os.rename(src, dst)
        return f"已移动：{src} → {dst}"
    except Exception as e:
        return f"错误：移动文件失败 - {e}"


def search_code(query, path=".", include=None, max_results=30):
    """在文件夹里搜索关键词（类似 IDE 全局搜索，不支持正则）。"""
    if not query or not query.strip():
        return "错误：未提供搜索关键词"
    query = query.strip()
    path = os.path.abspath(path)
    if not os.path.exists(path):
        return f"错误：路径不存在 - {path}"
    if not os.path.isdir(path):
        return f"错误：路径不是文件夹 - {path}"

    # 不进入这些文件夹（体积大或者没意义）
    skip_dirs = {".venv", "__pycache__", "node_modules", ".git", ".idea", "archive",
                 "vosk_model", "napcat", "electron-frontend", "dist", "build", "target"}
    skip_exts = {".pyc", ".pyo", ".exe", ".dll", ".so", ".bin", ".jpg", ".png",
                 ".gif", ".svg", ".ico"}

    results = []
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        for fname in files:
            if include and not fname.endswith(tuple(include.split(","))):
                continue
            if os.path.splitext(fname)[1].lower() in skip_exts:
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        if query.lower() in line.lower():
                            rel = os.path.relpath(fpath, os.getcwd())
                            results.append(f"{rel}:{lineno}: {line.rstrip()[:200]}")
                            if len(results) >= max_results:
                                break
            except Exception:
                pass
            if len(results) >= max_results:
                break
        if len(results) >= max_results:
            break

    if not results:
        return f"未找到匹配「{query}」的代码"
    return f"搜索「{query}」共 {len(results)} 条结果：\n" + "\n".join(results)


def batch_read(paths):
    """一次性读取多个文件内容（paths 用英文逗号分隔）。"""
    if not paths:
        return "错误：未提供文件路径列表"
    if isinstance(paths, str):
        paths = [p.strip() for p in paths.split(",") if p.strip()]
    outputs = []
    for p in paths:
        if not p:
            continue
        ap = os.path.abspath(p)
        if not os.path.exists(ap):
            outputs.append(f"【{p}】\n  文件不存在")
            continue
        if not os.path.isfile(ap):
            outputs.append(f"【{p}】\n  不是文件")
            continue
        try:
            with open(ap, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            limited = content[:30000]
            if len(content) > 30000:
                limited += "\n...(文件过长，已截断至 30000 字符)"
            outputs.append(f"【{p}】\n{limited}")
        except Exception as e:
            outputs.append(f"【{p}】\n  读取失败：{e}")
    return "\n\n---\n\n".join(outputs)


# ============================================================
# 系统命令与代码执行
# ============================================================

def run_cmd(command, timeout=30, cwd=None):
    """在系统终端中执行命令（Windows 用 cmd，Linux 用 bash）。"""
    if not command or not command.strip():
        return "错误：未提供要执行的命令"
    shell = os.name == "nt"
    try:
        result = subprocess.run(
            command,
            shell=shell,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or None,
        )
        output = ""
        if result.stdout:
            output += result.stdout
        if result.stderr:
            if output:
                output += "\n--- stderr ---\n"
            output += result.stderr
        if not output:
            output = "(命令执行完毕，无输出)"
        return f"返回码: {result.returncode}\n{'=' * 40}\n{output}"
    except subprocess.TimeoutExpired:
        return f"错误：命令执行超时（超过 {timeout} 秒）"
    except Exception as e:
        return f"错误：执行命令失败 - {e}"


def get_system_info():
    """获取当前系统的详细信息。"""
    info = []
    info.append(f"操作系统: {platform.system()} {platform.release()}")
    info.append(f"版本: {platform.version()}")
    info.append(f"架构: {platform.machine()}")
    info.append(f"处理器: {platform.processor() or '未知'}")
    info.append(f"主机名: {platform.node()}")
    info.append(f"Python 版本: {platform.python_version()}")
    info.append(f"CPU 逻辑核心: {os.cpu_count() or 0}")
    try:
        if os.name == "nt":
            usage = shutil.disk_usage(os.getcwd().split(":")[0] + ":\\")
        else:
            usage = shutil.disk_usage("/")
        info.append(f"磁盘总空间: {usage.total / 1024**3:.1f} GB")
        info.append(f"磁盘可用空间: {usage.free / 1024**3:.1f} GB")
        info.append(f"磁盘使用率: {usage.used / usage.total * 100:.1f}%")
    except Exception:
        info.append("磁盘信息: 获取失败")
    return "\n".join(info)


def get_env_info():
    """获取当前开发环境信息（Node/npm/Git/CUDA 是否安装等）。"""
    lines = [f"操作系统: {platform.system()} {platform.release()}", f"Python: {sys.version.split()[0]}"]

    def _exe_version(cmd, name):
        """调用一条命令行工具拿版本，失败就显示"未安装"。"""
        try:
            r = subprocess.run([cmd, "--version"], capture_output=True, text=True, timeout=5)
            lines.append(f"{name}: {r.stdout.strip() if r.returncode == 0 else '未安装'}")
        except Exception:
            lines.append(f"{name}: 未安装")

    _exe_version("node", "Node.js")
    _exe_version("npm", "npm")
    _exe_version("git", "Git")

    cuda_avail = False
    try:
        import torch
        cuda_avail = torch.cuda.is_available()
    except Exception:
        pass
    lines.append(f"CUDA: {'可用' if cuda_avail else '不可用'}")
    lines.append("Shell: PowerShell/CMD" if os.name == "nt" else "Shell: bash")
    return "\n".join(lines)


# Python 代码执行：先静态检查（禁止 import / 危险属性），
# 再扔进子进程用 -I（隔离）模式跑，防止代码搞坏服务本身。
_ast_forbidden = frozenset({
    "__subclasses__", "__bases__", "__globals__", "__code__",
    "__closure__", "__self__", "__class__", "__builtins__",
    "__import__", "__loader__", "__spec__", "__reduce__",
    "__reduce_ex__", "__getattr__", "__setattr__",
})


def _ast_check(code):
    """静态检查代码：不通过就抛 ValueError。"""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise ValueError(f"语法错误: {e}")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _ast_forbidden:
            raise ValueError(f"禁止访问危险属性: {node.attr}")
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in ("exec", "eval", "compile", "__import__", "open"):
                raise ValueError(f"禁止调用危险函数: {fn.id}")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError("禁止使用 import 语句（已限制内置函数可用）")


def python_exec(code, timeout=15, cwd=None):
    """执行受限的 Python 代码并返回输出（在子进程里跑，搞不坏服务）。"""
    if not code or not code.strip():
        return "错误：未提供 Python 代码"
    try:
        _ast_check(textwrap.dedent(code))
    except ValueError as e:
        return f"安全拦截: {e}"

    # 把用户代码包进一个小程序，出错时打印错误信息
    wrapper = (
        "import sys\n"
        "try:\n"
        "    exec('''\n"
        + textwrap.dedent(code).replace("'''", "\\x27\\x27\\x27")
        + "\n    ''')\n"
        "except Exception as e:\n"
        "    print(f'执行错误：{type(e).__name__}: {e}', file=sys.stderr)\n"
    )

    tmp = tempfile.mktemp(suffix=".py", prefix="dcs_")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(wrapper)
        result = subprocess.run(
            [sys.executable, "-I", tmp],
            capture_output=True, text=True, timeout=timeout,
            env={},
            cwd=cwd or tempfile.gettempdir(),
        )
        out = (result.stdout or "")[:5000]
        err = (result.stderr or "")[:5000]
        output = out
        if err:
            output += "\n--- stderr ---\n" + err
        return output if output else "(代码执行完毕，无输出)"
    except subprocess.TimeoutExpired:
        return f"错误：代码执行超时（超过 {timeout} 秒）"
    except Exception as e:
        return f"执行错误：{type(e).__name__}: {e}"
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass


# ============================================================
# HTTP 请求
# ============================================================

def http_get(url, headers=None):
    """向已知 URL 发送 HTTP GET 请求，返回状态码和响应内容。"""
    if not url or not url.strip():
        return "错误：未提供 URL"
    try:
        resp = requests.get(url, headers=headers or {}, timeout=15)
        body = resp.text[:5000]
        if len(resp.text) > 5000:
            body += "\n...(响应体过长，已截断)"
        result = (f"状态码: {resp.status_code}\n"
                  f"耗时: {resp.elapsed.total_seconds():.2f}s\n"
                  f"响应头:\n{json.dumps(dict(resp.headers), ensure_ascii=False, indent=2)}\n"
                  f"{'=' * 40}\n{body}")
        if "application/json" in resp.headers.get("content-type", "") and len(resp.text) < 10000:
            try:
                pretty = json.dumps(resp.json(), ensure_ascii=False, indent=2)
                result += f"\n\n(JSON 格式化):\n{pretty[:3000]}"
            except Exception:
                pass
        return result
    except requests.exceptions.Timeout:
        return "错误：请求超时（超过 15 秒）"
    except Exception as e:
        return f"错误：HTTP GET 请求失败 - {e}"


def http_post(url, data=None, json_data=None, headers=None):
    """向已知 URL 发送 HTTP POST 请求，返回状态码和响应内容。"""
    if not url or not url.strip():
        return "错误：未提供 URL"
    try:
        resp = requests.post(url, data=data, json=json_data, headers=headers or {}, timeout=15)
        body = resp.text[:5000]
        if len(resp.text) > 5000:
            body += "\n...(响应体过长，已截断)"
        result = (f"状态码: {resp.status_code}\n"
                  f"耗时: {resp.elapsed.total_seconds():.2f}s\n"
                  f"响应头:\n{json.dumps(dict(resp.headers), ensure_ascii=False, indent=2)}\n"
                  f"{'=' * 40}\n{body}")
        if "application/json" in resp.headers.get("content-type", "") and len(resp.text) < 10000:
            try:
                pretty = json.dumps(resp.json(), ensure_ascii=False, indent=2)
                result += f"\n\n(JSON 格式化):\n{pretty[:3000]}"
            except Exception:
                pass
        return result
    except requests.exceptions.Timeout:
        return "错误：请求超时（超过 15 秒）"
    except Exception as e:
        return f"错误：HTTP POST 请求失败 - {e}"


# ============================================================
# 记忆读写
# ============================================================

def read_memory(target="forever", date=None):
    """读取长期记忆（forever）或每日记忆（daily）。"""
    try:
        if target == "daily":
            if date:
                return mm.get_daily_memory(date)
            dates = mm.list_daily_memories()
            if not dates:
                return "（暂无每日记忆）"
            return "可用每日记忆日期：\n" + "\n".join(dates)
        return mm.get_forever_memory()
    except Exception as e:
        return f"错误：读取记忆失败 - {e}"


def write_memory(content):
    """写入内容到长期记忆。"""
    if not content or not content.strip():
        return "错误：未提供要写入的内容"
    try:
        mm.append_forever_memory(content)
        return "长期记忆已更新喵~"
    except Exception as e:
        return f"错误：写入记忆失败 - {e}"


def search_memory(query, max_results=5):
    """搜索 AI 的记忆（长期记忆、每日记忆、归档会话）。"""
    results = mm.search_memory(query, max_results)
    return results if results else f"未找到与「{query}」相关的记忆"


# ============================================================
# SSH 连接管理
# ============================================================

# 已建立的连接 p2store：连接ID -> (SSH 客户端, 空闲锁)
# 与清理线程共享，所以所有读写都在 _SSH_LOCK 里做。
_SSH_CONNECTIONS = {}      # 连接ID -> paramiko.SSHClient
_SSH_LOCKS = {}            # 连接ID -> threading.Lock（防止并发执行命令）
_SSH_LAST_USE = {}         # 连接ID -> 最后使用时间
_SSH_LOCK = threading.Lock()
_SSH_IDLE_TIMEOUT = 600    # 空闲 10 分钟自动断开
_SSH_CLEANUP_STARTED = False


def _ssh_cleanup_loop():
    """后台线程：每 5 分钟清理一次闲置超过 10 分钟的 SSH 连接。"""
    while True:
        time.sleep(300)
        now = time.time()
        dead = []
        with _SSH_LOCK:
            for cid, last in list(_SSH_LAST_USE.items()):
                if now - last > _SSH_IDLE_TIMEOUT:
                    try:
                        _SSH_CONNECTIONS[cid].close()
                    except Exception:
                        pass
                    _SSH_CONNECTIONS.pop(cid, None)
                    _SSH_LOCKS.pop(cid, None)
                    _SSH_LAST_USE.pop(cid, None)
                    dead.append(cid)
        if dead:
            logger.info("SSH", f"清理闲置连接: {dead}")


def _ensure_ssh_cleanup():
    """确保清理线程只启动一次。"""
    global _SSH_CLEANUP_STARTED
    with _SSH_LOCK:
        if not _SSH_CLEANUP_STARTED:
            _SSH_CLEANUP_STARTED = True
            threading.Thread(target=_ssh_cleanup_loop, daemon=True).start()


def ssh_connect(host, port=22, username="root", password="", key_path=""):
    """SSH 连接到远程服务器，返回连接 ID；之后用 ssh_exec 执行命令。"""
    import paramiko
    if not host or not host.strip():
        return "错误：未提供主机地址"
    if not username or not username.strip():
        return "错误：未提供用户名"
    if not password and not key_path:
        return "错误：请提供密码或密钥文件路径"

    conn_id = f"{username}@{host}:{port}"
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        if key_path:
            key_path = os.path.abspath(key_path)
            if not os.path.exists(key_path):
                return f"错误：密钥文件不存在 - {key_path}"
            pkey = paramiko.RSAKey.from_private_key_file(key_path)
            client.connect(host, port=port, username=username, pkey=pkey, timeout=15)
        else:
            client.connect(host, port=port, username=username, password=password, timeout=15)

        with _SSH_LOCK:
            old = _SSH_CONNECTIONS.pop(conn_id, None)
            if old is not None:
                try:
                    old.close()
                except Exception:
                    pass
            _SSH_CONNECTIONS[conn_id] = client
            _SSH_LOCKS[conn_id] = threading.Lock()
            _SSH_LAST_USE[conn_id] = time.time()

        _ensure_ssh_cleanup()
        logger.info("SSH", f"连接成功: {conn_id}")
        return f"连接成功，连接ID: {conn_id}"
    except paramiko.AuthenticationException:
        return f"错误：身份验证失败 - {conn_id}"
    except paramiko.SSHException as e:
        return f"错误：SSH 连接失败 - {e}"
    except Exception as e:
        return f"错误：连接失败 - {type(e).__name__}: {e}"


def ssh_exec(conn_id, command, timeout=30):
    """在已建立的 SSH 连接上执行命令，返回输出和退出码。"""
    import paramiko
    if not conn_id or not conn_id.strip():
        return "错误：未提供连接ID"
    if not command or not command.strip():
        return "错误：未提供要执行的命令"

    with _SSH_LOCK:
        client = _SSH_CONNECTIONS.get(conn_id)
        if client is None:
            return f"错误：连接不存在或已断开 - {conn_id}，请先使用 ssh_connect"

    lock = _SSH_LOCKS.get(conn_id)
    try:
        if lock:
            lock.acquire(timeout=30)
        logger.info("SSH", f"[{conn_id}] 执行: {command[:200]}")
        _SSH_LAST_USE[conn_id] = time.time()
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        result = f"退出码: {exit_code}\n" + "=" * 40 + "\n"
        if out:
            result += out
        if err:
            if out:
                result += "\n--- stderr ---\n"
            result += err
        if not out and not err:
            result += "(命令执行完毕，无输出)"
        logger.info("SSH", f"[{conn_id}] 完成: exit={exit_code} out={len(out)} err={len(err)}")
        return result
    except paramiko.SSHException as e:
        return f"错误：SSH 执行失败 - {e}"
    except Exception as e:
        return f"错误：命令执行异常 - {type(e).__name__}: {e}"
    finally:
        if lock:
            lock.release()


def ssh_disconnect(conn_id):
    """断开指定的 SSH 连接，释放资源。"""
    if not conn_id or not conn_id.strip():
        return "错误：未提供连接ID"
    with _SSH_LOCK:
        client = _SSH_CONNECTIONS.pop(conn_id, None)
        _SSH_LOCKS.pop(conn_id, None)
        _SSH_LAST_USE.pop(conn_id, None)
    if client is None:
        return f"连接不存在或已断开: {conn_id}"
    try:
        client.close()
        logger.info("SSH", f"已断开: {conn_id}")
        return f"连接已断开: {conn_id}"
    except Exception as e:
        return f"断开时发生异常: {e}"


def ssh_list():
    """列出当前所有活跃的 SSH 连接及其空闲时间。"""
    with _SSH_LOCK:
        if not _SSH_CONNECTIONS:
            return "（当前无活跃 SSH 连接）"
        lines = ["当前活跃 SSH 连接:"]
        for cid in _SSH_CONNECTIONS:
            last = _SSH_LAST_USE.get(cid, 0)
            idle = time.time() - last
            lines.append(f"  [{cid}] 空闲 {idle:.0f}s")
        return "\n".join(lines)
