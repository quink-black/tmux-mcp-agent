#!/usr/bin/env bash
# setup.sh - Quick setup for tmux-mcp-agent
# Run: bash setup.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# ---- Detect platform ----
OS_TYPE="$(uname -s 2>/dev/null || echo 'unknown')"
case "$OS_TYPE" in
    Linux*)   PLATFORM="Linux" ;;
    Darwin*)  PLATFORM="macOS" ;;
    CYGWIN*|MINGW*|MSYS*) PLATFORM="Windows (Git Bash / MSYS2)" ;;
    *)        PLATFORM="$OS_TYPE" ;;
esac

# tmux does not run natively on Windows; WSL is required
if echo "$OS_TYPE" | grep -qiE 'CYGWIN|MINGW|MSYS'; then
    echo "⚠️  Detected Windows native shell ($PLATFORM)."
    echo "   tmux is NOT supported on Windows natively."
    echo "   Please use WSL (Windows Subsystem for Linux) instead:"
    echo "     1. Install WSL: https://learn.microsoft.com/en-us/windows/wsl/install"
    echo "     2. Open a WSL terminal and re-run this script."
    exit 1
fi

echo "🖥️  Platform: $PLATFORM"

echo "============================================"
echo "  tmux-mcp-agent - Setup"
echo "============================================"
echo ""

# ---- Check tmux ----
if command -v tmux &>/dev/null; then
    echo "✅ tmux found: $(tmux -V)"
else
    echo "❌ tmux not found. Please install tmux manually:"
    echo "   macOS:          brew install tmux"
    echo "   Debian/Ubuntu:  sudo apt install tmux"
    echo "   Fedora/RHEL:    sudo dnf install tmux"
    echo "   Arch Linux:     sudo pacman -S tmux"
    echo "   Windows (WSL):  sudo apt install tmux"
    exit 1
fi
echo ""

# ---- Check Python ----
PYTHON=""
if command -v python3 &>/dev/null; then
    PYTHON="python3"
elif command -v python &>/dev/null; then
    PYTHON="python"
else
    echo "❌ Python not found. Please install Python 3.10+."
    exit 1
fi

echo "✅ Python found: $($PYTHON --version)"
echo ""

# ---- Create virtual environment ----
if [ -d "$VENV_DIR" ]; then
    echo "✅ Virtual environment already exists at $VENV_DIR"
else
    echo "📦 Creating virtual environment..."
    $PYTHON -m venv "$VENV_DIR"
    echo "✅ Virtual environment created at $VENV_DIR"
fi
echo ""

# ---- Resolve venv bin/Scripts path ----
# Unix: .venv/bin/  |  Windows (WSL uses Unix layout too)
VENV_BIN="$VENV_DIR/bin"
if [ ! -d "$VENV_BIN" ] && [ -d "$VENV_DIR/Scripts" ]; then
    VENV_BIN="$VENV_DIR/Scripts"
fi

# ---- Install dependencies ----
echo "📦 Installing dependencies..."
"$VENV_BIN/pip" install --upgrade pip -q
"$VENV_BIN/pip" install -r "$SCRIPT_DIR/requirements.txt" -q
echo "✅ Dependencies installed"
echo ""

# ---- Print usage instructions ----
echo "============================================"
echo "  Setup Complete! Quick Start:"
echo "============================================"
echo ""
echo "1️⃣  Create a tmux session and login to your server:"
echo ""
echo "    tmux new-session -s remote_work"
echo "    ssh jump_host        # login to jump host"
echo "    ssh target_server    # login to target server"
echo ""
echo "2️⃣  Test the controller (in another terminal):"
echo ""
echo "    $VENV_BIN/python $SCRIPT_DIR/tmux_agent.py list"
echo "    $VENV_BIN/python $SCRIPT_DIR/tmux_agent.py capture"
echo "    $VENV_BIN/python $SCRIPT_DIR/tmux_agent.py run 'uptime'"
echo ""
echo "3️⃣  Start the MCP server (for AI agent integration):"
echo ""
echo "    $VENV_BIN/python $SCRIPT_DIR/mcp_server.py"
echo ""
echo "4️⃣  Add to your IDE MCP config (see README.md for details)"
echo ""
