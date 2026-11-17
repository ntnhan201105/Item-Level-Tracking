"""Offline restock selection, order reconciliation, and retry checks."""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from order_pricing import calculate_pricing
from restock_sold_range import queue_restock, read_plan
from sell_item_range import expand_range


class RestockTests(unittest.TestCase):
    def database(self, data):
        db = Mock()
        events = []

        def document(collection, doc_id):
            path = f"{collection}/{doc_id}"

            def get(transaction=None):
                self.assertFalse(any(event == "write" for event in events))
                events.append("read")
                return SimpleNamespace(exists=path in data, to_dict=lambda: data.get(path))

            return SimpleNamespace(id=doc_id, path=path, get=get)

        db.collection.side_effect = lambda name: SimpleNamespace(document=lambda doc_id: document(name, doc_id))
        return db, events

    def test_first_67_sold_skipping_stock_and_missing(self):
        ids = expand_range("PIN1-0001", "PIN1-0100")
        data = {f"items/{key}": {"sku": "PIN1", "status": "SOLD"} for key in ids[2:]}
        data["items/PIN1-0001"] = {"sku": "PIN1", "status": "IN_STOCK"}
        db, _ = self.database(data)
        plan = read_plan(db, ids, 67, "operation")
        self.assertEqual([candidate for _, candidate, _ in plan[1]], ids[2:69])
        self.assertEqual(plan[2], [])
        db.transaction.assert_not_called()

    def test_insufficient_sold_no_writes(self):
        db, _ = self.database({"items/PIN1-0001": {"sku": "PIN1", "status": "SOLD"}})
        with self.assertRaisesRegex(ValueError, "only 1 SOLD"):
            read_plan(db, expand_range("PIN1-0001", "PIN1-0100"), 67, "operation")

    def test_reconcile_shared_order_preserve_history_and_all_reads_before_writes(self):
        data = {
            "items/PIN1-0001": {"sku": "PIN1", "status": "SOLD", "orderId": "order", "soldAt": "old-time", "soldBy": "seller"},
            "items/PIN1-0002": {"sku": "PIN1", "status": "SOLD", "orderId": "order"},
            "orders/order": {"subtotal": 60000, "items": [
                {"itemId": "PIN1-0001", "sku": "PIN1", "price": 20000},
                {"itemId": "PIN1-0002", "sku": "PIN1", "price": 20000},
                {"itemId": "PIN2-0001", "sku": "PIN2", "price": 20000},
            ]},
        }
        db, events = self.database(data)
        tx = Mock()
        tx.update.side_effect = lambda *args: events.append("write")
        tx.set.side_effect = lambda *args: events.append("write")
        plan = read_plan(db, expand_range("PIN1-0001", "PIN1-0002"), 2, "operation", tx)
        changes = plan[2][0][1]
        self.assertEqual(changes["activeItems"], 1)
        self.assertEqual(changes["activeTotalAmount"], 20000)
        self.assertEqual(changes["status"], "PARTIALLY_VOIDED")
        queue_restock(tx, plan, "actor", "correction", "now")
        self.assertEqual(tx.update.call_count, 3)
        first = tx.update.call_args_list[0].args[1]
        self.assertEqual(first["status"], "IN_STOCK")
        self.assertIsNone(first["orderId"])
        self.assertEqual(first["lastSoldAt"], "old-time")
        self.assertEqual(first["lastOrderId"], "order")
        order_update = tx.update.call_args_list[-1].args[1]
        self.assertNotIn("items", order_update)
        self.assertNotIn("totalAmount", order_update)
        self.assertEqual(tx.set.call_args.args[1]["count"], 2)

    def test_full_return_with_existing_voided_items(self):
        data = {
            "items/PIN1-0001": {"sku": "PIN1", "status": "SOLD", "orderId": "order"},
            "orders/order": {"voidedItemIds": ["PIN1-0002"], "items": [
                {"itemId": "PIN1-0001", "price": 20000}, {"itemId": "PIN1-0002", "price": 20000}]},
        }
        db, _ = self.database(data)
        changes = read_plan(db, ["PIN1-0001"], 1, "operation")[2][0][1]
        self.assertEqual(changes["status"], "VOIDED")
        self.assertEqual(changes["activeTotalAmount"], 0)
        self.assertEqual(len(changes["voidedItemIds"]), 2)

    def test_retry_receipt_and_missing_order_abort(self):
        db, _ = self.database({"restock_operations/operation": {"count": 67}})
        with self.assertRaisesRegex(ValueError, "already completed"):
            read_plan(db, ["PIN1-0001"], 1, "operation")
        db, _ = self.database({"items/PIN1-0001": {"sku": "PIN1", "status": "SOLD", "orderId": "missing"}})
        with self.assertRaisesRegex(ValueError, "missing"):
            read_plan(db, ["PIN1-0001"], 1, "operation")

    def test_combo_removed_after_pin_return(self):
        lines = [{"itemId": "DC1-0001", "sku": "DC1", "price": 50000},
                 {"itemId": "PIN1-0001", "sku": "PIN1", "price": 20000},
                 {"itemId": "KHAN-0001", "sku": "KHAN", "price": 100000}]
        self.assertEqual(calculate_pricing(lines)["totalAmount"], 129000)
        data = {"items/PIN1-0001": {"sku": "PIN1", "status": "SOLD", "orderId": "order"},
                "orders/order": {"subtotal": 170000, "items": lines}}
        db, _ = self.database(data)
        changes = read_plan(db, ["PIN1-0001"], 1, "operation")[2][0][1]
        self.assertEqual(changes["activeTotalAmount"], 150000)
        self.assertEqual(changes["activeCombos"], [])

    def test_combo_priority_and_retail_cap(self):
        lines = [{"itemId": f"{sku}-{n}", "sku": sku, "price": price}
                 for sku, price in [("THUN", 160000), ("KHAN", 100000), ("DC2", 50000), ("PIN2", 20000)]
                 for n in range(5)]
        self.assertEqual(calculate_pricing(lines)["appliedCombos"][0]["comboId"], "COMBO7")
        cheap = [{**line, "price": 1} for line in lines]
        self.assertEqual(calculate_pricing(cheap)["appliedCombos"], [])


if __name__ == "__main__":
    unittest.main()
