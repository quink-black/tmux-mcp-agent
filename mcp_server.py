#!/usr/bin/env python3
"""
MCP Server for tmux-mcp-agent - Expose TmuxAgent as MCP tools.

This allows AI agents (e.g. Claude via CodeBuddy/Cursor) to directly call tmux
operations as MCP tools, enabling remote server control through an
already-established tmux session.

Usage:
    python mcp_server.py
"""

import asyncio
import json
import logging
import re
import subprocess
import sys
from typing import Any

# tmux is not available on native Windows (non-MSYS2/Cygwin/WSL)
if sys.platform == "win32":
    import os
    # Detect if running under MSYS2 / Git Bash / Cygwin (which provide tmux):
    #   MSYSTEM  - set by MSYS2 / Git Bash (e.g. MSYS, MINGW64, UCRT64)
    #   CYGWIN   - Cygwin environment
    #   TERM     - MSYS2/Cygwin terminals usually set this (e.g. xterm-256color)
    # WSL sets sys.platform to 'linux', so it won't enter this branch
    _is_unix_like = (
        os.environ.get("MSYSTEM")           # MSYS2 / Git Bash
        or os.environ.get("CYGWIN")         # Cygwin
        or os.environ.get("MSYS")           # Legacy MSYS
        or "cygwin" in os.environ.get("TERM", "").lower()
        or "msys" in os.environ.get("TERM", "").lower()
    )
    if not _is_unix_like:
        sys.exit(
            "❌ tmux-mcp-agent does not support native Windows CMD/PowerShell.\n"
            "   Please use one of the following environments that provide tmux:\n"
            "   - MSYS2 (Windows):  pacman -S tmux\n"
            "   - WSL (Windows Subsystem for Linux): https://learn.microsoft.com/en-us/windows/wsl/install\n"
            "   - macOS / Linux: brew install tmux  /  sudo apt install tmux"
        )

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from tmux_agent import TmuxAgent

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("tmux-mcp-server")


class ServerRegistry:
    """In-memory server registry cache.

    tmux windows/panes change frequently, so persisting to file would
    quickly become stale. Keep everything in memory and refresh on discover.
    """

    def __init__(self):
        self.servers: dict[str, dict] = {}
        # User-registered metadata (tags/name/description), stored separately
        # keyed by hostname so it survives pane index changes.
        self.user_meta: dict[str, dict] = {}

    def register(self, target: str, info: dict):
        """Register or update server info for a target (from auto-discovery)."""
        self.servers[target] = {**info, "target": target}

    def set_user_meta(self, hostname: str, meta: dict):
        """Save user-assigned metadata for a host (name/tags/description)."""
        self.user_meta[hostname] = meta

    def get_user_meta(self, hostname: str) -> dict:
        return self.user_meta.get(hostname, {})

    def get(self, target: str) -> dict | None:
        return self.servers.get(target)

    def find_best_match(self, intent: str) -> str | None:
        """Find the best matching target based on user intent."""
        intent_lower = intent.lower()

        # Direct target reference (e.g. "work:0.1")
        for target in self.servers:
            if target in intent:
                return target

        best_target = None
        best_score = 0

        for target, info in self.servers.items():
            score = 0
            hostname = info.get("hostname", "").lower()

            # Match hostname
            if hostname and hostname in intent_lower:
                score += 10

            # Match user metadata
            meta = self.user_meta.get(info.get("hostname", ""), {})
            if meta.get("name", "").lower() in intent_lower and meta.get("name"):
                score += 20
            for tag in meta.get("tags", []):
                if tag.lower() in intent_lower:
                    score += 15
            if meta.get("description", "").lower() in intent_lower and meta.get("description"):
                score += 5

            if score > best_score:
                best_score = score
                best_target = target

        return best_target

    def list_all(self) -> list[dict]:
        """List all discovered servers with user metadata."""
        result = []
        for info in self.servers.values():
            merged = {**info}
            meta = self.user_meta.get(info.get("hostname", ""), {})
            if meta:
                merged.update({k: v for k, v in meta.items() if v})
            result.append(merged)
        return result

    def clear(self):
        self.servers.clear()


# Global registry instance
registry = ServerRegistry()

# Concurrency safety: per-target lock to prevent concurrent commands to the same pane
_target_locks: dict[str, asyncio.Lock] = {}


def _get_target_lock(target: str) -> asyncio.Lock:
    """Get or create an asyncio.Lock for the specified target."""
    if target not in _target_locks:
        _target_locks[target] = asyncio.Lock()
    return _target_locks[target]


# ----------------------------------------------------------------
# MCP Server Definition
# ----------------------------------------------------------------

