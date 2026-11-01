# kuma-agent

Muse を頭脳にした、対話型の相棒エージェント。人格と記憶を持ち、
会話しながら道具を使って用事をこなす、個人専用のエージェントを
自分の手で作るためのプロジェクト。

## 大事にすること

- **人格**: 誰として振る舞うかをファイル（SOUL.md / IDENTITY.md）で決める
- **記憶**: 会話を越えて残る長期記憶（MEMORY.md）と、人や予定の記録を持つ
- **道具**: ファイル、シェル、カレンダーなど、現実の作業ができる
- **対話が中心**: 指示待ちの自動化ではなく、話しながら一緒に進める相棒

詳しい設計は [docs/DESIGN.md](docs/DESIGN.md) を参照。

## 状態

対話ループまで実装済み。頭脳は 5 種から選べます（`KUMA_LLM_PROVIDER`）:
Meta Model API（Muse）/ Claude API / Gemini API と、Claude Code CLI /
Gemini CLI（こちらは API キー不要、各 CLI のログインを使います）。

## 使い方（予定）

```bash
pip install -e .
kuma-agent chat          # 対話を開始する
```
