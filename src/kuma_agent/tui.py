"""ターミナル UI。標準ライブラリの curses で対話する。

画面構成: 上部ヘッダー（クマ / プロバイダ名）、中央に会話ログ、
下部に入力欄。Enter で送信、Ctrl-C か /quit で終了する。

日本語などの全角文字は表示幅の計算が端末依存で崩れることがある。
そのため描画まわりは try/except で守り、崩れても落ちないことを優先する。
"""
from __future__ import annotations

import threading
import unicodedata
from collections.abc import Iterable, Sequence
from pathlib import Path

from .llm import (
    MAX_ATTACHMENT_BYTES,
    Attachment,
    Message,
    guess_media_type,
    quoted_content,
)
from .session import THINKING, Session

USER_LABEL = "あなた"
ASSISTANT_LABEL = "クマ"
QUIT_COMMAND = "/quit"
ATTACH_COMMAND = "/attach"
QUOTE_COMMAND = "/quote"


# ---------------------------------------------------------------- 純粋関数

def display_width(text: str) -> int:
    """端末上のおおよその表示幅（全角は 2、半角は 1）。"""
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def wrap_display(text: str, width: int) -> list[str]:
    """表示幅に合わせて折り返す。改行はそのまま区切りとして扱う。"""
    if width < 1:
        width = 1
    lines: list[str] = []
    for raw in text.split("\n"):
        if raw == "":
            lines.append("")
            continue
        current, current_width = "", 0
        for ch in raw:
            ch_width = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            if current and current_width + ch_width > width:
                lines.append(current)
                current, current_width = "", 0
            current += ch
            current_width += ch_width
        lines.append(current)
    return lines


def visible_messages(messages: Iterable[Message]) -> list[Message]:
    """画面に出す発話だけを順番に取り出す。

    道具のやり取りと、本文も添付も無い発話は除く。通し番号は
    この並びに対して付ける。
    """
    visible: list[Message] = []
    for msg in messages:
        if msg.role not in ("user", "assistant"):
            continue
        if msg.content or msg.attachments or msg.quote:
            visible.append(msg)
    return visible


def numbered_messages(
        messages: Iterable[Message]) -> list[tuple[int, Message]]:
    """表示対象の発話に 1 始まりの通し番号を付ける。"""
    return list(enumerate(visible_messages(messages), start=1))


def quote_text_for_number(messages: Iterable[Message],
                          number: int) -> str | None:
    """通し番号に対応する発話の引用文を取り出す。

    本文が無い発話では添付のプレースホルダを引用文にする。
    対応する番号が無ければ None。
    """
    for num, msg in numbered_messages(messages):
        if num != number:
            continue
        if msg.content:
            return msg.content
        if msg.attachments:
            return " ".join(att.placeholder() for att in msg.attachments)
        return msg.quote
    return None


def format_log(messages: Iterable[Message], width: int = 80) -> list[str]:
    """会話履歴を表示用の行リストに整形する。

    道具のやり取り（role == "tool" や道具呼び出しだけの応答）は
    画面には出さず、利用者とクマの発話だけを見せる。各発話には
    `/quote` で指定するための通し番号を付け、引用がある発話は
    引用ブロックを本文の前に表示する。添付はプレースホルダ文で
    併記する。
    """
    lines: list[str] = []
    for number, msg in numbered_messages(messages):
        text = quoted_content(msg)
        if msg.attachments:
            marks = " ".join(att.placeholder() for att in msg.attachments)
            text = f"{text} {marks}" if text else marks
        if not text:
            continue
        speaker = USER_LABEL if msg.role == "user" else ASSISTANT_LABEL
        label = f"[{number}] {speaker}"
        body = wrap_display(text, max(1, width - display_width(label) - 2))
        lines.append(f"{label}: {body[0]}" if body else f"{label}:")
        indent = " " * (display_width(label) + 2)
        lines.extend(indent + line for line in body[1:])
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def is_quit(text: str) -> bool:
    """終了コマンドかどうか。"""
    return text.strip() == QUIT_COMMAND


def parse_attach_command(text: str) -> str | None:
    """`/attach <パス>` ならパスを、そうでなければ None を返す。

    パスが無い `/attach` 単体は空文字列を返す（呼び出し側で案内する）。
    """
    stripped = text.strip()
    if stripped == ATTACH_COMMAND:
        return ""
    if stripped.startswith(ATTACH_COMMAND + " "):
        return stripped[len(ATTACH_COMMAND):].strip()
    return None


