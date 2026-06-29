"""GMO CSV の精算区分ごとの日本円受渡金額を集計するスクリプト。

使い方:
    python scripts/check_gmo_csv.py <CSVファイルパス>
"""
import csv
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        print("使い方: python scripts/check_gmo_csv.py <CSVファイルパス>")
        sys.exit(1)

    path = Path(sys.argv[1])
    totals: dict[str, list] = defaultdict(lambda: [Decimal(0), 0])

    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = row.get("精算区分", "").strip() or "(空白)"
            raw = (row.get("日本円受渡金額") or "").strip().replace(",", "")
            try:
                amount = Decimal(raw) if raw else Decimal(0)
            except InvalidOperation:
                amount = Decimal(0)
            totals[key][0] += amount
            totals[key][1] += 1

    grand_total = sum(v[0] for v in totals.values())
    grand_count = sum(v[1] for v in totals.values())

    print(f"\n{'精算区分':<28} {'件数':>5}  {'日本円受渡金額 合計':>18}")
    print("-" * 58)
    for key, (amount, count) in sorted(totals.items()):
        print(f"{key:<28} {count:>5}件  {amount:>18,.0f} JPY")
    print("-" * 58)
    print(f"{'合計':<28} {grand_count:>5}件  {grand_total:>18,.0f} JPY")


if __name__ == "__main__":
    main()
