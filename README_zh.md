# tmux-mcp-agent

> 通过 tmux 让 AI Agent 操控你已登录的远程服务器终端 — 适用于需要跳板机登录、无法直接 SSH 的场景。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[English](README.md) | 中文

## 工作原理

```
┌──────────────────────────────────────────────────────┐
│                    你的机器 (本地)                      │
│                                                      │
│  ┌──────────────┐         ┌────────────────────┐     │
│  │  tmux session │ ◄─────► │  AI Agent (MCP)    │     │
│  │  (任意名称)   │         │  mcp_server.py     │     │
│  │              │         │                    │     │
│  │  手动登录:    │         │  send-keys ───►    │     │
│  │  跳板机→服务器 │         │  ◄─── capture-pane │     │
│  └──────┬───────┘         └────────────────────┘     │
│         │                                            │
└─────────┼────────────────────────────────────────────┘
          │ SSH
    ┌─────▼─────┐
    │  跳板机     │  ← 无需部署任何程序
    └─────┬─────┘
          │ SSH
    ┌─────▼─────┐
    │  目标服务器  │  ← AI Agent 实际操控的机器
    └───────────┘
```

**核心思路**: 你手动通过跳板机建立 SSH 连接，AI Agent 通过 tmux API (`send-keys` / `capture-pane`) 向你已建立的会话发送命令并读取输出。所有工具都在本地运行，跳板机和服务器无需任何改动。

## 快速开始

### 1. 安装配置

```bash
git clone https://github.com/your-username/tmux-mcp-agent.git
cd tmux-mcp-agent
bash setup.sh
```

自动完成:
- 检查并安装 tmux
- 创建 Python 虚拟环境
- 安装依赖 (`mcp` SDK)

### 2. 建立远程连接

创建 tmux 会话并手动登录。**会话名称完全自由**：

```bash
tmux new-session -s work
ssh your_jump_host        # 登录跳板机
ssh your_target_server    # 登录目标服务器
# 按 Ctrl+B D 从 tmux 会话中 detach（会话会在后台保持）
```

### 3. 测试

打开**另一个终端窗口**测试：

```bash
python3 tmux_agent.py list                   # 查看所有 tmux 会话
python3 tmux_agent.py -t work:0.0 capture    # 读取屏幕内容
python3 tmux_agent.py -t work:0.0 run "hostname"  # 执行命令
```

## 在 IDE 中集成 (MCP Server)

### CodeBuddy / Cursor 配置

在 IDE 的 MCP 配置文件中添加:

```json
{
  "mcpServers": {
    "tmux-remote": {
      "command": "/path/to/tmux-mcp-agent/.venv/bin/python",
      "args": ["/path/to/tmux-mcp-agent/mcp_server.py"],
      "env": {}
    }
  }
}
```

### 可用的 MCP 工具

| 工具名 | 功能 |
|-------|------|
| `tmux_list_sessions` | 列出所有 tmux 会话 |
| `tmux_list_all_panes` | ⚡ 快速概览所有 session 的所有面板 |
| `tmux_discover_servers` | 🔍 深度扫描：在面板中运行 hostname/whoami |
| `tmux_run_command` | 执行命令并返回输出（智能等待完成） |
| `tmux_capture_pane` | 读取当前屏幕内容 |
| `tmux_send_keys` | 发送原始按键（适用于交互式程序） |
| `tmux_send_ctrl_c` | 发送 Ctrl+C 中断 |
| `tmux_safe_execute` | 🛡️ 带连接安全检查的命令执行 |
| `tmux_connection_guard` | 🔍 检测 SSH 连接状态 |
| `tmux_remote_parallel` | 🚀 在远程 tmux 中并行执行任务 |
| `tmux_check_remote_tasks` | 📊 监控并行任务状态 |
| `tmux_register_server` | 为服务器添加标签，支持自然语言匹配 |
| `tmux_find_server` | 通过自然语言查找服务器 |
| `tmux_health_check` | 快速检查 shell 响应 |
| `tmux_set_pane_title` | 为面板设置标题方便识别 |
| `tmux_create_session` | 创建新的 tmux session |
| `tmux_create_window` | 在 session 中创建新 window |
| `tmux_split_pane` | 水平或垂直分割面板 |
| `tmux_kill_session/window/pane` | 销毁 session、window 或 pane |

### 使用示例

配置完成后，你可以直接让 AI Agent:

- "查看远程服务器的磁盘使用情况"
- "检查远程服务器上 nginx 服务的状态"
- "帮我在远程服务器上查找最近修改的日志文件"
- "帮我部署最新的代码到远程服务器"

## 核心特性

### 🛡️ 连接安全保护

`tmux_safe_execute` 和 `tmux_connection_guard` 可防止 SSH 断连后的危险误操作：

- **健康检查**: shell 是否可响应？
- **主机名验证**: 是否在预期的远程主机上？
- **本地检测**: 如果意外操作了本地机器则阻止命令
- **重连指引**: 检测到断连时给出明确操作指引

### 🚀 远程并行执行

在远程主机上并行执行多个命令，SSH 断连后任务继续运行：

```
tmux_remote_parallel    → 在远程创建 tmux session 并行执行
tmux_check_remote_tasks → 随时检查任务进度
```

### 🎯 智能目标定位

通过多种策略自动找到正确的面板：
1. 显式 `target` 参数
2. `server_hint` 自然语言匹配
3. 面板标题匹配（即时，不发送命令）
4. 唯一服务器时自动选择

## 多服务器支持

```bash
tmux new-session -s dev    # 开发环境
tmux new-session -s ops    # 运维环境
# 在各 session 中登录不同的服务器
```

为服务器添加标签，支持自然语言定位：

```
tmux_register_server(target="dev:0.0", name="Web前端", tags=["prod", "web"])
tmux_run_command(server_hint="web前端", command="uptime")
```

## Python API

```python
from tmux_agent import TmuxAgent

agent = TmuxAgent(session_name="work", pane_target="work:0.0")
output = agent.run_command("ls -la /var/log")
print(output)
```

## 文件结构

```
tmux-mcp-agent/
├── README.md           # 英文文档
├── README_zh.md        # 中文文档
├── tmux_agent.py       # 核心控制器（TmuxAgent 类 + CLI）
├── mcp_server.py       # MCP Server（AI Agent 集成）
├── setup.sh            # 一键安装配置
├── requirements.txt    # Python 依赖
└── LICENSE             # MIT 开源协议
```

## 注意事项

1. **安全性**: 跳板机密码/密钥需要你手动输入，AI Agent 不会接触认证信息
2. **超时**: 默认命令超时 5 分钟（`max_wait=300`），可按需调整
3. **输出限制**: `capture-pane` 默认捕获 200 行，超长输出建议用 `| tail` 或 `| head` 限制
4. **交互式程序**: 如 `vim`, `top` 等，使用 `send_keys` 而非 `run_command`
5. **连接断开**: 如果 SSH 连接断了，需要重新手动登录；使用 `connection_guard` 可自动检测

## 开源协议

[MIT](LICENSE)
