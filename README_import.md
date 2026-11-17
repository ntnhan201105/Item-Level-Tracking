FIRESTORE ITEM IMPORTER
=======================

Files
-----
- stock.xlsx           : stock-entry template
- import_items.py      : bulk importer
- requirements.txt     : Python dependencies

1) Prepare stock.xlsx
---------------------
Replace/delete the sample rows on the "Stock" sheet.

Columns:
SKU            Unique product type, e.g. CAP-BLK
PRODUCT_NAME   Display name
PRICE_VND      Unit price in VND
QUANTITY       Number of physical serialized items to create
ID_PREFIX      Optional. Blank means use SKU.

Example:
CAP-BLK | Mũ đen | 150000 | 1000 | [blank]

The importer creates:
CAP-BLK-000001
CAP-BLK-000002
...
CAP-BLK-001000

For later imports, it continues the sequence automatically using Firestore
collection "counters". You do NOT need to calculate START_NUMBER manually.

2) Install dependencies
-----------------------
Windows PowerShell / Terminal:

    pip install -r requirements.txt

3) Get server credentials
-------------------------
Use either:

A. A local service-account JSON file:
   Firebase / Google Cloud -> Service accounts -> create/download a key.
   Keep it PRIVATE and NEVER upload it to GitHub.

or

B. Google Application Default Credentials if already configured.

4) Dry run first
----------------
With a service-account file:

    python import_items.py stock.xlsx --service-account serviceAccountKey.json

No Firestore writes happen. A generated_items_*.csv preview is created.
The preview reads counters/{ID_PREFIX} from Firebase and starts at its
nextNumber (or 1 if the counter does not exist). Valid Firebase credentials
and read access are required, and these counter reads use Firestore read quota.
Preview IDs are not reserved: another import can change the counter before
you commit. Use the successful real import's CSV for final QR labels.

5) Real import
--------------

    python import_items.py stock.xlsx --service-account serviceAccountKey.json --commit

The script will:
- upsert products/{SKU}
- reserve a safe sequence range in counters/{ID_PREFIX}
- create items/{ITEM_ID}
- mark every new item IN_STOCK
- output generated_items_*.csv containing ITEM_ID + QR_CONTENT

6) QR labels
------------
QR_CONTENT equals ITEM_ID by default, e.g. CAP-BLK-000123.
Use the generated CSV as input for whatever QR-label printing workflow you choose.

Security
--------
- firebaseConfig in the public web app is okay.
- serviceAccountKey.json is NOT okay to publish.
- Add serviceAccountKey.json to .gitignore if you ever use Git locally.

Recommended .gitignore lines:

    serviceAccountKey.json
    *service-account*.json
    generated_items_*.csv

Notes
-----
- The importer is dry-run by default for safety.
- If an import fails after counters have been reserved, gaps in ITEM_ID numbering
  are harmless. Do not decrement the Firestore counter to reuse IDs.
- A few thousand items is fine for this workflow. The script commits writes in
  batches rather than one document at a time.

Manual sale of an item range
---------------------------
Preview one sale order for an inclusive range (maximum 400 items, one SKU):

    python sell_item_range.py PIN2-0084 PIN2-0150 --payment-method CASH --service-account serviceAccountKey.json

Use TRANSFER instead of CASH for a bank-transfer payment. Add --commit to
create the order and mark all selected items SOLD in a single transaction.
The script stops if an item is missing, is not IN_STOCK, or has an orderId.
It uses the current product retail price; the app's multi-SKU combos cannot
apply to a single-SKU range. Commit rechecks availability and pricing.
The stable order ID prevents creating the same range order twice.
Sale timestamps are the execution time. The seller defaults to
manual-range-script; use --seller-uid and --seller-email to record a real seller.

Restock the first N sold items in a range
---------------------------------------
Preview the first 67 SOLD items by item number from PIN1-0001 to PIN1-0100:

    python restock_sold_range.py PIN1-0001 PIN1-0100 --count 67 --service-account serviceAccountKey.json

Add --commit to return the selected items to IN_STOCK and update their orders
atomically. Missing items and non-SOLD items are skipped. If fewer than 67
SOLD items exist, nothing changes. A missing linked order or inconsistent
order lines also stops the operation. Orders retain original sale history;
their active totals, combos, and voided item lists are updated like app restock.
The script preserves last-sale fields on the items and writes a receipt under
restock_operations to prevent accidental repeats of the same range/count.
Use --operation-id with a new name only for a deliberately separate restock.
The commit rechecks selection; IDs can differ from a separate earlier dry run.
Optional --actor and --reason identify who made the correction and why.

