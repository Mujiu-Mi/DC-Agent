import os


PATH_TOOLS = {
    "read_file", "write_file", "edit_file", "delete_file", "list_dir", "create_dir",
    "move_file", "search_code", "batch_read",
}


def extract_tool_path(name: str, args: dict) -> str | None:
    """从工具调用参数中取出需要做工作目录校验的路径。

    返回 ``None`` 表示该工具没有可可靠判断的单一路径，例如 ``run_cmd``。
    此函数必须与 ``PATH_TOOLS`` 保持同步，供 MCP 内置工具执行闸门调用。
    """
    if name in ("read_file", "write_file", "edit_file", "delete_file", "create_dir"):
        return args.get("path", "")
    if name == "list_dir":
        return args.get("path", ".")
    if name == "move_file":
        return args.get("src", "")
    if name == "run_cmd":
        return None
    if name == "search_code":
        return args.get("path", ".")
    if name == "batch_read":
        return args.get("paths", "").split(",")[0].strip() if args.get("paths") else None
    return None


class PermissionManager:
    """维护每个会话的文件系统访问范围。

    默认只允许 ``root_dir`` 内的路径。前端审批 ``once`` 时只放行当前轮，
    ``always`` 时放行到会话结束；调用 ``end_turn`` 会清除前者。
    """
    def __init__(self, root_dir=""):
        """以 ``root_dir`` 建立沙盒边界；空字符串表示不限制路径。"""
        self.root_dir = os.path.abspath(root_dir) if root_dir else ""
        self._session_allows = set()
        self._turn_allows = set()

    def set_root(self, root_dir):
        """切换沙盒根目录，并清除该会话此前授予的额外路径权限。"""
        self.root_dir = os.path.abspath(root_dir) if root_dir else ""
        self.reset()

    @property
    def has_root(self) -> bool:
        return bool(self.root_dir)

    def is_allowed(self, path: str) -> bool:
        """判断 ``path`` 是否位于根目录或已获用户授权的额外目录。"""
        if not self.root_dir:
            return True
        if not path:
            return True
        abs_path = os.path.abspath(path)
        if abs_path.startswith(self.root_dir):
            return True
        if any(abs_path.startswith(a) for a in self._session_allows):
            return True
        if any(abs_path.startswith(a) for a in self._turn_allows):
            return True
        return False

    def grant(self, path: str, mode: str):
        """授权路径。``mode`` 为 ``once``（本轮）或 ``always``（本会话）。"""
        abs_path = os.path.abspath(path)
        if mode == "always":
            self._session_allows.add(abs_path)
        elif mode == "once":
            self._turn_allows.add(abs_path)

    def end_turn(self):
        """结束一轮对话，回收 ``once`` 授权。"""
        self._turn_allows.clear()

    def reset(self):
        """清除所有临时和会话级路径授权。"""
        self._session_allows.clear()
        self._turn_allows.clear()
