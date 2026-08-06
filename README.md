# ai-devteam — AI開発フロー テンプレート集

役割別AIセッション（PM / Tech Lead / 実装担当 / 監査AI×2）で開発を進めるためのテンプレートと運用規約のマスターリポジトリ。

## 構成

```
ai-devteam/
├── README.md                        # このファイル（運用手順）
├── AGENTS.md                        # 各プロジェクトのルートへコピーする共通規約
├── codex/skills/                    # Codex用の役割Skill（マスター）
│   ├── pm/SKILL.md                  # $pm          プロジェクトマネージャー
│   ├── tech-lead/SKILL.md           # $tech-lead   技術判断
│   ├── implementer/SKILL.md         # $implementer 実装＋テスト
│   ├── auditor/SKILL.md             # $auditor     Codex監査
│   └── */agents/openai.yaml         # 暗黙発動の禁止設定（各Skillに同梱）
├── claude/skills/auditor/SKILL.md   # Claude Code用の監査Skill（/auditor）
├── claude/settings.json             # プロジェクトへコピーするガードレール雛形（git変更操作のdeny）
└── scripts/install.sh               # ローカル環境への配備スクリプト
```

**このリポジトリを `~/.agents/skills/` へ直接cloneしないこと。**
マスターはここに置き、install.shでコピーする（Codexの旧custom prompts
`~/.codex/prompts/` はdeprecatedのため使わない。install.shが残骸を掃除する）。

**役割Skillはすべて明示呼び出し専用。** 各Skillの `agents/openai.yaml`
（`allow_implicit_invocation: false`）とClaude側の `disable-model-invocation: true`
により、ユーザーが `$pm` 等を打たない限り自動発動しない。ai-devteamを使わない
プロジェクトのセッションが勝手に役割モードへ入ることを防ぐため、この設定を外さない。

## セットアップ（最初に1回）

```sh
sh scripts/install.sh
```

これで全プロジェクトから `$pm` `$tech-lead` `$implementer` `$auditor`（Codex。
`/skills` からも選択可）と監査Skill `/auditor`（Claude Code）が使えるようになる。
テンプレを改訂したら、このリポジトリで編集 → コミット → install.sh再実行。

## プロジェクトごとの準備（プロジェクトにつき1回）

```sh
cp ~/Desktop/ai-devteam/AGENTS.md <プロジェクトルート>/AGENTS.md
mkdir -p <プロジェクトルート>/.claude
cp ~/Desktop/ai-devteam/claude/settings.json <プロジェクトルート>/.claude/settings.json

cat >> <プロジェクトルート>/.gitignore <<'EOF'

# AI開発フロー（ai-devteam）の運用ファイル — コミットしない
/AGENTS.md
/CLAUDE.md
/docs/flow/
EOF
```

`docs/flow/` はPMが最初の機能開発時に自動作成するので事前準備は不要。
（Claude Codeを併用するプロジェクトでは、AGENTS.mdの内容をCLAUDE.mdにも反映するか
シンボリックリンクを張る）

### gitignoreの方針

規約ファイル（AGENTS.md / CLAUDE.md）と工程ファイル（docs/flow/）は
**プロジェクトのリポジトリにコミットしない**。AI運用のためのローカルファイルであり、
プロダクトの成果物ではないため、プロジェクトのgit履歴を汚さない。

- ignoreする: `/AGENTS.md`、`/CLAUDE.md`、`/docs/flow/`
- ignoreしない: `.claude/settings.json`（ガードレールなのでリポジトリに残す。
  `.claude/settings.local.json` は各自のローカル設定なのでignoreしてよい）

この方針の帰結として、次の2点に注意する。

- **工程記録はgit履歴に残らない。** よって機能クローズ後に spec.md の確定内容を
  正式ドキュメント（コミット対象）へ反映する工程（Step 8）が必須になる
- **docs/flow/ はローカル限定。** 別マシン・別クローンには引き継がれないので、
  他人と共有したい情報は正式ドキュメントに書く

すでにコミット済みのプロジェクトでは、追跡から外してからコミットする（ファイル自体は残る）。

```sh
git rm -r --cached AGENTS.md CLAUDE.md docs/flow
```

### ガードレール（ルールの物理的な強制）

