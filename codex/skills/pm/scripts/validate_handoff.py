#!/usr/bin/env python3
"""PMへ渡された実装成果物の形式ゲートを検証する。"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path


SUMMARY_TAGS = (
    "TITLE",
    "PUBLIC API",
    "RULES",
    "BRANCHES",
    "ERRORS",
    "ASSUMPTIONS",
    "DEVIATION",
)


def validate_summary(path: Path) -> list[str]:
    errors: list[str] = []
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    if len(lines) > 12:
        errors.append(f"summary.mdは空行を除いて12行以内にする（現在{len(lines)}行）")

    counts = {tag: 0 for tag in SUMMARY_TAGS}
    for index, line in enumerate(lines, start=1):
        match = re.match(r"^\[([A-Z ]+)\]\s*(.+)$", line)
        if match is None:
            errors.append(f"summary.md {index}行目が定型タグで始まっていない")
            continue
        tag, value = match.groups()
        if tag not in counts:
            errors.append(f"summary.md {index}行目に規定外タグ[{tag}]がある")
            continue
        counts[tag] += 1
        if not value.strip():
            errors.append(f"summary.md {index}行目の[{tag}]が空である")

    for tag, count in counts.items():
        if count == 0:
            errors.append(f"summary.mdに必須タグ[{tag}]がない")
    if counts["TITLE"] > 1:
        errors.append("summary.mdの[TITLE]は1行だけにする")

    return errors


def markdown_section(text: str, heading: str) -> str | None:
    match = re.search(
        rf"^## {re.escape(heading)}\s*$\n(?P<body>.*?)(?=^##\s|\Z)",
        text,
        flags=re.MULTILINE | re.DOTALL,
    )
    return match.group("body") if match else None


def parse_fields(section: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in section.splitlines():
        match = re.match(r"^- ([^:：]+)[:：]\s*(.+)$", line.strip())
        if match:
            fields[match.group(1).strip()] = match.group(2).strip()
    return fields


def table_rows(section: str, header: str) -> list[list[str]]:
    lines = [line.strip() for line in section.splitlines()]
    try:
        start = lines.index(header)
    except ValueError:
        return []
    rows: list[list[str]] = []
    for line in lines[start + 1 :]:
        if not line.startswith("|"):
            if rows:
                break
            continue
        if re.fullmatch(r"\|[ :\-|]+\|", line):
            continue
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def validate_table(
    section: str,
    heading: str,
    header: str,
    label: str,
    *,
    expected_result: str | None = None,
) -> list[str]:
    errors: list[str] = []
    if heading not in section:
        errors.append(f"証跡に『{heading}』がない")
    if header not in section:
        errors.append(f"証跡に{label}の判定表がない")
        return errors

    rows = table_rows(section, header)
    if not rows:
        errors.append(f"証跡の{label}判定表にデータ行がない")
    elif expected_result is not None:
        for row in rows:
            if len(row) < 2 or row[1] != expected_result:
                name = row[0] if row else "不明"
                errors.append(f"{label}『{name}』の判定は『{expected_result}』でなければならない")
    return errors


def validate_preflight(text: str, required: bool) -> list[str]:
    errors: list[str] = []
    section = markdown_section(text, "実装前検証証跡")
    if section is None:
        return ["loop-state.mdに『## 実装前検証証跡』がない"] if required else []

    fields = parse_fields(section)
    required_fields = (
        "実施要否",
        "実施方式",
        "起動回数",
        "起動記録",
        "検証対象",
        "最終判定",
        "未解決事項",
        "実装開始前のプロダクト差分",
    )
    for field in required_fields:
        if not fields.get(field):
            errors.append(f"実装前検証証跡に『{field}』がない")

    if fields.get("実施要否") != "必須":
        errors.append("実装前検証証跡の『実施要否』は『必須』にする")
    if fields.get("実施方式") != "別コンテキストのサブエージェント":
        errors.append("実装前検証の実施方式は『別コンテキストのサブエージェント』と明記する")
    if not re.fullmatch(r"[1-9][0-9]*", fields.get("起動回数", "")):
        errors.append("実装前検証の起動回数は1以上の整数にする")
    if fields.get("起動記録", "") in {"", "なし", "不明", "未確認"}:
        errors.append("実装前検証の起動記録には実際の識別子、または識別子が未提供だった事実を記録する")
    if fields.get("最終判定") != "実装開始可":
        errors.append("PM提出には実装前検証の最終判定『実装開始可』が必要")
    if fields.get("未解決事項") != "なし":
        errors.append("未解決事項がある実装前検証では実装を開始しない")
    if fields.get("実装開始前のプロダクト差分") != "なし":
        errors.append("実装前検証より先にプロダクト差分を作成してはならない")

    errors.extend(
        validate_table(
            section,
            "### 確認対象別判定",
            "| 確認対象 | 判定 | 根拠 |",
            "確認対象別",
            expected_result="実装開始可",
        )
    )
    return errors


def validate_loop_state(path: Path) -> list[str]:
    errors: list[str] = []
    section = markdown_section(path.read_text(encoding="utf-8"), "内部検証証跡")
    if section is None:
        return ["loop-state.mdに『## 内部検証証跡』がない"]

    fields = parse_fields(section)
    requirement = fields.get("実施要否")
    if requirement == "省略承認済み":
        reason = fields.get("省略根拠", "")
        if not reason or reason in {"なし", "不明", "未確認"}:
            errors.append("内部検証を省略する場合は具体的な省略根拠が必要")
        return errors

    if requirement != "必須":
        errors.append("内部検証証跡の『実施要否』は『必須』または『省略承認済み』にする")
        return errors

    required_fields = (
        "実施方式",
        "起動回数",
        "起動記録",
        "検証対象",
        "最終判定",
        "修正票",
        "対応結果",
        "合格後の実装・テスト・設定・自動生成物変更",
    )
    for field in required_fields:
        if not fields.get(field):
            errors.append(f"内部検証証跡に『{field}』がない")

    if fields.get("実施方式") != "別コンテキストのサブエージェント":
        errors.append("実施方式は『別コンテキストのサブエージェント』と明記する")

    launch_count = fields.get("起動回数", "")
    if not re.fullmatch(r"[1-9][0-9]*", launch_count):
        errors.append("起動回数は1以上の整数にする")

    launch_record = fields.get("起動記録", "")
    if launch_record in {"", "なし", "不明", "未確認"}:
        errors.append("起動記録には実際の識別子、または識別子が未提供だった事実を記録する")

    if fields.get("最終判定") != "合格":
        errors.append("PM提出には内部検証の最終判定『合格』が必要")

    if fields.get("合格後の実装・テスト・設定・自動生成物変更") != "なし":
        errors.append("内部検証合格後に差分変更がある場合は、新しい内部検証が必要")

    errors.extend(
        validate_table(
            section,
            "### 受け入れ条件別判定",
            "| 受け入れ条件 | 判定 | 根拠 |",
            "受け入れ条件別",
            expected_result="合格",
        )
    )

    return errors


def validate_report(path: Path) -> list[str]:
    errors: list[str] = []
    text = path.read_text(encoding="utf-8")
    section = markdown_section(text, "正式ドキュメント影響")
    if section is None:
        errors.append("report.mdに『## 正式ドキュメント影響』がない")
    else:
        fields = parse_fields(section)
        if fields.get("実装担当による正式ドキュメント変更") != "なし":
            errors.append("実装担当による正式ドキュメント変更は『なし』でなければならない")

        candidate = fields.get("PM更新候補", "")
        if not candidate:
            errors.append("正式ドキュメント影響に『PM更新候補』がない")
        elif candidate == "なし":
            reason = fields.get("更新不要の理由", "")
            if reason in {"", "なし", "不明", "未確認"}:
                errors.append("PM更新候補がない場合は具体的な『更新不要の理由』が必要")
        else:
            facts = fields.get("確認済み事実", "")
            if facts in {"", "なし", "不明", "未確認"}:
                errors.append("PM更新候補がある場合は文書化できる『確認済み事実』が必要")

    evidence = markdown_section(text, "受け入れ条件と検証証拠")
    if evidence is None:
        errors.append("report.mdに『## 受け入れ条件と検証証拠』がない")
    else:
        errors.extend(
            validate_table(
                evidence,
                "",
                "| 受け入れ条件 | 実装箇所 | 検証証拠 | 結果 |",
                "受け入れ条件と検証証拠",
            )
        )
        for line in evidence.splitlines():
            if not line.strip().startswith("|") or "---" in line:
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if cells and cells[0] != "受け入れ条件" and len(cells) == 4:
                if cells[3] != "成功":
                    errors.append(f"受け入れ条件『{cells[0]}』の結果が『成功』ではない")

    return errors


def validate_task_dir(task_dir: Path) -> list[str]:
    errors: list[str] = []
    for name in ("instruction.md", "report.md", "summary.md", "loop-state.md"):
        path = task_dir / name
        if not path.is_file():
            errors.append(f"{name}が存在しない")
        elif not path.read_text(encoding="utf-8").strip():
            errors.append(f"{name}が空である")

    if errors:
        return errors

    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8")
    markers = re.findall(
        r"^\s*[-*]?\s*実装前内部検証\s*[:：]\s*(必須|不要)\s*$",
        instruction,
        flags=re.MULTILINE,
    )
    if len(markers) != 1:
        errors.append("instruction.mdには『実装前内部検証: 必須』または『実装前内部検証: 不要』を1行だけ明記する")

    loop_text = (task_dir / "loop-state.md").read_text(encoding="utf-8")
    errors.extend(validate_summary(task_dir / "summary.md"))
    errors.extend(validate_report(task_dir / "report.md"))
    errors.extend(validate_preflight(loop_text, required=markers == ["必須"]))
    errors.extend(validate_loop_state(task_dir / "loop-state.md"))

    acceptance = markdown_section(instruction, "受け入れ条件")
    report_evidence = markdown_section(
        (task_dir / "report.md").read_text(encoding="utf-8"), "受け入れ条件と検証証拠"
    )
    loop_evidence = markdown_section(loop_text, "内部検証証跡")
    instruction_rows = table_rows(
        acceptance or "", "| 受け入れ条件 | 外部から観測できる期待結果 | 検証方法 |"
    )
    report_rows = table_rows(
        report_evidence or "", "| 受け入れ条件 | 実装箇所 | 検証証拠 | 結果 |"
    )
    loop_rows = table_rows(
        loop_evidence or "", "| 受け入れ条件 | 判定 | 根拠 |"
    )
    expected_conditions = [row[0] for row in instruction_rows if row]
    if not expected_conditions:
        errors.append("instruction.mdの受け入れ条件表を解析できない")
    else:
        if [row[0] for row in report_rows if row] != expected_conditions:
            errors.append("report.mdの受け入れ条件がinstruction.mdと同一・同順ではない")
        if [row[0] for row in loop_rows if row] != expected_conditions:
            errors.append("loop-state.mdの受け入れ条件がinstruction.mdと同一・同順ではない")
    return errors


def write_fixture(task_dir: Path, summary: str, loop_state: str, report: str) -> None:
    (task_dir / "instruction.md").write_text(
        """# 指示書

