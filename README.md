# kuma-agent

Muse を頭脳にした自律開発エージェント。GitHub の Issue を拾い、実装して
Pull Request を出すまでの流れを、どのリポジトリにも使える形にしたもの
（Sigil リポジトリで運用している複数エージェント開発方式の汎用版）。

## 仕組み

1. 対象リポジトリのオープン Issue を定期的に見る
2. `agent:kuma` ラベルで排他ロックし、着手コメントでブランチ名を宣言する
3. 専用ブランチで LLM（Meta Model API の Muse）が実装し、テストを通す
4. PR を作り、完了を Issue に報告する。人間は仕様とマージ判断に集中する

詳しい設計は [docs/DESIGN.md](docs/DESIGN.md) を参照。

## 状態

設計フェーズ。雛形コードと設計メモのみ。

## 使い方（予定）

```bash
pip install -e .
kuma-agent run --repo owner/name --once   # 1 回だけ Issue を処理
kuma-agent run --repo owner/name          # 常駐して監視
```
