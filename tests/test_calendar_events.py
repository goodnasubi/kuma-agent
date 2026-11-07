"""Google カレンダー連携の道具と朝のまとめへの組み込み（0.77.0）。

確認すること:
- (a) 道具 list_calendar_events が予定を整形して返す（終日／
  時刻つきの区別、繰り返し予定の展開、複数日・またぎの表示、
  件数と範囲の上限、日付の検証）
- (b) 道具の登録条件は設計 §3.1 どおり（無効化されておらず
  クライアント設定が存在すれば、認可の有無によらず道具が出る。
  未設定・無効では出ない。未認可で呼ぶと案内文が返る。両モード
  と委任の子で有効）
- (c) 失敗と失効が例外ではなく結果文字列になる（未設定・
  未認可・失効・取得失敗の区別、資格情報の値が漏れない）
- (d) 朝のまとめの材料に今日の予定が入り、強く重なる予定は
  用事の行へ寄る。節の並びと子の指示が設計どおりになる
- (e) 未設定・失敗のときは材料に入らず、日報にも混ざらない
- 活動の記録には予定の題名・場所・実カレンダー ID が残らない

Google 側は httpx.MockTransport で差し替え、実ネットワーク
には出ない。時刻の解釈は tz を固定（+09:00）して検証する。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from kuma_agent import proactive
from kuma_agent.activity import classify_tool_error, load_activity_records
from kuma_agent.config import Config
from kuma_agent.google_calendar import (
    FETCH_FAILED_RESULT,
    NOT_AUTHORIZED_RESULT,
    NOT_CONFIGURED_RESULT,
    REVOKED_RESULT,
    ClientSecrets,
    Credentials,
    GoogleCalendarClient,
    list_events_text,
    save_credentials,
    summarize_calendar_result,
)
from kuma_agent.llm import Message
from kuma_agent.proactive import (
    build_briefing_prompt,
    calendar_briefing_materials,
    check_and_post,
)
from kuma_agent.session import Session
from kuma_agent.tasks import Task
from kuma_agent.tools import activity_label, requires_approval
from kuma_agent.tools import summarize_tool_args

JST = timezone(timedelta(hours=9))
SECRETS = ClientSecrets(client_id="client-id-123",
                        client_secret="client-secret-xyz")
NOW = datetime(2026, 11, 7, 8, 0, tzinfo=JST)  # 2026-11-07 は土曜日


def _creds() -> Credentials:
    return Credentials(
        refresh_token="refresh-token-1",
        scope="https://www.googleapis.com/auth/calendar.events.readonly",
        authorized_at="2026-11-07T10:00:00+09:00",
        client_id="client-id-123")


def _timed(summary: str, start: str, end: str,
           location: str = "") -> dict:
    item: dict = {
        "summary": summary, "status": "confirmed",
        "start": {"dateTime": start}, "end": {"dateTime": end}}
    if location:
        item["location"] = location
    return item


def _all_day(summary: str, start: str, end: str) -> dict:
    return {"summary": summary, "status": "confirmed",
            "start": {"date": start}, "end": {"date": end}}


class _EventsStub:
    """Google 側のモック（トークン＋予定一覧）。

    pages にページごとの応答（items と nextPageToken）を用意
    すると、pageToken の有無で順に返す。
    """

    def __init__(self, pages: list[dict] | None = None) -> None:
        self.pages = pages if pages is not None else [{"items": []}]
        self.requests: list[httpx.Request] = []
        self.token_status = 200
        self.token_body: dict = {"access_token": "access-1",
                                 "expires_in": 3600}
        self.events_status = 200
        self.raise_network = False

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.raise_network:
            raise httpx.ConnectError("接続できません", request=request)
        if request.url.path == "/token":
            return httpx.Response(self.token_status,
                                  json=self.token_body)
        if request.url.path == "/calendar/v3/calendars/primary/events":
            self.requests.append(request)
            if self.events_status != 200:
                return httpx.Response(self.events_status, json={})
            index = sum(1 for r in self.requests
                        if "pageToken" in r.url.params)
            page = self.pages[min(index, len(self.pages) - 1)]
            return httpx.Response(200, json=page)
        raise AssertionError(f"想定外の宛先: {request.url.path}")

    def params(self, index: int = 0) -> dict:
        return dict(self.requests[index].url.params)


def _client(tmp_path: Path, stub: _EventsStub) -> GoogleCalendarClient:
    save_credentials(tmp_path, _creds())
    return GoogleCalendarClient(tmp_path, SECRETS,
                                transport=stub.transport())


def _today_items() -> list[dict]:
    """2026-11-07 の予定 3 件（終日・時刻つき・場所なし）。"""
    return [
        _timed("定例会議", "2026-11-07T09:00:00+09:00",
               "2026-11-07T10:30:00+09:00", location="会議室A"),
        _all_day("振替休日", "2026-11-07", "2026-11-08"),
        _timed("通院", "2026-11-07T13:00:00+09:00",
               "2026-11-07T14:00:00+09:00"),
    ]


# ------------------------------------------------ (a) 道具の整形

def test_tool_formats_todays_events(tmp_path: Path) -> None:
    stub = _EventsStub([{"items": _today_items()}])
    client = _client(tmp_path, stub)
    result = list_events_text(client, now=NOW, tz=JST)
    assert result == (
        "2026-11-07（土）から2026-11-07（土）までの予定は3件です。\n"
        "\n"
        "・終日: 振替休日\n"
        "・09:00–10:30: 定例会議（場所: 会議室A）\n"
        "・13:00–14:00: 通院")
    # 問い合わせの形: 繰り返しを展開し、開始順、削除済みは除き、
    # 取る項目は題名・場所・開始・終了・状態だけに絞る
    params = stub.params()
    assert params["singleEvents"] == "true"
    assert params["orderBy"] == "startTime"
    assert params["showDeleted"] == "false"
    assert params["timeMin"] == "2026-11-07T00:00:00+09:00"
    assert params["timeMax"] == "2026-11-08T00:00:00+09:00"
    assert params["fields"] == (
        "items(summary,location,start,end,status),nextPageToken")


def test_tool_recurring_instances_and_headings(tmp_path: Path) -> None:
    items = [
        _timed("週次定例", "2026-11-07T10:00:00+09:00",
               "2026-11-07T11:00:00+09:00"),
        # 繰り返しのうち取り消された回は出ない
        {**_timed("週次定例", "2026-11-14T10:00:00+09:00",
                  "2026-11-14T11:00:00+09:00"),
         "status": "cancelled"},
        _timed("週次定例", "2026-11-14T10:00:00+09:00",
               "2026-11-14T11:00:00+09:00"),
    ]
    stub = _EventsStub([{"items": items}])
    client = _client(tmp_path, stub)
    result = list_events_text(
        client, start_date="2026-11-07", end_date="2026-11-14",
        now=NOW, tz=JST)
    assert "予定は2件です" in result
    assert "2026-11-07（土）:" in result
    assert "2026-11-14（土）:" in result
    assert result.count("週次定例") == 2


def test_tool_multiday_allday_and_crossday(tmp_path: Path) -> None:
    items = [
        _all_day("合宿", "2026-11-07", "2026-11-10"),
        _timed("夜行バス", "2026-11-08T23:00:00+09:00",
               "2026-11-09T01:00:00+09:00"),
        {"status": "confirmed",
         "start": {"dateTime": "2026-11-09T09:00:00+09:00"},
         "end": {"dateTime": "2026-11-09T10:00:00+09:00"}},
    ]
    stub = _EventsStub([{"items": items}])
    client = _client(tmp_path, stub)
    result = list_events_text(
        client, start_date="2026-11-07", end_date="2026-11-09",
        now=NOW, tz=JST)
    # 終日予定の終了日は排他的なので、最終日は前日までを示す
    assert "・終日（11月7日〜11月9日）: 合宿" in result
    # 日をまたぐ時刻つき予定は、終了も日付つきで示す
    assert "・11月8日 23:00–11月9日 01:00: 夜行バス" in result
    # 題名が無い予定は「（無題の予定）」
    assert "・09:00–10:00: （無題の予定）" in result


def test_tool_paging_and_overflow_note(tmp_path: Path) -> None:
    pages = [
        {"items": _today_items()[:2], "nextPageToken": "t2"},
        {"items": _today_items()[2:]},
    ]
    stub = _EventsStub(pages)
    client = _client(tmp_path, stub)
    result = list_events_text(client, max_results=2, now=NOW, tz=JST)
    assert "予定は2件です" in result
    assert "（ほか 1 件あります）" in result
    assert len(stub.requests) == 2
    assert stub.params(1)["pageToken"] == "t2"


def test_tool_page_cap_note(tmp_path: Path) -> None:
    page = {"items": _today_items()[:1], "nextPageToken": "next"}
    stub = _EventsStub([page])
    client = _client(tmp_path, stub)
    result = list_events_text(client, now=NOW, tz=JST)
    assert len(stub.requests) == 3  # ページ送りは最大 3 ページまで
    assert "（ほかにも予定があります）" in result


def test_tool_no_events(tmp_path: Path) -> None:
    stub = _EventsStub([{"items": []}])
    client = _client(tmp_path, stub)
    assert list_events_text(client, now=NOW, tz=JST) \
        == "予定はありませんでした"


def test_tool_validation_errors(tmp_path: Path) -> None:
    stub = _EventsStub([{"items": []}])
    client = _client(tmp_path, stub)

    def call(**kwargs: object) -> str:
        return list_events_text(client, now=NOW, tz=JST, **kwargs)

    assert "日付の指定が正しくありません" in call(
        start_date="2026/11/07")
    assert "日付の指定が正しくありません" in call(
        start_date="2026-02-30")
    assert "終了日が開始日より前" in call(
        start_date="2026-11-08", end_date="2026-11-07")
    assert "期間は最大31日まで" in call(
        start_date="2026-11-01", end_date="2026-12-02")
    # 31 日ちょうどは通る
    assert call(start_date="2026-11-01", end_date="2026-12-01") \
        == "予定はありませんでした"
    assert "件数は 1 以上" in call(max_results=0)
    assert "件数の指定が正しくありません" in call(max_results="abc")
    assert "主カレンダー（primary）だけが対象" in call(
        calendar_id="work")
    # 上限を超える件数は 50 に抑えられ、エラーにはならない
    assert call(max_results=100) == "予定はありませんでした"


def test_tool_max_results_limits_shown(tmp_path: Path) -> None:
    stub = _EventsStub([{"items": _today_items()}])
    client = _client(tmp_path, stub)
    result = list_events_text(client, max_results=1, now=NOW, tz=JST)
    assert "予定は1件です" in result
    assert "（ほか 2 件あります）" in result


# ------------------------------------------------ (b) 道具の登録条件

class _EchoModel:
    model = "stub"

    def chat(self, system_prompt, messages, tools):
        return Message("assistant", "分かりました")


def _tool_names(session: Session) -> set[str]:
    return {s["name"] for s in session.tools.schemas()}


def _configure_env(monkeypatch: pytest.MonkeyPatch,
                   tmp_path: Path) -> Path:
    secrets_file = tmp_path / "client_secret.json"
    secrets_file.write_text(json.dumps({"installed": {
        "client_id": "client-id-123",
        "client_secret": "client-secret-xyz"}}), encoding="utf-8")
    monkeypatch.setenv(
        "KUMA_GOOGLE_CALENDAR_CLIENT_SECRETS_FILE", str(secrets_file))
    monkeypatch.delenv("KUMA_GOOGLE_CALENDAR", raising=False)
    return secrets_file


def test_tool_hidden_when_not_configured(tmp_path: Path) -> None:
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    assert "list_calendar_events" not in _tool_names(session)


def test_tool_shown_when_not_authorized(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 認可の有無は登録条件にしない（設計 §3.1）。未認可でも道具は
    # 出て、呼び出し結果で認可の手順を案内する
    _configure_env(monkeypatch, tmp_path)
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    assert "list_calendar_events" in _tool_names(session)
    assert session._list_calendar_events() == NOT_AUTHORIZED_RESULT
    wall = Config(data_dir=tmp_path, mode="wall")
    assert "list_calendar_events" in _tool_names(
        Session(wall, _EchoModel()))
    child = Session(cfg, _EchoModel(), delegate_child=True)
    assert "list_calendar_events" in _tool_names(child)


def test_tool_guidance_via_say_when_not_authorized(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 会話経由で呼んでも、例外ではなく認可の案内文がモデルへ返る
    _configure_env(monkeypatch, tmp_path)
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _ToolCallModel())
    assert session.say("今日の予定は？") == "分かりました"
    records = load_activity_records(cfg.user_data_dir)
    calls = [r for r in records if r.get("kind") == "tool_call"
             and r.get("tool") == "list_calendar_events"]
    assert len(calls) == 1
    assert calls[0]["result"] == NOT_AUTHORIZED_RESULT
    assert calls[0].get("error_kind") == "not_authorized"


def test_tool_shown_when_credentials_broken(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 資格情報の破損（invalid）でも道具は出て、呼び出し結果は
    # 再認可の案内になる
    _configure_env(monkeypatch, tmp_path)
    cfg = Config(data_dir=tmp_path)
    path = save_credentials(cfg.user_data_dir, _creds())
    path.write_text("壊れています", encoding="utf-8")
    session = Session(cfg, _EchoModel())
    assert "list_calendar_events" in _tool_names(session)
    assert session._list_calendar_events() == REVOKED_RESULT


def test_tool_shown_when_authorized_both_modes(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch, tmp_path)
    cfg = Config(data_dir=tmp_path)
    save_credentials(cfg.user_data_dir, _creds())
    assert "list_calendar_events" in _tool_names(
        Session(cfg, _EchoModel()))
    wall = Config(data_dir=tmp_path, mode="wall")
    assert "list_calendar_events" in _tool_names(
        Session(wall, _EchoModel()))
    # 委任の子（同じ利用者の読み取り）にも登録される
    child = Session(cfg, _EchoModel(), delegate_child=True)
    assert "list_calendar_events" in _tool_names(child)


def test_tool_hidden_when_disabled(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("KUMA_GOOGLE_CALENDAR", "0")
    cfg = Config(data_dir=tmp_path)
    save_credentials(cfg.user_data_dir, _creds())
    session = Session(cfg, _EchoModel())
    assert "list_calendar_events" not in _tool_names(session)


def test_tool_is_readonly_without_approval() -> None:
    assert requires_approval("list_calendar_events") is False
    assert activity_label("list_calendar_events") == "予定を確認中"


# ------------------------------------------------ (c) 失敗の結果文字列

def _exploding_client(tmp_path: Path) -> GoogleCalendarClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("ネットワークへ出てはいけない")
    return GoogleCalendarClient(
        tmp_path, SECRETS, transport=httpx.MockTransport(handler))


def test_not_configured_and_not_authorized_results(
        tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("ネットワークへ出てはいけない")
    transport = httpx.MockTransport(handler)
    client = GoogleCalendarClient(tmp_path, None, transport=transport)
    assert list_events_text(client, now=NOW, tz=JST) \
        == NOT_CONFIGURED_RESULT
    client = GoogleCalendarClient(tmp_path, SECRETS,
                                  transport=transport)  # 資格情報が無い
    result = list_events_text(client, now=NOW, tz=JST)
    assert result == NOT_AUTHORIZED_RESULT


def test_revoked_result(tmp_path: Path) -> None:
    stub = _EventsStub()
    stub.token_status = 400
    stub.token_body = {"error": "invalid_grant",
                       "error_description": "SECRET-MARKER"}
    client = _client(tmp_path, stub)
    result = list_events_text(client, now=NOW, tz=JST)
    assert result == REVOKED_RESULT
    assert "SECRET-MARKER" not in result
    assert "refresh-token-1" not in result


def test_fetch_failure_results(tmp_path: Path) -> None:
    stub = _EventsStub()
    stub.raise_network = True
    client = _client(tmp_path, stub)
    assert list_events_text(client, now=NOW, tz=JST) \
        == FETCH_FAILED_RESULT
    stub = _EventsStub()
    stub.events_status = 500
    client = _client(tmp_path, stub)
    assert list_events_text(client, now=NOW, tz=JST) \
        == FETCH_FAILED_RESULT


def test_error_kind_classification() -> None:
    assert classify_tool_error(NOT_CONFIGURED_RESULT) \
        == "not_configured"
    assert classify_tool_error(NOT_AUTHORIZED_RESULT) \
        == "not_authorized"
    assert classify_tool_error(REVOKED_RESULT) == "revoked"
    assert classify_tool_error(FETCH_FAILED_RESULT) == "error"


# ------------------------------------------------ 活動の記録の要約

def test_activity_summaries_hide_event_details() -> None:
    summary = summarize_tool_args("list_calendar_events", {
        "start_date": "2026-11-07", "end_date": "2026-11-08",
        "calendar_id": "primary"})
    assert "2026-11-07" in summary and "primary" in summary
    # 実カレンダー ID（メールアドレスを含み得る）は記録しない
    summary = summarize_tool_args("list_calendar_events", {
        "start_date": "2026-11-07",
        "calendar_id": "someone@example.com"})
    assert "example.com" not in summary

    text = ("2026-11-07（土）から2026-11-07（土）までの"
            "予定は3件です。\n\n・09:00–10:30: 定例会議")
    assert summarize_calendar_result(text) == "3件の予定を取得"
    assert summarize_calendar_result("予定はありませんでした") \
        == "0件の予定を取得"
    assert summarize_calendar_result(REVOKED_RESULT) == REVOKED_RESULT


class _ToolCallModel:
    """1 回目に list_calendar_events を呼び、2 回目に締める頭脳。"""

    model = "stub"

    def __init__(self) -> None:
        self.calls = 0

    def chat(self, system_prompt, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return Message("assistant", "", tool_calls=[{
                "id": "c1", "name": "list_calendar_events",
                "args": {"start_date": "2026-11-07",
                         "end_date": "2026-11-07"}}])
        return Message("assistant", "分かりました")


def test_activity_record_via_say_hides_titles(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_env(monkeypatch, tmp_path)
    cfg = Config(data_dir=tmp_path)
    save_credentials(cfg.user_data_dir, _creds())
    session = Session(cfg, _ToolCallModel())
    # 構築済みのクライアントを、モック transport のものへ差し替える
    stub = _EventsStub([{"items": _today_items()}])
    session.google_calendar_client = GoogleCalendarClient(
        cfg.user_data_dir, SECRETS, transport=stub.transport())
    assert session.say("今日の予定は？") == "分かりました"

    records = load_activity_records(cfg.user_data_dir)
    calls = [r for r in records if r.get("kind") == "tool_call"
             and r.get("tool") == "list_calendar_events"]
    assert len(calls) == 1
    assert calls[0]["result"] == "3件の予定を取得"
    raw = (cfg.user_data_dir / "activity.jsonl").read_text(
        encoding="utf-8")
    assert "定例会議" not in raw
    assert "会議室A" not in raw
    assert "refresh-token-1" not in raw


# ------------------------------------------------ (d) 朝のまとめの材料

def _briefing_client(tmp_path: Path, items: list[dict]) \
        -> GoogleCalendarClient:
    return _client(tmp_path, _EventsStub([{"items": items}]))


def test_briefing_materials_with_strong_overlap_fold(
        tmp_path: Path) -> None:
    cfg = Config(data_dir=tmp_path)
    items = [
        _timed("定例会議", "2026-11-07T10:00:00+09:00",
               "2026-11-07T11:00:00+09:00"),
        _timed("歯医者", "2026-11-07T15:00:00+09:00",
               "2026-11-07T16:00:00+09:00"),
    ]
    client = _briefing_client(tmp_path, items)
    tasks = [Task(id="1", title="定例会議 資料作成")]
    text, notes, empty = calendar_briefing_materials(
        cfg, tasks, NOW, client=client, tz=JST)
    assert empty is False
    # 強く重なる予定は節から外れ、用事の行への注記に寄る
    assert text is not None
    assert "歯医者" in text
    assert "定例会議" not in text
    assert notes == {"1": "（予定 10:00–11:00）"}


def test_briefing_materials_allday_fold(tmp_path: Path) -> None:
    cfg = Config(data_dir=tmp_path)
    client = _briefing_client(
        tmp_path, [_all_day("合同合宿", "2026-11-07", "2026-11-08")])
    tasks = [Task(id="1", title="合同合宿 準備")]
    text, notes, empty = calendar_briefing_materials(
        cfg, tasks, NOW, client=client, tz=JST)
    assert text is None  # 予定は全部用事側へ寄った
    assert notes == {"1": "（終日予定あり）"}
    assert empty is False


def test_briefing_materials_weak_overlap_not_folded(
        tmp_path: Path) -> None:
    cfg = Config(data_dir=tmp_path)
    client = _briefing_client(tmp_path, [
        _timed("会議", "2026-11-07T10:00:00+09:00",
               "2026-11-07T11:00:00+09:00")])
    tasks = [Task(id="1", title="会議 資料")]
    text, notes, empty = calendar_briefing_materials(
        cfg, tasks, NOW, client=client, tz=JST)
    # 弱い重なり（短い語 1 つだけ）は寄せず、予定として残す
    assert text is not None and "会議" in text
    assert notes == {}
    assert empty is False


def test_briefing_prompt_order_and_instructions() -> None:
    tasks = [Task(id="1", title="定例会議 資料作成"),
             Task(id="2", title="買い物")]
    prompt = build_briefing_prompt(
        tasks, NOW,
        calendar_text="2026-11-07（土）から2026-11-07（土）までの"
                      "予定は1件です。\n\n・15:00–16:00: 歯医者",
        task_notes={"1": "（予定 10:00–11:00）"})
    # 材料の並び: 予定の節が用事より先に来る
    assert prompt.index("今日の予定（Google カレンダー）") \
        < prompt.index("進行中の用事:")
    # 寄せた注記は用事の行の末尾に付く
    assert "・定例会議 資料作成（予定 10:00–11:00）" in prompt
    # 子は同じ範囲を取り直さない・同じ内容は 1 回だけ、の指示
    assert "取り直さない" in prompt
    assert "1 回だけ触れてください" in prompt


def test_briefing_prompt_empty_calendar_context() -> None:
    prompt = build_briefing_prompt([], NOW, calendar_empty=True)
    assert "Google カレンダーに今日の予定はありません。" in prompt


# --------------------------------------- (e) 未設定・失敗時の静かな動作

def test_briefing_materials_quiet_when_not_ready(
        tmp_path: Path) -> None:
    cfg = Config(data_dir=tmp_path)
    # 未認可（資格情報なし）
    client = _exploding_client(tmp_path)
    assert calendar_briefing_materials(
        cfg, [], NOW, client=client, tz=JST) == (None, {}, False)
    # 取得できたが 0 件
    client = _briefing_client(tmp_path, [])
    assert calendar_briefing_materials(
        cfg, [], NOW, client=client, tz=JST) == (None, {}, True)
    # 通信失敗
    stub = _EventsStub()
    stub.raise_network = True
    client = _client(tmp_path, stub)
    assert calendar_briefing_materials(
        cfg, [], NOW, client=client, tz=JST) == (None, {}, False)
    # 失効
    stub = _EventsStub()
    stub.token_status = 400
    stub.token_body = {"error": "invalid_grant"}
    client = _client(tmp_path, stub)
    assert calendar_briefing_materials(
        cfg, [], NOW, client=client, tz=JST) == (None, {}, False)


class _SpyExecutor:
    """朝のまとめの子の差し替え（test_briefing と同じ形）。"""

    def __init__(self, result: str = "おはようございます。") -> None:
        self.result = result
        self.prompts: list[str] = []

    def __call__(self, parent, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.result


def _write_tasks(user_dir: Path) -> None:
    stamp = "2026-11-03T08:00:00"  # 用事の時刻はローカル（naive）
    task = {"id": "1", "title": "買い物", "status": "open", "memo": "",
            "created_at": stamp, "updated_at": stamp}
    (user_dir / "tasks.json").write_text(
        json.dumps([task], ensure_ascii=False), encoding="utf-8")


def _stub_client_factory(tmp_path: Path, items: list[dict]):
    stub = _EventsStub([{"items": items}])
    client = GoogleCalendarClient(tmp_path, SECRETS,
                                  transport=stub.transport())
    return lambda *args, **kwargs: client


def test_check_and_post_includes_calendar_section(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_tasks(tmp_path)
    save_credentials(tmp_path, _creds())
    items = [_timed("会議", "2026-11-03T10:00:00+09:00",
                    "2026-11-03T11:00:00+09:00")]
    monkeypatch.setattr(
        proactive, "build_google_calendar_client",
        _stub_client_factory(tmp_path, items))
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()
    after = datetime(2026, 11, 3, 8, 0, 0)

    text = check_and_post(cfg, now=after,
                          session_provider=lambda: session,
                          briefing_executor=spy)
    assert text == "おはようございます。"
    assert len(spy.prompts) == 1
    assert "今日の予定（Google カレンダー）" in spy.prompts[0]
    assert "会議" in spy.prompts[0]
    assert "買い物" in spy.prompts[0]


def test_check_and_post_digest_fallback_excludes_calendar(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_tasks(tmp_path)
    save_credentials(tmp_path, _creds())
    items = [_timed("会議", "2026-11-03T10:00:00+09:00",
                    "2026-11-03T11:00:00+09:00")]
    monkeypatch.setattr(
        proactive, "build_google_calendar_client",
        _stub_client_factory(tmp_path, items))
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor(result="error: 子が失敗しました")
    after = datetime(2026, 11, 3, 8, 0, 0)

    text = check_and_post(cfg, now=after,
                          session_provider=lambda: session,
                          briefing_executor=spy)
    # 日報への倒し込みには予定を混ぜない
    assert text is not None
    assert "買い物" in text
    assert "会議" not in text


def test_check_and_post_quiet_when_unconfigured_or_failing(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_tasks(tmp_path)
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    after = datetime(2026, 11, 3, 8, 0, 0)

    # 未設定: 予定の節は材料に入らない（従来どおり用事だけ）。
    # 節の見出し（末尾の「:」つき）で判定する（子の指示文には
    # 節の名前自体は常に出るため）
    spy = _SpyExecutor()
    assert check_and_post(cfg, now=after,
                          session_provider=lambda: session,
                          briefing_executor=spy) is not None
    assert "今日の予定（Google カレンダー）:" not in spy.prompts[0]

    # 連携の取得が失敗しても、まとめ自体は従来どおり成立する
    save_credentials(tmp_path, _creds())
    stub = _EventsStub()
    stub.raise_network = True
    failing = GoogleCalendarClient(tmp_path, SECRETS,
                                   transport=stub.transport())
    monkeypatch.setattr(
        proactive, "build_google_calendar_client",
        lambda *args, **kwargs: failing)
    spy = _SpyExecutor()
    assert check_and_post(
        cfg, now=after + timedelta(days=1),
        session_provider=lambda: session,
        briefing_executor=spy) is not None
    assert len(spy.prompts) == 1
    assert "今日の予定（Google カレンダー）:" not in spy.prompts[0]
    assert "買い物" in spy.prompts[0]


def test_check_and_post_calendar_only_material_runs_child(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    save_credentials(tmp_path, _creds())
    items = [_timed("会議", "2026-11-03T10:00:00+09:00",
                    "2026-11-03T11:00:00+09:00")]
    monkeypatch.setattr(
        proactive, "build_google_calendar_client",
        _stub_client_factory(tmp_path, items))
    cfg = Config(data_dir=tmp_path)
    session = Session(cfg, _EchoModel())
    spy = _SpyExecutor()
    after = datetime(2026, 11, 3, 8, 0, 0)

    # 用事が無くても、今日の予定だけで子が動いてまとめが投稿される
    text = check_and_post(cfg, now=after,
                          session_provider=lambda: session,
                          briefing_executor=spy)
    assert text == "おはようございます。"
    assert len(spy.prompts) == 1
    assert "会議" in spy.prompts[0]
