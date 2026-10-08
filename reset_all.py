#!/usr/bin/env python3
"""
RESET ENTIRE FIRESTORE DATABASE.

This script deletes ALL documents from ALL top-level collections in the
configured Firestore database, including nested subcollections.

Examples:
  # Dry run (safe, deletes nothing)
  python reset_all.py --service-account serviceAccountKey.json

  # Actually wipe everything
  python reset_all.py --service-account serviceAccountKey.json --commit --confirm RESET_ALL

WARNING:
- This is destructive.
- It deletes products, items, orders, sales, counters, and any other
  top-level collection it finds.
- Firestore document deletion does not automatically delete subcollections,
  so this script recursively deletes nested subcollections too.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Tuple

import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore as admin_firestore


DEFAULT_PROJECT_ID = "item-level-tracking"
DEFAULT_BATCH_SIZE = 300
CONFIRM_TEXT = "RESET_ALL"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Delete ALL Firestore data in the project database."
    )
    p.add_argument(
        "--project-id",
        default=DEFAULT_PROJECT_ID,
        help=f"Firebase / GCP project ID (default: {DEFAULT_PROJECT_ID})",
    )
    p.add_argument(
        "--service-account",
        help="Path to service-account JSON. If omitted, Application Default Credentials are used.",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"Maximum document deletes per batch (default: {DEFAULT_BATCH_SIZE}; max 450).",
    )
    p.add_argument(
        "--commit",
        action="store_true",
        help="Actually delete data. Without this flag, only a dry run is performed.",
    )
    p.add_argument(
        "--confirm",
        default="",
        help=f'Required with --commit. Must be exactly "{CONFIRM_TEXT}".',
    )
    return p.parse_args()


def init_firestore(project_id: str, service_account: Optional[str]):
    if firebase_admin._apps:
        return admin_firestore.client()

    if service_account:
        cred = credentials.Certificate(service_account)
    else:
        cred = credentials.ApplicationDefault()

    firebase_admin.initialize_app(cred, {"projectId": project_id})
    return admin_firestore.client()


def count_collection_recursive(collection_ref) -> Tuple[int, int]:
    """
    Returns:
      (document_count, nested_collection_count)
    """
    doc_count = 0
    nested_collection_count = 0

    for snap in collection_ref.stream():
        doc_count += 1

        for subcol in snap.reference.collections():
            nested_collection_count += 1
            sub_docs, sub_cols = count_collection_recursive(subcol)
            doc_count += sub_docs
            nested_collection_count += sub_cols

    return doc_count, nested_collection_count


def delete_collection_recursive(db, collection_ref, batch_size: int) -> int:
    """
    Recursively delete every document in collection_ref and all nested
    subcollections. Returns total deleted document count.
    """
    deleted_total = 0

    while True:
        docs = list(collection_ref.limit(batch_size).stream())

        if not docs:
            break

        # Firestore does NOT automatically delete subcollections when a parent
        # document is deleted. Remove descendants first.
        for snap in docs:
            for subcol in snap.reference.collections():
                deleted_total += delete_collection_recursive(
                    db,
                    subcol,
                    batch_size,
                )

        batch = db.batch()

        for snap in docs:
            batch.delete(snap.reference)

        batch.commit()
        deleted_total += len(docs)

        print(
            f"Deleted {deleted_total} document(s) so far "
            f"under `{collection_ref.id}`..."
        )

    return deleted_total


def main() -> int:
    args = parse_args()

    if args.batch_size < 1 or args.batch_size > 450:
        print("ERROR: --batch-size must be between 1 and 450.", file=sys.stderr)
        return 2

    if args.commit and args.confirm != CONFIRM_TEXT:
        print(
            f'ERROR: destructive reset requires: --confirm {CONFIRM_TEXT}',
            file=sys.stderr,
        )
        return 2

    try:
        db = init_firestore(args.project_id, args.service_account)

        root_collections = list(db.collections())

        print(f"Project: {args.project_id}")

        if not root_collections:
            print("No top-level Firestore collections found. Database is already empty.")
            return 0

        print("\nTop-level collections found:")
        total_docs = 0

        for collection_ref in root_collections:
            docs, nested_cols = count_collection_recursive(collection_ref)
            total_docs += docs
            print(
                f"  - {collection_ref.id}: "
                f"{docs} document(s), {nested_cols} nested collection(s)"
            )

        print(f"\nTOTAL DOCUMENTS TO DELETE: {total_docs}")

        if not args.commit:
            print("\nDRY RUN: nothing was deleted.")
            print(
                "\nTo permanently wipe the entire Firestore database, run:\n"
                "  python reset_all.py "
                "--service-account serviceAccountKey.json "
                "--commit --confirm RESET_ALL"
            )
            return 0

        print("\n*** RESET STARTED — THIS CANNOT BE UNDONE BY THIS SCRIPT ***\n")

        grand_total = 0

        # Discover again in case the dry-run/count traversal took some time.
        for collection_ref in list(db.collections()):
            print(f"Deleting `{collection_ref.id}`...")
            deleted = delete_collection_recursive(
                db,
                collection_ref,
                args.batch_size,
            )
            grand_total += deleted
            print(f"Finished `{collection_ref.id}`: {deleted} document(s) deleted.\n")

        remaining = list(db.collections())

        print("RESET COMPLETE")
        print(f"Total deleted documents: {grand_total}")

        if remaining:
            print(
                "WARNING: Firestore still reports top-level collections. "
                "They may contain documents created concurrently during the reset:",
                file=sys.stderr,
            )
            for col in remaining:
                print(f"  - {col.id}", file=sys.stderr)
            return 1

        print("Firestore database is empty.")
        return 0

    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