「git変更操作はオーナーのみ」はテンプレの文章だけでなく、ツール設定でも強制する。

- **Claude Code側**: 上でコピーした `.claude/settings.json` が git 変更系コマンド
  （add / commit / push / checkout / merge / reset / tag / branch / gh pr 等）を
  deny し、読み取り系（status / log / diff / show / blame）のみ allow する。
  既存の settings.json があるプロジェクトでは permissions をマージする
- **Codex側**: `~/.codex/config.toml` に以下を設定する（全プロジェクト共通）

  ```toml
  approval_policy = "on-request"
  sandbox_mode    = "workspace-write"
  ```

  workspace-write サンドボックスでは `.git` などの保護パスへの書き込みが
  ブロックされるため、コミット等のgit変更操作は承認なしには実行されない。
  より厳しくするなら `approval_policy = "untrusted"`（シェルコマンド全般に承認要求）
- **CI**: テスト・lint・型チェックはプロジェクトのCIで必須化する（このリポジトリの
  管轄外だが、フローの前提。AGENTS.mdにも完了条件として明記済み）

## 機能開発の流れ

役割ごとにセッションを分け、受け渡しはファイルパスで行う。
あなた（ユーザー）の承認ゲートは従来どおり2つ: 実装前サマリ承認と最終クローズ判定。

```
1. PM        : プロジェクトdirで codex → $pm → 決定事項を伝える
               → 壁打ち → spec.md / tasks.md / task-01/instruction.md を書き出す
2. 実装担当   : 別セッションで $implementer → 「task-01/instruction.md を読んで」
               → pre-summary.md 提出 → ★あなたが承認 → 実装＋テスト
               → あなたがfeatureブランチへコミット → report.md / summary.md 書き出し
3. PM        : 「report.md を確認して」→ 裏取り → audit-request.md 書き出し
4. 監査       : 新規セッション×2（Codex: $auditor、Claude: 監査Skill）
               → 「audit-request.md を読んで監査して」
               → audit-codex.md / audit-claude.md 書き出し
5. PM        : 監査2件を整理 → audit-triage.md
               → 修正必要なら実装担当へ（2へ戻る。原則2ラウンドまで）
               → 「今すぐ直すべき」ゼロ → ★あなたがクローズ判定 → 完了
```

## チュートリアル: 1機能を通しで開発する

「ログイン機能」を例に、セッションの立ち上げからクローズまで、
あなたが実際に打つ言葉レベルで示す。

### 全体マップ

```
Step 1        Step 2              Step 3      Step 4     Step 5
PMが準備 ──▶ 実装＋テスト ──▶ PMが裏取り ──▶ 監査×2 ──▶ PMが整理
spec/tasks/   ★ゲート1:          audit-      audit-      audit-
指示書        サマリ承認          request     codex/      triage
                 ▲                            claude        │
                 │                                          │
                 └────── Step 6 修正指示（原則2ラウンド）◀──┤ 修正必要あり
                                                            │
                                          「今すぐ直すべき」ゼロ
                                                            ▼
                                              Step 7 ★ゲート2: クローズ判定
```

覚えることは5つだけ:

1. あなたが**判断する場面は2つ**（★ゲート1: 実装前サマリ承認、★ゲート2: クローズ判定）
2. あなたが**運ぶのはファイルパス1行**（内容のコピペは不要。AI同士はファイル経由で読み合う）
3. **監査だけ毎回新規セッション**（PM・実装担当は同じセッションを使い続ける）
4. **git変更操作（add / commit / branch / push）はすべてあなたが実行**（AIは変更ファイルと推奨メッセージを提示して依頼してくるだけ。読み取りはAIも可）
5. **次に何をするかは各セッションが教えてくれる**。工程完了ごとに「要約 → 書き出したパス → 次のアクション → 次のセッションへ貼るプロンプト」の形で報告してくるので、あなたは提示されたプロンプトを次のセッションへコピペするだけでよい（この手順書を暗記する必要はない）

### 登場するセッション

