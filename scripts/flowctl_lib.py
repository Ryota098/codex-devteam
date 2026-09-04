#!/usr/bin/env python3
"""ai-devteam の状態機械、検証、ガード、メトリクスの共通実装。"""

from __future__ import annotations

import contextlib
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterator, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None


SCHEMA_VERSION = 1
MANAGED_MARKER = "# AI開発フロー共通規約"
ROLES = {"pm", "tl", "implementer", "auditor-codex", "auditor-claude"}
RISK_LEVELS = {"low": "低", "standard": "標準", "high": "高"}
AUDITORS = {"codex", "claude"}
FINAL_STATES = {"closed"}

ROLE_FLOWCTL_COMMANDS = {
    "pm": {
        "init",
        "tl-request",
        "instruction-ready",
        "pm-review",
        "commit-recorded",
        "audit-ready",
        "triage",
        "next",
        "status",
        "metrics",
        "validate",
        "diagnose",
    },
    "tl": {"tl-complete", "next", "status", "metrics", "validate", "diagnose"},
    "implementer": {"feedback", "resume", "submit", "next", "status", "metrics", "validate", "diagnose"},
    "auditor-codex": {"audit-start", "audit-result", "next", "status", "metrics", "validate", "diagnose"},
    "auditor-claude": {"audit-start", "audit-result", "next", "status", "metrics", "validate", "diagnose"},
}

INACTIVE_READ_ONLY_FLOWCTL_COMMANDS = {
    "--help",
    "--version",
    "-h",
    "diagnose",
    "metrics",
    "status",
    "validate",
}

LEGACY_CLAUDE_GIT_DENIES = frozenset(
    {
        "Bash(git add:*)",
        "Bash(git commit)",
        "Bash(git commit:*)",
        "Bash(git push)",
        "Bash(git push:*)",
        "Bash(git pull)",
        "Bash(git pull:*)",
        "Bash(git checkout:*)",
        "Bash(git switch:*)",
        "Bash(git merge:*)",
        "Bash(git rebase:*)",
        "Bash(git reset:*)",
        "Bash(git restore:*)",
        "Bash(git rm:*)",
        "Bash(git mv:*)",
        "Bash(git clean:*)",
        "Bash(git stash)",
        "Bash(git stash:*)",
        "Bash(git tag:*)",
        "Bash(git branch:*)",
        "Bash(git cherry-pick:*)",
        "Bash(git revert:*)",
        "Bash(git init)",
        "Bash(git init:*)",
        "Bash(git remote:*)",
        "Bash(gh pr create:*)",
        "Bash(gh pr merge:*)",
    }
)

LEGACY_CLAUDE_GIT_ALLOWS = frozenset(
    {
        "Bash(git status)",
        "Bash(git status:*)",
        "Bash(git log:*)",
        "Bash(git diff:*)",
        "Bash(git show:*)",
        "Bash(git blame:*)",
        "Bash(git branch)",
    }
)

ROLE_WRITE_STATES = {
    "pm": {"planning", "tl_review", "implementation_paused", "pm_review", "post_commit_review", "audit_triage"},
    "tl": {"tl_review"},
    "implementer": {"implementation_preflight", "implementation"},
    "auditor-codex": {"auditing"},
    "auditor-claude": {"auditing"},
}

FLOW_OWNERS = {
    "pm": {
        "scope-baseline.md",
        "spec.md",
        "tasks.md",
        "instruction.md",
        "implementation-review.md",
        "audit-request.md",
        "audit-triage.md",
    },
    "implementer": {"pre-summary.md", "loop-state.md", "report.md", "summary.md"},
}

SECRET_PATH_PATTERNS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*credential*",
    "*credentials*",
    "*secret*",
    "**/.aws/**",
    "**/.ssh/**",
)

DEFAULT_FORMAL_DOC_GLOBS = (
    "README",
    "README.*",
    "CHANGELOG",
    "CHANGELOG.*",
    "docs/**",
    "doc/**",
    "adr/**",
    "ADR/**",
    "*.md",
    "*.mdx",
    "*.rst",
    "*.adoc",
    "**/*.md",
    "**/*.mdx",
    "**/*.rst",
    "**/*.adoc",
)

HIGH_RISK_WORDS = (
    "認証",
    "認可",
    "権限モデル",
    "信頼境界",
    "テナント",
    "課金",
    "billing",
    "payment",
    "migration",
    "マイグレーション",
    "不可逆",
    "物理削除",
    "秘密情報",
    "個人情報",
    "暗号化",
    "鍵管理",
    "webhook",
    "外部送信",
    "外部サービス契約",
    "並行実行",
    "競合",
    "二重実行",
    "rollback",
    "ロールバック",
)

NEGATIVE_WORDS = (
    "なし",
    "対象外",
    "変更しない",
    "影響しない",
    "追加しない",
    "実施しない",
    "行わない",
    "不要",
)

GIT_MUTATION = re.compile(
    r"(?:^|[;&|\n]\s*)(?:(?:command|sudo)\s+|env(?:\s+(?:-\S+|[A-Za-z_][A-Za-z0-9_]*=\S+))*\s+)*(?:\S*/)?git\s+(?:(?:-C|-c)\s+\S+\s+)*(?:add|commit|push|pull|checkout|switch|merge|rebase|reset|"
    r"restore|rm|mv|clean|stash|tag|branch\s+(?:-[dDmM]\S*|--delete|--move|[A-Za-z0-9][^\s]*)|cherry-pick|"
    r"revert|init|apply|am|notes|update-ref|worktree\s+(?:add|remove|move|prune)|"
    r"lfs\s+(?:track|untrack)|remote\s+(?:add|remove|set-url)|fetch|clone|config|update-index|"
    r"submodule|sparse-checkout|replace|symbolic-ref|bisect|gc|repack)\b",
    re.IGNORECASE,
)

GH_MUTATION = re.compile(
    r"(?:^|[;&|\n]\s*)gh\s+(?:pr\s+(?:create|merge|close|reopen|edit)|"
    r"release\s+(?:create|delete|edit|upload)|repo\s+(?:create|delete|fork|rename)|issue\s+(?:create|close|reopen|edit))\b",
    re.IGNORECASE,
)

HARD_DENY_COMMAND = re.compile(
    r"(?:^|\s)(?:sudo\s+)?(?:rm\s+-[^\n]*[rR][^\n]*[fF]|mkfs(?:\.|\s)|dd\s+if=|"
    r"shutdown\b|reboot\b|halt\b|docker\s+system\s+prune|kubectl\s+(?:apply|delete)|"
    r"terraform\s+(?:apply|destroy)|(?:aws|gcloud|az)\s+[^\n]*(?:deploy|delete|remove))",
    re.IGNORECASE,
)

PRODUCTION_COMMAND = re.compile(
    r"(?:--(?:environment|env|stage)[=\s]+(?:prod|production)\b|"
    r"\b(?:prod|production)[-_ ](?:db|database|cluster|project|environment)\b|"
    r"\bdeploy(?:ment)?\b[^\n]*(?:prod|production)|"
    r"\b(?:ssh|scp|sftp)\b|docker\s+login\b)",
    re.IGNORECASE,
)

NETWORK_COMMAND = re.compile(
    r"(?:^|[;&|]\s*)(?:curl|wget|nc|ncat|telnet|ftp|sftp|scp|ssh|gh\s+(?:api|pr|release)|"
    r"npm\s+(?:publish|login)|pnpm\s+publish|yarn\s+npm\s+publish)\b",
    re.IGNORECASE,
)

DEPENDENCY_INSTALL_COMMAND = re.compile(
    r"(?:^|[;&|]\s*)(?:npm\s+(?:install|ci|update|uninstall)|pnpm\s+(?:install|add|remove|update)|"
    r"yarn\s+(?:install|add|remove|upgrade)|bun\s+(?:install|add|remove|update)|"
    r"pip(?:3)?\s+install|poetry\s+(?:add|remove|install|update)|cargo\s+(?:add|update)|"
    r"go\s+get)\b",
    re.IGNORECASE,
)

MIGRATION_COMMAND = re.compile(
    r"(?:prisma\s+(?:migrate|db\s+push)|knex\s+migrate|sequelize\s+db:migrate|"
    r"rails\s+db:migrate|alembic\s+upgrade|typeorm\s+migration:run)",
    re.IGNORECASE,
)

