from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..core.models import CanonicalTx


def require_columns(
    fieldnames: list[str] | None,
    required: tuple[str, ...],
    format_hint: str,
) -> None:
    """CSV ヘッダーに必要な列が揃っているか検証する。

    別形式のファイル（他サービスのエクスポートや集計CSVなど）を選んで
    しまったとき、KeyError('列名') という手掛かりのないエラーではなく、
    どの列が無いか・実際のヘッダーが何か・どのファイルを選ぶべきかが
    ひと目で分かるメッセージで落とすためのもの。
    """
    fields = [f.strip() for f in (fieldnames or [])]
    missing = [c for c in required if c not in fields]
    if not missing:
        return
    found = "、".join(fields[:12]) + ("…" if len(fields) > 12 else "")
    raise ValueError(
        f"{format_hint}ではないようです。"
        f"必要な列が見つかりません: {'、'.join(missing)}"
        f" / このファイルの列: {found if found else '(ヘッダーなし)'}"
    )


def read_csv_text(path: Path, encodings: tuple[str, ...] = ("utf-8-sig", "cp932")) -> str:
    """CSV をエンコーディング自動判定で読み込む。

    UTF-8（BOM 付き含む）と Shift_JIS(cp932) の両方に対応する。
    ``encodings`` を先頭から順に試し、最初に成功したものを採用する。
    SJIS のバイト列は UTF-8 として解釈すると大半が UnicodeDecodeError に
    なるため、utf-8-sig → cp932 の順で安全に判定できる。
    """
    raw = path.read_bytes()
    last_err: UnicodeDecodeError | None = None
    for enc in encodings:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError as e:
            last_err = e
    # すべて失敗した場合は最後の cp932 を置換付きで読む（取り込み継続を優先）
    if last_err is not None:
        return raw.decode(encodings[-1], errors="replace")
    return raw.decode(encodings[-1])


class CsvSourceAdapter(ABC):
    """CSV アダプタの基底クラス。

    スキップ件数の計上（任意）:
      アダプタが「読めたが記録対象外」として落とした行を ``_skip(reason)`` で
      数えておくと、``load()`` 後に ``skipped`` / ``skip_reasons`` から取得できる。
      Web の取り込み結果に表示され、「入れたのに反映されない」の原因が見える。

      計上するのは *実データを持つ行を意図的に落とした場合のみ*。
      構造上ほぼ全行が対象外になるような形式（日次レポートの予定利息行など）は
      数えない。数えると意味のない巨大な件数になり、本当に見てほしい
      「未知の区分がある」というシグナルが埋もれるため。
    """

    def __init__(self, source_id: str):
        self.source_id = source_id
        self.skipped: int = 0
        self.skip_reasons: dict[str, int] = {}

    def _reset_skips(self) -> None:
        """load() の冒頭で呼ぶ。同一インスタンスの再利用でも件数が累積しない。"""
        self.skipped = 0
        self.skip_reasons = {}

    def _skip(self, reason: str) -> None:
        """記録対象外として落とした行を理由別に計上する。"""
        self.skipped += 1
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1

    @abstractmethod
    def load(self, path: Path) -> list[CanonicalTx]:
        ...
