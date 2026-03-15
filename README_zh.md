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
git clone https://github.com/quink-black/tmux-mcp-agent.git
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
| `tmux_check_remote_tasks` | 📊 监控并行任务状态（含退出码和时长） |
| `tmux_kill_remote_tasks` | 🗑️ 终止单个任务或整个远程 session |
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

在远程主机上并行执行多个耗时命令，**断连不丢失** — 即使 SSH 连接中断，任务仍在后台继续运行。

#### 工作原理

```
┌─────────────────────────────────────────────────────────────┐
│                    你的机器（本地）                            │
│                                                             │
│  ┌──────────────┐         ┌──────────────────────┐          │
│  │  tmux pane    │ ◄─────► │  AI Agent (MCP)      │          │
│  │  SSH 连接     │         │  mcp_server.py       │          │
│  └──────┬───────┘         └──────────────────────┘          │
└─────────┼───────────────────────────────────────────────────┘
          │ SSH（可以断开！）
    ┌─────▼───────────────────────────────────────────────┐
    │                  远程主机                             │
    │                                                     │
    │   tmux session "ai_work"    ← SSH 断连后继续运行      │
    │   ┌──────────┐ ┌──────────┐ ┌──────────┐           │
    │   │ window 0  │ │ window 1  │ │ window 2  │         │
    │   │ "build"   │ │ "test"    │ │ "deploy"  │         │
    │   │ make build│ │ make test │ │ ./deploy  │         │
    │   └──────────┘ └──────────┘ └──────────┘           │
    │                                                     │
    │   /tmp/_tmux_tasks_ai_work/    ← 状态追踪文件         │
    │   ├── build.status   (启动时间、退出码、耗时等)        │
    │   ├── test.status                                   │
    │   └── deploy.status                                 │
    └─────────────────────────────────────────────────────┘
```

**关键设计**：AI Agent 通过本地 tmux pane 向远程主机发送 `tmux new-session` 命令，在**远程主机内部**创建一个独立的 tmux session。每个任务在独立的 window 中运行，并被包装了状态追踪（记录启动时间、退出码、耗时）。即使本地 SSH 断开，远程 tmux session 依然存活。

#### 完整工作流

**第 1 步：启动并行任务**

告诉 AI Agent：
> "在远程服务器上并行运行 `make build`、`make test` 和 `tail -f /var/log/app.log`"

AI 调用 `tmux_remote_parallel`，它会：
- 在远程主机上创建 tmux session（不是本地的）
- 每个命令在独立 window 中运行，带状态追踪
- 自动记录启动时间，命令完成后记录退出码和耗时

**第 2 步：查看进度**

告诉 AI Agent：
> "看看远程任务跑得怎么样了"

AI 调用 `tmux_check_remote_tasks`，返回：
```
📊 Remote Tasks Status (session: ai_work):

  ✅ [build] completed (exit code: 0) in 2m34s
      Build successful: 142 targets built
  🔄 [test] running (1m12s elapsed)
      Running test suite: 87/120 passed...
  🔄 [logs] running (3m45s elapsed)
      [2024-01-15 10:23:45] INFO: Request processed in 12ms
```

**第 3 步：终止或清理**

> "停止日志跟踪任务" → `tmux_kill_remote_tasks(window_name='logs')`
> "清理所有远程任务" → `tmux_kill_remote_tasks()`（终止整个 session）

#### 使用场景

| 场景 | 命令示例 |
|------|---------|
| **编译 & 测试** | `['make build', 'make test', 'make lint']` |
| **日志监控** | `['tail -f /var/log/app.log', 'tail -f /var/log/error.log']` |
| **数据处理** | `['python process_batch_1.py', 'python process_batch_2.py']` |
| **部署流水线** | `['docker build -t app .', 'npm run build', 'python manage.py migrate']` |
| **系统诊断** | `['vmstat 1', 'iostat -x 1', 'tail -f /var/log/syslog']` |

#### 进阶：自定义窗口名称

```
tmux_remote_parallel(
    commands=['make build', 'pytest -v', 'flake8 .'],
    window_names=['build', 'test', 'lint'],
    session_name='ci_pipeline'
)
```

给每个窗口起有意义的名字，在 `tmux_check_remote_tasks` 输出中更容易识别。

#### 进阶：复用已有 Session

向已在运行的 session 中追加任务，不会终止之前的任务：

```
tmux_remote_parallel(
    commands=['tail -f /var/log/nginx/access.log'],
    window_names=['nginx_logs'],
    session_name='ai_work',
    reuse_session=True
)
```

#### SSH 断连恢复

如果任务执行过程中 SSH 连接断开：
1. 远程 tmux session 中的任务继续运行，不受影响
2. 手动重新连接 SSH（或让 AI 通过 `tmux_send_keys` 发送 SSH 命令）
3. 用 `tmux_check_remote_tasks` 查看任务状态 — 它读取状态追踪文件
4. 或手动交互查看：`tmux attach -t ai_work`

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
