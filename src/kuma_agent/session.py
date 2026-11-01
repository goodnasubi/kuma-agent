"""1 つの部屋（セッション）の対話。履歴管理と道具呼び出しの循環。

文脈の区切りは外側から階層になっている（詳細は docs/DESIGN.md）:
- テナント: 一番外側の区切り。人格・長期記憶・部屋・台帳の置き場が
  テナントごとに分かれる（実効データ置き場は Config.tenant_data_dir）
- ユーザー: テナントの下の区切り。同じテナントでもユーザーごとに
  置き場が分かれ、混ざらない（実効データ置き場は Config.user_data_dir）。
  識別は宣言によるもので認証ではない
- 部屋: 名前付きセッションごとに記録が分かれ、再開時に復元される
- 圧縮: 履歴が長くなったら古い発話をモデル自身に要約させて置き換える
- 長期記憶: セッションを越える事実は Memory（MEMORY.md）が持つ

部屋の中で並行する用事は台帳（TaskBoard）が持ち、進行中の用事は
対話の冒頭でシステムプロンプトに注入して思い出せるようにする。
"""
from __future__ import annotations

from pathlib import Path

from .config import Config
from .llm import Attachment, ChatModel, Message, quoted_content
from .memory import (
    Memory,
    normalize_session_id,
    normalize_tenant_name,
    normalize_user_name,
)
from .persona import load_persona
from .tasks import TaskBoard
from .tools import ToolBox

# 圧縮後の履歴の先頭に置く要約発話の見出し
SUMMARY_PREFIX = "（これまでの会話の要約）"

# 要約役に渡す専用プロンプト（人格プロンプトとは分ける）
_SUMMARY_SYSTEM = (
    "あなたは会話の要約者です。渡された会話の記録を、後から文脈を"
    "引き継げるよう簡潔に要約してください。重要な事実・決定事項・依頼・"
    "未完了の話題は残し、挨拶などの冗長な部分は省いてください。"
    "要約文だけを返してください。"
)


def _message_text(msg: Message, include_quote: bool = False) -> str:
    """発話を記録・要約向けの文字列にする（添付はプレースホルダで表す）。

    記録では引用を別フィールドに持つため、通常は本文だけを使う。
    要約向けでは引用ブロックも含め、どの発話への返信か分かるようにする。
    """
    text = quoted_content(msg) if include_quote else msg.content
    if msg.attachments:
        text = (text + " " if text else "") + " ".join(
            att.placeholder() for att in msg.attachments)
    return text


