# DC Server - AI Agent 服务端

DC Server 是一个基于 FastAPI 的 AI Agent 服务端，支持多种大语言模型（OpenAI/Anthropic），集成 MCP 工具系统，提供流式对话、记忆管理、权限控制等能力。

## 功能特性

- **多模型支持** - 兼容 OpenAI、Anthropic 格式 API（DeepSeek、Claude、GPT 等）
- **MCP 工具系统** - 26+ 内置工具 + 外部 MCP Server 扩展
- **流式对话** - WebSocket/SSE 实时输出，支持思考过程展示
- **记忆管理** - 长期记忆 + 每日记忆 + 自动总结
- **权限控制** - 工作目录限制、工具执行权限、安全审计
- **多渠道接入** - DC Bot、QQ 等渠道统一接入
- **会话管理** - 多会话池、上下文压缩、Token 统计

## 项目结构

```
DC Server/
├── main.py                 # 服务入口
├── requirements.txt        # Python 依赖
├── Config/
│   ├── Model.json.example  # 模型配置模板
│   └── mcp_servers.example.json  # MCP 配置模板
├── core/                   # 核心模块
│   ├── MangerConfig.py     # 配置管理器
│   ├── clients.py          # LLM 客户端封装
│   ├── prompts.py          # 提示词管理
│   ├── session_context.py  # 会话上下文
│   ├── difficulty.py       # 难度评估
│   ├── summary.py          # 对话总结
│   ├── auditor.py          # 安全审计
│   └── permission_manager.py  # 权限管理
├── chat/                   # 对话核心
│   ├── loop.py             # 聊天主循环
│   ├── stream.py           # 流式输出
│   └── session_pool.py     # 会话池管理
├── routes/                 # HTTP/WebSocket 路由
│   ├── dscat.py            # DC Bot 接入
│   ├── qq.py               # QQ 接入
│   ├── config_api.py       # 配置 API
│   └── misc.py             # 其他接口
├── tools/                  # 工具系统
│   ├── builtin_server.py   # 内置 MCP Server
│   ├── tool.py             # 工具函数实现
│   ├── tool_handler.py     # 工具调用处理
│   ├── mcp_client.py       # MCP 客户端聚合
│   └── safety.py           # 安全检查
├── memory/                 # 记忆系统
│   └── memory_manager.py   # 记忆管理器
├── Prompt/                 # 提示词文件
│   ├── system.md           # 系统指令
│   └── myself.md           # 人设配置
├── Skills/                 # 技能定义
├── utils/                  # 工具函数
│   └── logger.py           # 日志
└── agent_memory/           # 运行时记忆数据（已 gitignore）
```

## 安装部署

### 1. 克隆项目

```bash
git clone https://github.com/Mujiu-Mi/DC-Agent.git
cd DC-Agent
```

### 2. 创建虚拟环境

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux/Mac
source .venv/bin/activate
```

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

### 4. 配置模型

复制配置模板并填入你的 API Key：

```bash
cp Config/Model.json.example Config/Model.json
```

编辑 `Config/Model.json`：

```json
{
  "model": "model_1",
  "models": {
    "model_1": {
      "接口": "openai",
      "model_name": "deepseek-v4-flash",
      "api_key": "你的 API Key",
      "url": "https://api.deepseek.com/v1"
    }
  },
  "auto_think": true,
  "thinking": {
    "mode": "enabled",
    "effort": "high"
  },
  "server_host": "0.0.0.0",
  "server_port": 8520,
  "auto_audit": false
}
```

### 5. （可选）配置外部 MCP Server

```bash
cp Config/mcp_servers.example.json Config/mcp_servers.json
```

编辑 `Config/mcp_servers.json` 添加外部工具服务。

### 6. 启动服务

```bash
python main.py
```

服务默认监听 `http://0.0.0.0:8520`

## API 接口

### 对话接口

