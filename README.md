# tmux-mcp-agent

> Let AI Agents control your remote servers through tmux — perfect for jump-host environments where direct SSH isn't possible.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

## How It Works

```
┌──────────────────────────────────────────────────────┐
│                  Your Machine (Local)                 │
│                                                      │
│  ┌──────────────┐         ┌────────────────────┐     │
│  │  tmux session │ ◄─────► │  AI Agent (MCP)    │     │
│  │  (any name)   │         │  mcp_server.py     │     │
│  │              │         │                    │     │
│  │  Manual login:│         │  send-keys ───►    │     │
│  │  jump → target│         │  ◄─── capture-pane │     │
│  └──────┬───────┘         └────────────────────┘     │
│         │                                            │
└─────────┼────────────────────────────────────────────┘
          │ SSH
    ┌─────▼─────┐
    │ Jump Host  │  ← No deployment needed
    └─────┬─────┘
          │ SSH
    ┌─────▼─────┐
    │  Target    │  ← Server the AI agent controls
    └───────────┘
```

**Core idea**: You manually establish SSH connections through jump hosts. The AI Agent uses tmux API (`send-keys` / `capture-pane`) to send commands and read output from your established sessions. Everything runs locally — no changes needed on jump hosts or target servers.

## Quick Start

### 1. Install

```bash
git clone https://github.com/your-username/tmux-mcp-agent.git
cd tmux-mcp-agent
bash setup.sh
```

This will automatically:
- Check and install tmux
- Create a Python virtual environment
- Install dependencies (`mcp` SDK)

### 2. Establish Remote Connection

Create a tmux session and login manually. **Session names are completely flexible**:

```bash
tmux new-session -s work
ssh your_jump_host        # login to jump host
ssh your_target_server    # login to target server
# Press Ctrl+B D to detach (session stays in background)
```

### 3. Test

Open **another terminal** to test:

```bash
python3 tmux_agent.py list                   # list all tmux sessions
python3 tmux_agent.py -t work:0.0 capture    # read screen content
python3 tmux_agent.py -t work:0.0 run "hostname"  # run a command
```

## IDE Integration (MCP Server)

### CodeBuddy / Cursor Configuration

Add to your IDE's MCP config:

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

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `tmux_list_sessions` | List all tmux sessions |
| `tmux_list_all_panes` | ⚡ Fast overview of all panes across sessions |
| `tmux_discover_servers` | 🔍 Deep scan: run hostname/whoami in panes |
| `tmux_run_command` | Execute command and return output (smart wait) |
| `tmux_capture_pane` | Read current screen content |
| `tmux_send_keys` | Send raw keys (for interactive programs) |
| `tmux_send_ctrl_c` | Send Ctrl+C to interrupt |
| `tmux_safe_execute` | 🛡️ Run command with connection safety checks |
| `tmux_connection_guard` | 🔍 Check SSH connection status |
| `tmux_remote_parallel` | 🚀 Run parallel tasks in remote tmux |
| `tmux_check_remote_tasks` | 📊 Monitor parallel task status with exit codes & duration |
| `tmux_kill_remote_tasks` | 🗑️ Stop individual tasks or kill entire remote session |
| `tmux_register_server` | Tag servers for natural language matching |
| `tmux_find_server` | Find server by natural language query |
| `tmux_health_check` | Quick shell responsiveness check |
| `tmux_set_pane_title` | Label panes for easy identification |
| `tmux_create_session` | Create a new tmux session |
| `tmux_create_window` | Create a new window in a session |
| `tmux_split_pane` | Split a pane horizontally or vertically |
| `tmux_kill_session/window/pane` | Destroy sessions, windows, or panes |

### Usage Examples

After configuration, you can ask the AI Agent:

- "Check disk usage on the remote server"
- "Check nginx status on the target server"
- "Find recently modified log files"
- "Deploy the latest code to production"

## Key Features

### 🛡️ Connection Safety

The `tmux_safe_execute` and `tmux_connection_guard` tools prevent dangerous misoperations when SSH connections drop:

- **Health check**: Is the shell responsive?
- **Hostname verification**: Are we on the expected remote host?
- **Local detection**: Block commands if accidentally targeting local machine
- **Reconnection guidance**: Clear instructions when disconnection is detected

### 🚀 Remote Parallel Execution

Run multiple long-running commands in parallel on remote hosts. Tasks are **disconnect-resilient** — they continue running even if your SSH connection drops.

#### How It Works