| セッション | 立ち上げ方 | 寿命 |
|---|---|---|
| PM | プロジェクトdirで `codex` → `$pm` | 機能開発のあいだ維持 |
| 実装担当 | 別セッションで `codex` → `$implementer` | 機能開発のあいだ維持 |
| Tech Lead | 必要時のみ `codex` → `$tech-lead` | 相談ごと |
| Codex監査 | 監査ごとに新規 `codex` → `$auditor` | 使い捨て |
| Claude監査 | 監査ごとに新規 `claude` → `/auditor`（Skill） | 使い捨て |

あなたの承認ゲートは2つ: **実装前サマリの承認** と **最終クローズ判定**。
それ以外のあなたの仕事は「パスを1行伝える」ことと「質問に答える」こと。

### Step 1. PMセッションを立ち上げ、要件を伝える

```
cd <プロジェクト>
codex
```

```
あなた: $pm
あなた: ログイン機能を開発します。MTG決定事項は以下です。
        - メール+パスワードでログインできる
        - 失敗5回でロック
        - セッションは24時間有効
```

PMはプロジェクトのドキュメントを読み、全体像・注意点・不明点を返してくる。
以降もPMは勝手に次工程へ進まないので、あなたが「次へ」と指示して進める。

```
PM   : （全体像の報告と質問）ロック解除の手段は決まっていますか？
あなた: 管理者による手動解除のみです。壁打ちを進めてください
PM   : （壁打ちの質問リスト）
あなた: （回答する）実装ドキュメントにまとめてください
PM   : docs/flow/login/spec.md に書き出しました
あなた: タスク分割してください
PM   : docs/flow/login/tasks.md に書き出しました
あなた: task-01の指示書を作成してください
PM   : docs/flow/login/task-01/instruction.md に書き出しました
        （featureブランチ名とbase commitも指示書に記載済み）
```

### Step 2. 実装担当に指示書を渡す 【★ゲート1】

別ターミナルで:

```
cd <プロジェクト>
codex
```

```
あなた: $implementer
あなた: docs/flow/login/task-01/instruction.md を読んで作業を開始してください
実装 : （指示書・spec.md・既存コードを確認）不明点が2つあります。〜〜
あなた: （回答する）
実装 : docs/flow/login/task-01/pre-summary.md に実装前サマリを書き出しました
```

実装担当は、指示書・spec・既存実装のあいだに矛盾や技術的な違和感を見つけた
場合、従う前にあなたへ指摘してくる（実装前サマリにも指摘欄がある）。
指摘が出たらPMセッションへ持ち帰る:

```
あなた: （PMセッションで）実装担当から指摘が出ています。
        task-01/pre-summary.md の指摘事項を確認してください
PM   : 指摘は妥当です。spec.md と instruction.md を修正しました
```

★ゲート1: pre-summary.md を読み、問題なければ承認する。

```
あなた: 承認します。実装に進んでください
実装 : ブランチ作成をお願いします: git switch -c feature/login <base commit>
あなた: （実行して）作成しました
実装 : （実装＋テスト）完了しました。コミットをお願いします。
        変更ファイル: 〜〜 / 推奨メッセージ: feat: ログインAPIを追加
あなた: （git add / commit を実行）コミットしました
実装 : docs/flow/login/task-01/report.md と summary.md に書き出しました
```

git変更操作（add / commit / branch / push）はすべてあなたが行う。
実装担当は作業単位ごとに変更ファイルと推奨メッセージを提示して依頼してくる。

### Step 3. PMに完了報告を裏取りさせ、監査依頼を作らせる

```
あなた: docs/flow/login/task-01/report.md を確認してください
PM   : （report.mdを読み、コードと突き合わせて裏取り）問題ありません。
        docs/flow/login/task-01/audit-request.md に監査依頼を書き出しました
```

### Step 4. 監査セッションを2つ新規で立ち上げる

Codex側（新規セッション）:

```
あなた: $auditor
あなた: docs/flow/login/task-01/audit-request.md を読んで監査してください
監査 : docs/flow/login/task-01/audit-codex.md に書き出しました
        監査結果: 修正必要
```

Claude側（新規セッション、`claude` で起動）:

```
あなた: /auditor
あなた: docs/flow/login/task-01/audit-request.md を読んで監査してください
監査 : docs/flow/login/task-01/audit-claude.md に書き出しました
        監査結果: クローズ可
```

### Step 5. PMセッションで監査結果を整理させる