app = Server("tmux-mcp-agent")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="tmux_list_sessions",
            description=(
                "List all active tmux sessions. "
                "Use this first to discover available sessions. "
                "You do NOT need a special session name — any tmux session works."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="tmux_list_all_panes",
            description=(
                "⚡ FAST: List ALL panes across ALL sessions with rich metadata "
                "(title, command, size, pid, tty, active status). "
                "Returns instantly (zero send_keys, pure tmux API). "
                "\n\n🎯 RECOMMENDED FIRST STEP: Use this instead of tmux_discover_servers "
                "when you need a quick overview of all available panes. "
                "The pane title and running command often reveal which server it connects to. "
                "\n\nOutput format per line: session:window.pane|title|command|WxH|pid|tty|active"
                "\n\n💡 Decision flow for AI:"
                "\n  1. Call tmux_list_all_panes → get instant overview"
                "\n  2. If pane title/command clearly identifies the target → use it directly"
                "\n  3. Only if unclear → call tmux_discover_servers on specific pane(s)"
            ),
            inputSchema={
                "type": "object",
                "properties": {},
            },
        ),
        Tool(
            name="tmux_discover_servers",
            description=(
                "🔍 DEEP SCAN: Discover servers by running hostname/whoami/pwd in tmux panes. "
                "This is SLOW (sends real commands, ~5s per pane). "
                "\n\n⚠️ Prefer tmux_list_all_panes for quick overview. Only use this when:"
                "\n  - Pane titles don't reveal the server identity"
                "\n  - You need hostname/user/cwd info for a specific pane"
                "\n  - Use 'target' param to scan only ONE specific pane (much faster)"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {
                        "type": "string",
                        "description": "Tmux session name to scan. If omitted, scans ALL sessions.",
                    },
                    "target": {
                        "type": "string",
                        "description": "Scan ONLY this specific pane (e.g. 'myserver:1.1'). Much faster than full scan.",
                    },
                },
            },
        ),
        Tool(
            name="tmux_register_server",
            description=(
                "Register a server with metadata for easier discovery. "
                "Allows tagging servers (e.g., 'prod', 'test', 'db') for natural language matching. "
                "Use this to make servers discoverable by name or tags."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Tmux pane target (e.g. 'work:0.0'). Required.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Human-readable name for this server.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags for categorizing (e.g., ['prod', 'web']). Optional.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Additional description of this server.",
                    },
                },
                "required": ["target"],
            },
        ),
        Tool(
            name="tmux_find_server",
            description=(
                "Find a target based on natural language description. "
                "Use this when the user refers to a server by name, tags, or description. "
                "Returns the matching target(s) with confidence scores."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (e.g., 'production web server', 'test database').",
                    },
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="tmux_run_command",
            description=(
                "Execute a shell command in the remote tmux session and return the output. "
                "Uses unique marker + temp file to reliably detect command completion. "
                "Returns structured result with stdout, exit_code, timed_out, and duration. "
                "Concurrency-safe: commands to the same pane are serialized automatically. "
                "\n\n🎯 Target resolution (in priority order):"
                "\n  1. Explicit 'target' param → used directly"
                "\n  2. 'server_hint' → matches against registry + pane titles"
                "\n  3. If only 1 server in registry → auto-selected"
                "\n\n💡 If unsure about target: call tmux_list_all_panes first (instant)"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute on the remote server.",
                    },
                    "target": {
                        "type": "string",
                        "description": (
                            "Tmux pane target (e.g. 'myserver:1.1'). "
                            "Format: 'session:window.pane'. "
                            "Use tmux_list_all_panes to find available targets."
                        ),
                    },
                    "server_hint": {
                        "type": "string",
                        "description": (
                            "Natural language hint to auto-detect target "
                            "(e.g., 'the myserver server', 'database'). "
                            "Matches against pane titles, registry, and hostname."
                        ),
                    },
                    "max_wait": {
                        "type": "number",
                        "description": "Max seconds to wait for command completion. Default: 300 (5 min). Set higher for long builds.",
                        "default": 300,
                    },
                },
                "required": ["command"],
            },
        ),
        Tool(
            name="tmux_capture_pane",
            description=(
                "Capture and return the current visible content of the tmux pane. "
                "Useful for reading output that's already on screen without sending a new command."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Tmux pane target (e.g. 'mywork:0.1'). Required.",
                    },
                    "lines": {
                        "type": "integer",
                        "description": "Number of history lines to capture. Default: 200",
                        "default": 200,
                    },
                    "colors": {
                        "type": "boolean",
                        "description": "Include ANSI color/escape sequences in output. Default: false",
                        "default": False,
                    },
                },
                "required": ["target"],
            },
        ),
        Tool(
            name="tmux_send_keys",
            description=(
                "Send raw keys to the tmux pane. Useful for interactive programs, "
                "sending Ctrl sequences, or answering prompts (e.g. 'y' for confirmation). "
                "Special keys: C-c (Ctrl+C), C-d (Ctrl+D), C-z (Ctrl+Z), Enter, Tab, Escape."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "string",
                        "description": "Keys to send (text or tmux key names like C-c, Enter).",
                    },
                    "target": {
                        "type": "string",
                        "description": "Tmux pane target (e.g. 'mywork:0.1'). Required.",
                    },
                    "press_enter": {
                        "type": "boolean",
                        "description": "Whether to press Enter after sending keys. Default: true",
                        "default": True,
                    },
                },
                "required": ["keys", "target"],
            },
        ),
        Tool(
            name="tmux_send_ctrl_c",
            description="Send Ctrl+C to interrupt the currently running process in the tmux pane.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Tmux pane target (e.g. 'mywork:0.1'). Required.",
                    },
                },
                "required": ["target"],
            },
        ),
        Tool(
            name="tmux_list_panes",
            description=(
                "List panes in a specific tmux session with rich metadata. "
                "For cross-session overview, use tmux_list_all_panes instead."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {
                        "type": "string",
                        "description": "Tmux session name. Required.",
                    },
                },
                "required": ["session"],
            },
        ),
        Tool(
            name="tmux_set_pane_title",
            description=(
                "Set a human-readable title for a tmux pane. "
                "This title appears in tmux_list_all_panes output and enables fast identification. "
                "\n\n💡 Best practice: Set descriptive titles (e.g. 'myserver-ssh', 'db-prod', 'build-server') "
                "after connecting to a server. This makes subsequent target resolution instant."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Tmux pane target (e.g. 'myserver:1.1'). Required.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Human-readable title for this pane (e.g. 'myserver-ssh', 'web-server'). Required.",
                    },
                },
                "required": ["target", "title"],
            },
        ),
        Tool(
            name="tmux_health_check",
            description=(
                "Quick health check on a tmux pane to verify the shell is responsive. "
                "Sends a lightweight echo command and checks if a response comes back within timeout. "
                "Use this to verify SSH connection is still alive before running commands."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Tmux pane target (e.g. 'mywork:0.1'). Required.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "Max seconds to wait for response. Default: 3",
                        "default": 3,
                    },
                },
                "required": ["target"],
            },
        ),
        Tool(
            name="tmux_create_session",
            description=(
                "Create a new tmux session. "
                "Use this to set up a new workspace for connecting to additional servers."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name for the new tmux session.",
                    },
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="tmux_create_window",
            description=(
                "Create a new window in a tmux session. "
                "Useful for managing multiple remote connections within the same session."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {
                        "type": "string",
                        "description": "Tmux session name to create the window in. Required.",
                    },
                    "name": {
                        "type": "string",
                        "description": "Name for the new window.",
                    },
                },
                "required": ["session", "name"],
            },
        ),
        Tool(
            name="tmux_split_pane",
            description=(
                "Split a tmux pane horizontally or vertically. "
                "Useful for running parallel tasks (e.g., compiling while tailing logs)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Tmux pane target to split (e.g. 'mywork:0.1'). Required.",
                    },
                    "direction": {
                        "type": "string",
                        "enum": ["horizontal", "vertical"],
                        "description": "Split direction: 'horizontal' (side by side) or 'vertical' (top/bottom). Default: 'vertical'.",
                    },
                    "size": {
                        "type": "integer",
                        "description": "Size of the new pane as percentage (1-99). Default: 50.",
                    },
                },
                "required": ["target"],
            },
        ),
        Tool(
            name="tmux_kill_session",
            description="Kill (destroy) a tmux session by name.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session": {
                        "type": "string",
                        "description": "Name of the tmux session to kill. Required.",
                    },
                },
                "required": ["session"],
            },
        ),
        Tool(
            name="tmux_kill_window",
            description="Kill (destroy) a tmux window by target.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Tmux window target to kill (e.g. 'mywork:1'). Required.",
                    },
                },
                "required": ["target"],
            },
        ),
        Tool(
            name="tmux_kill_pane",
            description="Kill (destroy) a tmux pane by target.",
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Tmux pane target to kill (e.g. 'mywork:0.1'). Required.",
                    },
                },
                "required": ["target"],
            },
        ),
        # ----------------------------------------------------------------
        # Skill tools: composite advanced operations
        # ----------------------------------------------------------------
        Tool(
            name="tmux_safe_execute",
            description=(
                "🛡️ SAFE EXECUTE: Composite skill that runs a command with full safety checks. "
                "Performs connection_guard (health check + hostname verification + local/remote detection) "
                "BEFORE executing the command. Prevents dangerous misoperations on local machine "
                "when SSH connection is dead."
                "\n\n🔒 Safety flow:"
                "\n  1. Health check → is the shell responsive?"
                "\n  2. Hostname verify → are we on the expected remote host (not local)?"
                "\n  3. If safe → execute command and return result"
                "\n  4. If unsafe → BLOCK execution and return warning + reconnection instructions"
                "\n\n💡 Use this instead of tmux_run_command when:"
                "\n  - Operating on remote servers (SSH connections)"
                "\n  - Running potentially destructive commands (rm, kill, etc.)"
                "\n  - You want automatic disconnection detection"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute safely.",
                    },
                    "target": {
                        "type": "string",
                        "description": "Tmux pane target (e.g. 'myserver:1.1').",
                    },
                    "server_hint": {
                        "type": "string",
                        "description": "Natural language hint to auto-detect target.",
                    },
                    "expected_hostname": {
                        "type": "string",
                        "description": (
                            "Expected remote hostname. If the pane is on a different host, "
                            "the command will be BLOCKED. If omitted, only checks if the pane "
                            "is on a remote host (not local)."
                        ),
                    },
                    "max_wait": {
                        "type": "number",
                        "description": "Max seconds to wait for command completion. Default: 300.",
                        "default": 300,
                    },
                },
                "required": ["command"],
            },
        ),
        Tool(
            name="tmux_connection_guard",
            description=(
                "🔍 CONNECTION GUARD: Check if a pane is connected to the expected remote host. "
                "Detects SSH disconnections, identifies local vs remote, and provides reconnection guidance. "
                "\n\n🛡️ Use cases:"
                "\n  - Before running destructive commands on remote servers"
                "\n  - To verify SSH connection is still alive"
                "\n  - To detect if you've fallen back to the local shell after SSH disconnect"
                "\n\nReturns: connected status, hostname, is_remote flag, warnings, action_required"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Tmux pane target (e.g. 'myserver:1.1'). Required.",
                    },
                    "expected_hostname": {
                        "type": "string",
                        "description": (
                            "Expected remote hostname to verify against. "
                            "If provided, will warn if current host doesn't match."
                        ),
                    },
                },
                "required": ["target"],
            },
        ),
        Tool(
            name="tmux_remote_parallel",
            description=(
                "🚀 REMOTE PARALLEL: Create a tmux session on the REMOTE host and run multiple "
                "commands in parallel (each in its own window). "
                "\n\n🔑 Key benefits:"
                "\n  - Commands survive SSH disconnection (remote tmux keeps running)"
                "\n  - True parallel execution (not sequential)"
                "\n  - Tracks exit code, duration, and start/end time per task"
                "\n  - Can check task status anytime via tmux_check_remote_tasks"
                "\n  - Can kill individual tasks or all via tmux_kill_remote_tasks"
                "\n  - Reconnect and resume after network interruption"
                "\n\n⚠️ Prerequisites:"
                "\n  - The target pane must be SSH'd into a remote host"
                "\n  - Remote host must have tmux installed"
                "\n\n📝 Example workflow:"
                "\n  1. tmux_remote_parallel(commands=['make build', 'make test', 'tail -f app.log'])"
                "\n  2. tmux_check_remote_tasks() → see progress, exit codes, durations"
                "\n  3. tmux_kill_remote_tasks(window_name='task_2') → stop log tailing"
                "\n  4. tmux_kill_remote_tasks() → clean up everything when done"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Tmux pane target connected to the remote host. Required.",
                    },
                    "server_hint": {
                        "type": "string",
                        "description": "Natural language hint to auto-detect target.",
                    },
                    "commands": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "List of shell commands to run in parallel. "
                            "Each command gets its own tmux window on the remote host. "
                            "Commands are wrapped with timing/status tracking automatically."
                        ),
                    },
                    "window_names": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Optional human-readable names for each window (e.g. ['build', 'test', 'logs']). "
                            "If fewer names than commands, remaining use 'task_N'. "
                            "Names are used in tmux_check_remote_tasks output."
                        ),
                    },
                    "session_name": {
                        "type": "string",
                        "description": "Name for the remote tmux session. Default: 'ai_work'.",
                        "default": "ai_work",
                    },
                    "reuse_session": {
                        "type": "boolean",
                        "description": (
                            "If true, append new windows to an existing session instead of "
                            "killing and recreating it. Useful for adding tasks incrementally. Default: false."
                        ),
                        "default": False,
                    },
                },
                "required": ["commands"],
            },
        ),
        Tool(
            name="tmux_check_remote_tasks",
            description=(
                "📊 CHECK REMOTE TASKS: Check the status of parallel tasks running in a "
                "remote tmux session (created by tmux_remote_parallel). "
                "Shows each task's latest output, exit code, duration, and whether it's still running."
                "\n\n📈 Status information per task:"
                "\n  - Running / Completed status"
                "\n  - Exit code (0 = success, non-zero = error)"
                "\n  - Duration in seconds (elapsed for running tasks, total for completed)"
                "\n  - Last N lines of output"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Tmux pane target connected to the remote host. Required.",
                    },
                    "server_hint": {
                        "type": "string",
                        "description": "Natural language hint to auto-detect target.",
                    },
                    "session_name": {
                        "type": "string",
                        "description": "Remote tmux session name to check. Default: 'ai_work'.",
                        "default": "ai_work",
                    },
                    "window_filter": {
                        "type": "string",
                        "description": "Check only this specific window/task name. If omitted, checks all tasks.",
                    },
                    "capture_lines": {
                        "type": "integer",
                        "description": "Number of output tail lines to capture per task. Default: 10.",
                        "default": 10,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="tmux_kill_remote_tasks",
            description=(
                "🗑️ KILL REMOTE TASKS: Stop and clean up remote parallel tasks. "
                "Can kill a specific task (window) or the entire session."
                "\n\n💡 Usage:"
                "\n  - Kill one task: tmux_kill_remote_tasks(window_name='build')"
                "\n  - Kill all tasks: tmux_kill_remote_tasks() — kills entire session + cleans up status files"
                "\n\n⚠️ This is destructive — killed tasks cannot be resumed."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Tmux pane target connected to the remote host. Required.",
                    },
                    "server_hint": {
                        "type": "string",
                        "description": "Natural language hint to auto-detect target.",
                    },
                    "session_name": {
                        "type": "string",
                        "description": "Remote tmux session name. Default: 'ai_work'.",
                        "default": "ai_work",
                    },
                    "window_name": {
                        "type": "string",
                        "description": (
                            "Specific task/window name to kill (e.g. 'build', 'task_0'). "
                            "If omitted, kills the entire session and all tasks."
                        ),
                    },
                },
                "required": [],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        result = await _dispatch_tool(name, arguments)
        return [TextContent(type="text", text=result)]
    except Exception as e:
        logger.exception(f"Error in tool {name}")
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def _dispatch_tool(name: str, args: dict[str, Any]) -> str:
    if name == "tmux_list_sessions":
        ctrl = TmuxAgent()
        return ctrl.list_sessions()

    elif name == "tmux_discover_servers":
        session = args.get("session")
        single_target = args.get("target")
        return await _discover_servers(session, single_target)

    elif name == "tmux_register_server":
        target = args["target"]
        server_name = args.get("name", "")
        tags = args.get("tags", [])
        description = args.get("description", "")
        return await _register_server(target, server_name, tags, description)

    elif name == "tmux_find_server":
        query = args["query"]
        return _find_server_by_query(query)

    elif name == "tmux_run_command":
        command = args["command"]
        max_wait = args.get("max_wait", 300)

        # Resolve target: explicit > server_hint auto-match
        target = _resolve_target(args)
        if not target:
            return (
                "❌ Cannot determine target pane. Please provide one of:\n"
                "  - target: explicit pane target (e.g. 'mywork:0.1')\n"
                "  - server_hint: natural language description\n"
                "Or run tmux_discover_servers first."
            )

        # Concurrency safety: serialize commands to the same target
        lock = _get_target_lock(target)
        async with lock:
            ctrl = _make_controller(target)
            # Run in thread pool (run_command has blocking time.sleep calls)
            output = await asyncio.get_event_loop().run_in_executor(
                None, lambda: ctrl.run_command(command, max_wait=max_wait)
            )
            return output

    elif name == "tmux_capture_pane":
        target = args.get("target")
        if not target:
            return "❌ 'target' is required. Use tmux_discover_servers to find targets."
        ctrl = _make_controller(target)
        lines = args.get("lines", 200)
        colors = args.get("colors", False)
        return ctrl.capture_pane(lines=lines, colors=colors)

    elif name == "tmux_send_keys":
        target = args.get("target")
        if not target:
            return "❌ 'target' is required. Use tmux_discover_servers to find targets."
        ctrl = _make_controller(target)
        keys = args["keys"]
        press_enter = args.get("press_enter", True)
        success = ctrl.send_keys(keys, press_enter=press_enter)
        if success:
            await asyncio.sleep(0.5)
            return f"Keys sent successfully.\n\nCurrent pane content:\n{ctrl.capture_pane(lines=30)}"
        return "Failed to send keys."

    elif name == "tmux_send_ctrl_c":
        target = args.get("target")
        if not target:
            return "❌ 'target' is required. Use tmux_discover_servers to find targets."
        ctrl = _make_controller(target)
        success = ctrl.send_ctrl_c()
        if success:
            await asyncio.sleep(0.5)
            return f"Ctrl+C sent.\n\nCurrent pane content:\n{ctrl.capture_pane(lines=30)}"
        return "Failed to send Ctrl+C."

    elif name == "tmux_list_all_panes":
        raw = TmuxAgent.list_all_panes()
        return _format_pane_list(raw)

    elif name == "tmux_list_panes":
        session = args.get("session")
        if not session:
            return "❌ 'session' is required. Use tmux_list_sessions to find sessions."
        ctrl = TmuxAgent(session_name=session)
        return _format_pane_list(ctrl.list_panes(session))

    elif name == "tmux_set_pane_title":
        target = args["target"]
        title = args["title"]
        return TmuxAgent.set_pane_title(target, title)

    elif name == "tmux_health_check":
        target = args.get("target")
        if not target:
            return "❌ 'target' is required. Use tmux_discover_servers to find targets."
        timeout = args.get("timeout", 3)
        ctrl = _make_controller(target)
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: ctrl.health_check(timeout=timeout)
        )
        if result["alive"]:
            return f"✅ Pane {target} is alive (latency: {result['latency_ms']}ms)"
        return f"❌ Pane {target} is NOT responding: {result['error']}"

    elif name == "tmux_create_session":
        session_name = args["name"]
        ctrl = TmuxAgent()
        return ctrl.create_session(session_name)

    elif name == "tmux_create_window":
        session = args["session"]
        window_name = args["name"]
        ctrl = TmuxAgent(session_name=session)
        return ctrl.create_window(window_name)

    elif name == "tmux_split_pane":
        target = args["target"]
        direction = args.get("direction", "vertical")
        size = args.get("size")
        ctrl = _make_controller(target)
        return ctrl.split_pane(direction=direction, size=size)

    elif name == "tmux_kill_session":
        session = args["session"]
        ctrl = TmuxAgent()
        return ctrl.kill_session(session_name=session)

    elif name == "tmux_kill_window":
        target = args["target"]
        ctrl = _make_controller(target)
        return ctrl.kill_window(window_target=target)

    elif name == "tmux_kill_pane":
        target = args["target"]
        ctrl = _make_controller(target)
        return ctrl.kill_pane(pane_target=target)

    # ----------------------------------------------------------------
    # Skill tools dispatch
    # ----------------------------------------------------------------
    elif name == "tmux_safe_execute":
        return await _safe_execute(args)

    elif name == "tmux_connection_guard":
        target = args.get("target")
        if not target:
            return "❌ 'target' is required."
        expected = args.get("expected_hostname")
        ctrl = _make_controller(target)
        lock = _get_target_lock(target)
        async with lock:
            result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: ctrl.connection_guard(expected_hostname=expected)
            )
        return _format_connection_guard(target, result)

    elif name == "tmux_remote_parallel":
        return await _remote_parallel(args)

    elif name == "tmux_check_remote_tasks":
        return await _check_remote_tasks(args)

    elif name == "tmux_kill_remote_tasks":
        return await _kill_remote_tasks(args)

    else:
        return f"Unknown tool: {name}"


