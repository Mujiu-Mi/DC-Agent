import uuid
import time


def _now_str():
    n = time.localtime()
    return f"{n.tm_year}年{n.tm_mon}月{n.tm_mday}日{n.tm_hour}时{n.tm_min}分{n.tm_sec}秒"


class SessionContext:
    def __init__(self, perm_mgr=None):
        self.session_id = str(uuid.uuid4())[:8]
        self.perm_mgr = perm_mgr
        self._messages = []
        self._workdir = None
        self._dialog_count = 0

    # ── 上下文（每会话独立） ──

    def add_context(self, identity, text):
        # Prompt caching requires previous messages to retain identical text.
        self._messages.append({"identity": identity, "text": text, "timestamp": _now_str()})

    def set_workdir(self, workdir):
        """Record the current working directory only when it changes."""
        if not workdir or workdir == self._workdir:
            return False
        self._workdir = workdir
        self.add_context("System", f"[当前工作目录] {workdir}")
        return True

    def get_context(self):
        return "\n".join(
            f"{m.get('timestamp', '')}:[{m['identity']}]:{m['text']}"
            for m in self._messages
        )

    def get_context_size(self):
        text = "\n".join(f"[{m['identity']}]:{m['text']}" for m in self._messages)
        try:
            import tiktoken
            enc = tiktoken.get_encoding("cl100k_base")
            return len(enc.encode(text))
        except Exception:
            return len(text) // 2

    def is_context_overflow(self):
        return self.get_context_size() > 256 * 1024

    def get_recent_context(self, lines_count=20):
        lines = [f"[{m['identity']}]:{m['text']}" for m in self._messages]
        recent = lines[-lines_count:] if len(lines) > lines_count else lines
        return "\n".join(recent)

    # ── 对话计数（每会话独立） ──

    def increment_dialog(self):
        self._dialog_count += 1
        return self._dialog_count

    def should_summarize_memory(self):
        return self._dialog_count >= 10

    def reset_dialog_count(self):
        self._dialog_count = 0

    # ── 清理 ──

    def clear_context(self):
        self._messages.clear()
        self._workdir = None
        self._dialog_count = 0
