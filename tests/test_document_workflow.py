from __future__ import annotations

import unittest
from pathlib import Path

import test_flowctl as fixtures


REPO = Path(__file__).resolve().parents[1]
lib = fixtures.lib


class DocumentWorkflowTest(unittest.TestCase):
    def test_no_handoff_format_validator_or_capability_command_remains(self) -> None:
        forbidden = (
            REPO / "scripts" / "validate_handoff.py",
            REPO / "codex" / "skills" / "pm" / "scripts" / "validate_handoff.py",
        )
        for path in forbidden:
            self.assertFalse(path.exists(), path)
        for path in (REPO / "README.md", REPO / "AGENTS.md", REPO / "scripts" / "install.sh"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("flowctl approve", text)
            self.assertNotIn("dependency-install", text)

    def test_instruction_is_human_readable_not_a_fixed_machine_format(self) -> None:
        fixture = fixtures.RepoFixture()
        fixture.setup()
        try:
            instruction = fixture.task / "instruction.md"
            instruction.write_text("修正対象は既存serviceと直接テスト\n", encoding="utf-8")
            self.assertEqual(lib.parse_instruction(fixture.task, {}), ([], []))
            self.assertTrue(lib.document_policy(fixture.task)["instruction_exists"])
            self.assertIsNone(
                lib.check_write_path("implementer", "src/new_service.py", None, fixture.root, fixture.task)
            )
        finally:
            fixture.close()

    def test_document_ownership_stays_separate_except_owner_directed_mode(self) -> None:
        fixture = fixtures.RepoFixture()
        fixture.setup()
        try:
            self.assertIsNotNone(
                lib.check_write_path("implementer", "README.md", None, fixture.root, fixture.task)
            )
            self.assertIsNone(lib.check_write_path("pm", "README.md", None, fixture.root, fixture.task))
            self.assertIsNone(
                lib.check_write_path("owner-directed", "README.md", None, fixture.root, fixture.task)
            )
            self.assertIsNone(
                lib.check_write_path(
                    "owner-directed",
                    "docs/flow/profile/task-01/instruction.md",
                    None,
                    fixture.root,
                    fixture.task,
                )
            )
        finally:
            fixture.close()

    def test_owner_directed_and_optional_evaluator_are_documented_for_all_providers(self) -> None:
        auditor_files = (
            REPO / "codex" / "skills" / "auditor" / "SKILL.md",
            REPO / "claude" / "skills" / "auditor" / "SKILL.md",
        )
        for path in auditor_files:
            text = path.read_text(encoding="utf-8")
            self.assertIn("owner-directed", text)
            self.assertIn("新規セッションやコピペを要求しない", text)
            self.assertIn("オーナー判断に従う", text)
        loop = (REPO / "codex" / "skills" / "implementer" / "references" / "implementation-loop.md").read_text(encoding="utf-8")
        self.assertIn("必要な場合だけ", loop)
        self.assertNotIn("高リスクなら必須", loop)

    def test_comment_and_migration_rules_are_preserved_without_flowctl_permission(self) -> None:
        rules = (REPO / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("複数行のJSDoc", rules)
        self.assertIn("コメントの文末には「。」を付けない", rules)
        self.assertIn("migrate dev", rules)
        self.assertIn("隔離・破棄可能な環境", rules)
        self.assertIn("flowctl許可は不要", rules)

    def test_pm_initial_understanding_and_narrow_followup_are_documented(self) -> None:
        rules = (REPO / "AGENTS.md").read_text(encoding="utf-8")
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

    def test_role_skills_read_current_project_rules_and_do_not_replace_formal_sessions(self) -> None:
        rules = (REPO / "AGENTS.md").read_text(encoding="utf-8")
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
        pm = (REPO / "codex" / "skills" / "pm" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Builder実装担当", pm)
        self.assertIn("`role-start`を使わない", pm)
        scenarios = (REPO / "tests" / "pm-scenarios.md").read_text(encoding="utf-8")
        self.assertIn("PMによる正式役割の子起動", scenarios)


if __name__ == "__main__":
    unittest.main()
