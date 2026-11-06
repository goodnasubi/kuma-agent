"""1 つの部屋（セッション）の対話。履歴管理と道具呼び出しの循環。

文脈の区切りは外側から階層になっている（詳細は docs/DESIGN.md）:
- テナント: 一番外側の区切り。人格・長期記憶・部屋・用事の記録の置き場が
  テナントごとに分かれる（実効データ置き場は Config.tenant_data_dir）
- ユーザー: テナントの下の区切り。同じテナントでもユーザーごとに
  置き場が分かれ、混ざらない（実効データ置き場は Config.user_data_dir）。
  識別は宣言によるもので認証ではない
- 部屋: 名前付きセッションごとに記録が分かれ、再開時に復元される
- 圧縮: 履歴が長くなったら古い発話をモデル自身に要約させて置き換える
- 長期記憶: セッションを越える事実は Memory（MEMORY.md）が持つ

会話の中で並行する用事は用事の一覧（TaskBoard）が持ち、進行中の用事は
対話の冒頭でシステムプロンプトに注入して思い出せるようにする。
用事の一覧は用事の持ち主であるユーザーごとに 1 つだけを持ち（階層は
テナント＞ユーザー＞用事の一覧）、ファイルはユーザー置き場直下の
tasks.json に置く。

会話の分け方はモード（Config.mode）で選べる:
- ledger（相棒モード、既定）: 部屋は実質 1 つ（main）に固定し、
  進行中の用事はすべて注入する。セッション ID の指定は無視される
- wall（スレッドモード）: 会話を部屋（スレッド）ごとに分ける。
  用事の一覧は無効で、道具に用事系を登録せず、進行中の用事の注入もしない
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import transcribe
from .config import Config, normalize_mode
from .delegate import DELEGATE_NOTICE, run_delegated_task
from .jobs import JobBoard
from .llm import Attachment, ChatModel, GeminiClient, Message, quoted_content
from .mcp_client import McpManager, get_shared_manager, resolve_config_dir
from .memory import (
    DEFAULT_SESSION_ID,
    Memory,
    format_conversation_hit,
    memory_inject_text,
    normalize_session_id,
    normalize_tenant_name,
    normalize_user_name,
)
from .outputs import OutputBoard
from .persona import load_persona
from .settings import approvals_enabled_for
from .skills import load_skills
from .tasks import TaskBoard, format_remind_at
from .tools import Tool, ToolBox, activity_label
from .usage import record_usage
from .watches import WatchBoard
from .worksteps import WorkSteps, format_steps

# 曜日（月曜始まり、datetime.weekday() の並び）
_WEEKDAYS_JA = ("月", "火", "水", "木", "金", "土", "日")


def current_time_text(now: datetime | None = None) -> str:
    """モデルへ渡す現在日時の文（サーバーのローカル時刻、曜日つき）。

    「明日9時に知らせて」のような相対指定を利用者が言ったとき、
    モデルが絶対時刻（ISO 8601）に直せるよう、発話のたびに新しい
    時刻をシステムプロンプトへ添える。
    """
    now = now or datetime.now()
    iso = now.isoformat(timespec="seconds")
    human = (f"{now.year}年{now.month}月{now.day}日"
             f"（{_WEEKDAYS_JA[now.weekday()]}）"
             f"{now.hour}:{now.minute:02d}")
    return (f"現在日時は {iso}（{human}）です。"
            "時刻はサーバーのローカル時刻です。"
            "「明日」「来週月曜」のような指定はこの日時を基準に"
            "絶対時刻へ直してください。")

# 圧縮後の履歴の先頭に置く要約発話の見出し
SUMMARY_PREFIX = "（これまでの会話の要約）"

# 作業中表示の文言（UI がそのまま出す）。道具の作業名は
# tools.activity_label が道具ごとに出す。作業が終わったら空文字列を
# 通知して「表示なし」に戻す。
THINKING = "考え中"
SUMMARIZING = "会話を要約中"
TRANSCRIBING = "音声を文字にしています"
# 承認待ちと、その結果の文言。結果は次の状態通知ですぐ上書き
# されるが、ポーリング間隔によっては画面に残ることもある
APPROVAL_DENIED = "承認されませんでした"


def approval_waiting_text(name: str) -> str:
    """承認待ちの作業中表示（どの道具の承認待ちか添える）。"""
    return f"承認を待っています（{name}）"

# 要約役に渡す専用プロンプト（人格プロンプトとは分ける）
_SUMMARY_SYSTEM = (
    "あなたは会話の要約者です。渡された会話の記録を、後から文脈を"
    "引き継げるよう簡潔に要約してください。重要な事実・決定事項・依頼・"
    "未完了の話題は残し、挨拶などの冗長な部分は省いてください。"
    "要約文だけを返してください。"
)


def _model_name(model: ChatModel) -> str:
    """使用量の記録に載せるモデル名をクライアントから引く。

    クライアントは実効モデル名を model 属性に持ち、未指定の場合は
    クラス既定（DEFAULT_MODEL）を使う作りなので、その順に引く。
    どちらも無い頭脳（テスト用のスタブ等）は空文字列にする。
    """
    name = getattr(model, "model", None) or getattr(model, "DEFAULT_MODEL", None)
    return str(name) if name else ""


def _message_text(msg: Message, include_quote: bool = False) -> str:
    """発話を記録・要約向けの文字列にする（添付はプレースホルダで表す）。

    記録では引用を別フィールドに持つため、通常は本文だけを使う。
    要約向けでは引用ブロックも含め、どの発話への返信か分かるようにする。
    音声の文字起こしがある場合は、添付の要約として文章も残す
    （バイト列は残さないという既存の流儀は変えない）。
    """
    text = quoted_content(msg) if include_quote else msg.content
    if msg.attachments:
        text = (text + " " if text else "") + " ".join(
            att.model_note() for att in msg.attachments)
    return text


class Session:
    def __init__(self, cfg: Config, model: ChatModel,
                 session_id: str = "main",
                 on_status: Callable[[str], None] | None = None,
                 mcp_manager: McpManager | None = None,
                 approver: Callable[[str, dict], bool] | None = None,
                 delegate_child: bool = False) -> None:
        self.cfg = cfg
        self.model = model
        # 作業中表示のコールバック。対話ループの各段階で「今なにを
        # しているか」を文字列で通知し、終わったら空文字列を通知する。
        # UI はこれを受けて表示を切り替える。属性として差し替えも可。
        self.on_status = on_status
        # UI 側の承認者: 承認対象の道具（run_shell と MCP 道具）を
        # 実行する前に（道具名, 引数）で呼ばれ、True のときだけ実行
        # する。UI（Web / CLI / TUI）が属性として差し込む。承認が
        # 有効なテナントで承認者が付いていない場合は、安全側に倒
        # して常に却下になる（_check_approval）。属性として差し
        # 替えも可。
        self.approver = approver
        # 委任で作られた子セッションかどうか。子は会話を親と混ぜ
        # ないため、相棒モードでも部屋の固定（main）の対象にせず、
        # delegate- で始まる自分のセッション ID をそのまま使う。
        # また子には delegate 道具自体を登録しない（委任は1段まで）
        self.delegate_child = delegate_child
        self.tenant = normalize_tenant_name(cfg.tenant)
        self.user = normalize_user_name(cfg.user)
        self.mode = normalize_mode(cfg.mode)
        # 相棒モードでは部屋は実質 1 つに固定する（指定は無視する）
        if self.mode == "ledger" and not delegate_child:
            self.session_id = DEFAULT_SESSION_ID
        else:
            self.session_id = normalize_session_id(session_id)
        # 人格・長期記憶・部屋・用事の記録はユーザーの実効データ置き場で分ける
        data_dir = cfg.user_data_dir
        self.memory = Memory(data_dir, self.session_id)
        # 用事の一覧はユーザーごとに 1 つ（tasks.json）だけを使う
        self.task_board = TaskBoard(data_dir)
        # 定期ジョブもユーザーごとに 1 つ（jobs.json）だけを使う。
        # 用事系と違い、定期ジョブの道具はモードを問わず登録する
        self.job_board = JobBoard(data_dir)
        # 成果物の置き場もユーザーごとに 1 つ（outputs/）だけを使う。
        # 定期ジョブと同じく、成果物の道具はモードを問わず登録する
        self.output_board = OutputBoard(data_dir)
        # 作業の手順（エージェント自身の作業メモ）は会話ごとに持つ。
        # 相棒モードではセッションが main に固定されるため利用者
        # ごとに 1 冊になり、スレッドモードではスレッドごとに
        # 分かれる。用事系と違い、道具はモードを問わず登録する
        self.work_steps = WorkSteps(data_dir, self.session_id)
        # 見張りもユーザーごとに 1 つ（watches.json）だけを使う。
        # 定期ジョブと同じく、道具はモードを問わず登録する
        self.watch_board = WatchBoard(data_dir)
        # スレッドモードでは用事の一覧を無効にする: 道具に用事系を登録せず、
        # 進行中の用事もプロンプトへ注入しない。長期記憶の検索道具も
        # 用事系と同じ条件（相棒モードのときだけ）で出す。
        board = None if self.mode == "wall" else self.task_board
        # 手順書（スキル）の置き場はデータ置き場ルート（全テナント
        # 共通）なので cfg.data_dir を渡す。道具を出す条件は用事系と
        # 同じ（相棒モードのときだけ）
        self.tools = ToolBox(
            Path.cwd(), task_board=board,
            memory=None if self.mode == "wall" else self.memory,
            data_dir=None if self.mode == "wall" else cfg.data_dir,
            job_board=self.job_board,
            chat_model=model,
            output_board=self.output_board,
            work_steps=self.work_steps,
            watch_board=self.watch_board)
        # MCP サーバーの道具を組み込み道具と並べて登録する。接続は
        # 実効の設定の置き場単位で共有する（テナントの置き場に
        # mcp.json があればその置き場、無ければデータ置き場ルート。
        # Web 経路でも部屋ごとにサーバープロセスを複製しない）
        self.mcp_manager = mcp_manager
        if self.mcp_manager is None and not cfg.is_cli_provider:
            self.mcp_manager = get_shared_manager(
                resolve_config_dir(cfg.data_dir, self.tenant),
                cfg.mcp_timeout_seconds)
        self._register_mcp_tools()
        # 承認の配線: テナント設定で承認が有効なら、ToolBox の承認
        # フックに _check_approval を差し込む。無効ならフック自体を
        # 差し込まない（承認者は呼ばれず、従来どおり実行される）
        self.approvals_enabled = approvals_enabled_for(cfg.data_dir, self.tenant)
        if self.approvals_enabled:
            self.tools.approval_check = self._check_approval
        # 委任（delegate）の道具は、委任で作られた子には登録
        # しない（委任は1段まで。それ以上は子の仕事の範囲を
        # 際限なく広げてしまうため）
        if not delegate_child:
            self._register_delegate_tool()
        # 会話記録の横断検索は、用事や長期記憶の仕組みとは独立
        # （対象は会話記録そのもの）なため、モードを問わず登録する
        self._register_history_search_tool()
        self.history: list[Message] = self._restore_history()
        memory_text = self.memory.load_long_term()
        self.system_prompt = load_persona(data_dir)
        if memory_text:
            # 記憶が閾値以下なら全文を、超えたら新しい方の項目だけを
            # 注入する（項目の途中で切らない。memory_inject_text）
            inject_text, partial = memory_inject_text(
                memory_text, cfg.memory_full_inject_chars)
            self.system_prompt += f"\n\n# 長期記憶\n{inject_text}"
            if partial:
                # スレッドモードでは検索道具自体を出さないため、
                # 案内文は道具のことに触れない形にする
                if self.mode == "wall":
                    note = "（長期記憶は一部のみ表示しています）"
                else:
                    note = ("（長期記憶は一部のみ表示しています。"
                            "残りは search_memory 道具で検索できます）")
                self.system_prompt += f"\n{note}"
        if self.mode != "wall":
            tasks_text = self._open_tasks_text()
            if tasks_text:
                self.system_prompt += f"\n\n# 進行中の用事\n{tasks_text}"
            skills_text = self._skills_catalog_text()
            if skills_text:
                self.system_prompt += f"\n\n# 使える手順書\n{skills_text}"
        steps_text = self._work_steps_text()
        if steps_text:
            self.system_prompt += f"\n\n# 作業の手順（前回の続き）\n{steps_text}"
        if delegate_child:
            # 子には「委ねられた仕事の担当である」ことを伝え、
            # 最後の応答が依頼元への報告になるようにする
            self.system_prompt += f"\n\n# 委任された作業\n{DELEGATE_NOTICE}"

    def _skills_catalog_text(self) -> str:
        """使える手順書の目次をシステムプロンプト用に組み立てる。

        載せるのは名前と説明だけで、本文は載せない（必要な手順書
        だけ read_skill で読む方が文脈を節約できる）。手順書が
        1 件も無い・走査に失敗した場合は空文字列（節自体を足さない）。
        手順書はデータ置き場ルートの skills/ にあり、全テナント共通。
        """
        try:
            found = load_skills(self.cfg.data_dir)
        except Exception:
            return ""  # 走査の失敗で対話を落とさない
        if not found:
            return ""
        lines = ["関係する仕事のときは、read_skill 道具で本文を"
                 "読んでから、その手順に従うこと。"]
        for skill in found:
            line = f"- {skill.name}"
            if skill.description:
                line += f": {skill.description}"
            lines.append(line)
        return "\n".join(lines)

    def _register_mcp_tools(self) -> None:
        """MCP サーバーの道具を組み込み道具と並べて登録する。

        CLI 系の頭脳は道具ループを自前で持つため、組み込み道具と
        同じく MCP 道具も出さない（マネージャ自体を持たない）。
        サーバーに接続できない場合は、その道具が出ないだけにして
        対話自体は落とさない。道具名は <サーバー名>__<道具名> の
        前置名で、説明文と入力スキーマはサーバーのものをそのまま
        使う（mcp_client.McpManager.tool_schemas が組み立てる）。
        """
        manager = self.mcp_manager
        if manager is None:
            return
        try:
            schemas = manager.tool_schemas()
        except Exception:
            return  # 発見の失敗で対話を落とさない
        for schema in schemas:
            prefixed = schema["name"]

            def call_mcp(_name: str = prefixed, **args: object) -> str:
                return manager.call(_name, dict(args))

            self.tools.register(Tool(
                prefixed, schema["description"], schema["parameters"],
                call_mcp))

    def _register_delegate_tool(self) -> None:
        """委任（delegate）の道具を登録する。

        実体は delegate.run_delegated_task で、新しい Session を
        子として作って仕事を渡し、子の最終応答を結果として返す。
        子には道具一式（ファイル・用事・記憶・MCP・手順書など親と
        同じ構成）を使わせるが、delegate 自体は登録しない
        （__init__ で delegate_child の場合はここを通らない）。
        """
        self.tools.register(Tool(
            "delegate",
            "別のエージェントに仕事を委ねる。独立して進められる仕事を、"
            "前提や期待する成果物まで含めて自己完結した文章で task に"
            "書いて渡すこと。委ねた先のエージェントは同じ道具（ファイル・"
            "用事・記憶・手順書など）を使え、作業が終わると最終報告が"
            "この道具の結果として返る。",
            {"type": "object", "properties": {
                "task": {"type": "string",
                         "description": "委ねる仕事の内容（自己完結した文章）"}},
             "required": ["task"]},
            self._run_delegate))

    def _run_delegate(self, task: str) -> str:
        return run_delegated_task(self, task)

    def _register_history_search_tool(self) -> None:
        """会話記録の横断検索（search_conversations）の道具を登録する。

        対象はこのユーザーの置き場にある全セッションの会話記録で、
        長期記憶（MEMORY.md）の検索とは別物。用事や長期記憶の
        道具と違いモードによる出し分けはしない（スレッドモード
        でも過去の会話は探せる方が自然なため）。
        """
        self.tools.register(Tool(
            "search_conversations",
            "過去の会話記録をキーワードで検索する。全セッション"
            "（部屋）を横断して発話単位で探し、セッション名・日時・"
            "話者つきで返す。利用者が「前に話した」「以前決めた」"
            "ことなど、会話そのものの内容を思い出す必要があるときに"
            "使う。長期記憶に書かれた事実を探す search_memory とは"
            "別物で、こちらは実際の発言を探す。",
            {"type": "object", "properties": {
                "query": {"type": "string",
                          "description": "検索キーワード。空白や句読点で区切って複数指定できる"},
                "limit": {"type": "integer",
                          "description": "返す最大件数（既定 5）"}},
             "required": ["query"]},
            self._search_conversations))

    def _search_conversations(self, query: str, limit: int = 5) -> str:
        hits = self.memory.search_conversations(query, limit=limit)
        if not hits:
            return "見つかりませんでした"
        return "\n".join(format_conversation_hit(hit) for hit in hits)

    def _open_tasks_text(self) -> str:
        """進行中の用事をシステムプロンプト用の箇条書きにする。

        相棒モードでは進行中の用事をすべて出す。用事がない
        場合は空文字列（節自体を足さない）。
        """
        lines = []
        for task in self.task_board.open_tasks():
            line = f"- {task.title}"
            if task.memo:
                line += f": {task.memo}"
            if task.remind_at:
                line += f"（{format_remind_at(task.remind_at)} に通知）"
            lines.append(line)
        return "\n".join(lines)

    def _work_steps_text(self) -> str:
        """前回までの作業の手順をシステムプロンプト用に組み立てる。

        終わっていない手順が残っている場合だけ節を出す（全部
        終わった手順は過去の記録なので出さない）。続きの作業
        なら引き継ぎ、別の用事なら置き換えるよう案内を添える。
        """
        if not self.work_steps.has_open():
            return ""
        return (
            "前回の作業で残した、自分の作業手順です。続きの作業なら"
            "この手順を引き継ぎ、別の用事なら set_work_steps 道具で"
            "新しい手順に置き換えてください。\n"
            + format_steps(self.work_steps.items()))

    def _restore_history(self) -> list[Message]:
        """このセッションの過去の発話を記録から履歴へ復元する。

        添付のバイト列は復元しない（記録に残る要約文の形で文脈に戻る）。
        記録は追記のみなので、要約レコードが現れたら「それ以前の発話は
        この要約に圧縮済み」とみなし、復元済みの発話を要約発話で
        置き換える（メモリ上の圧縮と同じ状態を再現する）。
        """
        history: list[Message] = []
        for index, record in enumerate(self.memory.load_turns()):
            role = record.get("role")
            content = str(record.get("content", ""))
            if role == "summary":
                history = [Message("user", f"{SUMMARY_PREFIX}\n{content}",
                                   record_index=index)]
            elif role in ("user", "assistant"):
                quote = record.get("quote")
                history.append(Message(
                    role, content,
                    quote=quote if isinstance(quote, str) and quote else None,
                    record_index=index,
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
        # 音声添付は、頭脳へ渡す前に必要なら文字起こししておく
        # （履歴と記録の両方に文章が流れるよう、この合流点で行う）
        self._transcribe_audio_attachments(atts)
        user_msg = Message(
            "user", user_text, attachments=atts, quote=quote_text)
        self.history.append(user_msg)
        user_msg.record_index = self.memory.append_turn(
            "user", _message_text(user_msg), quote=quote_text)
        try:
            for _ in range(self.cfg.max_tool_rounds):
                # モデルへの問い合わせ（CLI 系バックエンドもこの経路）
                # 現在日時は発話のたびに新しいものを添える（リマインダーの
                # 絶対時刻への変換に使う）
                self._notify(THINKING)
                prompt = (f"{self.system_prompt}\n\n# 現在日時\n"
                          f"{current_time_text()}")
                reply = self._call_model(prompt, self.history, self.tools.schemas())
                self.history.append(reply)
                if not reply.tool_calls:
                    reply.record_index = self.memory.append_turn(
                        "assistant", reply.content)
                    self._maybe_compact()
                    return reply.content
                for call in reply.tool_calls:
                    args = call.get("args", {})
                    self._notify(activity_label(call["name"], args))
                    result = self.tools.call(call["name"], args)
                    self.history.append(Message("tool", result, tool_call_id=call.get("id"), tool_name=call["name"]))
            return "（道具の呼び出しが上限に達しました）"
        finally:
            self._notify("")  # 応答が終わったら作業中表示を消す

    def _transcribe_audio_attachments(self, atts: list[Attachment]) -> None:
        """音声添付を、頭脳へ渡す前に Gemini で文字起こしする。

        今の頭脳が Gemini（音声データをそのまま聞ける）のときは
        従来どおりデータを渡すため文字起こしはしない。文字起こし
        が無効なとき（KUMA_TRANSCRIBE=0）・Gemini のキーが無い
        ときも何もしない。失敗した場合はその添付だけ従来の扱い
        （注記・ファイルのみ）に戻し、会話は壊さない。結果は
        Attachment.transcript に載り、頭脳への渡し方（API 系の
        注記・CLI 系の指示）と会話ログの要約の両方に流れる。
        """
        targets = [a for a in atts if a.is_audio and not a.transcript]
        if not targets:
            return
        if self.cfg.provider == "gemini" or isinstance(self.model, GeminiClient):
            return  # Gemini は音声データ自体を聞ける
        if not transcribe.available():
            return
        self._notify(TRANSCRIBING)
        for att in targets:
            try:
                att.transcript = transcribe.transcribe_audio(att)
            except Exception:
                att.transcript = None  # 失敗は従来の扱いに戻す

    def post_proactive_message(self, content: str) -> None:
        """能動発信の発言を履歴と記録に足す（モデルの応答とは別の経路）。

        エージェントから先に送る発言（proactive）専用で、道具の循環は
        回さない。記録には能動発信の印（proactive: true）を付けるが、
        復元時は通常の assistant 発言として戻る。
        """
        msg = Message("assistant", content)
        self.history.append(msg)
        msg.record_index = self.memory.append_turn(
            "assistant", content, proactive=True)

    def _notify(self, status: str) -> None:
        """作業中表示のコールバックへ現在の作業を通知する。

        表示側の失敗で対話自体を落とさないよう、コールバックの例外は
        ここで握りつぶす。
        """
        callback = self.on_status
        if callback is None:
            return
        try:
            callback(status)
        except Exception:
            pass

    def _check_approval(self, name: str, args: dict) -> bool:
        """承認対象の道具を実行してよいか、UI 側の承認者に聞く。

        承認待ちであることは作業中表示にも出す。承認者が付いて
        いない経路（テストや組み込み利用）では安全側に倒して常に
        却下する。承認者の例外も却下として扱う。承認されたら表示
        をその道具の作業名に戻す（実行中の表示が承認待ちのままに
        ならないよう）。
        """
        self._notify(approval_waiting_text(name))
        approver = self.approver
        if approver is None:
            self._notify(APPROVAL_DENIED)
            return False
        try:
            approved = bool(approver(name, dict(args)))
        except Exception:
            approved = False
        if approved:
            self._notify(activity_label(name, args))
        else:
            self._notify(APPROVAL_DENIED)
        return approved

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
        self._notify(SUMMARIZING)
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
        reply = self._call_model(_SUMMARY_SYSTEM, [prompt], [])
        return reply.content.strip()

    def _with_reaction_notes(self, messages: list[Message]) -> list[Message]:
        """モデルへ渡す直前に、リアクションの注記を付けた写しを返す。

        利用者が付けたリアクションは、会話記録（JSONL）にも履歴の
        発話そのものにも混ぜず、モデルへの入力を作るこの一点でのみ
        「（この発話に利用者が❤️を付けた）」の注記として添える。
        履歴表示（/api/history は履歴の発話を読む）には出ない。
        リアクションの付いていない発話はそのまま（写しを作らない）。
        """
        reactions = self.memory.load_reactions()
        if not reactions:
            return messages
        annotated: list[Message] = []
        for msg in messages:
            emojis = (reactions.get(msg.record_index)
                      if msg.record_index is not None else None)
            if emojis and msg.role in ("user", "assistant"):
                note = ("（この発話に利用者が"
                        + "と".join(emojis) + "を付けた）")
                annotated.append(
                    replace(msg, content=msg.content + "\n" + note))
            else:
                annotated.append(msg)
        return annotated

    def _call_model(self, system_prompt: str, messages: list[Message],
                    tools: list[dict]) -> Message:
        """モデルを 1 回呼び、使用量を記録してから応答を返す。

        使用量の記録（usage.record_usage）はこのユーザーへの帰属で、
        委任の子セッションも自分の Session からここを通るため、子の
        利用も同じユーザーに記録される（session_id で区別できる）。
        記録は応答のたび（道具の往復で複数回呼ぶ場合は呼び出し
        ごと）に行い、失敗しても会話には影響しない。
        """
        reply = self.model.chat(
            system_prompt, self._with_reaction_notes(messages), tools)
        record_usage(self.cfg.user_data_dir, self.cfg.provider,
                     _model_name(self.model),
                     getattr(reply, "usage", None), self.session_id)
        return reply
