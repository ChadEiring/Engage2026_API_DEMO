#!/usr/bin/env python3
"""Mock ERP extract -> live Smartsheet sheet (upsert on Order ID), then a CSV export for BI.

This is the script behind "Scenario 1: keep Smartsheet in sync with ERP, HR and BI
without exports". The pattern is: read, decide, write.

    ERP extract (CSV)
        -> match each record to a sheet row on a shared ID (Order ID)
        -> UPDATE rows that changed, ADD rows that are new, leave the rest alone
        -> Smartsheet sheet (live)
        -> CSV export of the sheet, dropped in a folder for BI tools

Smartsheet API endpoints used (via the Smartsheet Python SDK):
    GET  /sheets/{id}              Sheets.get_sheet()        read columns + rows
    PUT  /sheets/{id}/rows         Sheets.update_rows()      change existing rows
    POST /sheets/{id}/rows         Sheets.add_rows()         append new rows
    GET  /sheets/{id} (text/csv)   Sheets.get_sheet_as_csv() export for BI

Design points worth copying into your own integrations:
    * Idempotent: running it twice with the same file changes nothing the second time,
      so it is safe to schedule hourly.
    * Minimal writes: only cells whose value actually changed are sent. Cells the team
      edits in Smartsheet (Delivery Owner, Delivery Notes) are never touched.
    * Batched: Smartsheet accepts up to 500 rows per write call (see BATCH).
    * Dry run first: --dry-run prints the plan and writes nothing.

Setup:
    pip install -r requirements.txt
    cp .env.example .env                 # then fill in the two values:
        SMARTSHEET_ACCESS_TOKEN=...      #   Profile > Personal Settings > API Access
        SHEET_ID=...                     #   numeric sheet ID of the target sheet
    (Or export them as environment variables instead; those take priority over .env.)

Usage:
    python erp_sync.py erp_orders_update.csv --dry-run   # show the plan only
    python erp_sync.py erp_orders_update.csv             # apply it + write BI export

Treat the API token like a password: keep it in .env (git-ignored) or a secrets manager,
never in the script itself or in source control.
"""
import argparse
import csv
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import smartsheet

import demo_env

demo_env.load()   # read SMARTSHEET_ACCESS_TOKEN / SHEET_ID from .env if present

# Optional: use the operating system's certificate store for HTTPS. This is needed on
# networks that inspect TLS (corporate proxies / VPNs), where Python's bundled
# certificates fail with "CERTIFICATE_VERIFY_FAILED". Harmless if not installed.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Sheet layout. These column titles must match the target sheet exactly
# (column titles are case-sensitive). Change them to adapt this to your own data.
# ---------------------------------------------------------------------------

# The column shared by the ERP extract and the sheet. It must be unique per record;
# it is how we decide "same order" (update) versus "new order" (add).
KEY = "Order ID"

# Columns the ERP owns. If the ERP value differs from the sheet, the ERP wins and the
# cell is overwritten. Any other column on the sheet (e.g. Delivery Owner, Delivery
# Notes) belongs to the team working in Smartsheet and is never touched.
ERP_COLUMNS = ["Customer", "Product", "Quantity", "Order Value", "Order Date",
               "Requested Delivery", "ERP Status", "Carrier", "Tracking Number"]

# ERP columns that should be written to Smartsheet as numbers, not text.
NUMERIC = {"Quantity", "Order Value"}

# ERP columns holding dates. Smartsheet DATE columns take ISO text (YYYY-MM-DD).
DATES = {"Order Date", "Requested Delivery"}

# Two columns the script itself maintains, as an audit trail on each changed row.
SYNC_STAMP = "Last ERP Sync"      # when the row was last changed by this script
SYNC_NOTE = "Last ERP Change"     # what changed, e.g. "ERP Status: Picking -> Shipped"

# Smartsheet accepts at most 500 rows per add/update call; larger loads are split up.
BATCH = 500

