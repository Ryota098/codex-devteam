#!/bin/sh
# ai-devteam のテンプレ一式をローカル環境へ配備する
# - codex/skills/*  → ~/.agents/skills/   (Codexの $pm $tech-lead $implementer $auditor)
# - claude/skills/* → ~/.claude/skills/   (Claude Codeの監査Skill /auditor)
# テンプレを改訂したら、このスクリプトを再実行して反映する
set -eu

repo_dir=$(cd "$(dirname "$0")/.." && pwd)

mkdir -p "$HOME/.agents/skills"
for skill_dir in "$repo_dir"/codex/skills/*/; do
  name=$(basename "$skill_dir")
  rm -rf "$HOME/.agents/skills/$name"
  cp -R "$skill_dir" "$HOME/.agents/skills/$name"
done
echo "installed: $(ls -d "$repo_dir"/codex/skills/*/ | wc -l | tr -d ' ') codex skills -> ~/.agents/skills/"

mkdir -p "$HOME/.claude/skills/auditor"
cp "$repo_dir/claude/skills/auditor/SKILL.md" "$HOME/.claude/skills/auditor/SKILL.md"
echo "installed: auditor skill -> ~/.claude/skills/auditor/"

# 旧配備先(custom prompts。deprecated)の残骸を掃除する
for f in pm tech-lead implementer auditor; do
  if [ -f "$HOME/.codex/prompts/$f.md" ]; then
    rm "$HOME/.codex/prompts/$f.md"
    echo "removed legacy: ~/.codex/prompts/$f.md"
  fi
done

echo "done"