def _resolve_target(args: dict[str, Any]) -> str | None:
    """Resolve target from tool arguments with multi-level matching strategy.

    Priority:
    1. Explicit target parameter
    2. server_hint -> registry match (hostname/name/tags)
    3. server_hint -> pane title real-time match (no pre-discover needed)
    4. Auto-select if only one server in registry
    """
    target = args.get("target")
    if target:
        return target

    server_hint = args.get("server_hint")
    if server_hint:
        # Strategy 2: match from registry cache
        resolved = registry.find_best_match(server_hint)
        if resolved:
            logger.info(f"Auto-resolved target '{resolved}' from registry hint: {server_hint}")
            return resolved

        # Strategy 3: real-time match from pane title/command (lightweight, no commands sent)
        resolved = _match_target_from_pane_titles(server_hint)
        if resolved:
            logger.info(f"Auto-resolved target '{resolved}' from pane title hint: {server_hint}")
            return resolved

    # Strategy 4: auto-select the only server
    if len(registry.servers) == 1:
        only_target = next(iter(registry.servers))
        logger.info(f"Auto-selected the only available target: {only_target}")
        return only_target

    return None


def _match_target_from_pane_titles(hint: str) -> str | None:
    """Real-time match target from tmux pane titles and commands.

    Lightweight operation (pure tmux API, no commands sent to remote),
    useful for intelligent pane location even when registry is empty.
    """
    hint_lower = hint.lower()

    raw = TmuxAgent.list_all_panes()
    if not raw or raw == "No panes found.":
        return None

    best_target = None
    best_score = 0

    for line in raw.strip().split("\n"):
        parsed = TmuxAgent.parse_pane_line(line)
        if not parsed:
            continue

        score = 0
        target = parsed["target"]
        title = parsed.get("title", "").lower()
        command = parsed.get("command", "").lower()

        # Match pane title (user-set titles have highest priority)
        if title and hint_lower in title:
            score += 25
        elif title:
            # Reverse match: title contained in hint
            for word in title.split():
                if word and word in hint_lower:
                    score += 15

        # Match target string itself (e.g. "myserver" in "myserver:1.1")
        session_name = target.split(":")[0].lower() if ":" in target else ""
        if session_name and session_name in hint_lower:
            score += 20

        # Match running command (e.g. "ssh" indicates a remote connection)
        if command and hint_lower in command:
            score += 5

        if score > best_score:
            best_score = score
            best_target = target

    return best_target if best_score > 0 else None