| 路径 | 方法 | 说明 |
|------|------|------|
| `/dscat/chat` | POST | HTTP 对话（支持流式） |
| `/dscat/ws` | WebSocket | WebSocket 实时对话 |
| `/qq/chat` | POST | QQ 渠道对话 |
| `/qq/ws` | WebSocket | QQ WebSocket |

### 配置接口

| 路径 | 方法 | 说明 |
|------|------|------|
| `/config/models` | GET | 列出所有模型 |
| `/config/model` | POST | 添加/修改模型 |
| `/config/model/{id}` | DELETE | 删除模型 |
| `/config/model/{id}/select` | POST | 切换当前模型 |
| `/config/mcp` | GET | 列出 MCP 配置 |
| `/config/mcp` | POST | 添加 MCP Server |
| `/config/mcp/{name}` | DELETE | 删除 MCP Server |

### 其他接口

| 路径 | 方法 | 说明 |
|------|------|------|
| `/models` | GET | 模型列表（不含 Key） |
| `/usage_stats` | GET | Token 用量统计 |
| `/compress_context` | POST | 压缩上下文 |

## 内置工具

| 工具 | 说明 |
|------|------|
| `read_file` | 读取文件 |
| `write_file` | 写入文件 |
| `edit_file` | 精确编辑文件 |
| `list_dir` | 列出目录 |
| `create_dir` | 创建目录 |
| `delete_file` | 删除文件 |
| `move_file` | 移动/重命名 |
| `run_cmd` | 执行系统命令 |
| `python_exec` | 执行 Python 代码 |
| `web_search` | 网页搜索 |
| `http_get` / `http_post` | HTTP 请求 |
| `search_code` | 代码搜索 |
| `batch_read` | 批量读取文件 |
| `ssh_connect` / `ssh_exec` / `ssh_disconnect` | SSH 远程操作 |
| `read_memory` / `write_memory` / `search_memory` | 记忆操作 |
| `get_current_time` | 获取时间 |
| `get_system_info` | 系统信息 |
| `get_env_info` | 环境信息 |
| `get_location` | 位置信息 |

## 配置说明

### 模型配置 (Model.json)

```json
{
  "model": "model_1",           // 当前使用的模型 ID
  "models": {                   // 模型列表
    "model_1": {
      "接口": "openai",         // openai / anthropic
      "model_name": "...",      // 模型名称
      "api_key": "...",         // API Key
      "url": "..."              // API 地址
    }
  },
  "auto_think": true,           // 自动思考模式
  "thinking": {
    "mode": "enabled",          // enabled / disabled
    "effort": "high"            // high / medium / low
  },
  "server_host": "0.0.0.0",     // 监听地址
  "server_port": 8520,          // 监听端口
  "auto_audit": false           // 工具安全审计
}
```

### MCP 配置 (mcp_servers.json)

```json
[
  {
    "name": "weather",
    "transport": "stdio",       // stdio / sse / http
    "command": "python",
    "args": ["-m", "weather_server"]
  },
  {
    "name": "api",
    "transport": "sse",
    "url": "http://localhost:8080/sse"
  }
]
```

## 安全机制

1. **工作目录限制** - 文件操作限制在授权目录内
2. **危险命令拦截** - 自动阻止 `rm -rf`、系统路径访问等
3. **工具审计** - 可选的 AI 二次审核机制
4. **权限弹窗** - WebSocket 模式支持工具执行前确认

## 开发说明

### 新增工具

1. 在 `tools/tool.py` 添加函数
2. 在 `tools/builtin_server.py` 用 `@register` 装饰

```python
# tools/tool.py
def my_tool(param: str) -> str:
    """工具说明"""
    return "结果"

# tools/builtin_server.py
@register
def my_tool(param: str) -> str:
    """工具说明"""
    return tool.my_tool(param=param)
```

### 提示词定制

- `Prompt/system.md` - 系统行为指令
- `Prompt/myself.md` - AI 人设配置

## License

MIT
