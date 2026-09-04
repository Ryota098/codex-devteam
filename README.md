# codex-devteam

PM、TL（Tech Lead）、実装担当、Codex監査、Claude監査を独立セッションで運用する、明示起動型のAI開発フローです。実装担当内では、Builderと読み取り専用の内部検証担当が制限付きループを行います。

`flowctl`が、有効化された役割セッションの工程順、変更範囲、オーナー承認、監査数を検証します。AIや別セッションを自動起動するツールではありません。

## 有効化

新しいセッションは通常モードで始まり、ai-devteamは無効です。プロジェクトに`AGENTS.md`や`docs/flow/`があっても、通常の質問・調査・実装をai-devteamの工程へ自動変換しません。

| ユーザーの依頼 | 動作 |
| --- | --- |
| 役割指定なし | 通常のCodexとして動作。ai-devteamのフック制限、工程遷移、ハンドオフを適用しない |
| `$pm`、`$tl`、`$implementer`、`$auditor`で役割開始を明示 | Skillが最初に`flowctl role-start`を実行し、成功後からそのセッションだけai-devteamを有効化 |
| 呼出し名を質問・説明・比較・引用・例文で記載 | Skill本文が添付されても役割を開始せず、通常モードを維持 |

役割開始後はセッション終了まで同じ役割を維持します。通常モードのセッションを途中から役割セッションとして使う場合も、ユーザーが役割開始を明示した場合に限ります。
Codex Skillは`allow_implicit_invocation: false`に設定し、モデルによる暗黙選択を無効化しています。

## 運用原則

| 領域 | 現行ルール |
| --- | --- |
| スコープ | 外部成果・全変更パス・リスク区分／領域・ファイル数／差分行数をオーナー固定 |
| TL | 上流設計、重大な技術・セキュリティ判断が必要な場合だけ使用 |
| 実装 | 既存パターン調査、実装、テスト、内部検証、修正を上限付きで反復 |
| 文書 | 正式ドキュメントはPMだけが更新。実装担当は影響を報告 |
| 工程 | 実装担当から必ずPMへ戻し、PM確認・オーナーコミット後だけ監査可能 |
| 監査 | 独立2監査が既定。1監査はオーナーがスコープ固定時に選んだ場合だけ |
| 制御 | `role-start`後、フックが役割外書込み、Git変更、秘密情報、本番・共有環境等を拒否 |
| 計測 | 所要時間、セッション数、PM差し戻し率、初回監査合格を自動記録 |

## セットアップ

マスターリポジトリで実行します。

```sh
sh scripts/install.sh
```

この処理はテスト後、役割Skill、`~/.ai-devteam/bin/flowctl`、Codex/Claudeのフック、Codexの役割別権限プロファイルを配備します。グローバルフックは通常モードでは素通しし、`role-start`後だけ制御します。配備後はCodexとClaudeの既存セッションを終了し、新しいセッションを開始してください。

プロジェクトでは最新版の`AGENTS.md`をルートへ置きます。Claudeも使う場合は同じ内容を`CLAUDE.md`へ置きます。

```sh
cp AGENTS.md <project>/AGENTS.md
cp AGENTS.md <project>/CLAUDE.md
```

`AGENTS.md`、`CLAUDE.md`、`docs/flow/`はローカル工程ファイルとしてgitignoreします。役割制御はグローバルフックが行うため、`claude/settings.json`の旧Git deny雛形はプロジェクトへコピーしません。

旧版の雛形をコピー済みのプロジェクトでは、まず`diagnose`で確認し、検出された場合だけオーナーが自分のターミナルで除去します。既知のGit deny 27件と読み取りallow 7件だけを削除し、その他のClaude設定を保持したバックアップを作成します。

```sh
~/.ai-devteam/bin/flowctl diagnose --project-root <project>
~/.ai-devteam/bin/flowctl remove-legacy-claude-guards \
  --project-root <project> \
  --owner-confirmed
```

## 基本フロー

```text
PM: 壁打ち・根拠確認・scope-baseline/spec/tasks/instruction
 ↓ 必要な場合だけ
TL: 上流の技術・設計・セキュリティ判断
 ↓
実装担当: 既存調査 → pre-summary → オーナー開始承認
           → 実装・テスト → 内部検証 → 修正・再検証
 ↓
PM: 未コミット差分と証拠を裏取り、正式ドキュメントを更新
 ↓
オーナー: コミット
 ↓
PM: 確定差分を再確認しaudit-request.mdを作成
 ↓
Codex監査・Claude監査（既定2、独立セッション）
 ↓
PM: 監査整理
 ↓
オーナー: クローズ
```

各役割はユーザーが新しい独立セッションで`$pm`等を明示して開始します。PMと実装担当は同じ機能内でセッションを再利用できます。TLは相談ごと、監査は監査ごとに新規セッションを使います。

## スコープと不足の扱い

PMは`docs/flow/<feature>/scope-baseline.md`へ次を記載します。

