#!/usr/bin/env python3
"""独立したAI役割セッション間の工程を検証・記録するCLI。"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Sequence

from flowctl_lib import (
    AUDITORS,
    RISK_LEVELS,
    ROLES,
    FlowError,
    aggregate_metrics,
    append_event,
    audit_results_for_round,
    atomic_write_json,
    calculate_metrics,
    current_audit_round,
    current_state,
    document_policy,
    find_managed_root,
    git_output,
    git_root,
    handle_hook,
    install_hooks,
    iso_now,
    legacy_claude_git_allows,
    legacy_claude_git_denies,
    latest_event,
    load_events,
    load_policy,
    load_runtime_session,
    load_scope_lock,
    metrics_markdown,
    normalize_relative,
    parse_instruction,
    parse_owner_approval_summary,
    parse_scope_baseline,
    policy_path,
    product_diff_digest,
    refresh_derived_files,
    remove_legacy_claude_git_permissions,
    required_auditors,
    runtime_sessions_dir,
    safe_summary,
    save_policy,
    save_runtime_session,
    sha256_text,
    sha256_file,
    snapshot_candidate_changes,
    snapshot_committed_changes,
    snapshot_formal_docs,
    scope_lock_path,
    task_git_diff_files,
    task_lock,
    task_meta_dir,
    transition,
    utc_now,
    validate_implementation_scope,
    validate_pm_formal_scope,
    validate_scope_lock,
)


VERSION = "2.5.0"


def task_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def document_task_root(task_dir: Path) -> Path:
    root = find_managed_root(task_dir)
    if root is None or not task_dir.is_dir():
        raise FlowError("対象リポジトリと既存のtaskディレクトリを確認してください")
    try:
        relative = task_dir.relative_to(root / "docs" / "flow")
    except ValueError as error:
        raise FlowError("task-dirは対象プロジェクトのdocs/flow配下を指定してください") from error
    if not relative.parts:
        raise FlowError("docs/flow直下ではなく対象機能またはtaskを指定してください")
    return root


def discover_flow_documents(directory: Path) -> list[dict[str, Any]]:
    """List handoff entry points only, without traversing products or Git history."""
    root = find_managed_root(directory)
    if root is not None:
        candidates = [root]
    elif (directory / "docs" / "flow").is_dir():
        candidates = [directory]
    elif directory.is_dir():
        candidates = sorted(path for path in directory.iterdir() if path.is_dir() and not path.is_symlink())
    else:
        raise FlowError("project-rootが存在しません")
    result = []
    for candidate in candidates:
        flow = candidate / "docs" / "flow"
        if not flow.is_dir() or flow.is_symlink():
            continue
        for feature in sorted(flow.iterdir()):
            if not feature.is_dir() or feature.is_symlink() or feature.name.startswith("."):
                continue
            tasks = []
            for task in sorted(feature.iterdir()):
                if not task.is_dir() or task.is_symlink() or task.name.startswith(".") or task.name == "tech-lead":
                    continue
                artifacts = sorted(
                    str(path) for path in task.glob("*.md")
                    if not path.is_symlink() and path.is_file()
                    and path.name.startswith(("instruction", "report", "summary", "audit-", "loop-state"))
                )
                if artifacts or policy_path(task).is_file():
                    tasks.append({
                        "task_dir": str(task),
                        "workflow": "managed" if policy_path(task).is_file() else "documents",
                        "recorded_state": current_state(load_events(task)) if policy_path(task).is_file() else None,
                        "artifacts": artifacts,
                    })
            result.append({
                "project_root": str(candidate), "feature_dir": str(feature),
                "entrypoints": [str(feature / name) for name in ("tasks.md", "spec.md", "instruction.md") if (feature / name).is_file()],
                "tasks": tasks,
            })
    return result


def flowctl_command_block(
    subcommand: str,
    options: Sequence[tuple[str, str | Path | int | None]],
) -> str:
    """Render a terminal-safe command without relying on visual line wrapping."""
    rows = []
    for flag, value in options:
        rows.append(flag if value is None else f"{flag} {shlex.quote(str(value))}")
    lines = [f"~/.ai-devteam/bin/flowctl {subcommand}"]
    if rows:
        lines[0] += " \\"
        for index, row in enumerate(rows):
            suffix = " \\" if index < len(rows) - 1 else ""
            lines.append(f"  {row}{suffix}")
    return "```sh\n" + "\n".join(lines) + "\n```"


def ensure_sha(value: str, label: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", value):
        raise FlowError(f"{label}は7〜64桁のGit SHAで指定してください")
    return value.lower()


def validate_branch_and_base(task_dir: Path, branch: str, base: str) -> None:
    root = git_root(task_dir)
    current = git_output(root, "branch", "--show-current").strip()
    if current != branch:
        raise FlowError(f"現在ブランチが不一致です: 現在={current or 'detached'}、指定={branch}")
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{base}^{{commit}}"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise FlowError(f"base commitが存在しません: {base}")


def print_scope_lock_owner_receipt(
    requirements: dict[str, dict[str, Any]],
    owner_summary: dict[str, str],
    audits: int,
    single_auditor: str | None,
) -> None:
    print("オーナー承認記録:")
    for requirement_id, requirement in sorted(requirements.items()):
        print(f"- {requirement_id}: {requirement['outcome']}")
        print(
            "  範囲: "
            + "、".join(requirement["write_globs"])
            + f" / リスク: {requirement['risk_level']} / 上限: "
            + f"{requirement['max_files']}ファイル・{requirement['max_changed_lines']}行"
        )
    print(f"- 範囲・上限の理由: {owner_summary['scope_reason']}")
    print(f"- 分割・移行判断: {owner_summary['transition_decision']}")
    if audits == 1:
        print(f"- 監査: 1件（{single_auditor}）")
    else:
        print("- 監査: 独立2監査（Codex・Claude）")
    print("- この操作で固定するもの: 上記の成果、変更可能パス、リスク、上限、監査数")
    print("- この操作だけでは許可しないこと: Git、DB・migration実行、外部サービス操作。実装はPMの指示書ゲート合格後だけです")


def prepare_scope_lock(
    scope_file_value: str,
    audits: int,
    single_auditor: str | None,
) -> dict[str, Any]:
    """Read-only validation shared by scope-check and scope-lock.

    The PM can run this before asking the owner for a command.  scope-lock calls
    it again so a changed file or lock between the check and approval is never
    accepted on the strength of stale output.
    """
    scope_file = Path(scope_file_value).expanduser().resolve()
    root = find_managed_root(scope_file)
    if root is None:
        raise FlowError("ai-devteam管理対象プロジェクトを特定できません")
    try:
        relative = scope_file.relative_to(root).as_posix()
    except ValueError as error:
        raise FlowError("scope-baseline.mdは管理対象プロジェクト内に置いてください") from error
    if not relative.startswith("docs/flow/") or scope_file.name != "scope-baseline.md":
        raise FlowError("スコープ基準は docs/flow/<機能名>/scope-baseline.md に置いてください")
    requirements, errors = parse_scope_baseline(scope_file)
    if errors:
        raise FlowError("スコープ基準に不備があります:\n- " + "\n- ".join(errors))
    if audits == 1 and single_auditor not in AUDITORS:
        raise FlowError("1監査では --single-auditor codex|claude が必要です")
    if audits == 2 and single_auditor:
        raise FlowError("2監査では --single-auditor を指定しません")
    owner_summary, owner_errors = parse_owner_approval_summary(scope_file)
    path = scope_lock_path(scope_file)
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("active"):
            if existing.get("sha256") != sha256_file(scope_file):
                raise FlowError("既存スコープは固定中です。変更前にscope-unlockが必要です")
            same_audit_policy = (
                int(existing.get("audit_count", 2)) == audits
                and existing.get("single_auditor") == single_auditor
            )
            if not same_audit_policy:
                raise FlowError("監査数・監査担当も固定中です。変更前にscope-unlockが必要です")
            if "owner_summary" not in existing:
                return {
                    "scope_file": scope_file,
                    "relative": relative,
                    "requirements": requirements,
                    "owner_summary": None,
                    "path": path,
                    "status": "legacy-current",
                }
            if owner_errors:
                raise FlowError("オーナー承認サマリに不備があります:\n- " + "\n- ".join(owner_errors))
            return {
                "scope_file": scope_file,
                "relative": relative,
                "requirements": requirements,
                "owner_summary": owner_summary,
                "path": path,
                "status": "already-current",
            }
    if owner_errors:
        raise FlowError("オーナー承認サマリに不備があります:\n- " + "\n- ".join(owner_errors))
    return {
        "scope_file": scope_file,
        "relative": relative,
        "requirements": requirements,
        "owner_summary": owner_summary,
        "path": path,
        "status": "ready-to-lock",
    }


def cmd_scope_check(args: argparse.Namespace) -> int:
    prepared = prepare_scope_lock(args.scope_file, args.audits, args.single_auditor)
    status = prepared["status"]
    if status == "legacy-current":
        print("scope check: legacy active（既存固定は有効です。オーナー操作は不要です）")
        return 0
    print_scope_lock_owner_receipt(
        prepared["requirements"], prepared["owner_summary"], args.audits, args.single_auditor
    )
    if status == "already-current":
        print("scope check: already current（オーナー操作は不要です）")
    else:
        print("scope check: PASS（内容を承認する場合だけscope-lockを実行してください）")
        options: list[tuple[str, str | Path | int | None]] = [
            ("--scope-file", prepared["scope_file"]),
            ("--audits", args.audits),
        ]
        if args.single_auditor:
            options.append(("--single-auditor", args.single_auditor))
        options.append(("--owner-confirmed", None))
        print("\nオーナーが内容を承認する場合のコピペ用コマンド:")
        print(flowctl_command_block("scope-lock", options))
    return 0


def cmd_scope_lock(args: argparse.Namespace) -> int:
    if not args.owner_confirmed:
        raise FlowError("スコープ固定には --owner-confirmed が必要です")
    prepared = prepare_scope_lock(args.scope_file, args.audits, args.single_auditor)
    scope_file = prepared["scope_file"]
    relative = prepared["relative"]
    requirements = prepared["requirements"]
    owner_summary = prepared["owner_summary"]
    path = prepared["path"]
    if prepared["status"] == "legacy-current":
        print("scope lock: legacy active（既存固定はそのまま有効です。再固定時からオーナー承認サマリが必要です）")
        return 0
    if prepared["status"] == "already-current":
        print_scope_lock_owner_receipt(requirements, owner_summary, args.audits, args.single_auditor)
        print("scope lock: already current")
        return 0
    value = {
        "schema_version": 1,
        "active": True,
        "scope_file": relative,
        "sha256": sha256_file(scope_file),
        "requirements": requirements,
        "owner_summary": owner_summary,
        "audit_count": args.audits,
        "single_auditor": args.single_auditor,
        "locked_at": iso_now(),
    }
    atomic_write_json(path, value)
    history = path.parent / "scope-lock-events"
    history.mkdir(parents=True, exist_ok=True)
    atomic_write_json(history / f"{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}-locked.json", value)
    print_scope_lock_owner_receipt(requirements, owner_summary, args.audits, args.single_auditor)
    print(f"scope lock: PASS ({len(requirements)} requirements)")
    print(path)
    return 0


def cmd_scope_unlock(args: argparse.Namespace) -> int:
    if not args.owner_confirmed:
        raise FlowError("スコープ解除には --owner-confirmed が必要です")
    scope_file = Path(args.scope_file).expanduser().resolve()
    root = find_managed_root(scope_file)
    if root is None:
        raise FlowError("ai-devteam管理対象プロジェクトを特定できません")
    try:
        relative = scope_file.relative_to(root).as_posix()
    except ValueError as error:
        raise FlowError("scope-baseline.mdは管理対象プロジェクト内に置いてください") from error
    if not relative.startswith("docs/flow/") or scope_file.name != "scope-baseline.md":
        raise FlowError("スコープ基準は docs/flow/<機能名>/scope-baseline.md に置いてください")
    path = scope_lock_path(scope_file)
    lock = load_scope_lock(scope_file)
    if lock is None:
        raise FlowError("有効なスコープ固定がありません")
    lock["active"] = False
    lock["unlocked_at"] = iso_now()
    lock["unlock_reason"] = safe_summary(args.reason)
    atomic_write_json(path, lock)
    history = path.parent / "scope-lock-events"
    history.mkdir(parents=True, exist_ok=True)
    atomic_write_json(history / f"{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}-unlocked.json", lock)
    print("scope lock: UNLOCKED。PMが差分案を更新し、オーナーが再固定するまで実装不可です")
    return 0


def refresh_policy_scope(task_dir: Path, policy: dict[str, Any]) -> dict[str, Any]:
    root = git_root(task_dir)
    scope_file = root / str(policy.get("scope_file", ""))
    lock = validate_scope_lock(scope_file)
    requirement_id = str(policy.get("scope_requirement_id", ""))
    requirement = lock.get("requirements", {}).get(requirement_id)
    if not isinstance(requirement, dict):
        raise FlowError(f"固定済みスコープに要求IDがありません: {requirement_id}")
    if requirement.get("risk_level") != policy.get("risk_level"):
        raise FlowError("既存taskのリスク区分は途中変更できません。新しいtaskとして再初期化してください")
    if int(lock.get("audit_count", 2)) != int(policy.get("audit_count", 2)) or lock.get(
        "single_auditor"
    ) != policy.get("single_auditor"):
        raise FlowError("既存taskの監査数・監査担当は途中変更できません")
    policy["scope_sha256"] = lock["sha256"]
    policy["scope_requirement"] = {"id": requirement_id, **requirement}
    save_policy(task_dir, policy)
    return policy


def cmd_init(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    if task_dir.exists() and not task_dir.is_dir():
        raise FlowError(f"task-dirにディレクトリ以外が存在します: {task_dir}")
    if policy_path(task_dir).exists():
        raise FlowError("このtask-dirは既に初期化済みです。既存policyを上書きしません")
    if args.risk == "high" and args.tl == "not-required" and not args.tl_reason:
        raise FlowError("高リスクでTL不要とする場合は --tl-reason が必要です")
    if args.tl == "required" and not args.tl_reason:
        raise FlowError("TL相談の論点を --tl-reason で記録してください")

    scope_file = Path(args.scope_file).expanduser().resolve()
    if task_dir.parent.resolve() != scope_file.parent.resolve():
        raise FlowError("task-dirとscope-baseline.mdは同じ機能ディレクトリ配下にしてください")
    base = ensure_sha(args.base, "base commit")
    validate_branch_and_base(scope_file.parent, args.branch, base)
    scope_lock = validate_scope_lock(scope_file)
    audit_count = int(scope_lock.get("audit_count", 2))
    single_auditor = scope_lock.get("single_auditor")
    if args.audits is not None and args.audits != audit_count:
        raise FlowError("監査数はオーナーがscope-lock時に固定した値と一致させてください")
    if args.single_auditor and args.single_auditor != single_auditor:
        raise FlowError("監査担当はオーナーがscope-lock時に固定した値と一致させてください")
    scope_requirement = scope_lock.get("requirements", {}).get(args.scope_id)
    if not isinstance(scope_requirement, dict):
        raise FlowError(f"固定済みスコープに要求IDがありません: {args.scope_id}")
    if scope_requirement.get("risk_level") != args.risk:
        raise FlowError("リスク区分はオーナーがscope-lock時に固定した値と一致させてください")
    root = git_root(scope_file.parent)
    policy = {
        "schema_version": 1,
        "created_at": iso_now(),
        "risk_level": args.risk,
        "audit_count": audit_count,
        "single_auditor": single_auditor,
        "branch": args.branch,
        "base_commit": base,
        "scope_file": scope_file.relative_to(root).as_posix(),
        "scope_sha256": scope_lock["sha256"],
        "scope_requirement_id": args.scope_id,
        "scope_requirement": {"id": args.scope_id, **scope_requirement},
        "tl_required": args.tl == "required",
        "tl_reason": safe_summary(args.tl_reason) if args.tl_reason else "既存方針内で判断可能",
        "pre_evaluator_required": False,
        "pre_summary_required": args.pre_summary == "required",
        "post_evaluator_required": args.risk == "high",
        "verification_evidence_required": True,
        "formal_doc_globs": sorted(set(args.formal_doc or [])),
        "generated_doc_globs": sorted(set(args.generated_doc or [])),
        "allowed_write_globs": [],
        "instruction_sha256": None,
        "pm_formal_doc_snapshots": {},
    }
    task_dir.mkdir(exist_ok=True)
    task_meta_dir(task_dir).mkdir(parents=True, exist_ok=False)
    save_policy(task_dir, policy)
    with task_lock(task_dir):
        append_event(task_dir, "task_initialized", role="pm", data={"risk_level": args.risk})
        destination = "tl_review" if policy["tl_required"] else "planning"
        transition(
            task_dir,
            {None},
            destination,
            role="pm",
            reason="タスクを初期化",
        )
    print(f"flowctl init: PASS ({destination})")
    print(f"state: {task_meta_dir(task_dir) / 'state.json'}")
    return 0


def cmd_adopt(args: argparse.Namespace) -> int:
    """既存の進行中taskを、安全側の工程からflowctl管理へ取り込む。"""
    if not args.owner_confirmed:
        raise FlowError("進行中taskの取込みには --owner-confirmed が必要です")
    task_dir = task_path(args.task_dir)
    if not task_dir.is_dir():
        raise FlowError(f"task-dirが存在しません: {task_dir}")
    if policy_path(task_dir).exists():
        raise FlowError("このtask-dirは既にflowctl管理下です")
    base = ensure_sha(args.base, "base commit")
    validate_branch_and_base(task_dir, args.branch, base)
    scope_file = Path(args.scope_file).expanduser().resolve()
    if task_dir.parent.resolve() != scope_file.parent.resolve():
        raise FlowError("task-dirとscope-baseline.mdは同じ機能ディレクトリ配下にしてください")
    scope_lock = validate_scope_lock(scope_file)
    requirement = scope_lock.get("requirements", {}).get(args.scope_id)
    if not isinstance(requirement, dict):
        raise FlowError(f"固定済みスコープに要求IDがありません: {args.scope_id}")
    if requirement.get("risk_level") != args.risk:
        raise FlowError("リスク区分はオーナーがscope-lock時に固定した値と一致させてください")
    root = git_root(task_dir)
    policy = {
        "schema_version": 1,
        "created_at": iso_now(),
        "adopted": True,
        "risk_level": args.risk,
        "audit_count": int(scope_lock.get("audit_count", 2)),
        "single_auditor": scope_lock.get("single_auditor"),
        "branch": args.branch,
        "base_commit": base,
        "scope_file": scope_file.relative_to(root).as_posix(),
        "scope_sha256": scope_lock["sha256"],
        "scope_requirement_id": args.scope_id,
        "scope_requirement": {"id": args.scope_id, **requirement},
        "tl_required": False,
        "tl_reason": "取込み前の既存判断を継承。新しい上流判断はplanningへ戻す",
        "pre_evaluator_required": False,
        "pre_summary_required": args.pre_summary == "required",
        "post_evaluator_required": args.risk == "high",
        # 進行中taskの取込みでは既存成果物の形式を後付けで壊さない。
        # 新規init taskだけが再現可能な検証コマンドを必須にする。
        "verification_evidence_required": False,
        "formal_doc_globs": sorted(set(args.formal_doc or [])),
        "generated_doc_globs": sorted(set(args.generated_doc or [])),
        "allowed_write_globs": [],
        "instruction_sha256": None,
        "pm_formal_doc_snapshots": {},
    }
    if args.state != "planning":
        errors, allowed = parse_instruction(task_dir, policy)
        if errors:
            raise FlowError("取込み前の指示書品質ゲートに不合格です:\n- " + "\n- ".join(errors))
        policy["allowed_write_globs"] = allowed
        policy["instruction_sha256"] = sha256_file(task_dir / "instruction.md")
    if args.state in {"implementation_preflight", "implementation", "pm_review"} and policy["pre_summary_required"]:
        pre_summary = task_dir / "pre-summary.md"
        if not pre_summary.is_file() or not pre_summary.read_text(encoding="utf-8").strip():
            raise FlowError("指定工程への取込みには既存pre-summary.mdが必要です")
    if args.state == "pm_review":
        run_handoff_validator(task_dir, Path(args.validator).resolve() if args.validator else None)
        scope_errors = validate_implementation_scope(task_dir, policy)
        if scope_errors:
            raise FlowError("取込み前の差分境界ゲートに不合格です:\n- " + "\n- ".join(scope_errors))

    task_meta_dir(task_dir).mkdir(parents=True, exist_ok=False)
    save_policy(task_dir, policy)
    with task_lock(task_dir):
        append_event(
            task_dir,
            "task_initialized",
            role="owner",
            data={"risk_level": args.risk, "adopted": True},
        )
        append_event(
            task_dir,
            "task_adopted",
            role="owner",
            data={"state": args.state, "reason": safe_summary(args.reason)},
        )
        if args.state == "pm_review":
            digest = product_diff_digest(task_dir, policy)
            append_event(
                task_dir,
                "implementation_submitted",
                role="implementer",
                data={"product_diff_sha256": digest, "changed_file_count": len(task_git_diff_files(task_dir, policy)), "adopted": True},
            )
        transition(task_dir, {None}, args.state, role="owner", reason="進行中taskをflowctlへ取込み")
    print(f"flowctl adopt: PASS ({args.state})")
    return 0


def cmd_tl_complete(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    decision = Path(args.decision_file).expanduser().resolve()
    if not decision.is_file():
        raise FlowError(f"Tech Lead判断ファイルが存在しません: {decision}")
    try:
        relative_decision = decision.relative_to(task_dir.parent)
    except ValueError as error:
        raise FlowError("Tech Lead判断ファイルは同じ機能のdocs/flow配下に置いてください") from error
    if not relative_decision.as_posix().startswith("tech-lead/"):
        raise FlowError("Tech Lead判断ファイルは同じ機能のtech-lead/配下に置いてください")
    with task_lock(task_dir):
        events = load_events(task_dir)
        request = latest_event(events, "tl_consultation_requested")
        latest_decision = latest_event(events, "tl_decision_recorded")
        if not request or (
            latest_decision and latest_decision.get("at", "") > request.get("at", "")
        ):
            raise FlowError("未処理のTech Lead相談登録がありません")
        return_state = (
            str(request.get("data", {}).get("return_state")) if request else "planning"
        )
        if return_state not in {"planning", "implementation_paused"}:
            raise FlowError("Tech Lead相談の復帰工程が不正です")
        append_event(
            task_dir,
            "tl_decision_recorded",
            role="tl",
            data={"decision_file": relative_decision.as_posix(), "sha256": sha256_file(decision)},
        )
        return_context = {}
        if request and return_state == "implementation_paused":
            for key in (
                "classification",
                "instruction_sha256_at_pause",
                "scope_sha256_at_pause",
            ):
                if key in request.get("data", {}):
                    return_context[key] = request["data"][key]
        transition(
            task_dir,
            {"tl_review"},
            return_state,
            role="tl",
            reason="Tech Lead判断完了",
            extra=return_context,
        )
    print(f"Tech Lead gate: PASS ({return_state})")
    return 0


def cmd_tl_request(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    consultation = Path(args.consultation_file).expanduser().resolve()
    if not consultation.is_file() or not consultation.read_text(encoding="utf-8").strip():
        raise FlowError(f"Tech Lead相談資料が存在しないか空です: {consultation}")
    try:
        relative_consultation = consultation.relative_to(task_dir.parent)
    except ValueError as error:
        raise FlowError("Tech Lead相談資料は同じ機能のtech-lead/配下に置いてください") from error
    if not relative_consultation.as_posix().startswith("tech-lead/"):
        raise FlowError("Tech Lead相談資料は同じ機能のtech-lead/配下に置いてください")
    with task_lock(task_dir):
        events = load_events(task_dir)
        state = current_state(events)
        if state not in {"planning", "implementation_paused", "tl_review"}:
            raise FlowError("Tech Lead相談はplanning、実装停止中、または初回TL準備中からだけ開始できます")
        latest_request = latest_event(events, "tl_consultation_requested")
        latest_decision = latest_event(events, "tl_decision_recorded")
        if latest_request and (not latest_decision or latest_request.get("at", "") > latest_decision.get("at", "")):
            raise FlowError("未処理のTech Lead相談が既に登録されています")
        policy = refresh_policy_scope(task_dir, load_policy(task_dir))
        policy["tl_required"] = True
        policy["tl_reason"] = safe_summary(args.summary)
        save_policy(task_dir, policy)
        return_state = "planning" if state == "tl_review" else state
        request_data = {
            "consultation_file": relative_consultation.as_posix(),
            "consultation_sha256": sha256_file(consultation),
            "return_state": return_state,
            "summary": safe_summary(args.summary),
        }
        if state == "implementation_paused":
            pause = latest_event(events, "transition")
            for key in (
                "classification",
                "instruction_sha256_at_pause",
                "scope_sha256_at_pause",
            ):
                if pause and key in pause.get("data", {}):
                    request_data[key] = pause["data"][key]
        append_event(
            task_dir,
            "tl_consultation_requested",
            role="pm",
            data=request_data,
        )
        if state != "tl_review":
            transition(task_dir, {state}, "tl_review", role="pm", reason="上流技術判断をTech Leadへ依頼")
    print("Tech Lead request: PASS (tl_review)")
    return 0


def cmd_instruction_ready(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    with task_lock(task_dir):
        policy = refresh_policy_scope(task_dir, load_policy(task_dir))
        events = load_events(task_dir)
        state = current_state(events)
        if state not in {"planning", "implementation_paused", "implementation", "pm_review", "audit_triage"}:
            raise FlowError(f"この工程では指示書を発行できません: {state}")
        if state in {"implementation", "pm_review", "audit_triage"} and sha256_file(task_dir / "instruction.md") == policy.get("instruction_sha256"):
            raise FlowError("既存taskの指示更新がありません。工程を再演せず現在の成果物を利用してください")
        pause = latest_event(events, "transition") if state == "implementation_paused" else None
        if state == "implementation_paused" and (
            not pause
            or pause.get("data", {}).get("classification") not in {
                "scope-change",
                "tl-review",
                "preflight-return",
            }
        ):
            raise FlowError("この一時停止はPMの指示書更新では再開できません")
        if state == "implementation_paused" and sha256_file(task_dir / "instruction.md") == pause.get(
            "data", {}
        ).get("instruction_sha256_at_pause"):
            raise FlowError("PM差し戻し後のinstruction.md更新が確認できません")
        if policy.get("tl_required") and not latest_event(events, "tl_decision_recorded"):
            raise FlowError("必須のTech Lead判断証跡がありません")
        scope_change = latest_event(events, "scope_change_required")
        if scope_change and scope_change.get("data", {}).get("old_scope_sha256") == policy.get("scope_sha256"):
            raise FlowError("スコープ変更後のscope-baseline.md再固定が確認できません")
        errors, allowed = parse_instruction(task_dir, policy)
        if errors:
            raise FlowError("指示書品質ゲートに不合格です:\n- " + "\n- ".join(errors))
        policy["allowed_write_globs"] = allowed
        policy["instruction_sha256"] = sha256_file(task_dir / "instruction.md")
        candidate_errors = validate_implementation_scope(task_dir, policy)
        if candidate_errors:
            raise FlowError(
                "候補差分の事前検証に不合格です。オーナー操作は不要です:\n- "
                + "\n- ".join(candidate_errors)
            )
        policy["instruction_ready_candidate_diff_sha256"] = product_diff_digest(task_dir, policy)
        if state == "implementation_paused":
            pre_summary = task_dir / "pre-summary.md"
            loop_state = task_dir / "loop-state.md"
            policy["pre_summary_sha_before_scope_change"] = (
                sha256_file(pre_summary) if pre_summary.is_file() else "missing"
            )
            if policy.get("pre_evaluator_required"):
                policy["pre_evaluator_sha_before_scope_change"] = (
                    sha256_file(loop_state) if loop_state.is_file() else "missing"
                )
        save_policy(task_dir, policy)
        append_event(
            task_dir,
            "instruction_validated",
            role="pm",
            data={"instruction_sha256": policy["instruction_sha256"], "allowed_write_count": len(allowed)},
        )
        transition(task_dir, {state}, "instruction_ready", role="pm", reason="指示書品質ゲート合格")
    print("instruction gate: PASS (instruction_ready)")
    return 0


def recent_runtime_record(role: str, task_dir: Path) -> dict[str, Any] | None:
    directory = runtime_sessions_dir()
    if not directory.is_dir():
        return None
    records: list[dict[str, Any]] = []
    for path in directory.glob("*.json"):
        with contextlib.suppress(FlowError, OSError, json.JSONDecodeError):
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("role") == role and value.get("task_dir"):
                if Path(value["task_dir"]).resolve() == task_dir:
                    records.append(value)
    return max(records, key=lambda item: item.get("started_at", ""), default=None)


def cmd_role_start(args: argparse.Namespace) -> int:
    if args.role not in ROLES:
        raise FlowError("未知の役割です")
    if not args.task_dir:
        print(f"role: {args.role}")
        print("task未関連付け。task-dirが確定したらrole-startを同じ役割で再実行してください")
        return 0
    task_dir = task_path(args.task_dir)
    if not policy_path(task_dir).is_file():
        document_task_root(task_dir)
        if args.role == "implementer":
            document_policy(task_dir)
        if args.role.startswith("auditor-"):
            record = recent_runtime_record(args.role, task_dir)
            expected = args.role.removeprefix("auditor-")
            if not record or record.get("provider") != expected or (args.provider and args.provider != expected):
                raise FlowError("独立監査はproviderの一致する別セッションのlifecycle hookが必要です")
            if not (task_dir / "audit-request.md").is_file():
                raise FlowError("PMのaudit-request.mdが必要です。監査対象を推測しないでください")
        print(f"role-start: {args.role} (documents)")
        print("既存資料を使う文書運用です。工程の初期化は不要です。役割と安全境界を継続してください")
        if args.role.startswith("auditor-"):
            print("役割登録は監査範囲の合格ではありません。監査依頼と確定Git差分の開始条件を確認してください")
        return 0
    record = recent_runtime_record(args.role, task_dir)
    if record and args.provider and args.provider != record.get("provider"):
        raise FlowError("hookが記録したproviderと--providerが一致しません")
    provider = (record.get("provider") if record else None) or args.provider
    session_id = record.get("session_id") if record else f"manual-{uuid.uuid4().hex}"
    if record is None:
        if args.role.startswith("auditor-"):
            raise FlowError("独立監査はlifecycle hookが検出できる新しい監査セッションで開始してください")
        print("warning: lifecycle hook未検出。セッション数・稼働時間は記録しません", file=sys.stderr)

    with task_lock(task_dir):
        events = load_events(task_dir)
        state = current_state(events)
        if args.role == "implementer":
            if state == "instruction_ready":
                transition(
                    task_dir,
                    {"instruction_ready"},
                    "implementation_preflight",
                    role="implementer",
                    provider=provider,
                    session_id=session_id,
                    reason="既存調査サマリの軽量確認を開始",
                )
            elif state == "implementation_preflight":
                pass
            elif state != "implementation":
                raise FlowError(f"実装担当を開始できる工程ではありません: {state}")
        elif args.role == "tl":
            if state != "tl_review":
                raise FlowError(f"TLを開始できる工程ではありません: {state}")
            events = load_events(task_dir)
            request = latest_event(events, "tl_consultation_requested")
            decision = latest_event(events, "tl_decision_recorded")
            if not request or (decision and decision.get("at", "") > request.get("at", "")):
                raise FlowError("PMが登録した未処理のTech Lead相談資料がありません")
        elif args.role.startswith("auditor-"):
            auditor = args.role.removeprefix("auditor-")
            start_audit(task_dir, auditor, provider, session_id)
        elif args.role == "pm" and state is None:
            raise FlowError(f"PMを開始できる工程ではありません: {state or '未初期化'}")
        if record is None:
            append_event(
                task_dir,
                "session_measurement_unavailable",
                role=args.role,
                provider=provider,
                session_id=session_id,
                data={"reason": "lifecycle-hook-not-detected"},
            )
    print(f"role-start: PASS ({args.role}, state={current_state(load_events(task_dir))})")
    return 0


def cmd_start_approve(args: argparse.Namespace) -> int:
    if not args.owner_confirmed:
        raise FlowError("実装開始承認には --owner-confirmed が必要です")
    task_dir = task_path(args.task_dir)
    with task_lock(task_dir):
        state = current_state(load_events(task_dir))
        if state == "implementation":
            print("implementation start: already active (legacy command ignored)")
            return 0
        append_event(
            task_dir,
            "owner_command_rejected",
            role="owner",
            data={"command": "start-approve", "reason_code": "command-retired"},
        )
    raise FlowError(
        "start-approveは廃止しました。オーナー操作は不要です。"
        "同じ実装担当セッションでpre-summary.mdを整え、flowctl preflight-completeを実行してください"
    )


def cmd_preflight_complete(args: argparse.Namespace) -> int:
    """Confirm the lightweight existing-pattern survey without owner approval."""
    task_dir = task_path(args.task_dir)
    with task_lock(task_dir):
        policy = refresh_policy_scope(task_dir, load_policy(task_dir))
        if current_state(load_events(task_dir)) != "implementation_preflight":
            raise FlowError("implementation_preflightからだけ実装前確認を完了できます")
        errors = validate_pre_summary(task_dir)
        baseline_digest = policy.get("instruction_ready_candidate_diff_sha256")
        current_digest = product_diff_digest(task_dir, policy)
        if baseline_digest and current_digest != baseline_digest:
            errors.append("instruction-ready後にプロダクト候補差分が変わっています")
        if not baseline_digest:
            loop_state = task_dir / "loop-state.md"
            legacy_text = loop_state.read_text(encoding="utf-8") if loop_state.is_file() else ""
            decisions = re.findall(r"最終判定\s*[:：]\s*(PM差し戻し|実装開始可)", legacy_text)
            if decisions and decisions[-1] == "PM差し戻し":
                summary = "旧実装前検証のPM差し戻しが未解消"
                append_event(
                    task_dir,
                    "owner_feedback",
                    role="implementer",
                    data={"classification": "preflight-return", "summary": summary},
                )
                transition(
                    task_dir,
                    {"implementation_preflight"},
                    "implementation_paused",
                    role="implementer",
                    reason="旧実装前検証の差し戻しをPMへ戻す",
                    extra={
                        "classification": "preflight-return",
                        "instruction_sha256_at_pause": policy.get("instruction_sha256"),
                        "scope_sha256_at_pause": policy.get("scope_sha256"),
                        "candidate_diff_sha256_at_pause": current_digest,
                        "resume_state": "implementation_preflight",
                    },
                )
                print("legacy preflight return: PMへ自動で戻しました。オーナー操作は不要です")
                return 0
        errors.extend(validate_implementation_scope(task_dir, policy))
        if errors:
            raise FlowError(
                "軽量実装前確認に未解決事項があります:\n- "
                + "\n- ".join(errors)
            )
        policy.pop("pre_summary_sha_before_scope_change", None)
        policy.pop("pre_evaluator_sha_before_scope_change", None)
        save_policy(task_dir, policy)
        append_event(
            task_dir,
            "implementation_preflight_completed",
            role="implementer",
            data={
                "candidate_diff_sha256": product_diff_digest(task_dir, policy),
                "owner_approval_required": False,
            },
        )
        transition(
            task_dir,
            {"implementation_preflight"},
            "implementation",
            role="implementer",
            reason="既存パターン調査と開始時差分不変を確認",
        )
    print("lightweight implementation preflight: PASS (implementation)")
    return 0


def validate_pre_summary(task_dir: Path) -> list[str]:
    path = task_dir / "pre-summary.md"
    if not path.is_file():
        return ["pre-summary.mdがありません"]
    text = path.read_text(encoding="utf-8")
    required = ("既存パターン", "予定差分", "検証方法", "未解決事項")
    values = {
        label: re.findall(
            rf"^[ \t]*[-*]?[ \t]*{label}[ \t]*[:：][ \t]*(.*)$",
            text,
            re.MULTILINE,
        )
        for label in required
    }
    missing = [label for label, matches in values.items() if not matches]
    if missing:
        return ["pre-summary.mdに必要な項目がありません: " + "、".join(missing)]
    empty = [
        label
        for label in ("既存パターン", "予定差分", "検証方法")
        if values[label][-1].strip() in {"", "なし", "不明", "未確認"}
    ]
    if empty:
        return ["pre-summary.mdの内容が空または未確認です: " + "、".join(empty)]
    if values["未解決事項"][-1].strip() != "なし":
        return ["未解決事項があるため実装を開始できません。PMへ戻してください"]
    return []


def cmd_feedback(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    summary = safe_summary(args.summary)
    with task_lock(task_dir):
        state = current_state(load_events(task_dir))
        if state not in {"implementation_preflight", "implementation"}:
            raise FlowError(f"実装前・実装中フィードバックを記録できる工程ではありません: {state}")
        if args.kind == "preflight-return" and state != "implementation_preflight":
            raise FlowError("preflight-returnはimplementation_preflightでだけ記録できます")
        policy = load_policy(task_dir)
        append_event(
            task_dir,
            "owner_feedback",
            role="implementer",
            data={"classification": args.kind, "summary": summary},
        )
        if args.kind in {
            "scope-change",
            "tl-review",
            "preflight-return",
            "stop",
            "tooling-blocker",
        }:
            if args.kind == "scope-change":
                append_event(
                    task_dir,
                    "scope_change_required",
                    role="implementer",
                    data={"old_scope_sha256": policy.get("scope_sha256"), "summary": summary},
                )
            if args.kind == "tooling-blocker":
                append_event(
                    task_dir,
                    "tooling_blocker_detected",
                    role="implementer",
                    data={
                        "candidate_diff_sha256": product_diff_digest(task_dir, policy),
                        "summary": summary,
                    },
                )
            transition(
                task_dir,
                {state},
                "implementation_paused",
                role="implementer",
                reason="オーナーフィードバックで一時停止",
                extra={
                    "classification": args.kind,
                    "instruction_sha256_at_pause": policy.get("instruction_sha256"),
                    "scope_sha256_at_pause": policy.get("scope_sha256"),
                    "candidate_diff_sha256_at_pause": product_diff_digest(task_dir, policy),
                    "resume_state": state,
                },
            )
    if args.kind in {"question", "correction"}:
        print("feedback: 記録済み。現在の実装担当セッションで継続できます")
    elif args.kind == "tooling-blocker":
        print("feedback: ツール起因として停止しました。スコープは変更しません。解消後は同じ実装担当セッションでresumeできます")
    elif args.kind == "preflight-return":
        print("feedback: 実装前確認をPMへ戻しました。オーナー操作は不要です")
    else:
        print("feedback: 実装を停止しました。次はPMが仕様・指示書を確認します")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    with task_lock(task_dir):
        events = load_events(task_dir)
        if current_state(events) != "implementation_paused":
            raise FlowError("implementation_pausedからだけ再開できます")
        pause = latest_event(events, "transition")
        classification = pause.get("data", {}).get("classification") if pause else None
        pause_data = pause.get("data", {}) if pause else {}
        resume_state = pause_data.get("resume_state", "implementation")
        if resume_state not in {"implementation_preflight", "implementation"}:
            resume_state = "implementation"
        if classification in {"scope-change", "preflight-return"}:
            if args.owner_confirmed:
                append_event(
                    task_dir,
                    "owner_command_rejected",
                    role="owner",
                    data={"command": "resume", "reason_code": "scope-change-requires-pm"},
                )
            raise FlowError("PM差し戻しはPMがinstruction-readyを通し、実装前確認を再実施してください")
        if classification == "tl-review":
            if args.owner_confirmed:
                append_event(
                    task_dir,
                    "owner_command_rejected",
                    role="owner",
                    data={"command": "resume", "reason_code": "tl-review-requires-pm"},
                )
            raise FlowError("Tech Lead判断待ちはPMが指示書を更新してから再開してください")
        if classification == "tooling-blocker":
            policy = refresh_policy_scope(task_dir, load_policy(task_dir))
            expected_digest = pause.get("data", {}).get("candidate_diff_sha256_at_pause")
            actual_digest = product_diff_digest(task_dir, policy)
            instruction = task_dir / "instruction.md"
            expected_instruction = pause.get("data", {}).get("instruction_sha256_at_pause")
            actual_instruction = sha256_file(instruction) if instruction.is_file() else "missing"
            errors = validate_implementation_scope(task_dir, policy)
            if expected_digest != actual_digest or expected_instruction != actual_instruction or errors:
                details = list(errors)
                if expected_digest != actual_digest:
                    details.insert(0, "ツール停止後に候補差分が変わっています")
                if expected_instruction != actual_instruction:
                    details.insert(0, "ツール停止後に指示書が変わっています")
                raise FlowError(
                    "ツール起因停止を自動再開できません。PMが実際の差分を確認してください:\n- "
                    + "\n- ".join(details)
                )
            append_event(
                task_dir,
                "tooling_blocker_resolved",
                role="implementer",
                data={"candidate_diff_sha256": actual_digest},
            )
        if classification == "stop" and not args.owner_confirmed:
            raise FlowError("停止指示からの再開には --owner-confirmed が必要です")
        transition(
            task_dir,
            {"implementation_paused"},
            resume_state,
            role="implementer",
            reason="既存の実装担当セッションを再開",
        )
    print(f"resume: PASS ({resume_state})")
    return 0


def cmd_recover_tooling(args: argparse.Namespace) -> int:
    """Recover a legacy false scope-change without weakening real scope gates.

    Earlier flowctl versions had no tooling-blocker classification.  This is only
    available when the pause's scope and instruction are still exactly current;
    otherwise the normal scope-change path remains mandatory.
    """
    raise FlowError(
        "旧scope-changeがツール誤判定だったことを安全に証明できないためrecover-toolingは廃止しました。"
        "真の範囲変更工程を使うか、既にtooling-blockerとして記録された停止はresumeしてください"
    )


def run_handoff_validator(task_dir: Path, validator: Path | None) -> None:
    script = validator or Path(__file__).with_name("validate_handoff.py")
    if not script.is_file():
        raise FlowError(f"引き渡し検証スクリプトがありません: {script}")
    result = subprocess.run(
        [sys.executable, str(script), str(task_dir)],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stdout + result.stderr).strip()
        raise FlowError(f"引き渡し形式ゲートに不合格です:\n{detail}")


def validate_reproducible_verification_evidence(task_dir: Path) -> list[str]:
    """Require concise, rerunnable evidence for tasks created by this version."""
    report = task_dir / "report.md"
    if not report.is_file():
        return ["report.mdが存在しない"]
    text = report.read_text(encoding="utf-8")
    match = re.search(
        r"^## 再現可能な検証コマンド\s*$\n(?P<body>.*?)(?=^##\s|\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        return ["report.mdに『## 再現可能な検証コマンド』がない"]
    body = match.group("body")
    commands = [value.strip() for value in re.findall(r"`([^`\n]+)`", body) if value.strip()]
    if not commands:
        return ["再現可能な検証コマンドをバッククォートで1件以上記録してください"]
    if not re.search(r"(?:結果|result)\s*[:：].*(?:成功|pass|passed|0件失敗|failures?\s*[:：]?\s*0)", body, re.IGNORECASE):
        return ["各検証コマンドの実行結果を『結果: 成功』等で記録してください"]
    return []


def validate_required_post_evaluator(task_dir: Path, policy: dict[str, Any]) -> list[str]:
    """Validate declared supplemental evidence, not subagent identity or independence."""
    if not policy.get("post_evaluator_required"):
        return []
    loop_state = task_dir / "loop-state.md"
    text = loop_state.read_text(encoding="utf-8") if loop_state.is_file() else ""
    match = re.search(
        r"^## 内部検証証跡\s*$\n(?P<body>.*?)(?=^##\s|\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    if match is None:
        return ["高リスクタスクに必須の実装後内部検証証跡がありません"]
    body = match.group("body")
    required = (
        "実施要否: 必須",
        "最終判定: 合格",
        "合格後の実装・テスト・設定・自動生成物変更: なし",
    )
    missing = [marker for marker in required if marker not in body]
    return ["高リスクタスクの実装後内部検証証跡が不足しています: " + "、".join(missing)] if missing else []


def cmd_submit(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    with task_lock(task_dir):
        policy = refresh_policy_scope(task_dir, load_policy(task_dir))
        if current_state(load_events(task_dir)) != "implementation":
            raise FlowError("implementation工程からだけPM提出できます")
        run_handoff_validator(task_dir, Path(args.validator).resolve() if args.validator else None)
        post_evaluator_errors = validate_required_post_evaluator(task_dir, policy)
        if post_evaluator_errors:
            raise FlowError("内部検証記録の形式確認に不合格です:\n- " + "\n- ".join(post_evaluator_errors))
        if policy.get("verification_evidence_required"):
            evidence_errors = validate_reproducible_verification_evidence(task_dir)
            if evidence_errors:
                raise FlowError("再現可能な検証証拠が不足しています:\n- " + "\n- ".join(evidence_errors))
        errors = validate_implementation_scope(task_dir, policy)
        if errors:
            raise FlowError("差分境界ゲートに不合格です:\n- " + "\n- ".join(errors))
        digest = product_diff_digest(task_dir, policy)
        append_event(
            task_dir,
            "implementation_submitted",
            role="implementer",
            data={"product_diff_sha256": digest, "changed_file_count": len(task_git_diff_files(task_dir, policy))},
        )
        transition(task_dir, {"implementation"}, "pm_review", role="implementer", reason="PMへ候補差分を提出")
    print("implementation handoff: PASS (pm_review)")
    return 0


def cmd_pm_review(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    with task_lock(task_dir):
        policy = refresh_policy_scope(task_dir, load_policy(task_dir))
        events = load_events(task_dir)
        if current_state(events) != "pm_review":
            raise FlowError("pm_review工程からだけ判定できます")
        if args.result == "return":
            summary = safe_summary(args.summary or "仕様・差分・検証証跡の不備")
            append_event(
                task_dir,
                "pm_returned",
                role="pm",
                data={"classification": "scope-change" if args.scope_change else "in-scope-fix", "summary": summary},
            )
            if args.scope_change:
                append_event(
                    task_dir,
                    "scope_change_required",
                    role="pm",
                    data={"old_scope_sha256": policy.get("scope_sha256"), "summary": summary},
                )
            destination = "planning" if args.scope_change else "implementation"
            transition(task_dir, {"pm_review"}, destination, role="pm", reason="PM差し戻し")
            print(f"PM review: RETURN ({destination})")
            return 0

        review = task_dir / "implementation-review.md"
        if not review.is_file() or not review.read_text(encoding="utf-8").strip():
            raise FlowError("acceptにはimplementation-review.mdが必要です")
        submitted = latest_event(events, "implementation_submitted")
        expected_digest = submitted.get("data", {}).get("product_diff_sha256") if submitted else None
        actual_digest = product_diff_digest(task_dir, policy)
        if expected_digest != actual_digest:
            raise FlowError("実装担当提出後にプロダクト差分が変わっています。実装担当へ戻してください")
        formal_errors = validate_pm_formal_scope(task_dir, policy)
        if formal_errors:
            raise FlowError("PM正式ドキュメント・変更量ゲートに不合格です:\n- " + "\n- ".join(formal_errors))
        policy["pm_formal_doc_snapshots"] = snapshot_formal_docs(task_dir, policy)
        policy["pm_candidate_snapshots"] = snapshot_candidate_changes(task_dir, policy)
        save_policy(task_dir, policy)
        candidate_digest = sha256_text(
            json.dumps(policy["pm_candidate_snapshots"], ensure_ascii=False, sort_keys=True)
        )
        append_event(
            task_dir,
            "pm_accepted",
            role="pm",
            data={
                "implementation_review_sha256": sha256_file(review),
                "candidate_diff_sha256": candidate_digest,
                "candidate_file_count": len(policy["pm_candidate_snapshots"]),
            },
        )
        transition(task_dir, {"pm_review"}, "awaiting_commit", role="pm", reason="PMが候補差分を承認")
    print("PM review: ACCEPT (awaiting_commit)")
    return 0


def cmd_commit_recorded(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    head = ensure_sha(args.head, "確定HEAD")
    with task_lock(task_dir):
        policy = load_policy(task_dir)
        root = git_root(task_dir)
        actual_head = git_output(root, "rev-parse", "HEAD").strip().lower()
        resolved = git_output(root, "rev-parse", head).strip().lower()
        if actual_head != resolved:
            raise FlowError(f"現在HEADが申告SHAと一致しません: current={actual_head}, supplied={resolved}")
        base = str(policy["base_commit"])
        if git_output(root, "diff", "--name-only", f"{base}..{resolved}").strip() == "":
            raise FlowError("base commitから確定HEADまでの監査差分が空です")
        expected_snapshots = policy.get("pm_candidate_snapshots")
        if not isinstance(expected_snapshots, dict):
            raise FlowError("PMが承認した候補差分スナップショットがありません")
        committed_snapshots = snapshot_committed_changes(task_dir, policy, resolved)
        if committed_snapshots != expected_snapshots:
            raise FlowError("確定コミットがPM承認済み候補差分と一致しません")
        dirty = [line for line in git_output(root, "status", "--porcelain", "--untracked-files=all").splitlines() if line]
        allowed_unrelated = {normalize_relative(path) for path in (args.allow_unrelated_file or [])}
        unexpected = [line for line in dirty if line[3:] not in allowed_unrelated]
        if unexpected:
            names = ", ".join(line[3:] for line in unexpected[:10])
            raise FlowError(f"未コミット差分が残っています: {names}")
        append_event(
            task_dir,
            "commit_recorded",
            role="pm",
            data={"head": resolved, "allow_unrelated_files": sorted(allowed_unrelated)},
        )
        transition(
            task_dir,
            {"awaiting_commit"},
            "post_commit_review",
            role="pm",
            reason="オーナーコミットをPMが裏取り",
        )
    print("commit gate: PASS (post_commit_review)")
    return 0


def cmd_audit_ready(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    request = task_dir / "audit-request.md"
    if not request.is_file() or not request.read_text(encoding="utf-8").strip():
        raise FlowError("audit-request.mdが必要です")
    with task_lock(task_dir):
        policy = refresh_policy_scope(task_dir, load_policy(task_dir))
        events = load_events(task_dir)
        commit = latest_event(events, "commit_recorded")
        if not commit:
            raise FlowError("確定コミット証跡がありません")
        root = git_root(task_dir)
        base = str(policy.get("base_commit"))
        head = str(commit.get("data", {}).get("head"))
        actual_head = git_output(root, "rev-parse", "HEAD").strip().lower()
        if actual_head != head:
            raise FlowError("commit-recorded後にHEADが変わっています")
        dirty = git_output(root, "status", "--porcelain", "--untracked-files=all").splitlines()
        allowed_unrelated = set(commit.get("data", {}).get("allow_unrelated_files", []))
        unexpected = [line[3:] for line in dirty if line and line[3:] not in allowed_unrelated]
        if unexpected:
            raise FlowError("監査開始前に未コミット差分があります: " + "、".join(unexpected[:10]))
        text = request.read_text(encoding="utf-8")
        markers = [base, head, f"git diff {base}..{head}", "report.md", "summary.md", "loop-state.md", "implementation-review.md"]
        markers.extend(f"audit-{auditor}.md" for auditor in required_auditors(policy))
        markers.extend(
            line.strip()
            for line in git_output(root, "diff", "--name-only", f"{base}..{head}").splitlines()
            if line.strip()
        )
        missing = [marker for marker in markers if marker not in text]
        if missing:
            raise FlowError("audit-request.mdに確定監査情報が不足しています: " + "、".join(missing))
        append_event(
            task_dir,
            "audit_request_validated",
            role="pm",
            data={"audit_request_sha256": sha256_file(request), "head": head},
        )
        transition(task_dir, {"post_commit_review"}, "audit_ready", role="pm", reason="監査依頼準備完了")
    print("audit request gate: PASS (audit_ready)")
    return 0


def ensure_audit_boundary(task_dir: Path, policy: dict[str, Any], events: Sequence[dict[str, Any]]) -> None:
    commit = latest_event(events, "commit_recorded")
    validated = latest_event(events, "audit_request_validated")
    if not commit or not validated:
        raise FlowError("確定コミットまたは監査依頼の検証証跡がありません")
    root = git_root(task_dir)
    head = str(commit.get("data", {}).get("head", ""))
    if git_output(root, "rev-parse", "HEAD").strip().lower() != head:
        raise FlowError("監査準備後にHEADが変わっています")
    request = task_dir / "audit-request.md"
    if not request.is_file() or sha256_file(request) != validated.get("data", {}).get(
        "audit_request_sha256"
    ):
        raise FlowError("監査準備後にaudit-request.mdが変更されています")


def start_audit(
    task_dir: Path,
    auditor: str,
    provider: str | None = None,
    session_id: str | None = None,
) -> None:
    policy = load_policy(task_dir)
    if auditor not in required_auditors(policy):
        raise FlowError(f"このタスクで要求されていない監査です: {auditor}")
    if provider != auditor:
        raise FlowError(f"{auditor}監査はprovider={auditor}の独立セッションで開始してください")
    if not session_id or session_id.startswith("manual-"):
        raise FlowError("独立監査のsession IDをhookで確認できません")
    events = load_events(task_dir)
    state = current_state(events)
    if state not in {"audit_ready", "auditing"}:
        raise FlowError(f"監査開始可能な工程ではありません: {state}")
    ensure_audit_boundary(task_dir, policy, events)
    round_number = current_audit_round(events)
    results = audit_results_for_round(events, round_number)
    if auditor in results:
        raise FlowError(f"第{round_number}ラウンドの{auditor}監査は既に完了しています")
    duplicate_start = any(
        event.get("kind") == "audit_started"
        and event.get("data", {}).get("round") == round_number
        and event.get("data", {}).get("auditor") == auditor
        for event in events
    )
    if duplicate_start:
        raise FlowError(f"第{round_number}ラウンドの{auditor}監査は既に開始済みです")
    if any(
        event.get("kind") == "audit_started"
        and event.get("data", {}).get("round") == round_number
        and event.get("session_id") == session_id
        for event in events
    ):
        raise FlowError("同じ独立セッションで複数監査を開始できません")
    append_event(
        task_dir,
        "audit_started",
        role=f"auditor-{auditor}",
        provider=provider,
        session_id=session_id,
        data={"auditor": auditor, "round": round_number},
    )
    if state == "audit_ready":
        transition(
            task_dir,
            {"audit_ready"},
            "auditing",
            role=f"auditor-{auditor}",
            provider=provider,
            session_id=session_id,
            reason="独立監査開始",
        )


def infer_audit_result(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    matches = re.findall(r"監査結果\s*[:：]\s*(クローズ可|修正必要|監査前提不足)", text)
    if not matches:
        raise FlowError("監査結果ファイルに最終判定がありません")
    mapping = {"クローズ可": "pass", "修正必要": "fail", "監査前提不足": "prerequisite-missing"}
    return mapping[matches[-1]]


def cmd_audit_result(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    result_file = Path(args.file).expanduser().resolve()
    if not result_file.is_file():
        raise FlowError(f"監査結果ファイルが存在しません: {result_file}")
    expected_result_file = (task_dir / f"audit-{args.auditor}.md").resolve()
    if result_file != expected_result_file:
        raise FlowError(f"監査結果は指定taskの{expected_result_file.name}へ書き出してください")
    inferred = infer_audit_result(result_file)
    if args.result != "auto" and args.result != inferred:
        raise FlowError(f"指定判定と監査結果ファイルが一致しません: specified={args.result}, file={inferred}")
    result = inferred
    with task_lock(task_dir):
        policy = load_policy(task_dir)
        events = load_events(task_dir)
        if current_state(events) != "auditing":
            raise FlowError("auditing工程からだけ監査結果を登録できます")
        ensure_audit_boundary(task_dir, policy, events)
        round_number = current_audit_round(events)
        starts = [
            event
            for event in events
            if event.get("kind") == "audit_started"
            and event.get("data", {}).get("round") == round_number
            and event.get("data", {}).get("auditor") == args.auditor
        ]
        if not starts:
            raise FlowError(f"{args.auditor}監査の開始記録がありません")
        existing = audit_results_for_round(events, round_number)
        if args.auditor in existing:
            raise FlowError(f"{args.auditor}監査結果は既に登録済みです")
        append_event(
            task_dir,
            "audit_result",
            role=f"auditor-{args.auditor}",
            data={
                "auditor": args.auditor,
                "round": round_number,
                "result": result,
                "result_file": result_file.name,
                "result_sha256": sha256_file(result_file),
            },
        )
        results = audit_results_for_round(load_events(task_dir), round_number)
        if all(auditor in results for auditor in required_auditors(policy)):
            transition(
                task_dir,
                {"auditing"},
                "audit_triage",
                role=f"auditor-{args.auditor}",
                reason="必要な独立監査が完了",
            )
    print(f"audit result: {result} (state={current_state(load_events(task_dir))})")
    return 0


def cmd_triage(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    if args.scope_change and args.result != "return":
        raise FlowError("--scope-changeはreturnの場合だけ指定できます")
    triage = task_dir / "audit-triage.md"
    if not triage.is_file() or not triage.read_text(encoding="utf-8").strip():
        raise FlowError("audit-triage.mdが必要です")
    with task_lock(task_dir):
        events = load_events(task_dir)
        if current_state(events) != "audit_triage":
            raise FlowError("audit_triage工程からだけ整理結果を登録できます")
        round_number = current_audit_round(events)
        results = audit_results_for_round(events, round_number)
        required = required_auditors(load_policy(task_dir))
        if not all(auditor in results for auditor in required):
            raise FlowError("必要な独立監査結果がそろっていません")
        if args.result == "recommend-close" and any(value != "pass" for value in results.values()):
            raise FlowError("修正必要または監査前提不足があるためクローズ推薦できません")
        policy = refresh_policy_scope(task_dir, load_policy(task_dir))
        append_event(
            task_dir,
            "audit_triaged",
            role="pm",
            data={"round": round_number, "result": args.result, "scope_change": args.scope_change, "triage_sha256": sha256_file(triage)},
        )
        if args.result == "return" and args.scope_change:
            append_event(
                task_dir,
                "scope_change_required",
                role="pm",
                data={"old_scope_sha256": policy.get("scope_sha256"), "summary": "監査是正でスコープ変更が必要"},
            )
        destination = (
            "owner_close"
            if args.result == "recommend-close"
            else ("planning" if args.scope_change else "implementation")
        )
        transition(task_dir, {"audit_triage"}, destination, role="pm", reason="PMが監査結果を整理")
    print(f"triage: PASS ({destination})")
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    if not args.owner_confirmed:
        raise FlowError("クローズには --owner-confirmed が必要です")
    task_dir = task_path(args.task_dir)
    with task_lock(task_dir):
        if current_state(load_events(task_dir)) != "owner_close":
            raise FlowError("owner_close工程からだけクローズできます")
        append_event(task_dir, "owner_closed", data={"confirmed": True})
        transition(task_dir, {"owner_close"}, "closed", role="owner", reason="オーナーがクローズ")
    print("close: PASS")
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    if not args.owner_confirmed:
        raise FlowError("一時許可には --owner-confirmed が必要です")
    if args.minutes < 1 or args.minutes > 240:
        raise FlowError("一時許可は1〜240分に限定してください")
    task_dir = task_path(args.task_dir)
    expires = utc_now() + dt.timedelta(minutes=args.minutes)
    with task_lock(task_dir):
        append_event(
            task_dir,
            "capability_granted",
            role="owner",
            data={
                "capability": args.capability,
                "granted_role": args.role,
                "expires_at": expires.isoformat(timespec="seconds").replace("+00:00", "Z"),
                "reason": safe_summary(args.reason),
            },
        )
    print(f"temporary capability granted: {args.capability} -> {args.role} ({args.minutes} minutes)")
    return 0


def cmd_revoke(args: argparse.Namespace) -> int:
    if not args.owner_confirmed:
        raise FlowError("一時許可の取消には --owner-confirmed が必要です")
    task_dir = task_path(args.task_dir)
    with task_lock(task_dir):
        append_event(
            task_dir,
            "capability_revoked",
            role="owner",
            data={"capability": args.capability, "granted_role": args.role},
        )
    print(f"temporary capability revoked: {args.capability} -> {args.role}")
    return 0


def role_token(role: str, provider: str) -> str:
    base = "auditor" if role.startswith("auditor-") else role
    return f"${base}" if provider == "codex" else f"/{base}"


def existing_files(task_dir: Path, names: Sequence[str]) -> list[str]:
    return [str(task_dir / name) for name in names if (task_dir / name).is_file()]


def prompt_for(role: str, provider: str, task_dir: Path, state: str) -> str:
    events = load_events(task_dir)
    reusable_role = role in {"pm", "implementer"}
    runtime_record = recent_runtime_record(role, task_dir) if reusable_role else None
    active_runtime = bool(
        runtime_record
        and not runtime_record.get("ended_at")
        and runtime_record.get("provider") == provider
    )
    token = role_token(role, provider) if not active_runtime else ""
    names_by_role_state = {
        ("pm", "planning"): ["instruction.md", "scope-baseline.md"],
        ("pm", "implementation_paused"): ["instruction.md", "pre-summary.md", "loop-state.md"],
        ("pm", "pm_review"): ["instruction.md", "report.md", "summary.md", "loop-state.md"],
        ("pm", "post_commit_review"): ["implementation-review.md", "report.md", "summary.md"],
        ("pm", "audit_triage"): ["audit-request.md", "audit-codex.md", "audit-claude.md", "audit-triage.md"],
        ("implementer", "instruction_ready"): ["instruction.md", "pre-summary.md", "loop-state.md"],
        ("implementer", "implementation"): ["instruction.md", "loop-state.md", "report.md", "summary.md"],
        ("auditor-codex", "audit_ready"): ["audit-request.md", "report.md", "summary.md"],
        ("auditor-claude", "audit_ready"): ["audit-request.md", "report.md", "summary.md"],
    }
    names = names_by_role_state.get((role, state), ["instruction.md", "loop-state.md"])
    files = existing_files(task_dir, names)
    if role == "tl":
        request = latest_event(events, "tl_consultation_requested")
        if request:
            consultation = task_dir.parent / str(request.get("data", {}).get("consultation_file", ""))
            if consultation.is_file():
                files.insert(0, str(consultation))
    file_lines = "\n".join(f"- {path}" for path in files)
    action = {
        "pm": "工程状態と成果物を裏取りし、現在工程で必要なPM作業だけを進めてください。",
        "tl": "相談資料を読み、上流の技術・設計・セキュリティ判断だけを返してください。",
        "implementer": "instruction.mdと既存実装を確認し、承認済み範囲だけを実装してください。",
        "auditor-codex": "audit-request.mdで固定されたコミット差分を独立監査してください。",
        "auditor-claude": "audit-request.mdで固定されたコミット差分を独立監査してください。",
    }[role]
    opening = (
        f"既存の{('PM' if role == 'pm' else '実装担当')}独立セッションへ、以下を貼り付けてください。"
        if active_runtime
        else token
    )
    first_command = (
        "最初に次を実行してください。既に関連付け済みのためrole-startは再実行しません。\n"
        + flowctl_command_block("status", [("--task-dir", task_dir)])
        if active_runtime
        else "最初に次を実行してください。\n"
        + flowctl_command_block(
            "role-start", [("--role", role), ("--task-dir", task_dir)]
        )
    )
    if active_runtime and role == "implementer" and state == "instruction_ready":
        first_command = (
            "更新された指示書を同じ実装担当セッションで確認し、軽量preflightへ進むため次を実行してください。\n"
            + flowctl_command_block("role-start", [("--role", role), ("--task-dir", task_dir)])
        )
    return "\n".join(
        (
            opening,
            "",
            f"対象task: {task_dir}",
            f"flowctl工程: {state}",
            action,
            "初回は以下を確認してください。既存セッションでは変更された成果物・節だけを再確認し、hash不変の資料は全文再読不要です。",
            file_lines or "- （現時点で追加成果物なし）",
            "",
            first_command,
            "別役割の独立セッションは起動せず、工程完了時はflowctl nextの出力を提示してください。別機能なら、このセッションへ貼らず新しい独立セッションを開始してください。",
        )
    )


def cmd_next(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    if not policy_path(task_dir).is_file():
        document_task_root(task_dir)
        print(f"文書運用: {task_dir}")
        print("tasks.mdの現在地と最新の指示・報告・監査から、担当役割の成果物を作成して引き継いでください。")
        print("機械的な次工程・合格判定はありません。initやscope-lockをこの案内だけで追加しないでください。")
        return 0
    policy = load_policy(task_dir)
    events = load_events(task_dir)
    state = current_state(events)
    provider = args.provider
    outputs: list[str] = []
    if state == "tl_review":
        request = latest_event(events, "tl_consultation_requested")
        decision = latest_event(events, "tl_decision_recorded")
        if not request or (decision and decision.get("at", "") > request.get("at", "")):
            outputs.append(
                "既存のPM独立セッションでTech Lead相談資料を作成し、次を実行してください。\n"
                + flowctl_command_block(
                    "tl-request",
                    [
                        ("--task-dir", task_dir),
                        ("--consultation-file", "<相談資料>"),
                        ("--summary", "<判断論点>"),
                    ],
                )
                + "\n"
                "登録後にflowctl nextを再実行してください。"
            )
        else:
            outputs.append(prompt_for("tl", provider, task_dir, state))
    elif state in {"planning", "pm_review", "post_commit_review", "audit_triage"}:
        outputs.append(prompt_for("pm", provider, task_dir, state))
    elif state in {"instruction_ready", "implementation"}:
        outputs.append(prompt_for("implementer", provider, task_dir, state))
    elif state == "implementation_preflight":
        policy = refresh_policy_scope(task_dir, policy)
        errors = validate_implementation_scope(task_dir, policy)
        if errors:
            outputs.append(
                "旧実装前工程に範囲外候補があります。オーナー操作は不要です。"
                "実装担当が不要な候補を撤回し、元の成果に不可欠で固定glob外ならPMへ戻してください。\n- "
                + "\n- ".join(errors)
            )
        else:
            outputs.append(
                "同じ実装担当セッションでpre-summary.mdへ既存パターン・予定差分・検証方法・未解決事項を記録し、次を実行してください。別Evaluatorとオーナー開始承認は不要です。\n"
                + flowctl_command_block("preflight-complete", [("--task-dir", task_dir)])
            )
    elif state == "implementation_paused":
        pause = latest_event(events, "transition")
        classification = pause.get("data", {}).get("classification") if pause else None
        if classification in {"scope-change", "tl-review", "preflight-return"}:
            outputs.append(prompt_for("pm", provider, task_dir, state))
        elif classification == "tooling-blocker":
            outputs.append(
                "ツール起因の停止です。スコープ固定・指示書・オーナー承認を繰り返しません。\n"
                "ツール側の問題が解消した後、同じ実装担当独立セッションで候補差分を変更せずに次を実行してください。\n"
                + flowctl_command_block("resume", [("--task-dir", task_dir)])
                + "\n"
                "再開時に固定スコープと候補差分digestを再検証します。"
            )
        else:
            outputs.append(
                "オーナーが停止理由を確認し、再開する場合だけ次を実行してください。\n"
                + flowctl_command_block(
                    "resume", [("--task-dir", task_dir), ("--owner-confirmed", None)]
                )
            )
    elif state == "awaiting_commit":
        outputs.append("あなた（オーナー）がimplementation-review.mdを確認してコミットし、同じPMセッションへ確定SHAを伝えてください。")
    elif state in {"audit_ready", "auditing"}:
        round_number = current_audit_round(events)
        results = audit_results_for_round(events, round_number)
        started = {
            str(event.get("data", {}).get("auditor"))
            for event in events
            if event.get("kind") == "audit_started" and event.get("data", {}).get("round") == round_number
        }
        for auditor in required_auditors(policy):
            if auditor not in results and auditor not in started:
                outputs.append(prompt_for(f"auditor-{auditor}", auditor, task_dir, state))
        if not outputs:
            outputs.append("開始済みの独立監査結果を待ってください。監査セッションを重複起動しません。")
    elif state == "owner_close":
        outputs.append(
            "あなた（オーナー）が監査整理を確認し、問題なければ次を実行してください。\n"
            + flowctl_command_block(
                "close", [("--task-dir", task_dir), ("--owner-confirmed", None)]
            )
        )
    elif state == "closed":
        outputs.append("このタスクはオーナーによりクローズ済みです。")
    else:
        raise FlowError(f"次工程を生成できない状態です: {state}")
    print("\n\n---\n\n".join(outputs))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if not args.task_dir:
        directory = Path(args.project_root or os.getcwd()).expanduser().resolve()
        features = discover_flow_documents(directory)
        if args.json:
            print(json.dumps({"features": features, "current_task_inferred": False}, ensure_ascii=False, indent=2))
        else:
            print("文書の所在（現在task・完了状態は推測していません）:")
            for feature in features:
                print(feature["feature_dir"])
                for entrypoint in feature["entrypoints"]:
                    print(f"  入口: {entrypoint}")
                for task in feature["tasks"]:
                    print(f"  task: {task['task_dir']} ({task['workflow']}, 記録={task['recorded_state'] or 'なし'})")
            if not features:
                print("docs/flowの資料は見つかりません。依頼の対象パスを確認してください。初期化は要求しません。")
        return 0
    task_dir = task_path(args.task_dir)
    if not task_dir.is_dir():
        raise FlowError(f"対象taskが存在しません: {task_dir}")
    if not policy_path(task_dir).is_file():
        result = {"workflow": "documents", "state": None, "task_dir": str(task_dir), "metrics": None}
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print("workflow: documents（機械工程の記録なし。実装の進捗は文書で確認します）")
            print("未初期化は実装不備ではありません。既存資料をそのまま利用でき、init/adoptは不要です")
        return 0
    policy = load_policy(task_dir)
    events = load_events(task_dir)
    metrics = calculate_metrics(task_dir)
    result = {
        "workflow": "managed",
        "state": current_state(events),
        "risk": policy.get("risk_level"),
        "audits": required_auditors(policy),
        "event_count": len(events),
        "metrics": metrics,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(f"state: {result['state']}")
        print(f"risk: {result['risk']}")
        print(f"audits: {', '.join(result['audits'])}")
        print(f"sessions: {metrics['session_count']}")
        print(f"PM returns: {metrics['pm_returns']}/{metrics['implementation_submissions']}")
        print(f"first audit pass: {metrics['first_audit_pass']}")
        print("記録上の状態です。実際の完了・品質は成果物と差分で確認してください")
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    if args.flow_root:
        print(json.dumps(aggregate_metrics(Path(args.flow_root).resolve()), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    task_dir = task_path(args.task_dir)
    metrics = calculate_metrics(task_dir)
    print(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True) if args.json else metrics_markdown(metrics))
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    policy = refresh_policy_scope(task_dir, load_policy(task_dir))
    errors: list[str] = []
    instruction_errors, _ = parse_instruction(task_dir, policy)
    errors.extend(instruction_errors)
    state = current_state(load_events(task_dir))
    if state in {
        "instruction_ready",
        "implementation_preflight",
        "implementation",
        "implementation_paused",
        "pm_review",
    }:
        errors.extend(validate_implementation_scope(task_dir, policy))
    if errors:
        raise FlowError("validate: FAIL\n- " + "\n- ".join(errors))
    print("validate: PASS")
    return 0


def cmd_hook(args: argparse.Namespace) -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as error:
        raise FlowError(f"hook入力JSONが不正です: {error}") from error
    if not isinstance(payload, dict):
        raise FlowError("hook入力はobjectである必要があります")
    result = handle_hook(payload, args.provider)
    if result is not None:
        print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_install_hooks(args: argparse.Namespace) -> int:
    executable = Path(args.executable or __file__).expanduser().resolve()
    default = Path.home() / (".codex/hooks.json" if args.provider == "codex" else ".claude/settings.json")
    config = Path(args.config).expanduser().resolve() if args.config else default
    config.parent.mkdir(parents=True, exist_ok=True)
    changed, backup = install_hooks(args.provider, executable, config)
    print(f"hooks: {'installed' if changed else 'already current'} -> {config}")
    if backup:
        print(f"backup: {backup}")
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    root = find_managed_root(Path(args.project_root).resolve())
    print(
        "managed project: "
        + ("role-capable (normal sessions stay inactive until role-start)" if root else "no")
    )
    for provider, path in (
        ("codex", Path.home() / ".codex/hooks.json"),
        ("claude", Path.home() / ".claude/settings.json"),
    ):
        present = path.is_file() and ".ai-devteam/bin/flowctl" in path.read_text(encoding="utf-8")
        print(f"{provider} hook: {'installed' if present else 'missing'}")
    config = Path.home() / ".codex/config.toml"
    legacy = False
    if config.is_file():
        text = config.read_text(encoding="utf-8")
        legacy = bool(re.search(r"^\s*sandbox_mode\s*=", text, re.MULTILINE))
    print(f"codex legacy sandbox_mode: {'present (permission profiles are ignored)' if legacy else 'absent'}")
    project_config = root / ".claude" / "settings.json" if root else None
    legacy_denies = legacy_claude_git_denies(project_config) if project_config else []
    legacy_allows = legacy_claude_git_allows(project_config) if project_config else []
    if legacy_denies or legacy_allows:
        print(
            "project legacy Claude Git permissions: "
            f"{len(legacy_denies)} denies, {len(legacy_allows)} allows present "
            "(normal Claude sessions are affected)"
        )
    else:
        print("project legacy Claude Git permissions: absent")
    return 1 if not root else 0


def cmd_remove_legacy_claude_guards(args: argparse.Namespace) -> int:
    if not args.owner_confirmed:
        raise FlowError("旧Claude静的ガードの除去には--owner-confirmedが必要です")
    root = Path(args.project_root).expanduser().resolve()
    if not root.is_dir():
        raise FlowError(f"project-rootがディレクトリではありません: {root}")
    config = root / ".claude" / "settings.json"
    removed_denies, removed_allows, backup = remove_legacy_claude_git_permissions(config)
    if not removed_denies and not removed_allows:
        print(f"legacy Claude Git permissions: already absent -> {config}")
        return 0
    print(
        "legacy Claude Git permissions: "
        f"removed {removed_denies} denies and {removed_allows} allows -> {config}"
    )
    print(f"backup: {backup}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flowctl",
        description="ai-devteamの独立セッション工程を検証・記録する。AIや別役割を自動起動しない。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    scope_lock = sub.add_parser("scope-lock", help="オーナーが承認済みスコープ基準を固定する")
    scope_lock.add_argument("--scope-file", required=True)
    scope_lock.add_argument("--audits", type=int, choices=(1, 2), default=2)
    scope_lock.add_argument("--single-auditor", choices=sorted(AUDITORS))
    scope_lock.add_argument("--owner-confirmed", action="store_true")
    scope_lock.set_defaults(func=cmd_scope_lock)

    scope_check = sub.add_parser(
        "scope-check",
        help="PMがオーナー操作前にスコープ固定内容を読み取り検証する",
    )
    scope_check.add_argument("--scope-file", required=True)
    scope_check.add_argument("--audits", type=int, choices=(1, 2), default=2)
    scope_check.add_argument("--single-auditor", choices=sorted(AUDITORS))
    scope_check.set_defaults(func=cmd_scope_check)

    scope_unlock = sub.add_parser("scope-unlock", help="オーナーがスコープ再検討のため固定を解除する")
    scope_unlock.add_argument("--scope-file", required=True)
    scope_unlock.add_argument("--reason", required=True)
    scope_unlock.add_argument("--owner-confirmed", action="store_true")
    scope_unlock.set_defaults(func=cmd_scope_unlock)

    init = sub.add_parser("init", help="PMがタスクのリスク・監査数・Git境界を固定する")
    init.add_argument("--task-dir", required=True)
    init.add_argument("--scope-file", required=True)
    init.add_argument("--scope-id", required=True)
    init.add_argument("--risk", choices=RISK_LEVELS, required=True)
    init.add_argument("--audits", type=int, choices=(1, 2))
    init.add_argument("--single-auditor", choices=sorted(AUDITORS))
    init.add_argument("--branch", required=True)
    init.add_argument("--base", required=True)
    init.add_argument("--tl", choices=("required", "not-required"), required=True)
    init.add_argument("--tl-reason")
    init.add_argument("--pre-evaluator", choices=("required", "not-required"), default="not-required", help="旧工程互換。新規taskでは使用しない")
    init.add_argument("--pre-summary", choices=("required", "not-required"), default="required")
    init.add_argument("--formal-doc", action="append")
    init.add_argument("--generated-doc", action="append")
    init.set_defaults(func=cmd_init)

    adopt = sub.add_parser("adopt", help="オーナーが進行中taskを安全側の工程から管理へ取り込む")
    adopt.add_argument("--task-dir", required=True)
    adopt.add_argument("--scope-file", required=True)
    adopt.add_argument("--scope-id", required=True)
    adopt.add_argument("--risk", choices=RISK_LEVELS, required=True)
    adopt.add_argument("--branch", required=True)
    adopt.add_argument("--base", required=True)
    adopt.add_argument("--state", choices=("planning", "instruction_ready", "implementation_preflight", "implementation", "pm_review"), required=True)
    adopt.add_argument("--pre-evaluator", choices=("required", "not-required"), default="not-required", help="旧工程互換。取込み後の開始ゲートには使用しない")
    adopt.add_argument("--pre-summary", choices=("required", "not-required"), default="required")
    adopt.add_argument("--formal-doc", action="append")
    adopt.add_argument("--generated-doc", action="append")
    adopt.add_argument("--validator")
    adopt.add_argument("--reason", required=True)
    adopt.add_argument("--owner-confirmed", action="store_true")
    adopt.set_defaults(func=cmd_adopt)

    tl = sub.add_parser("tl-complete", help="Tech Lead判断を記録してPMへ戻す")
    tl.add_argument("--task-dir", required=True)
    tl.add_argument("--decision-file", required=True)
    tl.set_defaults(func=cmd_tl_complete)

    tl_request = sub.add_parser("tl-request", help="PMが途中で必要になった上流判断をTech Leadへ依頼する")
    tl_request.add_argument("--task-dir", required=True)
    tl_request.add_argument("--consultation-file", required=True)
    tl_request.add_argument("--summary", required=True)
    tl_request.set_defaults(func=cmd_tl_request)

    ready = sub.add_parser("instruction-ready", help="指示書品質ゲートを通す")
    ready.add_argument("--task-dir", required=True)
    ready.set_defaults(func=cmd_instruction_ready)

    role = sub.add_parser("role-start", help="独立セッションの役割を固定しタスクへ関連付ける")
    role.add_argument("--role", choices=sorted(ROLES), required=True)
    role.add_argument("--task-dir")
    role.add_argument("--project-root")
    role.add_argument("--provider", choices=("codex", "claude"))
    role.set_defaults(func=cmd_role_start)

    preflight = sub.add_parser("preflight-complete", help="実装担当が実装前確認の合格を記録して実装へ進む")
    preflight.add_argument("--task-dir", required=True)
    preflight.set_defaults(func=cmd_preflight_complete)

    start = sub.add_parser("start-approve", help="旧工程互換: オーナーが実装前確認を承認する")
    start.add_argument("--task-dir", required=True)
    start.add_argument("--owner-confirmed", action="store_true")
    start.set_defaults(func=cmd_start_approve)

    feedback = sub.add_parser("feedback", help="実装前・実装中の質問・指摘を分類して記録する")
    feedback.add_argument("--task-dir", required=True)
    feedback.add_argument(
        "--kind",
        choices=("question", "correction", "tl-review", "scope-change", "preflight-return", "tooling-blocker", "stop"),
        required=True,
    )
    feedback.add_argument("--summary", required=True)
    feedback.set_defaults(func=cmd_feedback)

    resume = sub.add_parser("resume", help="停止済み実装を同じ実装担当セッションで再開する")
    resume.add_argument("--task-dir", required=True)
    resume.add_argument("--owner-confirmed", action="store_true")
    resume.set_defaults(func=cmd_resume)

    recover_tooling = sub.add_parser(
        "recover-tooling",
        help="PMが旧版で誤分類されたツール起因停止を安全に復旧する",
    )
    recover_tooling.add_argument("--task-dir", required=True)
    recover_tooling.add_argument("--summary", required=True)
    recover_tooling.set_defaults(func=cmd_recover_tooling)

    submit = sub.add_parser("submit", help="引き渡し・差分境界を検証してPMレビューへ進める")
    submit.add_argument("--task-dir", required=True)
    submit.add_argument("--validator")
    submit.set_defaults(func=cmd_submit)

    review = sub.add_parser("pm-review", help="PMが候補差分をacceptまたはreturnする")
    review.add_argument("--task-dir", required=True)
    review.add_argument("--result", choices=("accept", "return"), required=True)
    review.add_argument("--scope-change", action="store_true")
    review.add_argument("--summary")
    review.set_defaults(func=cmd_pm_review)

    commit = sub.add_parser("commit-recorded", help="オーナーコミットをPMが裏取りする")
    commit.add_argument("--task-dir", required=True)
    commit.add_argument("--head", required=True)
    commit.add_argument("--allow-unrelated-file", action="append")
    commit.set_defaults(func=cmd_commit_recorded)

    audit_ready = sub.add_parser("audit-ready", help="PMのaudit-requestを検証する")
    audit_ready.add_argument("--task-dir", required=True)
    audit_ready.set_defaults(func=cmd_audit_ready)

    audit_result = sub.add_parser("audit-result", help="監査ファイルの判定を登録する")
    audit_result.add_argument("--task-dir", required=True)
    audit_result.add_argument("--auditor", choices=sorted(AUDITORS), required=True)
    audit_result.add_argument("--result", choices=("auto", "pass", "fail", "prerequisite-missing"), default="auto")
    audit_result.add_argument("--file", required=True)
    audit_result.set_defaults(func=cmd_audit_result)

    triage = sub.add_parser("triage", help="PMが監査結果を整理する")
    triage.add_argument("--task-dir", required=True)
    triage.add_argument("--result", choices=("return", "recommend-close"), required=True)
    triage.add_argument("--scope-change", action="store_true")
    triage.set_defaults(func=cmd_triage)

    close = sub.add_parser("close", help="オーナーだけが最終クローズする")
    close.add_argument("--task-dir", required=True)
    close.add_argument("--owner-confirmed", action="store_true")
    close.set_defaults(func=cmd_close)

    approve = sub.add_parser("approve", help="オーナーが期限付きのローカル操作権限を付与する")
    approve.add_argument("--task-dir", required=True)
    approve.add_argument("--capability", choices=("isolated-db", "migration", "network", "dependency-install"), required=True)
    approve.add_argument("--role", choices=sorted(ROLES), default="implementer")
    approve.add_argument("--minutes", type=int, default=60)
    approve.add_argument("--reason", required=True)
    approve.add_argument("--owner-confirmed", action="store_true")
    approve.set_defaults(func=cmd_approve)

    revoke = sub.add_parser("revoke", help="オーナーが期限付き権限を取り消す")
    revoke.add_argument("--task-dir", required=True)
    revoke.add_argument("--capability", choices=("isolated-db", "migration", "network", "dependency-install"), required=True)
    revoke.add_argument("--role", choices=sorted(ROLES), default="implementer")
    revoke.add_argument("--owner-confirmed", action="store_true")
    revoke.set_defaults(func=cmd_revoke)

    next_parser = sub.add_parser("next", help="現在工程から次セッション用プロンプトを生成する")
    next_parser.add_argument("--task-dir", required=True)
    next_parser.add_argument("--provider", choices=("codex", "claude"), required=True)
    next_parser.set_defaults(func=cmd_next)

    status = sub.add_parser("status", help="記録された工程、またはプロジェクト内の文書の所在を読み取り表示する")
    status_target = status.add_mutually_exclusive_group()
    status_target.add_argument("--task-dir")
    status_target.add_argument("--project-root")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    metrics = sub.add_parser("metrics", help="タスクまたはflow全体の自動指標を表示する")
    group = metrics.add_mutually_exclusive_group(required=True)
    group.add_argument("--task-dir")
    group.add_argument("--flow-root")
    metrics.add_argument("--json", action="store_true")
    metrics.set_defaults(func=cmd_metrics)

    validate = sub.add_parser("validate", help="現在の指示書・差分境界を再検証する")
    validate.add_argument("--task-dir", required=True)
    validate.set_defaults(func=cmd_validate)

    hook = sub.add_parser("hook", help=argparse.SUPPRESS)
    hook.add_argument("--provider", choices=("codex", "claude"), required=True)
    hook.set_defaults(func=cmd_hook)

    install = sub.add_parser("install-hooks", help="既存設定を保持してライフサイクルフックを追加する")
    install.add_argument("--provider", choices=("codex", "claude"), required=True)
    install.add_argument("--config")
    install.add_argument("--executable")
    install.set_defaults(func=cmd_install_hooks)

    diagnose = sub.add_parser("diagnose", help="ガードの有効状態を秘密値なしで確認する")
    diagnose.add_argument("--project-root", default=os.getcwd())
    diagnose.set_defaults(func=cmd_diagnose)

    remove_legacy = sub.add_parser(
        "remove-legacy-claude-guards",
        help="通常セッションにも作用する旧Claude Git permissionだけをバックアップ後に除去する",
    )
    remove_legacy.add_argument("--project-root", default=os.getcwd())
    remove_legacy.add_argument("--owner-confirmed", action="store_true")
    remove_legacy.set_defaults(func=cmd_remove_legacy_claude_guards)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except FlowError as error:
        print(f"flowctl: FAIL\n{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
