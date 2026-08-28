"""
记忆管理模块（用 Markdown 文件存储）

三份记忆：
  1. 上下文（context/current_context.md）：当前会话的完整对话记录
  2. 长期记忆（long_term/）：分主题保存关键信息（用户偏好、承诺等）
  3. 每日记忆（daily/）：每天 23:30 对当天对话的自动总结

实现要点：
  - 所有文件读写都带全局写锁，多个会话并发对话时也不怕写坏
  - 写入用的是"临时文件 + os.replace"的原子替换，中途崩溃不会留半个文件
"""
import os
import time
import json
import yaml
import threading
import re
from datetime import datetime

# ───── 文件路径 ─────
AGENT_MEMORY_DIR = "agent_memory"
CONTEXT_FILE = os.path.join(AGENT_MEMORY_DIR, "context", "current_context.md")
LONG_TERM_DIR = os.path.join(AGENT_MEMORY_DIR, "long_term")
INDEX_FILE = os.path.join(LONG_TERM_DIR, "index.md")
RULES_FILE = os.path.join(LONG_TERM_DIR, "rules.md")
DAILY_DIR = os.path.join(AGENT_MEMORY_DIR, "daily")
ARCHIVE_DIR = os.path.join(AGENT_MEMORY_DIR, "archive")
CONFIG_DIR = os.path.join(AGENT_MEMORY_DIR, "config")
CONFIG_FILE = os.path.join(CONFIG_DIR, "memory_config.yaml")
DIALOG_LOG_FILE = os.path.join(AGENT_MEMORY_DIR, "dialog_log.txt")
DIALOG_COUNT_FILE = os.path.join(AGENT_MEMORY_DIR, "dialog_count.txt")

CONTEXT_MAX_SIZE = 256 * 1024  # 上下文超过 256KB（≈256K token）就压缩
SUMMARY_TRIGGER = 10           # 每 10 轮对话提取一次长期记忆

DEFAULT_CONFIG = {
    "context": {"max_tokens": 256000, "archive_on_overflow": True, "keep_summary_rounds": 5},
    "long_term": {"summary_trigger_rounds": 10, "auto_classify": True},
    "daily": {"summary_time": "23:30", "auto_generate": True},
    "archive": {"retention_days": 30},
}

# 全局写锁：同一时间只允许一个线程写文件
_WRITE_LOCK = threading.Lock()

# 运行时配置（启动时读一次）
_config = None


# ============================================================
# 基础工具函数
# ============================================================

def _ensure_dirs():
    for d in [AGENT_MEMORY_DIR, LONG_TERM_DIR, DAILY_DIR, ARCHIVE_DIR, CONFIG_DIR,
              os.path.join(AGENT_MEMORY_DIR, "context")]:
        os.makedirs(d, exist_ok=True)


def _read_file(path, default=""):
    """读整个文件；文件不存在或读失败时返回 default。"""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
    except Exception:
        pass
    return default


def _atomic_write(path, content):
    """原子写入：先写临时文件再替换，防止写一半进程退出留下坏文件。"""
    _ensure_dirs()
    with _WRITE_LOCK:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)


def _append_line(path, text):
    """追加一行文本。"""
    _ensure_dirs()
    with _WRITE_LOCK:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")


def _now_str():
    """格式化时间：2026年8月28日12时30分45秒"""
    n = time.localtime()
    return f"{n.tm_year}年{n.tm_mon}月{n.tm_mday}日{n.tm_hour}时{n.tm_min}分{n.tm_sec}秒"


def _today_str():
    """格式化日期：2026-08-28"""
    n = time.localtime()
    return f"{n.tm_year}-{n.tm_mon:02d}-{n.tm_mday:02d}"


def _now_dt():
    """格式化时间戳：2026-08-28 12:30:45"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _session_id():
    return f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def _extract_frontmatter(content):
    """把文件开头的 --- yaml --- 元数据拆出来，返回 (meta, 正文)。"""
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
                return meta, parts[2].strip()
            except Exception:
                pass
    return {}, content


def _build_frontmatter(meta):
    return "---\n" + yaml.dump(meta, allow_unicode=True, default_flow_style=False).strip() + "\n---\n"


def get_config():
    """读取记忆配置（memory_config.yaml，可以覆盖默认值）。"""
    global _config
    if _config is None:
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    _config = {**DEFAULT_CONFIG, **yaml.safe_load(f)}
            else:
                _config = dict(DEFAULT_CONFIG)
        except Exception:
            _config = dict(DEFAULT_CONFIG)
    return _config


# ============================================================
# 一、上下文（当前会话）
# ============================================================

def init_context():
    """重置当前会话上下文（写入空模板）。"""
    meta = {"session_id": _session_id(), "started_at": _now_dt(), "updated_at": _now_dt()}
    body = """# 当前会话上下文

