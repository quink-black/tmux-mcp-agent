#!/usr/bin/env python3
"""
TmuxAgent - AI Agent remote terminal controller via tmux MCP.

Workflow:
  1. You manually create a tmux session and login through jump host to target server.
  2. AI agent uses this controller to send commands and read output.

All operations run locally on your machine. No deployment on jump host needed.
"""

import json
import os
import subprocess
import sys
import time
import re
import shlex
import uuid
from typing import Optional

# Module-level storage for pending (timed-out) tasks.
# Keyed by task_id (uid), stores marker/file info so wait_for_command can resume.
# This must be module-level because mcp_server.py creates new TmuxAgent instances
# per tool call.
_pending_tasks: dict[str, dict] = {}

# tmux is not available on native Windows; requires MSYS2 / WSL or macOS/Linux
if sys.platform == "win32":
    import os as _os
    _is_unix_like = (
        _os.environ.get("MSYSTEM")           # MSYS2 / Git Bash (MSYS, MINGW64, UCRT64, etc.)
        or _os.environ.get("CYGWIN")         # Cygwin
        or _os.environ.get("MSYS")           # Legacy MSYS
        or "cygwin" in _os.environ.get("TERM", "").lower()
        or "msys" in _os.environ.get("TERM", "").lower()
    )
    if not _is_unix_like:
        sys.exit(
            "❌ tmux-mcp-agent does not support native Windows CMD/PowerShell.\n"
            "   Please use one of the following environments that provide tmux:\n"
            "   - MSYS2 (Windows):  pacman -S tmux\n"
            "   - WSL (Windows Subsystem for Linux): https://learn.microsoft.com/en-us/windows/wsl/install\n"
            "   - macOS / Linux: brew install tmux  /  sudo apt install tmux"
        )


# On Windows, when this process is launched with piped stdin/stdout (e.g. as an
# MCP server), MSYS2/Cygwin child processes (like tmux.exe) may inherit the pipe
# file descriptors and deadlock during their cygwin compatibility layer init.
# To prevent this, all subprocess.run() calls that invoke tmux must use
# stdin=subprocess.DEVNULL so tmux doesn't inherit the parent's stdin pipe.
_SUBPROCESS_STDIN = subprocess.DEVNULL if sys.platform == "win32" else None

# On Windows (msys2), subprocess.run with text=True defaults to the system
# locale encoding (e.g. GBK on Chinese Windows), but tmux outputs UTF-8.
_SUBPROCESS_ENCODING = "utf-8" if sys.platform == "win32" else None


def _tmux_fmt(fmt: str) -> str:
    """Prepare a tmux format string for the current platform.

    MSYS2 tmux has a bug where ``#{var}`` is not expanded when:
    1. The format string starts with ``#{`` (the ``#`` at position 0 is not
       recognized as a format introducer).
    2. ``}`` is immediately followed by a non-space character (e.g.
       ``#{a}.#{b}`` or ``#{a}|#{b}``).

    Work around both issues by:
    - Prepending a space if the format starts with ``#{``
    - Inserting a space after every ``}`` that is followed by a non-space char
    """
    if sys.platform != "win32":
        return fmt
    # Workaround 1: leading #{  →  add space prefix
    if fmt.startswith("#{"):
        fmt = " " + fmt
    # Workaround 2: }X  →  } X  (where X is not a space)
    fmt = re.sub(r'\}(?=\S)', '} ', fmt)
    return fmt


def _tmux_unfmt(output: str) -> str:
    """Remove the extra spaces inserted by :func:`_tmux_fmt`.

    :func:`_tmux_fmt` inserts a space after every ``}`` that precedes a
    non-space character, and prepends a space when the format starts with
    ``#{``.  After tmux expands the variables the result looks like
    ``" 1 .1 |title |bash |213 x66 |1194 |/dev/pty1 |1"`` — i.e. there
    is an extra leading space and extra spaces *before* each separator.
    This function collapses those back to the intended format.
    """
    if sys.platform != "win32":
        return output
    # Process each line independently (multi-line output)
    lines = output.split('\n')
    result_lines = []
    for line in lines:
        # Strip leading space added by _tmux_fmt
        if line.startswith(' '):
            line = line[1:]
        # " ." between digits  →  "."   (e.g. "1 .1" → "1.1")
        line = re.sub(r'(\d) \.(\d)', r'\1.\2', line)
        # " |"  →  "|"   (e.g. "title |bash" → "title|bash")
        line = re.sub(r' \|', '|', line)
        # " x" between digits  →  "x"   (e.g. "213 x66" → "213x66")
        line = re.sub(r'(\d) x(\d)', r'\1x\2', line)
        # " :" between digits  →  ":"   (e.g. "0 :1" → "0:1")
        line = re.sub(r'(\w) :(\d)', r'\1:\2', line)
        result_lines.append(line)
    return '\n'.join(result_lines)


