"""Offline validation of manual range sales; no Firebase access."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from sell_item_range import expand_range, queue_sale, read_sale


class RangeSaleTests(unittest.TestCase):
    def database(self, item_ids, overrides=None):
        data = {f"items/{item_id}": {"sku": "PIN2", "status": "IN_STOCK"} for item_id in item_ids}
        data["products/PIN2"] = {"name": "Pin 2", "price": 20000}
        data.update(overrides or {})
        db = Mock()
        events = []

        def document(collection, doc_id):
            path = f"{collection}/{doc_id}"

            def get(transaction=None):
                self.assertFalse(any(event[0] == "write" for event in events))
                events.append(("read", path, transaction))
                return SimpleNamespace(exists=path in data, to_dict=lambda: data.get(path))

            return SimpleNamespace(id=doc_id, path=path, get=get)

        db.collection.side_effect = lambda name: SimpleNamespace(document=lambda doc_id: document(name, doc_id))
        return db, events

    def test_inclusive_range_and_atomic_order_payload(self):
        ids = expand_range("PIN2-0084", "PIN2-0150")
        self.assertEqual(len(ids), 67)
        self.assertEqual((ids[0], ids[-1]), ("PIN2-0084", "PIN2-0150"))
        db, events = self.database(ids)
        transaction = Mock()
        transaction.update.side_effect = lambda *args: events.append(("write", "item"))
        transaction.set.side_effect = lambda *args: events.append(("write", "order"))
        ref, records, items = read_sale(db, ids, "ORD-TEST", transaction)
        queue_sale(transaction, ref, records, items, "TRANSFER", "seller", "seller@example.com", "timestamp")
        self.assertEqual(transaction.update.call_count, 67)
        order = transaction.set.call_args.args[1]
        self.assertEqual(order["totalAmount"], 1340000)
        self.assertEqual(order["activeItems"], 67)
        self.assertEqual(order["paymentMethod"], "TRANSFER")
        self.assertEqual(order["items"][-1]["itemId"], "PIN2-0150")
        self.assertEqual(transaction.update.call_args.args[1]["orderId"], "ORD-TEST")
        self.assertTrue(all(e[2] is transaction for e in events if e[0] == "read"))

    def test_unavailable_or_missing_item_aborts_before_writes(self):
        ids = expand_range("PIN2-0084", "PIN2-0085")
        for override in ({"items/PIN2-0085": {"sku": "PIN2", "status": "SOLD"}}, {}):
            db, _ = self.database(ids if override else ids[:1], override)
            transaction = Mock()
            with self.assertRaises(ValueError):
                read_sale(db, ids, "ORD-TEST", transaction)
            transaction.update.assert_not_called()
            transaction.set.assert_not_called()

    def test_duplicate_order_rejected(self):
        ids = expand_range("PIN2-0084", "PIN2-0084")
        db, _ = self.database(ids, {"orders/ORD-TEST": {"status": "COMPLETED"}})
        with self.assertRaisesRegex(ValueError, "already exists"):
            read_sale(db, ids, "ORD-TEST")

    def test_preview_only_reads(self):
        ids = expand_range("PIN2-0084", "PIN2-0084")
        db, events = self.database(ids)
        read_sale(db, ids, "ORD-TEST")
        self.assertTrue(all(e[0] == "read" for e in events))
        db.batch.assert_not_called()
        db.transaction.assert_not_called()

    def test_mixed_skus_and_invalid_prices_rejected(self):
        ids = expand_range("PIN2-0084", "PIN2-0085")
        db, _ = self.database(ids, {"items/PIN2-0085": {"sku": "PIN1", "status": "IN_STOCK"}})
        with self.assertRaisesRegex(ValueError, "same SKU"):
            read_sale(db, ids, "ORD-TEST")
        for price in (None, -1, 0, True, "20000", float("nan"), float("inf"), 1.5):
            db, _ = self.database(ids, {"products/PIN2": {"price": price}})
            with self.assertRaisesRegex(ValueError, "price"):
                read_sale(db, ids, "ORD-TEST")

    def test_invalid_ranges_rejected(self):
        for first, last in [("PIN2-0150", "PIN2-0084"), ("PIN2-0084", "PIN1-0150"), ("PIN2-0000", "PIN2-0010"), ("PIN2-0001", "PIN2-0401"), ("PIN2-084", "PIN2-0150")]:
            with self.assertRaises(ValueError):
                expand_range(first, last)


if __name__ == "__main__":
    unittest.main()