- 実装前内部検証: 必須

## 受け入れ条件

| 受け入れ条件 | 外部から観測できる期待結果 | 検証方法 |
| --- | --- | --- |
| 条件1 | 期待結果 | test-1 |
""",
        encoding="utf-8",
    )
    (task_dir / "report.md").write_text(report, encoding="utf-8")
    (task_dir / "summary.md").write_text(summary, encoding="utf-8")
    (task_dir / "loop-state.md").write_text(loop_state, encoding="utf-8")


def self_test() -> int:
    valid_summary = "\n".join(
        (
            "[TITLE] テスト",
            "[PUBLIC API] API",
            "[RULES] 規則",
            "[BRANCHES] 分岐",
            "[ERRORS] エラー",
            "[ASSUMPTIONS] なし",
            "[DEVIATION] なし",
        )
    )
    valid_loop = """# ループ

## 実装前検証証跡

- 実施要否: 必須
- 実施方式: 別コンテキストのサブエージェント
- 起動回数: 1
- 起動記録: agent-pre-123
- 検証対象: 指示書、仕様、実装前サマリ、既存実装
- 最終判定: 実装開始可
- 未解決事項: なし
- 実装開始前のプロダクト差分: なし

### 確認対象別判定

| 確認対象 | 判定 | 根拠 |
| --- | --- | --- |
| 業務入口と全呼出し元 | 実装開始可 | route.tsとservice.ts |

