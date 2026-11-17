import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from undo_restock import queue_restore, read_plan


class UndoTests(unittest.TestCase):
    def database(self, changes=None):
        manifest = {"sourceOperation": "source", "items": {"PIN1-0019": "order"}}
        data = {
            "restock_operations/source": {"itemIds": ["PIN1-0001", "PIN1-0019"], "createdAt": "restock-time"},
            "items/PIN1-0019": {"status": "IN_STOCK", "lastOrderId": "order", "lastSoldAt": "sale-time",
                                "lastSoldBy": "seller", "lastSoldDeviceId": "device", "restockedAt": "restock-time"},
            "orders/order": {"subtotal": 40000, "voidedItemIds": ["PIN1-0001", "PIN1-0019"],
                             "items": [{"itemId": "PIN1-0001", "sku": "PIN1", "price": 20000},
                                       {"itemId": "PIN1-0019", "sku": "PIN1", "price": 20000}]},
        }
        data.update(changes or {})
        writes = []
        tx = Mock()
        tx.update.side_effect = lambda *args: writes.append(args)
        tx.set.side_effect = lambda *args: writes.append(args)
        db = Mock()

        def ref(collection, key):
            path = f"{collection}/{key}"
            def get(transaction=None):
                self.assertFalse(writes, "All reads must precede writes")
                return SimpleNamespace(exists=path in data, to_dict=lambda: data.get(path))
            return SimpleNamespace(id=key, path=path, get=get)

        db.collection.side_effect = lambda name: SimpleNamespace(document=lambda key: ref(name, key))
        return db, tx, manifest

    def test_restore_original_fields_and_keep_other_voids(self):
        db, tx, manifest = self.database()
        plan = read_plan(db, manifest, tx)
        self.assertEqual(plan[2][0][2]["voidedItemIds"], ["PIN1-0001"])
        self.assertEqual(plan[2][0][2]["activeTotalAmount"], 20000)
        self.assertEqual(plan[2][0][2]["status"], "PARTIALLY_VOIDED")
        queue_restore(tx, plan, manifest, "now")
        item = tx.update.call_args_list[0].args[1]
        self.assertEqual(item["soldAt"], "sale-time")
        self.assertEqual(item["soldBy"], "seller")
        self.assertEqual(item["orderId"], "order")
        self.assertEqual(tx.update.call_count, 2)
        self.assertNotIn("totalAmount", tx.update.call_args_list[-1].args[1])
        self.assertNotIn("items", tx.update.call_args_list[-1].args[1])

    def test_changed_or_resold_items_rejected(self):
        for field, value in [("status", "SOLD"), ("lastOrderId", "other"), ("restockedAt", "later"), ("lastSoldAt", None)]:
            db, tx, manifest = self.database()
            # Read the original item, then replace that document in a second mock DB.
            item = db.collection("items").document("PIN1-0019").get().to_dict().copy()
            item[field] = value
            db, tx, manifest = self.database({"items/PIN1-0019": item})
            with self.assertRaises(ValueError):
                read_plan(db, manifest, tx)
            tx.update.assert_not_called()

    def test_manifest_is_exact_last_fifty(self):
        manifest = json.loads(Path("restore_pin1_last50.json").read_text())
        expected = [f"PIN1-{n:04d}" for n in range(19, 71) if n not in (22, 65)]
        self.assertEqual(list(manifest["items"]), expected)
        self.assertEqual(len(expected), 50)

    def test_receipt_prevents_retry(self):
        db, tx, manifest = self.database()
        receipt_id = read_plan(db, manifest)[0].id
        db, tx, manifest = self.database({f"restock_corrections/{receipt_id}": {"count": 1}})
        with self.assertRaisesRegex(ValueError, "already completed"):
            read_plan(db, manifest, tx)
        tx.update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