class TmuxAgent:
    """Control a tmux session programmatically for AI agent automation."""

    # Default prompt patterns to detect command completion
    DEFAULT_PROMPT_PATTERNS = [
        r"[\$#>]\s*$",        # common shell prompts: $, #, >
        r"\]\$\s*$",          # [user@host dir]$
        r"\]#\s*$",           # [root@host dir]#
        r"~\$\s*$",           # ~$
        r"❯\s*$",             # starship / oh-my-zsh prompt
    ]

    # Supported shell types and their exit code variables
    SHELL_EXIT_CODE_VAR = {
        "bash": "${PIPESTATUS[0]:-$?}",
        "zsh": "${pipestatus[1]:-$?}",
        "fish": "$status",
    }

    def __init__(
        self,
        session_name: str = "remote_work",
        pane_target: Optional[str] = None,
        prompt_patterns: Optional[list[str]] = None,
        default_wait: float = 2.0,
        max_wait: float = 300.0,
        poll_interval: float = 0.3,
        capture_lines: int = 200,
        shell_type: str = "bash",
    ):
        """
        Args:
            session_name: tmux session name to attach to.
            pane_target: tmux target pane (e.g. "remote_work:0.0"). If None, uses session_name.
            prompt_patterns: regex patterns to detect command completion.
            default_wait: default seconds to wait after sending a command.
            max_wait: maximum seconds to wait for command completion (default 300s = 5min).
            poll_interval: initial seconds between output polls (adaptive: grows over time).
            capture_lines: number of lines to capture from pane.
        """
        self.session = session_name
        self.target = pane_target or session_name
        self.prompt_patterns = [
            re.compile(p) for p in (prompt_patterns or self.DEFAULT_PROMPT_PATTERNS)
        ]
        self.default_wait = default_wait
        self.max_wait = max_wait
        self.poll_interval = poll_interval
        self.capture_lines = capture_lines
        # Shell type affects how exit code variable is expressed
        self.shell_type = shell_type if shell_type in self.SHELL_EXIT_CODE_VAR else "bash"

    # ----------------------------------------------------------------
    # Session management
    # ----------------------------------------------------------------

    def list_sessions(self) -> str:
        """List all tmux sessions."""
        result = subprocess.run(
            ["tmux", "list-sessions"],
            capture_output=True, text=True, stdin=_SUBPROCESS_STDIN,
            encoding=_SUBPROCESS_ENCODING, errors="replace",
        )
        if result.returncode != 0:
            return f"Error: {result.stderr.strip()}"
        return result.stdout.strip()

    def session_exists(self) -> bool:
        """Check if the configured tmux session exists."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", self.session],
            capture_output=True, text=True, stdin=_SUBPROCESS_STDIN,
            encoding=_SUBPROCESS_ENCODING, errors="replace",
        )
        return result.returncode == 0

    def list_panes(self, session: Optional[str] = None) -> str:
        """List all panes in the session with rich info (title, command, tty, pid).

        Uses tmux built-in format variables, zero send_keys, millisecond-level response.
        """
        target_session = session or self.session
        # -s flag: list all panes across all windows in the session
        fmt = _tmux_fmt(
            "#{window_index}.#{pane_index}|#{pane_title}|#{pane_current_command}|"
            "#{pane_width}x#{pane_height}|#{pane_pid}|#{pane_tty}|#{pane_active}"
        )
        result = subprocess.run(
            ["tmux", "list-panes", "-s", "-t", target_session, "-F", fmt],
            capture_output=True, text=True, stdin=_SUBPROCESS_STDIN,
            encoding=_SUBPROCESS_ENCODING, errors="replace",
        )
        if result.returncode != 0:
            return f"Error: {result.stderr.strip()}"
        return _tmux_unfmt(result.stdout.strip())

    @staticmethod
    def list_all_panes() -> str:
        """List ALL panes across ALL sessions with rich info.

        Provides a global view to help AI quickly locate target panes.
        Format: session:window.pane|title|command|size|pid|tty|active
        """
        # Get all sessions first
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", _tmux_fmt("#{session_name}")],
            capture_output=True, text=True, stdin=_SUBPROCESS_STDIN,
            encoding=_SUBPROCESS_ENCODING, errors="replace",
        )
        if result.returncode != 0:
            return f"Error: {result.stderr.strip()}"

        sessions = [s.strip() for s in result.stdout.strip().split("\n") if s.strip()]
        all_lines = []

        for session in sessions:
            fmt = _tmux_fmt(
                f"{session}:" + "#{window_index}.#{pane_index}|#{pane_title}|"
                "#{pane_current_command}|#{pane_width}x#{pane_height}|"
                "#{pane_pid}|#{pane_tty}|#{pane_active}"
            )
            res = subprocess.run(
                ["tmux", "list-panes", "-s", "-t", session, "-F", fmt],
                capture_output=True, text=True, stdin=_SUBPROCESS_STDIN,
                encoding=_SUBPROCESS_ENCODING, errors="replace",
            )
            if res.returncode == 0 and res.stdout.strip():
                all_lines.append(_tmux_unfmt(res.stdout.strip()))

        return "\n".join(all_lines) if all_lines else "No panes found."

    @staticmethod
    def set_pane_title(target: str, title: str) -> str:
        """Set a title for a specific pane for quick identification.

        The title appears in the tmux status bar (if configured) and in list_panes output.

        Args:
            target: pane target (e.g. 'myserver:1.1').
            title: title to set (e.g. 'myserver-ssh', 'db-server').

        Returns:
            Success/failure message.
        """
        # Use select-pane -T to set pane title
        result = subprocess.run(
            ["tmux", "select-pane", "-t", target, "-T", title],
            capture_output=True, text=True, stdin=_SUBPROCESS_STDIN,
            encoding=_SUBPROCESS_ENCODING, errors="replace",
        )
        if result.returncode != 0:
            return f"Error setting pane title: {result.stderr.strip()}"
        return f"Pane '{target}' title set to '{title}'."

    # ----------------------------------------------------------------
    # Command execution
    # ----------------------------------------------------------------

    def send_keys(self, keys: str, press_enter: bool = True) -> bool:
        """
        Send raw keys to the tmux pane.

        Args:
            keys: the text or key sequence to send.
            press_enter: if True, append C-m (Enter key).

        Returns:
            True if the tmux command succeeded.
        """
        cmd = ["tmux", "send-keys", "-t", self.target, keys]
        if press_enter:
            cmd.append("C-m")
        result = subprocess.run(cmd, capture_output=True, text=True, stdin=_SUBPROCESS_STDIN,
                                encoding=_SUBPROCESS_ENCODING, errors="replace")
        return result.returncode == 0

    def send_ctrl_c(self) -> bool:
        """Send Ctrl+C to interrupt the current process."""
        result = subprocess.run(
            ["tmux", "send-keys", "-t", self.target, "C-c"],
            capture_output=True, text=True, stdin=_SUBPROCESS_STDIN,
            encoding=_SUBPROCESS_ENCODING, errors="replace",
        )
        return result.returncode == 0

    def send_ctrl_d(self) -> bool:
        """Send Ctrl+D (EOF)."""
        result = subprocess.run(
            ["tmux", "send-keys", "-t", self.target, "C-d"],
            capture_output=True, text=True, stdin=_SUBPROCESS_STDIN,
            encoding=_SUBPROCESS_ENCODING, errors="replace",
        )
        return result.returncode == 0

    def capture_pane(self, lines: Optional[int] = None, colors: bool = False) -> str:
        """
        Capture the current content of the tmux pane.

        Args:
            lines: number of history lines to capture. Uses self.capture_lines if None.
            colors: if True, include ANSI color/escape sequences in output (-e flag).

        Returns:
            The captured text content.
        """
        n = lines or self.capture_lines
        cmd = ["tmux", "capture-pane", "-t", self.target, "-p", "-S", f"-{n}"]
        if colors:
            cmd.insert(3, "-e")  # insert -e flag before -t
        result = subprocess.run(cmd, capture_output=True, text=True, stdin=_SUBPROCESS_STDIN,
                                encoding=_SUBPROCESS_ENCODING, errors="replace")
        if result.returncode != 0:
            return f"Error: {result.stderr.strip()}"
        return result.stdout

    def run_command(
        self,
        command: str,
        wait: Optional[float] = None,
        smart_wait: bool = True,
        max_wait: Optional[float] = None,
    ) -> str:
        """
        Send a command and wait for output.

        Uses a unique end-marker to reliably detect when the command finishes,
        instead of relying on fragile prompt-pattern matching.

        Args:
            command: shell command to execute.
            wait: fixed wait time in seconds. Ignored if smart_wait is True.
            smart_wait: if True, use marker-based detection for completion.
            max_wait: override max wait time for this command.

        Returns:
            Captured pane output after command execution.
            If timed out, the output ends with a [TIMEOUT] marker.
        """
        if smart_wait:
            return self._run_with_marker(command, max_wait or self.max_wait)
        else:
            self.send_keys(command, press_enter=True)
            time.sleep(wait or self.default_wait)
            return self.capture_pane()

    def health_check(self, timeout: float = 3.0) -> dict:
        """Quick health check to verify the shell in a pane is responsive.

        Sends a lightweight echo command and checks for a response within timeout.

        Returns:
            {"alive": bool, "latency_ms": float, "error": str|None}
        """
        marker = f"__HC_{uuid.uuid4().hex[:8]}__"
        self.send_keys(f"echo {marker}", press_enter=True)

        start = time.time()
        time.sleep(0.15)

        while (time.time() - start) < timeout:
            output = self.capture_pane(lines=30)
            if marker in output:
                latency = (time.time() - start) * 1000
                return {"alive": True, "latency_ms": round(latency, 1), "error": None}
            time.sleep(0.2)

        return {"alive": False, "latency_ms": -1, "error": f"No response within {timeout}s — SSH connection may be dead"}

    def _run_with_marker(self, command: str, timeout: float) -> str:
        """Reliable command output capture using temp files + unique markers.

        Improvements over plain capture-pane:
        1. Clear screen before execution to eliminate stale data
        2. Redirect output to a remote temp file (no 200-line buffer limit)
        3. Use UUID marker to detect command completion
        4. Read full output via cat on the temp file
        5. Clean up temp files

        Flow:
          clear
        CMD 2>&1 | tee ${TMPDIR:-/tmp}/_tmux_out_<id>; echo __DONE_<id>__ $? > ${TMPDIR:-/tmp}/_tmux_rc_<id>
          (poll capture-pane waiting for marker)
          cat ${TMPDIR:-/tmp}/_tmux_rc_<id>   -> get exit code
          cat ${TMPDIR:-/tmp}/_tmux_out_<id>  -> get full output
          rm -f ${TMPDIR:-/tmp}/_tmux_out_<id> ${TMPDIR:-/tmp}/_tmux_rc_<id>
        """
        uid = uuid.uuid4().hex[:12]
        marker = f"__DONE_{uid}__"
        out_file = f"${{TMPDIR:-/tmp}}/_tmux_out_{uid}"
        rc_file = f"${{TMPDIR:-/tmp}}/_tmux_rc_{uid}"

        # Step 1: clear screen to eliminate stale output
        self.send_keys("clear", press_enter=True)
        time.sleep(0.15)

        # Step 2: send command, output written to both terminal and temp file
        # Use tee instead of pure redirect so capture-pane can see live progress
        # Exit code written to separate file (since $? in pipe is tee's exit code)
        # Choose correct exit code variable based on shell type
        exit_var = self.SHELL_EXIT_CODE_VAR.get(self.shell_type, "${PIPESTATUS[0]:-$?}")
        wrapped = (
            f'{{ {command} ; }} 2>&1 | tee {out_file}; '
            f'echo "{marker} {exit_var}" | tee {rc_file}'
        )
        self.send_keys(wrapped, press_enter=True)

        # Step 3: adaptive polling for marker on screen
        # Note: marker appears in send_keys command echo line,
        # so we can't simply use `marker in screen`.
        # Must match "marker + exit_code" format on a standalone line,
        # e.g. "__DONE_xxx__ 0" — the echo output, not the command echo.
        marker_pattern = re.compile(rf"^{re.escape(marker)}\s+\d+", re.MULTILINE)

        start = time.time()
        interval = self.poll_interval
        time.sleep(0.2)

        marker_found = False
        while (time.time() - start) < timeout:
            screen = self.capture_pane()
            if marker_pattern.search(screen):
                marker_found = True
                break

            # Adaptive polling interval
            elapsed = time.time() - start
            if elapsed < 5:
                interval = self.poll_interval
            elif elapsed < 30:
                interval = min(1.0, self.poll_interval + elapsed * 0.02)
            else:
                interval = min(2.0, 1.0 + (elapsed - 30) * 0.01)
            time.sleep(interval)

        duration = round(time.time() - start, 2)

        if not marker_found:
            # Timeout - save pending task info for wait_for_command to resume
            _pending_tasks[uid] = {
                "marker": marker,
                "marker_pattern": marker_pattern,
                "out_file": out_file,
                "rc_file": rc_file,
                "target": self.target,
                "start_time": start,
            }
            output = self.capture_pane()
            result = {
                "stdout": output,
                "exit_code": -1,
                "timed_out": True,
                "task_id": uid,
                "duration_seconds": duration,
            }
            return self._format_result(result)

        # Step 4: read full output from temp file (no screen buffer limit)
        time.sleep(0.1)  # ensure file write is complete

        # Read exit code
        exit_code = self._read_remote_file(rc_file, marker)

        # Read command output
        stdout = self._read_remote_file_content(out_file)

        # Step 5: clean up temp files
        self.send_keys(f"rm -f {out_file} {rc_file}", press_enter=True)
        time.sleep(0.1)

        result = {
            "stdout": stdout,
            "exit_code": exit_code,
            "timed_out": False,
            "duration_seconds": duration,
        }
        return self._format_result(result)

    def _read_remote_file(self, rc_file: str, marker: str) -> int:
        """Read exit code from rc file.

        rc file format: __DONE_xxx__ <exit_code>
        Falls back to parsing from screen content on failure.
        """
        # Try to find the marker line from current screen content
        screen = self.capture_pane()
        m = re.search(rf"{re.escape(marker)}\s+(\d+)", screen)
        if m:
            return int(m.group(1))
        return -1

    def _read_remote_file_content(self, filepath: str) -> str:
        """Read remote temp file content via send-keys + capture-pane.

        Uses cat command + a new marker for reliable file reading.
        """
        read_marker = f"__EOF_{uuid.uuid4().hex[:8]}__"

        # Clear first for clean cat output capture
        self.send_keys("clear", press_enter=True)
        time.sleep(0.15)

        self.send_keys(f"cat {filepath}; echo {read_marker}", press_enter=True)

        # Wait for read_marker to appear
        start = time.time()
        while (time.time() - start) < 10:
            screen = self.capture_pane()
            if read_marker in screen:
                # Extract content between cat command and read_marker
                lines = screen.splitlines()
                marker_idx = None
                cmd_idx = None
                for i in range(len(lines) - 1, -1, -1):
                    if read_marker in lines[i] and marker_idx is None:
                        marker_idx = i
                    if marker_idx is not None and (f"cat {filepath}" in lines[i]):
                        cmd_idx = i
                        break

                if cmd_idx is not None and marker_idx is not None:
                    output_lines = lines[cmd_idx + 1 : marker_idx]
                    return "\n".join(output_lines)
                elif marker_idx is not None:
                    # fallback: content before marker
                    start_idx = max(0, marker_idx - 100)
                    return "\n".join(lines[start_idx:marker_idx])
                return ""
            time.sleep(0.3)

        # Read timeout (command completed per marker detection, but output may be incomplete)
        return "[Warning: Could not read output file, output may be truncated]\n" + self.capture_pane()

    def _format_result(self, result: dict) -> str:
        """Format structured result into AI Agent-friendly text.

        Includes both JSON metadata and human-readable output.
        """
        parts = []

        # Command output
        stdout = result.get("stdout", "").strip()
        if stdout:
            parts.append(stdout)

        # Status line
        status_parts = []
        exit_code = result.get("exit_code", -1)
        duration = result.get("duration_seconds", 0)
        timed_out = result.get("timed_out", False)
        task_id = result.get("task_id")

        if timed_out:
            status_parts.append(f"⏱️ [TIMEOUT after {duration:.0f}s] Command may still be running.")
            if task_id:
                status_parts.append(
                    f"Use tmux_wait_for_command(task_id='{task_id}') to continue waiting for completion."
                )
                status_parts.append("Use tmux_capture_pane to check live output, or tmux_send_ctrl_c to interrupt.")
            else:
                status_parts.append("Use tmux_capture_pane to check progress, or tmux_send_ctrl_c to interrupt.")
        elif exit_code != 0 and exit_code != -1:
            status_parts.append(f"⚠️ [Exit code: {exit_code}]")

        if status_parts:
            parts.append("\n" + "\n".join(status_parts))

        # JSON metadata (for programmatic parsing)
        meta_dict = {
            "exit_code": exit_code,
            "timed_out": timed_out,
            "duration_seconds": duration,
        }
        if task_id:
            meta_dict["task_id"] = task_id
        meta = json.dumps(meta_dict, ensure_ascii=False)
        parts.append(f"\n📊 [meta: {meta}]")

        return "\n".join(parts)

    # ----------------------------------------------------------------
    # Wait for pending command completion
    # ----------------------------------------------------------------

    def wait_for_command(self, task_id: str, max_wait: Optional[float] = None) -> str:
        """Wait for a previously timed-out command to complete.

        When tmux_run_command times out, it saves the task's marker info and
        returns a task_id. This method resumes waiting for that same marker
        without re-sending the command.

        Args:
            task_id: the task_id returned by a timed-out tmux_run_command.
            max_wait: max seconds to wait. Defaults to self.max_wait.

        Returns:
            Formatted result string (same format as run_command).
        """
        timeout = max_wait or self.max_wait

        task = _pending_tasks.get(task_id)
        if not task:
            return (
                f"❌ Unknown task_id '{task_id}'. "
                f"It may have already completed, been cleaned up, or the server was restarted.\n"
                f"Use tmux_capture_pane to check the current pane content."
            )

        marker = task["marker"]
        marker_pattern = task["marker_pattern"]
        out_file = task["out_file"]
        rc_file = task["rc_file"]
        original_start = task["start_time"]

        # Poll for marker (same adaptive logic as _run_with_marker)
        start = time.time()
        interval = self.poll_interval
        time.sleep(0.2)

        marker_found = False
        while (time.time() - start) < timeout:
            screen = self.capture_pane()
            if marker_pattern.search(screen):
                marker_found = True
                break

            elapsed = time.time() - start
            if elapsed < 5:
                interval = self.poll_interval
            elif elapsed < 30:
                interval = min(1.0, self.poll_interval + elapsed * 0.02)
            else:
                interval = min(2.0, 1.0 + (elapsed - 30) * 0.01)
            time.sleep(interval)

        # Total duration since the original command was sent
        total_duration = round(time.time() - original_start, 2)

        if not marker_found:
            # Still not done — keep task pending for another wait_for_command call
            output = self.capture_pane()
            result = {
                "stdout": output,
                "exit_code": -1,
                "timed_out": True,
                "task_id": task_id,
                "duration_seconds": total_duration,
            }
            return self._format_result(result)

        # Command completed — collect results and clean up
        del _pending_tasks[task_id]

        time.sleep(0.1)  # ensure file write is complete

        exit_code = self._read_remote_file(rc_file, marker)
        stdout = self._read_remote_file_content(out_file)

        self.send_keys(f"rm -f {out_file} {rc_file}", press_enter=True)
        time.sleep(0.1)

        result = {
            "stdout": stdout,
            "exit_code": exit_code,
            "timed_out": False,
            "duration_seconds": total_duration,
        }
        return self._format_result(result)

    @staticmethod
    def get_pending_task(task_id: str) -> Optional[dict]:
        """Check if a pending task exists (for external callers)."""
        return _pending_tasks.get(task_id)

    # ----------------------------------------------------------------
    # Session / Window / Pane management
    # ----------------------------------------------------------------

    def create_session(self, name: str) -> str:
        """Create a new tmux session.

        Args:
            name: name for the new session.

        Returns:
            Success/failure message.
        """
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", name],
            capture_output=True, text=True, stdin=_SUBPROCESS_STDIN,
            encoding=_SUBPROCESS_ENCODING, errors="replace",
        )
        if result.returncode != 0:
            return f"Error creating session: {result.stderr.strip()}"
        return f"Session '{name}' created successfully."

    def create_window(self, name: str) -> str:
        """Create a new window in the current session.

        Args:
            name: name for the new window.

        Returns:
            Success/failure message.
        """
        result = subprocess.run(
            ["tmux", "new-window", "-t", self.session, "-n", name],
            capture_output=True, text=True, stdin=_SUBPROCESS_STDIN,
            encoding=_SUBPROCESS_ENCODING, errors="replace",
        )
        if result.returncode != 0:
            return f"Error creating window: {result.stderr.strip()}"
        return f"Window '{name}' created in session '{self.session}'."

    def split_pane(self, direction: str = "vertical", size: Optional[int] = None) -> str:
        """Split the current pane.

        Args:
            direction: 'horizontal' (side by side, -h) or 'vertical' (top/bottom, -v).
            size: new pane size percentage (1-99), default 50%.

        Returns:
            New pane info or error message.
        """
        cmd = ["tmux", "split-window"]
        if direction == "horizontal":
            cmd.append("-h")
        else:
            cmd.append("-v")
        cmd.extend(["-t", self.target])
        if size is not None and 0 < size < 100:
            cmd.extend(["-p", str(size)])

        result = subprocess.run(cmd, capture_output=True, text=True, stdin=_SUBPROCESS_STDIN,
                                encoding=_SUBPROCESS_ENCODING, errors="replace")
        if result.returncode != 0:
            return f"Error splitting pane: {result.stderr.strip()}"

        # Get newly created pane info
        fmt = _tmux_fmt(
            "#{window_index}.#{pane_index}: #{pane_width}x#{pane_height} #{pane_current_command}"
        )
        list_result = subprocess.run(
            ["tmux", "list-panes", "-t", self.session, "-F", fmt],
            capture_output=True, text=True, stdin=_SUBPROCESS_STDIN,
            encoding=_SUBPROCESS_ENCODING, errors="replace",
        )
        panes = _tmux_unfmt(list_result.stdout.strip()) if list_result.returncode == 0 else ""
        return f"Pane split successfully ({direction}).\nCurrent panes:\n{panes}"

    def kill_session(self, session_name: Optional[str] = None) -> str:
        """Kill (destroy) a tmux session.

        Args:
            session_name: session name to kill. Defaults to current session.

        Returns:
            Success/failure message.
        """
        target = session_name or self.session
        result = subprocess.run(
            ["tmux", "kill-session", "-t", target],
            capture_output=True, text=True, stdin=_SUBPROCESS_STDIN,
            encoding=_SUBPROCESS_ENCODING, errors="replace",
        )
        if result.returncode != 0:
            return f"Error killing session: {result.stderr.strip()}"
        return f"Session '{target}' has been killed."

    def kill_window(self, window_target: Optional[str] = None) -> str:
        """Kill (destroy) a tmux window.

        Args:
            window_target: window to kill (e.g. 'session:1'). Defaults to current window.

        Returns:
            Success/failure message.
        """
        target = window_target or self.target
        result = subprocess.run(
            ["tmux", "kill-window", "-t", target],
            capture_output=True, text=True, stdin=_SUBPROCESS_STDIN,
            encoding=_SUBPROCESS_ENCODING, errors="replace",
        )
        if result.returncode != 0:
            return f"Error killing window: {result.stderr.strip()}"
        return f"Window '{target}' has been killed."

    def kill_pane(self, pane_target: Optional[str] = None) -> str:
        """Kill (destroy) a tmux pane.

        Args:
            pane_target: pane to kill (e.g. 'session:0.1'). Defaults to current pane.

        Returns:
            Success/failure message.
        """
        target = pane_target or self.target
        result = subprocess.run(
            ["tmux", "kill-pane", "-t", target],
            capture_output=True, text=True, stdin=_SUBPROCESS_STDIN,
            encoding=_SUBPROCESS_ENCODING, errors="replace",
        )
        if result.returncode != 0:
            return f"Error killing pane: {result.stderr.strip()}"
        return f"Pane '{target}' has been killed."

    @staticmethod
    def parse_pane_line(line: str) -> Optional[dict]:
        """Parse a single line from list_all_panes into a structured dict.

        Format: session:window.pane|title|command|WxH|pid|tty|active
        """
        parts = line.split("|")
        if len(parts) < 7:
            return None
        return {
            "target": parts[0],
            "title": parts[1],
            "command": parts[2],
            "size": parts[3],
            "pid": parts[4],
            "tty": parts[5],
            "active": parts[6] == "1",
        }

    def _wait_for_prompt(self, timeout: float) -> str:
        """Legacy prompt pattern detection (kept as fallback).

        Used for scenarios where inserting a marker is not suitable (e.g. interactive programs).
        """
        start = time.time()
        time.sleep(0.3)

        while (time.time() - start) < timeout:
            output = self.capture_pane()
            lines = output.rstrip().splitlines()
            if lines:
                last_line = lines[-1]
                for pattern in self.prompt_patterns:
                    if pattern.search(last_line):
                        return output
            time.sleep(self.poll_interval)

        return self.capture_pane()

    # ----------------------------------------------------------------
    # Convenience methods
    # ----------------------------------------------------------------

    def get_last_command_output(self, command_text: Optional[str] = None) -> str:
        """
        Extract just the output of the last command from the captured pane.

        Heuristic: find the last occurrence of the command text, then extract
        lines between it and the next prompt.
        """
        full = self.capture_pane()
        lines = full.splitlines()

        if not command_text:
            # Return last 50 lines as fallback
            return "\n".join(lines[-50:])

        # Find the last line containing the command
        cmd_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if command_text in lines[i]:
                cmd_idx = i
                break

        if cmd_idx is None:
            return "\n".join(lines[-50:])

        # Extract from command line+1 to last prompt
        output_lines = []
        for line in lines[cmd_idx + 1:]:
            is_prompt = False
            for pattern in self.prompt_patterns:
                if pattern.search(line):
                    is_prompt = True
                    break
            if is_prompt:
                break
            output_lines.append(line)

        return "\n".join(output_lines)

    def run_and_extract(self, command: str, **kwargs) -> str:
        """Run a command and return only its output (excluding prompts)."""
        self.run_command(command, **kwargs)
        return self.get_last_command_output(command)

    # ----------------------------------------------------------------
    # Connection guard - disconnection detection and safety
    # ----------------------------------------------------------------

    def connection_guard(self, expected_hostname: Optional[str] = None, timeout: float = 3.0) -> dict:
        """Detect pane connection status, identify disconnections and host identity.

        Core benefits:
        1. Prevent accidental local execution after SSH disconnection (e.g. rm -rf)
        2. Prompt user to reconnect when disconnection is detected
        3. Verify current pane is connected to the expected host

        Returns:
            {
                "connected": bool,          # whether shell is responsive
                "hostname": str,             # current hostname
                "is_remote": bool,           # whether on a remote host (not local)
                "is_expected_host": bool,    # whether on the expected host
                "local_hostname": str,       # local hostname (for comparison)
                "warning": str|None,         # warning message
                "action_required": str|None, # action the user should take
            }
        """
        result = {
            "connected": False,
            "hostname": "unknown",
            "is_remote": False,
            "is_expected_host": True,
            "local_hostname": "",
            "warning": None,
            "action_required": None,
        }

        # Get local hostname for comparison
        try:
            local_result = subprocess.run(
                ["hostname"], capture_output=True, text=True, timeout=3,
                stdin=_SUBPROCESS_STDIN,
                encoding=_SUBPROCESS_ENCODING, errors="replace",
            )
            result["local_hostname"] = local_result.stdout.strip()
        except Exception:
            result["local_hostname"] = "unknown"

        # Step 1: basic health check
        health = self.health_check(timeout=timeout)
        if not health["alive"]:
            result["warning"] = (
                "⚠️ Shell not responding — SSH connection may be dead. "
                "Do NOT execute any commands in this pane!"
            )
            result["action_required"] = (
                "Please manually reconnect SSH, or use tmux_send_keys to send SSH commands."
            )
            return result

        result["connected"] = True

        # Step 2: get remote hostname and determine if it's a remote host
        marker = f"__CG_{uuid.uuid4().hex[:8]}__"
        self.send_keys(f"echo {marker}$(hostname){marker}", press_enter=True)
        time.sleep(0.3)

        start = time.time()
        pattern = re.compile(rf"{re.escape(marker)}(.+?){re.escape(marker)}")
        while (time.time() - start) < timeout:
            screen = self.capture_pane(lines=20)
            # Scan line by line, skip command echo lines (lines containing 'echo')
            for line in screen.splitlines():
                if "echo" in line:
                    continue  # Skip command echo
                m = pattern.search(line)
                if m:
                    remote_hostname = m.group(1).strip()
                    result["hostname"] = remote_hostname
                    result["is_remote"] = (remote_hostname != result["local_hostname"])
                    break
            if result["hostname"] != "unknown":
                break
            time.sleep(0.2)

        # Step 3: verify expected host
        if expected_hostname:
            result["is_expected_host"] = (
                result["hostname"].lower() == expected_hostname.lower()
                or expected_hostname.lower() in result["hostname"].lower()
            )
            if not result["is_expected_host"]:
                if result["is_remote"]:
                    result["warning"] = (
                        f"⚠️ Currently connected to '{result['hostname']}', "
                        f"not the expected '{expected_hostname}'."
                    )
                else:
                    result["warning"] = (
                        f"🚨 DANGER! Current pane is on local machine '{result['hostname']}', "
                        f"not remote host '{expected_hostname}'. SSH may be disconnected!"
                    )
                    result["action_required"] = (
                        "Please manually reconnect SSH. NEVER execute remote commands in this pane!"
                    )

        # Even without expected_hostname, warn if not a remote host
        elif not result["is_remote"] and result["hostname"] != "unknown":
            result["warning"] = (
                f"ℹ️ Current pane is on local machine '{result['hostname']}', "
                "not a remote SSH connection."
            )

        return result

    def detect_remote_tmux(self, timeout: float = 5.0) -> dict:
        """Detect if there are existing tmux sessions on the remote host.

        Returns:
            {
                "has_tmux": bool,       # whether tmux is installed on remote
                "sessions": list[str],  # existing tmux session list on remote
            }
        """
        output = self.run_command("tmux ls 2>/dev/null || echo __NO_TMUX__", max_wait=timeout)
        if "__NO_TMUX__" in output or "no server running" in output.lower() or "error" in output.lower():
            # Check if tmux is installed
            which_output = self.run_command("which tmux 2>/dev/null || echo __NOT_INSTALLED__", max_wait=3)
            has_tmux = "__NOT_INSTALLED__" not in which_output
            return {"has_tmux": has_tmux, "sessions": []}

        sessions = []
        for line in output.strip().split("\n"):
            # tmux ls output format: session_name: N windows ...
            m = re.match(r"^([\w.-]+):", line.strip())
            if m:
                sessions.append(m.group(1))
        return {"has_tmux": True, "sessions": sessions}

    def setup_remote_tmux(
        self,
        session_name: str = "ai_work",
        commands: Optional[list[str]] = None,
        window_names: Optional[list[str]] = None,
        reuse_session: bool = False,
        timeout: float = 10.0,
    ) -> dict:
        """Run multiple tasks in parallel inside tmux on the remote host.

        Creates a tmux session on the remote host with each command in its own window.
        Tasks survive SSH disconnection; reconnect to resume viewing.

        Each command is wrapped with a timing/status tracker that records:
        - Start time
        - End time and duration
        - Exit code
        - A completion marker file for reliable status checking

        Args:
            session_name: remote tmux session name.
            commands: list of commands to run in parallel, each in a separate window.
            window_names: optional list of human-readable names for each window.
                          If shorter than commands, remaining windows use 'task_N'.
            reuse_session: if True, append new windows to an existing session instead
                          of killing and recreating it.
            timeout: timeout per operation step.

        Returns:
            {
                "success": bool,
                "session_name": str,
                "windows": list[{"index": int, "name": str, "command": str}],
                "reused": bool,
                "instructions": str,
            }
        """
        result = {
            "success": False,
            "session_name": session_name,
            "windows": [],
            "reused": False,
            "instructions": "",
        }

        if not commands:
            result["instructions"] = "No commands provided."
            return result

        # Normalize window names
        names = list(window_names or [])
        for i in range(len(names), len(commands)):
            names.append(f"task_{i}")

        # Check remote tmux status first
        tmux_info = self.detect_remote_tmux(timeout=timeout)
        if not tmux_info["has_tmux"]:
            result["instructions"] = (
                "tmux not installed on remote host. Install it first:\n"
                "  Ubuntu/Debian: sudo apt install -y tmux\n"
                "  CentOS/RHEL:   sudo yum install -y tmux\n"
                "  macOS:         brew install tmux"
            )
            return result

        session_exists = session_name in tmux_info["sessions"]

        if session_exists and reuse_session:
            # Reuse existing session: just add new windows
            result["reused"] = True
        elif session_exists:
            # Kill and recreate
            self.run_command(f"tmux kill-session -t {shlex.quote(session_name)} 2>/dev/null", max_wait=3)
            time.sleep(0.3)
            session_exists = False

        # Create status directory on remote host
        status_dir = f"/tmp/_tmux_tasks_{session_name}"
        self.run_command(f"mkdir -p {shlex.quote(status_dir)}", max_wait=3)

        if not session_exists:
            # Create new session (detached, won't affect current shell)
            first_window = shlex.quote(names[0])
            create_output = self.run_command(
                f"tmux new-session -d -s {shlex.quote(session_name)} -n {first_window}",
                max_wait=timeout
            )
            if "error" in create_output.lower() and "duplicate" not in create_output.lower():
                result["instructions"] = f"Failed to create remote tmux session: {create_output}"
                return result

        result["success"] = True

        # Execute commands in separate windows
        for i, cmd in enumerate(commands):
            wname = names[i]
            safe_wname = shlex.quote(wname)
            safe_session = shlex.quote(session_name)

            # Wrap command with timing/status tracking
            # Records: start time, exit code, end time, duration
            status_file = f"{status_dir}/{wname}.status"
            wrapped_cmd = (
                f'echo "STARTED $(date +%s)" > {status_file}; '
                f'_start_ts=$(date +%s); '
                f'{cmd}; '
                f'_exit_code=$?; '
                f'_end_ts=$(date +%s); '
                f'_duration=$((_end_ts - _start_ts)); '
                f'echo "FINISHED $_exit_code $_duration $(date +%s)" >> {status_file}'
            )

            need_create_window = True
            if not session_exists and i == 0:
                # First command uses the window created with new-session
                need_create_window = False
            elif session_exists and reuse_session and i == 0:
                # When reusing, always create new windows for all commands
                need_create_window = True

            if need_create_window:
                self.run_command(
                    f"tmux new-window -t {safe_session} -n {safe_wname}",
                    max_wait=3
                )

            self.run_command(
                f"tmux send-keys -t {safe_session}:{safe_wname} {shlex.quote(wrapped_cmd)} C-m",
                max_wait=3
            )
            result["windows"].append({"index": i, "name": wname, "command": cmd})
            time.sleep(0.2)

        action = "reused" if result["reused"] else "created"
        result["instructions"] = (
            f"Remote tmux session '{session_name}' {action}.\n"
            f"{len(result['windows'])} tasks running in parallel.\n\n"
            f"How to monitor:\n"
            f"  - tmux_check_remote_tasks: get status, exit codes, and duration of all tasks\n"
            f"  - tmux_run_command: 'tmux capture-pane -t {session_name}:<window_name> -p' for live output\n"
            f"  - tmux_run_command: 'tmux ls' to check session status\n\n"
            f"After SSH reconnection:\n"
            f"  - 'tmux attach -t {session_name}' to view interactively\n\n"
            f"Cleanup:\n"
            f"  - tmux_kill_remote_tasks to stop all tasks and clean up\n"
            f"  - Or: tmux_run_command 'tmux kill-session -t {session_name}'"
        )
        return result

    def check_remote_tmux_tasks(
        self,
        session_name: str = "ai_work",
        window_filter: Optional[str] = None,
        capture_lines: int = 10,
        timeout: float = 10.0,
    ) -> dict:
        """Check the status of tasks in a remote tmux session.

        Uses status files (written by setup_remote_tmux wrapper) for accurate
        tracking of exit codes, start/end times, and durations.
        Falls back to prompt detection when status files are unavailable.

        Args:
            session_name: remote tmux session name to check.
            window_filter: if provided, only check this specific window name.
            capture_lines: number of tail lines to capture per task (default 10).
            timeout: timeout per operation step.

        Returns:
            {
                "session_exists": bool,
                "tasks": list[{
                    "window": str,
                    "output_tail": str,
                    "is_running": bool,
                    "exit_code": int | None,
                    "duration_seconds": int | None,
                    "started_at": int | None,      # unix timestamp
                    "finished_at": int | None,      # unix timestamp
                }],
            }
        """
        result = {"session_exists": False, "tasks": []}

        # Check if session exists
        check = self.run_command(
            f"tmux has-session -t {shlex.quote(session_name)} 2>/dev/null && echo __EXISTS__ || echo __NOT_EXISTS__",
            max_wait=5
        )
        if "__NOT_EXISTS__" in check:
            return result
        result["session_exists"] = True

        # List all windows (using window name as identifier, more reliable than index)
        windows_output = self.run_command(
            f"tmux list-windows -t {shlex.quote(session_name)} -F '#{{window_name}}'",
            max_wait=5
        )

        window_names = [
            name.strip() for name in windows_output.strip().split("\n")
            if name.strip() and not name.strip().startswith(("📊", "⚠", "⏱"))
        ]

        # Filter to specific window if requested
        if window_filter:
            window_names = [w for w in window_names if w == window_filter]

        # Read all status files in batch for efficiency
        status_dir = f"/tmp/_tmux_tasks_{session_name}"
        status_raw = self.run_command(
            f"cat {shlex.quote(status_dir)}/*.status 2>/dev/null; echo __STATUS_END__",
            max_wait=5
        )
        # Parse status files into a dict keyed by window name
        status_map = self._parse_task_status_files(status_raw, window_names, status_dir)

        for win_name in window_names:
            # Capture pane output
            full_output = self.run_command(
                f"tmux capture-pane -t {shlex.quote(session_name)}:{shlex.quote(win_name)} -p",
                max_wait=5
            )

            # Remove meta info lines from output, keep only actual content
            clean_lines = []
            for line in full_output.strip().split("\n"):
                if line.strip().startswith(("📊", "⚠", "⏱")):
                    continue
                clean_lines.append(line)

            # Take last N lines as tail
            tail_lines = clean_lines[-capture_lines:] if clean_lines else []
            tail_text = "\n".join(tail_lines)

            # Check status from status file first
            status_info = status_map.get(win_name, {})
            is_running = status_info.get("is_running", None)
            exit_code = status_info.get("exit_code", None)
            duration = status_info.get("duration_seconds", None)
            started_at = status_info.get("started_at", None)
            finished_at = status_info.get("finished_at", None)

            # Fallback: determine if running from prompt detection
            if is_running is None:
                is_running = True
                last_nonempty = ""
                for line in reversed(clean_lines):
                    if line.strip():
                        last_nonempty = line.strip()
                        break
                for pattern in self.DEFAULT_PROMPT_PATTERNS:
                    if re.search(pattern, last_nonempty):
                        is_running = False
                        break

            # Calculate elapsed time for running tasks
            if is_running and started_at:
                # Get current time on remote
                now_output = self.run_command("date +%s", max_wait=3)
                try:
                    now_ts = int(re.search(r"(\d{10,})", now_output).group(1))
                    duration = now_ts - started_at
                except (AttributeError, ValueError):
                    pass

            result["tasks"].append({
                "window": win_name,
                "output_tail": tail_text[-500:] if tail_text else "",
                "is_running": is_running,
                "exit_code": exit_code,
                "duration_seconds": duration,
                "started_at": started_at,
                "finished_at": finished_at,
            })

        return result

    def _parse_task_status_files(
        self,
        raw_output: str,
        window_names: list[str],
        status_dir: str,
    ) -> dict:
        """Parse batch-read status files into a structured dict.

        Status file format (per window):
            STARTED <unix_timestamp>
            FINISHED <exit_code> <duration_seconds> <unix_timestamp>

        Returns:
            dict keyed by window name with status info.
        """
        status_map = {}

        # Read individual status files for each window
        for wname in window_names:
            status_file = f"{status_dir}/{wname}.status"
            content = self.run_command(
                f"cat {shlex.quote(status_file)} 2>/dev/null || echo __NO_STATUS__",
                max_wait=3
            )

            if "__NO_STATUS__" in content:
                continue

            info = {"is_running": True, "exit_code": None, "duration_seconds": None,
                    "started_at": None, "finished_at": None}

            for line in content.strip().split("\n"):
                line = line.strip()
                # STARTED <timestamp>
                m = re.match(r"STARTED\s+(\d+)", line)
                if m:
                    info["started_at"] = int(m.group(1))
                    continue
                # FINISHED <exit_code> <duration> <timestamp>
                m = re.match(r"FINISHED\s+(\d+)\s+(\d+)\s+(\d+)", line)
                if m:
                    info["exit_code"] = int(m.group(1))
                    info["duration_seconds"] = int(m.group(2))
                    info["finished_at"] = int(m.group(3))
                    info["is_running"] = False
                    continue

            status_map[wname] = info

        return status_map

    def kill_remote_tmux_tasks(
        self,
        session_name: str = "ai_work",
        window_name: Optional[str] = None,
        timeout: float = 5.0,
    ) -> dict:
        """Kill remote tmux tasks — either a specific window or the entire session.

        Also cleans up the status directory.

        Args:
            session_name: remote tmux session name.
            window_name: specific window to kill. If None, kills the entire session.
            timeout: timeout per operation step.

        Returns:
            {
                "success": bool,
                "killed": str,       # what was killed (session or window name)
                "message": str,
            }
        """
        result = {"success": False, "killed": "", "message": ""}

        # Check if session exists
        check = self.run_command(
            f"tmux has-session -t {shlex.quote(session_name)} 2>/dev/null && echo __EXISTS__ || echo __NOT_EXISTS__",
            max_wait=5
        )
        if "__NOT_EXISTS__" in check:
            result["message"] = f"Session '{session_name}' does not exist on remote host."
            return result

        if window_name:
            # Kill specific window
            kill_output = self.run_command(
                f"tmux kill-window -t {shlex.quote(session_name)}:{shlex.quote(window_name)} 2>&1; echo $?",
                max_wait=timeout
            )
            if "0" in kill_output.strip().split("\n")[-1:]:
                result["success"] = True
                result["killed"] = f"{session_name}:{window_name}"
                result["message"] = f"Window '{window_name}' killed in session '{session_name}'."
                # Clean up status file for this window
                status_file = f"/tmp/_tmux_tasks_{session_name}/{window_name}.status"
                self.run_command(f"rm -f {shlex.quote(status_file)}", max_wait=3)
            else:
                result["message"] = f"Failed to kill window '{window_name}': {kill_output}"
        else:
            # Kill entire session
            kill_output = self.run_command(
                f"tmux kill-session -t {shlex.quote(session_name)} 2>&1; echo $?",
                max_wait=timeout
            )
            result["success"] = True
            result["killed"] = session_name
            result["message"] = f"Session '{session_name}' and all its tasks killed."
            # Clean up status directory
            status_dir = f"/tmp/_tmux_tasks_{session_name}"
            self.run_command(f"rm -rf {shlex.quote(status_dir)}", max_wait=3)

        return result


# ----------------------------------------------------------------
# CLI interface for quick testing
# ----------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
    description="TmuxAgent CLI - send commands to a tmux session via tmux-mcp-agent"
    )
    parser.add_argument(
        "-s", "--session", default="remote_work",
        help="tmux session name (default: remote_work)"
    )
    parser.add_argument(
        "-t", "--target", default=None,
        help="tmux pane target (e.g. remote_work:0.1)"
    )

    sub = parser.add_subparsers(dest="action", required=True)

    # list sessions
    sub.add_parser("list", help="List all tmux sessions")

    # capture
    cap = sub.add_parser("capture", help="Capture pane output")
    cap.add_argument("-n", "--lines", type=int, default=100,
                     help="Number of lines to capture")

    # run command
    run = sub.add_parser("run", help="Run a command in the session")
    run.add_argument("command", help="Command to execute")
    run.add_argument("--wait", type=float, default=None,
                     help="Fixed wait time (disables smart wait)")

    # send raw keys
    send = sub.add_parser("send", help="Send raw keys")
    send.add_argument("keys", help="Keys to send")
    send.add_argument("--no-enter", action="store_true",
                      help="Don't press Enter after keys")

    # ctrl-c
    sub.add_parser("ctrl-c", help="Send Ctrl+C")

    args = parser.parse_args()

    ctrl = TmuxAgent(
        session_name=args.session,
        pane_target=args.target,
    )

    if args.action == "list":
        print(ctrl.list_sessions())

    elif args.action == "capture":
        print(ctrl.capture_pane(lines=args.lines))

    elif args.action == "run":
        if args.wait is not None:
            output = ctrl.run_command(args.command, wait=args.wait, smart_wait=False)
        else:
            output = ctrl.run_command(args.command)
        print(output)

    elif args.action == "send":
        ctrl.send_keys(args.keys, press_enter=not args.no_enter)
        print("Keys sent.")

    elif args.action == "ctrl-c":
        ctrl.send_ctrl_c()
        print("Ctrl+C sent.")


if __name__ == "__main__":
    main()