DATABASE_COMMAND = re.compile(
    r"(?:^|[;&|]\s*)(?:psql|mysql|mariadb|sqlite3|mongosh|redis-cli)\b|"
    r"RUN_DB_INTEGRATION_TESTS\s*=\s*(?:1|true)",
    re.IGNORECASE,
)

DIRECT_FILE_WRITE_COMMAND = re.compile(
    r"(?:^|[;&|]\s*)(?:(?:command|sudo)\s+|env\s+)*(?:\S*/)?(?:cp|mv|rm|touch|truncate|install|mkdir|rmdir|ln|chmod|chown|chgrp)\s|"
    r"\b(?:tee|sed\s+-i|perl\s+-pi)\b",
    re.IGNORECASE,
)

INLINE_INTERPRETER_COMMAND = re.compile(
    r"(?:^|[;&|]\s*)(?:(?:\S*/)?(?:ba|z|da|k)?sh\s+-[A-Za-z]*c[A-Za-z]*|"
    r"(?:\S*/)?python(?:3(?:\.\d+)?)?\s+(?:-[A-Za-z]+\s+)*-c|"
    r"(?:\S*/)?node\s+(?:--[^\s]+\s+)*-[A-Za-z]*e[A-Za-z]*|"
    r"(?:\S*/)?ruby\s+-e|(?:\S*/)?perl\s+-e)\b",
    re.IGNORECASE,
)

DEPENDENCY_MANIFESTS = {
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lock",
    "bun.lockb",
    "pyproject.toml",
    "poetry.lock",
    "pdm.lock",
    "uv.lock",
    "pipfile",
    "pipfile.lock",
    "cargo.toml",
    "cargo.lock",
    "go.mod",
    "go.sum",
    "composer.json",
    "composer.lock",
    "gemfile",
    "gemfile.lock",
}

SECRET_VALUE_PATTERN = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{12,}|AKIA[0-9A-Z]{12,}|"
    r"(?:password|passwd|token|secret|api[_-]?key)\s*[=:]\s*\S+)",
    re.IGNORECASE,
)