## 内部検証証跡

- 実施要否: 必須
- 実施方式: 別コンテキストのサブエージェント
- 起動回数: 1
- 起動記録: agent-123
- 検証対象: 現在の候補差分
- 最終判定: 合格
- 修正票: なし
- 対応結果: 修正票なし
- 合格後の実装・テスト・設定・自動生成物変更: なし

### 受け入れ条件別判定

| 受け入れ条件 | 判定 | 根拠 |
| --- | --- | --- |
| 条件1 | 合格 | test-1 |
"""
    valid_report = """# 報告

## 正式ドキュメント影響

- 実装担当による正式ドキュメント変更: なし
- PM更新候補: docs/guide.md
- 確認済み事実: route.tsの公開挙動とtest-1の結果

## 受け入れ条件と検証証拠

| 受け入れ条件 | 実装箇所 | 検証証拠 | 結果 |
| --- | --- | --- | --- |
| 条件1 | route.ts | test-1 | 成功 |
"""
    invalid_summary = valid_summary.replace("[BRANCHES] 分岐\n", "[TESTS] 成功\n")
    invalid_loop = "# ループ\n\n## 内部検証\n\n合格。\n"
    row_failure_loop = valid_loop.replace("| 条件1 | 合格 | test-1 |", "| 条件1 | 不合格 | test-1 |")
    invalid_report = "# 報告\n"

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        valid = root / "valid"
        invalid = root / "invalid"
        row_failure = root / "row-failure"
        valid.mkdir()
        invalid.mkdir()
        row_failure.mkdir()
        write_fixture(valid, valid_summary, valid_loop, valid_report)
        write_fixture(invalid, invalid_summary, invalid_loop, invalid_report)
        write_fixture(row_failure, valid_summary, row_failure_loop, valid_report)
        if validate_task_dir(valid):
            raise AssertionError("正常fixtureが失敗した")
        invalid_errors = validate_task_dir(invalid)
        if not any("規定外タグ[TESTS]" in error for error in invalid_errors):
            raise AssertionError("規定外summaryタグを検出できなかった")
        if not any("内部検証証跡" in error for error in invalid_errors):
            raise AssertionError("内部検証証跡の欠落を検出できなかった")
        if not any("実装前検証証跡" in error for error in invalid_errors):
            raise AssertionError("必須の実装前検証証跡の欠落を検出できなかった")
        if not any("正式ドキュメント影響" in error for error in invalid_errors):
            raise AssertionError("正式ドキュメント影響の欠落を検出できなかった")
        row_errors = validate_task_dir(row_failure)
        if not any("判定は『合格』" in error for error in row_errors):
            raise AssertionError("受け入れ条件単位の不合格を検出できなかった")

    print("self-test: 7 checks passed")
    return 0


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        return self_test()
    if len(sys.argv) != 2:
        print("usage: validate_handoff.py <task-dir>", file=sys.stderr)
        return 2

    task_dir = Path(sys.argv[1])
    if not task_dir.is_dir():
        print(f"handoff validation: FAIL\n- task directory not found: {task_dir}")
        return 1

    errors = validate_task_dir(task_dir)
    if errors:
        print("handoff validation: FAIL")
        for error in errors:
            print(f"- {error}")
        return 1

    print("handoff validation: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
