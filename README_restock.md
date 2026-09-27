FIRESTORE SYNC PATCH FOR RESTOCK / VOIDED SALES
================================================

The updated POS now writes:
- items.updatedAt on checkout/restock
- orders.updatedAt on checkout/restock
- orders.voidedItemIds when an item is restored to stock

To keep Google Sheets correct after restocking, make these 3 changes
in your existing FirestoreSync.gs.

1) Query changed orders by updatedAt, NOT createdAt
---------------------------------------------------
Find:

  const orders =
    fsQuerySince(
      "orders",
      "createdAt",
      orderSince
    );

Replace with:

  const orders =
    fsQuerySince(
      "orders",
      "updatedAt",
      orderSince
    );

Also keep ORDERS_LAST_SYNC as the cursor name; only the Firestore field changes.


2) Remove old SALES rows for every changed order before re-inserting active rows
---------------------------------------------------------------------------------
Add this function:

function deleteSalesRowsForOrders(orderIds) {
  if (!orderIds || !orderIds.length) return;

  const sheet =
    SpreadsheetApp
      .getActive()
      .getSheetByName(TAB_SALES);

  if (!sheet || sheet.getLastRow() < 2) return;

  const wanted =
    new Set(
      orderIds.map(String)
    );

  const values =
    sheet
      .getRange(
        2,
        1,
        sheet.getLastRow() - 1,
        7
      )
      .getValues();

  const kept =
    values.filter(
      row => !wanted.has(String(row[0]))
    );

  sheet
    .getRange(
      2,
      1,
      Math.max(sheet.getLastRow() - 1, 1),
      7
    )
    .clearContent();

  if (kept.length) {
    sheet
      .getRange(
        2,
        1,
        kept.length,
        7
      )
      .setValues(kept);
  }
}

Then replace upsertSales(orders) with:

function upsertSales(orders) {
  const products =
    readProductsFromSheet();

  const items =
    readItemsFromSheet();

  // Important: an order may now have a voided/restocked item.
  // Remove its old rows first, then rebuild only active item rows.
  deleteSalesRowsForOrders(
    orders.map(order => order.id)
  );

  const rows =
    flattenOrdersToSales(
      orders,
      products,
      items,
      true
    );

  if (rows.length) {
    upsertRows(
      TAB_SALES,
      rows,
      row =>
        String(row[0])
        + "|"
        + String(row[1])
    );
  }
}


3) Skip voidedItemIds when flattening an order into SALES
---------------------------------------------------------
Inside flattenOrdersToSales(), after:

  const orderId =
    orderDoc.id;

add:

  const voidedItemIds =
    new Set(
      (order.voidedItemIds || [])
        .map(
          id =>
            String(id)
              .trim()
              .toUpperCase()
        )
    );

Then, inside orderItems.forEach(rawItem => { ... }),
AFTER itemId has been resolved, add:

  itemId =
    String(itemId || "")
      .trim()
      .toUpperCase();

  if (
    !itemId ||
    voidedItemIds.has(itemId)
  ) {
    return;
  }

That means:
- normal sold item => appears in SALES
- restored/voided item => removed from SALES
- multi-item order with 1 restored item => only the remaining sold items stay in SALES
- fully voided order => no SALES rows for that order


After applying this patch:
--------------------------
1. Push the new index.html to GitHub Pages.
2. Use the updated importer for all future item imports.
3. Run initialFullSync() ONCE so old test data gets aligned.
4. Then keep the 5-minute incremental sync trigger enabled.

Note:
The POS keeps the original order and audit metadata in Firestore even when an item
is restored. Google Sheets SALES represents currently valid sales, not the audit log.
