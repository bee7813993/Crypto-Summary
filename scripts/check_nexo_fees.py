"""
Nexo Pro SpotHistory のフィー・残高を診断する。

1. feeCurrency が NEXO 以外の取引を列挙
2. filledAmount=0 だが fee がある行（現在スキップされている）を確認
3. DnW と Spot を合算した資産別残高をCSVから直接計算
"""
from __future__ import annotations

import csv
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path


def _d(v: str) -> Decimal:
    v = v.strip()
    if not v:
        return Decimal(0)
    try:
        return Decimal(v)
    except InvalidOperation:
        return Decimal(0)


def main(spot_path: Path, dnw_path: Path) -> None:
    print("=" * 70)
    print("【1】feeCurrency が NEXO 以外の取引")
    print("=" * 70)
    non_nexo_fees: list[dict] = []

    with open(spot_path, encoding="utf-8-sig", newline="") as f:
        spot_rows = list(csv.DictReader(f))

    for row in spot_rows:
        fc = row.get("feeCurrency", "").strip().upper()
        fa = _d(row.get("tradingFee", ""))
        filled = _d(row.get("filledAmount", ""))
        if fc and fc != "NEXO":
            non_nexo_fees.append(row)
            pair = row["pair"]
            side = row["side"]
            ep = _d(row.get("executedPrice", ""))
            quote_amt = filled * ep if ep else Decimal(0)
            print(f"  id={row['id']}  {row['timestamp'][:10]}")
            print(f"    {pair} {side.upper()}  filled={filled}  execPrice={ep}")
            print(f"    → recv/sent QUOTE ≈ {quote_amt:.6f}")
            print(f"    fee: {fa} {fc}")
            print()

    print(f"  合計 {len(non_nexo_fees)} 件\n")

    print("=" * 70)
    print("【2】filledAmount=0 かつ fee>0 の行（現在スキップ → fee未計上）")
    print("=" * 70)
    skipped_fees: list[dict] = []
    for row in spot_rows:
        filled = _d(row.get("filledAmount", ""))
        fa = _d(row.get("tradingFee", ""))
        fc = row.get("feeCurrency", "").strip().upper()
        if filled == 0 and fa > 0:
            skipped_fees.append(row)
            print(f"  id={row['id']}  {row['timestamp'][:10]}  {row['pair']} {row['side']}")
            print(f"    filledAmount=0  fee={fa} {fc}  type={row.get('type','')}")
            print()
    if not skipped_fees:
        print("  なし\n")
    else:
        print(f"  合計 {len(skipped_fees)} 件\n")

    print("=" * 70)
    print("【3】SpotHistory の資産別残高（アダプタなし・CSV直計算）")
    print("=" * 70)
    spot_bal: dict[str, Decimal] = {}
    skipped_rows = 0

    for row in spot_rows:
        filled = _d(row.get("filledAmount", ""))
        ep = _d(row.get("executedPrice", ""))
        fa = _d(row.get("tradingFee", ""))
        fc = row.get("feeCurrency", "").strip().upper() or None

        # filledAmount=0 はスキップ（fee があっても）
        if not filled:
            skipped_rows += 1
            continue

        pair = row["pair"].strip()
        if "/" not in pair:
            continue
        base, quote = pair.split("/")
        base, quote = base.upper(), quote.upper()
        side = row["side"].strip().lower()
        quote_amt = filled * ep if ep else Decimal(0)

        if side == "buy":
            spot_bal[base]  = spot_bal.get(base,  Decimal(0)) + filled
            spot_bal[quote] = spot_bal.get(quote, Decimal(0)) - quote_amt
        else:
            spot_bal[quote] = spot_bal.get(quote, Decimal(0)) + quote_amt
            spot_bal[base]  = spot_bal.get(base,  Decimal(0)) - filled

        if fc and fa:
            spot_bal[fc] = spot_bal.get(fc, Decimal(0)) - fa

    print(f"  (skipped {skipped_rows} rows with filledAmount=0)\n")
    for asset in sorted(spot_bal):
        v = spot_bal[asset]
        flag = " ← 負!" if v < -Decimal("0.00000001") else ""
        print(f"  {asset:10s} {v:+.8f}{flag}")

    print()
    print("=" * 70)
    print("【4】DnWHistory の資産別残高（CSV直計算）")
    print("=" * 70)
    dnw_bal: dict[str, Decimal] = {}

    with open(dnw_path, encoding="utf-8-sig", newline="") as f:
        dnw_rows = list(csv.DictReader(f))

    for row in dnw_rows:
        asset = row["asset"].strip().upper()
        amount = _d(row["amount"])
        side = row["side"].strip().upper()
        if side == "DEPOSIT":
            dnw_bal[asset] = dnw_bal.get(asset, Decimal(0)) + amount
        else:
            dnw_bal[asset] = dnw_bal.get(asset, Decimal(0)) - amount

    for asset in sorted(dnw_bal):
        v = dnw_bal[asset]
        flag = " ← 負!" if v < -Decimal("0.00000001") else ""
        print(f"  {asset:10s} {v:+.8f}{flag}")

    print()
    print("=" * 70)
    print("【5】Spot + DnW 合算残高（CSV直計算）")
    print("=" * 70)
    all_assets = set(spot_bal) | set(dnw_bal)
    for asset in sorted(all_assets):
        v = spot_bal.get(asset, Decimal(0)) + dnw_bal.get(asset, Decimal(0))
        flag = " ← 負!" if v < -Decimal("0.00000001") else ""
        print(f"  {asset:10s} {v:+.8f}{flag}")


if __name__ == "__main__":
    spot = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        "/root/.claude/uploads/734e20a1-9773-56c7-a35c-00d7cfb46959/d96dc694-SpotHistory178132178869219864713.csv"
    )
    dnw = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(
        "/root/.claude/uploads/734e20a1-9773-56c7-a35c-00d7cfb46959/00fec67a-DnWHistory178132177859619864713.csv"
    )
    main(spot, dnw)