def _format_pane_list(raw: str) -> str:
    """Format raw list_panes / list_all_panes output into a human-readable table."""
    if not raw or raw.startswith("Error") or raw == "No panes found.":
        return raw

    lines = ["📋 Panes Overview:\n"]
    lines.append(f"  {'TARGET':<20} {'TITLE':<20} {'COMMAND':<15} {'SIZE':<12} {'ACTIVE'}")
    lines.append(f"  {'─' * 20} {'─' * 20} {'─' * 15} {'─' * 12} {'─' * 6}")

    for line in raw.strip().split("\n"):
        parsed = TmuxAgent.parse_pane_line(line)
        if not parsed:
            lines.append(f"  {line}")  # Output as-is when unable to parse
            continue

        active_str = "✅" if parsed["active"] else ""
        title = parsed["title"][:18] + ".." if len(parsed["title"]) > 20 else parsed["title"]
        lines.append(
            f"  {parsed['target']:<20} {title:<20} {parsed['command']:<15} "
            f"{parsed['size']:<12} {active_str}"
        )

    lines.append(f"\n💡 Use tmux_set_pane_title to label panes for easy identification.")
    lines.append(f"   Use 'target' value in other tools to operate on a specific pane.")
    return "\n".join(lines)


def _make_controller(target: str) -> TmuxAgent:
    """Create a TmuxAgent from a target string.

    target format: 'session:window.pane', session_name is parsed from it.
    """
    session = target.split(":")[0] if ":" in target else target
    return TmuxAgent(
        session_name=session,
        pane_target=target,
    )