```
┌─────────────────────────────────────────────────────────────┐
│                    Your Machine (Local)                      │
│                                                             │
│  ┌──────────────┐         ┌──────────────────────┐          │
│  │  tmux pane    │ ◄─────► │  AI Agent (MCP)      │          │
│  │  SSH session  │         │  mcp_server.py       │          │
│  └──────┬───────┘         └──────────────────────┘          │
└─────────┼───────────────────────────────────────────────────┘
          │ SSH (can disconnect!)
    ┌─────▼───────────────────────────────────────────────┐
    │               Remote Host                           │
    │                                                     │
    │   tmux session "ai_work"    ← survives disconnect   │
    │   ┌──────────┐ ┌──────────┐ ┌──────────┐           │
    │   │ window 0  │ │ window 1  │ │ window 2  │         │
    │   │ "build"   │ │ "test"    │ │ "deploy"  │         │
    │   │ make build│ │ make test │ │ ./deploy  │         │
    │   └──────────┘ └──────────┘ └──────────┘           │
    │                                                     │
    │   /tmp/_tmux_tasks_ai_work/   ← status tracking     │
    │   ├── build.status   (start time, exit code, etc.)  │
    │   ├── test.status                                   │
    │   └── deploy.status                                 │
    └─────────────────────────────────────────────────────┘
```

#### Complete Workflow

**Step 1: Launch parallel tasks**

Ask the AI Agent:
> "Run `make build`, `make test`, and `tail -f /var/log/app.log` in parallel on the remote server"

This calls `tmux_remote_parallel` which:
- Creates a tmux session on the **remote** host (not local)
- Each command runs in its own window with status tracking
- Records start time, and will record exit code + duration on completion

**Step 2: Monitor progress**

Ask the AI Agent:
> "Check how the remote tasks are going"

This calls `tmux_check_remote_tasks` which returns:
```
📊 Remote Tasks Status (session: ai_work):

  ✅ [build] completed (exit code: 0) in 2m34s
      Build successful: 142 targets built
  🔄 [test] running (1m12s elapsed)
      Running test suite: 87/120 passed...
  🔄 [logs] running (3m45s elapsed)
      [2024-01-15 10:23:45] INFO: Request processed in 12ms
```

**Step 3: Stop or clean up**

> "Stop the log tailing task" → `tmux_kill_remote_tasks(window_name='logs')`
> "Clean up all remote tasks" → `tmux_kill_remote_tasks()` (kills entire session)

#### Use Cases

| Scenario | Commands |
|----------|----------|
| **Build & Test** | `['make build', 'make test', 'make lint']` |
| **Log Monitoring** | `['tail -f /var/log/app.log', 'tail -f /var/log/error.log']` |
| **Data Processing** | `['python process_batch_1.py', 'python process_batch_2.py']` |
| **Deployment** | `['docker build -t app .', 'npm run build', 'python manage.py migrate']` |
| **System Diagnostics** | `['vmstat 1', 'iostat -x 1', 'tail -f /var/log/syslog']` |

#### Advanced: Custom Window Names

```
tmux_remote_parallel(
    commands=['make build', 'pytest -v', 'flake8 .'],
    window_names=['build', 'test', 'lint'],
    session_name='ci_pipeline'
)
```

#### Advanced: Reuse Existing Session

Add tasks to an already-running session without killing previous tasks:

```
tmux_remote_parallel(
    commands=['tail -f /var/log/nginx/access.log'],
    window_names=['nginx_logs'],
    session_name='ai_work',
    reuse_session=True
)
```

#### SSH Disconnection Recovery

If your SSH connection drops mid-task:
1. Tasks continue running in the remote tmux session
2. Reconnect SSH manually (or have AI send SSH command via `tmux_send_keys`)
3. Check task status with `tmux_check_remote_tasks` — it reads status files
4. Or attach interactively: `tmux attach -t ai_work`

### 🎯 Smart Target Resolution

Find the right pane automatically through multiple strategies:
1. Explicit `target` parameter
2. `server_hint` natural language matching
3. Pane title matching (instant, no commands sent)
4. Auto-select when only one server exists

## Multi-Server Support

```bash
tmux new-session -s dev    # development environment
tmux new-session -s ops    # operations environment
# Login to different servers in each session
```

Tag servers for natural language targeting:

```
tmux_register_server(target="dev:0.0", name="Web Frontend", tags=["prod", "web"])
tmux_run_command(server_hint="web frontend", command="uptime")
```

## Python API

```python
from tmux_agent import TmuxAgent

agent = TmuxAgent(session_name="work", pane_target="work:0.0")
output = agent.run_command("ls -la /var/log")
print(output)
```

## File Structure

```
tmux-mcp-agent/
├── README.md           # English documentation
├── README_zh.md        # Chinese documentation
├── tmux_agent.py       # Core controller (TmuxAgent class + CLI)
├── mcp_server.py       # MCP Server (AI Agent integration)
├── setup.sh            # Quick setup script
├── requirements.txt    # Python dependencies
└── LICENSE             # MIT License
```

## Notes

1. **Security**: Jump host passwords/keys are entered manually — the AI Agent never touches credentials
2. **Timeout**: Default command timeout is 5 minutes (`max_wait=300`), adjustable per command
3. **Output limit**: `capture-pane` captures 200 lines by default; use `| tail` or `| head` for long output
4. **Interactive programs**: Use `send_keys` instead of `run_command` for `vim`, `top`, etc.
5. **Connection loss**: If SSH disconnects, you need to reconnect manually; use `connection_guard` for detection

## License

[MIT](LICENSE)
