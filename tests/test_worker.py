from kuma_agent.config import Config
from kuma_agent.worker import pick_issue


def _issue(number, labels):
    return {"number": number, "title": f"task {number}",
            "labels": [{"name": l} for l in labels]}


def test_pick_issue_skips_locked_and_blocked():
    cfg = Config(repo="o/r")
    issues = [_issue(1, ["agent:kuma"]), _issue(2, ["blocked"]), _issue(3, [])]
    assert pick_issue(issues, cfg)["number"] == 3


def test_pick_issue_none_when_all_busy():
    cfg = Config(repo="o/r")
    assert pick_issue([_issue(1, ["agent:other"])], cfg) is None
