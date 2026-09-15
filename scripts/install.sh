#!/bin/sh
# codex-devteam のテンプレ一式をローカル環境へ配備する
# - codex/skills/*  → ~/.agents/skills/   (Codexの $pm $tl $implementer $auditor)
# - claude/skills/* → ~/.claude/skills/   (Claude Codeの監査Skill /auditor)
# - scripts/flowctl* → ~/.ai-devteam/bin/ (役割・安全ガード、旧履歴の参照)
# - Codex/Claude lifecycle hooks（既存JSONを保持してマージ）
# テンプレを改訂したら、このスクリプトを再実行して反映する
set -eu

repo_dir=$(cd "$(dirname "$0")/.." && pwd)

python3 -B -m unittest discover -s "$repo_dir/tests" >/dev/null
echo "verified: flowctl regression tests"

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
cp "$repo_dir/scripts/flowctl.py" "$runtime_dir/flowctl"
cp "$repo_dir/scripts/flowctl_lib.py" "$runtime_dir/flowctl_lib.py"
rm -f "$runtime_dir/validate_handoff.py"
chmod 755 "$runtime_dir/flowctl" "$runtime_dir/flowctl_lib.py"
echo "installed: flowctl runtime -> ~/.ai-devteam/bin/"
echo "removed: obsolete handoff format validator"
echo "note: workflow approval/state-sync commands are retired; existing task history is preserved and is not an execution gate"

mkdir -p "$HOME/.codex"
for profile in "$repo_dir"/codex/profiles/*.config.toml; do
  cp "$profile" "$HOME/.codex/$(basename "$profile")"
done
echo "installed: codex least-privilege profiles -> ~/.codex/"

python3 -B "$runtime_dir/flowctl" install-hooks --provider codex --executable "$runtime_dir/flowctl"
python3 -B "$runtime_dir/flowctl" install-hooks --provider claude --executable "$runtime_dir/flowctl"

# 旧配備先(custom prompts。deprecated)の残骸を掃除する
for f in pm tech-lead implementer auditor; do
  if [ -f "$HOME/.codex/prompts/$f.md" ]; then
    rm "$HOME/.codex/prompts/$f.md"
    echo "removed legacy: ~/.codex/prompts/$f.md"
  fi
done

echo "note: restart sessions only if lifecycle hook configuration was added or changed; otherwise reread the updated common rules and current role Skill in the existing session"
echo "note: ai-devteam is opt-in; roleless sessions stay normal until an explicit Skill runs flowctl role-start"
echo "note: existing projects are not rewritten; copy $repo_dir/AGENTS.md to each Codex project and the matching CLAUDE.md to each Claude project when common rules change"
echo "note: new role Skills explicitly reread the matching project rule; existing sessions need one reread but no restart unless hook settings changed"
echo "note: Codex permission profiles are optional hardening; legacy sandbox_mode in ~/.codex/config.toml takes precedence and disables them"
echo "done"
