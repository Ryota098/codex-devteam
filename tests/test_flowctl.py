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
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))

import flowctl  # noqa: E402
import flowctl_lib as lib  # noqa: E402


VALID_SCOPE = """# 承認済みスコープ基準

## 承認対象

| 要求ID | 承認済みの外部成果 | 変更可能パス | 許可するリスク領域 | リスク区分 | 変更上限 |
| --- | --- | --- | --- | --- | --- |
| 要求1 | 利用者がプロフィール名を安全に更新できる | `src/**`<br>`tests/**` | 入力検証 | 標準 | 10ファイル / 500行 |

## 明示的な対象外

- 通知、課金、認証方式、DB schemaは変更しない。
"""


VALID_INSTRUCTION = """# 実装指示書

- リスク区分: 標準
- 主要要求ID: 要求1
- 主要な外部挙動数: 1
- 主要な外部挙動: 利用者がプロフィール名を安全に更新できる
- 不可逆境界数: 0
- 不可逆境界: なし
- リスク領域: 入力検証
- 実装前内部検証: 不要
- 実装前内部検証の理由: 既存方式内の局所変更

## 実装担当の変更許可パス

- `src/**`
- `tests/**`

## 受け入れ条件

| 受け入れ条件 | 外部から観測できる期待結果 | 検証方法 |
| --- | --- | --- |
| 条件1（正常な名前更新） | 正常な名前を保存して返す | profile testを実行する |
"""


VALID_SUMMARY = """[TITLE] プロフィール名更新
[PUBLIC API] 更新API
[RULES] 入力検証
[BRANCHES] 正常と拒否
[ERRORS] 不正入力を拒否
[ASSUMPTIONS] なし
[DEVIATION] なし
"""


VALID_LOOP = """# 実装ループ

## 内部検証証跡

- 実施要否: 必須
- 実施方式: 別コンテキストのサブエージェント
- 起動回数: 1
- 起動記録: evaluator-1
- 検証対象: 現在の候補差分
- 最終判定: 合格
- 修正票: なし
- 対応結果: 修正票なし
- 合格後の実装・テスト・設定・自動生成物変更: なし

### 受け入れ条件別判定

| 受け入れ条件 | 判定 | 根拠 |
| --- | --- | --- |
| 条件1（正常な名前更新） | 合格 | profile test |
"""


VALID_REPORT = """# 実装報告

## 正式ドキュメント影響

- 実装担当による正式ドキュメント変更: なし
- PM更新候補: なし
- 更新不要の理由: 公開契約と利用手順は変わらないため

## 受け入れ条件と検証証拠

| 受け入れ条件 | 実装箇所 | 検証証拠 | 結果 |
| --- | --- | --- | --- |
| 条件1（正常な名前更新） | src/profile.py | tests/test_profile.py | 成功 |
"""


class RepoFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.flow = self.root / "docs" / "flow" / "profile"
        self.task = self.flow / "task-01"
        self.scope = self.flow / "scope-baseline.md"

    def close(self) -> None:
        self.temporary.cleanup()

    def git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self.root, text=True, capture_output=True, check=True
        )
        return result.stdout.strip()

    def setup(self) -> str:
        self.git("init", "-b", "feature/profile")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "flowctl test")
        (self.root / "AGENTS.md").write_text(lib.MANAGED_MARKER + "\n", encoding="utf-8")
        (self.root / ".gitignore").write_text("/AGENTS.md\n/docs/flow/\n", encoding="utf-8")
        (self.root / "src").mkdir()
        (self.root / "tests").mkdir()
        (self.root / "src" / "profile.py").write_text("NAME = 'before'\n", encoding="utf-8")
        (self.root / "tests" / "test_profile.py").write_text("def test_profile():\n    assert True\n", encoding="utf-8")
        self.flow.mkdir(parents=True)
        self.task.mkdir()
        self.scope.write_text(VALID_SCOPE, encoding="utf-8")
        (self.task / "instruction.md").write_text(VALID_INSTRUCTION, encoding="utf-8")
        self.git("add", ".gitignore", "src", "tests")
        self.git("commit", "-m", "initial")
        return self.git("rev-parse", "HEAD")


class FlowctlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = RepoFixture()
        self.base = self.fixture.setup()
        self.old_cwd = Path.cwd()
        os.chdir(self.fixture.root)

    def tearDown(self) -> None:
        os.chdir(self.old_cwd)
        self.fixture.close()

    def invoke(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = flowctl.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def lock_and_init(self, *, audits: int = 2, extra: tuple[str, ...] = ()) -> None:
        lock_args = [
            "scope-lock",
            "--scope-file",
            str(self.fixture.scope),
            "--audits",
            str(audits),
            "--owner-confirmed",
        ]
        if audits == 1:
            lock_args.extend(("--single-auditor", "codex"))
        code, _, error = self.invoke(
            *lock_args,
        )
        self.assertEqual(code, 0, error)
        arguments = [
            "init",
            "--task-dir",
            str(self.fixture.task),
            "--scope-file",
            str(self.fixture.scope),
            "--scope-id",
            "要求1",
            "--risk",
            "standard",
            "--audits",
            str(audits),
            "--branch",
            "feature/profile",
            "--base",
            self.base,
            "--tl",
            "not-required",
            "--pre-evaluator",
            "not-required",
            *extra,
        ]
        code, _, error = self.invoke(*arguments)
        self.assertEqual(code, 0, error)

    def make_handoff(self) -> None:
        (self.fixture.task / "summary.md").write_text(VALID_SUMMARY, encoding="utf-8")
        (self.fixture.task / "loop-state.md").write_text(VALID_LOOP, encoding="utf-8")
        (self.fixture.task / "report.md").write_text(VALID_REPORT, encoding="utf-8")

    def test_one_audit_must_be_fixed_by_owner_scope_lock(self) -> None:
        code, _, error = self.invoke(
            "scope-lock", "--scope-file", str(self.fixture.scope), "--owner-confirmed"
        )
        self.assertEqual(code, 0, error)
        code, _, error = self.invoke(
            "init",
            "--task-dir",
            str(self.fixture.task),
            "--scope-file",
            str(self.fixture.scope),
            "--scope-id",
            "要求1",
            "--risk",
            "standard",
            "--audits",
            "1",
            "--single-auditor",
            "codex",
            "--branch",
            "feature/profile",
            "--base",
            self.base,
            "--tl",
            "not-required",
            "--pre-evaluator",
            "not-required",
        )
        self.assertEqual(code, 1)
        self.assertIn("scope-lock時に固定", error)

    def test_temporary_capability_is_bound_to_role(self) -> None:
        self.lock_and_init()
        self.assertEqual(
            self.invoke(
                "approve",
                "--task-dir",
                str(self.fixture.task),
                "--capability",
                "isolated-db",
                "--minutes",
                "10",
                "--reason",
                "破棄可能な専用DBで結合テスト",
                "--owner-confirmed",
            )[0],
            0,
        )
        self.assertIn("isolated-db", lib.current_capabilities(self.fixture.task, "implementer"))
        self.assertNotIn("isolated-db", lib.current_capabilities(self.fixture.task, "pm"))

    def test_scope_lock_detects_post_approval_change(self) -> None:
        code, _, error = self.invoke(
            "scope-lock", "--scope-file", str(self.fixture.scope), "--owner-confirmed"
        )
        self.assertEqual(code, 0, error)
        self.fixture.scope.write_text(VALID_SCOPE.replace("入力検証", "入力検証、Slack通知"), encoding="utf-8")
        with self.assertRaises(lib.FlowError):
            lib.validate_scope_lock(self.fixture.scope)

    def test_scope_lock_cannot_change_audit_policy_without_unlock(self) -> None:
        self.assertEqual(
            self.invoke(
                "scope-lock",
                "--scope-file",
                str(self.fixture.scope),
                "--audits",
                "2",
                "--owner-confirmed",
            )[0],
            0,
        )
        code, _, error = self.invoke(
            "scope-lock",
            "--scope-file",
            str(self.fixture.scope),
            "--audits",
            "1",
            "--single-auditor",
            "codex",
            "--owner-confirmed",
        )
        self.assertEqual(code, 1)
        self.assertIn("監査数・監査担当も固定中", error)

    def test_init_cannot_raise_owner_locked_risk(self) -> None:
        self.assertEqual(
            self.invoke(
                "scope-lock",
                "--scope-file",
                str(self.fixture.scope),
                "--owner-confirmed",
            )[0],
            0,
        )
        code, _, error = self.invoke(
            "init",
            "--task-dir",
            str(self.fixture.task),
            "--scope-file",
            str(self.fixture.scope),
            "--scope-id",
            "要求1",
            "--risk",
            "high",
            "--branch",
            "feature/profile",
            "--base",
            self.base,
            "--tl",
            "required",
            "--tl-reason",
            "不可逆境界を確認",
            "--pre-evaluator",
            "required",
        )
        self.assertEqual(code, 1)
        self.assertIn("リスク区分はオーナー", error)

    def test_owner_can_adopt_existing_task_at_safe_state(self) -> None:
        self.assertEqual(
            self.invoke(
                "scope-lock",
                "--scope-file",
                str(self.fixture.scope),
                "--owner-confirmed",
            )[0],
            0,
        )
        code, _, error = self.invoke(
            "adopt",
            "--task-dir",
            str(self.fixture.task),
            "--scope-file",
            str(self.fixture.scope),
            "--scope-id",
            "要求1",
            "--risk",
            "standard",
            "--branch",
            "feature/profile",
            "--base",
            self.base,
            "--state",
            "instruction_ready",
            "--pre-evaluator",
            "not-required",
            "--reason",
            "既存taskを安全側から移行",
            "--owner-confirmed",
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(lib.current_state(lib.load_events(self.fixture.task)), "instruction_ready")
        self.assertTrue(lib.load_policy(self.fixture.task)["adopted"])

    def test_instruction_cannot_expand_scope_paths_or_outcome(self) -> None:
        self.lock_and_init()
        expanded = VALID_INSTRUCTION.replace("- `tests/**`", "- `tests/**`\n- `integrations/slack/**`")
        (self.fixture.task / "instruction.md").write_text(expanded, encoding="utf-8")
        code, _, error = self.invoke("instruction-ready", "--task-dir", str(self.fixture.task))
        self.assertEqual(code, 1)
        self.assertIn("変更可能パスを越えています", error)

        (self.fixture.task / "instruction.md").write_text(
            VALID_INSTRUCTION.replace(
                "利用者がプロフィール名を安全に更新できる",
                "プロフィールを更新してSlack通知も送る",
            ),
            encoding="utf-8",
        )
        code, _, error = self.invoke("instruction-ready", "--task-dir", str(self.fixture.task))
        self.assertEqual(code, 1)
        self.assertIn("承認済み外部成果と同一文", error)

    def test_submit_rejects_owner_locked_change_budget_overrun(self) -> None:
        self.fixture.scope.write_text(
            VALID_SCOPE.replace("10ファイル / 500行", "1ファイル / 1行"),
            encoding="utf-8",
        )
        self.lock_and_init()
        self.assertEqual(self.invoke("instruction-ready", "--task-dir", str(self.fixture.task))[0], 0)
        self.assertEqual(
            self.invoke("role-start", "--role", "implementer", "--task-dir", str(self.fixture.task))[0],
            0,
        )
        (self.fixture.task / "pre-summary.md").write_text("# 実装前サマリ\n", encoding="utf-8")
        self.assertEqual(
            self.invoke("start-approve", "--task-dir", str(self.fixture.task), "--owner-confirmed")[0],
            0,
        )
        self.make_handoff()
        (self.fixture.root / "src" / "profile.py").write_text("NAME = 'after'\nMORE = True\n", encoding="utf-8")
        code, _, error = self.invoke("submit", "--task-dir", str(self.fixture.task))
        self.assertEqual(code, 1)
        self.assertIn("変更上限", error)

    def test_feedback_question_continues_but_scope_change_pauses(self) -> None:
        self.lock_and_init()
        self.assertEqual(
            self.invoke("instruction-ready", "--task-dir", str(self.fixture.task))[0], 0
        )
        self.assertEqual(
            self.invoke(
                "role-start", "--role", "implementer", "--task-dir", str(self.fixture.task)
            )[0],
            0,
        )
        (self.fixture.task / "pre-summary.md").write_text("# 実装前サマリ\n", encoding="utf-8")
        self.assertEqual(
            self.invoke(
                "start-approve", "--task-dir", str(self.fixture.task), "--owner-confirmed"
            )[0],
            0,
        )
        self.assertEqual(
            self.invoke(
                "feedback",
                "--task-dir",
                str(self.fixture.task),
                "--kind",
                "question",
                "--summary",
                "境界値の確認",
            )[0],
            0,
        )
        self.assertEqual(lib.current_state(lib.load_events(self.fixture.task)), "implementation")
        self.assertEqual(
            self.invoke(
                "feedback",
                "--task-dir",
                str(self.fixture.task),
                "--kind",
                "scope-change",
                "--summary",
                "外部成果の追加候補",
            )[0],
            0,
        )
        self.assertEqual(lib.current_state(lib.load_events(self.fixture.task)), "implementation_paused")
        code, _, error = self.invoke("resume", "--task-dir", str(self.fixture.task))
        self.assertEqual(code, 1)
        self.assertIn("PMがinstruction-ready", error)

    def test_scope_change_reapproval_returns_through_preflight(self) -> None:
        self.lock_and_init()
        self.assertEqual(self.invoke("instruction-ready", "--task-dir", str(self.fixture.task))[0], 0)
        self.assertEqual(
            self.invoke("role-start", "--role", "implementer", "--task-dir", str(self.fixture.task))[0],
            0,
        )
        pre_summary = self.fixture.task / "pre-summary.md"
        pre_summary.write_text("# 実装前サマリ\n\n初回\n", encoding="utf-8")
        self.assertEqual(
            self.invoke("start-approve", "--task-dir", str(self.fixture.task), "--owner-confirmed")[0],
            0,
        )
        self.assertEqual(
            self.invoke(
                "feedback",
                "--task-dir",
                str(self.fixture.task),
                "--kind",
                "scope-change",
                "--summary",
                "検証用ファイルを追加する必要がある",
            )[0],
            0,
        )
        updated_instruction = VALID_INSTRUCTION.replace(
            "既存方式内の局所変更", "再承認された検証範囲内の局所変更"
        )
        (self.fixture.task / "instruction.md").write_text(updated_instruction, encoding="utf-8")
        code, _, error = self.invoke("instruction-ready", "--task-dir", str(self.fixture.task))
        self.assertEqual(code, 1)
        self.assertIn("再固定", error)

        self.assertEqual(
            self.invoke(
                "scope-unlock",
                "--scope-file",
                str(self.fixture.scope),
                "--reason",
                "検証範囲を再承認するため",
                "--owner-confirmed",
            )[0],
            0,
        )
        self.fixture.scope.write_text(
            VALID_SCOPE.replace("10ファイル / 500行", "11ファイル / 600行"),
            encoding="utf-8",
        )
        self.assertEqual(
            self.invoke(
                "scope-lock",
                "--scope-file",
                str(self.fixture.scope),
                "--audits",
                "2",
                "--owner-confirmed",
            )[0],
            0,
        )
        self.assertEqual(self.invoke("instruction-ready", "--task-dir", str(self.fixture.task))[0], 0)
        self.assertEqual(
            self.invoke("role-start", "--role", "implementer", "--task-dir", str(self.fixture.task))[0],
            0,
        )
        code, _, error = self.invoke(
            "start-approve", "--task-dir", str(self.fixture.task), "--owner-confirmed"
        )
        self.assertEqual(code, 1)
        self.assertIn("pre-summary.mdを更新", error)
        pre_summary.write_text("# 実装前サマリ\n\n再承認範囲を確認済み\n", encoding="utf-8")
        self.assertEqual(
            self.invoke("start-approve", "--task-dir", str(self.fixture.task), "--owner-confirmed")[0],
            0,
        )
        self.assertEqual(lib.current_state(lib.load_events(self.fixture.task)), "implementation")

    def test_mid_implementation_tl_review_returns_to_same_task(self) -> None:
        self.lock_and_init()
        self.assertEqual(self.invoke("instruction-ready", "--task-dir", str(self.fixture.task))[0], 0)
        self.assertEqual(
            self.invoke("role-start", "--role", "implementer", "--task-dir", str(self.fixture.task))[0],
            0,
        )
        pre_summary = self.fixture.task / "pre-summary.md"
        pre_summary.write_text("# 実装前サマリ\n\n初回\n", encoding="utf-8")
        self.assertEqual(
            self.invoke("start-approve", "--task-dir", str(self.fixture.task), "--owner-confirmed")[0],
            0,
        )
        self.assertEqual(
            self.invoke(
                "feedback",
                "--task-dir",
                str(self.fixture.task),
                "--kind",
                "tl-review",
                "--summary",
                "既存の信頼境界に関する判断が不足",
            )[0],
            0,
        )
        tech_lead = self.fixture.flow / "tech-lead"
        tech_lead.mkdir()
        consultation = tech_lead / "trust-boundary.md"
        consultation.write_text("# Tech Lead相談\n", encoding="utf-8")
        self.assertEqual(
            self.invoke(
                "tl-request",
                "--task-dir",
                str(self.fixture.task),
                "--consultation-file",
                str(consultation),
                "--summary",
                "既存の信頼境界との整合判断",
            )[0],
            0,
        )
        decision = tech_lead / "trust-boundary-decision.md"
        decision.write_text("# Tech Lead判断\n\n既存境界を維持する。\n", encoding="utf-8")
        self.assertEqual(
            self.invoke(
                "tl-complete",
                "--task-dir",
                str(self.fixture.task),
                "--decision-file",
                str(decision),
            )[0],
            0,
        )
        self.assertEqual(lib.current_state(lib.load_events(self.fixture.task)), "implementation_paused")
        (self.fixture.task / "instruction.md").write_text(
            VALID_INSTRUCTION.replace("既存方式内の局所変更", "Tech Lead判断済みの既存境界を維持"),
            encoding="utf-8",
        )
        self.assertEqual(self.invoke("instruction-ready", "--task-dir", str(self.fixture.task))[0], 0)
        self.assertEqual(
            self.invoke("role-start", "--role", "implementer", "--task-dir", str(self.fixture.task))[0],
            0,
        )
        pre_summary.write_text("# 実装前サマリ\n\nTech Lead判断を反映済み\n", encoding="utf-8")
        self.assertEqual(
            self.invoke("start-approve", "--task-dir", str(self.fixture.task), "--owner-confirmed")[0],
            0,
        )

    def test_initial_tl_requires_registered_consultation(self) -> None:
        self.assertEqual(
            self.invoke(
                "scope-lock",
                "--scope-file",
                str(self.fixture.scope),
                "--owner-confirmed",
            )[0],
            0,
        )
        self.assertEqual(
            self.invoke(
                "init",
                "--task-dir",
                str(self.fixture.task),
                "--scope-file",
                str(self.fixture.scope),
                "--scope-id",
                "要求1",
                "--risk",
                "standard",
                "--branch",
                "feature/profile",
                "--base",
                self.base,
                "--tl",
                "required",
                "--tl-reason",
                "入力境界の上流判断",
                "--pre-evaluator",
                "not-required",
            )[0],
            0,
        )
        code, _, error = self.invoke(
            "role-start", "--role", "tl", "--task-dir", str(self.fixture.task)
        )
        self.assertEqual(code, 1)
        self.assertIn("相談資料", error)
        code, output, error = self.invoke(
            "next", "--task-dir", str(self.fixture.task), "--provider", "codex"
        )
        self.assertEqual(code, 0, error)
        self.assertIn("tl-request", output)
        tech_lead = self.fixture.flow / "tech-lead"
        tech_lead.mkdir()
        consultation = tech_lead / "input-boundary.md"
        consultation.write_text("# Tech Lead相談\n", encoding="utf-8")
        self.assertEqual(
            self.invoke(
                "tl-request",
                "--task-dir",
                str(self.fixture.task),
                "--consultation-file",
                str(consultation),
                "--summary",
                "入力境界を決める",
            )[0],
            0,
        )
        code, output, error = self.invoke(
            "next", "--task-dir", str(self.fixture.task), "--provider", "codex"
        )
        self.assertEqual(code, 0, error)
        self.assertIn("$tl", output)
        self.assertIn(str(consultation), output)
        self.assertEqual(
            self.invoke("role-start", "--role", "tl", "--task-dir", str(self.fixture.task))[0],
            0,
        )
        decision = tech_lead / "input-boundary-decision.md"
        decision.write_text("# Tech Lead判断\n", encoding="utf-8")
        self.assertEqual(
            self.invoke(
                "tl-complete",
                "--task-dir",
                str(self.fixture.task),
                "--decision-file",
                str(decision),
            )[0],
            0,
        )
        self.assertEqual(lib.current_state(lib.load_events(self.fixture.task)), "planning")

    def test_commit_must_match_pm_accepted_candidate(self) -> None:
        self.lock_and_init()
        self.assertEqual(self.invoke("instruction-ready", "--task-dir", str(self.fixture.task))[0], 0)
        self.assertEqual(
            self.invoke("role-start", "--role", "implementer", "--task-dir", str(self.fixture.task))[0],
            0,
        )
        (self.fixture.task / "pre-summary.md").write_text("# 実装前サマリ\n", encoding="utf-8")
        self.assertEqual(
            self.invoke("start-approve", "--task-dir", str(self.fixture.task), "--owner-confirmed")[0],
            0,
        )
        self.make_handoff()
        target = self.fixture.root / "src" / "profile.py"
        target.write_text("NAME = 'accepted'\n", encoding="utf-8")
        self.assertEqual(self.invoke("submit", "--task-dir", str(self.fixture.task))[0], 0)
        (self.fixture.task / "implementation-review.md").write_text("# PM確認\n", encoding="utf-8")
        self.assertEqual(
            self.invoke("pm-review", "--task-dir", str(self.fixture.task), "--result", "accept")[0],
            0,
        )
        target.write_text("NAME = 'changed-after-accept'\n", encoding="utf-8")
        self.fixture.git("add", "src/profile.py")
        self.fixture.git("commit", "-m", "changed candidate")
        head = self.fixture.git("rev-parse", "HEAD")
        code, _, error = self.invoke(
            "commit-recorded", "--task-dir", str(self.fixture.task), "--head", head
        )
        self.assertEqual(code, 1)
        self.assertIn("PM承認済み候補差分と一致しません", error)

    def test_pm_cannot_accept_formal_doc_outside_locked_paths(self) -> None:
        self.lock_and_init()
        self.assertEqual(self.invoke("instruction-ready", "--task-dir", str(self.fixture.task))[0], 0)
        self.assertEqual(
            self.invoke("role-start", "--role", "implementer", "--task-dir", str(self.fixture.task))[0],
            0,
        )
        (self.fixture.task / "pre-summary.md").write_text("# 実装前サマリ\n", encoding="utf-8")
        self.assertEqual(
            self.invoke("start-approve", "--task-dir", str(self.fixture.task), "--owner-confirmed")[0],
            0,
        )
        self.make_handoff()
        (self.fixture.root / "src" / "profile.py").write_text("NAME = 'after'\n", encoding="utf-8")
        self.assertEqual(self.invoke("submit", "--task-dir", str(self.fixture.task))[0], 0)
        (self.fixture.task / "implementation-review.md").write_text("# PM確認\n", encoding="utf-8")
        (self.fixture.root / "README.md").write_text("# 未承認の正式文書\n", encoding="utf-8")
        code, _, error = self.invoke(
            "pm-review", "--task-dir", str(self.fixture.task), "--result", "accept"
        )
        self.assertEqual(code, 1)
        self.assertIn("変更パス外の正式ドキュメント", error)

    def test_complete_two_audit_lifecycle_and_metrics(self) -> None:
        self.lock_and_init()
        self.assertEqual(self.invoke("instruction-ready", "--task-dir", str(self.fixture.task))[0], 0)
        self.assertEqual(
            self.invoke("role-start", "--role", "implementer", "--task-dir", str(self.fixture.task))[0],
            0,
        )
        (self.fixture.task / "pre-summary.md").write_text("# 実装前サマリ\n", encoding="utf-8")
        self.assertEqual(
            self.invoke(
                "start-approve", "--task-dir", str(self.fixture.task), "--owner-confirmed"
            )[0],
            0,
        )
        self.make_handoff()
        (self.fixture.root / "src" / "profile.py").write_text("NAME = 'after'\n", encoding="utf-8")
        code, _, error = self.invoke("submit", "--task-dir", str(self.fixture.task))
        self.assertEqual(code, 0, error)
        (self.fixture.task / "implementation-review.md").write_text("# PM確認\n\n判定: コミット可\n", encoding="utf-8")
        code, _, error = self.invoke(
            "pm-review", "--task-dir", str(self.fixture.task), "--result", "accept"
        )
        self.assertEqual(code, 0, error)
        self.fixture.git("add", "src/profile.py")
        self.fixture.git("commit", "-m", "update profile")
        head = self.fixture.git("rev-parse", "HEAD")
        code, _, error = self.invoke(
            "commit-recorded", "--task-dir", str(self.fixture.task), "--head", head
        )
        self.assertEqual(code, 0, error)
        (self.fixture.task / "audit-request.md").write_text(
            "\n".join(
                (
                    "# 監査依頼",
                    "",
                    self.base,
                    head,
                    f"git diff {self.base}..{head}",
                    "report.md summary.md loop-state.md implementation-review.md",
                    "src/profile.py",
                    "audit-codex.md audit-claude.md",
                    "",
                )
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.invoke("audit-ready", "--task-dir", str(self.fixture.task))[0], 0)

        for auditor in ("codex", "claude"):
            code, _, error = self.invoke(
                "audit-start", "--task-dir", str(self.fixture.task), "--auditor", auditor
            )
            self.assertEqual(code, 0, error)
            result_file = self.fixture.task / f"audit-{auditor}.md"
            result_file.write_text("# 監査\n\n監査結果: クローズ可\n", encoding="utf-8")
            if auditor == "codex":
                wrong_file = self.fixture.task / "wrong-audit.md"
                wrong_file.write_text("# 監査\n\n監査結果: クローズ可\n", encoding="utf-8")
                code, _, error = self.invoke(
                    "audit-result",
                    "--task-dir",
                    str(self.fixture.task),
                    "--auditor",
                    auditor,
                    "--file",
                    str(wrong_file),
                )
                self.assertEqual(code, 1)
                self.assertIn("指定task", error)
                request = self.fixture.task / "audit-request.md"
                original_request = request.read_text(encoding="utf-8")
                request.write_text(original_request + "\n変更\n", encoding="utf-8")
                code, _, error = self.invoke(
                    "audit-result",
                    "--task-dir",
                    str(self.fixture.task),
                    "--auditor",
                    auditor,
                    "--file",
                    str(result_file),
                )
                self.assertEqual(code, 1)
                self.assertIn("audit-request.mdが変更", error)
                request.write_text(original_request, encoding="utf-8")
            code, _, error = self.invoke(
                "audit-result",
                "--task-dir",
                str(self.fixture.task),
                "--auditor",
                auditor,
                "--file",
                str(result_file),
            )
            self.assertEqual(code, 0, error)
        self.assertEqual(lib.current_state(lib.load_events(self.fixture.task)), "audit_triage")
        (self.fixture.task / "audit-triage.md").write_text("# 監査整理\n\n今すぐ直すべきもの: なし\n", encoding="utf-8")
        self.assertEqual(
            self.invoke(
                "triage",
                "--task-dir",
                str(self.fixture.task),
                "--result",
                "recommend-close",
            )[0],
            0,
        )
        self.assertEqual(
            self.invoke("close", "--task-dir", str(self.fixture.task), "--owner-confirmed")[0],
            0,
        )
        metrics = lib.calculate_metrics(self.fixture.task)
        self.assertTrue(metrics["first_audit_pass"])
        self.assertEqual(metrics["pm_returns"], 0)
        self.assertGreaterEqual(metrics["session_count"], 1)


class GuardTest(unittest.TestCase):
    def test_role_write_ownership(self) -> None:
        policy = {"allowed_write_globs": ["src/**", "tests/**"], "formal_doc_globs": [], "generated_doc_globs": []}
        self.assertIsNone(lib.check_write_path("implementer", "src/app.py", policy))
        self.assertIn("PM所有", lib.check_write_path("implementer", "README.md", policy) or "")
        self.assertIsNone(lib.check_write_path("pm", "README.md", policy))
        self.assertIn("プロダクトコード", lib.check_write_path("pm", "src/app.py", policy) or "")
        self.assertIsNone(lib.check_write_path("auditor-codex", "docs/flow/x/task-01/audit-codex.md", policy))
        self.assertIsNotNone(lib.check_write_path("auditor-codex", "docs/flow/x/task-01/report.md", policy))
        self.assertIn("品質ゲート", lib.check_write_path("implementer", "src/app.py", {"allowed_write_globs": []}) or "")

    def test_write_guard_is_bound_to_project_and_associated_task(self) -> None:
        fixture = RepoFixture()
        fixture.setup()
        policy = {
            "allowed_write_globs": ["src/**", "tests/**"],
            "scope_requirement": {"write_globs": ["src/**", "tests/**"]},
            "formal_doc_globs": [],
            "generated_doc_globs": [],
        }
        self.assertIn(
            "プロジェクト外",
            lib.check_write_path(
                "pm", "/private/tmp/README.md", policy, fixture.root, fixture.task
            )
            or "",
        )
        self.assertIn(
            "関連付けたtask",
            lib.check_write_path(
                "implementer",
                "docs/flow/profile/task-02/report.md",
                policy,
                fixture.root,
                fixture.task,
            )
            or "",
        )
        self.assertIn(
            "関連付けたtask",
            lib.check_write_path(
                "auditor-codex",
                "docs/flow/profile/task-02/audit-codex.md",
                policy,
                fixture.root,
                fixture.task,
            )
            or "",
        )
        self.assertIsNone(
            lib.check_write_path(
                "pm", "docs/flow/profile/spec.md", policy, fixture.root, fixture.task
            )
        )
        self.assertIn(
            "固定済み変更パス外",
            lib.check_write_path("pm", "README.md", policy, fixture.root, fixture.task) or "",
        )
        fixture.close()

    def test_role_state_and_flowctl_command_guards(self) -> None:
        fixture = RepoFixture()
        base = fixture.setup()
        scope_lock = {
            "schema_version": 1,
            "active": True,
            "scope_file": "docs/flow/profile/scope-baseline.md",
            "sha256": lib.sha256_file(fixture.scope),
            "requirements": lib.parse_scope_baseline(fixture.scope)[0],
            "audit_count": 2,
        }
        lib.atomic_write_json(lib.scope_lock_path(fixture.scope), scope_lock)
        policy = {
            "schema_version": 1,
            "branch": "feature/profile",
            "base_commit": base,
            "allowed_write_globs": ["src/**"],
        }
        lib.task_meta_dir(fixture.task).mkdir(parents=True)
        lib.save_policy(fixture.task, policy)
        lib.append_event(fixture.task, "transition", role="pm", data={"from": None, "to": "implementation_preflight"})
        self.assertIn(
            "承認前",
            lib.check_role_write_state("implementer", fixture.task, "src/profile.py") or "",
        )
        self.assertIsNone(
            lib.check_role_write_state(
                "implementer", fixture.task, "docs/flow/profile/task-01/pre-summary.md"
            )
        )
        self.assertEqual(lib.parse_flowctl_command("~/.ai-devteam/bin/flowctl pm-review --task-dir x"), "pm-review")
        self.assertNotIn("pm-review", lib.ROLE_FLOWCTL_COMMANDS["implementer"])
        fixture.close()

    def test_command_guardrails(self) -> None:
        self.assertIsNone(lib.check_bash_command("git commit -m normal", None, set()))
        self.assertIsNone(lib.check_write_path(None, "README.md"))
        self.assertIsNone(lib.check_external_tool("spawn_agent", None))
        self.assertIn("オーナー", lib.check_bash_command("git commit -m test", "implementer", set()) or "")
        self.assertIn("秘密情報", lib.check_bash_command("sed -n '1p' .env", "implementer", set()) or "")
        self.assertIn("一時許可", lib.check_bash_command("npx prisma migrate dev", "implementer", set()) or "")
        self.assertIsNone(
            lib.check_bash_command(
                "npx prisma migrate dev", "implementer", {"isolated-db", "migration"}
            )
        )
        self.assertNotIn("close", lib.ROLE_FLOWCTL_COMMANDS["pm"])
        self.assertIsNone(lib.check_bash_command("rg 'value > limit' src", "implementer", set()))
        self.assertIn(
            "直接ファイル変更",
            lib.check_bash_command("printf value > output.txt", "implementer", set()) or "",
        )
        self.assertIn(
            "直接ファイル変更",
            lib.check_bash_command("mkdir generated", "implementer", set()) or "",
        )
        self.assertIn(
            "git変更操作",
            lib.check_bash_command("env git commit -m test", "implementer", set()) or "",
        )
        self.assertIn(
            "インラインスクリプト",
            lib.check_bash_command("python3 -B -c 'print(1)'", "implementer", set()) or "",
        )

    def test_dependency_and_migration_writes_need_temporary_capability(self) -> None:
        self.assertIn(
            "dependency-install",
            lib.check_capability_write("implementer", "package.json", set()) or "",
        )
        self.assertIsNone(
            lib.check_capability_write("implementer", "package.json", {"dependency-install"})
        )
        self.assertIn(
            "migration",
            lib.check_capability_write(
                "implementer", "prisma/migrations/001_init/migration.sql", set()
            )
            or "",
        )
        self.assertIsNone(
            lib.check_capability_write(
                "implementer",
                "prisma/migrations/001_init/migration.sql",
                {"migration"},
            )
        )

    def test_auditor_hook_cannot_register_the_other_auditor(self) -> None:
        fixture = RepoFixture()
        base = fixture.setup()
        policy = {
            "schema_version": 1,
            "branch": "feature/profile",
            "base_commit": base,
            "allowed_write_globs": ["src/**"],
        }
        lib.task_meta_dir(fixture.task).mkdir(parents=True)
        lib.save_policy(fixture.task, policy)
        fake_home = fixture.root / "home"
        with mock.patch.object(Path, "home", return_value=fake_home):
            lib.save_runtime_session(
                "codex",
                "audit-session",
                {
                    "schema_version": 1,
                    "provider": "codex",
                    "session_id": "audit-session",
                    "root": str(fixture.root),
                    "role": "auditor-codex",
                    "task_dir": str(fixture.task),
                    "started_at": lib.iso_now(),
                    "span_id": "span",
                    "event_recorded": False,
                },
            )
            result = lib.handle_hook(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": "audit-session",
                    "cwd": str(fixture.root),
                    "tool_name": "Bash",
                    "tool_input": {
                        "command": (
                            f"{fake_home}/.ai-devteam/bin/flowctl audit-result "
                            f"--task-dir {fixture.task} --auditor claude --file audit-claude.md"
                        )
                    },
                },
                "codex",
            )
            self.assertEqual(result["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn("codex監査だけ", result["hookSpecificOutput"]["permissionDecisionReason"])
        fixture.close()

    def test_hook_is_inactive_until_explicit_role_start(self) -> None:
        fixture = RepoFixture()
        fixture.setup()
        fake_home = fixture.root / "home"
        payload_start = {
            "hook_event_name": "SessionStart",
            "session_id": "session-1",
            "cwd": str(fixture.root),
        }
        with mock.patch.object(Path, "home", return_value=fake_home):
            self.assertIsNone(lib.handle_hook(payload_start, "codex"))
            self.assertFalse(lib.runtime_session_path("codex", "session-1").exists())
            self.assertIsNone(
                lib.handle_hook(
                    {
                        "hook_event_name": "PreToolUse",
                        "session_id": "session-1",
                        "cwd": str(fixture.root),
                        "tool_name": "apply_patch",
                        "tool_input": {
                            "command": "*** Begin Patch\n*** Update File: src/profile.py\n*** End Patch"
                        },
                    },
                    "codex",
                )
            )
            self.assertIsNone(
                lib.handle_hook(
                    {
                        "hook_event_name": "PreToolUse",
                        "session_id": "session-1",
                        "cwd": str(fixture.root),
                        "tool_name": "Bash",
                        "tool_input": {"command": "git commit -m normal-session"},
                    },
                    "codex",
                )
            )
            self.assertIsNone(
                lib.handle_hook(
                    {
                        "hook_event_name": "PreToolUse",
                        "session_id": "session-1",
                        "cwd": str(fixture.root),
                        "tool_name": "Bash",
                        "tool_input": {"command": "npm test"},
                    },
                    "codex",
                )
            )
            self.assertIsNone(
                lib.handle_hook(
                    {
                        "hook_event_name": "PreToolUse",
                        "session_id": "session-1",
                        "cwd": str(fixture.root),
                        "tool_name": "spawn_agent",
                        "tool_input": {"task": "通常セッション内の調査"},
                    },
                    "codex",
                )
            )
            owner_operation = lib.handle_hook(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": "session-1",
                    "cwd": str(fixture.root),
                    "tool_name": "Bash",
                    "tool_input": {
                        "command": (
                            f"{fake_home}/.ai-devteam/bin/flowctl close "
                            f"--task-dir {fixture.task} --owner-confirmed"
                        )
                    },
                },
                "codex",
            )
            self.assertEqual(owner_operation["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertIn(
                "オーナー",
                owner_operation["hookSpecificOutput"]["permissionDecisionReason"],
            )
            role_command = f"{fake_home}/.ai-devteam/bin/flowctl role-start --role implementer --task-dir {fixture.task}"
            self.assertIsNone(
                lib.handle_hook(
                    {
                        "hook_event_name": "PreToolUse",
                        "session_id": "session-1",
                        "cwd": str(fixture.root),
                        "tool_name": "Bash",
                        "tool_input": {"command": role_command},
                    },
                    "codex",
                )
            )
            self.assertEqual(
                lib.load_runtime_session("codex", "session-1")["role"], "implementer"
            )
            formal = lib.handle_hook(
                {
                    "hook_event_name": "PreToolUse",
                    "session_id": "session-1",
                    "cwd": str(fixture.root),
                    "tool_name": "apply_patch",
                    "tool_input": {"command": "*** Begin Patch\n*** Update File: README.md\n*** End Patch"},
                },
                "codex",
            )
            self.assertEqual(formal["hookSpecificOutput"]["permissionDecision"], "deny")
        fixture.close()

    def test_skill_metadata_disables_implicit_invocation(self) -> None:
        configs = sorted((REPO / "codex" / "skills").glob("*/agents/openai.yaml"))
        self.assertTrue(configs)
        for config in configs:
            self.assertIn(
                "allow_implicit_invocation: false",
                config.read_text(encoding="utf-8"),
            )
        claude_skills = sorted((REPO / "claude" / "skills").glob("*/SKILL.md"))
        self.assertTrue(claude_skills)
        for skill in claude_skills:
            frontmatter = skill.read_text(encoding="utf-8").split("---", 2)[1]
            self.assertIn("disable-model-invocation: true", frontmatter)

    def test_legacy_claude_guards_are_removed_with_backup(self) -> None:
        self.assertEqual(len(lib.LEGACY_CLAUDE_GIT_DENIES), 27)
        self.assertEqual(len(lib.LEGACY_CLAUDE_GIT_ALLOWS), 7)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ".claude" / "settings.json"
            config.parent.mkdir()
            config.write_text(
                json.dumps(
                    {
                        "_comment": "ai-devteamのプロジェクト用補助ガード。Git変更を拒否する。",
                        "custom": {"preserved": True},
                        "permissions": {
                            "deny": ["Bash(git commit)", "Bash(custom dangerous command:*)"],
                            "allow": ["Bash(git status:*)", "Bash(custom safe command:*)"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                self.assertEqual(
                    flowctl.main(
                        ["remove-legacy-claude-guards", "--project-root", str(root)]
                    ),
                    1,
                )
            self.assertIn("owner-confirmed", stderr.getvalue())
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(
                    flowctl.main(
                        [
                            "remove-legacy-claude-guards",
                            "--project-root",
                            str(root),
                            "--owner-confirmed",
                        ]
                    ),
                    0,
                )
            value = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(value["custom"], {"preserved": True})
            self.assertEqual(
                value["permissions"]["deny"], ["Bash(custom dangerous command:*)"]
            )
            self.assertEqual(
                value["permissions"]["allow"], ["Bash(custom safe command:*)"]
            )
            backups = list(config.parent.glob("settings.json.ai-devteam-opt-in-backup-*"))
            self.assertEqual(len(backups), 1)

    def test_hook_install_preserves_existing_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.json"
            config.write_text(json.dumps({"permissions": {"deny": ["Bash(git push:*)"]}}), encoding="utf-8")
            changed, backup = lib.install_hooks("claude", SCRIPTS / "flowctl.py", config)
            self.assertTrue(changed)
            self.assertIsNotNone(backup)
            value = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(value["permissions"]["deny"], ["Bash(git push:*)"])
            self.assertIn("PreToolUse", value["hooks"])
            changed_again, _ = lib.install_hooks("claude", SCRIPTS / "flowctl.py", config)
            self.assertFalse(changed_again)


if __name__ == "__main__":
    unittest.main()
