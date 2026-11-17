#!/usr/bin/env python3
"""Create one sale order for an inclusive range of items from a single SKU.

Dry run by default. Example:
  python sell_item_range.py PIN2-0084 PIN2-0150 --payment-method CASH --service-account serviceAccountKey.json
Add --commit to create the order and mark all items SOLD atomically.
Single-SKU ranges use product retail prices; current app combos all require
multiple SKU groups, so none apply to this operation.
"""

from __future__ import annotations

import argparse
import re
import sys


def expand_range(first, last):
    first, last = first.strip().upper(), last.strip().upper()
    start = re.fullmatch(r"([A-Z0-9][A-Z0-9_-]*)-(\d+)", first)
    end = re.fullmatch(r"([A-Z0-9][A-Z0-9_-]*)-(\d+)", last)
    if not start or not end or start[1] != end[1] or len(start[2]) != len(end[2]):
        raise ValueError("Use matching prefixes and number widths, e.g. PIN2-0084 PIN2-0150")
    low, high = int(start[2]), int(end[2])
    if low < 1 or high < low or high - low + 1 > 400:
        raise ValueError("Range must be ascending and contain 1 to 400 items, starting at 1 or higher")
    return [f"{start[1]}-{n:0{len(start[2])}d}" for n in range(low, high + 1)]


def read_sale(db, item_ids, order_id, transaction=None):
    """Read and validate everything before queuing any writes."""
    order_ref = db.collection("orders").document(order_id)
    if order_ref.get(transaction=transaction).exists:
        raise ValueError(f"Order {order_id} already exists; no sale will be created")
    records = []
    for item_id in item_ids:
        ref = db.collection("items").document(item_id)
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise ValueError(f"Item {item_id} does not exist")
        data = snap.to_dict() or {}
        status = data.get("status", data.get("STATUS"))
        if status != "IN_STOCK" or data.get("orderId"):
            raise ValueError(f"Item {item_id} is unavailable (status={status}, orderId={data.get('orderId')})")
        sku = str(data.get("sku", data.get("SKU", ""))).strip().upper()
        if not sku:
            raise ValueError(f"Item {item_id} has no SKU")
        records.append((ref, item_id, sku))
    skus = {record[2] for record in records}
    if len(skus) != 1:
        raise ValueError("All range items must belong to the same SKU")
    sku = next(iter(skus))
    snap = db.collection("products").document(sku).get(transaction=transaction)
    if not snap.exists:
        raise ValueError(f"Product {sku} does not exist")
    product = snap.to_dict() or {}
    price = product.get("price", product.get("PRICE"))
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not 0 < price < float("inf") or int(price) != price:
        raise ValueError(f"Product {sku} needs a positive whole-number VND price")
    name = product.get("name", product.get("productName", product.get("PRODUCT_NAME", sku)))
    items = [{"itemId": item_id, "sku": sku, "productName": name, "price": int(price)} for _, item_id, _ in records]
    return order_ref, records, items


def queue_sale(transaction, order_ref, records, items, payment_method, actor, email, timestamp):
    total = sum(item["price"] for item in items)
    for ref, _, _ in records:
        transaction.update(ref, {
            "status": "SOLD", "orderId": order_ref.id,
            "soldAt": timestamp, "soldBy": actor,
            "soldDeviceId": "manual-range-script", "updatedAt": timestamp,
        })
    transaction.set(order_ref, {
        "orderId": order_ref.id, "status": "COMPLETED", "paymentMethod": payment_method,
        "subtotal": total, "discountAmount": 0, "totalAmount": total,
        "activeSubtotal": total, "activeDiscountAmount": 0, "activeTotalAmount": total,
        "activeItems": len(items), "totalItems": len(items),
        "appliedCombos": [], "activeCombos": [], "voidedItemIds": [],
        "sellerUid": actor, "sellerEmail": email, "deviceId": "manual-range-script",
        "createdAt": timestamp, "updatedAt": timestamp, "items": items,
        "source": "manual-range-script",
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("first", help="First item ID, inclusive")
    parser.add_argument("last", help="Last item ID, inclusive")
    parser.add_argument("--payment-method", required=True, choices=["CASH", "TRANSFER"])
    parser.add_argument("--service-account")
    parser.add_argument("--project-id", default="item-level-tracking")
    parser.add_argument("--seller-uid", default="manual-range-script", help="Optional actual seller Firebase UID")
    parser.add_argument("--seller-email", default="")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    try:
        item_ids = expand_range(args.first, args.last)
        # A stable order ID also protects retries after an uncertain network response.
        order_id = f"ORD-MANUAL-{item_ids[0]}-TO-{item_ids[-1]}"
        from reset_all import init_firestore
        from google.cloud import firestore

        db = init_firestore(args.project_id, args.service_account)
        print(f"Project: {args.project_id}\nOrder: {order_id}")
        print(f"Range: {item_ids[0]} through {item_ids[-1]} ({len(item_ids)} items)")
        print(f"Payment: {args.payment_method}; seller: {args.seller_uid}")
        if args.commit:
            @firestore.transactional
            def sell(transaction):
                order_ref, records, items = read_sale(db, item_ids, order_id, transaction)
                queue_sale(transaction, order_ref, records, items, args.payment_method,
                           args.seller_uid, args.seller_email, firestore.SERVER_TIMESTAMP)
                return items

            items = sell(db.transaction())
            print("SALE COMPLETE: order created and all range items marked SOLD.")
        else:
            _, _, items = read_sale(db, item_ids, order_id)
            print("DRY RUN: nothing written. Add --commit to create this sale.")
            print("Commit rechecks stock and product prices; preview does not reserve items.")
        print(f"Unit price: {items[0]['price']:,} VND")
        print(f"Total: {sum(item['price'] for item in items):,} VND (no combo discount)")
        print("Sale timestamps use the execution time, not a historical sale date.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
