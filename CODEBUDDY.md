# CODEBUDDY.md

This file provides guidance to CodeBuddy Code when working with code in this repository.

## Project Overview

tmux-mcp-agent lets AI agents control remote servers through tmux sessions. Designed for jump-host environments where direct SSH is not possible. Users manually establish SSH connections in tmux; the AI uses tmux's `send-keys`/`capture-pane` APIs. No deployment needed on remote hosts.

## Commands

```bash
# Setup (creates .venv, installs mcp SDK)
bash setup.sh

# CLI testing
python3 tmux_agent.py list                              # list tmux sessions
python3 tmux_agent.py -t <session>:<window>.<pane> capture   # read pane content
python3 tmux_agent.py -t <session>:<window>.<pane> run "hostname"  # run command

# MCP server (started by IDE via stdio; not meant for direct use)
python3 mcp_server.py
```

No build step, no test suite, no linter config, no CI/CD.

## Architecture

Two-file architecture with clear separation:

### `tmux_agent.py` — Core controller library + CLI

- **`TmuxAgent` class**: Wraps all tmux CLI operations via `subprocess.run()`. Each tool call in `mcp_server.py` creates a new `TmuxAgent` instance.
- **`_run_with_marker()`**: Reliable command output capture. Wraps commands with UUID markers and temp files (`${TMPDIR:-/tmp}/_tmux_out_<id>`, `_tmux_rc_<id>`) instead of fragile prompt-pattern matching. The marker `__DONE_<uid>__ <exit_code>` signals completion.
- **Adaptive polling**: Starts at 0.3s, grows to 1s at 30s elapsed, caps at 2s.
- **`_pending_tasks`**: Module-level dict for timed-out commands. Keyed by task_id (uid). Shared across TmuxAgent instances so `wait_for_command(task_id)` can resume waiting without re-sending the command.
- **`connection_guard()`**: Checks shell responsiveness, detects local vs remote, verifies expected hostname, blocks dangerous commands when SSH is dead.
- **Remote parallel execution**: Creates a tmux session on the remote host with status tracking files (`/tmp/_tmux_tasks_<session>/`) recording start time, exit code, and duration. Tasks survive SSH disconnection.
- **MSYS2/Windows compat**: `_tmux_fmt()`/`_tmux_unfmt()` work around MSYS2 tmux format string bugs. `_SUBPROCESS_STDIN = subprocess.DEVNULL` prevents pipe deadlocks on Windows.
- **CLI entry point (`main()`)**: argparse-based, supports `list`, `capture`, `run`, `send`, `ctrl-c` subcommands.

### `mcp_server.py` — MCP Server (22 tools)

- Uses `mcp.server.Server` + `mcp.server.stdio.stdio_server` from the `mcp` SDK.
- **`ServerRegistry`**: In-memory server registry for natural language target resolution (hostname, tags, descriptions). Keyed by hostname so metadata survives pane index changes.
- **`_resolve_target()`**: Multi-strategy target resolution: explicit target > registry match > pane title match > auto-select single server.
- **Per-target asyncio locks** (`_target_locks`): Concurrency safety — only one command per target at a time.
- Blocking `TmuxAgent` calls dispatched to thread pool via `run_in_executor()`.

Tool categories:
- **Discovery**: `tmux_list_sessions`, `tmux_list_all_panes`, `tmux_discover_servers`
- **Registry**: `tmux_register_server`, `tmux_find_server`
- **Execution**: `tmux_run_command`, `tmux_wait_for_command`, `tmux_capture_pane`, `tmux_send_keys`, `tmux_send_ctrl_c`
- **Session management**: `tmux_create_session`, `tmux_create_window`, `tmux_split_pane`, `tmux_kill_session/window/pane`, `tmux_list_panes`, `tmux_set_pane_title`
- **Safety**: `tmux_safe_execute`, `tmux_connection_guard`, `tmux_health_check`
- **Remote parallel**: `tmux_remote_parallel`, `tmux_check_remote_tasks`, `tmux_kill_remote_tasks`

## Key Design Decisions

- **Marker-based completion** over prompt-pattern matching: UUID markers + temp files are reliable regardless of shell prompt configuration.
- **Module-level `_pending_tasks`**: Because `mcp_server.py` creates new `TmuxAgent` instances per tool call, pending task state must be module-level to allow `wait_for_command` to resume a timed-out command from a different instance.
- **`_SUBPROCESS_STDIN`/`_SUBPROCESS_ENCODING`**: Duplicated in both files rather than imported, because `mcp_server.py` needs them before importing from `tmux_agent.py` (which exits on unsupported platforms).
- **No concurrency within a single target**: Per-target asyncio locks prevent interleaved output. Parallelism is achieved via remote tmux sessions (`tmux_remote_parallel`), not concurrent local commands.

## Constraints

- Python 3.10+ (uses `dict[str, str]`, `str | None` type hints)
- Single runtime dependency: `mcp>=1.0.0`
- Requires tmux installed locally
- Platforms: macOS, Linux, MSYS2/Cygwin (not native Windows CMD/PowerShell)
- Default command timeout: 5 minutes (`max_wait=300`)
- Default capture limit: 200 lines