class FlowError(RuntimeError):
    """利用者へそのまま返せるフロー違反。"""


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise FlowError(f"JSONを安全に読めません: {path}: {error}") from error
    if not isinstance(value, dict):
        raise FlowError(f"JSONのルートはobjectである必要があります: {path}")
    return value


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_summary(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise FlowError("要約は空にできません")
    if len(value) > 200:
        raise FlowError("要約は200文字以内にしてください。原文やログは保存しません")
    if SECRET_VALUE_PATTERN.search(value):
        raise FlowError("秘密情報らしい値を検出したため記録しません")
    return value


def normalize_relative(path: str | Path) -> str:
    value = str(path).replace("\\", "/")
    while value.startswith("./"):
        value = value[2:]
    return value.strip("/")


def find_managed_root(start: str | Path) -> Path | None:
    current = Path(start).expanduser().resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        for name in ("AGENTS.md", "CLAUDE.md"):
            rule_file = candidate / name
            try:
                with rule_file.open("r", encoding="utf-8") as handle:
                    if MANAGED_MARKER in handle.read(4096):
                        return candidate
            except (OSError, UnicodeError):
                continue
    return None


def git_root(start: str | Path) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=Path(start),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise FlowError("作業ディレクトリがGitリポジトリではありません")
    return Path(result.stdout.strip()).resolve()


def git_output(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, text=True, capture_output=True, check=False
    )
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        raise FlowError(f"git {' '.join(args)} に失敗しました: {detail[-1] if detail else '詳細なし'}")
    return result.stdout


def task_meta_dir(task_dir: Path) -> Path:
    return task_dir.resolve() / ".ai-devteam"


def policy_path(task_dir: Path) -> Path:
    return task_meta_dir(task_dir) / "policy.json"


def scope_lock_path(scope_file: Path) -> Path:
    return scope_file.resolve().parent / ".ai-devteam" / "scope-lock.json"


def parse_scope_baseline(scope_file: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    if not scope_file.is_file():
        return {}, [f"スコープ基準ファイルが存在しません: {scope_file}"]
    text = scope_file.read_text(encoding="utf-8")
    errors: list[str] = []
    section = re.search(
        r"^## 承認対象\s*$\n(?P<body>.*?)(?=^##\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if section is None:
        return {}, ["scope-baseline.mdに『## 承認対象』がありません"]
    expected_header = ["要求ID", "承認済みの外部成果", "変更可能パス", "許可するリスク領域", "リスク区分", "変更上限"]
    rows: list[list[str]] = []
    for line in section.group("body").splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells == expected_header or all(re.fullmatch(r"[-: ]+", cell or "-") for cell in cells):
            continue
        if len(cells) == 6:
            rows.append(cells)
    if not rows:
        errors.append("承認対象表に1件以上の要求が必要です")
    requirements: dict[str, dict[str, Any]] = {}
    for index, cells in enumerate(rows, start=1):
        requirement_id, outcome, paths_value, risks_value, risk_level_value, budget_value = cells
        if not re.fullmatch(r"要求[1-9][0-9]*", requirement_id):
            errors.append(f"承認対象表の行{index}: 要求IDは『要求1』形式にしてください")
        if requirement_id in requirements:
            errors.append(f"承認対象表の要求IDが重複しています: {requirement_id}")
        paths = [normalize_relative(item.strip(" `")) for item in re.split(r"<br\s*/?>|、|,", paths_value) if item.strip(" `")]
        budget_match = re.fullmatch(r"\s*([1-9][0-9]*)\s*ファイル\s*[/／・,、]\s*([1-9][0-9]*)\s*行\s*", budget_value)
        risk_names = {value: key for key, value in RISK_LEVELS.items()}
        if not outcome or not paths or not risks_value or risk_level_value not in risk_names or budget_match is None:
            errors.append(f"承認対象表の行{index}に空欄があります")
        if risk_level_value not in risk_names:
            errors.append(f"承認対象表の行{index}: リスク区分は低・標準・高のいずれかにしてください")
        if budget_match is None:
            errors.append(f"承認対象表の行{index}: 変更上限は『20ファイル / 2000行』形式にしてください")
        for pattern in paths:
            if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
                errors.append(f"変更可能パスはプロジェクト相対globにしてください: {pattern}")
        requirements[requirement_id] = {
            "outcome": outcome,
            "write_globs": sorted(set(paths)),
            "risk_domains": [item.strip() for item in re.split(r"<br\s*/?>|、|,", risks_value) if item.strip()],
            "risk_level": risk_names.get(risk_level_value, ""),
            "max_files": int(budget_match.group(1)) if budget_match else 0,
            "max_changed_lines": int(budget_match.group(2)) if budget_match else 0,
        }
    outside = re.search(
        r"^## 明示的な対象外\s*$\n(?P<body>.*?)(?=^##\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if outside is None or not re.search(r"^\s*[-*]\s+\S", outside.group("body"), re.MULTILINE):
        errors.append("『## 明示的な対象外』に少なくとも1項目を記載してください（対象外なしの場合も理由を記載）")
    return requirements, errors


def load_scope_lock(scope_file: Path) -> dict[str, Any] | None:
    path = scope_lock_path(scope_file)
    if not path.is_file():
        return None
    value = read_json(path)
    return value if value.get("active") else None


def validate_scope_lock(scope_file: Path) -> dict[str, Any]:
    lock = load_scope_lock(scope_file)
    if lock is None:
        raise FlowError("オーナーが固定したscope-baseline.mdがありません")
    actual_hash = sha256_file(scope_file)
    if lock.get("sha256") != actual_hash:
        raise FlowError("scope-baseline.mdがオーナー固定後に変更されています。再固定が必要です")
    requirements, errors = parse_scope_baseline(scope_file)
    if errors:
        raise FlowError("スコープ基準が不正です:\n- " + "\n- ".join(errors))
    if lock.get("requirements") != requirements:
        raise FlowError("スコープ基準の解析結果が固定時と一致しません")
    return lock


def events_dir(task_dir: Path) -> Path:
    return task_meta_dir(task_dir) / "events"


@contextlib.contextmanager
def task_lock(task_dir: Path) -> Iterator[None]:
    lock_path = task_meta_dir(task_dir) / "lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_policy(task_dir: Path) -> dict[str, Any]:
    path = policy_path(task_dir)
    if not path.is_file():
        raise FlowError(
            f"flowctl未初期化です: {task_dir}。PMが flowctl init を実行してください"
        )
    policy = read_json(path)
    if policy.get("schema_version") != SCHEMA_VERSION:
        raise FlowError("未対応のflowctl policy schemaです")
    return policy


def save_policy(task_dir: Path, policy: dict[str, Any]) -> None:
    atomic_write_json(policy_path(task_dir), policy)


def load_events(task_dir: Path) -> list[dict[str, Any]]:
    directory = events_dir(task_dir)
    if not directory.is_dir():
        return []
    events: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        event = read_json(path)
        if event.get("schema_version") != SCHEMA_VERSION:
            raise FlowError(f"未対応のevent schemaです: {path.name}")
        events.append(event)
    return sorted(events, key=lambda item: (item.get("at", ""), item.get("id", "")))


def current_state(events: Sequence[dict[str, Any]]) -> str | None:
    state: str | None = None
    for event in events:
        if event.get("kind") == "transition":
            state = event.get("data", {}).get("to")
    return state


def append_event(
    task_dir: Path,
    kind: str,
    *,
    role: str | None = None,
    provider: str | None = None,
    session_id: str | None = None,
    data: dict[str, Any] | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    forbidden = {"prompt", "transcript", "credential", "password", "token", "secret"}
    payload = data or {}
    if forbidden.intersection(key.lower() for key in payload):
        raise FlowError("プロンプト、会話本文、認証情報はイベントへ保存できません")
    event_id = uuid.uuid4().hex
    timestamp = at or iso_now()
    event = {
        "schema_version": SCHEMA_VERSION,
        "id": event_id,
        "at": timestamp,
        "kind": kind,
        "role": role,
        "provider": provider,
        "session_id": session_id,
        "data": payload,
    }
    stamp = timestamp.replace(":", "").replace("-", "").replace(".", "")
    path = events_dir(task_dir) / f"{stamp}-{event_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(event, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    refresh_derived_files(task_dir)
    return event


def transition(
    task_dir: Path,
    expected: set[str | None],
    destination: str,
    *,
    role: str,
    reason: str,
    provider: str | None = None,
    session_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    events = load_events(task_dir)
    state = current_state(events)
    if state not in expected:
        expected_text = ", ".join("未初期化" if item is None else item for item in expected)
        raise FlowError(
            f"不正な工程遷移です: 現在={state or '未初期化'}、必要={expected_text}、次={destination}"
        )
    data: dict[str, Any] = {"from": state, "to": destination, "reason": reason}
    if extra:
        data.update(extra)
    return append_event(
        task_dir,
        "transition",
        role=role,
        provider=provider,
        session_id=session_id,
        data=data,
    )


def is_secret_path(relative_path: str) -> bool:
    normalized = normalize_relative(relative_path)
    parts = normalized.split("/")
    basename = parts[-1] if parts else normalized
    if basename == ".env.example" or basename.endswith(".env.example"):
        return False
    candidates = {normalized, basename}
    return any(
        fnmatch.fnmatch(candidate.lower(), pattern.lower())
        for candidate in candidates
        for pattern in SECRET_PATH_PATTERNS
    )


def path_matches(path: str, patterns: Sequence[str]) -> bool:
    normalized = normalize_relative(path)
    return any(fnmatch.fnmatch(normalized, normalize_relative(pattern)) for pattern in patterns)


def glob_is_within(pattern: str, allowed_patterns: Sequence[str]) -> bool:
    candidate = normalize_relative(pattern)
    for allowed in allowed_patterns:
        maximum = normalize_relative(allowed)
        if candidate == maximum:
            return True
        if maximum.endswith("/**"):
            prefix = maximum[:-3].rstrip("/")
            if candidate == prefix or candidate.startswith(prefix + "/"):
                return True
    return False


def is_formal_doc(path: str, policy: dict[str, Any] | None = None) -> bool:
    normalized = normalize_relative(path)
    if normalized == "docs/flow" or normalized.startswith("docs/flow/"):
        return False
    patterns = list(DEFAULT_FORMAL_DOC_GLOBS)
    if policy:
        patterns.extend(policy.get("formal_doc_globs", []))
    return path_matches(normalized, patterns)


def is_generated_doc(path: str, policy: dict[str, Any] | None) -> bool:
    return bool(policy and path_matches(path, policy.get("generated_doc_globs", [])))


def flow_artifact_allowed(role: str, relative_path: str) -> bool:
    normalized = normalize_relative(relative_path)
    if not normalized.startswith("docs/flow/"):
        return False
    if "/.ai-devteam/" in f"/{normalized}/" or normalized.endswith("/.ai-devteam"):
        return False
    name = Path(normalized).name
    if role == "tl":
        return "/tech-lead/" in f"/{normalized}"
    if role == "auditor-codex":
        return bool(re.fullmatch(r"audit-codex(?:-[1-9][0-9]*)?\.md", name))
    if role == "auditor-claude":
        return bool(re.fullmatch(r"audit-claude(?:-[1-9][0-9]*)?\.md", name))
    if role == "pm" and "/tech-lead/" in f"/{normalized}":
        return True
    return name in FLOW_OWNERS.get(role, set())


def check_role_write_state(role: str | None, task_dir: Path | None, relative_path: str) -> str | None:
    if role is None or task_dir is None or not policy_path(task_dir).is_file():
        return None
    state = current_state(load_events(task_dir))
    if state not in ROLE_WRITE_STATES.get(role, set()):
        return f"{role}は現在工程「{state}」ではファイルを変更できません"
    normalized = normalize_relative(relative_path)
    if role == "implementer" and state == "implementation_preflight":
        if not (
            normalized.startswith("docs/flow/")
            and Path(normalized).name in {"pre-summary.md", "loop-state.md"}
        ):
            return "実装前サマリのオーナー承認前はプロダクト差分を変更できません"
    return None


def check_write_path(
    role: str | None,
    relative_path: str,
    policy: dict[str, Any] | None = None,
    root: Path | None = None,
    task_dir: Path | None = None,
) -> str | None:
    if role is None:
        return None
    raw_path = str(relative_path).replace("\\", "/")
    if Path(raw_path).is_absolute() or raw_path == ".." or raw_path.startswith("../"):
        return f"管理対象プロジェクト外は変更できません: {raw_path}"
    path = normalize_relative(relative_path)
    if is_secret_path(path):
        return f"秘密情報を含み得るパスは読み書き禁止です: {path}"
    if path in {"AGENTS.md", "CLAUDE.md"} or path.startswith((".codex/", ".claude/")):
        return f"プロジェクト規約・エージェント設定はオーナー管理です: {path}"
    if "/.ai-devteam/" in f"/{path}/" or path.startswith(".ai-devteam/"):
        return f"flowctl内部状態は直接編集できません: {path}"
    if Path(path).name == "scope-baseline.md" and root is not None:
        lock = load_scope_lock(root / path)
        if lock is not None:
            return "scope-baseline.mdはオーナー固定済みです。変更にはオーナーによるunlockが必要です"
    if path == "docs/flow" or path.startswith("docs/flow/"):
        if not flow_artifact_allowed(role, path):
            return f"{role} が所有しない工程成果物は変更できません: {path}"
        if task_dir is not None and root is not None:
            try:
                relative_task = task_dir.resolve().relative_to(root.resolve()).as_posix()
            except ValueError:
                return "関連付けたtask-dirが管理対象プロジェクト外です"
            feature = Path(relative_task).parent.as_posix()
            parent = Path(path).parent.as_posix()
            if role == "pm":
                feature_files = {
                    f"{feature}/scope-baseline.md",
                    f"{feature}/spec.md",
                    f"{feature}/tasks.md",
                }
                in_scope = (
                    path in feature_files
                    or path.startswith(f"{feature}/tech-lead/")
                    or (parent == relative_task and Path(path).name in FLOW_OWNERS["pm"])
                )
            elif role == "tl":
                in_scope = path.startswith(f"{feature}/tech-lead/")
            elif role == "implementer":
                in_scope = parent == relative_task and Path(path).name in FLOW_OWNERS["implementer"]
            elif role == "auditor-codex":
                in_scope = parent == relative_task and bool(
                    re.fullmatch(r"audit-codex(?:-[1-9][0-9]*)?\.md", Path(path).name)
                )
            elif role == "auditor-claude":
                in_scope = parent == relative_task and bool(
                    re.fullmatch(r"audit-claude(?:-[1-9][0-9]*)?\.md", Path(path).name)
                )
            else:
                in_scope = False
            if not in_scope:
                return f"この独立セッションに関連付けたtask・機能外の工程成果物です: {path}"
        return None
    if role == "pm":
        if is_formal_doc(path, policy):
            if policy is not None:
                maximum = policy.get("scope_requirement", {}).get("write_globs", [])
                if maximum and not path_matches(path, maximum):
                    return f"オーナー固定済み変更パス外の正式ドキュメントです: {path}"
            return None
        return f"PMはプロダクトコード・テスト・設定を変更できません: {path}"
    if role in {"tl", "auditor-codex", "auditor-claude"}:
        return f"{role} は工程成果物以外を変更できません: {path}"
    if role == "implementer":
        if is_formal_doc(path, policy) and not is_generated_doc(path, policy):
            return f"正式ドキュメントはPM所有です: {path}"
        if policy is None:
            return "flowctlのtask policyへ関連付けるまで実装できません"
        allowed = policy.get("allowed_write_globs", [])
        if not allowed:
            return "指示書品質ゲートで変更許可パスが固定されるまで実装できません"
        if not path_matches(path, allowed):
            return f"instruction.mdで許可されていない変更パスです: {path}"
        return None
    return f"不明な役割です: {role}"


def check_capability_write(
    role: str | None,
    relative_path: str,
    capabilities: set[str],
) -> str | None:
    if role != "implementer":
        return None
    path = normalize_relative(relative_path)
    basename = Path(path).name.lower()
    if (
        basename in DEPENDENCY_MANIFESTS
        or basename.startswith("requirements") and basename.endswith(".txt")
    ) and "dependency-install" not in capabilities:
        return "依存関係ファイルの変更にはオーナーによる dependency-install の一時許可が必要です"
    parts = {part.lower() for part in Path(path).parts}
    if ("migrations" in parts or basename == "schema.prisma") and "migration" not in capabilities:
        return "schema・migrationファイルの変更にはオーナーによる migration の一時許可が必要です"
    return None


def extract_patch_paths(command: str) -> list[str]:
    return re.findall(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", command, re.MULTILINE)


def extract_tool_paths(tool_name: str, tool_input: dict[str, Any]) -> list[str]:
    values: list[str] = []
    if tool_name == "apply_patch":
        patch_text = tool_input.get("command") or tool_input.get("patch") or tool_input.get("input") or ""
        return extract_patch_paths(str(patch_text))
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            values.append(value)
    return values


def relative_to_root(path: str, root: Path, cwd: Path) -> str:
    expanded = Path(path).expanduser()
    absolute = expanded if expanded.is_absolute() else cwd / expanded
    try:
        return absolute.resolve(strict=False).relative_to(root).as_posix()
    except ValueError:
        return absolute.resolve(strict=False).as_posix()


def current_capabilities(task_dir: Path | None, role: str | None = None) -> set[str]:
    if task_dir is None or not policy_path(task_dir).is_file():
        return set()
    now = utc_now()
    granted: dict[tuple[str, str], dt.datetime] = {}
    for event in load_events(task_dir):
        if event.get("kind") == "capability_granted":
            capability = str(event.get("data", {}).get("capability", ""))
            granted_role = str(event.get("data", {}).get("granted_role", "implementer"))
            expires = str(event.get("data", {}).get("expires_at", ""))
            with contextlib.suppress(ValueError):
                granted[(granted_role, capability)] = parse_time(expires)
        elif event.get("kind") == "capability_revoked":
            capability = str(event.get("data", {}).get("capability", ""))
            revoked_role = str(event.get("data", {}).get("granted_role", "implementer"))
            granted.pop((revoked_role, capability), None)
    return {
        capability
        for (granted_role, capability), expires in granted.items()
        if expires > now and granted_role == role
    }


def check_bash_command(
    command: str,
    role: str | None,
    capabilities: set[str],
) -> str | None:
    if role is None:
        return None
    if SECRET_VALUE_PATTERN.search(command):
        return "コマンドに秘密情報らしい値が含まれるため実行できません"
    lowered = command.lower()
    if any(
        token in lowered
        for token in (".env", "id_rsa", "id_ed25519", ".pem", "credentials", "secret_key")
    ) and ".env.example" not in lowered:
        return "秘密情報を含み得るファイルや値をコマンドから参照できません"
    if GIT_MUTATION.search(command) or GH_MUTATION.search(command):
        return "git変更操作はオーナーだけが実行できます"
    if HARD_DENY_COMMAND.search(command):
        return "破壊的または外部状態を変更するコマンドはAIセッションから実行できません"
    if INLINE_INTERPRETER_COMMAND.search(command):
        return "インラインスクリプトはパス検査を回避できるためAIセッションでは実行できません"
    if PRODUCTION_COMMAND.search(command):
        return "本番・共有環境・実credentialを使う操作は常時禁止です"
    if NETWORK_COMMAND.search(command) and "network" not in capabilities:
        return "外部ネットワーク操作にはオーナーがflowctlで付与した一時許可が必要です"
    if DEPENDENCY_INSTALL_COMMAND.search(command) and "dependency-install" not in capabilities:
        return "依存関係変更にはオーナーがflowctlで付与した一時許可が必要です"
    if MIGRATION_COMMAND.search(command):
        required = {"isolated-db", "migration"}
        if not required.issubset(capabilities):
            return "migration実行にはオーナーによる isolated-db と migration の一時許可が必要です"
    if DATABASE_COMMAND.search(command) and "isolated-db" not in capabilities:
        return "DB接続にはオーナーによる isolated-db の一時許可が必要です"
    shell_write = bool(DIRECT_FILE_WRITE_COMMAND.search(command))
    if not shell_write:
        with contextlib.suppress(ValueError):
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
            lexer.whitespace_split = True
            shell_write = any(">" in token and set(token).issubset({">", "&"}) for token in lexer)
    if shell_write:
        return "シェル経由の直接ファイル変更は禁止です。パス検査される編集ツールを使ってください"
    return None


def check_external_tool(tool_name: str, role: str | None) -> str | None:
    if role is None:
        return None
    lowered = tool_name.lower()
    if lowered in {"bash", "exec_command", "apply_patch", "edit", "write", "read"}:
        return None
    mutation_words = ("create", "update", "delete", "remove", "send", "post", "put", "write", "deploy")
    if any(word in lowered for word in mutation_words):
        return "外部ツールによる状態変更はai-devteam役割セッションから実行できません"
    return None


def parse_instruction(task_dir: Path, policy: dict[str, Any]) -> tuple[list[str], list[str]]:
    instruction = task_dir / "instruction.md"
    if not instruction.is_file():
        return ["instruction.mdが存在しません"], []
    text = instruction.read_text(encoding="utf-8")
    errors: list[str] = []
    expected_risk = RISK_LEVELS.get(str(policy.get("risk_level")))
    risk_match = re.findall(r"^\s*[-*]?\s*リスク区分\s*[:：]\s*(低|標準|高)\s*$", text, re.MULTILINE)
    if risk_match != [expected_risk]:
        errors.append(f"instruction.mdの『リスク区分』を1行だけ『{expected_risk}』で記載してください")
    for field, expected in (("主要な外部挙動数", "1"),):
        values = re.findall(rf"^\s*[-*]?\s*{field}\s*[:：]\s*([^\s]+)\s*$", text, re.MULTILINE)
        if values != [expected]:
            errors.append(f"instruction.mdの『{field}』は{expected}にしてください")
    irreversible = re.findall(r"^\s*[-*]?\s*不可逆境界数\s*[:：]\s*([0-9]+)\s*$", text, re.MULTILINE)
    if len(irreversible) != 1 or irreversible[0] not in {"0", "1"}:
        errors.append("instruction.mdの『不可逆境界数』は0または1を1行だけ記載してください")
    extracted: dict[str, str] = {}
    for field in ("主要な外部挙動", "不可逆境界", "主要要求ID", "リスク領域"):
        values = re.findall(rf"^\s*[-*]?\s*{field}\s*[:：]\s*(.+)$", text, re.MULTILINE)
        if len(values) != 1 or not values[0].strip():
            errors.append(f"instruction.mdに『{field}』を1行だけ具体的に記載してください")
        else:
            extracted[field] = values[0].strip()

    scope_requirement = policy.get("scope_requirement")
    if isinstance(scope_requirement, dict):
        expected_id = str(scope_requirement.get("id", ""))
        if extracted.get("主要要求ID") != expected_id:
            errors.append(f"主要要求IDはオーナー固定済みの『{expected_id}』と一致させてください")
        if extracted.get("主要な外部挙動") != scope_requirement.get("outcome"):
            errors.append("主要な外部挙動はscope-baseline.mdの承認済み外部成果と同一文にしてください")
        declared_domains = {
            item.strip()
            for item in re.split(r"<br\s*/?>|、|,", extracted.get("リスク領域", ""))
            if item.strip()
        }
        approved_domains = set(scope_requirement.get("risk_domains", []))
        if not declared_domains.issubset(approved_domains):
            extra = "、".join(sorted(declared_domains - approved_domains))
            errors.append(f"オーナー固定範囲にないリスク領域があります: {extra}")

    preflight = re.findall(
        r"^\s*[-*]?\s*実装前内部検証\s*[:：]\s*(必須|不要)\s*$", text, re.MULTILINE
    )
    if len(preflight) != 1:
        errors.append("『実装前内部検証: 必須』または『実装前内部検証: 不要』を1行だけ記載してください")
    elif policy.get("pre_evaluator_required") and preflight[0] != "必須":
        errors.append("高リスクまたは複合タスクでは実装前内部検証を必須にしてください")

    header_pattern = re.compile(
        r"\|\s*受け入れ条件\s*\|\s*外部から観測できる期待結果\s*\|\s*検証方法\s*\|"
    )
    header_match = header_pattern.search(text)
    if not header_match:
        errors.append("受け入れ条件・外部から観測できる期待結果・検証方法の3列表が必要です")
    else:
        rows = []
        for line in text[header_match.end() :].splitlines()[1:]:
            if not line.lstrip().startswith("|"):
                break
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) == 3 and not all(re.fullmatch(r"[-: ]+", cell or "-") for cell in cells):
                rows.append(cells)
        if not rows:
            errors.append("受け入れ条件表に1件以上のデータ行が必要です")
        else:
            identifiers = [row[0] for row in rows]
            if len(set(identifiers)) != len(identifiers):
                errors.append("受け入れ条件の番号はタスク内で一意にしてください")
            for index, row in enumerate(rows, start=1):
                if any(not cell for cell in row):
                    errors.append(f"受け入れ条件表のデータ行{index}に空欄があります")

    positive_high_risk_lines = []
    for line in text.splitlines():
        lowered = line.lower()
        if re.search(r"(?:リスク区分|不可逆境界数|実装前内部検証)\s*[:：]", lowered):
            continue
        if any(word.lower() in lowered for word in HIGH_RISK_WORDS) and not any(
            negative in lowered for negative in NEGATIVE_WORDS
        ):
            positive_high_risk_lines.append(line.strip())
    if positive_high_risk_lines and policy.get("risk_level") != "high":
        errors.append("高リスク要素が肯定形で記載されています。リスク区分を高にするか、PMが内容を修正してください")

    section = re.search(
        r"^## 実装担当の変更許可パス\s*$\n(?P<body>.*?)(?=^##\s|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    allowed: list[str] = []
    if section:
        for match in re.finditer(r"^\s*[-*]\s*`([^`]+)`\s*$", section.group("body"), re.MULTILINE):
            pattern = normalize_relative(match.group(1))
            if pattern and ".." not in Path(pattern).parts and not Path(pattern).is_absolute():
                allowed.append(pattern)
    if not allowed:
        errors.append("『## 実装担当の変更許可パス』に相対パスglobを1件以上記載してください")
    for pattern in allowed:
        if is_formal_doc(pattern, policy) and not is_generated_doc(pattern, policy):
            errors.append(f"正式ドキュメントを実装担当の許可パスに含められません: {pattern}")
        if isinstance(scope_requirement, dict) and not glob_is_within(
            pattern, scope_requirement.get("write_globs", [])
        ):
            errors.append(f"scope-baseline.mdの変更可能パスを越えています: {pattern}")
    return errors, sorted(set(allowed))


def task_git_diff_files(task_dir: Path, policy: dict[str, Any]) -> list[str]:
    root = git_root(task_dir)
    base = str(policy.get("base_commit", ""))
    files = set(
        line.strip()
        for line in git_output(root, "diff", "--name-only", base).splitlines()
        if line.strip()
    )
    files.update(
        line.strip()
        for line in git_output(root, "ls-files", "--others", "--exclude-standard").splitlines()
        if line.strip()
    )
    return sorted(files)


def implementation_change_size(task_dir: Path, policy: dict[str, Any]) -> tuple[int, int]:
    root = git_root(task_dir)
    base = str(policy.get("base_commit", ""))
    line_counts: dict[str, int] = {}
    for line in git_output(root, "diff", "--numstat", base).splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if path.startswith("docs/flow/"):
            continue
        count = 1 if added == "-" or deleted == "-" else int(added) + int(deleted)
        line_counts[path] = count
    for path in task_git_diff_files(task_dir, policy):
        if path in line_counts or path.startswith("docs/flow/"):
            continue
        candidate = root / path
        if candidate.is_file() and not is_secret_path(path):
            content = candidate.read_bytes()
            line_counts[path] = max(1, content.count(b"\n") + (0 if content.endswith(b"\n") else 1))
        else:
            line_counts[path] = 1
    return len(line_counts), sum(line_counts.values())


def validate_change_budget(task_dir: Path, policy: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    requirement = policy.get("scope_requirement", {})
    file_count, changed_lines = implementation_change_size(task_dir, policy)
    max_files = int(requirement.get("max_files", 0) or 0)
    max_lines = int(requirement.get("max_changed_lines", 0) or 0)
    if max_files and file_count > max_files:
        errors.append(f"固定済み変更上限を超えています: {file_count}ファイル > {max_files}ファイル")
    if max_lines and changed_lines > max_lines:
        errors.append(f"固定済み変更上限を超えています: {changed_lines}行 > {max_lines}行")
    return errors


def validate_pm_formal_scope(task_dir: Path, policy: dict[str, Any]) -> list[str]:
    maximum = policy.get("scope_requirement", {}).get("write_globs", [])
    errors: list[str] = []
    for path in task_git_diff_files(task_dir, policy):
        if path.startswith("docs/flow/"):
            continue
        if is_formal_doc(path, policy) and not is_generated_doc(path, policy):
            if not maximum or not path_matches(path, maximum):
                errors.append(f"オーナー固定済み変更パス外の正式ドキュメント差分です: {path}")
    errors.extend(validate_change_budget(task_dir, policy))
    return errors


def validate_implementation_scope(task_dir: Path, policy: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    root = git_root(task_dir)
    branch = git_output(root, "branch", "--show-current").strip()
    if branch != policy.get("branch"):
        errors.append(f"ブランチが不一致です: 現在={branch or 'detached'}、指定={policy.get('branch')}")
    allowed = policy.get("allowed_write_globs", [])
    for path in task_git_diff_files(task_dir, policy):
        if path in {"AGENTS.md", "CLAUDE.md"} or path.startswith("docs/flow/"):
            continue
        if is_secret_path(path):
            errors.append(f"秘密情報を含み得るファイルが差分にあります（内容は開きません）: {path}")
            continue
        if is_formal_doc(path, policy) and not is_generated_doc(path, policy):
            expected = policy.get("pm_formal_doc_snapshots", {}).get(path)
            candidate = root / path
            actual = sha256_file(candidate) if candidate.is_file() else "deleted"
            if expected == actual:
                continue
            errors.append(f"PM確認済みスナップショットと一致しない正式ドキュメント差分があります: {path}")
            continue
        if allowed and not path_matches(path, allowed):
            errors.append(f"変更許可パス外の差分です: {path}")
    errors.extend(validate_change_budget(task_dir, policy))
    return errors


def snapshot_formal_docs(task_dir: Path, policy: dict[str, Any]) -> dict[str, str]:
    root = git_root(task_dir)
    snapshots: dict[str, str] = {}
    for path in task_git_diff_files(task_dir, policy):
        if is_formal_doc(path, policy) and not is_generated_doc(path, policy):
            candidate = root / path
            snapshots[path] = sha256_file(candidate) if candidate.is_file() else "deleted"
    return snapshots


def product_diff_digest(task_dir: Path, policy: dict[str, Any]) -> str:
    root = git_root(task_dir)
    base = str(policy.get("base_commit"))
    paths = [
        path
        for path in task_git_diff_files(task_dir, policy)
        if not path.startswith("docs/flow/")
        and (not is_formal_doc(path, policy) or is_generated_doc(path, policy))
    ]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.encode("utf-8"))
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", path],
            cwd=root,
            capture_output=True,
            check=False,
        ).returncode == 0
        if tracked:
            content = git_output(root, "diff", "--binary", base, "--", path).encode("utf-8")
        else:
            candidate = root / path
            content = candidate.read_bytes() if candidate.is_file() else b""
        digest.update(content)
    return digest.hexdigest()


def _working_tree_snapshot(root: Path, paths: Sequence[str]) -> dict[str, str]:
    snapshots: dict[str, str] = {}
    for path in sorted(set(paths)):
        candidate = root / path
        if candidate.is_symlink():
            content = os.readlink(candidate).encode("utf-8", errors="surrogateescape")
            kind = b"symlink\0"
        elif candidate.is_file():
            content = candidate.read_bytes()
            executable = bool(candidate.stat().st_mode & 0o111)
            kind = b"file-executable\0" if executable else b"file\0"
        else:
            snapshots[path] = "deleted"
            continue
        snapshots[path] = hashlib.sha256(kind + content).hexdigest()
    return snapshots


def snapshot_candidate_changes(task_dir: Path, policy: dict[str, Any]) -> dict[str, str]:
    root = git_root(task_dir)
    paths = [path for path in task_git_diff_files(task_dir, policy) if not path.startswith("docs/flow/")]
    return _working_tree_snapshot(root, paths)


def snapshot_committed_changes(task_dir: Path, policy: dict[str, Any], head: str) -> dict[str, str]:
    root = git_root(task_dir)
    base = str(policy.get("base_commit"))
    paths = [
        line.strip()
        for line in git_output(root, "diff", "--name-only", f"{base}..{head}").splitlines()
        if line.strip() and not line.startswith("docs/flow/")
    ]
    snapshots: dict[str, str] = {}
    for path in sorted(set(paths)):
        tree = subprocess.run(
            ["git", "ls-tree", head, "--", path],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        if tree.returncode != 0 or not tree.stdout.strip():
            snapshots[path] = "deleted"
            continue
        mode = tree.stdout.split(maxsplit=1)[0]
        content = subprocess.run(
            ["git", "show", f"{head}:{path}"],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if content.returncode != 0:
            snapshots[path] = "unreadable"
            continue
        kind = b"symlink\0" if mode == "120000" else (b"file-executable\0" if mode == "100755" else b"file\0")
        snapshots[path] = hashlib.sha256(kind + content.stdout).hexdigest()
    return snapshots


def latest_event(events: Sequence[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    return next((event for event in reversed(events) if event.get("kind") == kind), None)


def current_audit_round(events: Sequence[dict[str, Any]]) -> int:
    commits = sum(1 for event in events if event.get("kind") == "commit_recorded")
    return max(1, commits)


def audit_results_for_round(events: Sequence[dict[str, Any]], round_number: int) -> dict[str, str]:
    results: dict[str, str] = {}
    for event in events:
        if event.get("kind") != "audit_result":
            continue
        data = event.get("data", {})
        if data.get("round") == round_number:
            results[str(data.get("auditor"))] = str(data.get("result"))
    return results


def required_auditors(policy: dict[str, Any]) -> list[str]:
    if int(policy.get("audit_count", 2)) == 1:
        return [str(policy.get("single_auditor"))]
    return ["codex", "claude"]


def calculate_metrics(task_dir: Path, now: dt.datetime | None = None) -> dict[str, Any]:
    policy = load_policy(task_dir)
    events = load_events(task_dir)
    current = now or utc_now()
    start_event = next((event for event in events if event.get("kind") == "task_initialized"), None)
    closed_event = latest_event(events, "owner_closed")
    start_time = parse_time(start_event["at"]) if start_event else current
    end_time = parse_time(closed_event["at"]) if closed_event else current
    cycle_seconds = max(0.0, (end_time - start_time).total_seconds())

    starts: dict[str, tuple[dt.datetime, str]] = {}
    intervals: list[tuple[dt.datetime, dt.datetime]] = []
    session_ids: set[str] = set()
    for event in events:
        if event.get("kind") == "session_started":
            span = str(event.get("data", {}).get("span_id", event.get("id")))
            starts[span] = (parse_time(event["at"]), str(event.get("session_id") or span))
            session_ids.add(str(event.get("session_id") or span))
        elif event.get("kind") == "session_ended":
            span = str(event.get("data", {}).get("span_id", ""))
            if span in starts:
                began, _ = starts.pop(span)
                intervals.append((began, parse_time(event["at"])))
    for began, _ in starts.values():
        intervals.append((began, end_time))

    clipped = sorted(
        (max(start_time, begin), min(end_time, finish))
        for begin, finish in intervals
        if finish > start_time and begin < end_time and finish > begin
    )
    merged: list[list[dt.datetime]] = []
    for begin, finish in clipped:
        if not merged or begin > merged[-1][1]:
            merged.append([begin, finish])
        elif finish > merged[-1][1]:
            merged[-1][1] = finish
    active_seconds = sum((finish - begin).total_seconds() for begin, finish in merged)
    active_seconds = min(cycle_seconds, max(0.0, active_seconds))

    submissions = sum(1 for event in events if event.get("kind") == "implementation_submitted")
    returns = sum(1 for event in events if event.get("kind") == "pm_returned")
    round_one = audit_results_for_round(events, 1)
    required = required_auditors(policy)
    if not all(auditor in round_one for auditor in required):
        first_audit_pass: bool | None = None
    else:
        first_audit_pass = all(round_one[auditor] == "pass" for auditor in required)

    transitions = [event for event in events if event.get("kind") == "transition"]
    stage_seconds: dict[str, float] = {}
    for index, event in enumerate(transitions):
        state = str(event.get("data", {}).get("to", "unknown"))
        began = parse_time(event["at"])
        finished = parse_time(transitions[index + 1]["at"]) if index + 1 < len(transitions) else end_time
        stage_seconds[state] = stage_seconds.get(state, 0.0) + max(0.0, (finished - began).total_seconds())

    return {
        "schema_version": SCHEMA_VERSION,
        "state": current_state(events),
        "risk_level": policy.get("risk_level"),
        "audit_count": policy.get("audit_count"),
        "cycle_seconds": round(cycle_seconds, 3),
        "active_seconds": round(active_seconds, 3),
        "wait_seconds": round(max(0.0, cycle_seconds - active_seconds), 3),
        "session_count": len(session_ids),
        "implementation_submissions": submissions,
        "pm_returns": returns,
        "pm_return_rate": round(returns / submissions, 4) if submissions else None,
        "first_audit_pass": first_audit_pass,
        "stage_seconds": {key: round(value, 3) for key, value in sorted(stage_seconds.items())},
        "updated_at": current.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}時間{minutes}分{secs}秒"


def metrics_markdown(metrics: dict[str, Any]) -> str:
    first_pass = metrics["first_audit_pass"]
    first_pass_text = "未確定" if first_pass is None else ("合格" if first_pass else "不合格")
    return "\n".join(
        (
            "# ai-devteam 自動メトリクス",
            "",
            f"- 現在工程: {metrics['state']}",
            f"- 経過時間: {format_duration(metrics['cycle_seconds'])}",
            f"- セッション稼働時間（重複除外）: {format_duration(metrics['active_seconds'])}",
            f"- 待ち時間: {format_duration(metrics['wait_seconds'])}",
            f"- 独立セッション数: {metrics['session_count']}",
            f"- 実装提出回数: {metrics['implementation_submissions']}",
            f"- PM差し戻し回数: {metrics['pm_returns']}",
            f"- PM差し戻し率: {metrics['pm_return_rate'] if metrics['pm_return_rate'] is not None else '未確定'}",
            f"- 初回監査合格: {first_pass_text}",
            "",
            "プロンプト本文、会話ログ、秘密情報、認証情報は記録しない。",
            "",
        )
    )


def refresh_derived_files(task_dir: Path) -> None:
    if not policy_path(task_dir).is_file():
        return
    events = load_events(task_dir)
    policy = load_policy(task_dir)
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "state": current_state(events),
        "risk_level": policy.get("risk_level"),
        "audit_count": policy.get("audit_count"),
        "required_auditors": required_auditors(policy),
        "event_count": len(events),
        "updated_at": events[-1]["at"] if events else policy.get("created_at"),
    }
    atomic_write_json(task_meta_dir(task_dir) / "state.json", snapshot)
    metrics = calculate_metrics(task_dir)
    atomic_write_json(task_meta_dir(task_dir) / "metrics.json", metrics)
    atomic_write_text(task_meta_dir(task_dir) / "metrics.md", metrics_markdown(metrics))


def runtime_sessions_dir() -> Path:
    return Path.home() / ".ai-devteam" / "runtime" / "sessions"


def runtime_session_path(provider: str, session_id: str) -> Path:
    digest = hashlib.sha256(f"{provider}:{session_id}".encode("utf-8")).hexdigest()
    return runtime_sessions_dir() / f"{digest}.json"


def load_runtime_session(provider: str, session_id: str) -> dict[str, Any] | None:
    path = runtime_session_path(provider, session_id)
    return read_json(path) if path.is_file() else None


def save_runtime_session(provider: str, session_id: str, value: dict[str, Any]) -> None:
    atomic_write_json(runtime_session_path(provider, session_id), value)


def parse_role_start_command(command: str) -> tuple[str, Path | None] | None:
    if re.search(r"[;&|><`]", command):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None
    index = 0
    if Path(tokens[0]).name.startswith("python"):
        if len(tokens) < 2:
            return None
        index = 1
    executable = Path(tokens[index].replace("$HOME", str(Path.home()))).name
    if executable not in {"flowctl", "flowctl.py"}:
        return None
    rest = tokens[index + 1 :]
    if not rest or rest[0] != "role-start":
        return None
    role: str | None = None
    task_dir: Path | None = None
    cursor = 1
    while cursor < len(rest):
        if rest[cursor] == "--role" and cursor + 1 < len(rest):
            role = rest[cursor + 1]
            cursor += 2
        elif rest[cursor] == "--task-dir" and cursor + 1 < len(rest):
            task_dir = Path(rest[cursor + 1])
            cursor += 2
        elif rest[cursor] == "--project-root" and cursor + 1 < len(rest):
            cursor += 2
        else:
            return None
    if role not in ROLES:
        return None
    return role, task_dir


def parse_flowctl_invocation(command: str) -> tuple[str, Path | None] | None:
    if re.search(r"[;&|><`]", command):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None
    index = 0
    if Path(tokens[0]).name.startswith("python"):
        if len(tokens) < 3:
            return None
        index = 1
    if Path(tokens[index].replace("$HOME", str(Path.home()))).name not in {"flowctl", "flowctl.py"}:
        return None
    if len(tokens) <= index + 1:
        return None
    subcommand = tokens[index + 1]
    task_dir: Path | None = None
    with contextlib.suppress(ValueError, IndexError):
        task_index = tokens.index("--task-dir", index + 2)
        task_dir = Path(tokens[task_index + 1])
    return subcommand, task_dir


def parse_flowctl_command(command: str) -> str | None:
    parsed = parse_flowctl_invocation(command)
    return parsed[0] if parsed else None


def command_option(command: str, name: str) -> str | None:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    for index, token in enumerate(tokens):
        if token == name and index + 1 < len(tokens):
            return tokens[index + 1]
        if token.startswith(name + "="):
            return token.split("=", 1)[1]
    return None


def register_runtime_role(
    provider: str,
    session_id: str,
    cwd: Path,
    root: Path,
    role: str,
    task_dir: Path | None,
) -> str | None:
    record = load_runtime_session(provider, session_id) or {
        "schema_version": SCHEMA_VERSION,
        "provider": provider,
        "session_id": session_id,
        "root": str(root),
        "started_at": iso_now(),
        "span_id": uuid.uuid4().hex,
        "event_recorded": False,
    }
    existing_role = record.get("role")
    if existing_role and existing_role != role:
        return f"この独立セッションは既に{existing_role}です。{role}へ役割変更できません"
    record["role"] = role
    if task_dir is not None:
        resolved_task = (cwd / task_dir).resolve() if not task_dir.is_absolute() else task_dir.resolve()
        try:
            resolved_task.relative_to(root)
        except ValueError:
            return "task-dirは管理対象プロジェクト内に限定してください"
        existing_task = record.get("task_dir")
        if existing_task and Path(existing_task).resolve() != resolved_task:
            previous_task = Path(existing_task).resolve()
            same_feature = previous_task.parent == resolved_task.parent
            if role not in {"pm", "implementer"} or not same_feature:
                return "同じ独立セッションは、PM・実装担当が同じ機能内のtaskへ移る場合だけ再利用できます"
            if record.get("event_recorded") and policy_path(previous_task).is_file():
                with task_lock(previous_task):
                    append_event(
                        previous_task,
                        "session_ended",
                        role=role,
                        provider=provider,
                        session_id=session_id,
                        data={"span_id": record.get("span_id"), "reason": "same-feature-task-switch"},
                    )
            record["started_at"] = iso_now()
            record["span_id"] = uuid.uuid4().hex
            record["event_recorded"] = False
            record.pop("ended_at", None)
        record["task_dir"] = str(resolved_task)
        if policy_path(resolved_task).is_file() and not record.get("event_recorded"):
            with task_lock(resolved_task):
                append_event(
                    resolved_task,
                    "session_started",
                    role=role,
                    provider=provider,
                    session_id=session_id,
                    data={"span_id": record["span_id"]},
                    at=record.get("started_at"),
                )
            record["event_recorded"] = True
    save_runtime_session(provider, session_id, record)
    return None


def end_runtime_session(provider: str, session_id: str, at: str | None = None) -> None:
    record = load_runtime_session(provider, session_id)
    if not record or not record.get("task_dir") or not record.get("event_recorded"):
        return
    task_dir = Path(record["task_dir"])
    if policy_path(task_dir).is_file() and not record.get("ended_at"):
        with task_lock(task_dir):
            append_event(
                task_dir,
                "session_ended",
                role=record.get("role"),
                provider=provider,
                session_id=session_id,
                data={"span_id": record.get("span_id")},
                at=at,
            )
        record["ended_at"] = at or iso_now()
        save_runtime_session(provider, session_id, record)


def find_task_from_runtime(record: dict[str, Any] | None) -> Path | None:
    if not record or not record.get("task_dir"):
        return None
    task = Path(str(record["task_dir"]))
    return task if policy_path(task).is_file() else None


def deny_output(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def handle_hook(payload: dict[str, Any], provider: str) -> dict[str, Any] | None:
    event = str(payload.get("hook_event_name", ""))
    session_id = str(payload.get("session_id", ""))
    cwd = Path(str(payload.get("cwd") or os.getcwd())).resolve()
    root = find_managed_root(cwd)
    if not session_id or root is None:
        return None
    if event == "SessionStart":
        existing = load_runtime_session(provider, session_id)
        if existing and existing.get("role"):
            existing["started_at"] = iso_now()
            existing["span_id"] = uuid.uuid4().hex
            existing["event_recorded"] = False
            existing.pop("ended_at", None)
            save_runtime_session(provider, session_id, existing)
            task = find_task_from_runtime(existing)
            if task:
                register_runtime_role(
                    provider,
                    session_id,
                    cwd,
                    root,
                    str(existing["role"]),
                    task,
                )
        # 役割を明示していない通常セッションはai-devteamへ登録しない。
        # role-startが最初に実行された時点から、そのセッションだけを制御する。
        return None
    if event == "SessionEnd":
        end_runtime_session(provider, session_id)
        return None
    if event != "PreToolUse":
        return None

    tool_name = str(payload.get("tool_name", ""))
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    record = load_runtime_session(provider, session_id)
    role = str(record.get("role")) if record and record.get("role") else None
    task_dir = find_task_from_runtime(record)
    capabilities = current_capabilities(task_dir, role)

    if tool_name in {"Bash", "exec_command"}:
        command = str(tool_input.get("command") or tool_input.get("cmd") or "")
        parsed = parse_role_start_command(command)
        if parsed:
            new_role, supplied_task = parsed
            reason = register_runtime_role(provider, session_id, cwd, root, new_role, supplied_task)
            return deny_output(reason) if reason else None
        invocation = parse_flowctl_invocation(command)
        flowctl_command = invocation[0] if invocation else None
        owner_commands = {
            "adopt",
            "approve",
            "close",
            "remove-legacy-claude-guards",
            "revoke",
            "scope-lock",
            "scope-unlock",
            "start-approve",
        }
        if flowctl_command in owner_commands:
            return deny_output("この工程承認・権限操作はオーナーが自分のターミナルから実行してください")
        if flowctl_command == "resume" and "--owner-confirmed" in command:
            return deny_output("停止指示からの再開承認はオーナーが自分のターミナルから実行してください")
        if role is None:
            if flowctl_command and flowctl_command not in INACTIVE_READ_ONLY_FLOWCTL_COMMANDS:
                return deny_output(
                    "ai-devteamはこの通常セッションでは無効です。役割Skillを明示してrole-startを完了してください"
                )
            return None
        if flowctl_command and flowctl_command not in ROLE_FLOWCTL_COMMANDS.get(role, set()):
            return deny_output(f"{role or '役割未登録'}には flowctl {flowctl_command} の実行権限がありません")
        if role in {"auditor-codex", "auditor-claude"} and flowctl_command in {
            "audit-start",
            "audit-result",
        }:
            expected_auditor = role.removeprefix("auditor-")
            if command_option(command, "--auditor") != expected_auditor:
                return deny_output(f"{role}は{expected_auditor}監査だけを登録できます")
        if invocation and task_dir is None and role == "pm" and flowctl_command == "init" and invocation[1] is not None:
            reason = register_runtime_role(provider, session_id, cwd, root, role, invocation[1])
            if reason:
                return deny_output(reason)
        if invocation and invocation[1] is not None and task_dir is not None:
            supplied = (cwd / invocation[1]).resolve() if not invocation[1].is_absolute() else invocation[1].resolve()
            if supplied != task_dir.resolve():
                same_feature_init = role == "pm" and flowctl_command == "init" and supplied.parent == task_dir.resolve().parent
                if not same_feature_init:
                    return deny_output("この独立セッションに関連付けたtask以外へflowctl操作はできません")
        reason = check_bash_command(command, role, capabilities)
        return deny_output(reason) if reason else None

    # 明示的なrole-start前は通常セッションであり、ai-devteam固有の
    # 書込み・パス・外部ツール制御を適用しない。
    if role is None:
        return None

    policy = load_policy(task_dir) if task_dir and policy_path(task_dir).is_file() else None
    mutating_tool = tool_name in {"apply_patch", "Edit", "Write", "MultiEdit", "NotebookEdit"}
    if mutating_tool:
        paths = extract_tool_paths(tool_name, tool_input)
        if not paths:
            return deny_output("変更対象パスを検査できないため書き込みを停止しました")
        for path in paths:
            relative = relative_to_root(path, root, cwd)
            reason = check_role_write_state(role, task_dir, relative)
            if reason:
                return deny_output(reason)
            reason = check_capability_write(role, relative, capabilities)
            if reason:
                return deny_output(reason)
            reason = check_write_path(role, relative, policy, root, task_dir)
            if reason:
                return deny_output(reason)
        return None

    paths = extract_tool_paths(tool_name, tool_input)
    for path in paths:
        relative = relative_to_root(path, root, cwd)
        if Path(relative).is_absolute() or relative == ".." or relative.startswith("../"):
            return deny_output(f"管理対象プロジェクト外は開けません: {relative}")
        if is_secret_path(relative):
            return deny_output(f"秘密情報を含み得るファイルは開けません: {relative}")
    reason = check_external_tool(tool_name, role)
    return deny_output(reason) if reason else None


def hook_entries(command: str) -> dict[str, list[dict[str, Any]]]:
    handler = {"type": "command", "command": command, "timeout": 10}
    return {
        "SessionStart": [{"hooks": [dict(handler)]}],
        "PreToolUse": [{"matcher": "*", "hooks": [dict(handler)]}],
        "SessionEnd": [{"hooks": [dict(handler)]}],
    }


def is_our_hook_group(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    for hook in group.get("hooks", []):
        if isinstance(hook, dict):
            command = str(hook.get("command", ""))
            if ".ai-devteam/bin/flowctl" in command or re.search(
                r"(?:^|/)flowctl(?:\.py)?['\"]?\s+hook\s+--provider\s+(?:codex|claude)\b",
                command,
            ):
                return True
    return False


def install_hooks(provider: str, executable: Path, config_path: Path) -> tuple[bool, Path | None]:
    if provider not in {"codex", "claude"}:
        raise FlowError("providerはcodexまたはclaudeです")
    if config_path.is_file():
        config = read_json(config_path)
    else:
        config = {}
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise FlowError(f"既存設定のhooksがobjectではありません: {config_path}")
    quoted = shlex.quote(str(executable.resolve()))
    desired = hook_entries(f"python3 {quoted} hook --provider {provider}")
    changed = False
    for event, groups in desired.items():
        existing = hooks.setdefault(event, [])
        if not isinstance(existing, list):
            raise FlowError(f"既存設定のhooks.{event}が配列ではありません")
        filtered = [group for group in existing if not is_our_hook_group(group)]
        replacement = filtered + groups
        if replacement != existing:
            hooks[event] = replacement
            changed = True
    backup: Path | None = None
    if changed:
        if config_path.is_file():
            stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
            backup = config_path.with_name(f"{config_path.name}.ai-devteam-backup-{stamp}")
            if not backup.exists():
                backup.write_bytes(config_path.read_bytes())
        atomic_write_json(config_path, config)
    return changed, backup


def legacy_claude_git_denies(config_path: Path) -> list[str]:
    if not config_path.is_file():
        return []
    config = read_json(config_path)
    permissions = config.get("permissions")
    if not isinstance(permissions, dict):
        return []
    deny = permissions.get("deny")
    if not isinstance(deny, list):
        return []
    return sorted(
        value
        for value in deny
        if isinstance(value, str) and value in LEGACY_CLAUDE_GIT_DENIES
    )


def legacy_claude_git_allows(config_path: Path) -> list[str]:
    if not config_path.is_file():
        return []
    config = read_json(config_path)
    permissions = config.get("permissions")
    if not isinstance(permissions, dict):
        return []
    allow = permissions.get("allow")
    if not isinstance(allow, list):
        return []
    return sorted(
        value
        for value in allow
        if isinstance(value, str) and value in LEGACY_CLAUDE_GIT_ALLOWS
    )


def remove_legacy_claude_git_permissions(
    config_path: Path,
) -> tuple[int, int, Path | None]:
    matching_denies = legacy_claude_git_denies(config_path)
    matching_allows = legacy_claude_git_allows(config_path)
    if not matching_denies and not matching_allows:
        return 0, 0, None
    config = read_json(config_path)
    permissions = config.get("permissions")
    if not isinstance(permissions, dict):
        raise FlowError(f"Claude permissionsを安全に読めません: {config_path}")
    for key, matches in (("deny", matching_denies), ("allow", matching_allows)):
        if not matches:
            continue
        values = permissions.get(key)
        if not isinstance(values, list):
            raise FlowError(f"Claude permissions.{key}を安全に読めません: {config_path}")
        remove = set(matches)
        remaining = [value for value in values if value not in remove]
        if remaining:
            permissions[key] = remaining
        else:
            permissions.pop(key, None)
    if not permissions:
        config.pop("permissions", None)
    comment = config.get("_comment")
    if isinstance(comment, str) and (
        comment.startswith("ai-devteamのプロジェクト用補助ガード")
        or comment.startswith("ai-devteamの旧静的Git denyは削除済み")
    ):
        config["_comment"] = (
            "ai-devteamは明示起動方式です。役割制御はrole-start後のflowctlフックが行います"
        )
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    backup = config_path.with_name(f"{config_path.name}.ai-devteam-opt-in-backup-{stamp}")
    if backup.exists():
        backup = config_path.with_name(
            f"{config_path.name}.ai-devteam-opt-in-backup-{stamp}-{uuid.uuid4().hex[:8]}"
        )
    backup.write_bytes(config_path.read_bytes())
    atomic_write_json(config_path, config)
    return len(matching_denies), len(matching_allows), backup


def aggregate_metrics(flow_root: Path) -> dict[str, Any]:
    task_dirs = sorted({path.parent.parent for path in flow_root.glob("**/.ai-devteam/policy.json")})
    values = [calculate_metrics(task) for task in task_dirs]
    submissions = sum(item["implementation_submissions"] for item in values)
    returns = sum(item["pm_returns"] for item in values)
    decided = [item["first_audit_pass"] for item in values if item["first_audit_pass"] is not None]
    return {
        "task_count": len(values),
        "average_cycle_seconds": round(sum(item["cycle_seconds"] for item in values) / len(values), 3)
        if values
        else 0,
        "session_count": sum(item["session_count"] for item in values),
        "pm_return_rate": round(returns / submissions, 4) if submissions else None,
        "first_audit_pass_rate": round(sum(1 for item in decided if item) / len(decided), 4)
        if decided
        else None,
    }
