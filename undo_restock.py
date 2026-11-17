#!/usr/bin/env python3
"""Restore explicit restocked items to their original sales. Dry run by default.

python undo_restock.py restore_pin1_last50.json --service-account serviceAccountKey.json
Add --commit to restore saved sale fields and recalculate original orders atomically.
The manifest contains sourceOperation and an items object mapping IDs to original
order IDs. A correction receipt prevents repeating the same correction.
"""

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

from order_pricing import calculate_pricing
from restock_sold_range import item_id


def read_plan(db, manifest, transaction=None):
    mapping = manifest["items"]
    if not isinstance(mapping, dict) or not 1 <= len(mapping) <= 200:
        raise ValueError("Manifest needs 1 to 200 explicit item/order mappings")
    source_id = manifest["sourceOperation"]
    if any(not isinstance(x, str) or not x or "/" in x for x in [source_id, *mapping, *mapping.values()]):
        raise ValueError("Invalid document ID in manifest")
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:24]
    receipt = db.collection("restock_corrections").document(f"UNDO-{digest}")
    if receipt.get(transaction=transaction).exists:
        raise ValueError(f"Correction {receipt.id} already completed")
    source = db.collection("restock_operations").document(source_id).get(transaction=transaction)
    if not source.exists:
        raise ValueError(f"Source restock operation {source_id} does not exist")
    source_data = source.to_dict() or {}
    if not set(mapping) <= set(source_data.get("itemIds", [])) or not source_data.get("createdAt"):
        raise ValueError("Items are not all recorded in the source restock operation")
    selected, order_ids = [], set()
    for candidate, order_id in mapping.items():
        ref = db.collection("items").document(candidate)
        snap = ref.get(transaction=transaction)
        data = snap.to_dict() or {}
        if not snap.exists or data.get("status", data.get("STATUS")) != "IN_STOCK" or data.get("orderId") or data.get("ORDER_ID"):
            raise ValueError(f"{candidate} must exist, be IN_STOCK, and have no current order")
        if data.get("lastOrderId") != order_id or not data.get("lastSoldAt"):
            raise ValueError(f"{candidate} has different or missing saved sale details")
        if data.get("restockedAt") != source_data["createdAt"]:
            raise ValueError(f"{candidate} was restocked by a different operation")
        selected.append((ref, candidate, data))
        order_ids.add(order_id)
    orders = []
    for order_id in sorted(order_ids):
        ref = db.collection("orders").document(order_id)
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise ValueError(f"Original order {order_id} is missing")
        data = snap.to_dict() or {}
        lines = data.get("items")
        if not isinstance(lines, list) or not lines:
            raise ValueError(f"Order {order_id} has no valid lines")
        ids = [item_id(line) for line in lines]
        restored = {key for key, value in mapping.items() if value == order_id}
        voided = {str(key).upper() for key in (data.get("voidedItemIds") or [])}
        if not all(ids) or len(ids) != len(set(ids)) or not restored <= set(ids) or not restored <= voided:
            raise ValueError(f"Order {order_id} does not contain the expected voided items")
        voided -= restored
        active = []
        for line in lines:
            if item_id(line) in voided:
                continue
            if not isinstance(line, dict) or "price" not in line:
                raise ValueError(f"Order {order_id} is missing recorded prices")
            price = line["price"]
            if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0:
                raise ValueError(f"Order {order_id} has an invalid price")
            active.append({**line, "itemId": item_id(line)})
        if "subtotal" in data:
            pricing = calculate_pricing(active)
        else:
            total = sum(line["price"] for line in active)
            pricing = {"subtotal": total, "discountAmount": 0, "totalAmount": total, "appliedCombos": []}
        changes = {
            "status": "PARTIALLY_VOIDED" if set(ids) & voided else "COMPLETED",
            "voidedItemIds": sorted(voided), "activeItems": len(active),
            "activeSubtotal": pricing["subtotal"], "activeDiscountAmount": pricing["discountAmount"],
            "activeTotalAmount": pricing["totalAmount"], "activeCombos": pricing["appliedCombos"],
        }
        orders.append((ref, data, changes))
    return receipt, selected, orders


def queue_restore(transaction, plan, manifest, timestamp):
    receipt, selected, orders = plan
    for ref, candidate, data in selected:
        changes = {
            "status": "SOLD", "orderId": manifest["items"][candidate],
            "soldAt": data["lastSoldAt"], "soldBy": data.get("lastSoldBy"),
            "soldDeviceId": data.get("lastSoldDeviceId"), "updatedAt": timestamp,
            "restockCorrectedAt": timestamp, "restockCorrectionId": receipt.id,
        }
        if "STATUS" in data:
            changes["STATUS"] = "SOLD"
        if "ORDER_ID" in data:
            changes["ORDER_ID"] = manifest["items"][candidate]
        transaction.update(ref, changes)
    for ref, _, changes in orders:
        transaction.update(ref, {**changes, "updatedAt": timestamp,
                                 "restockCorrectedAt": timestamp, "restockCorrectionId": receipt.id})
    transaction.set(receipt, {"sourceOperation": manifest["sourceOperation"],
                              "items": manifest["items"], "count": len(selected),
                              "createdAt": timestamp, "reason": "Undo mistaken restock; restore original sales"})


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("manifest")
    parser.add_argument("--service-account")
    parser.add_argument("--project-id", default="item-level-tracking")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        from reset_all import init_firestore
        from google.cloud import firestore

        db = init_firestore(args.project_id, args.service_account)
        plan = read_plan(db, manifest)
        print(f"Project: {args.project_id}; restore {len(plan[1])} items across {len(plan[2])} orders")
        for _, candidate, _ in plan[1]:
            print(f"  {candidate} -> SOLD in {manifest['items'][candidate]}")
        for ref, before, changes in plan[2]:
            print(f"  {ref.id}: {before.get('activeTotalAmount')} -> {changes['activeTotalAmount']} VND; {changes['status']}")
        if not args.commit:
            print("DRY RUN: nothing changed. Add --commit to restore these original sales.")
            return 0
        backup_dir = Path("correction_backups")
        backup_dir.mkdir(exist_ok=True)
        backup = backup_dir / f"{plan[0].id}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
        # Capture the exact transaction snapshot before queuing writes, including retries.
        @firestore.transactional
        def restore(transaction):
            fresh = read_plan(db, manifest, transaction)
            backup.write_text(json.dumps({"manifest": manifest,
                "items": {ref.path: data for ref, _, data in fresh[1]},
                "orders": {ref.path: data for ref, data, _ in fresh[2]}},
                indent=2, default=str), encoding="utf-8")
            queue_restore(transaction, fresh, manifest, firestore.SERVER_TIMESTAMP)

        restore(db.transaction())
        print(f"RESTORE COMPLETE. Backup: {backup}; receipt: {plan[0].path}")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
