#!/usr/bin/env bash
# Glean MCP — one-shot installer
# Sets up a virtualenv and registers the server with Claude Code + Claude Desktop.

set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$DIR/.venv"
PYTHON="$VENV/bin/python3"
SERVER="$DIR/server.py"

echo "=== Glean MCP installer ==="
echo "Directory: $DIR"
echo ""

# ── 1. Create venv ─────────────────────────────────────────────────────────────
if [ ! -f "$PYTHON" ]; then
  echo "Creating virtualenv …"
  python3 -m venv "$VENV"
fi

echo "Installing dependencies …"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r "$DIR/requirements.txt"
echo "  Done."
echo ""

# ── 2. Smoke-test import ───────────────────────────────────────────────────────
echo "Checking imports …"
"$PYTHON" -c "import mcp, httpx, sentence_transformers, rank_bm25, anthropic; print('  All imports OK')"
echo ""

# ── 3. The MCP server config block ────────────────────────────────────────────
CONFIG_BLOCK=$(cat <<JSONBLOCK
{
  "mcpServers": {
    "glean": {
      "command": "$PYTHON",
      "args": ["$SERVER"],
      "env": {
        "ANTHROPIC_API_KEY": "<your-key-here>"
      }
    }
  }
}
JSONBLOCK
)

# ── 4. Claude Code ─────────────────────────────────────────────────────────────
CLAUDE_CODE_SETTINGS="$HOME/.claude/settings.json"
echo "--- Claude Code ---"
if [ -f "$CLAUDE_CODE_SETTINGS" ]; then
  # Check if mcpServers key already exists
  if python3 -c "import json,sys; d=json.load(open('$CLAUDE_CODE_SETTINGS')); sys.exit(0 if 'mcpServers' not in d else 1)" 2>/dev/null; then
    # Safe to merge: add mcpServers
    python3 - <<PYEOF
import json
path = "$CLAUDE_CODE_SETTINGS"
with open(path) as f:
    cfg = json.load(f)
cfg.setdefault("mcpServers", {})["glean"] = {
    "command": "$PYTHON",
    "args": ["$SERVER"],
    "env": {"ANTHROPIC_API_KEY": "<your-key-here>"}
}
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print("  Added glean to $CLAUDE_CODE_SETTINGS")
PYEOF
  else
    echo "  mcpServers already exists in $CLAUDE_CODE_SETTINGS"
    echo "  Manually add this block under 'mcpServers':"
    echo ""
    echo '    "glean": {'
    echo "      \"command\": \"$PYTHON\","
    echo "      \"args\": [\"$SERVER\"],"
    echo '      "env": {"ANTHROPIC_API_KEY": "<your-key-here>"}'
    echo '    }'
  fi
else
  echo "  $CLAUDE_CODE_SETTINGS not found — creating it."
  python3 -c "
import json
cfg = {'mcpServers': {'glean': {'command': '$PYTHON', 'args': ['$SERVER'], 'env': {'ANTHROPIC_API_KEY': '<your-key-here>'}}}}
open('$CLAUDE_CODE_SETTINGS','w').write(json.dumps(cfg, indent=2))
"
  echo "  Created $CLAUDE_CODE_SETTINGS"
fi
echo ""

# ── 5. Claude Desktop ─────────────────────────────────────────────────────────
DESKTOP_DIR="$HOME/Library/Application Support/Claude"
DESKTOP_CFG="$DESKTOP_DIR/claude_desktop_config.json"
echo "--- Claude Desktop ---"
if [ ! -d "$DESKTOP_DIR" ]; then
  echo "  Claude Desktop config dir not found — skipping (not installed or not on Mac)."
else
  if [ -f "$DESKTOP_CFG" ]; then
    python3 - <<PYEOF
import json
path = "$DESKTOP_CFG"
with open(path) as f:
    cfg = json.load(f)
cfg.setdefault("mcpServers", {})["glean"] = {
    "command": "$PYTHON",
    "args": ["$SERVER"],
    "env": {"ANTHROPIC_API_KEY": "<your-key-here>"}
}
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print("  Added glean to Claude Desktop config.")
PYEOF
  else
    python3 -c "
import json
cfg = {'mcpServers': {'glean': {'command': '$PYTHON', 'args': ['$SERVER'], 'env': {'ANTHROPIC_API_KEY': '<your-key-here>'}}}}
open('$DESKTOP_CFG','w').write(json.dumps(cfg, indent=2))
"
    echo "  Created $DESKTOP_CFG"
  fi
fi
echo ""

# ── 6. Final instructions ──────────────────────────────────────────────────────
cat <<MSG
=== Installation complete ===

Next steps:

1. Replace <your-key-here> with your actual ANTHROPIC_API_KEY in:
     $CLAUDE_CODE_SETTINGS
     $DESKTOP_CFG   (if Claude Desktop is installed)

2. Restart Claude Code or Claude Desktop to pick up the new MCP server.

3. Test it — paste a Substack URL into Claude and ask:
     "Search https://aakashgupta.substack.com for content about pricing strategy"

Tools available:
  search_substack   — returns ranked excerpts (Claude synthesizes the answer)
  list_posts        — lists all public posts from the newsletter
  ask_substack      — full answer via Haiku (only if ANTHROPIC_API_KEY is set)

MSG
