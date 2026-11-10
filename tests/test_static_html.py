"""index.html の入れ子の検査。

閉じタグが 1 つずれると、ブラウザはそこでページの div ごと閉じてしまい、カードが
別のページや全ページに出る（エラーにはならないので気づきにくい）。
"""

from html.parser import HTMLParser
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "src" / "crypto_summary" / "web" / "static" / "index.html"

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "source", "track", "wbr"}

PAGES = ("page-dashboard", "page-accounts", "page-assets", "page-transactions",
         "page-import", "page-admin")


class _Nesting(HTMLParser):
    """開きタグと閉じタグの対応を追い、id を持つ要素の祖先（タグ名と id）を記録する。"""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, str | None, int]] = []
        self.problems: list[str] = []
        self.ancestors: dict[str, list[tuple[str, str | None]]] = {}

    def _record(self, attrs) -> None:
        el_id = dict(attrs).get("id")
        if el_id:
            self.ancestors[el_id] = [(tag, i) for tag, i, _ in self.stack]

    def handle_starttag(self, tag, attrs):
        self._record(attrs)
        if tag not in VOID:
            self.stack.append((tag, dict(attrs).get("id"), self.getpos()[0]))

    def handle_startendtag(self, tag, attrs):
        self._record(attrs)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if self.stack and self.stack[-1][0] == tag:
            self.stack.pop()
            return
        opened = self.stack[-1] if self.stack else None
        self.problems.append(f"line {self.getpos()[0]}: </{tag}> but the open element is {opened}")
        # 同名の最も近い要素まで閉じて続ける（ブラウザに近い扱い）
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break


def _parse() -> _Nesting:
    parser = _Nesting()
    parser.feed(INDEX.read_text(encoding="utf-8"))
    parser.close()
    return parser


def _ids(parser: _Nesting, el_id: str) -> list[str]:
    return [i for _, i in parser.ancestors[el_id] if i]


def test_every_tag_is_closed_in_order():
    parser = _parse()
    assert parser.problems == []
    assert parser.stack == []


def test_pages_sit_directly_in_main():
    parser = _parse()
    for page in PAGES:
        assert parser.ancestors[page][-1][0] == "main", page


def test_cards_sit_in_their_page():
    parser = _parse()
    # 取り込み系はインポート画面に
    for el_id in ("inbox-section", "pbr-sync-section", "import-accounts-table",
                  "import-batches-table"):
        assert "page-import" in _ids(parser, el_id), el_id
    # 利用者ごとの設定は設定ページに。管理者だけの囲みには入れない（全員に出す）
    for el_id in ("prefs-section", "mcp-section"):
        ids = _ids(parser, el_id)
        assert "page-admin" in ids and "admin-only-sections" not in ids, el_id
    # 全ユーザー共通の設定は、設定ページの管理者だけの囲みに
    assert "page-admin" in _ids(parser, "admin-only-sections")
    for el_id in ("admin-oauth-section", "admin-emails-section", "system-etherscan",
                  "admin-coingecko-input"):
        assert "admin-only-sections" in _ids(parser, el_id), el_id