# ---------------------------------------------------------------------------
# Highlighting. Cell formatting travels in a "format descriptor": a comma-separated
# string with one slot per formatting option (font, bold, alignment, colors, ...).
# We only use the background-color slot. Colors are numbers, not hex values: they are
# positions in Smartsheet's color table, which you can read from
# https://api.smartsheet.com/2.0/serverinfo (no authentication needed).
# ---------------------------------------------------------------------------
HL_UPDATED = 13   # light yellow: a cell this run changed
HL_NEW = 14       # light green: a row this run added
BG_CLEAR = 3      # "transparent": how a highlight is removed (see fmt() for why not 0)
BG_POS = 9        # slot of the background color within the format descriptor
FORMAT_SLOTS = 17  # total number of slots in a descriptor

# ANSI terminal colors, so the output reads well on a screen recording.
G, Y, C, D, R = "\033[92m", "\033[93m", "\033[96m", "\033[2m", "\033[0m"


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

def norm(col: str, v) -> str:
    """Normalise a value so a CSV value and a sheet value compare as equal.

    The CSV gives us text ("10"); the API gives back typed values (10.0 for a number,
    "2026-11-05" or a longer ISO string for a date). Without normalising, every row
    would look "changed" on every run. Comparing normalised strings fixes that.
    """
    if v is None or v == "":
        return ""
    if col in NUMERIC:
        f = float(v)                              # "10", 10, 10.0 all become "10"
        return str(int(f)) if f == int(f) else str(f)
    s = str(v)
    return s[:10] if col in DATES else s.strip()  # keep just YYYY-MM-DD for dates


def typed(col: str, v: str):
    """Convert a normalised string into the value we send to the API.

    Numeric columns are sent as numbers so Smartsheet can sum and sort them; everything
    else (including dates, which Smartsheet parses from ISO text) goes as text.
    """
    if v == "":
        return ""
    return float(v) if col in NUMERIC else v