## 对话历史

## 实体与槽位

## 待澄清问题
"""
    _atomic_write(CONTEXT_FILE, _build_frontmatter(meta) + body)


def load_context():
    """读取上下文文件，返回 (meta, 正文)。文件为空时先初始化。"""
    raw = _read_file(CONTEXT_FILE, "")
    if not raw.strip():
        init_context()
        raw = _read_file(CONTEXT_FILE, "")
    return _extract_frontmatter(raw)


def get_context():
    """返回上下文正文。"""
    _, body = load_context()
    return body.strip()


def get_context_with_meta():
    return load_context()


def save_context(body, meta=None):
    """整体保存上下文正文（会更新 updated_at）。"""
    current_meta, _ = load_context()
    if meta:
        current_meta.update(meta)
    current_meta["updated_at"] = _now_dt()
    _atomic_write(CONTEXT_FILE, _build_frontmatter(current_meta) + body.strip() + "\n")


def append_context(identity, text):
    """向上下文追加一行：时间:[身份]:文本"""
    meta, body = load_context()
    line = f"{_now_str()}:[{identity}]:{text}"
    meta["updated_at"] = _now_dt()
    _atomic_write(CONTEXT_FILE, _build_frontmatter(meta) + body.rstrip() + "\n" + line + "\n")


def clear_context():
    """清空上下文（等于开一个新会话）。"""
    init_context()
    _atomic_write(DIALOG_LOG_FILE, "")


def get_context_size():
    """计算上下文用了多少 token（估算法）。"""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(_read_file(CONTEXT_FILE, "")))
    except Exception:
        return len(_read_file(CONTEXT_FILE, "")) // 2


def is_context_overflow():
    return get_context_size() > CONTEXT_MAX_SIZE


def get_recent_context(lines_count=20):
    """返回最近 N 行上下文。"""
    lines = get_context().splitlines()
    recent = lines[-lines_count:] if len(lines) > lines_count else lines
    return "\n".join(recent)


def get_context_dialog_lines():
    """只提取"## 对话历史"下面的行（不含元数据）。"""
    _, body = load_context()
    lines = []
    in_history = False
    for line in body.splitlines():
        if line.strip().startswith("## 对话历史"):
            in_history = True
            continue
        if line.strip().startswith("## "):
            in_history = False
            continue
        if in_history and line.strip():
            lines.append(line.strip())
    return lines


def compress_context(ai_summarize_fn):
    """
    把当前对话压缩成摘要（AI 总结），腾出上下文空间。

    ai_summarize_fn 是"给一段文字返回摘要"的函数（core/summary.py 的 call_summary）。
    """
    meta, body = load_context()
    if not body.strip():
        return False

    archive_current_context()

    dialog_text = "\n".join(get_context_dialog_lines())
    if not dialog_text.strip():
        return False

    prompt = (
        "以下是一段 AI 对话的上下文记录。请完成以下任务：\n"
        "1. 将对话历史压缩为一段简洁的摘要，保留关键信息、已做出的决策、用户身份信息\n"
        "2. 提取对话中出现的实体、时间、任务\n"
        "3. 列出尚未澄清的待办事项\n\n"
        "输出格式：\n"
        "【对话摘要】\n...\n\n【实体信息】\n...\n\n【待办事项】\n...\n\n"
        "对话记录：\n" + dialog_text
    )

    compressed = ai_summarize_fn(prompt) if callable(ai_summarize_fn) else ""
    if not compressed:
        return False

    meta["compressed_at"] = _now_dt()
    meta["compressed_count"] = meta.get("compressed_count", 0) + 1

    new_body = f"""## 对话历史
{_now_str()}:[System]:[上下文已压缩，共 {meta['compressed_count']} 次]
{_now_str()}:[System]:{compressed}

## 实体与槽位

