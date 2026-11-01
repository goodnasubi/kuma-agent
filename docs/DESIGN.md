# kuma-agent 設計メモ

## 目的

GitHub の Issue を起点に、自律エージェントが「ロック → 実装 → 検証 →
PR」を回す仕組みを、対象リポジトリに依存しない汎用部品として作る。
原型は goodnasubi/sigil で運用中の方式（Issue 起点、agent ラベルによる
排他ロック、レーン制、コミットトレーラー、バックログ同期）。

## 構成

```
src/kuma_agent/
  cli.py      エントリポイント（run / status）
  config.py   設定（対象リポジトリ、ラベル名、間隔、上限）
  github.py   GitHub API の薄いラッパ（Issue 一覧、ラベル、コメント、PR）
  gitops.py   clone / branch / commit / push の操作
  llm.py      Meta Model API（Muse）のクライアントとツール実行ループ
  worker.py   1 Issue 分の作業手順（ロック→実装→検証→PR→報告）
  prompts.py  システムプロンプトと Issue 用プロンプトの組み立て
```

依存は最小限にする：標準ライブラリ中心 + `httpx` のみ（GitHub API と
Model API の両方に使う）。

## 作業手順（1 Issue）

1. **発見**: オープン Issue のうち、担当ラベルがなく `blocked` でない
   ものを古い順に取る
2. **ロック**: `agent:kuma` ラベルを付け、ブランチ名をコメントする。
   競合したら着手コメントの時刻が早い方が正（Sigil の決着ルールを継承）
3. **準備**: 対象リポジトリを clone / fetch し、ブランチを切る
   （`kuma/<issue>-<slug>`）
4. **実装**: LLM ループ。読み取り・編集・コマンド実行のツールを渡し、
   リポジトリの AGENTS.md があればそれを最優先の規約として読ませる
5. **検証**: 設定された検証コマンド（例: `cargo test`、`pytest`）を実行。
   失敗したら上限回数まで LLM に直させる
6. **提出**: push して PR を作成。本文に `Closes #<issue>` と作業報告
7. **報告**: Issue に完了コメント。失敗時は理由と途中経過を残して
   ロックを外す（`needs-human` ラベルを付ける）

## 安全装置

- 1 回の実行で扱う Issue は既定で 1 件。同時実行はロックで防ぐ
- 検証コマンドが通らない PR は作らない
- 破壊的操作（force push、Issue のクローズ等）はしない
- API キーとトークンは環境変数からのみ読む（`GITHUB_TOKEN`、
  `META_MODEL_API_KEY`）。ファイルに書き出さない
- マージはしない（人間の仕事）。ただし対象リポジトリ側の規約で
  委任されている場合はその規約に従う

## 未決事項

- LLM のツール実行ループの詳細（関数呼び出し形式は Meta Model API の
  仕様に合わせる）
- 複数リポジトリの同時監視を 1 プロセスでやるか、リポジトリごとに
  プロセスを分けるか
- 実行基盤：手元の cron / 常駐プロセス / GitHub Actions のどれを
  標準にするか
