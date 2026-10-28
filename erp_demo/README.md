# Mock ERP file to live Smartsheet sheet

Sample code from the session **From One to Many: Solving Scale with the Smartsheet API**
(Scenario 1: keep Smartsheet in sync with ERP, HR and BI without exports).

A Python script reads an ERP extract (CSV), matches each order to a sheet row on **Order ID**,
updates the rows that changed, adds the new ones, and exports the sheet as a CSV for BI.
Run it hourly and the sheet is always current, with no manual export, clean-up or paste.

```
ERP extract -> match on Order ID -> update changed rows / add new rows -> Smartsheet sheet -> CSV export for BI
```

Endpoints used: **Get Sheet**, **Update Rows**, **Add Rows** (plus Get Sheet as CSV for the export).

## Files

| File | Purpose |
|---|---|
| `erp_sync.py` | The sync script. Start here; it is commented step by step. |
| `reset_demo.py` | Wipes the demo sheet and reloads the baseline, so you can repeat the demo. |
| `erp_orders_baseline.csv` | Mock ERP extract: 25 orders (the sheet's starting state). |
| `erp_orders_update.csv` | Mock "latest" extract: 6 changed orders, 5 new, 19 unchanged. |
| `gen_mock_data.py` | Regenerates the two CSVs. |
| `requirements.txt` | Python dependencies. |

## 1. Create the target sheet

Create a sheet with these columns (titles must match exactly):

| Column | Type | Owned by |
|---|---|---|
| Order ID (primary) | Text/Number | ERP |
| Customer | Text/Number | ERP |
| Product | Text/Number | ERP |
| Quantity | Text/Number | ERP |
| Order Value | Text/Number | ERP |
| Order Date | Date | ERP |
| Requested Delivery | Date | ERP |
| ERP Status | Dropdown: Open, Picking, Backordered, Shipped, Delivered | ERP |
| Carrier | Text/Number | ERP |
| Tracking Number | Text/Number | ERP |
| Last ERP Sync | Text/Number | script |
| Last ERP Change | Text/Number | script |
| Delivery Owner | Contact List | **your team** |
| Delivery Notes | Text/Number | **your team** |

The script only ever writes the ERP columns and the two script columns. Delivery Owner and
Delivery Notes are never touched, which is the point: the ERP stays the source of truth for
order data, and your team keeps working in Smartsheet on top of it.

Use a test sheet, not a production one. Find the sheet ID in Sheet > File > Properties.

## 2. Set up

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env     # then edit .env and fill in the two values
```

```
SMARTSHEET_ACCESS_TOKEN=<your API token>   # Profile > Personal Settings > API Access
SHEET_ID=<your sheet ID>
```

The scripts read `.env` automatically. Exporting the same names in your shell also works and takes
priority. Treat the token like a password: `.env` is git-ignored; never commit or share it.

## 3. Run it

```bash
# Load the starting state (25 orders) into the empty sheet
.venv/bin/python erp_sync.py erp_orders_baseline.csv --initial-load

# See what the new extract would change (writes nothing)
.venv/bin/python erp_sync.py erp_orders_update.csv --dry-run

# Apply it: changed cells turn yellow, new rows green, a CSV lands in ./bi_folder
.venv/bin/python erp_sync.py erp_orders_update.csv

# Run it again: 0 updates, 0 adds, and the highlights clear. Safe to schedule hourly.
.venv/bin/python erp_sync.py erp_orders_update.csv
```

To start over: `.venv/bin/python reset_demo.py` (deletes every row on the sheet after you
confirm by typing its name).

Options: `--dry-run`, `--no-highlight` (use this for a scheduled job), `--no-export`,
`--export-dir DIR`, `--sheet-id ID`.

## Making it your own

* **Your source system:** replace `read_erp()` in `erp_sync.py` with a call to your ERP's API or
  database. Everything after it stays the same.
* **Your columns:** edit `KEY`, `ERP_COLUMNS`, `NUMERIC` and `DATES` at the top of `erp_sync.py`.
* **Scheduling:** run it from cron, a CI job or a cloud scheduler, for example hourly:
  `0 * * * * cd /path/to/this/folder && .venv/bin/python erp_sync.py latest_extract.csv --no-highlight`
  with the two environment variables set for that job.

## Troubleshooting

* **`CERTIFICATE_VERIFY_FAILED`:** your network inspects TLS. Install `truststore` (it is in
  `requirements.txt`); the scripts use it automatically to trust your OS certificates.
* **`Sheet is missing columns`:** a column title doesn't match the list above exactly.
* **An `ApiError` with a code and message:** the API rejected a request. The message names the
  cause; check the column types and that the token's user can edit the sheet.
* **Rate limits:** the API allows 300 requests per minute per token. This script makes
  only a handful of calls per run (one read, one or two writes, one export), however many rows change.

API reference: https://developers.smartsheet.com/api/smartsheet