## 待澄清问题
"""
    _atomic_write(CONTEXT_FILE, _build_frontmatter(meta) + new_body)
    return True


def archive_current_context():
    """把当前上下文存一份到 archive/，然后重置上下文。"""
    raw = _read_file(CONTEXT_FILE, "")
    if raw.strip():
        meta, _ = _extract_frontmatter(raw)
        sid = meta.get("session_id", _session_id())
        _atomic_write(os.path.join(ARCHIVE_DIR, f"{sid}.md"), raw)
    init_context()


# ============================================================
# 二、长期记忆（分主题）
# ============================================================

def get_forever_memory():
    """拼出长期记忆全文：索引 + 规则 + 各主题文件。"""
    parts = []
    index_content = _read_file(INDEX_FILE, "").strip()
    if index_content:
        parts.append("【记忆索引】\n" + index_content)
    rules = _read_file(RULES_FILE, "").strip()
    if rules:
        parts.append("【行为规则】\n" + rules)
    if os.path.exists(LONG_TERM_DIR):
        for fname in sorted(os.listdir(LONG_TERM_DIR)):
            if fname.startswith("topic_") and fname.endswith(".md"):
                content = _read_file(os.path.join(LONG_TERM_DIR, fname), "").strip()
                if content:
                    _, topic_body = _extract_frontmatter(content)
                    display = fname.replace("topic_", "").replace(".md", "")
                    parts.append(f"【{display}】\n{topic_body}")
    return "\n\n".join(parts) if parts else "（暂无长期记忆）"


def _classify_topic(content):
    """按关键词把内容归到某个主题。"""
    kw_map = {
        "用户": ["我叫", "我是", "我的名字", "称呼", "姓名", "年龄", "生日", "性别", "住址"],
        "工作": ["工作", "项目", "代码", "编程", "开发", "部署", "架构", "后端", "前端"],
        "偏好": ["喜欢", "讨厌", "偏好", "习惯", "想要", "希望", "爱吃", "爱玩"],
        "技术": ["Python", "Java", "docker", "kubernetes", "linux", "数据库", "API"],
    }
    for topic, kws in kw_map.items():
        for kw in kws:
            if kw in content:
                return topic
    return "通用"


def append_forever_memory(content):
    """往长期记忆追加一条记录，并重建索引。"""
    content = content.strip()
    if not content:
        return

    # 用"主题"这个关键词把内容分类，然后存到对应文件
    auto_classify = get_config().get("long_term", {}).get("auto_classify", True)
    topic = _classify_topic(content) if auto_classify else "通用"
    safe_topic = re.sub(r'[\\/:*?"<>|]', "_", topic)
    fpath = os.path.join(LONG_TERM_DIR, f"topic_{safe_topic}.md")

    meta, topic_body = {}, ""
    if os.path.exists(fpath):
        raw = _read_file(fpath, "")
        if raw.strip():
            meta, topic_body = _extract_frontmatter(raw)
    if not meta:
        meta = {"type": "long_term", "topic": topic, "tags": [],
                "created": _today_str(), "expire": None}

    meta["updated"] = _today_str()
    appendix = f"- {_now_str()}: {content}"
    new_body = (topic_body + "\n" + appendix) if topic_body else f"# {topic}\n{appendix}"
    _atomic_write(fpath, _build_frontmatter(meta) + new_body.strip() + "\n")
    _rebuild_index()


def _rebuild_index():
    """重建长期记忆索引表（index.md 那棵表格）。"""
    lines = ["# 长期记忆索引\n", "| 主题 | 摘要 | 最后更新 |\n", "|------|------|----------|\n"]
    if os.path.exists(LONG_TERM_DIR):
        for fname in sorted(os.listdir(LONG_TERM_DIR)):
            if fname.startswith("topic_") and fname.endswith(".md"):
                raw = _read_file(os.path.join(LONG_TERM_DIR, fname), "")
                if raw.strip():
                    meta, topic_body = _extract_frontmatter(raw)
                    display = fname.replace("topic_", "").replace(".md", "")
                    summary = topic_body.strip().split("\n")[0][:60] if topic_body.strip() else ""
                    updated = meta.get("updated", "未知")
                    lines.append(f"| {display} | {summary} | {updated} |\n")
    _atomic_write(INDEX_FILE, "".join(lines))


def get_rule(key=None):
    """读取行为规则文件。（key 参数保留兼容，无实际作用）"""
    content = _read_file(RULES_FILE, "").strip()
    return content or "（暂无行为规则）"


def save_rule(rule):
    _append_line(RULES_FILE, f"- {_today_str()}: {rule}")


def list_topics():
    """列出所有长期记忆的主题名。"""
    topics = []
    if os.path.exists(LONG_TERM_DIR):
        for fname in sorted(os.listdir(LONG_TERM_DIR)):
            if fname.startswith("topic_") and fname.endswith(".md"):
                topics.append(fname.replace("topic_", "").replace(".md", ""))
    return topics


def search_longterm(keyword):
    """在长期记忆里搜关键词，返回命中的主题。"""
    results = []
    if not keyword or not os.path.exists(LONG_TERM_DIR):
        return results
    for fname in os.listdir(LONG_TERM_DIR):
        if fname.startswith("topic_") and fname.endswith(".md"):
            if keyword in _read_file(os.path.join(LONG_TERM_DIR, fname), ""):
                results.append({"topic": fname.replace("topic_", "").replace(".md", ""), "file": fname})
    return results


# ============================================================
# 三、对话日志与计数
# ============================================================

def append_dialog_log(identity, text):
    """把一轮对话追加到 dialog_log.txt（每日总结用）。"""
    _append_line(DIALOG_LOG_FILE, f"{_now_str()}:[{identity}]:{text}")


def get_today_dialogs():
    """取今天的对话日志。"""
    today = _today_str()
    lines = _read_file(DIALOG_LOG_FILE, "").splitlines()
    today_lines = [line for line in lines if line.startswith(today)]
    return "\n".join(today_lines) if today_lines else "（今日暂无对话）"


def clear_dialog_log():
    _atomic_write(DIALOG_LOG_FILE, "")


def get_dialog_count():
    """读当前累计对话轮数。"""
    try:
        return int(_read_file(DIALOG_COUNT_FILE, "0").strip())
    except Exception:
        return 0


def increment_dialog():
    count = get_dialog_count() + 1
    _atomic_write(DIALOG_COUNT_FILE, str(count))
    return count


def reset_dialog_count():
    _atomic_write(DIALOG_COUNT_FILE, "0")


def should_summarize_memory():
    return get_dialog_count() >= SUMMARY_TRIGGER


# ============================================================
# 四、每日记忆
# ============================================================

def save_daily_memory(summary):
    """把今天的总结存到 daily/<日期>.md。"""
    today = _today_str()
    fpath = os.path.join(DAILY_DIR, f"{today}.md")
    meta = {"type": "daily", "date": today, "generated_at": _now_str()}
    body = f"# {today} 记忆摘要\n生成时间: {_now_str()}\n\n{summary}\n"
    _atomic_write(fpath, _build_frontmatter(meta) + body)


def get_daily_memory(date_str=None):
    """读某天的每日记忆；date_str 缺省是今天。"""
    date_str = date_str or _today_str()
    raw = _read_file(os.path.join(DAILY_DIR, f"{date_str}.md"), "")
    if not raw.strip():
        return f"（{date_str} 暂无每日记忆）"
    _, body = _extract_frontmatter(raw)
    return body.strip()


def list_daily_memories():
    """列出所有有每日记忆的日期。"""
    if not os.path.exists(DAILY_DIR):
        return []
    return [f.replace(".md", "") for f in sorted(os.listdir(DAILY_DIR)) if f.endswith(".md")]


# ============================================================
# 五、记忆检索
# ============================================================

def retrieve(query, max_results=5):
    """在长期记忆、每日记忆、归档会话三处搜关键词。"""
    if not query or not query.strip():
        return []
    query = query.strip().lower()
    results = []

    # 1. 长期记忆
    if os.path.exists(LONG_TERM_DIR):
        for fname in sorted(os.listdir(LONG_TERM_DIR)):
            if fname.startswith("topic_") and fname.endswith(".md"):
                content = _read_file(os.path.join(LONG_TERM_DIR, fname), "")
                if query in content.lower():
                    _, topic_body = _extract_frontmatter(content)
                    lines = [l for l in topic_body.splitlines() if query in l.lower()]
                    meta, _ = _extract_frontmatter(content)
                    results.append({
                        "source": "长期记忆",
                        "topic": fname.replace("topic_", "").replace(".md", ""),
                        "matches": lines[:3],
                        "updated": meta.get("updated", ""),
                    })

    # 2. 每日记忆
    if os.path.exists(DAILY_DIR):
        for fname in sorted(os.listdir(DAILY_DIR), reverse=True):
            if fname.endswith(".md"):
                content = _read_file(os.path.join(DAILY_DIR, fname), "")
                if query in content.lower():
                    lines = [l for l in content.splitlines() if query in l.lower()]
                    results.append({
                        "source": "每日记忆",
                        "date": fname.replace(".md", ""),
                        "matches": lines[:3],
                    })

    # 3. 归档会话
    if os.path.exists(ARCHIVE_DIR):
        for fname in sorted(os.listdir(ARCHIVE_DIR), reverse=True):
            if fname.endswith(".md"):
                content = _read_file(os.path.join(ARCHIVE_DIR, fname), "")
                if query in content.lower():
                    lines = [l for l in content.splitlines() if query in l.lower()]
                    results.append({
                        "source": "归档会话",
                        "session": fname.replace(".md", ""),
                        "matches": lines[:3],
                    })

    return results[:max_results]


def search_memory(query, max_results=5):
    return retrieve(query, max_results)


def get_store():
    """保留兼容入口：新代码不需要用，各功能都是同名模块函数。"""
    return None
