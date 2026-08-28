"""完整配置管理器"""

import json

FORMAT_LABELS = {
    "openai": "OpenAI 格式",
    "anthropic": "Anthropic 格式",
}


class Config:
    """`Config/Model.json` 的运行时读写封装。

    用法::

        config = Config("Config/Model.json")
        client = get_client(config.model)
        config.set_model_entry("new-model", entry)

    属性读取的是最近一次 ``load`` / ``reload`` 的快照；修改模型条目后会
    立即写回 JSON。调用方修改文件本身后，应执行 ``reload()`` 刷新快照。
    """
    def __init__(self, path):
        """加载 ``path`` 指向的模型配置；文件缺失或 JSON 无效时使用默认值。"""
        self.path = path
        self.model = "model_1"
        self.models = {}
        self.auto_think = True
        self.auto_effort = True
        self.think_mode = "enabled"
        self.think_effort = "high"
        self.server_host = "0.0.0.0"
        self.server_port = 8520
        self.root_dir = ""
        self.auto_audit = False
        self.load(path)

    def load(self, path):
        """从 JSON 文件加载全部运行配置，并覆盖当前对象的属性。"""
        self.path = path
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}

        self.model = data.get("model", "model_1")
        self.models = data.get("models", {})
        if not self.models or not isinstance(self.models, dict):
            self.models = {}
        self.auto_think = data.get("auto_think", True)
        self.auto_effort = data.get("auto_effort", True)
        thinking = data.get("thinking", {})
        self.think_mode = thinking.get("mode", "enabled")
        self.think_effort = thinking.get("effort", "high")
        self.server_host = data.get("server_host", "0.0.0.0")
        self.server_port = data.get("server_port", 8520)
        self.root_dir = data.get("root_dir", "")
        self.auto_audit = data.get("auto_audit", False)

    def reload(self):
        """重新读取构造时或上次 ``load`` 指定的配置文件。"""
        self.load(self.path)

    @property
    def active_entry(self) -> dict:
        return self.models.get(self.model, {})

    @property
    def active_api_key(self) -> str:
        return self.active_entry.get("api_key", "")

    @property
    def active_model_name(self) -> str:
        return self.active_entry.get("model_name", "")

    @property
    def active_api_url(self) -> str:
        return self.active_entry.get("url", "")

    @property
    def active_provider(self) -> str:
        return self.active_entry.get("接口", "openai")

    @property
    def is_valid(self) -> bool:
        return bool(self.active_api_key)

    @property
    def model_ids(self) -> list[str]:
        return list(self.models.keys())

    def get_entry(self, model_id: str) -> dict:
        """返回指定模型条目；模型不存在时返回空字典，不抛异常。"""
        return self.models.get(model_id, {})

    def set_model_entry(self, model_id: str, entry: dict):
        """新增或覆盖一个模型条目，并持久化 ``models`` 字段。

        ``entry`` 支持 ``接口``、``model_name``、``api_key``、``url``；未提供的
        字段按空值或 OpenAI 兼容接口处理。
        """
        self.models[model_id] = {
            "接口": entry.get("接口", "openai"),
            "model_name": entry.get("model_name", ""),
            "api_key": entry.get("api_key", ""),
            "url": entry.get("url", ""),
        }
        self._save_field("models", self.models)

    def remove_model_entry(self, model_id: str):
        """删除模型；若删的是当前模型，自动切换到剩余的第一个模型。"""
        self.models.pop(model_id, None)
        if self.model == model_id and self.models:
            self.model = next(iter(self.models))
            self._save_field("model", self.model)
        self._save_field("models", self.models)

    def _save_field(self, key: str, value):
        """保留其它 JSON 字段，仅写回一个顶层配置字段。"""
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        data[key] = value
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