def parse_quote_command(text: str) -> int | None:
    """`/quote <番号>` なら番号を、そうでなければ None を返す。

    番号が無い・数値でない・1 未満の場合は 0 を返す（呼び出し側で
    使い方を案内する）。
    """
    stripped = text.strip()
    if stripped == QUOTE_COMMAND:
        return 0
    if not stripped.startswith(QUOTE_COMMAND + " "):
        return None
    raw = stripped[len(QUOTE_COMMAND):].strip()
    try:
        number = int(raw)
    except ValueError:
        return 0
    return number if number >= 1 else 0


def status_dots(frame: int) -> str:
    """作業中表示の点。フレームが進むたびに 0〜3 個を繰り返す。"""
    return "." * (frame % 4)


def status_line(text: str, frame: int) -> str:
    """作業中表示の 1 行（今の作業の文言＋うごめく点）。

    文言が空なら表示自体を出さない（空文字列を返す）。
    """
    if not text:
        return ""
    return f"{text}{status_dots(frame)}"


def load_attachment(path_text: str, base_dir: Path | None = None) -> Attachment:
    """パスのファイルを読み込んで Attachment にする。

    相対パスは base_dir（無ければカレントディレクトリ）基準で解釈する。
    読めない場合や上限（MAX_ATTACHMENT_BYTES）超過は例外にする。
    """
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = (base_dir or Path.cwd()) / path
    data = path.read_bytes()
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError(
            f"添付が大きすぎます（上限 {MAX_ATTACHMENT_BYTES} バイト）: {path.name}")
    return Attachment(name=path.name, media_type=guess_media_type(path.name), data=data)


# ---------------------------------------------------------------- curses 本体

def run_tui(session: Session, provider: str) -> int:
    """curses の対話画面を起動する。終了コードを返す。"""
    import curses

    def main(stdscr: "curses.window") -> None:
        _tui_loop(stdscr, session, provider)

    try:
        curses.wrapper(main)
    except Exception:
        # curses が使えない端末では諦めて呼び出し側に知らせる
        raise
    return 0


def _safe_addstr(win: object, y: int, x: int, text: str) -> None:
    """addstr の失敗（画面端・文字化け等）で落とさないための薄い包み。"""
    try:
        win.addstr(y, x, text)  # type: ignore[attr-defined]
    except Exception:
        pass


