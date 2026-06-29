from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..core.models import CanonicalTx


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
    def __init__(self, source_id: str):
        self.source_id = source_id

    @abstractmethod
    def load(self, path: Path) -> list[CanonicalTx]:
        ...
