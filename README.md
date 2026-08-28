# DC Client

DC Server 的终端客户端，基于 [Textual](https://textual.textualize.io/) 构建的美观 TUI 界面。

## 功能特性

- 连接 DC Server 进行 AI 对话
- 支持多种主题切换（ocean、midnight、forest 等）
- 流式输出显示
- 思考过程展示
- 工具调用权限管理
- 本地对话历史保存
- 自动启动本地服务端

## 快速开始

### 安装依赖

```bash
pip install -r requirements.txt
```

### 启动客户端

```bash
# 使用启动脚本（推荐）
dccode.bat

# 或直接运行
python dccode.py
```

## 命令列表

| 命令 | 说明 |
|------|------|
| `/help` | 显示帮助 |
| `/clear` | 清除对话上下文 |
| `/model` | 查看当前模型 |
| `/config` | 查看/修改配置 |
| `/settings` | 打开外观设置 |
| `/theme <名称>` | 快速切换主题 |
| `/approve` | 当前对话自动批准工具请求 |
| `/think` | 调整 AI 思考模式 |
| `/mcp` | 管理外部 MCP 服务器 |
| `/reconnect` | 重新连接服务器 |
| `/restart` | 重启服务端 |
| `/sessions` | 列出历史对话 |
| `/resume <编号>` | 恢复历史对话 |
| `/new` | 新建对话 |
| `/quit` | 退出程序 |

## 配置

配置文件 `config.json` 会在首次运行时自动生成，可手动修改：

```json
{
  "server_host": "127.0.0.1",
  "server_port": 8520,
  "auto_start_local_server": true,
  "appearance": {
    "theme": "ocean",
    "animations": true,
    "animation_speed": "normal",
    "accent": null,
    "user_color": null,
    "assistant_color": null,
    "think_color": null,
    "error_color": null,
    "tool_color": null,
    "logo_color": null
  }
}
```

### 连接配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `server_host` | string | `"127.0.0.1"` | DC Server 的 IP 地址 |
| `server_port` | integer | `8520` | DC Server 的端口号 |
| `auto_start_local_server` | boolean | `true` | 自动启动本地 DC Server |

### 外观配置 (appearance)

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `theme` | string | `"ocean"` | 主题名称（可选：ocean、ocean-dark、midnight、forest、sunset、noir） |
| `animations` | boolean | `true` | 是否启用动画效果 |
| `animation_speed` | string | `"normal"` | 动画速度（可选：fast、normal、slow） |
| `accent` | string/null | `null` | 强调色（#RRGGBB 格式，null 为默认） |
| `user_color` | string/null | `null` | 用户消息颜色（#RRGGBB 格式，null 为默认） |
| `assistant_color` | string/null | `null` | AI 消息颜色（#RRGGBB 格式，null 为默认） |
| `think_color` | string/null | `null` | 思考内容颜色（#RRGGBB 格式，null 为默认） |
| `error_color` | string/null | `null` | 报错信息颜色（#RRGGBB 格式，null 为默认） |
| `tool_color` | string/null | `null` | 工具调用颜色（#RRGGBB 格式，null 为默认） |
| `logo_color` | string/null | `null` | LOGO 高亮颜色（#RRGGBB 格式，null 为默认） |

### 主题

可用主题：`ocean`（默认）、`ocean-dark`、`midnight`、`forest`、`sunset`、`noir`

### 颜色配置示例

```json
{
  "appearance": {
    "accent": "#38bdf8",
    "user_color": "#34d399",
    "assistant_color": "#a78bfa"
  }
}
```

## 项目结构

```
DC Client/
├── dccode.py          # 主程序
├── dccode.bat         # 启动脚本
├── requirements.txt   # 依赖列表
├── config.json        # 配置文件（自动生成）
└── conversations.json # 对话历史（自动生成）
```

## 许可证

MIT License
