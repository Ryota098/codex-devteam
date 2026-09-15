from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
RULES = REPO / "AGENTS.md"
ROLE_SKILLS = (
    REPO / "codex" / "skills" / "pm" / "SKILL.md",
    REPO / "codex" / "skills" / "tl" / "SKILL.md",
    REPO / "codex" / "skills" / "implementer" / "SKILL.md",
    REPO / "codex" / "skills" / "auditor" / "SKILL.md",
    REPO / "claude" / "skills" / "auditor" / "SKILL.md",
)


class DocumentWorkflowTest(unittest.TestCase):
    def test_workflow_engine_is_removed_from_new_role_sessions(self) -> None:
        self.assertFalse((REPO / "scripts" / "flowctl.py").exists())
        self.assertFalse((REPO / "scripts" / "flowctl_lib.py").exists())
        self.assertFalse((REPO / "tests" / "test_flowctl.py").exists())
        for skill in ROLE_SKILLS:
            text = skill.read_text(encoding="utf-8")
            self.assertNotIn("flowctl", text, skill)
            self.assertNotIn("role-start", text, skill)

        rules = RULES.read_text(encoding="utf-8")
        self.assertIn("工程ツールやprovider hookは廃止した", rules)
        self.assertIn("SSH、`scp`、`sftp`、`rsync`", rules)

    def test_legacy_hook_cleanup_only_removes_ai_devteam_groups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "hooks.json"
            config.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "PreToolUse": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "python3 ~/.ai-devteam/bin/flowctl hook --provider codex",
                                        }
                                    ]
                                },
                                {"hooks": [{"type": "command", "command": "./scripts/keep-hook"}]},
                            ],
                            "SessionEnd": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "python3 /tmp/flowctl.py hook --provider claude",
                                        }
                                    ]
                                }
                            ],
                        },
                        "keep": True,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(REPO / "scripts" / "remove_legacy_role_hooks.py"), str(config)],
                capture_output=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            cleaned = json.loads(config.read_text(encoding="utf-8"))
            self.assertTrue(cleaned["keep"])
            self.assertEqual(cleaned["hooks"], {"PreToolUse": [{"hooks": [{"type": "command", "command": "./scripts/keep-hook"}]}]})
            self.assertEqual(len(list(config.parent.glob("hooks.json.ai-devteam-retired-backup-*"))), 1)

    def test_compatibility_bridge_never_blocks_an_already_open_session(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(REPO / "scripts" / "retired_flowctl_compat.py"),
                "hook",
                "--provider",
                "codex",
            ],
            input='{"hook_event_name":"PreToolUse"}\n',
            capture_output=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_comment_migration_and_document_ownership_rules_are_preserved(self) -> None:
        rules = RULES.read_text(encoding="utf-8")
        self.assertIn("複数行のJSDoc", rules)
        self.assertIn("コメントの文末には「。」を付けない", rules)
        self.assertIn("migrate dev", rules)
        self.assertIn("隔離・破棄可能な環境", rules)
        self.assertIn("README、ガイド、運用手順、設計書、ADR等の正式文書はPMが更新する", rules)

    def test_role_skills_read_project_rules_and_do_not_replace_formal_sessions(self) -> None:
        rules = RULES.read_text(encoding="utf-8")
        self.assertIn("呼び名が`task10_builder_impl`", rules)
        self.assertIn("正式役割の代行であり禁止", rules)
        self.assertIn("PMは実装、監査、TL判断を目的とするサブエージェントを起動しない", rules)

        expected_rules = {
            "codex/skills/pm/SKILL.md": "`AGENTS.md`を全文確認",
            "codex/skills/tl/SKILL.md": "`AGENTS.md`を全文確認",
            "codex/skills/implementer/SKILL.md": "`AGENTS.md`を全文確認",
            "codex/skills/auditor/SKILL.md": "`AGENTS.md`を全文確認",
            "claude/skills/auditor/SKILL.md": "`CLAUDE.md`を全文確認",
        }
        for relative_path, marker in expected_rules.items():
            skill = (REPO / relative_path).read_text(encoding="utf-8")
            self.assertIn(marker, skill)
            self.assertIn("自動読込みだけを根拠に省略しない", skill)
        scenarios = (REPO / "tests" / "pm-scenarios.md").read_text(encoding="utf-8")
        self.assertIn("PMによる正式役割の子起動", scenarios)

    def test_pm_initial_understanding_and_narrow_followup_are_documented(self) -> None:
        rules = RULES.read_text(encoding="utf-8")
        skill = (REPO / "codex" / "skills" / "pm" / "SKILL.md").read_text(encoding="utf-8")
        delivery = (REPO / "codex" / "skills" / "pm" / "references" / "delivery-gates.md").read_text(
            encoding="utf-8"
        )
        scenarios = (REPO / "tests" / "pm-scenarios.md").read_text(encoding="utf-8")
        for text in (rules, skill):
            self.assertIn("初めて扱う機能・task・既存案件", text)
            self.assertIn("一時診断スクリプト", text)
            self.assertIn("初回把握", text)
        self.assertIn("フルテストはリリース前の定例儀式ではない", delivery)
        self.assertIn("初回キャッチアップ", scenarios)
        self.assertIn("既存CLIのdry-run失敗", scenarios)


if __name__ == "__main__":
    unittest.main()
