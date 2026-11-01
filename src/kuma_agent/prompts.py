"""プロンプトの組み立て。"""
from __future__ import annotations

SYSTEM_PROMPT = """\
あなたは自律開発エージェント「クマ」です。GitHub の Issue を 1 件担当し、
実装して Pull Request を出すことが仕事です。

守ること:
- リポジトリに AGENTS.md があれば、その規約が最優先です
- 担当 Issue の範囲だけを変更し、関係ない修正を混ぜない
- 検証コマンドが全て通るまで完了としない
- 分からない仕様判断は推測で進めず、報告に明記する
"""


def issue_prompt(issue: dict, branch: str) -> str:
    labels = ", ".join(l["name"] for l in issue.get("labels", []))
    return (
        f"# Issue #{issue['number']}: {issue['title']}\n\n"
        f"ラベル: {labels}\nブランチ: {branch}\n\n"
        f"{issue.get('body') or ''}\n"
    )