```
あなた: 監査結果2件を整理してください
PM   : docs/flow/login/task-01/audit-triage.md に整理しました。
        今すぐ直すべき: 1件（ロック回数の境界値テスト欠落）
        次タスクでよい: 2件 / 残リスク: 1件
```

### Step 6. 修正指示 → 再監査（原則2ラウンドまで）

実装担当セッションに戻る:

```
あなた: docs/flow/login/task-01/audit-triage.md の修正指示に対応してください
実装 : （修正）コミットをお願いします。推奨メッセージ: fix: ロック回数の境界値テストを追加
あなた: （コミットする）
実装 : report.md を更新しました
```

PMセッションで再監査依頼を作らせ、Step 4を繰り返す
（再監査は前回からの差分中心。監査結果は audit-codex-2.md 等に書かれる）。

### Step 7. クローズ判定 【★ゲート2】

★ゲート2: 両監査の「今すぐ直すべき」がゼロになったら、あなたが判定する。

```
あなた: （audit-triage.md を確認して）クローズします。task-02へ進めてください
PM   : docs/flow/login/task-02/instruction.md に書き出しました
```

以降、Step 2〜7をタスクごとに繰り返す。全タスク完了後、PMに全体整合性
チェック（テンプレのステップ7）をさせてから、マージ・PR作成を指示する。

### Step 8. 正式ドキュメントへの反映（機能全体のクローズ後）

工程ファイルは開発中の受け渡し用で、gitignore対象のためgit履歴には残らない。
機能が閉じたら、確定した仕様を正式ドキュメント（コミット対象）へ必ず反映する。

```
あなた: （PMセッションで）クローズ後の反映指示書を作成してください
PM   : docs/flow/login/task-cleanup/instruction.md に書き出しました
あなた: （実装担当セッションで）docs/flow/login/task-cleanup/instruction.md を
        読んで対応してください
実装 : spec.md の確定内容を docs/ の正式ドキュメントへ反映しました。
        コミットをお願いします
あなた: （コミットする）
```

docs/flow/ の削除は行わない（gitignore対象なのでリポジトリは膨らまない）。
過去機能の工程記録・監査結果はローカルの docs/flow/ にそのまま残る。

### 途中でTech Lead相談が発生した場合

PMまたは実装担当が「Tech Lead相談条件に該当する」と報告してきたら:

```
あなた: （PMセッションで）Tech Lead相談資料を作成してください
PM   : docs/flow/login$tech-lead/jwt-aud互換性.md に書き出しました
```

新規セッションで:

```
あなた: $tech-lead
あなた: docs/flow/login$tech-lead/jwt-aud互換性.md を読んで判断してください
TL   : （判断結果を同ディレクトリに書き出し）
```

PMセッションに戻り「Tech Leadの判断が出ました。〜〜-decision.md を確認して
spec.mdへ反映してください」と伝える。

### 軽量パスを使う場合

小規模な変更（数ファイル・100行未満、外部IF/データ/認証に無関係、
挙動変更なし）では、PMに軽量パスを提案させることができる:

```
あなた: $pm
あなた: READMEの手順修正です。軽量パスでお願いします
PM   : 軽量パス条件に該当します。壁打ちを省略し、
        docs/flow/readme-fix/task-01/instruction.md を作成しました
```

実装前サマリなしで実装へ進み、監査は1AIのみでクローズできる。
条件に1つでも外れる場合、PMは通常フローを要求してくる。

### チートシート: あなたが打つ言葉一覧

1タスク分の流れを、あなたの発言だけ抜き出したもの。迷ったらここを見る。

