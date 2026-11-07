"""委任（delegate 道具）: 仕事を別のエージェント（子 Session）へ渡す。

親の道具ループから呼ばれ、新しい Session を子として作って、依頼
文を 1 発話として渡し、子の最終応答を道具の結果として親へ返す
（同期実行）。子の会話は親と混ぜず、delegate- で始まる別セッ
ションとして記録に残すため、後から何を頼んで何が起きたか辿れる。

制限時間を過ぎたら、親は子へ協調キャンセルの印（threading.Event）
を立てて時間切れを返す。子は次の確認点で会話を打ち切って終わる
ため、時間切れの後も子が裏で動き続けることはない（実行中の道具
やモデル呼び出しそのものは止められない）。

Session への import は循環参照を避けるため関数内（遅延）で行う。
"""
from __future__ import annotations

import itertools
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING

from .llm import create_client

if TYPE_CHECKING:
    from .session import Session

# 子のシステムプロンプトに足す一文。子の最終応答がそのまま親への
# 報告になることを伝える
DELEGATE_NOTICE = (
    "あなたは別のエージェントから仕事を委ねられた担当エージェントです。"
    "渡された仕事を完了し、最後の応答で結果を簡潔に報告してください"
    "（その応答がそのまま依頼元への報告になります）。"
)

# 子セッション ID の連番（同一プロセス・同一秒の委任を一意にする）
_counter = itertools.count(1)

# 実行中の子のスレッドの台帳。時間切れ後に協調キャンセルで子が
# 止まったことを、テストや運用の確認で確かめられるようにする
# （子のスレッドは自分の終了時に台帳から自分を外す）
_workers_lock = threading.Lock()
_workers: set[threading.Thread] = set()


def active_child_threads() -> list[threading.Thread]:
    """今動いている委任の子のスレッドを返す（終了確認用）。"""
    with _workers_lock:
        return [t for t in _workers if t.is_alive()]


def wait_child_threads(timeout: float) -> bool:
    """子のスレッドが全部終わるまで待ち、終われば True を返す。

    時間内に終わらなければ False。主にテストと運用確認のための
    小さな仕組みで、通常の委任の流れからは呼ばない。
    """
    deadline = time.monotonic() + timeout
    while True:
        alive = active_child_threads()
        if not alive:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        for t in alive:
            t.join(min(0.05, remaining))


def _new_child_session_id() -> str:
    """子のセッション ID（delegate-<日時>-<連番>）を作る。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"delegate-{stamp}-{next(_counter)}"


def _child_model(parent: Session):
    """子の頭脳を用意する。

    モデルの指定（KUMA_DELEGATE_MODEL / Config.delegate_model）が
    あれば、親と同じプロバイダでそのモデルのクライアントを作る。
    指定が無ければ親のモデルをそのまま使う（親の道具呼び出しの
    中で同期的に待つため、親と同時に呼ばれることはない）。
    """
    cfg = parent.cfg
    if cfg.delegate_model:
        return create_client(cfg.provider, cfg.resolve_api_key(),
                             model=cfg.delegate_model)
    return parent.model


def run_delegated_task(parent: Session, task: str) -> str:
    """仕事を子エージェントへ委ね、子の最終応答を返す。

    承認は親の承認者を子にも配線し、親と同じ仕組みが子の道具でも
    働くようにする。子の実行は別スレッドで行い、制限時間
    （Config.delegate_timeout_seconds）を過ぎたら時間切れを失敗
    として返す（スレッドはデーモンで、親の会話はそのまま続く）。
    時間切れのときは子へ協調キャンセルの印を立てるため、子は
    次の確認点（道具ループの各周回・モデル呼び出しの前・道具
    呼び出しの前）で会話を打ち切って終わる。実行中の道具や
    モデル呼び出しそのものは止められない（それがこの仕組みの
    限界）。打ち切られた子の応答は、親へ既に時間切れが返って
    いるため破棄され、子の会話記録にだけ打ち切りの文言が残る。
    子の側で何が起きても例外は外へ漏らさず、error の結果文字列に
    して親へ返す。
    """
    from .session import Session  # 循環参照を避けるための遅延 import

    cfg = parent.cfg

    def relay(status: str) -> None:
        # 子の作業中表示を、親の表示へ中継する（誰の作業か分かる
        # ように前置きを付ける。表示を消す通知は中継しない: 親の
        # 道具ループが後続の表示を出すため）
        if status:
            parent._notify(f"別のエージェントが作業中（{status}）")

    outcome: dict[str, str] = {}
    # 協調キャンセルの印。この委任専用に作り、子へだけ渡す
    cancel_event = threading.Event()

    def work() -> None:
        try:
            child = Session(
                cfg, _child_model(parent),
                session_id=_new_child_session_id(),
                on_status=relay,
                mcp_manager=parent.mcp_manager,
                approver=parent.approver,
                delegate_child=True,
                cancel_event=cancel_event)
            outcome["result"] = child.say(task)
        except Exception as e:  # 子の失敗で親の会話を落とさない
            outcome["error"] = f"error: 委任先のエージェントで失敗しました: {e}"
        finally:
            with _workers_lock:
                _workers.discard(worker)

    worker = threading.Thread(target=work, daemon=True)
    with _workers_lock:
        _workers.add(worker)
    worker.start()
    worker.join(cfg.delegate_timeout_seconds)
    if worker.is_alive():
        # 時間切れ: 親は先へ進むが、子には協調キャンセルの印を
        # 立て、次の確認点で打ち切って終わらせる（子の応答は
        # 破棄され、結果として親へは届かない）
        cancel_event.set()
        return (f"error: 委任先のエージェントが時間内に終わりませんでした"
                f"（制限 {cfg.delegate_timeout_seconds} 秒）")
    if "error" in outcome:
        return outcome["error"]
    return outcome.get("result", "error: 委任先のエージェントから応答がありませんでした")
