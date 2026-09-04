from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel


class TxType(str, Enum):
    TRADE    = "trade"
    DEPOSIT  = "deposit"
    WITHDRAW = "withdraw"
    FEE      = "fee"
    REWARD   = "reward"
    TRANSFER = "transfer"


class CanonicalTx(BaseModel):
    id: str
    source: str
    timestamp: datetime
    type: TxType
    received_asset: str | None = None
    received_amount: Decimal | None = None
    sent_asset: str | None = None
    sent_amount: Decimal | None = None
    fee_asset: str | None = None
    fee_amount: Decimal | None = None
    label: str | None = None
    tx_hash: str | None = None
    raw: dict[str, Any] = {}

    @staticmethod
    def make_id(source: str, exchange_key: str) -> str:
        h = hashlib.sha256(f"{source}:{exchange_key}".encode()).hexdigest()
        return h[:16]
