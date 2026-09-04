"""DB に取り込まれなかった GMO CSV 行を特定するスクリプト。

使い方:
    python scripts/check_gmo_missing.py <CSVファイルパス> [--source-id gmo]
"""
import csv
import hashlib
import sys
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path


def make_id(source_id: str, raw_key: str) -> str:
    digest = hashlib.sha256(f"{source_id}:{raw_key}".encode()).hexdigest()
    return digest[:16]


def _d(v: str):
    v = (v or "").strip().replace(",", "")
    try:
        return Decimal(v) if v else None
    except InvalidOperation:
        return None


def build_key(row: dict, source_id: str) -> str:
    raw_key = "|".join([
        row["日時"], row["注文ID"], row["銘柄名"],
        row["売買区分"], row["約定数量"], row["約定金額"],
    ])
    return make_id(source_id, raw_key)


def main():
    if len(sys.argv) < 2:
        print("使い方: python scripts/check_gmo_missing.py <CSV> [source_id]")
        sys.exit(1)

    path = Path(sys.argv[1])
    source_id = sys.argv[2] if len(sys.argv) > 2 else "gmo"

    # --- CSV 全行を読んでIDを生成 ---
    csv_rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("精算区分", "").strip() == "取引所現物取引":
                csv_rows.append(row)

    print(f"CSV内の「取引所現物取引」: {len(csv_rows)} 行")

    # IDごとに何行マッピングされるか
    id_map: dict[str, list[dict]] = defaultdict(list)
    for row in csv_rows:
        tx_id = build_key(row, source_id)
        id_map[tx_id].append(row)

    # 衝突（同じIDに複数行）を探す
    collisions = {k: v for k, v in id_map.items() if len(v) > 1}
    print(f"生成されるユニークID数 : {len(id_map)}")
    print(f"ID衝突が起きているID数 : {len(collisions)}")
    dropped = sum(len(v) - 1 for v in collisions.values())
    print(f"衝突で消える行数      : {dropped} 行")

    if collisions:
        print("\n=== 衝突の詳細（最初の3件） ===")
        for i, (tx_id, rows) in enumerate(list(collisions.items())[:3]):
            print(f"\n[ID: {tx_id}] — {len(rows)}行が同じIDに")
            for r in rows:
                print(f"  日時={r['日時']} 銘柄={r['銘柄名']} 区分={r['売買区分']} "
                      f"約定ID={r.get('約定ID','')} 約定数量={r['約定数量']} 約定金額={r['約定金額']}")

    # 売買区分の種類を確認
    print("\n=== 売買区分の種類 ===")
    sides = defaultdict(int)
    for row in csv_rows:
        sides[row.get("売買区分", "").strip()] += 1
    for s, c in sorted(sides.items()):
        print(f"  {s!r}: {c}件")

    # 約定IDがある場合に含めたキーでのユニーク数
    id_map2: dict[str, list[dict]] = defaultdict(list)
    for row in csv_rows:
        raw_key2 = "|".join([
            row["日時"], row["注文ID"], row.get("約定ID", ""), row["銘柄名"],
            row["売買区分"], row["約定数量"], row["約定金額"],
        ])
        tx_id2 = make_id(source_id, raw_key2)
        id_map2[tx_id2].append(row)

    collisions2 = {k: v for k, v in id_map2.items() if len(v) > 1}
    print(f"\n約定ID追加後のユニークID数: {len(id_map2)}")
    print(f"約定ID追加後の衝突ID数    : {len(collisions2)}")


if __name__ == "__main__":
    main()