| # | セッション | あなたが打つ言葉 | 返ってくるもの |
|---|---|---|---|
| 1 | PM | `$pm` → MTG決定事項を伝える | 全体像の報告と質問 |
| 2 | PM | 「壁打ちを進めてください」→ 質問に回答 | 壁打ちの質問リスト |
| 3 | PM | 「実装ドキュメントにまとめてください」 | spec.md |
| 4 | PM | 「タスク分割してください」 | tasks.md |
| 5 | PM | 「task-01の指示書を作成してください」 | task-01/instruction.md |
| 6 | 実装 | `$implementer` → 「task-01/instruction.md を読んで作業を開始してください」 | 質問・指摘 → pre-summary.md |
| 7 | 実装 | ★「承認します。実装に進んでください」 | 実装＋テスト → コミット依頼（あなたが実行） → report.md / summary.md |
| 8 | PM | 「task-01/report.md を確認してください」 | 裏取り → audit-request.md |
| 9 | 監査×2（毎回新規） | `$auditor`（Codex）/`/auditor`（Claude） → 「task-01/audit-request.md を読んで監査してください」 | audit-codex.md / audit-claude.md |
| 10 | PM | 「監査結果2件を整理してください」 | audit-triage.md |
| 11 | 実装 | 「task-01/audit-triage.md の修正指示に対応してください」（修正必要時のみ） | 修正 → コミット依頼（あなたが実行） → report.md 更新 → 9へ戻る |
| 12 | PM | ★「クローズします。task-02へ進めてください」 | task-02/instruction.md |
| 13 | PM | 「クローズ後の反映指示書を作成してください」（機能全体のクローズ後） | task-cleanup/instruction.md |
| 14 | 実装 | 「task-cleanup/instruction.md を読んで対応してください」 | 正式ドキュメントへ反映 → コミット依頼（あなたが実行） |

★ = あなたの承認ゲート。6〜12をタスクごとに繰り返し、機能全体が閉じたら13〜14で正式ドキュメントへ反映する。

### よくある質問

- **PMの回答を実装担当へコピペする必要は？** → ない。すべてファイル経由。
  あなたが運ぶのはパス1行だけ
- **セッションを閉じてしまったら？** → 成果物はすべて docs/flow/ にあるので、
  同じ役割プロンプトで立ち上げ直し「docs/flow/<機能名>/ を読んで状況を
  把握してください」から再開できる
- **docs/flow はコミットしなくていい？** → 不要（gitignore対象）。ただしローカルに
  しか無いので、恒久的に必要な内容は機能クローズ後に正式ドキュメントへ反映する（Step 8）
- **監査セッションを使い回していい？** → 不可。監査は毎回新規で立ち上げる
  （独立性の担保）。再監査も新規セッションでよい
- **機能開発の途中でテンプレを改訂したくなったら？** → メモしておき、
  機能クローズ後に改訂して install.sh を再実行する

## 運用ルールの要点

- **監査の一次ソースはPMのspec.md。** 実装担当のsummary.mdは「実装側の主張」として突き合わせる
- **監査境界は `git diff <base>..HEAD`。** 監査依頼にブランチ名とbase commitを必ず書く
- **クローズ条件は「今すぐ直すべき」ゼロ＋あなたの判定。** 監査は原則2ラウンドまで、3ラウンド目はTech Lead相談へ
- **PMはリポジトリを一切変更しない。** コメント追加・ファイル削除などの軽作業も実装担当へ
- **git変更操作はすべてオーナー（あなた）が行う。** PM / Tech Lead / 実装担当 / 監査のいずれもadd・commit・branch作成・checkout・pushを実行しない。実装担当は作業単位ごとに変更ファイルと推奨コミットメッセージを提示して依頼してくる。読み取り（status / log / diff）はAIも可
- **軽量パス**（小規模・外部IF/データ/認証に無関係・挙動変更なし）はPMテンプレの条件を満たし、あなたが承認した場合のみ。壁打ち・実装前サマリ省略、監査1AI
- **Codex監査とClaude監査の両方を維持する。** 異種モデル監査がこのフローの独立性の実体
- **監査セッションは毎回新規で立ち上げる**（再監査で前回指摘との差分を見る場合を除く）
- **規約ファイルと工程ファイルはgitignoreする。** `/AGENTS.md` `/CLAUDE.md` `/docs/flow/` はコミットしない（`.claude/settings.json` はガードレールなのでコミットする）。工程記録がgit履歴に残らないぶん、機能クローズ後に spec.md の確定内容を正式ドキュメントへ反映するのが必須（PMテンプレのステップ8）。docs/flow/ の削除は行わない
- **重要ルールはプロンプトだけでなく設定で強制する。** git変更操作の禁止は `.claude/settings.json` のdenyとCodexのサンドボックス設定で物理的にブロックし、品質検証はCI（テスト・lint・型チェック必須）で担保する
