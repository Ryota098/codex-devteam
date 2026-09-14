#!/usr/bin/env python3
"""Document-workflow role and safety helpers; not a workflow approval engine"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Sequence

from flowctl_lib import (
    RETIRED_WORKFLOW_COMMANDS, ROLES, FlowError,
    aggregate_metrics, calculate_metrics, current_state,
    find_managed_root, handle_hook, install_hooks, iso_now,
    legacy_claude_git_allows, legacy_claude_git_denies, load_events,
    metrics_markdown, policy_path, remove_legacy_claude_git_permissions,
    runtime_sessions_dir,
)

VERSION = "3.1.0"


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
                        "workflow": "documents",
                        "process_gate": False,
                        "legacy_state": legacy_state(task),
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


def role_token(role: str, provider: str) -> str:
    base = "auditor" if role.startswith("auditor-") else role
    return f"${base}" if provider == "codex" else f"/{base}"


def cmd_metrics(args: argparse.Namespace) -> int:
    if args.flow_root:
        print(json.dumps(aggregate_metrics(Path(args.flow_root).resolve()), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    task_dir = task_path(args.task_dir)
    metrics = calculate_metrics(task_dir)
    print(json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True) if args.json else metrics_markdown(metrics))
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


def legacy_state(task_dir: Path) -> str | None:
    """Read historical state for diagnostics, never as a gate"""
    if not policy_path(task_dir).is_file():
        return None
    try:
        return current_state(load_events(task_dir))
    except (FlowError, OSError, ValueError):
        return "unreadable-history"


def cmd_role_start(args: argparse.Namespace) -> int:
    if args.task_dir:
        task_dir = task_path(args.task_dir)
        document_task_root(task_dir)
        if args.role.startswith("auditor-"):
            expected = args.role.removeprefix("auditor-")
            record = recent_runtime_record(args.role, task_dir)
            if args.provider and args.provider != expected:
                raise FlowError("監査役とproviderが一致しません")
            if not record or record.get("provider") != expected:
                raise FlowError("独立監査の役割を確認できません。対応providerのhookを確認してください")
    print(f"role-start: {args.role} (documents)")
    print("安全ガードの役割登録です。実装・監査の合格や開始承認ではありません")
    print("PMが指定した現行資料を使ってください。旧stateの同期・初期化・工程コマンドは不要です")
    if not args.task_dir:
        print("対象taskが分かり次第、同じ役割で--task-dirを関連付けてください")
    return 0


def cmd_retired(args: argparse.Namespace) -> int:
    print(f"flowctl {args.command}: 廃止済みの工程操作です（変更なし）")
    print("旧state・scope-lockは履歴として保存し、現在の開始条件には使用しません")
    print("工程同期・再固定・task複製は不要です。現行のPM資料と証拠から担当作業を続けてください")
    print("これは承認・合格・停止解除ではありません。未解決事項とオーナーの停止・安全条件は維持してください")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    document_task_root(task_path(args.task_dir))
    print("次の担当は旧stateでは決めません。現行のPM指示・報告・監査依頼と実差分から判断してください")
    print("実装担当はreport / summary / loop-stateをPMへ提出。PMの確認後にPM指定の依頼書で独立監査へ渡します")
    print("監査担当は自分の結果をPMへ返します。工程承認コマンドの転送は不要です")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if not args.task_dir:
        directory = Path(args.project_root or os.getcwd()).expanduser().resolve()
        result = {
            "workflow": "documents", "process_gate": False,
            "current_task_inferred": False, "features": discover_flow_documents(directory),
        }
    else:
        task_dir = task_path(args.task_dir)
        document_task_root(task_dir)
        result = {
            "workflow": "documents", "state": None, "process_gate": False,
            "task_dir": str(task_dir), "legacy_state": legacy_state(task_dir),
            "metrics": None,
        }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print("workflow: documents（工程状態による開始・書込みゲートなし）")
        print("現在地はPMのtasks.mdと最新の指示・報告・監査を参照します。init/adoptや状態同期は不要です")
        if args.task_dir:
            print(f"legacy state: {result['legacy_state'] or 'なし'}（参考履歴のみ、現在の進捗ではありません）")
        else:
            for feature in result["features"]:
                print(feature["feature_dir"])
                for entry in feature["entrypoints"]:
                    print(f"  {entry}")
                for task in feature["tasks"]:
                    print(f"  {task['task_dir']}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    task_dir = task_path(args.task_dir)
    document_task_root(task_dir)
    instruction = task_dir / "instruction.md"
    print(f"instruction.md: {'present' if instruction.is_file() and not instruction.is_symlink() else 'missing'}")
    print("書式・パス列挙・工程状態は検査しません。実装可否のゲートではありません")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flowctl",
        description="ai-devteamの役割・安全ガードと文書の所在確認。工程承認・状態同期は行わない。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)

    role = sub.add_parser("role-start", help="AIセッションの役割と作業対象を安全ガードへ登録する")
    role.add_argument("--role", choices=sorted(ROLES), required=True)
    role.add_argument("--task-dir")
    role.add_argument("--project-root")
    role.add_argument("--provider", choices=("codex", "claude"))
    role.set_defaults(func=cmd_role_start)

    status = sub.add_parser("status", help="文書の所在と参考履歴だけを表示する")
    target = status.add_mutually_exclusive_group()
    target.add_argument("--task-dir")
    target.add_argument("--project-root")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    next_parser = sub.add_parser("next", help="文書による受け渡しの案内（開始承認は生成しない）")
    next_parser.add_argument("--task-dir", required=True)
    next_parser.add_argument("--provider", choices=("codex", "claude"))
    next_parser.set_defaults(func=cmd_next)

    validate = sub.add_parser("validate", help="現行instruction.mdの所在だけを読み取り確認する（任意）")
    validate.add_argument("--task-dir", required=True)
    validate.set_defaults(func=cmd_validate)

    metrics = sub.add_parser("metrics", help="旧工程の計測履歴を表示する（現在の実作業時間ではない）")
    metric_target = metrics.add_mutually_exclusive_group(required=True)
    metric_target.add_argument("--task-dir")
    metric_target.add_argument("--flow-root")
    metrics.add_argument("--json", action="store_true")
    metrics.set_defaults(func=cmd_metrics)

    hook = sub.add_parser("hook", help=argparse.SUPPRESS)
    hook.add_argument("--provider", choices=("codex", "claude"), required=True)
    hook.set_defaults(func=cmd_hook)

    install = sub.add_parser("install-hooks", help="既存設定を保持して安全フックを配置する")
    install.add_argument("--provider", choices=("codex", "claude"), required=True)
    install.add_argument("--config")
    install.add_argument("--executable")
    install.set_defaults(func=cmd_install_hooks)

    diagnose = sub.add_parser("diagnose", help="安全ガードの設定を読み取り確認する")
    diagnose.add_argument("--project-root", default=os.getcwd())
    diagnose.set_defaults(func=cmd_diagnose)

    remove_legacy = sub.add_parser("remove-legacy-claude-guards", help="通常セッションにも作用する旧Claude設定だけをバックアップ後に除去する")
    remove_legacy.add_argument("--project-root", default=os.getcwd())
    remove_legacy.add_argument("--owner-confirmed", action="store_true")
    remove_legacy.set_defaults(func=cmd_remove_legacy_claude_guards)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in RETIRED_WORKFLOW_COMMANDS:
        # Accept old pasted commands only to explain retirement, without running
        # any old workflow function or interpreting their options as approval
        return cmd_retired(argparse.Namespace(command=arguments[0]))
    parser = build_parser()
    args = parser.parse_args(arguments)
    try:
        return int(args.func(args))
    except (FlowError, OSError, ValueError) as error:
        print(f"flowctl: FAIL\n{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
