"""
QQ 会话池：跨 HTTP/WebSocket 请求保留多轮上下文。

为什么需要会话池：
  HTTP 是无状态的，每次请求都是独立的。但 QQ 对话需要多轮上下文
  （"刚才说的那个文件"要能接住），所以用一个全局 OrderedDict 缓存 session。

键的规则（决定上下文隔离粒度）：
  - 群聊：f"group:{group_qq}"    -- 同一群所有人共享上下文
  - 私信：f"private:{sender_qq}" -- 每个发送人独立上下文

淘汰策略：LRU + 空闲超时
  - OrderedDict 按访问顺序排，最近访问的挪到末尾
  - 超过 QQ_SESSION_TTL 秒没消息的会话会被清掉
"""
import time
import asyncio
from collections import OrderedDict
from core.permission_manager import PermissionManager
from core.session_context import SessionContext

# 会话空闲超时（秒），60 分钟无消息则淘汰
QQ_SESSION_TTL = 3600

# 全局会话池：key -> (SessionContext, 最后访问时间戳)
_qq_sessions: "OrderedDict[str, tuple[SessionContext, float]]" = OrderedDict()

# 异步锁：保护 _qq_sessions 的读写（多个 QQ 请求可能并发）
_qq_sessions_lock = asyncio.Lock()


def qq_session_key(chat_type: str, sender_qq: str, group_qq: str) -> str:
    """
    根据聊天类型/发送人/群号生成会话键。

    参数:
        chat_type: "private" 或 "group"
        sender_qq: 发送人 QQ 号
        group_qq: 群号（群聊场景）

    返回:
        str: 会话键，如 "private:123456" 或 "group:789012"
    """
    if chat_type == "group":
        gq = (group_qq or "").strip() or "unknown_group"
        return f"group:{gq}"
    sq = (sender_qq or "").strip() or "unknown_user"
    return f"private:{sq}"


async def get_qq_session(chat_type: str, sender_qq: str, group_qq: str, workdir: str) -> SessionContext:
    """
    获取或创建 QQ 会话，自动清理过期会话。

    参数:
        chat_type: "private" 或 "group"
        sender_qq: 发送人 QQ 号
        group_qq: 群号
        workdir: 工作目录（用于 PermissionManager 初始化）

    返回:
        SessionContext: 该会话的上下文管理器
    """
    key = qq_session_key(chat_type, sender_qq, group_qq)
    now = time.time()
    async with _qq_sessions_lock:
        # 1. 淘汰过期会话
        expired = [k for k, (_, ts) in _qq_sessions.items() if now - ts > QQ_SESSION_TTL]
        for k in expired:
            _qq_sessions.pop(k, None)

        # 2. 已存在 -> 提到末尾（LRU 更新）并返回
        if key in _qq_sessions:
            session, _ = _qq_sessions.pop(key)
            _qq_sessions[key] = (session, now)
            return session

        # 3. 新建会话
        perm_mgr = PermissionManager(workdir)
        session = SessionContext(perm_mgr=perm_mgr)
        _qq_sessions[key] = (session, now)
        return session


async def clear_qq_session(chat_type: str, sender_qq: str, group_qq: str) -> bool:
    """
    根据聊天信息删一个会话（给 QQ WebSocket 的 clear 命令用）。

    返回:
        bool: True=找到了并删了，False=本来就没有
    """
    key = qq_session_key(chat_type, sender_qq, group_qq)
    async with _qq_sessions_lock:
        return _qq_sessions.pop(key, None) is not None


async def clear_qq_session_by_key(key: str) -> bool:
    """根据完整 key 删会话（给已知 key 的场景用）。"""
    async with _qq_sessions_lock:
        return _qq_sessions.pop(key, None) is not None
