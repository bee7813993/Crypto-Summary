from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..core.models import CanonicalTx


class CsvSourceAdapter(ABC):
    def __init__(self, source_id: str):
        self.source_id = source_id

    @abstractmethod
    def load(self, path: Path) -> list[CanonicalTx]:
        ...
