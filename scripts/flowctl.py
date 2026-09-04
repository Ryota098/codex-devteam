#!/usr/bin/env python3
"""独立したAI役割セッション間の工程を検証・記録するCLI。"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import re
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


VERSION = "2.1.0"


def task_path(value: str) -> Path:
    return Path(value).expanduser().resolve()


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


def cmd_scope_lock(args: argparse.Namespace) -> int:
    if not args.owner_confirmed:
        raise FlowError("スコープ固定には --owner-confirmed が必要です")
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
    requirements, errors = parse_scope_baseline(scope_file)
    if errors:
        raise FlowError("スコープ基準に不備があります:\n- " + "\n- ".join(errors))
    if args.audits == 1 and args.single_auditor not in AUDITORS:
        raise FlowError("1監査では --single-auditor codex|claude が必要です")
    if args.audits == 2 and args.single_auditor:
        raise FlowError("2監査では --single-auditor を指定しません")
    path = scope_lock_path(scope_file)
    if path.is_file():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("active"):
            if existing.get("sha256") == sha256_file(scope_file):
                same_audit_policy = (
                    int(existing.get("audit_count", 2)) == args.audits
                    and existing.get("single_auditor") == args.single_auditor
                )
                if same_audit_policy:
                    print("scope lock: already current")
                    return 0
                raise FlowError("監査数・監査担当も固定中です。変更前にscope-unlockが必要です")
            raise FlowError("既存スコープは固定中です。変更前にscope-unlockが必要です")
    value = {
        "schema_version": 1,
        "active": True,
        "scope_file": relative,
        "sha256": sha256_file(scope_file),
        "requirements": requirements,
        "audit_count": args.audits,
        "single_auditor": args.single_auditor,
        "locked_at": iso_now(),
    }
    atomic_write_json(path, value)
    history = path.parent / "scope-lock-events"
    history.mkdir(parents=True, exist_ok=True)
    atomic_write_json(history / f"{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}-locked.json", value)
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


def ensure_pre_evaluator_evidence(task_dir: Path, policy: dict[str, Any]) -> None:
    if not policy.get("pre_evaluator_required"):
        return
    loop_state = task_dir / "loop-state.md"
    evidence = loop_state.read_text(encoding="utf-8") if loop_state.is_file() else ""
    required_markers = (
        "## 実装前検証証跡",
        "実施方式: 別コンテキストのサブエージェント",
        "最終判定: 実装開始可",
        "実装開始前のプロダクト差分: なし",
    )
    missing = [marker for marker in required_markers if marker not in evidence]
    if missing:
        raise FlowError("必須の実装前内部検証証跡が不足しています: " + "、".join(missing))
    previous = policy.get("pre_evaluator_sha_before_scope_change")
    if previous and sha256_file(loop_state) == previous:
        raise FlowError("スコープ再承認後の実装前内部検証を新しい差分で再実施してください")


def cmd_init(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    if not task_dir.is_dir():
        raise FlowError(f"task-dirが存在しません: {task_dir}")
    if policy_path(task_dir).exists():
        raise FlowError("このtask-dirは既に初期化済みです。既存policyを上書きしません")
    if args.risk == "high" and args.pre_evaluator == "not-required":
        raise FlowError("高リスクタスクは実装前内部検証を省略できません")
    if args.risk == "high" and args.pre_summary == "not-required":
        raise FlowError("高リスクタスクは実装前サマリを省略できません")
    if args.risk == "high" and args.tl == "not-required" and not args.tl_reason:
        raise FlowError("高リスクでTL不要とする場合は --tl-reason が必要です")
    if args.tl == "required" and not args.tl_reason:
        raise FlowError("TL相談の論点を --tl-reason で記録してください")

    base = ensure_sha(args.base, "base commit")
    validate_branch_and_base(task_dir, args.branch, base)
    scope_file = Path(args.scope_file).expanduser().resolve()
    if task_dir.parent.resolve() != scope_file.parent.resolve():
        raise FlowError("task-dirとscope-baseline.mdは同じ機能ディレクトリ配下にしてください")
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
    root = git_root(task_dir)
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
        "pre_evaluator_required": args.pre_evaluator == "required" or args.risk == "high",
        "pre_summary_required": args.pre_summary == "required",
        "post_evaluator_required": True,
        "formal_doc_globs": sorted(set(args.formal_doc or [])),
        "generated_doc_globs": sorted(set(args.generated_doc or [])),
        "allowed_write_globs": [],
        "instruction_sha256": None,
        "pm_formal_doc_snapshots": {},
    }
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
    if args.risk == "high" and args.pre_evaluator == "not-required":
        raise FlowError("高リスクタスクは実装前内部検証を省略できません")
    if args.risk == "high" and args.pre_summary == "not-required":
        raise FlowError("高リスクタスクは実装前サマリを省略できません")
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
        "pre_evaluator_required": args.pre_evaluator == "required" or args.risk == "high",
        "pre_summary_required": args.pre_summary == "required",
        "post_evaluator_required": True,
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
    if args.state in {"implementation", "pm_review"}:
        ensure_pre_evaluator_evidence(task_dir, policy)
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
        if state not in {"planning", "implementation_paused"}:
            raise FlowError(f"instruction-readyはplanningまたは範囲変更停止からだけ実行できます: {state}")
        pause = latest_event(events, "transition") if state == "implementation_paused" else None
        if state == "implementation_paused" and (
            not pause
            or pause.get("data", {}).get("classification") not in {"scope-change", "tl-review"}
        ):
            raise FlowError("停止指示による一時停止はPMの指示書更新では再開できません")
        if state == "implementation_paused" and sha256_file(task_dir / "instruction.md") == pause.get(
            "data", {}
        ).get("instruction_sha256_at_pause"):
            raise FlowError("範囲変更後のinstruction.md更新が確認できません")
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
    record = recent_runtime_record(args.role, task_dir)
    provider = args.provider or (record.get("provider") if record else None)
    session_id = record.get("session_id") if record else f"manual-{uuid.uuid4().hex}"
    if record is None:
        append_event(
            task_dir,
            "session_started",
            role=args.role,
            provider=provider,
            session_id=session_id,
            data={"span_id": session_id, "measurement": "manual-start-only"},
        )
        print("warning: lifecycle hook未検出。セッション時間は開始時刻のみ記録します", file=sys.stderr)

    with task_lock(task_dir):
        events = load_events(task_dir)
        state = current_state(events)
        if args.role == "implementer":
            if state == "instruction_ready":
                destination = (
                    "implementation_preflight"
                    if load_policy(task_dir).get("pre_summary_required", True)
                    else "implementation"
                )
                transition(
                    task_dir,
                    {"instruction_ready"},
                    destination,
                    role="implementer",
                    provider=provider,
                    session_id=session_id,
                    reason="実装担当セッション開始",
                )
            elif state not in {"implementation_preflight", "implementation"}:
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
        elif args.role == "pm" and state in {"closed", None}:
            raise FlowError(f"PMを開始できる工程ではありません: {state or '未初期化'}")
    print(f"role-start: PASS ({args.role}, state={current_state(load_events(task_dir))})")
    return 0


def cmd_start_approve(args: argparse.Namespace) -> int:
    if not args.owner_confirmed:
        raise FlowError("実装開始承認には --owner-confirmed が必要です")
    task_dir = task_path(args.task_dir)
    pre_summary = task_dir / "pre-summary.md"
    if not pre_summary.is_file() or not pre_summary.read_text(encoding="utf-8").strip():
        raise FlowError("実装開始承認にはpre-summary.mdが必要です")
    with task_lock(task_dir):
        policy = refresh_policy_scope(task_dir, load_policy(task_dir))
        if current_state(load_events(task_dir)) != "implementation_preflight":
            raise FlowError("implementation_preflightからだけ実装開始を承認できます")
        previous_summary = policy.get("pre_summary_sha_before_scope_change")
        if previous_summary and sha256_file(pre_summary) == previous_summary:
            raise FlowError("スコープ再承認後の内容でpre-summary.mdを更新してください")
        ensure_pre_evaluator_evidence(task_dir, policy)
        policy.pop("pre_summary_sha_before_scope_change", None)
        policy.pop("pre_evaluator_sha_before_scope_change", None)
        save_policy(task_dir, policy)
        append_event(task_dir, "implementation_start_approved", role="owner", data={"pre_summary_sha256": sha256_file(pre_summary)})
        transition(
            task_dir,
            {"implementation_preflight"},
            "implementation",
            role="owner",
            reason="オーナーが実装前サマリを承認",
        )
    print("implementation start: APPROVED")
    return 0


def cmd_feedback(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    summary = safe_summary(args.summary)
    with task_lock(task_dir):
        state = current_state(load_events(task_dir))
        if state != "implementation":
            raise FlowError(f"実装中フィードバックを記録できる工程ではありません: {state}")
        policy = load_policy(task_dir)
        append_event(
            task_dir,
            "owner_feedback",
            role="implementer",
            data={"classification": args.kind, "summary": summary},
        )
        if args.kind in {"scope-change", "tl-review", "stop"}:
            if args.kind == "scope-change":
                append_event(
                    task_dir,
                    "scope_change_required",
                    role="implementer",
                    data={"old_scope_sha256": policy.get("scope_sha256"), "summary": summary},
                )
            transition(
                task_dir,
                {"implementation"},
                "implementation_paused",
                role="implementer",
                reason="オーナーフィードバックで一時停止",
                extra={
                    "classification": args.kind,
                    "instruction_sha256_at_pause": policy.get("instruction_sha256"),
                    "scope_sha256_at_pause": policy.get("scope_sha256"),
                },
            )
    if args.kind in {"question", "correction"}:
        print("feedback: 記録済み。現在の実装担当セッションで継続できます")
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
        if classification == "scope-change":
            raise FlowError("範囲変更はPMがinstruction-readyを通し、実装前確認を再実施して再開してください")
        if classification == "stop" and not args.owner_confirmed:
            raise FlowError("停止指示からの再開には --owner-confirmed が必要です")
        transition(
            task_dir,
            {"implementation_paused"},
            "implementation",
            role="implementer",
            reason="既存の実装担当セッションを再開",
        )
    print("resume: PASS (implementation)")
    return 0


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


def cmd_submit(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    with task_lock(task_dir):
        policy = refresh_policy_scope(task_dir, load_policy(task_dir))
        if current_state(load_events(task_dir)) != "implementation":
            raise FlowError("implementation工程からだけPM提出できます")
        run_handoff_validator(task_dir, Path(args.validator).resolve() if args.validator else None)
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


def cmd_audit_start(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    with task_lock(task_dir):
        start_audit(task_dir, args.auditor, args.provider, None)
    print(f"audit start: PASS ({args.auditor})")
    return 0


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
    base = role.removeprefix("auditor-") if role.startswith("auditor-") else role
    return f"${base}" if provider == "codex" else f"/{base}"


def existing_files(task_dir: Path, names: Sequence[str]) -> list[str]:
    return [str(task_dir / name) for name in names if (task_dir / name).is_file()]


def prompt_for(role: str, provider: str, task_dir: Path, state: str) -> str:
    events = load_events(task_dir)
    reusable_role = role in {"pm", "implementer"}
    used_before = any(
        event.get("kind") == "session_started" and event.get("role") == role
        for event in events
    )
    token = role_token(role, provider) if not (reusable_role and used_before) else ""
    names = ["instruction.md", "pre-summary.md", "loop-state.md", "report.md", "summary.md", "implementation-review.md", "audit-request.md", "audit-codex.md", "audit-claude.md", "audit-triage.md"]
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
        if reusable_role and used_before
        else token
    )
    return "\n".join(
        (
            opening,
            "",
            f"対象task: {task_dir}",
            f"flowctl工程: {state}",
            action,
            "以下の存在するファイルを全文確認してください。",
            file_lines or "- （現時点で追加成果物なし）",
            "",
            f"最初に ~/.ai-devteam/bin/flowctl role-start --role {role} --task-dir {task_dir} を実行してください。",
            "別役割の独立セッションは起動せず、工程完了時はflowctl nextの出力を提示してください。",
        )
    )


def cmd_next(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
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
                f"~/.ai-devteam/bin/flowctl tl-request --task-dir {task_dir} "
                "--consultation-file <相談資料> --summary <判断論点>\n"
                "登録後にflowctl nextを再実行してください。"
            )
        else:
            outputs.append(prompt_for("tl", provider, task_dir, state))
    elif state in {"planning", "pm_review", "post_commit_review", "audit_triage"}:
        outputs.append(prompt_for("pm", provider, task_dir, state))
    elif state in {"instruction_ready", "implementation"}:
        outputs.append(prompt_for("implementer", provider, task_dir, state))
    elif state == "implementation_preflight":
        outputs.append(
            "pre-summary.mdを確認し、問題なければオーナー自身のターミナルで次を実行してください。\n"
            f"~/.ai-devteam/bin/flowctl start-approve --task-dir {task_dir} --owner-confirmed\n"
            "承認後は現在の実装担当セッションへ、そのまま続行するよう伝えてください。"
        )
    elif state == "implementation_paused":
        pause = latest_event(events, "transition")
        if pause and pause.get("data", {}).get("classification") in {"scope-change", "tl-review"}:
            outputs.append(prompt_for("pm", provider, task_dir, state))
        else:
            outputs.append("オーナーが停止理由を確認し、再開する場合だけ flowctl resume --owner-confirmed を実行してください。")
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
        outputs.append("あなた（オーナー）が監査整理を確認し、問題なければ flowctl close --owner-confirmed を実行してください。")
    elif state == "closed":
        outputs.append("このタスクはオーナーによりクローズ済みです。")
    else:
        raise FlowError(f"次工程を生成できない状態です: {state}")
    print("\n\n---\n\n".join(outputs))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    with task_lock(task_dir):
        refresh_derived_files(task_dir)
    policy = load_policy(task_dir)
    events = load_events(task_dir)
    metrics = calculate_metrics(task_dir)
    result = {
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
    if current_state(load_events(task_dir)) in {"implementation", "pm_review"}:
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
    init.add_argument("--pre-evaluator", choices=("required", "not-required"), required=True)
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
    adopt.add_argument("--pre-evaluator", choices=("required", "not-required"), required=True)
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

    start = sub.add_parser("start-approve", help="オーナーが実装前サマリを承認する")
    start.add_argument("--task-dir", required=True)
    start.add_argument("--owner-confirmed", action="store_true")
    start.set_defaults(func=cmd_start_approve)

    feedback = sub.add_parser("feedback", help="実装中の質問・指摘を分類して記録する")
    feedback.add_argument("--task-dir", required=True)
    feedback.add_argument(
        "--kind",
        choices=("question", "correction", "tl-review", "scope-change", "stop"),
        required=True,
    )
    feedback.add_argument("--summary", required=True)
    feedback.set_defaults(func=cmd_feedback)

    resume = sub.add_parser("resume", help="停止済み実装を同じ実装担当セッションで再開する")
    resume.add_argument("--task-dir", required=True)
    resume.add_argument("--owner-confirmed", action="store_true")
    resume.set_defaults(func=cmd_resume)

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

    audit_start = sub.add_parser("audit-start", help="要求された独立監査を開始記録する")
    audit_start.add_argument("--task-dir", required=True)
    audit_start.add_argument("--auditor", choices=sorted(AUDITORS), required=True)
    audit_start.add_argument("--provider", choices=("codex", "claude"))
    audit_start.set_defaults(func=cmd_audit_start)

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

    status = sub.add_parser("status", help="現在工程を表示する")
    status.add_argument("--task-dir", required=True)
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
