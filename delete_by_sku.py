#!/usr/bin/env python3
"""Delete a SKU's items, product, whole orders, and related sales.

Preview: python delete_by_sku.py DC1 --service-account serviceAccountKey.json
Delete:  python delete_by_sku.py DC1 --service-account serviceAccountKey.json --commit --confirm DELETE_SKU:DC1

SKU is an exact match (DC1, not the serialized item ID DC1-000001).
Mixed orders are deleted in full; other SKUs' inventory items are retained.
Counters are retained unless --reset-counters is supplied. That option deletes
only counters/{SKU}; custom ID_PREFIX counters must be managed separately.
Pause checkout/import activity before previewing and committing the cleanup.
"""

from __future__ import annotations

import argparse
import sys

def init_firestore(project_id, service_account):
    # Keep help and local selection tests usable without Firebase installed.
    from reset_all import init_firestore as initialize

    return initialize(project_id, service_account)


def normalize(value):
    return str(value or "").strip().upper()


def values_for(data, fields):
    """Read known reference fields, including nested order/sale line items."""
    found = set()
    if isinstance(data, dict):
        for key, value in data.items():
            if key.lower() in fields:
                values = value if isinstance(value, list) else [value]
                found.update(normalize(v) for v in values if isinstance(v, (str, int)))
            if isinstance(value, (dict, list)):
                found.update(values_for(value, fields))
    elif isinstance(data, list):
        for value in data:
            found.update(values_for(value, fields))
    return found - {""}


def item_references(data):
    refs = values_for(data, {"itemid", "item_id", "itemids", "voideditemids", "lastrestockeditemid"})
    # Older orders can store items as strings, or objects with an `id` field.
    if isinstance(data, dict):
        for entry in data.get("items", []) or []:
            if isinstance(entry, str):
                refs.add(normalize(entry))
            elif isinstance(entry, dict) and entry.get("id"):
                refs.add(normalize(entry["id"]))
    return refs


def build_plan(records, sku, reset_counters=False):
    """Build a fixed deletion plan from collection snapshots without writes."""
    plan = {name: {} for name in ("items", "orders", "sales", "products", "counters")}
    item_ids = set()
    order_ids = set()

    for snap in records["items"]:
        data = snap.to_dict() or {}
        if sku in values_for(data, {"sku"}):
            plan["items"][snap.reference.path] = snap
            item_ids.add(normalize(snap.id))
            item_ids.update(values_for(data, {"itemid", "item_id"}))
            order_ids.update(values_for(data, {"orderid", "lastorderid"}))

    for snap in records["orders"]:
        data = snap.to_dict() or {}
        if (normalize(snap.id) in order_ids
                or sku in values_for(data, {"sku"})
                or item_references(data) & item_ids):
            plan["orders"][snap.reference.path] = snap
            order_ids.add(normalize(snap.id))
            order_ids.update(values_for(data, {"orderid"}))

    for snap in records["sales"]:
        data = snap.to_dict() or {}
        if (sku in values_for(data, {"sku"})
                or item_references(data) & item_ids
                or values_for(data, {"orderid", "order_id"}) & order_ids
                or normalize(snap.id) in order_ids):
            plan["sales"][snap.reference.path] = snap

    for snap in records["products"]:
        if normalize(snap.id) == sku or sku in values_for(snap.to_dict() or {}, {"sku"}):
            plan["products"][snap.reference.path] = snap

    if reset_counters:
        for snap in records["counters"]:
            if normalize(snap.id) == sku:
                plan["counters"][snap.reference.path] = snap

    return plan


def descendant_refs(ref):
    # list_documents also visits missing parent documents that have descendants.
    for collection in ref.collections():
        for child in collection.list_documents():
            yield from descendant_refs(child)
            yield child


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("sku", help="Exact SKU, e.g. DC1 (case insensitive)")
    parser.add_argument("--service-account")
    parser.add_argument("--project-id", default="item-level-tracking")
    parser.add_argument("--batch-size", type=int, default=300)
    parser.add_argument("--reset-counters", action="store_true", help="Also delete counters/{SKU}")
    parser.add_argument("--commit", action="store_true")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    sku = normalize(args.sku)
    if not sku or any(char in sku for char in "/?*"):
        parser.error("Provide one exact SKU, without slashes or wildcards.")
    if not 1 <= args.batch_size <= 450:
        parser.error("--batch-size must be between 1 and 450")
    confirmation = f"DELETE_SKU:{sku}"
    if args.commit and args.confirm != confirmation:
        parser.error(f"Deletion requires --confirm {confirmation}")

    try:
        db = init_firestore(args.project_id, args.service_account)
        names = ["items", "orders", "sales", "products"]
        if args.reset_counters:
            names.append("counters")
        print(f"Project: {args.project_id}\nExact SKU: {sku}")
        print("Scanning collections (pause checkout/import activity during cleanup)...")
        records = {name: list(db.collection(name).stream()) for name in names}
        plan = build_plan(records, sku, args.reset_counters)
        paths = {}
        for name, snapshots in plan.items():
            print(f"\n{name}: {len(snapshots)} matching document(s)")
            for path, snap in sorted(snapshots.items()):
                print(f"  {path}")
                paths[path] = snap.reference
                for ref in descendant_refs(snap.reference):
                    paths[ref.path] = ref

        print(f"\nTotal document paths to delete, including descendants: {len(paths)}")
        print("Whole matching orders and their related sales will be deleted.")
        print("Other SKUs' inventory items are retained, including their existing sale/order fields.")
        if not args.reset_counters:
            print("Counters retained; future imports continue existing numbering.")
        else:
            print(f"Only counter {sku} is selected; custom ID_PREFIX counters are retained.")
        if not args.commit:
            print(f"\nDRY RUN: nothing deleted. To delete, add --commit --confirm {confirmation}")
            return 0

        # Descendants first; delete only the paths displayed by this plan.
        refs = sorted(paths.values(), key=lambda ref: (-ref.path.count("/"), ref.path))
        deleted = 0
        for offset in range(0, len(refs), args.batch_size):
            batch = db.batch()
            chunk = refs[offset:offset + args.batch_size]
            for ref in chunk:
                batch.delete(ref)
            batch.commit()
            deleted += len(chunk)
            print(f"Deleted {deleted}/{len(refs)} document paths...")
        print("DELETE COMPLETE")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("If deletion had started, some batches may already be committed. Preview again before retrying.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