# ----------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------

async def main():
    logger.info("Starting Tmux MCP Server (session-agnostic, use tmux_discover_servers to find targets)")
    async with stdio_server() as (read_stream, write_stream):
        init_options = app.create_initialization_options()
        await app.run(read_stream, write_stream, init_options)


# ----------------------------------------------------------------
# Helper functions for server discovery and registry
# ----------------------------------------------------------------

async def _discover_servers(session_filter: str | None = None, single_target: str | None = None) -> str:
    """Scan panes and discover connected servers.

    Supports two modes:
    - Full scan: scan all session/pane (slow, ~5s per pane)
    - Single target: probe only the specified pane (fast, ~5s total)
    """

    discovered: list[dict] = []

    if single_target:
        # Fast mode: probe only one pane
        targets_to_scan = [single_target]
    else:
        # Full scan mode
        if session_filter:
            sessions = [session_filter]
        else:
            result = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                return f"Error listing sessions: {result.stderr.strip()}"
            sessions = [s.strip() for s in result.stdout.strip().split("\n") if s.strip()]

        # Clear old cache (only in full scan mode)
        registry.clear()

        targets_to_scan = []
        for session in sessions:
            result = subprocess.run(
                [
                    "tmux", "list-panes", "-s", "-t", session,
                    "-F", f"{session}:" + "#{window_index}.#{pane_index}",
                ],
                capture_output=True, text=True,
            )
            if result.returncode == 0:
                targets_to_scan.extend(
                    [p.strip() for p in result.stdout.strip().split("\n") if p.strip()]
                )

    for target in targets_to_scan:
        session = target.split(":")[0] if ":" in target else target
        pane_id = target.split(":")[1] if ":" in target else "0.0"

        pane_ctrl = TmuxAgent(
            session_name=session,
            pane_target=target,
        )

        try:
            combined = pane_ctrl.run_command(
                'echo "__HOST__=$(hostname)|__USER__=$(whoami)|__CWD__=$(pwd)"',
                max_wait=5,
            )
            host_m = re.search(r"__HOST__=([^|\s]+)", combined)
            user_m = re.search(r"__USER__=([^|\s]+)", combined)
            cwd_m = re.search(r"__CWD__=([^|\s]+)", combined)

            hostname = host_m.group(1) if host_m else "unknown"
            user = user_m.group(1) if user_m else "unknown"
            cwd = cwd_m.group(1) if cwd_m else "unknown"

            screen = pane_ctrl.capture_pane(lines=5)

            server_info = {
                "target": target,
                "session": session,
                "pane": pane_id,
                "hostname": hostname,
                "user": user,
                "cwd": cwd,
                "screen_preview": screen[:200] + "..." if len(screen) > 200 else screen,
            }

            registry.register(target, server_info)
            discovered.append(server_info)

        except Exception as e:
            discovered.append({
                "target": target,
                "session": session,
                "pane": pane_id,
                "error": str(e),
                "status": "unavailable",
            })

    if not discovered:
        return "No servers discovered. Make sure you have active tmux sessions with panes."

    lines = ["📡 Discovered Servers:\n"]
    for info in discovered:
        if "error" in info:
            lines.append(f"  ❌ {info['target']}: {info['error']}")
        else:
            meta = registry.get_user_meta(info["hostname"])
            tag_str = f" [{', '.join(meta['tags'])}]" if meta.get("tags") else ""
            name_str = f" ({meta['name']})" if meta.get("name") else ""
            lines.append(f"  ✅ {info['target']}{name_str}{tag_str}")
            lines.append(f"     HOST: {info['hostname']} | USER: {info['user']} | CWD: {info['cwd']}")

    lines.append("\n💡 Tips:")
    lines.append("   - Use tmux_set_pane_title to label panes for instant identification")
    lines.append("   - Use tmux_list_all_panes for a quick overview (no commands sent)")
    lines.append("   - Use server_hint parameter for natural language targeting")

    return "\n".join(lines)


