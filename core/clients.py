"""
AI 客户端工厂

根据 Model.json 里的 "接口" 字段（openai / anthropic）创建对应的客户端实例。
所有需要调 AI 的地方都通过 get_client(model_id) 拿客户端，统一入口。
"""
from openai import OpenAI
from anthropic import Anthropic
from core.MangerConfig import Config

# 全局配置实例（Config 类每次 new 都会重新 load 文件，所以这里 new 一次就够了；
# 配置改了之后调 Model.reload() 即可）
Model = Config("Config/Model.json")


def get_client(model_id: str):
    """
    根据模型 ID 创建 AI 客户端。

    参数:
        model_id: Model.json 里的模型 key（如 "model_1"）

    返回:
        OpenAI() 或 Anthropic() 客户端实例
    """
    entry = Model.get_entry(model_id)
    fmt = entry.get("接口", "openai")
    key = entry.get("api_key", "")
    url = entry.get("url", "")
    if fmt == "anthropic":
        return Anthropic(api_key=key, base_url=url)
    return OpenAI(api_key=key, base_url=url)
