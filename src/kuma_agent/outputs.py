"""成果物（利用者に渡したファイル）の置き場。

エージェントが作ったレポートや文書などのファイルを、利用者が
Web 画面から開ける（ダウンロードできる）形で渡すための仕組み。
成果物はユーザーごとの置き場に outputs/ フォルダを作ってコピーし、
一覧は outputs/index.json（ファイル名・タイトル・サイズ・登録日時）
に保持する。

一覧の読み書きは定期ジョブ（JobBoard）と同じ流儀にする:
- 書き込みの前にはファイルを読み直し、他のセッションの登録を
  消さないようにする
- ファイルが無い・壊れている場合は空の一覧として始める
  （例外で落とさない）。壊れたレコードは読み飛ばす

成果物は「利用者に渡したもの」なので、保持期限の自動クリーン
アップの対象には含めない（詳細は docs/DESIGN.md の「成果物」節）。
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

OUTPUTS_DIRNAME = "outputs"
INDEX_FILENAME = "index.json"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def format_size(size: int) -> str:
    """バイト数を表示用に整える（例: 512 B、1.5 KB、2.0 MB）。"""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return str(size)
    if value < 1024:
        return f"{int(value)} B"
    for unit in ("KB", "MB", "GB"):
        value /= 1024
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
    return f"{value:.1f} GB"  # 到達しない（上のループで必ず返す）


def format_created_at(raw: str) -> str:
    """登録日時を表示用に整える（例: 2026/11/2 9:30）。

    読めない文字列はそのまま返す（手編集されたデータでも表示が
    壊れないように）。
    """
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return raw
    return (f"{parsed.year}/{parsed.month}/{parsed.day} "
            f"{parsed.hour}:{parsed.minute:02d}")


@dataclass
class Output:
    """成果物 1 件（index.json の 1 レコード）。

    filename は outputs/ 内のファイル名（パスは含まない）、
    title は画面に出す表示名、size はバイト数、created_at は
    登録日時（ISO 8601）。
    """
    filename: str
    title: str
    size: int = 0
    created_at: str = field(default_factory=_now)

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "title": self.title,
            "size": self.size,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, record: object) -> Output | None:
        """辞書から復元する。形が不正なら None（読み飛ばす）。"""
        if not isinstance(record, dict):
            return None
        # 初期の手書きデータなどで "name" と書かれている場合も受ける
        filename = record.get("filename") or record.get("name")
        if not isinstance(filename, str) or not filename:
            return None
        if Path(filename).name != filename:
            return None  # パスを含む名前は成果物として扱わない
        size = record.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            size = 0
        title = record.get("title")
        return cls(
            filename=filename,
            title=title if isinstance(title, str) and title else filename,
            size=size,
            created_at=str(record.get("created_at") or _now()),
        )


class OutputBoard:
    """成果物の一覧。outputs/ フォルダと index.json への保存・読み込みを持つ。

    用事の一覧（TaskBoard）や定期ジョブ（JobBoard）と同じ流儀で、
    書き込みの前にはファイルを読み直し、他のセッションの登録を
    消さないようにする。
    """

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.outputs_dir = data_dir / OUTPUTS_DIRNAME
        self.index_file = self.outputs_dir / INDEX_FILENAME
        self._outputs: list[Output] = self._load()

    # ------------------------------------------------------------ 永続化

    def _load(self) -> list[Output]:
        if not self.index_file.exists():
            return []
        try:
            raw = json.loads(self.index_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []  # 壊れたファイルは空として始める
        if not isinstance(raw, list):
            return []
        return [output for record in raw
                if (output := Output.from_dict(record)) is not None]

    def _save(self) -> None:
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        text = json.dumps([o.to_dict() for o in self._outputs],
                          ensure_ascii=False, indent=2)
        self.index_file.write_text(text + "\n", encoding="utf-8")

    # ------------------------------------------------------------ 操作

    def list(self) -> list[Output]:
        """成果物の一覧（登録順）。返す前にファイルを読み直す。"""
        self._outputs = self._load()
        return list(self._outputs)

    def get(self, filename: str) -> Output | None:
        """ファイル名で成果物を探す。一覧を読み直してから探す。"""
        for output in self.list():
            if output.filename == filename:
                return output
        return None

    def _unique_filename(self, source: Path) -> str:
        """outputs/ 内で衝突しないファイル名を決める。

        同名のファイルが既にある（または index に登録済みの）場合は、
        拡張子の前に連番を付ける（report.txt → report-2.txt →
        report-3.txt …）。添付の保存（agent_cli）と同じ流儀。
        成果物置き場の中にあるファイル自身を登録する場合は、
        そのファイルとは衝突扱いにしない（自分のコピーで上書き
        しないため）。
        """
        used = {o.filename for o in self._outputs}
        try:
            inside_self = source.resolve().parent == self.outputs_dir.resolve()
        except OSError:
            inside_self = False
        if inside_self:
            used.discard(source.name)
        candidate = source.name
        suffix = 1
        while candidate in used or (
                (self.outputs_dir / candidate).exists()
                and not (inside_self and candidate == source.name)):
            suffix += 1
            candidate = f"{source.stem}-{suffix}{source.suffix}"
        return candidate

    def add(self, source: Path, title: str | None = None) -> Output:
        """ファイルを outputs/ へコピーし、index に登録して保存する。

        source は実在する通常ファイルでなければならない（無ければ
        FileNotFoundError、ディレクトリなら ValueError）。title が
        無ければファイル名を表示名にする。同名の成果物がある場合は
        連番を付けて衝突を避ける。
        """
        source = Path(source)
        if not source.exists():
            raise FileNotFoundError(f"ファイルが見つかりません: {source}")
        if not source.is_file():
            raise ValueError(f"ファイルではありません: {source}")
        self._outputs = self._load()  # 他のセッションの登録を消さないよう読み直す
        filename = self._unique_filename(source)
        self.outputs_dir.mkdir(parents=True, exist_ok=True)
        dest = self.outputs_dir / filename
        try:
            same = source.resolve() == dest.resolve()
        except OSError:
            same = False
        if not same:
            shutil.copyfile(source, dest)
        display_title = str(title or "").strip() or source.name
        output = Output(filename=filename, title=display_title,
                        size=dest.stat().st_size)
        self._outputs.append(output)
        self._save()
        return output

    def resolve_download(self, name: str | None) -> Path | None:
        """ダウンロードしてよいファイルのパスを返す。許可外は None。

        許可するのは index.json に登録されているファイル名だけ。
        名前にパス区切りが含まれるもの（../ など）は basename の
        一致で弾き、実ファイルが outputs/ の直下に実在することも
        確認する（パストラバーサルができない作り）。
        """
        if not name or not isinstance(name, str):
            return None
        if Path(name).name != name:
            return None
        if self.get(name) is None:
            return None
        path = self.outputs_dir / name
        try:
            if path.resolve().parent != self.outputs_dir.resolve():
                return None
        except OSError:
            return None
        if not path.is_file():
            return None
        return path