```markdown
## 承認対象

| 要求ID | 承認済みの外部成果 | 変更可能パス | 許可するリスク領域 | リスク区分 | 変更上限 |
| --- | --- | --- | --- | --- | --- |
| 要求1 | 利用者が安全にデータを削除できる | `src/delete/**`<br>`tests/delete/**` | 不可逆削除 | 高 | 20ファイル / 2000行 |

## 明示的な対象外

- Slack通知、課金変更、別サービスの改修は行わない。
```

オーナーは仕様承認と同時に固定します。

```sh
~/.ai-devteam/bin/flowctl scope-lock \
  --scope-file docs/flow/<feature>/scope-baseline.md \
  --audits 2 \
  --owner-confirmed
```

実装中の発見は次の基準で扱います。

- 元の外部成果がその変更なしでは成立せず、固定パス・リスク・変更上限内に収まる不足：根拠を残して同じタスクへ含める
- 質問、壁打ち、候補案：回答しても自動的には実装へ追加しない
- 既存方針で決められない上流の技術・セキュリティ判断：実装を止め、PMが`tl-request`で独立TLへ渡す
- 新しい外部成果、利用者挙動、不可逆境界、変更パス、サービス責務、リスク領域：PMが差分化し、オーナー再承認後だけ追加
- 「あると良い」改善、将来対応、周辺整理：別タスク候補

不足を同じタスクへ含める場合も、実コード・既存契約・再現テストで不可欠性を説明し、`loop-state.md`へ残します。
変更可能パスはコード・テスト・設定・migration・PMが更新する正式ドキュメントを含む全候補です。`instruction.md`では、その内から正式ドキュメントを除いた実装担当のパスだけを選びます。

範囲拡大を採用する場合は、実装を止め、PMの差分提示とオーナー再固定後に`instruction-ready`を通します。同じ実装担当セッションでpre-summaryと必要な実装前内部検証を更新し、オーナーの`start-approve`後に再開します。リスク区分または監査構成自体が変わる場合は新しいtaskへ分けますが、同じ機能の実装担当セッションは再利用できます。

## flowctlを実行する人とタイミング

| 実行者 | タイミング | 主なコマンド |
| --- | --- | --- |
| フック | AIがツールを使う直前。通常モードは素通し | `flowctl hook`（自動） |
| PM | 初期化、必要時のTL相談、指示書完成、差分確認、監査準備、監査整理 | `init`、`tl-request`、`instruction-ready`、`pm-review`、`audit-ready`、`triage` |
| 実装担当 | 役割開始、途中フィードバック、PM提出 | `role-start`、`feedback`、`submit` |
| TL | 判断完了 | `tl-complete` |
| 監査 | 開始、結果登録 | `role-start`、`audit-result` |
| オーナー | スコープ、開始承認、一時権限、最終終了 | `scope-lock`、`start-approve`、`approve`、`close` |

役割開始を明示されたAIは、各Skillに従って節目のコマンドを自発的に実行します。通常モードでは実行しません。オーナー専用コマンドをAIフック経由で実行すると拒否されます。

```sh
~/.ai-devteam/bin/flowctl status --task-dir docs/flow/<feature>/task-01
~/.ai-devteam/bin/flowctl next --task-dir docs/flow/<feature>/task-01 --provider codex
```

`next`は文面を表示するだけです。セッションの起動とプロンプトの貼り付けはユーザーが行います。

## 一時権限

実credential、本番環境、共有DBは常時禁止です。隔離DB、migration、依存変更、外部ネットワークが必要な場合だけ、オーナーが対象taskへ期限付きで許可します。

```sh
~/.ai-devteam/bin/flowctl approve \
  --task-dir docs/flow/<feature>/task-01 \
  --role implementer \
  --capability isolated-db \
  --minutes 30 \
  --reason "専用の破棄可能DBで結合テスト" \
  --owner-confirmed
```

許可は工程・役割・スコープ制約を解除しません。

## 指標

```sh
~/.ai-devteam/bin/flowctl metrics --task-dir docs/flow/<feature>/task-01
~/.ai-devteam/bin/flowctl metrics --flow-root docs/flow
```

イベントは、有効化された役割セッションだけを対象に`task-NN/.ai-devteam/`へ追記専用で保存します。通常セッション、プロンプト本文、会話ログ、秘密情報、credentialは記録しません。

## 進行中taskへの導入

新規taskはPMが`flowctl init`を行います。すでに進行中なら、PMがscope-baseline.mdとinstruction.mdを新形式に整え、オーナーがscope-lock後に安全側の工程へ取り込みます。

```sh
~/.ai-devteam/bin/flowctl adopt \
  --task-dir docs/flow/<feature>/task-04 \
  --scope-file docs/flow/<feature>/scope-baseline.md \
  --scope-id 要求1 \
  --risk high \
  --branch feature/example \
  --base <base-sha> \
  --state implementation \
  --pre-evaluator required \
  --reason "新しい工程制御へ移行" \
  --owner-confirmed
```

取込み可能な状態は`planning`、`instruction_ready`、`implementation_preflight`、`implementation`、`pm_review`です。証拠が不足する場合は安全側へ戻して取り込みます。

## 物理制御の範囲

`role-start`後のフックは、役割外ファイル、正式ドキュメントの実装担当編集、Git変更、秘密情報パス、本番・共有環境、未許可DB・migration等を直接拒否します。通常モードにはこの制御を適用しません。任意の子プロセス内部まで完全に隔離するOS sandboxではないため、Codexでは役割別プロファイルも併用します。

```sh
codex -p ai-devteam-pm
codex -p ai-devteam-implementer
codex -p ai-devteam-review
```

`~/.codex/config.toml`に旧`sandbox_mode`設定があると新しいpermission profileが優先されません。`flowctl diagnose --project-root <project>`で状態を確認してください。既存設定はinstallerが無断で削除しません。

## マスター構成

```text
AGENTS.md                         共通規約
codex/skills/                    Codexの役割Skill
codex/skills/*/agents/openai.yaml Codex Skillの明示起動ポリシー
claude/skills/auditor/           Claude監査Skill
claude/settings.json             静的denyを持たない参照用設定
codex/profiles/                  Codex最小権限プロファイル
scripts/flowctl.py               工程CLI
scripts/flowctl_lib.py           検証・フック・指標
scripts/validate_handoff.py      実装提出の形式検証
tests/test_flowctl.py            回帰テスト
scripts/install.sh               検証と配備
```
