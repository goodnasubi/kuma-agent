# kuma-agent 設計メモ

## 目的

Muse（Meta の個人エージェント）のような「対話型の相棒エージェント」を
自作する。単発のタスク自動化ではなく、同じ相手と長く付き合い、人格と
記憶を持ち、道具で現実の作業もできる存在を目指す。

当初の構想（GitHub Issue を拾う自律開発エージェント）から方針転換した
（2026-11-01）。開発タスクの自動化は将来の「道具」の 1 つとして扱う。

## 構成

```
src/kuma_agent/
  cli.py       エントリポイント（chat）
  config.py    設定（モデル、データ置き場、API キー）
  llm.py       LLM バックエンド（API 系 3 種）と共通の Message 表現
  agent_cli.py エージェント実行環境（CLI 系 2 種）をバックエンドにする実装
  persona.py   人格ファイル（SOUL.md / IDENTITY.md / USER.md）の読み込み
  memory.py    長期記憶（MEMORY.md）と会話履歴の読み書き・検索
  tools.py     道具の登録と実行（ファイル、シェル、時刻など）
  session.py   1 回の対話セッション（履歴管理、道具呼び出しの循環）
data/（実行時に作る、リポジトリには入れない）
  SOUL.md / IDENTITY.md / USER.md / MEMORY.md / conversations/
```

## 対話ループ

1. 人格ファイルと長期記憶を読み、システムプロンプトを組み立てる
2. 利用者の発話を受ける
3. LLM が応答する。道具の呼び出しがあれば実行して結果を戻し、
   呼び出しがなくなるまで循環する
4. 会話履歴を保存する。重要な事実は長期記憶へ書き出す（要約と抽出）

## 記憶の設計

- **長期記憶**（MEMORY.md）: 人間が読める Markdown。事実・好み・約束を
  箇条書きで保持し、毎回の対話の冒頭で読み込む
- **会話履歴**: 日付ごとの JSONL。検索は当面は単純なキーワード一致
- 書き込みはエージェント自身が行い、利用者がいつでも直接編集できる

## 人格の設計

- SOUL.md: 振る舞いと価値観（口調、姿勢、好み）
- IDENTITY.md: 名前・呼び名・シグネチャ
- USER.md: 相手（利用者）の情報。名前、タイムゾーン、関心事
- 人格はコードから分離し、ファイルの差し替えで別キャラクターにできる

## 道具（ツール）

最初に用意するもの:
- ファイルの読み書き（作業ディレクトリ内）
- シェルコマンドの実行（確認が必要な操作は利用者に聞く）
- 現在時刻・日付の取得
- 記憶の検索と書き込み

将来: Web 検索、カレンダー、GitHub（Issue 駆動の開発補助を含む）

## 安全装置

- 取り返しのつかない操作（削除、送信、公開）は利用者の確認を取る
- API キーは環境変数からのみ読む（`META_MODEL_API_KEY`）
- 道具の実行範囲は作業ディレクトリと許可リストに制限する

## LLM バックエンド（決定済み）

頭脳は差し替え可能にし、5 種を用意する。選択は環境変数
`KUMA_LLM_PROVIDER`（既定 muse）、モデルは `KUMA_LLM_MODEL`。

API 系（kuma-agent が道具ループを回す）:
- `muse`: Meta Model API（Muse Wonder）。キー: META_MODEL_API_KEY
- `claude`: Anthropic Messages API。キー: ANTHROPIC_API_KEY
- `gemini`: Google Gemini API。キー: GEMINI_API_KEY

CLI 系（相手側が道具ループを持つ。文脈をプロンプトで渡し、作業
ディレクトリでヘッドレス実行する。認証は各 CLI のログインを使う）:
- `claude-code`: Claude Code CLI（`claude -p`）
- `gemini-cli`: Gemini CLI（`gemini -p`）

## Meta Model API の使い方

- OpenAI 互換の Chat Completions 形式（`https://api.meta.ai/v1`、
  モデル `muse-wonder-1.0`、Bearer 認証）
- 人格などの誘導は `developer` ロールで渡す
- 関数ツールは OpenAI 形式。`stop`・`logit_bias`・
  `reasoning_effort="none"` は送らない（400 になる）

## 未決事項

- 記憶の抽出（会話 → MEMORY.md）をいつ、どのモデルでやるか
- 音声入出力、メッセージングアプリ（WhatsApp 等）への接続
- 複数セッション・先回り通知（朝の予定連絡など）の仕組み
