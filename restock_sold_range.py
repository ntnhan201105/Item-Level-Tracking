#!/usr/bin/env python3
"""Restock the first N SOLD items in an inclusive numeric ID range.

Preview:
  python restock_sold_range.py PIN1-0001 PIN1-0100 --count 67 --service-account serviceAccountKey.json
Add --commit to update items, linked orders, and an operation receipt atomically.
Selection uses ascending item number, not sale date. Insufficient SOLD items
aborts the entire operation. An operation receipt prevents accidental reruns.
"""

import argparse
import math
import sys

from order_pricing import calculate_pricing
from sell_item_range import expand_range


def item_id(entry):
    return str(entry.get("itemId", entry.get("id", "")) if isinstance(entry, dict) else entry).strip().upper()


def read_plan(db, candidates, count, operation_id, transaction=None):
    receipt = db.collection("restock_operations").document(operation_id)
    if receipt.get(transaction=transaction).exists:
        raise ValueError(f"Operation {operation_id} already completed; no more items will be restocked")
    selected = []
    expected_sku = None
    for candidate in candidates:
        ref = db.collection("items").document(candidate)
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            continue
        data = snap.to_dict() or {}
        if data.get("status", data.get("STATUS")) != "SOLD":
            continue
        sku = str(data.get("sku", data.get("SKU", ""))).strip().upper()
        if not sku or (expected_sku is not None and sku != expected_sku):
            raise ValueError(f"Missing or inconsistent SKU on {candidate}")
        expected_sku = sku
        selected.append((ref, candidate, data))
        if len(selected) == count:
            break
    if len(selected) != count:
        raise ValueError(f"Found only {len(selected)} SOLD items in range; need {count}. Nothing changed")

    by_order = {}
    for _, candidate, data in selected:
        order_id = data.get("orderId") or data.get("ORDER_ID")
        if order_id:
            by_order.setdefault(order_id, []).append(candidate)
    orders = []
    for order_id, returned in by_order.items():
        ref = db.collection("orders").document(order_id)
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise ValueError(f"Linked order {order_id} is missing; resolve it before restocking")
        data = snap.to_dict() or {}
        lines = data.get("items")
        if not isinstance(lines, list) or not lines:
            raise ValueError(f"Order {order_id} has no valid items list")
        ids = [item_id(line) for line in lines]
        if not all(ids) or len(set(ids)) != len(ids) or not set(returned) <= set(ids):
            raise ValueError(f"Order {order_id} has inconsistent item references")
        voided = {str(x).upper() for x in (data.get("voidedItemIds") or [])}
        if voided & set(returned):
            raise ValueError(f"Selected items are already voided in order {order_id}")
        voided.update(returned)
        remaining = [line for line in lines if item_id(line) not in voided]
        normalized = []
        for line in remaining:
            if not isinstance(line, dict):
                raise ValueError(f"Order {order_id} has a remaining line without a recorded price")
            price = line.get("price", 0) or 0
            if isinstance(price, bool) or not isinstance(price, (float, int)) or not math.isfinite(price) or price < 0:
                raise ValueError(f"Order {order_id} has an invalid price")
            normalized.append({**line, "itemId": item_id(line), "price": price})
        if "subtotal" in data:
            pricing = calculate_pricing(normalized)
        else:
            total = sum(line["price"] for line in normalized)
            pricing = {"subtotal": total, "discountAmount": 0, "totalAmount": total, "appliedCombos": []}
        orders.append((ref, {
            "status": "PARTIALLY_VOIDED" if remaining else "VOIDED",
            "voidedItemIds": sorted(voided), "activeItems": len(remaining),
            "activeSubtotal": pricing["subtotal"], "activeDiscountAmount": pricing["discountAmount"],
            "activeTotalAmount": pricing["totalAmount"], "activeCombos": pricing["appliedCombos"],
            "lastRestockedItemId": returned[-1],
        }))
    return receipt, selected, orders


def queue_restock(transaction, plan, actor, reason, timestamp):
    receipt, selected, orders = plan
    for ref, _, data in selected:
        changes = {
            "status": "IN_STOCK", "orderId": None, "soldAt": None,
            "soldBy": None, "soldDeviceId": None, "updatedAt": timestamp,
            "restockedAt": timestamp, "restockedBy": actor,
            "restockedDeviceId": "manual-restock-script", "restockReason": reason,
            "lastOrderId": data.get("orderId") or data.get("ORDER_ID"),
            "lastSoldAt": data.get("soldAt"), "lastSoldBy": data.get("soldBy"),
            "lastSoldDeviceId": data.get("soldDeviceId"),
        }
        if "ORDER_ID" in data:
            changes["ORDER_ID"] = None
        if "STATUS" in data:
            changes["STATUS"] = "IN_STOCK"
        transaction.update(ref, changes)
    for ref, changes in orders:
        transaction.update(ref, {**changes, "updatedAt": timestamp, "lastRestockedAt": timestamp,
                                 "lastRestockedBy": actor, "lastRestockReason": reason})
    transaction.set(receipt, {
        "itemIds": [candidate for _, candidate, _ in selected],
        "orderIds": [ref.id for ref, _ in orders], "count": len(selected),
        "createdAt": timestamp, "actor": actor, "reason": reason,
    })


def print_plan(plan):
    receipt, selected, orders = plan
    print(f"Operation: {receipt.id}\nSelected SOLD items: {len(selected)}")
    for _, candidate, data in selected:
        print(f"  {candidate} (order: {data.get('orderId') or data.get('ORDER_ID') or 'none'})")
    for ref, changes in orders:
        print(f"  Order {ref.id}: {changes['status']}, active items={changes['activeItems']}, active total={changes['activeTotalAmount']:,} VND")
    unlinked = sum(not (data.get("orderId") or data.get("ORDER_ID")) for _, _, data in selected)
    if unlinked:
        print(f"{unlinked} selected items have no order reference; only their inventory records will be updated.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("first")
    parser.add_argument("last")
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--service-account")
    parser.add_argument("--project-id", default="item-level-tracking")
    parser.add_argument("--actor", default="manual-restock-script")
    parser.add_argument("--reason", default="Manual inventory correction")
    parser.add_argument("--operation-id", help="Override receipt ID only for a deliberately separate restock")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    try:
        candidates = expand_range(args.first, args.last)
        if not 1 <= args.count <= min(200, len(candidates)):
            raise ValueError("--count must be between 1 and 200 and no larger than the range")
        operation_id = args.operation_id or f"RESTOCK-{candidates[0]}-TO-{candidates[-1]}-{args.count}"
        if not operation_id.strip() or "/" in operation_id or operation_id in (".", ".."):
            raise ValueError("Invalid operation ID")
        from reset_all import init_firestore
        from google.cloud import firestore

        db = init_firestore(args.project_id, args.service_account)
        print(f"Project: {args.project_id}")
        plan = read_plan(db, candidates, args.count, operation_id)
        print_plan(plan)
        if not args.commit:
            print("DRY RUN: nothing changed. Add --commit to restock these items and update orders.")
            return 0
        expected_ids = [candidate for _, candidate, _ in plan[1]]

        @firestore.transactional
        def restock(transaction):
            fresh = read_plan(db, candidates, args.count, operation_id, transaction)
            if [candidate for _, candidate, _ in fresh[1]] != expected_ids:
                raise ValueError("Selected items changed during execution; preview again")
            queue_restock(transaction, fresh, args.actor, args.reason, firestore.SERVER_TIMESTAMP)

        restock(db.transaction())
        print(f"RESTOCK COMPLETE: {args.count} items returned to IN_STOCK; linked orders updated.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
