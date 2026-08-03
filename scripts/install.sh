#!/bin/sh
# ai-devteam のプロンプト一式をローカル環境へ配備する
# - prompts/*.md      → ~/.codex/prompts/        (Codexの /pm /tech-lead /implementer /auditor)
# - claude/skills/*   → ~/.claude/skills/        (Claude Codeの監査Skill)
# テンプレを改訂したら、このスクリプトを再実行して反映する
set -eu

repo_dir=$(cd "$(dirname "$0")/.." && pwd)

mkdir -p "$HOME/.codex/prompts"
cp "$repo_dir"/prompts/*.md "$HOME/.codex/prompts/"
echo "installed: $(ls "$repo_dir"/prompts/*.md | wc -l | tr -d ' ') prompts -> ~/.codex/prompts/"

mkdir -p "$HOME/.claude/skills/auditor"
cp "$repo_dir/claude/skills/auditor/SKILL.md" "$HOME/.claude/skills/auditor/SKILL.md"
echo "installed: auditor skill -> ~/.claude/skills/auditor/"

echo "done"
