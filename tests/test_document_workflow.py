from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_flowctl as fixtures


flowctl = fixtures.flowctl
lib = fixtures.lib


class DocumentWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.RepoFixture()
        self.base = self.fixture.setup()
        self.old_cwd = Path.cwd()
        os.chdir(self.fixture.root)

    def tearDown(self) -> None:
        os.chdir(self.old_cwd)
        self.fixture.close()

    invoke = fixtures.FlowctlTest.invoke
    lock_and_init = fixtures.FlowctlTest.lock_and_init
    begin_implementation = fixtures.FlowctlTest.begin_implementation
    complete_lightweight_preflight = fixtures.FlowctlTest.complete_lightweight_preflight

    def test_legacy_status_and_pm_start_do_not_initialize_or_replay(self) -> None:
        for role in ("pm", "tl", "implementer"):
            code, output, error = self.invoke("role-start", "--role", role, "--task-dir", str(self.fixture.task))
            self.assertEqual(code, 0, error)
            self.assertIn("documents", output)
        code, output, error = self.invoke("status", "--task-dir", str(self.fixture.task), "--json")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["workflow"], "documents")
        self.assertIsNone(json.loads(output)["state"])
        self.assertFalse(lib.task_meta_dir(self.fixture.task).exists())

    def test_parent_discovery_lists_handoff_artifacts_without_selecting_a_task(self) -> None:
        workspace = self.fixture.root / "workspace"
        entry = workspace / "service-a" / "docs" / "flow" / "search" / "tasks.md"
        entry.parent.mkdir(parents=True)
        entry.write_text("# 現在地\n", encoding="utf-8")
        task = entry.parent / "task-01"
        task.mkdir()
        (task / "instruction.md").write_text("# 指示書\n", encoding="utf-8")
        decoy = workspace / "service-a" / "node_modules" / "package" / "docs" / "flow" / "decoy"
        decoy.mkdir(parents=True)
        # Use a parent outside a managed-root lookup: discovery is intentionally
        # limited to the supplied directory and its immediate repositories.
        with mock.patch.object(flowctl, "find_managed_root", return_value=None):
            code, output, error = self.invoke("status", "--project-root", str(workspace), "--json")
        self.assertEqual(code, 0, error)
        value = json.loads(output)
        self.assertFalse(value["current_task_inferred"])
        self.assertEqual(len(value["features"]), 1)
        self.assertEqual(value["features"][0]["entrypoints"], [str(entry.resolve())])
        self.assertEqual(value["features"][0]["tasks"][0]["workflow"], "documents")
        self.assertFalse(lib.task_meta_dir(task).exists())

    def test_managed_status_is_read_only(self) -> None:
        self.lock_and_init()
        meta = lib.task_meta_dir(self.fixture.task)
        before = {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in meta.rglob("*") if path.is_file()}
        code, output, error = self.invoke("status", "--task-dir", str(self.fixture.task), "--json")
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["workflow"], "managed")
        after = {str(path): (path.read_bytes(), path.stat().st_mtime_ns) for path in meta.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_document_paths_still_enforce_role_and_project_boundaries(self) -> None:
        check = lambda role, path: lib.check_write_path(role, path, None, self.fixture.root, self.fixture.task)
        self.assertIsNone(check("implementer", "src/profile.py"))
        for path in ("README.md", "other/unapproved.py", ".env", "../other.py", "docs/flow/profile/task-01/instruction.md"):
            with self.subTest(path=path):
                self.assertIsNotNone(check("implementer", path))
        self.assertIsNone(check("pm", "docs/flow/profile/task-01/audit-triage-remediation.md"))
        self.assertIsNone(check("auditor-codex", "docs/flow/profile/task-01/audit-codex-regression.md"))
        self.assertIsNotNone(check("auditor-codex", "docs/flow/profile/task-01/audit-claude-regression.md"))
        self.assertIsNotNone(check("pm", "src/profile.py"))

    def test_unsafe_or_ambiguous_document_allowlists_do_not_grant_writes(self) -> None:
        instruction = self.fixture.task / "instruction.md"
        for pattern in (
            "/etc/config", "../config", "**", "*/**", "**/**", "./**", "./**/**",
            ".//./**", ".//./**/**", ".///././**", "./", "./.claude//**",
            ".env", "README.md", "docs/flow/**", ".git/**", ".git*",
            "./.codex/**", "./.claude/**", "./AGENTS.md", "./CLAUDE.md",
        ):
            with self.subTest(pattern=pattern):
                instruction.write_text(f"## 実装担当の変更許可パス\n\n- `{pattern}`\n", encoding="utf-8")
                with self.assertRaises(lib.FlowError):
                    lib.document_policy(self.fixture.task)
        instruction.write_text("## 実装担当の変更許可パス\n\n- `src/**`\n\n## 実装担当の変更許可パス\n\n- `tests/**`\n", encoding="utf-8")
        with self.assertRaises(lib.FlowError):
            lib.document_policy(self.fixture.task)

    def test_fixed_scope_cannot_be_bypassed_with_policyless_sibling(self) -> None:
        self.lock_and_init()
        sibling = self.fixture.flow / "task-02"
        sibling.mkdir()
        (sibling / "instruction.md").write_text(fixtures.VALID_INSTRUCTION, encoding="utf-8")
        self.assertIsNotNone(lib.check_write_path("implementer", "src/profile.py", None, self.fixture.root, sibling))
        code, _, _ = self.invoke("role-start", "--role", "implementer", "--task-dir", str(sibling))
        self.assertNotEqual(code, 0)

    def test_equivalent_document_paths_allow_only_the_same_code_boundary(self) -> None:
        instruction = self.fixture.task / "instruction.md"
        for pattern in ("src/**", "./src/**", ".//./src//**", "././src/profile.py"):
            with self.subTest(pattern=pattern):
                instruction.write_text(f"## 実装担当の変更許可パス\n\n- `{pattern}`\n", encoding="utf-8")
                policy = lib.document_policy(self.fixture.task)
                self.assertIsNone(lib.check_write_path("implementer", "src/profile.py", policy, self.fixture.root, self.fixture.task))
                for path in ("unrelated/admin.py", ".git/config", "README.md"):
                    self.assertIsNotNone(lib.check_write_path("implementer", path, policy, self.fixture.root, self.fixture.task))

    def test_create_only_migration_still_requires_isolated_database_permissions(self) -> None:
        command = "npx prisma migrate dev --name add_profile --create-only"
        self.assertIsNotNone(lib.check_bash_command(command, "implementer", set()))
        self.assertIsNotNone(lib.check_bash_command(command, "implementer", {"isolated-db"}))
        self.assertIsNone(lib.check_bash_command(command, "implementer", {"isolated-db", "migration"}))

    def test_pm_can_prepare_remediation_without_granting_implementation(self) -> None:
        self.begin_implementation()
        path = "docs/flow/profile/task-01/audit-triage.md"
        self.assertIsNone(lib.check_role_write_state("pm", self.fixture.task, path))
        self.assertIsNotNone(lib.check_role_write_state("pm", self.fixture.task, "src/profile.py"))
        self.assertEqual(lib.current_state(lib.load_events(self.fixture.task)), "implementation")
        instruction = self.fixture.task / "instruction.md"
        instruction.write_text(fixtures.VALID_INSTRUCTION + "\n## 是正\n境界値の回帰試験を追加する。\n", encoding="utf-8")
        self.assertIsNotNone(lib.check_role_write_state("implementer", self.fixture.task, "src/profile.py"))
        code, _, error = self.invoke("instruction-ready", "--task-dir", str(self.fixture.task))
        self.assertEqual(code, 0, error)
        self.assertEqual(lib.current_state(lib.load_events(self.fixture.task)), "instruction_ready")
        self.assertIsNotNone(lib.check_role_write_state("implementer", self.fixture.task, "src/profile.py"))

    def test_nested_or_feature_root_document_task_cannot_bypass_fixed_scope(self) -> None:
        self.lock_and_init()
        nested = self.fixture.task / "remediation"
        nested.mkdir()
        with mock.patch.object(Path, "home", return_value=self.fixture.root / "home"):
            for index, task in enumerate((nested, self.fixture.flow)):
                (task / "instruction.md").write_text(fixtures.VALID_INSTRUCTION, encoding="utf-8")
                with self.subTest(task=task):
                    with self.assertRaises(lib.FlowError):
                        lib.document_policy(task)
                    session = f"nested-{index}"
                    denial = lib.register_runtime_role("codex", session, self.fixture.root, self.fixture.root, "implementer", task)
                    self.assertIsNotNone(denial)
                    self.assertIsNone(lib.load_runtime_session("codex", session))

    def test_invalid_task_start_does_not_leave_an_authorizing_runtime_record(self) -> None:
        outside = self.fixture.root / "work"
        outside.mkdir()
        (outside / "instruction.md").write_text(fixtures.VALID_INSTRUCTION, encoding="utf-8")
        with mock.patch.object(Path, "home", return_value=self.fixture.root / "home"):
            payload = {"hook_event_name": "PreToolUse", "session_id": "invalid", "cwd": str(self.fixture.root), "tool_name": "exec_command", "tool_input": {"cmd": f"flowctl role-start --role implementer --task-dir {outside}"}}
            self.assertIsNotNone(lib.handle_hook(payload, "codex"))
            self.assertIsNone(lib.load_runtime_session("codex", "invalid"))
            self.assertNotEqual(self.invoke("role-start", "--role", "implementer", "--task-dir", str(outside))[0], 0)

    def test_continued_managed_implementer_can_resume_updated_instruction(self) -> None:
        self.begin_implementation()
        task = self.fixture.task
        with tempfile.TemporaryDirectory(prefix="ai-devteam-test-home-") as runtime_home, mock.patch.object(Path, "home", return_value=Path(runtime_home)):
            self.assertIsNone(lib.register_runtime_role("codex", "continue", self.fixture.root, self.fixture.root, "implementer", task))
            (task / "instruction.md").write_text(fixtures.VALID_INSTRUCTION + "\n追加の範囲内回帰確認\n", encoding="utf-8")
            code, _, error = self.invoke("instruction-ready", "--task-dir", str(task))
            self.assertEqual(code, 0, error)
            code, output, error = self.invoke("next", "--task-dir", str(task), "--provider", "codex")
            self.assertEqual(code, 0, error)
            self.assertIn("role-start", output)
            code, _, error = self.invoke("role-start", "--role", "implementer", "--task-dir", str(task))
            self.assertEqual(code, 0, error)
            self.assertEqual(lib.current_state(lib.load_events(task)), "implementation_preflight")
            self.complete_lightweight_preflight()
            self.assertEqual(lib.current_state(lib.load_events(task)), "implementation")
            self.assertIsNone(lib.check_role_write_state("implementer", task, "src/profile.py"))

    def test_git_metadata_cannot_be_written_even_with_an_overbroad_policy(self) -> None:
        policy = {"allowed_write_globs": ["**"]}
        for role in ("pm", "implementer", "tl", "auditor-codex", "auditor-claude"):
            for path in (".git", ".git/config", "./.git/hooks/pre-commit"):
                with self.subTest(role=role, path=path):
                    self.assertIsNotNone(lib.check_write_path(role, path, policy, self.fixture.root, self.fixture.task))

    def test_secret_references_cannot_hide_in_absolute_paths_or_beside_samples(self) -> None:
        for command in (
            "cat .env", "cat /tmp/project/.env.local", "cat nested/.env",
            "cat /tmp/project/key.pem", "cat /tmp/project/.ssh/id_rsa",
            "cat /tmp/project/credentials.json", "cat .env.example .env",
            "cat .env.example /tmp/project/.env.local",
        ):
            with self.subTest(command=command):
                self.assertIsNotNone(lib.check_bash_command(command, "implementer", set()))
        self.assertIsNone(lib.check_bash_command("cat .env.example", "implementer", set()))
        self.assertIsNone(lib.check_bash_command("cat /tmp/project/.env.example", "implementer", set()))
        self.assertIsNone(lib.check_bash_command("cat src/workspace-db-credential.service.ts", "implementer", set()))

    def test_pm_can_update_handoff_index_but_not_frozen_audit_inputs(self) -> None:
        self.lock_and_init()
        lib.append_event(self.fixture.task, "transition", data={"from": "planning", "to": "auditing"})
        for name in ("tasks.md", "audit-triage.md", "instruction-draft.md"):
            self.assertIsNone(lib.check_role_write_state("pm", self.fixture.task, f"docs/flow/profile/task-01/{name}"))
        for name in ("spec.md", "instruction.md", "audit-request.md"):
            self.assertIsNotNone(lib.check_role_write_state("pm", self.fixture.task, f"docs/flow/profile/task-01/{name}"))

    def test_document_capability_is_explicit_and_role_bound(self) -> None:
        self.assertNotIn("isolated-db", lib.current_capabilities(self.fixture.task, "implementer"))
        code, _, error = self.invoke("approve", "--task-dir", str(self.fixture.task), "--capability", "isolated-db", "--role", "implementer", "--minutes", "10", "--reason", "isolated test", "--owner-confirmed")
        self.assertEqual(code, 0, error)
        self.assertIn("isolated-db", lib.current_capabilities(self.fixture.task, "implementer"))
        self.assertNotIn("isolated-db", lib.current_capabilities(self.fixture.task, "pm"))
        self.assertFalse(lib.policy_path(self.fixture.task).exists())

    def test_role_continues_and_instruction_change_requires_reread(self) -> None:
        fake_home = self.fixture.root / "home"
        with mock.patch.object(Path, "home", return_value=fake_home):
            def hook(tool: str, value: dict) -> object:
                return lib.handle_hook({"hook_event_name": "PreToolUse", "session_id": "continued", "cwd": str(self.fixture.root), "tool_name": tool, "tool_input": value}, "codex")
            command = f"flowctl role-start --role implementer --task-dir {self.fixture.task}"
            self.assertIsNone(hook("exec_command", {"cmd": command}))
            patch = {"command": "*** Begin Patch\n*** Update File: src/profile.py\n*** End Patch"}
            self.assertIsNone(hook("apply_patch", patch))
            self.assertIsNone(hook("exec_command", {"cmd": "git status --short"}))
            self.assertIsNone(hook("apply_patch", patch))
            instruction = self.fixture.task / "instruction.md"
            instruction.write_text(fixtures.VALID_INSTRUCTION + "\n追加の境界試験\n", encoding="utf-8")
            self.assertIsNotNone(hook("apply_patch", patch))
            self.assertIsNone(hook("exec_command", {"cmd": command}))
            self.assertIsNone(hook("apply_patch", patch))
            self.assertIsNotNone(hook("exec_command", {"cmd": "flowctl role-start --role pm"}))

    def test_parent_role_start_honors_project_root_and_does_not_leak_to_another_repo(self) -> None:
        fake_home = self.fixture.root / "home"
        other = self.fixture.root / "other"
        other.mkdir()
        (other / "AGENTS.md").write_text(lib.MANAGED_MARKER, encoding="utf-8")
        with mock.patch.object(Path, "home", return_value=fake_home):
            payload = {"hook_event_name": "PreToolUse", "session_id": "parent", "cwd": str(self.fixture.root.parent), "tool_name": "exec_command", "tool_input": {"cmd": f"flowctl role-start --role pm --project-root {self.fixture.root}"}}
            self.assertIsNone(lib.handle_hook(payload, "codex"))
            self.assertEqual(lib.load_runtime_session("codex", "parent")["root"], str(self.fixture.root.resolve()))
            payload.update(cwd=str(other), tool_name="apply_patch", tool_input={"command": "*** Begin Patch\n*** Update File: src/profile.py\n*** End Patch"})
            self.assertIsNotNone(lib.handle_hook(payload, "codex"))

    def test_document_audit_requires_real_provider_and_pm_request(self) -> None:
        fake_home = self.fixture.root / "home"
        with mock.patch.object(Path, "home", return_value=fake_home):
            self.assertIsNotNone(lib.register_runtime_role("codex", "wrong", self.fixture.root, self.fixture.root, "auditor-claude", self.fixture.task))
            code, _, _ = self.invoke("role-start", "--role", "auditor-codex", "--task-dir", str(self.fixture.task))
            self.assertNotEqual(code, 0)
            self.assertIsNone(lib.register_runtime_role("codex", "audit", self.fixture.root, self.fixture.root, "auditor-codex", self.fixture.task))
            code, _, _ = self.invoke("role-start", "--role", "auditor-codex", "--task-dir", str(self.fixture.task))
            self.assertNotEqual(code, 0)
            (self.fixture.task / "audit-request.md").write_text("PMの監査依頼。境界は監査担当が確認する。\n", encoding="utf-8")
            code, _, error = self.invoke("role-start", "--role", "auditor-codex", "--task-dir", str(self.fixture.task))
            self.assertEqual(code, 0, error)
            self.assertIsNone(lib.current_state(lib.load_events(self.fixture.task)))


if __name__ == "__main__":
    unittest.main()
