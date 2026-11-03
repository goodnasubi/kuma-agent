"""コンテナ一式（Dockerfile など）とアバター候補のテスト。

Docker が無い環境でも、ファイルの存在と要点の記載、それに
load_avatar_svg の候補まわりだけはテストで担保する。
"""
from pathlib import Path

from kuma_agent import web

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(name: str) -> str:
    path = REPO_ROOT / name
    assert path.is_file(), f"{name} がありません"
    return path.read_text(encoding="utf-8")


def test_dockerfile_points() -> None:
    text = _read("Dockerfile")
    assert "FROM python:3.12-slim" in text
    assert "pip install --no-cache-dir" in text
    assert "KUMA_DATA_DIR=/data" in text
    assert 'VOLUME ["/data"]' in text
    assert "EXPOSE 8765" in text
    assert '"kuma-agent", "web", "--host", "0.0.0.0", "--port", "8765"' in text
    assert "HEALTHCHECK" in text
    assert "PYTHONUNBUFFERED=1" in text
    # root 以外の専用ユーザーで実行する
    assert "USER kuma" in text
    assert "USER root" not in text
    # Node.js は公式イメージから /usr/local をコピーして入れる
    assert "FROM node:22-slim AS node" in text
    assert "COPY --from=node /usr/local /usr/local" in text
    # ブラウザ操作用の Chromium と日本語フォントを apt で入れる
    assert "apt-get install" in text
    assert "chromium" in text
    assert "fonts-noto-cjk" in text
    # MCP サーバー 2 つをグローバル導入し、コマンド名で起動できる
    assert "npm install -g" in text
    assert "@playwright/mcp" in text
    assert "@cocal/google-calendar-mcp" in text
    # HOME は /data/home（トークン等の状態がボリュームに残る）
    assert "HOME=/data/home" in text
    assert "mkdir -p /data/home" in text
    assert "chown -R kuma:kuma /data" in text


def test_compose_points() -> None:
    text = _read("docker-compose.yml")
    assert "build: ." in text
    assert '"8765:8765"' in text
    assert "env_file" in text
    assert "- .env" in text
    assert ":/data" in text
    assert "restart: unless-stopped" in text
    # 公開時の注意（パスワード設定の必須）がコメントに書いてある
    assert "KUMA_WEB_PASSWORD" in text
    assert "ログインなしでは" in text
    # お試し公開のトンネル（プロファイル指定のときだけ起動する）
    assert "cloudflare/cloudflared" in text
    assert "tunnel" in text


def test_env_example_points() -> None:
    text = _read(".env.example")
    for name in (
        "KUMA_LLM_PROVIDER",
        "GEMINI_API_KEY",
        "META_MODEL_API_KEY",
        "ANTHROPIC_API_KEY",
        "KUMA_TELEGRAM_BOT_TOKEN",
        "KUMA_TELEGRAM_ALLOWED_CHATS",
    ):
        assert f"{name}=" in text
    # キーの実値は空（見本に秘密を書かない）
    for line in text.splitlines():
        if "API_KEY=" in line or "BOT_TOKEN=" in line:
            assert line.endswith("="), line


def test_dockerignore_points() -> None:
    lines = {
        line.strip()
        for line in _read(".dockerignore").splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert ".env" in lines  # 秘密をイメージに含めない
    assert ".git" in lines
    assert ".venv" in lines
    assert "data" in lines
    assert "tests" in lines
    assert "__pycache__" in lines


def test_avatar_candidates_include_cwd(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    candidates = web._avatar_svg_candidates()
    assert tmp_path / "assets" / "avatar.svg" in candidates


def test_load_avatar_svg_reads_candidate(tmp_path: Path, monkeypatch) -> None:
    svg = tmp_path / "avatar.svg"
    svg.write_text("<svg>テスト</svg>", encoding="utf-8")
    monkeypatch.setattr(web, "_avatar_svg_candidates", lambda: [svg])
    assert web.load_avatar_svg() == "<svg>テスト</svg>"


def test_load_avatar_svg_falls_back_to_later_candidate(
    tmp_path: Path, monkeypatch
) -> None:
    svg = tmp_path / "assets" / "avatar.svg"
    svg.parent.mkdir()
    svg.write_text("<svg>2番目</svg>", encoding="utf-8")
    monkeypatch.setattr(
        web,
        "_avatar_svg_candidates",
        lambda: [tmp_path / "無い.svg", svg],
    )
    assert web.load_avatar_svg() == "<svg>2番目</svg>"


def test_load_avatar_svg_fallback_when_missing(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        web, "_avatar_svg_candidates", lambda: [tmp_path / "無い.svg"]
    )
    assert web.load_avatar_svg() == web._FALLBACK_AVATAR
