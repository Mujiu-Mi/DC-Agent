"""
AI 工具调用安全检查
在每个工具执行前检测，防止执行危险操作
"""

import os
import re

# Windows 危险命令模式
WINDOWS_DANGEROUS = [
    r'\bformat\b', r'\bdiskpart\b',
    r'\brmdir\s+/[sSqQ]', r'\brd\s+/[sSqQ]', r'\bdel\s+/[fFsSqQ]',
    r'\bshutdown\b', r'\brestart\b', r'\bpoweroff\b',
    r'\breg\s+delete\b', r'\breg\s+add\b', r'\bbcdedit\b',
    r'\bnet\s+user\s+[/]', r'\bnet\s+localgroup\b',
    r'\btaskkill\s+/[fF]',
    r'\becho\s+.*>\\(Windows|WINNT|System32)',
    r'\bcopy\s+.*\\(Windows|WINNT|System32)',
    r'\bnetsh\s+.*(stop|disable|delete)',
    r'\bipconfig\s+/release\b',
    r'Remove-Item\s+', r'rm\s+-rf\s+', r'Clear-Content\s+',
    r'Stop-Computer\b', r'Restart-Computer\b',
    r'Format-Volume\b', r'Clear-Disk\b',
    r'curl.*\||wget.*\||iwr.*\||Invoke-WebRequest.*\|',
]

# Linux 危险命令模式
LINUX_DANGEROUS = [
    r'\brm\s+-rf\s+/', r'\brm\s+-rf\s+/\*', r'\brm\s+-rf\s+\*',
    r'\bmkfs\.', r'\bdd\s+if=', r'\bdd\s+of=/dev/',
    r'\bfdisk\b', r'\bparted\b',
    r'\bchmod\s+-R\s+777\s+/', r'\bchown\s+-R\b',
    r'\bshutdown\b', r'\breboot\b', r'\bpoweroff\b', r'\binit\s+0\b', r'\binit\s+6\b',
    r'\bwget\b.*\|\s*(sh|bash|zsh)', r'\bcurl\b.*\|\s*(sh|bash|zsh)',
    r'>\s*/dev/sd', r'>\s*/dev/nvme', r'>\s*/dev/mmc',
    r':\(\)\{', r'\{.*:.*:.*&\};',
    r'\bapt-get\s+(remove|purge|autoremove)', r'\byum\s+(remove|erase)',
    r'\bpacman\s+-R', r'\bdpkg\s+-[rP]',
    r'\biptables\s+-[FXPZ]', r'\bip6tables\s+-[FXPZ]',
]

# 受保护的系统路径（不区分大小写）
PROTECTED_DIRS = [
    r'C:\Windows', r'C:\Program Files',
    r'C:\Program Files (x86)', r'C:\System32',
    '/etc', '/usr', '/bin', '/sbin',
    '/boot', '/dev', '/proc', '/sys', '/var/log',
]

# 受保护的文件扩展名
PROTECTED_EXTS = ['.exe', '.dll', '.sys', '.drv', '.ocx', '.msi']


def _match_patterns(text, patterns):
    for p in patterns:
        if re.search(p, text, re.IGNORECASE):
            return p
    return ""


def _is_protected_path(path):
    if not path: return ""
    # 先检查原始路径（处理 Linux 路径）
    for d in PROTECTED_DIRS:
        if path.lower().startswith(d.lower()):
            return d
    # 再检查绝对路径（处理 Windows 路径）
    abs_path = os.path.abspath(path)
    for d in PROTECTED_DIRS:
        if abs_path.lower().startswith(d.lower()):
            return d
    return ""


def check_tool(name, args):
    """
    安全检查主函数
    返回 (safe: bool, reason: str)
    safe=True 安全，safe=False 被拦截
    """
    if name == "run_cmd":
        command = args.get("command", "")
        if not command:
            return True, ""
        match = _match_patterns(command, WINDOWS_DANGEROUS)
        if match:
            return False, f"危险命令被拦截 [{match}]"
        match = _match_patterns(command, LINUX_DANGEROUS)
        if match:
            return False, f"危险命令被拦截 [{match}]"
        return True, ""

    if name == "delete_file":
        path = args.get("path", "")
        protected = _is_protected_path(path)
        if protected:
            return False, f"禁止删除系统目录文件 [{protected}]"
        return True, ""

    if name in ("write_file", "edit_file"):
        path = args.get("path", "")
        protected = _is_protected_path(path)
        if protected:
            return False, f"禁止写入系统目录 [{protected}]"
        _, ext = os.path.splitext(path)
        if ext.lower() in PROTECTED_EXTS:
            return False, f"禁止覆写 {ext} 系统文件"
        abs_path = os.path.abspath(path) if path else ""
        if "dscat" in abs_path.lower() and abs_path.endswith(".py"):
            return False, f"禁止覆写项目自身的 Python 文件"
        return True, ""

    if name == "python_exec":
        code = args.get("code", "")
        dangerous = ["import os", "from os", "os.system", "os.popen",
                     "subprocess", "__import__", "eval(", "exec(",
                     "compile(", "open(", "shutil.rmtree"]
        for kw in dangerous:
            if kw in code:
                return False, f"代码含受限操作 [{kw}]"
        return True, ""

    if name == "move_file":
        for p in [args.get("src", ""), args.get("dst", "")]:
            protected = _is_protected_path(p)
            if protected:
                return False, f"禁止移动系统目录文件 [{protected}]"
        return True, ""

    if name in ("http_get", "http_post"):
        url = args.get("url", "")
        sensitive = [
            r'^https?://127\.0\.0\.1', r'^https?://localhost',
            r'^https?://10\.\d+', r'^https?://172\.1[6-9]\.',
            r'^https?://172\.2\d+\.', r'^https?://172\.3[0-1]\.',
            r'^https?://192\.168\.', r'^https?://169\.254\.',
            r'^https?://0\.0\.0\.0', r'^https?://\[\:\:1\]',
        ]
        for p in sensitive:
            if re.match(p, url, re.IGNORECASE):
                return False, f"禁止访问内网地址"
        return True, ""

    if name == "ssh_connect":
        host = args.get("host", "")
        # 禁止连接内网地址
        internal = [
            r'^127\.0\.0\.1$', r'^localhost$',
            r'^10\.', r'^172\.1[6-9]\.', r'^172\.2\d+\.', r'^172\.3[0-1]\.',
            r'^192\.168\.', r'^169\.254\.', r'^0\.0\.0\.0$',
            r'^\[\:\:1\]$', r'^\:\:1$',
        ]
        for p in internal:
            if re.match(p, host, re.IGNORECASE):
                return False, f"禁止 SSH 连接到内网地址"
        # 禁止连接云元数据端点
        if host == "169.254.169.254" or "metadata" in host.lower():
            return False, f"禁止 SSH 连接到云元数据服务"
        return True, ""

    if name == "ssh_exec":
        command = args.get("command", "")
        if not command:
            return True, ""
        # 复用命令安全检查
        match = _match_patterns(command, WINDOWS_DANGEROUS)
        if match:
            return False, f"危险命令被拦截 [{match}]"
        match = _match_patterns(command, LINUX_DANGEROUS)
        if match:
            return False, f"危险命令被拦截 [{match}]"
        return True, ""

    if name == "create_dir":
        protected = _is_protected_path(args.get("path", ""))
        if protected:
            return False, f"禁止在系统目录创建文件夹 [{protected}]"
        return True, ""

    return True, ""
