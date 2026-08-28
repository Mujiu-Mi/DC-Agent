"""
简易日志系统 — 写入 logs/ 目录，按日期分割
"""
import os
from datetime import datetime

LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)


def _log_file():
    return os.path.join(LOG_DIR, f"{datetime.now().strftime('%Y-%m-%d')}.log")


def _ts():
    return datetime.now().strftime("%H:%M:%S")


def info(tag, msg):
    line = f"[{_ts()}] [INFO] [{tag}] {msg}"
    with open(_log_file(), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(f"  \033[2;34m{line}\033[0m")


def warn(tag, msg):
    line = f"[{_ts()}] [WARN] [{tag}] {msg}"
    with open(_log_file(), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(f"  \033[2;33m{line}\033[0m")


def error(tag, msg):
    line = f"[{_ts()}] [ERROR] [{tag}] {msg}"
    with open(_log_file(), "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(f"  \033[2;31m{line}\033[0m")


def dump(tag, obj, max_len=500):
    """调试用：将对象的关键属性写入日志"""
    import json
    try:
        if hasattr(obj, "__dict__"):
            raw = str(obj.__dict__)[:max_len]
        else:
            raw = str(obj)[:max_len]
        info(tag, f"dump: {raw}")
    except Exception as e:
        warn(tag, f"dump failed: {e}")