async def _register_server(
    target: str,
    server_name: str,
    tags: list[str],
    description: str,
) -> str:
    """Set user metadata (name/tags/description) for a server target.

    Metadata is stored in memory keyed by hostname, so it survives
    pane index changes as long as the hostname is the same.
    """
    try:
        ctrl_with_target = _make_controller(target)
        hostname = ctrl_with_target.run_command("hostname", max_wait=3).strip().split("\n")[-1]
    except Exception as e:
        return f"Error accessing target {target}: {e}"

    meta = {
        "name": server_name or hostname,
        "tags": tags,
        "description": description,
    }
    registry.set_user_meta(hostname, meta)

    tag_str = f" [{', '.join(tags)}]" if tags else ""
    name_str = f" ({server_name})" if server_name else ""
    desc_str = f"\n{description}" if description else ""
    return f"✅ Registered:{name_str} {target} → {hostname}{tag_str}{desc_str}"


def _find_server_by_query(query: str) -> str:
    """Match servers by natural language query from the registry."""
    query_lower = query.lower()
    matches: list[dict] = []

    if not registry.servers:
        return (
            f"❌ No servers in cache matching '{query}'.\n"
            "Registry is empty — run tmux_discover_servers first to scan active panes."
        )

    for target, info in registry.servers.items():
        score = 0
        reasons: list[str] = []
        hostname = info.get("hostname", "")
        meta = registry.get_user_meta(hostname)

        # Match hostname
        if hostname and query_lower in hostname.lower():
            score += 10
            reasons.append(f"hostname='{hostname}'")

        # Match user-set name
        srv_name = meta.get("name", "").lower()
        if srv_name and query_lower in srv_name:
            score += 20
            reasons.append(f"name='{meta['name']}'")

        # Match tags
        for tag in meta.get("tags", []):
            if query_lower in tag.lower():
                score += 15
                reasons.append(f"tag='{tag}'")

        # Match description
        desc = meta.get("description", "").lower()
        if desc and query_lower in desc:
            score += 5
            reasons.append("description matches")

        # Match target itself
        if query_lower in target.lower():
            score += 8
            reasons.append(f"target='{target}'")

        if score > 0:
            matches.append({"target": target, "info": info, "meta": meta, "score": score, "reasons": reasons})

    matches.sort(key=lambda x: x["score"], reverse=True)

    if not matches:
        return (
            f"❌ No servers found matching '{query}'.\n"
            "Try running tmux_discover_servers to refresh, or use tmux_register_server to add tags."
        )

    lines = [f"🔍 Found {len(matches)} match(es) for '{query}':\n"]
    for i, m in enumerate(matches[:5], 1):
        meta = m["meta"]
        tags = meta.get("tags", [])
        tag_str = f" [{', '.join(tags)}]" if tags else ""
        name_val = meta.get("name", m["info"].get("hostname", "unknown"))
        lines.append(f"  {i}. {m['target']} → {name_val}{tag_str}")
        lines.append(f"     Why: {', '.join(m['reasons'])}")

    best = matches[0]
    lines.append(f"\n⭐ Best match: {best['target']} (score: {best['score']})")
    lines.append(f"   Use target='{best['target']}' in other tools")

    return "\n".join(lines)


