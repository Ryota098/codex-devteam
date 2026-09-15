#!/bin/sh
# codex-devteam のテンプレ一式をローカル環境へ配備する
# - codex/skills/*  → ~/.agents/skills/   (Codexの $pm $tl $implementer $auditor)
# - claude/skills/* → ~/.claude/skills/   (Claude Codeの監査Skill /auditor)
# - 旧工程hookを設定から除去し、既に開いているセッション向けの無害な互換処理だけを置く
# テンプレを改訂したら、このスクリプトを再実行して反映する
set -eu

repo_dir=$(cd "$(dirname "$0")/.." && pwd)

python3 -B -m unittest discover -s "$repo_dir/tests" >/dev/null
echo "verified: document workflow regression tests"

mkdir -p "$HOME/.agents/skills"
if [ -d "$HOME/.agents/skills/tech-lead" ]; then
  rm -rf "$HOME/.agents/skills/tech-lead"
  echo "removed legacy skill: ~/.agents/skills/tech-lead/"
fi
for skill_dir in "$repo_dir"/codex/skills/*/; do
  name=$(basename "$skill_dir")
  rm -rf "$HOME/.agents/skills/$name"
  cp -R "$skill_dir" "$HOME/.agents/skills/$name"
done
echo "installed: $(ls -d "$repo_dir"/codex/skills/*/ | wc -l | tr -d ' ') codex skills -> ~/.agents/skills/"

mkdir -p "$HOME/.claude/skills/auditor"
cp "$repo_dir/claude/skills/auditor/SKILL.md" "$HOME/.claude/skills/auditor/SKILL.md"
echo "installed: auditor skill -> ~/.claude/skills/auditor/"

runtime_dir="$HOME/.ai-devteam/bin"
mkdir -p "$runtime_dir"
python3 -B "$repo_dir/scripts/remove_legacy_role_hooks.py" \
  "$HOME/.codex/hooks.json" \
  "$HOME/.claude/settings.json"
cp "$repo_dir/scripts/retired_flowctl_compat.py" "$runtime_dir/flowctl"
rm -f "$runtime_dir/flowctl_lib.py" "$runtime_dir/validate_handoff.py"
chmod 755 "$runtime_dir/flowctl"
echo "removed: retired workflow hooks and engine"
echo "installed: pass-through compatibility bridge for already-open sessions -> ~/.ai-devteam/bin/flowctl"

mkdir -p "$HOME/.codex"
for profile in "$repo_dir"/codex/profiles/*.config.toml; do
  cp "$profile" "$HOME/.codex/$(basename "$profile")"
done
echo "installed: codex least-privilege profiles -> ~/.codex/"

# 旧配備先(custom prompts。deprecated)の残骸を掃除する
for f in pm tech-lead implementer auditor; do
  if [ -f "$HOME/.codex/prompts/$f.md" ]; then
    rm "$HOME/.codex/prompts/$f.md"
    echo "removed legacy: ~/.codex/prompts/$f.md"
  fi
done

echo "note: ai-devteam is opt-in; roleless sessions stay normal until an explicit Skill is invoked"
echo "note: existing projects are not rewritten; copy $repo_dir/AGENTS.md to each Codex project and the matching CLAUDE.md to each Claude project when common rules change"
echo "note: new role Skills explicitly reread the matching project rule; existing sessions need one reread and do not need a restart"
echo "note: Codex permission profiles are optional hardening; legacy sandbox_mode in ~/.codex/config.toml takes precedence and disables them"
echo "done"
