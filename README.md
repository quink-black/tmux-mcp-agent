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
| `tmux_check_remote_tasks` | 📊 Monitor parallel task status |
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

Run multiple commands in parallel on remote hosts, surviving SSH disconnections:

```
tmux_remote_parallel    → creates remote tmux session with parallel windows
tmux_check_remote_tasks → monitor progress anytime
```

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
