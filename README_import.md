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