def _tui_loop(stdscr: object, session: Session, provider: str) -> None:
    import curses

    try:
        curses.curs_set(1)
    except Exception:
        pass
    stdscr.keypad(True)  # type: ignore[attr-defined]
    try:
        # 応答待ちの間も作業中表示を更新できるよう、キー読み取りに
        # タイムアウトを付けてポーリングにする
        stdscr.timeout(100)  # type: ignore[attr-defined]
    except Exception:
        pass

    buffer = ""  # 入力中の文字列
    offset = 0  # ログのスクロール量（0 で最下部に追従）
    pending: list[Attachment] = []  # 次の発話に添える添付
    pending_quote: str | None = None  # 次の発話で引用する過去の発話
    # 作業中表示の共有状態。Session からの通知で text が入れ替わり、
    # say を回すワーカースレッドの完了で done が立つ
    status: dict[str, object] = {"text": THINKING, "busy": False, "done": False}
    session.on_status = lambda text: status.__setitem__("text", text)
    frame = 0  # 作業中表示の点のアニメーション用カウンタ

    def notice(text: str) -> None:
        session.history.append(Message("assistant", text))

    def start_say(text: str, atts: list[Attachment],
                  quote: str | None) -> None:
        """応答待ちの間も画面を更新できるよう、say を別スレッドで回す。"""

        def work() -> None:
            try:
                session.say(text, atts, quote=quote)
            except Exception as exc:
                session.history.append(Message("assistant", f"（エラー: {exc}）"))
            finally:
                pending.clear()
                status["done"] = True

        status["busy"] = True
        status["done"] = False
        status["text"] = THINKING
        threading.Thread(target=work, daemon=True).start()

    def redraw() -> None:
        nonlocal frame
        frame += 1
        height, width = stdscr.getmaxyx()  # type: ignore[attr-defined]
        stdscr.erase()  # type: ignore[attr-defined]
        header = (f" クマ ─ provider: {provider} ─ Enter: 送信 / "
                  f"{QUOTE_COMMAND} <番号>: 引用 / "
                  f"{ATTACH_COMMAND} <パス>: 添付 / {QUIT_COMMAND}: 終了")
        _safe_addstr(stdscr, 0, 0, header[: max(0, width - 1)])
        try:
            stdscr.hline(1, 0, curses.ACS_HLINE, max(0, width - 1))  # type: ignore[attr-defined]
        except Exception:
            _safe_addstr(stdscr, 1, 0, "-" * max(0, width - 1))

        busy = bool(status["busy"])
        # 作業中表示の行（入力欄の上）を出す間はログを 1 行短くする
        log_height = max(0, height - 4 - (1 if busy else 0))
        lines = format_log(session.history, width - 2)
        view = lines[max(0, len(lines) - log_height - offset):]
        if offset:
            view = view[:log_height]
        else:
            view = view[-log_height:] if log_height else []
        for i, line in enumerate(view):
            _safe_addstr(stdscr, 2 + i, 0, line)

        if busy:
            shown_status = status_line(str(status["text"]), frame)
            if shown_status:
                _safe_addstr(stdscr, height - 2, 0,
                             f"{ASSISTANT_LABEL}: {shown_status}")

        prompt = "> "
        shown = prompt + buffer
        # 入力欄は 1 行に収め、末尾が見えるように左を切り落とす
        while display_width(shown) > max(0, width - 1) and len(shown) > len(prompt):
            shown = prompt + shown[len(prompt) + 1:]
        _safe_addstr(stdscr, height - 1, 0, shown)
        try:
            stdscr.move(height - 1, min(width - 1, display_width(shown)))  # type: ignore[attr-defined]
        except Exception:
            pass
        stdscr.refresh()  # type: ignore[attr-defined]

    while True:
        if status["busy"] and status["done"]:
            status["busy"] = False
            status["text"] = ""
        redraw()
        try:
            key = stdscr.get_wch()  # type: ignore[attr-defined]
        except KeyboardInterrupt:
            return
        except Exception:
            key = None  # タイムアウト: キーなしとして再描画へ戻る
        if key is None:
            continue

        if status["busy"]:
            # 応答待ちの間はスクロールと終了だけを受け付ける
            if key == "\x03":  # Ctrl-C
                return
            if key == curses.KEY_PPAGE:
                offset += 5
            elif key == curses.KEY_NPAGE:
                offset = max(0, offset - 5)
            continue

        if key == "\x03":  # Ctrl-C
            return
        if key in ("\n", "\r"):
            text = buffer.strip()
            buffer = ""
            if not text:
                continue
            if is_quit(text):
                return
            offset = 0
            attach_path = parse_attach_command(text)
            if attach_path is not None:
                if not attach_path:
                    notice(f"（使い方: {ATTACH_COMMAND} <ファイルのパス>）")
                    continue
                try:
                    att = load_attachment(attach_path, Path.cwd())
                except (OSError, ValueError) as exc:
                    notice(f"（添付できませんでした: {exc}）")
                    continue
                pending.append(att)
                notice(f"（添付しました: {att.name}。次の発話に添付されます）")
                continue
            quote_number = parse_quote_command(text)
            if quote_number is not None:
                if quote_number == 0:
                    notice(f"（使い方: {QUOTE_COMMAND} <発話の番号>）")
                    continue
                quote_text = quote_text_for_number(session.history, quote_number)
                if quote_text is None:
                    notice(f"（[{quote_number}] の発話が見つかりません）")
                    continue
                pending_quote = quote_text
                notice(f"（引用を設定しました: [{quote_number}] {quote_text}"
                       "。次の発話に引用されます）")
                continue
            quote = pending_quote
            pending_quote = None
            start_say(text, list(pending), quote)
            continue
        if isinstance(key, str) and key in ("\x7f", "\b"):  # Backspace / Delete
            buffer = buffer[:-1]
            continue
        if key == curses.KEY_BACKSPACE:
            buffer = buffer[:-1]
            continue
        if key == curses.KEY_PPAGE:
            offset += 5
            continue
        if key == curses.KEY_NPAGE:
            offset = max(0, offset - 5)
            continue
        if isinstance(key, str) and key.isprintable():
            buffer += key


def tail_lines(lines: Sequence[str], count: int) -> list[str]:
    """末尾 count 行を取り出す（テスト用の小さな補助）。"""
    if count <= 0:
        return []
    return list(lines[-count:])
