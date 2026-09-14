from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import flowctl  # noqa: E402
import flowctl_lib as lib  # noqa: E402


NATURAL_INSTRUCTION = """# プロフィール更新

目的: 既存のプロフィール更新を安全に修正する
対象外: READMEとDB schema
受け入れ条件: 正常値を保存し、不正値を拒否する
検証: 直接テストと型検査
変更対象: profile serviceと直接テスト
"""


class RepoFixture:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "project"
        self.flow = self.root / "docs" / "flow" / "profile"
        self.task = self.flow / "task-01"

    def setup(self) -> None:
        self.root.mkdir(parents=True)
        (self.root / "AGENTS.md").write_text(lib.MANAGED_MARKER + "\n", encoding="utf-8")
        self.task.mkdir(parents=True)
        (self.task / "instruction.md").write_text(NATURAL_INSTRUCTION, encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "src" / "profile.py").write_text("NAME = 'before'\n", encoding="utf-8")
        (self.root / "package.json").write_text('{"scripts":{"test":"true"}}\n', encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.email", "tests@example.invalid"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Tests"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "."], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.root, check=True)

    def close(self) -> None:
        self.temp.cleanup()


class FlowctlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = RepoFixture()
        self.fixture.setup()
        self.old_cwd = Path.cwd()
        os.chdir(self.fixture.root)
        self.fake_home = self.fixture.root / "home"

    def tearDown(self) -> None:
        os.chdir(self.old_cwd)
        self.fixture.close()

    def invoke(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = flowctl.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def hook(self, session: str, tool: str, value: dict) -> object:
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": session,
            "cwd": str(self.fixture.root),
            "tool_name": tool,
            "tool_input": value,
        }
        with mock.patch.object(Path, "home", return_value=self.fake_home):
            return lib.handle_hook(payload, "codex")

    def role_start(self, session: str, role: str, task: Path | None = None) -> object:
        selected = task or self.fixture.task
        command = (
            f"{self.fake_home}/.ai-devteam/bin/flowctl role-start "
            f"--role {role} --task-dir {selected}"
        )
        return self.hook(session, "Bash", {"command": command})

    def patch(self, relative: str) -> dict:
        return {"command": f"*** Begin Patch\n*** Update File: {relative}\n*** End Patch"}

    def test_natural_instruction_and_package_script_do_not_need_a_permission_token(self) -> None:
        self.assertIsNone(self.role_start("implementer", "implementer"))
        self.assertIsNone(self.hook("implementer", "apply_patch", self.patch("src/profile.py")))
        self.assertIsNone(
            self.hook(
                "implementer",
                "Write",
                {"file_path": str(self.fixture.root / "package.json"), "content": "{}\n"},
            )
        )
        (self.fixture.task / "instruction.md").write_text("自由な文章だけの更新\n", encoding="utf-8")
        self.assertIsNone(self.hook("implementer", "apply_patch", self.patch("src/profile.py")))
        policy = lib.document_policy(self.fixture.task)
        self.assertTrue(policy["instruction_exists"])
        self.assertEqual(lib.parse_instruction(self.fixture.task, {}), ([], []))

    def test_only_actual_safety_boundaries_block_implementer(self) -> None:
        self.assertIsNone(self.role_start("implementer", "implementer"))
        for path in ("README.md", ".env", ".git/config", "docs/flow/profile/task-01/instruction.md"):
            with self.subTest(path=path):
                denied = self.hook("implementer", "apply_patch", self.patch(path))
                self.assertIsNotNone(denied)
        self.assertIsNone(lib.check_bash_command("npm install", "implementer", set()))
        self.assertIsNone(lib.check_bash_command("npx prisma migrate dev --name add_profile", "implementer", set()))
        self.assertIsNone(lib.check_bash_command("curl -fsS https://example.invalid/health", "implementer", set()))
        for command in (
            "git commit -m test",
            "curl -X POST https://example.invalid/api",
            "npm publish",
            "kubectl apply -f deployment.yaml",
            "printf value > output.txt",
            "cat .env.local",
            "npm run deploy -- --environment production",
        ):
            with self.subTest(command=command):
                self.assertIsNotNone(lib.check_bash_command(command, "implementer", set()))

    def test_retired_approval_commands_do_not_mutate_or_authorize(self) -> None:
        before = list(self.fixture.task.rglob("*"))
        code, output, error = self.invoke(
            "approve", "--task-dir", str(self.fixture.task), "--capability", "dependency-install", "--owner-confirmed"
        )
        self.assertEqual(code, 0, error)
        self.assertIn("廃止済み", output)
        self.assertFalse((self.fixture.task / ".ai-devteam").exists())
        self.assertEqual(before, list(self.fixture.task.rglob("*")))
        code, output, error = self.invoke("validate", "--task-dir", str(self.fixture.task))
        self.assertEqual(code, 0, error)
        self.assertIn("実装可否のゲートではありません", output)

    def test_owner_can_turn_an_audit_into_same_conversation_pm_and_implementation(self) -> None:
        self.assertIsNone(self.role_start("audit", "auditor-codex"))
        self.assertIsNone(self.role_start("audit", "owner-directed"))
        with mock.patch.object(Path, "home", return_value=self.fake_home):
            record = lib.load_runtime_session("codex", "audit")
        self.assertEqual(record["role"], "owner-directed")
        self.assertEqual(record["role_handoff"]["from_role"], "auditor-codex")
        self.assertFalse(record["role_handoff"]["counts_as_independent_audit"])
        self.assertIsNone(self.hook("audit", "apply_patch", self.patch("src/profile.py")))
        self.assertIsNone(self.hook("audit", "apply_patch", self.patch("README.md")))
        self.assertIsNone(
            self.hook("audit", "apply_patch", self.patch("docs/flow/profile/task-01/instruction.md"))
        )
        denied = self.hook("audit", "Bash", {"command": "curl -X POST https://example.invalid/api"})
        self.assertIsNotNone(denied)

    def test_owner_can_turn_pm_into_same_conversation_implementation(self) -> None:
        self.assertIsNone(self.role_start("pm-direct", "pm"))
        self.assertIsNone(self.role_start("pm-direct", "owner-directed"))
        with mock.patch.object(Path, "home", return_value=self.fake_home):
            record = lib.load_runtime_session("codex", "pm-direct")
        self.assertEqual(record["role"], "owner-directed")
        self.assertEqual(record["role_handoff"]["from_role"], "pm")
        self.assertIsNone(self.hook("pm-direct", "apply_patch", self.patch("src/profile.py")))

    def test_pm_cannot_create_product_or_temporary_diagnostic_files(self) -> None:
        self.assertIsNone(self.role_start("pm-diagnose", "pm"))
        self.assertIsNotNone(self.hook("pm-diagnose", "apply_patch", self.patch("src/diagnose.ts")))
        self.assertIsNotNone(
            self.hook(
                "pm-diagnose",
                "Write",
                {"file_path": "/private/tmp/pm-diagnose.ts", "content": "export {}\n"},
            )
        )

    def test_owner_directed_transfer_cannot_switch_to_a_different_task(self) -> None:
        second = self.fixture.flow / "task-02"
        second.mkdir()
        (second / "instruction.md").write_text("# another\n", encoding="utf-8")
        self.assertIsNotNone(self.role_start("direct", "owner-directed"))
        self.assertIsNone(self.role_start("audit", "auditor-codex"))
        denied = self.role_start("audit", "owner-directed", second)
        self.assertIsNotNone(denied)
        self.assertIn("役割変更", denied["hookSpecificOutput"]["permissionDecisionReason"])

    def test_normal_role_ownership_and_independent_audit_boundaries_remain(self) -> None:
        self.assertIsNone(self.role_start("pm", "pm"))
        self.assertIsNotNone(self.hook("pm", "apply_patch", self.patch("src/profile.py")))
        self.assertIsNone(self.hook("pm", "apply_patch", self.patch("README.md")))
        self.assertIsNone(self.role_start("audit", "auditor-codex"))
        self.assertIsNone(
            self.hook("audit", "apply_patch", self.patch("docs/flow/profile/task-01/audit-codex-safety.md"))
        )
        self.assertIsNotNone(self.hook("audit", "apply_patch", self.patch("src/profile.py")))
        self.assertIsNotNone(
            self.hook("audit", "apply_patch", self.patch("docs/flow/profile/task-01/audit-claude-safety.md"))
        )

    def test_auditor_cannot_impersonate_the_other_provider(self) -> None:
        self.assertIsNone(self.role_start("audit", "auditor-codex"))
        denied = self.role_start("audit", "auditor-claude")
        self.assertIsNotNone(denied)
        self.assertIn("provider", denied["hookSpecificOutput"]["permissionDecisionReason"])

    def test_roleless_sessions_are_unmodified_until_explicit_start(self) -> None:
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "ordinary",
            "cwd": str(self.fixture.root),
            "tool_name": "apply_patch",
            "tool_input": self.patch("src/profile.py"),
        }
        with mock.patch.object(Path, "home", return_value=self.fake_home):
            self.assertIsNone(lib.handle_hook(payload, "codex"))
        self.assertFalse(lib.runtime_session_path("codex", "ordinary").exists())

    def test_multiline_command_renderer_keeps_paths_copyable(self) -> None:
        command = flowctl.flowctl_command_block(
            "status",
            [("--task-dir", "/tmp/project with spaces/docs/flow/task-01")],
        )
        self.assertIn("```sh", command)
        self.assertIn("\\\n", command)
        self.assertNotIn("scope-lock", command)

    def test_skill_metadata_keeps_roles_opt_in(self) -> None:
        for config in (REPO / "codex" / "skills").glob("*/agents/openai.yaml"):
            self.assertIn("allow_implicit_invocation: false", config.read_text(encoding="utf-8"))
        for skill in (REPO / "claude" / "skills").glob("*/SKILL.md"):
            frontmatter = skill.read_text(encoding="utf-8").split("---", 2)[1]
            self.assertIn("disable-model-invocation: true", frontmatter)


if __name__ == "__main__":
    unittest.main()
