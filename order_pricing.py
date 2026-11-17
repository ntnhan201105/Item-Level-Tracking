"""Python equivalent of the named-combo pricing in index.html."""

COMBOS = [
    ("COMBO1", "Accessories", 90000, {"DC": 1, "KEYR": 1, "PIN": 1, "STICK": 1}),
    ("COMBO2", "Mini Gift", 129000, {"KHAN": 1, "DC": 1, "PIN": 1}),
    ("COMBO3", "Classic", 250000, {"THUN": 1, "KHAN": 1, "PIN": 1}),
    ("COMBO4", "Basic", 290000, {"THUN": 1, "KHAN": 1, "DC": 1, "PIN": 1}),
    ("COMBO5", "Couple", 330000, {"THUN": 1, "BABYT": 1, "PIN": 2}),
    ("COMBO6", "Double", 570000, {"THUN": 2, "KHAN": 2, "DC": 2, "PIN": 2}),
    ("COMBO7", "Group", 1440000, {"THUN": 5, "KHAN": 5, "DC": 5, "PIN": 5}),
]


def group(sku):
    sku = str(sku or "").strip().upper()
    return {"DC1": "DC", "DC2": "DC", "PIN1": "PIN", "PIN2": "PIN", "STICKER": "STICK"}.get(sku, sku)


def calculate_pricing(items):
    available = {}
    for item in sorted(items, key=lambda x: x["itemId"]):
        available.setdefault(group(item.get("sku")), []).append(item)
    combos = []
    for combo_id, name, price, needs in reversed(COMBOS):
        while all(len(available.get(g, [])) >= n for g, n in needs.items()):
            selected = [item for g, n in needs.items() for item in available[g][:n]]
            retail = sum(item.get("price", 0) or 0 for item in selected)
            if retail < price:
                break
            for g, n in needs.items():
                del available[g][:n]
            combos.append({"comboId": combo_id, "name": name, "price": price,
                           "retailAmount": retail, "discountAmount": retail - price,
                           "itemIds": [item["itemId"] for item in selected]})
    subtotal = sum(item.get("price", 0) or 0 for item in items)
    discount = sum(combo["discountAmount"] for combo in combos)
    return {"subtotal": subtotal, "discountAmount": discount,
            "totalAmount": subtotal - discount, "appliedCombos": combos}
