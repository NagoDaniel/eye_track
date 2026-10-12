#!/usr/bin/env bash
# Formats the file Claude just edited. Never blocks: formatting problems are
# fixed automatically; real lint/type errors are caught by `make lint`.
# Tools are used only if installed, so this is a no-op before Milestone 1a.
set -u
root="${CLAUDE_PROJECT_DIR:-$(pwd)}"
file=$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("tool_input",{}).get("file_path",""))' 2>/dev/null) || exit 0
[ -n "$file" ] && [ -f "$file" ] || exit 0

find_tool() {  # $1 = tool name; prints a runnable path or nothing
  for c in "$root/backend/.venv/bin/$1" "$root/frontend/node_modules/.bin/$1"; do
    [ -x "$c" ] && { echo "$c"; return; }
  done
  command -v "$1" 2>/dev/null
}

case "$file" in
  *.py)
    ruff=$(find_tool ruff)
    [ -n "$ruff" ] && { "$ruff" format -q "$file"; "$ruff" check -q --fix-only "$file"; } >/dev/null 2>&1
    ;;
  *.ts|*.tsx|*.js|*.mjs|*.json|*.css|*.html|*.md|*.yml|*.yaml)
    case "$file" in */docs/PLAN.md|*/node_modules/*) exit 0 ;; esac
    prettier=$(find_tool prettier)
    [ -n "$prettier" ] && "$prettier" --log-level silent --write "$file" >/dev/null 2>&1
    ;;
esac
exit 0
