#!/usr/bin/env python3
"""
Delete ALL documents in Firestore collection `items`.

Dry-run by default.
Use --commit to actually delete.
Use --reset-counters if you also want future ITEM_ID sequences to restart from 0001.
"""

import argparse
import sys
from typing import Optional

import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore as admin_firestore

DEFAULT_PROJECT_ID = "item-level-tracking"
DEFAULT_BATCH_SIZE = 400


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--project-id", default=DEFAULT_PROJECT_ID)
    p.add_argument("--service-account")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--commit", action="store_true")
    p.add_argument("--reset-counters", action="store_true")
    return p.parse_args()


def init_firestore(project_id: str, service_account: Optional[str]):
    if firebase_admin._apps:
        return admin_firestore.client()

    cred = (
        credentials.Certificate(service_account)
        if service_account
        else credentials.ApplicationDefault()
    )
    firebase_admin.initialize_app(cred, {"projectId": project_id})
    return admin_firestore.client()


def count_docs(db, collection_name: str) -> int:
    return sum(1 for _ in db.collection(collection_name).stream())


def delete_collection(db, collection_name: str, batch_size: int) -> int:
    if not 1 <= batch_size <= 450:
        raise ValueError("--batch-size must be between 1 and 450")

    deleted = 0

    while True:
        docs = list(db.collection(collection_name).limit(batch_size).stream())
        if not docs:
            break

        batch = db.batch()
        for snap in docs:
            batch.delete(snap.reference)

        batch.commit()
        deleted += len(docs)
        print(f"Deleted {deleted} document(s) from `{collection_name}`...")

    return deleted


def main():
    args = parse_args()

    try:
        db = init_firestore(args.project_id, args.service_account)

        item_count = count_docs(db, "items")
        counter_count = count_docs(db, "counters") if args.reset_counters else 0

        print(f"Project: {args.project_id}")
        print(f"`items` documents found: {item_count}")

        if args.reset_counters:
            print(f"`counters` documents found: {counter_count}")

        if not args.commit:
            print("\nDRY RUN: nothing was deleted.")
            print("Run again with --commit to actually delete ALL items.")
            return 0

        deleted_items = delete_collection(db, "items", args.batch_size)

        deleted_counters = 0
        if args.reset_counters:
            deleted_counters = delete_collection(db, "counters", args.batch_size)

        print("\nDELETE COMPLETE")
        print(f"Deleted items: {deleted_items}")

        if args.reset_counters:
            print(f"Deleted counters: {deleted_counters}")
            print("Future imports can restart from 0001.")
        else:
            print("Counters kept; future imports continue existing numbering.")

        return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
