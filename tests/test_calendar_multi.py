"""Google カレンダー連携の複数カレンダー対応（0.78.0）。

確認すること:
- (a) カレンダー一覧の取得と整形（主カレンダーが先頭・名前順、
  ページ送り。CLI の一覧表示は名前と印だけで、実 ID を出さない）
- (b) 道具の calendar_id 指定（そのカレンダーの予定だけを取る。
  一覧に無い ID は見つからない扱い、権限なし・スコープ不足は
  再認可の案内として分かる）
- (c) 対象カレンダーの選択の保存と読み戻し（CLI の select で
  番号から選ぶ。既定は主カレンダーだけ）
- (d) 朝のまとめで、選択した複数カレンダーの予定が時刻順に
  1 つの節へ統合され、強い重なりの寄せが統合後も効く。
  一部のカレンダーが失敗しても取れた分で節を作る
- (e) 選択が無い場合は主カレンダーだけが対象（0.77.0 と互換）

Google 側は httpx.MockTransport で差し替え、実ネットワーク
には出ない。時刻の解釈は tz を固定（+09:00）して検証する。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

from kuma_agent import cli
from kuma_agent.activity import classify_tool_error
from kuma_agent.config import Config
from kuma_agent.google_calendar import (
    CALENDAR_NOT_FOUND_RESULT,
    FETCH_FAILED_RESULT,
    INSUFFICIENT_SCOPE_RESULT,
    CalendarSettings,
    ClientSecrets,
    Credentials,
    GoogleCalendarClient,
    fetch_calendar_list,
    list_events_text,
    load_calendar_settings,
    save_calendar_settings,
    save_credentials,
    selected_calendar_ids,
)
from kuma_agent.proactive import calendar_briefing_materials
from kuma_agent.tasks import Task

JST = timezone(timedelta(hours=9))
SECRETS = ClientSecrets(client_id="client-id-123",
                        client_secret="client-secret-xyz")
NOW = datetime(2026, 11, 7, 8, 0, tzinfo=JST)  # 2026-11-07 は土曜日

PRIMARY_ID = "taro@example.com"
PRIMARY_NAME = "山田太郎"
WORK_ID = "work-cal@group.calendar.google.com"
WORK_NAME = "仕事用"
FAMILY_ID = "family@group.calendar.google.com"
FAMILY_NAME = "家族共有"

BOTH_SCOPES = (
    "https://www.googleapis.com/auth/calendar.events.readonly "
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly")

_INSUFFICIENT_BODY = {
    "error": {
        "code": 403,
        "message": "Insufficient Authentication Scopes.",
        "errors": [{"domain": "global",
                    "reason": "insufficientPermissions",
                    "message": "Insufficient Permission"}]}}


def _creds() -> Credentials:
    return Credentials(
        refresh_token="refresh-token-1",
        scope=BOTH_SCOPES,
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


def _calendars_page() -> dict:
    """一覧 3 件（主カレンダーは並びの途中に入れておく）。"""
    return {"items": [
        {"id": WORK_ID, "summary": WORK_NAME, "accessRole": "owner"},
        {"id": PRIMARY_ID, "summary": PRIMARY_NAME,
         "primary": True, "accessRole": "owner"},
        {"id": FAMILY_ID, "summary": FAMILY_NAME,
         "accessRole": "reader"},
    ]}


class _MultiStub:
    """Google 側のモック（トークン＋カレンダー一覧＋複数予定）。"""

    def __init__(self) -> None:
        self.calendar_pages: list[dict] = [_calendars_page()]
        self.events: dict[str, list[dict]] = {}
        self.list_status = 200
        self.list_body: dict | None = None
        self.events_status: dict[str, int] = {}
        self.events_body: dict[str, dict] = {}
        self.requests: list[httpx.Request] = []
        self.token_status = 200
        self.token_body: dict = {"access_token": "access-1",
                                 "expires_in": 3600}

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/token":
            return httpx.Response(self.token_status,
                                  json=self.token_body)
        self.requests.append(request)
        if path == "/calendar/v3/users/me/calendarList":
            if self.list_body is not None:
                return httpx.Response(self.list_status,
                                      json=self.list_body)
            if self.list_status != 200:
                return httpx.Response(self.list_status, json={})
            index = sum(1 for r in self.requests
                        if r.url.path.endswith("calendarList")
                        and "pageToken" in r.url.params)
            page = self.calendar_pages[
                min(index, len(self.calendar_pages) - 1)]
            return httpx.Response(200, json=page)
        prefix = "/calendar/v3/calendars/"
        if path.startswith(prefix) and path.endswith("/events"):
            cal_id = unquote(path[len(prefix):-len("/events")])
            status = self.events_status.get(cal_id, 200)
            if cal_id in self.events_body:
                return httpx.Response(status,
                                      json=self.events_body[cal_id])
            if status != 200:
                return httpx.Response(status, json={})
            return httpx.Response(
                200, json={"items": self.events.get(cal_id, [])})
        raise AssertionError(f"想定外の宛先: {path}")

    def requested_calendars(self) -> list[str]:
        """予定を取りに行ったカレンダー（別名・実 ID のまま）。"""
        prefix = "/calendar/v3/calendars/"
        found = []
        for request in self.requests:
            path = request.url.path
            if path.startswith(prefix) and path.endswith("/events"):
                found.append(unquote(path[len(prefix):-len("/events")]))
        return found


def _client(tmp_path: Path, stub: _MultiStub) -> GoogleCalendarClient:
    save_credentials(tmp_path, _creds())
    return GoogleCalendarClient(tmp_path, SECRETS,
                                transport=stub.transport())


def _stock_events(stub: _MultiStub) -> None:
    """3 カレンダー分の今日の予定（時刻が交錯する並び）。"""
    stub.events["primary"] = [
        _timed("朝会", "2026-11-07T09:00:00+09:00",
               "2026-11-07T09:30:00+09:00"),
    ]
    stub.events[WORK_ID] = [
        _timed("始業チェック", "2026-11-07T08:30:00+09:00",
               "2026-11-07T08:45:00+09:00"),
        _timed("定例会議", "2026-11-07T13:00:00+09:00",
               "2026-11-07T14:00:00+09:00", location="会議室A"),
    ]
    stub.events[FAMILY_ID] = [
        _all_day("家族の記念日", "2026-11-07", "2026-11-08"),
    ]


def _select(tmp_path: Path, ids: list[str]) -> None:
    save_calendar_settings(tmp_path, CalendarSettings(calendar_ids=ids))


# -------------------------------------------------- (a) 一覧の取得

def test_calendar_list_order_and_fields(tmp_path: Path) -> None:
    stub = _MultiStub()
    # 2 ページに分けて返し、ページ送りが追われることも見る
    page = _calendars_page()
    stub.calendar_pages = [
        {"items": page["items"][:2], "nextPageToken": "next-1"},
        {"items": page["items"][2:]},
    ]
    entries = fetch_calendar_list(_client(tmp_path, stub))
    # 主カレンダーが先頭、残りは名前順（仕事用 → 家族共有）
    assert [e.id for e in entries] == [PRIMARY_ID, WORK_ID, FAMILY_ID]
    assert entries[0].primary is True
    assert entries[0].summary == PRIMARY_NAME
    assert entries[1].access_role == "owner"
    assert entries[2].access_role == "reader"


def test_cli_calendars_shows_names_not_ids(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    cfg = Config(data_dir=tmp_path)
    client = _client(cfg.user_data_dir, _MultiStub())
    assert cli._run_google_calendar_calendars(cfg, client) == 0
    out = capsys.readouterr().out
    assert PRIMARY_NAME in out and WORK_NAME in out
    assert FAMILY_NAME in out
    assert "主カレンダー" in out
    # 既定（選択なし）では主カレンダーだけが「対象」
    assert f"{PRIMARY_NAME}（対象・主カレンダー）" in out
    # 実 ID は表示しない（主カレンダーの ID はメールアドレス状）
    assert PRIMARY_ID not in out
    assert WORK_ID not in out
    assert "refresh-token-1" not in out


def test_cli_calendars_insufficient_scope_guides_reauth(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    cfg = Config(data_dir=tmp_path)
    stub = _MultiStub()
    stub.list_status = 403
    stub.list_body = _INSUFFICIENT_BODY
    client = _client(cfg.user_data_dir, stub)
    assert cli._run_google_calendar_calendars(cfg, client) == 1
    out = capsys.readouterr().out
    assert "許可が足りません" in out
    assert "auth" in out  # 再認可の案内


# --------------------------------------------- (b) calendar_id 指定

def test_tool_specific_calendar(tmp_path: Path) -> None:
    stub = _MultiStub()
    _stock_events(stub)
    client = _client(tmp_path, stub)
    result = list_events_text(client, calendar_id=WORK_ID,
                              now=NOW, tz=JST)
    assert "予定は2件です" in result
    assert "始業チェック" in result and "定例会議" in result
    assert "朝会" not in result
    # 単一カレンダーでは由来の名前は付けない（0.77.0 と同じ整形）
    assert f"（{WORK_NAME}）" not in result
    assert stub.requested_calendars() == [WORK_ID]


def test_tool_unknown_calendar_id(tmp_path: Path) -> None:
    stub = _MultiStub()
    _stock_events(stub)
    client = _client(tmp_path, stub)
    result = list_events_text(
        client, calendar_id="no-such@group.calendar.google.com",
        now=NOW, tz=JST)
    assert result == CALENDAR_NOT_FOUND_RESULT
    # 一覧に無いカレンダーの予定は取りに行かない
    assert stub.requested_calendars() == []


def test_tool_specific_calendar_list_scope_missing(
        tmp_path: Path) -> None:
    stub = _MultiStub()
    stub.list_status = 403
    stub.list_body = _INSUFFICIENT_BODY
    client = _client(tmp_path, stub)
    result = list_events_text(client, calendar_id=WORK_ID,
                              now=NOW, tz=JST)
    # スコープ不足は、再認可の案内として分かる結果になる
    assert result == INSUFFICIENT_SCOPE_RESULT
    assert classify_tool_error(result) == "insufficient_scope"


def test_tool_specific_calendar_events_scope_missing(
        tmp_path: Path) -> None:
    stub = _MultiStub()
    _stock_events(stub)
    stub.events_status[WORK_ID] = 403
    stub.events_body[WORK_ID] = _INSUFFICIENT_BODY
    client = _client(tmp_path, stub)
    result = list_events_text(client, calendar_id=WORK_ID,
                              now=NOW, tz=JST)
    assert result == INSUFFICIENT_SCOPE_RESULT


def test_tool_specific_calendar_events_forbidden(
        tmp_path: Path) -> None:
    stub = _MultiStub()
    _stock_events(stub)
    stub.events_status[WORK_ID] = 403  # 理由の無い 403
    client = _client(tmp_path, stub)
    result = list_events_text(client, calendar_id=WORK_ID,
                              now=NOW, tz=JST)
    assert result == FETCH_FAILED_RESULT


# --------------------------------------------------- (c) 選択の保存

def test_calendar_settings_roundtrip(tmp_path: Path) -> None:
    # 選択が無ければ主カレンダーだけ（0.77.0 と互換）
    assert selected_calendar_ids(tmp_path) == ["primary"]
    _select(tmp_path, [WORK_ID, "primary", WORK_ID])
    loaded = load_calendar_settings(tmp_path)
    # 重複は落として、並びは保つ
    assert loaded.calendar_ids == [WORK_ID, "primary"]
    assert selected_calendar_ids(tmp_path) == [WORK_ID, "primary"]
    # 壊れたファイルは「選択なし」として読む（連携は止めない）
    (tmp_path / "google-calendar" / "settings.json").write_text(
        "{壊れた json", encoding="utf-8")
    assert load_calendar_settings(tmp_path).calendar_ids == []
    assert selected_calendar_ids(tmp_path) == ["primary"]


def test_cli_select_saves_selection(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    cfg = Config(data_dir=tmp_path)
    stub = _MultiStub()
    client = _client(cfg.user_data_dir, stub)
    # 一覧の並びは 1: 主カレンダー、2: 仕事用、3: 家族共有
    assert cli._run_google_calendar_select(
        cfg, client, ["2", "1"]) == 0
    out = capsys.readouterr().out
    assert WORK_NAME in out and PRIMARY_NAME in out
    # 主カレンダーは実 ID ではなく別名 primary で保存される
    assert selected_calendar_ids(cfg.user_data_dir) == [
        WORK_ID, "primary"]
    saved_text = (cfg.user_data_dir / "google-calendar"
                  / "settings.json").read_text(encoding="utf-8")
    assert PRIMARY_ID not in saved_text
    # 番号なしは現在の選択の表示だけ
    assert cli._run_google_calendar_select(cfg, client, []) == 0
    out = capsys.readouterr().out
    assert WORK_NAME in out
    # 範囲外の番号は保存せず、エラーにする
    assert cli._run_google_calendar_select(
        cfg, client, ["9"]) == 1
    assert selected_calendar_ids(cfg.user_data_dir) == [
        WORK_ID, "primary"]


def test_cli_calendars_marks_saved_selection(
        tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    cfg = Config(data_dir=tmp_path)
    _select(cfg.user_data_dir, [WORK_ID])
    client = _client(cfg.user_data_dir, _MultiStub())
    assert cli._run_google_calendar_calendars(cfg, client) == 0
    out = capsys.readouterr().out
    assert f"{WORK_NAME}（対象）" in out
    assert f"{PRIMARY_NAME}（主カレンダー）" in out


# ------------------------------------------- (d) 朝のまとめでの統合

def test_briefing_merges_selected_calendars(tmp_path: Path) -> None:
    cfg = Config(data_dir=tmp_path)
    _select(cfg.user_data_dir, ["primary", WORK_ID, FAMILY_ID])
    stub = _MultiStub()
    _stock_events(stub)
    client = _client(cfg.user_data_dir, stub)
    text, notes, empty = calendar_briefing_materials(
        cfg, [], NOW, client=client, tz=JST)
    assert empty is False and notes == {}
    assert text is not None
    # 3 カレンダー分が 1 つの節に統合される（件数は合計）
    assert "予定は4件です" in text
    # 時刻順（終日が先、次に時刻の早い順）に並ぶ
    assert text.index("家族の記念日") < text.index("始業チェック")
    assert text.index("始業チェック") < text.index("朝会")
    assert text.index("朝会") < text.index("定例会議")
    # 複数カレンダーの統合では、由来の名前が各行に付く
    assert f"（{WORK_NAME}）" in text
    assert f"（{PRIMARY_NAME}）" in text
    assert f"（{FAMILY_NAME}）" in text


def test_briefing_fold_after_merge(tmp_path: Path) -> None:
    cfg = Config(data_dir=tmp_path)
    _select(cfg.user_data_dir, ["primary", WORK_ID])
    stub = _MultiStub()
    _stock_events(stub)
    client = _client(cfg.user_data_dir, stub)
    tasks = [Task(id="1", title="定例会議 資料作成")]
    text, notes, empty = calendar_briefing_materials(
        cfg, tasks, NOW, client=client, tz=JST)
    # 仕事用カレンダーの予定でも、強い重なりは用事側へ寄る
    assert notes == {"1": "（予定 13:00–14:00）"}
    assert text is not None
    assert "定例会議" not in text
    assert "朝会" in text and "始業チェック" in text


def test_briefing_partial_failure_uses_available(
        tmp_path: Path) -> None:
    cfg = Config(data_dir=tmp_path)
    _select(cfg.user_data_dir, ["primary", WORK_ID])
    stub = _MultiStub()
    _stock_events(stub)
    stub.events_status[WORK_ID] = 500
    client = _client(cfg.user_data_dir, stub)
    text, notes, empty = calendar_briefing_materials(
        cfg, [], NOW, client=client, tz=JST)
    # 取れた分（主カレンダー）だけで節を作る
    assert text is not None and "朝会" in text
    assert empty is False
    # 全部失敗なら、従来どおり材料なしで静かに動く
    stub.events_status["primary"] = 500
    assert calendar_briefing_materials(
        cfg, [], NOW, client=client, tz=JST) == (None, {}, False)


# --------------------------------------- (e) 選択なしは主だけ・互換

def test_default_is_primary_only(tmp_path: Path) -> None:
    cfg = Config(data_dir=tmp_path)
    stub = _MultiStub()
    _stock_events(stub)
    client = _client(cfg.user_data_dir, stub)
    # 道具（calendar_id 省略）は主カレンダーだけ
    result = list_events_text(client, now=NOW, tz=JST)
    assert "予定は1件です" in result and "朝会" in result
    assert stub.requested_calendars() == ["primary"]
    # 朝のまとめも主カレンダーだけ
    stub.requests.clear()
    text, notes, empty = calendar_briefing_materials(
        cfg, [], NOW, client=client, tz=JST)
    assert text is not None and "朝会" in text
    assert "始業チェック" not in text
    assert stub.requested_calendars() == ["primary"]


def test_tool_uses_selection_and_all(tmp_path: Path) -> None:
    stub = _MultiStub()
    _stock_events(stub)
    _select(tmp_path, ["primary", WORK_ID])
    client = _client(tmp_path, stub)
    # 省略時は選択したカレンダーを統合する
    result = list_events_text(client, now=NOW, tz=JST)
    assert "予定は3件です" in result
    assert result.index("始業チェック") < result.index("朝会")
    assert result.index("朝会") < result.index("定例会議")
    assert f"（{WORK_NAME}）" in result
    # all も同じ（選択した全カレンダー）
    assert list_events_text(client, calendar_id="all",
                            now=NOW, tz=JST) == result
    # primary を明示すれば主カレンダーだけ
    only = list_events_text(client, calendar_id="primary",
                            now=NOW, tz=JST)
    assert "予定は1件です" in only and "朝会" in only