def read_erp(path: Path) -> list[dict]:
    """Read the ERP extract into a list of {column: normalised value} dicts.

    This is the stand-in for "call the source system". In a real integration this is
    where you would query your ERP's API or database instead of reading a file; the
    rest of the script would not change. utf-8-sig tolerates the BOM that Excel adds.
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [{k: norm(k, v) for k, v in row.items()} for row in csv.DictReader(f)]


# ---------------------------------------------------------------------------
# Cell / format helpers
# ---------------------------------------------------------------------------

def fmt(bg: int | None) -> str:
    """Build a format descriptor that sets only the background color.

    The API treats an empty slot, and also the value 0 ("none"), as "leave this setting
    as it is" - so neither can remove a color. bg=None therefore writes BG_CLEAR
    (transparent), which looks identical to no fill. (Found by testing, not in the docs.)
    """
    parts = [""] * FORMAT_SLOTS
    parts[BG_POS] = str(BG_CLEAR if bg is None else bg)
    return ",".join(parts)


def cell(col_id: int, value, bg: int | None = None) -> smartsheet.models.Cell:
    """Build an API cell: which column, what value, and optionally a background color."""
    c = smartsheet.models.Cell()
    c.column_id = col_id
    c.value = value
    if bg is not None:
        c.format = fmt(bg)
    return c


def clear_highlight(col_id: int, value) -> smartsheet.models.Cell:
    """Build a cell that removes a highlight while keeping the value as it is.

    The API rejects a cell update that has a format but no value (error 1012), so we
    resend the cell's current value together with the reset format.
    """
    c = cell(col_id, "" if value is None else value)
    c.format = fmt(None)
    return c


def is_highlighted(c) -> bool:
    """True if a cell read back from the sheet carries one of our highlight colors."""
    f = (c.format or "").split(",")
    return len(f) > BG_POS and f[BG_POS] in (str(HL_UPDATED), str(HL_NEW))


def chunks(seq, n):
    """Yield successive slices of at most n items (used to respect the 500-row limit)."""
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


# ---------------------------------------------------------------------------
# Main flow
# ---------------------------------------------------------------------------

def main() -> int:
    # --- Command line ------------------------------------------------------
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("erp_file", type=Path, help="ERP extract (CSV)")
    ap.add_argument("--sheet-id", type=int, default=os.getenv("SHEET_ID"),
                    help="target sheet (defaults to the SHEET_ID environment variable)")
    ap.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    ap.add_argument("--export-dir", type=Path, default=Path("bi_folder"),
                    help="where the CSV export for BI lands (default: ./bi_folder)")
    ap.add_argument("--no-export", action="store_true", help="skip the CSV export for BI")
    ap.add_argument("--no-highlight", action="store_true", help="do not highlight changed cells")
    ap.add_argument("--initial-load", action="store_true",
                    help="first load of an empty sheet: no highlights, no export")
    args = ap.parse_args()
    if args.initial_load:
        args.no_highlight = args.no_export = True
    # None means "no color"; cell() then leaves the cell's formatting alone.
    hl_new = None if args.no_highlight else HL_NEW
    hl_upd = None if args.no_highlight else HL_UPDATED

    if not args.sheet_id:
        sys.exit("Set SHEET_ID or pass --sheet-id")
    if not os.getenv("SMARTSHEET_ACCESS_TOKEN"):
        sys.exit("Set SMARTSHEET_ACCESS_TOKEN")

    # The SDK picks the token up from SMARTSHEET_ACCESS_TOKEN on its own.
    # errors_as_exceptions makes API errors raise instead of returning an Error object,
    # so a failed write can never be mistaken for success.
    client = smartsheet.Smartsheet()
    client.errors_as_exceptions(True)
    sheet_id = int(args.sheet_id)

    # --- Step 1: read the ERP extract --------------------------------------
    erp = read_erp(args.erp_file)
    print(f"{C}ERP extract:{R} {len(erp)} orders from {args.erp_file.name}")

    # --- Step 2: read the sheet and index its rows by Order ID -------------
    # include="format" makes the API return cell formatting, which we need to find
    # highlights left over from a previous run.
    sheet = client.Sheets.get_sheet(sheet_id, include="format")

    # Columns are addressed by numeric ID in the API, not by title, so build a
    # title -> ID map (and its reverse). Fail early if the sheet doesn't have the
    # columns this script expects.
    col = {c.title: c.id for c in sheet.columns}
    missing = [c for c in [KEY, *ERP_COLUMNS, SYNC_STAMP, SYNC_NOTE] if c not in col]
    if missing:
        sys.exit(f"Sheet is missing columns: {missing}")
    id_to_title = {v: k for k, v in col.items()}

    existing = {}   # Order ID -> (row id, {column title: normalised value})
    stale = {}      # row id -> [(column id, value)] still highlighted from a previous run
    for row in sheet.rows:
        hot = [(c.column_id, c.value) for c in row.cells if is_highlighted(c)]
        if hot:
            stale[row.id] = hot
        vals = {id_to_title[c.column_id]: norm(id_to_title[c.column_id], c.value) for c in row.cells}
        if vals.get(KEY):                      # ignore blank rows
            existing[vals[KEY]] = (row.id, vals)
    print(f"{C}Sheet:{R} '{sheet.name}' has {len(existing)} rows")

    # --- Step 3: compare ERP vs sheet and decide what to write --------------
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    to_update, to_add, unchanged = [], [], 0
    for rec in erp:
        oid = rec[KEY]

        # New order: not on the sheet yet -> append a complete row at the bottom.
        if oid not in existing:
            row = smartsheet.models.Row()
            row.to_bottom = True
            row.cells.append(cell(col[KEY], oid, hl_new))
            for name in ERP_COLUMNS:
                row.cells.append(cell(col[name], typed(name, rec[name]), hl_new))
            row.cells.append(cell(col[SYNC_STAMP], stamp))
            row.cells.append(cell(col[SYNC_NOTE], "Initial load" if args.initial_load else "New order from ERP"))
            to_add.append((oid, row))
            continue

        # Known order: find which ERP-owned cells differ from what the sheet holds.
        row_id, current = existing[oid]
        diffs = [n for n in ERP_COLUMNS if rec[n] != current.get(n, "")]
        if not diffs:
            unchanged += 1               # nothing to do: the row costs us no API write
            continue

        # Changed order: build an update that carries ONLY the changed cells.
        # Sending nothing for the other columns is what protects the team's own
        # columns (Delivery Owner, Delivery Notes) from being overwritten.
        row = smartsheet.models.Row()
        row.id = row_id
        for name in diffs:
            row.cells.append(cell(col[name], typed(name, rec[name]), hl_upd))

        # Remove highlights left from the previous run on this row, except on cells
        # we are about to re-highlight now.
        fresh = {col[n] for n in diffs} | {col[SYNC_NOTE]}
        for col_id, value in stale.pop(row_id, []):
            if col_id not in fresh:
                row.cells.append(clear_highlight(col_id, value))

        # Audit trail: human-readable "old -> new" for every changed field.
        note = "; ".join(f"{n}: {current.get(n) or '-'} -> {rec[n] or '-'}" for n in diffs)
        row.cells.append(cell(col[SYNC_STAMP], stamp))
        row.cells.append(cell(col[SYNC_NOTE], note[:250], hl_upd))
        to_update.append((oid, row, note))

    # Rows whose ERP data did not change this run but still show last run's
    # highlights: they only need the color removed.
    to_clear = []
    for row_id, col_ids in stale.items():
        row = smartsheet.models.Row()
        row.id = row_id
        for col_id, value in col_ids:
            row.cells.append(clear_highlight(col_id, value))
        to_clear.append(row)

    # Print the plan. This is also everything --dry-run does.
    for oid, _, note in to_update:
        print(f"  {Y}UPDATE{R} {oid}  {D}{note}{R}")
    for oid, _ in to_add:
        print(f"  {G}ADD   {R} {oid}")
    print(f"\n{Y}{len(to_update)} to update{R}, {G}{len(to_add)} to add{R}, {D}{unchanged} unchanged{R}")
    if to_clear:
        print(f"{D}{len(to_clear)} rows still carry last run's highlights; they will be cleared{R}")

    if args.dry_run:
        print(f"{C}Dry run - nothing written.{R}")
        return 0

    # --- Step 4: write to Smartsheet ----------------------------------------
    # Updates and highlight-clearing both go through update_rows; new rows through
    # add_rows. Each call is capped at BATCH rows. If a call fails the SDK raises, and
    # because we use exceptions the script stops rather than reporting false success.
    for batch in chunks([r for _, r, _ in to_update] + to_clear, BATCH):
        client.Sheets.update_rows(sheet_id, batch)
    for batch in chunks([r for _, r in to_add], BATCH):
        client.Sheets.add_rows(sheet_id, batch)
    print(f"{G}Sheet is live: {len(to_update)} rows updated, {len(to_add)} rows added.{R}")

    # --- Step 5: export the sheet back out for BI ---------------------------
    # Smartsheet renders the sheet as CSV for us. We keep a timestamped copy as history
    # and refresh orders_latest.csv, a stable filename a BI tool can point at.
    if not args.no_export:
        args.export_dir.mkdir(parents=True, exist_ok=True)
        name = f"orders_{datetime.now():%Y%m%d_%H%M%S}.csv"
        client.Sheets.get_sheet_as_csv(sheet_id, str(args.export_dir), alternate_file_name=name)
        shutil.copyfile(args.export_dir / name, args.export_dir / "orders_latest.csv")
        print(f"{G}BI export:{R} {args.export_dir / name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