# ----------------------------------------------------------------
# Skill implementation functions
# ----------------------------------------------------------------

async def _safe_execute(args: dict[str, Any]) -> str:
    """Safe command execution Skill implementation.

    Flow: resolve target -> connection guard -> execute command
    If connection guard detects issues, the command is blocked.
    """
    command = args["command"]
    max_wait = args.get("max_wait", 300)
    expected_hostname = args.get("expected_hostname")

    # Step 1: resolve target
    target = _resolve_target(args)
    if not target:
        return (
            "❌ Cannot determine target pane. Please provide one of:\n"
            "  - target: explicit pane target (e.g. 'mywork:0.1')\n"
            "  - server_hint: natural language description\n"
            "Or run tmux_list_all_panes first."
        )

    lock = _get_target_lock(target)
    async with lock:
        ctrl = _make_controller(target)

        # Step 2: Connection guard check
        guard = await asyncio.get_event_loop().run_in_executor(
            None, lambda: ctrl.connection_guard(expected_hostname=expected_hostname)
        )

        if not guard["connected"]:
            return (
                f"🚫 COMMAND BLOCKED — pane {target} is not responding!\n\n"
                f"{_format_connection_guard(target, guard)}\n\n"
                f"Blocked command: {command}"
            )

        # If expected_hostname specified and mismatch, and not remote -> block
        if expected_hostname and not guard["is_expected_host"] and not guard["is_remote"]:
            return (
                f"🚫 COMMAND BLOCKED — pane {target} is on LOCAL machine, not remote host '{expected_hostname}'!\n\n"
                f"{_format_connection_guard(target, guard)}\n\n"
                f"Blocked command: {command}\n\n"
                f"⚠️ SSH connection appears to be dead. The command would run on your LOCAL machine!"
            )

        # If there are warnings but not blocking, prepend the warning
        warning_prefix = ""
        if guard["warning"]:
            warning_prefix = f"⚠️ Warning: {guard['warning']}\n\n"

        # Step 3: execute command
        output = await asyncio.get_event_loop().run_in_executor(
            None, lambda: ctrl.run_command(command, max_wait=max_wait)
        )

        return f"{warning_prefix}{output}"


def _format_connection_guard(target: str, result: dict) -> str:
    """Format connection_guard result into readable text."""
    lines = [f"🔍 Connection Guard Report for {target}:\n"]

    if result["connected"]:
        lines.append(f"  ✅ Shell: responsive")
        lines.append(f"  🏠 Hostname: {result['hostname']}")
        lines.append(f"  🖥️ Local hostname: {result['local_hostname']}")
        lines.append(f"  {'🌐 Remote' if result['is_remote'] else '📍 Local'} connection")

        if result.get("is_expected_host") is False:
            lines.append(f"  ❌ NOT on expected host!")
    else:
        lines.append(f"  ❌ Shell: NOT responding")

    if result.get("warning"):
        lines.append(f"\n  ⚠️ {result['warning']}")

    if result.get("action_required"):
        lines.append(f"  🔧 {result['action_required']}")

    return "\n".join(lines)


