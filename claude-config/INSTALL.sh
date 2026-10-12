#!/usr/bin/env bash
# Moves this folder's contents into .claude/ (Claude's remote tools may not
# write to .claude directly). Run once from the repository root:
#   bash claude-config/INSTALL.sh
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p .claude
cp -R claude-config/agents claude-config/skills claude-config/hooks claude-config/settings.json .claude/
chmod +x .claude/hooks/format.sh
rm -rf claude-config
echo "Installed Claude Code config into .claude/"
git add -A .claude claude-config 2>/dev/null && git commit -qm "Install Claude Code config" && echo "Committed." || true
