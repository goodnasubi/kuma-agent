# kuma-agent

<p align="center">
  <img src="assets/avatar.png" alt="kuma-agent のアバター：クマ" width="220">
</p>


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
kuma-agent chat          # 対話を開始する（プレーンな対話ループ）
kuma-agent chat --tui    # ターミナル UI（curses）で対話する
kuma-agent web           # Web UI を http://127.0.0.1:8765 で起動する
```

Web UI は `--host` / `--port` で待ち受け先を変えられますが、認証を
持たない単一ユーザー前提の画面なので、既定の 127.0.0.1 から
外に出す場合は注意してください。

## 画面イメージ

Web UI（`kuma-agent web`）:

![Web UI の画面](assets/screenshot-web.png)

TUI（`kuma-agent chat --tui`）:

![TUI の画面](assets/screenshot-tui.png)

## アバターとイメージカラー

アバターは、ココアブラウンのパーカーを着た若いクマ。大きく構えず、
近くにいて、頼れる相棒のイメージ。公式アバターは `assets/avatar.png`。
小さいサイズやファビコン用に、図形だけの簡略マーク
`assets/avatar.svg` も用意している。

| 役割 | 色 | コード | 由来 |
|---|---|---|---|
| メイン（毛色） | ハニーアンバー | `#E79A3C` | 蜂蜜の色。温かさと親しみ |
| 濃色（輪郭・背景） | ココアブラウン | `#4B3226` | クマの毛色と土の色。落ち着きと信頼 |
| 明色（口元・背景） | クリーム | `#FFF3E0` | やわらかい明るさ |
| アクセント | 若葉グリーン | `#7FB069` | 森の色。成長と新しさ |

- 迷ったらメインと濃色の 2 色で構成する（アバターがその基準形）
- アクセントはリンクや強調など、少量にだけ使う
- 背景はクリームを基本にし、白より少し温かい画面にする