async def _remote_parallel(args: dict[str, Any]) -> str:
    """Create a tmux session on the remote host and run multiple tasks in parallel."""
    commands = args.get("commands", [])
    session_name = args.get("session_name", "ai_work")
    window_names = args.get("window_names")
    reuse_session = args.get("reuse_session", False)

    if not commands:
        return "❌ 'commands' list is empty. Provide at least one command."

    # Resolve target
    target = _resolve_target(args)
    if not target:
        return (
            "❌ Cannot determine target pane. Please provide 'target' or 'server_hint'.\n"
            "Run tmux_list_all_panes first to find available targets."
        )

    lock = _get_target_lock(target)
    async with lock:
        ctrl = _make_controller(target)

        # Safety check first - ensure remote connection
        guard = await asyncio.get_event_loop().run_in_executor(
            None, lambda: ctrl.connection_guard()
        )

        if not guard["connected"]:
            return (
                f"🚫 Cannot setup remote tmux — pane {target} is not responding!\n"
                f"{_format_connection_guard(target, guard)}"
            )

        if not guard["is_remote"]:
            return (
                f"⚠️ Pane {target} appears to be on the LOCAL machine ({guard['hostname']}).\n"
                f"This tool is designed for REMOTE hosts. Use local tmux commands instead,\n"
                f"or verify that this pane has an active SSH connection."
            )

        # Execute remote tmux setup
        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: ctrl.setup_remote_tmux(
                session_name=session_name,
                commands=commands,
                window_names=window_names,
                reuse_session=reuse_session,
            )
        )

        if not result["success"]:
            return f"❌ Failed to setup remote tmux: {result['instructions']}"

        action = "Appended to existing" if result.get("reused") else "Started"
        lines = [
            f"🚀 Remote parallel execution — {action} session on {guard['hostname']}!",
            f"   Session: {result['session_name']}",
            f"   Tasks: {len(result['windows'])}",
            "",
        ]
        for w in result["windows"]:
            lines.append(f"   📋 [{w['name']}] {w['command']}")

        lines.append(f"\n{result['instructions']}")
        lines.append(f"\n💡 Use tmux_check_remote_tasks to monitor progress.")
        lines.append(f"   Use tmux_kill_remote_tasks to stop tasks when done.")

        return "\n".join(lines)


async def _check_remote_tasks(args: dict[str, Any]) -> str:
    """Check the status of tasks in a remote tmux session."""
    session_name = args.get("session_name", "ai_work")
    window_filter = args.get("window_filter")
    capture_lines = args.get("capture_lines", 10)

    # Resolve target
    target = _resolve_target(args)
    if not target:
        return (
            "❌ Cannot determine target pane. Please provide 'target' or 'server_hint'.\n"
            "Run tmux_list_all_panes first to find available targets."
        )

    lock = _get_target_lock(target)
    async with lock:
        ctrl = _make_controller(target)

        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: ctrl.check_remote_tmux_tasks(
                session_name=session_name,
                window_filter=window_filter,
                capture_lines=capture_lines,
            )
        )

        if not result["session_exists"]:
            return (
                f"❌ Remote tmux session '{session_name}' not found.\n"
                f"It may have been killed or never created. "
                f"Use tmux_remote_parallel to create one."
            )

        lines = [f"📊 Remote Tasks Status (session: {session_name}):\n"]

        all_done = True
        for task in result["tasks"]:
            is_running = task["is_running"]
            exit_code = task.get("exit_code")
            duration = task.get("duration_seconds")

            if is_running:
                all_done = False
                status_icon = "🔄"
                status_text = "running"
                if duration is not None:
                    status_text += f" ({_format_duration(duration)} elapsed)"
            else:
                if exit_code == 0:
                    status_icon = "✅"
                    status_text = "completed"
                elif exit_code is not None:
                    status_icon = "❌"
                    status_text = f"failed (exit code: {exit_code})"
                else:
                    status_icon = "✅"
                    status_text = "completed"
                if duration is not None:
                    status_text += f" in {_format_duration(duration)}"

            lines.append(f"  {status_icon} [{task['window']}] {status_text}")
            if task["output_tail"]:
                # Show tail lines indented
                tail_lines = task["output_tail"].strip().split("\n")[-3:]
                for tl in tail_lines:
                    lines.append(f"      {tl}")

        lines.append("")
        if all_done:
            lines.append("🎉 All tasks completed!")
            lines.append(f"   Use tmux_kill_remote_tasks to clean up.")
        else:
            lines.append("⏳ Some tasks still running. Check again later.")
            lines.append(f"   Use tmux_kill_remote_tasks(window_name='...') to stop individual tasks.")

        return "\n".join(lines)


async def _kill_remote_tasks(args: dict[str, Any]) -> str:
    """Kill remote tmux tasks — specific window or entire session."""
    session_name = args.get("session_name", "ai_work")
    window_name = args.get("window_name")

    # Resolve target
    target = _resolve_target(args)
    if not target:
        return (
            "❌ Cannot determine target pane. Please provide 'target' or 'server_hint'.\n"
            "Run tmux_list_all_panes first to find available targets."
        )

    lock = _get_target_lock(target)
    async with lock:
        ctrl = _make_controller(target)

        result = await asyncio.get_event_loop().run_in_executor(
            None, lambda: ctrl.kill_remote_tmux_tasks(
                session_name=session_name,
                window_name=window_name,
            )
        )

        if result["success"]:
            return f"✅ {result['message']}"
        else:
            return f"❌ {result['message']}"


def _format_duration(seconds: int) -> str:
    """Format duration in seconds to human-readable string."""
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        m, s = divmod(seconds, 60)
        return f"{m}m{s}s"
    else:
        h, remainder = divmod(seconds, 3600)
        m, s = divmod(remainder, 60)
        return f"{h}h{m}m{s}s"


if __name__ == "__main__":
    asyncio.run(main())