class Session:
    def __init__(self, cfg: Config, model: ChatModel,
                 session_id: str = "main") -> None:
        self.cfg = cfg
        self.model = model
        self.tenant = normalize_tenant_name(cfg.tenant)
        self.user = normalize_user_name(cfg.user)
        self.session_id = normalize_session_id(session_id)
        # 人格・長期記憶・部屋・台帳はユーザーの実効データ置き場で分ける
        data_dir = cfg.user_data_dir
        self.memory = Memory(data_dir, self.session_id)
        self.task_board = TaskBoard(data_dir)
        self.tools = ToolBox(Path.cwd(), task_board=self.task_board)
        self.history: list[Message] = self._restore_history()
        memory_text = self.memory.load_long_term()
        self.system_prompt = load_persona(data_dir)
        if memory_text:
            self.system_prompt += f"\n\n# 長期記憶\n{memory_text}"
        tasks_text = self._open_tasks_text()
        if tasks_text:
            self.system_prompt += f"\n\n# 進行中の用事\n{tasks_text}"

    def _open_tasks_text(self) -> str:
        """進行中の用事をシステムプロンプト用の箇条書きにする。

        用事がない場合は空文字列（節自体を足さない）。
        """
        lines = []
        for task in self.task_board.open_tasks():
            line = f"- {task.title}"
            if task.memo:
                line += f": {task.memo}"
            lines.append(line)
        return "\n".join(lines)

    def _restore_history(self) -> list[Message]:
        """このセッションの過去の発話を記録から履歴へ復元する。

        添付のバイト列は復元しない（記録に残る要約文の形で文脈に戻る）。
        記録は追記のみなので、要約レコードが現れたら「それ以前の発話は
        この要約に圧縮済み」とみなし、復元済みの発話を要約発話で
        置き換える（メモリ上の圧縮と同じ状態を再現する）。
        """
        history: list[Message] = []
        for record in self.memory.load_turns():
            role = record.get("role")
            content = str(record.get("content", ""))
            if role == "summary":
                history = [Message("user", f"{SUMMARY_PREFIX}\n{content}")]
            elif role in ("user", "assistant"):
                quote = record.get("quote")
                history.append(Message(
                    role, content,
                    quote=quote if isinstance(quote, str) and quote else None,
                ))
        return history

    def say(self, user_text: str, attachments: list[Attachment] | None = None,
            quote: str | None = None) -> str:
        """利用者の発話を受け、道具の循環を経て最終応答を返す。

        添付は履歴の発話に持たせてモデルへ渡す。長期記憶の記録には
        バイト列を残さず「[添付: 名前（種別）]」の要約だけを残す。
        quote は引用返信の引用元の本文で、発話と一緒に記録する。
        """
        atts = list(attachments or [])
        quote_text = quote if quote and quote.strip() else None
        self.history.append(Message(
            "user", user_text, attachments=atts, quote=quote_text))
        self.memory.append_turn(
            "user", _message_text(self.history[-1]), quote=quote_text)
        for _ in range(self.cfg.max_tool_rounds):
            reply = self.model.chat(self.system_prompt, self.history, self.tools.schemas())
            self.history.append(reply)
            if not reply.tool_calls:
                self.memory.append_turn("assistant", reply.content)
                self._maybe_compact()
                return reply.content
            for call in reply.tool_calls:
                result = self.tools.call(call["name"], call.get("args", {}))
                self.history.append(Message("tool", result, tool_call_id=call.get("id"), tool_name=call["name"]))
        return "（道具の呼び出しが上限に達しました）"

    # ---------------------------------------------------------- 圧縮

    def _maybe_compact(self) -> None:
        """履歴の発話数が閾値を超えたら、古い発話を要約 1 件に置き換える。

        user/assistant の発話数を数え、直近 compact_keep 件を残して
        それより古い部分を要約する。要約に失敗しても履歴は壊さず、
        その回は圧縮を諦める（例外は外に漏らさない）。
        """
        threshold = self.cfg.compact_after
        if threshold <= 0:
            return
        turn_idx = [i for i, m in enumerate(self.history)
                    if m.role in ("user", "assistant")]
        if len(turn_idx) <= threshold:
            return
        keep = max(0, self.cfg.compact_keep)
        if len(turn_idx) - keep <= 0:
            return  # 残すべき発話だけで閾値以下にならない場合は何もしない
        cutoff = turn_idx[len(turn_idx) - keep] if keep else len(self.history)
        try:
            summary = self._summarize(self.history[:cutoff])
        except Exception:
            return
        if not summary:
            return
        kept = self.history[cutoff:]
        self.history = [Message("user", f"{SUMMARY_PREFIX}\n{summary}")] + kept
        self.memory.append_summary(summary)
        # 再開時に「要約＋残した直近の発話」を再現できるよう、残した
        # 発話を要約の後ろへ記録し直す（記録は追記のみのため）。
        for msg in kept:
            if msg.role in ("user", "assistant"):
                self.memory.append_turn(msg.role, msg.content, quote=msg.quote)

    def _summarize(self, messages: list[Message]) -> str:
        """発話の並びをモデル自身に要約させる（道具なし、要約専用）。"""
        lines = []
        for msg in messages:
            if msg.role == "user":
                lines.append(f"利用者: {_message_text(msg, include_quote=True)}")
            elif msg.role == "assistant":
                if msg.content or msg.quote:
                    lines.append(f"クマ: {_message_text(msg, include_quote=True)}")
            elif msg.role == "tool":
                lines.append(f"（道具の結果: {msg.content}）")
        prompt = Message("user", "\n".join(lines))
        reply = self.model.chat(_SUMMARY_SYSTEM, [prompt], [])
        return reply.content.strip()
