#!/usr/bin/env python3
"""Reset the demo sheet to the baseline ERP extract (25 orders) so you can run it again.

What it does, in order:
    1. Reads the target sheet and shows how many rows it has.
    2. Asks you to type the sheet's name to confirm (skip with --yes).
    3. Deletes EVERY row on the sheet, including anything the team typed into
       Delivery Owner / Delivery Notes.
    4. Empties the generated CSV exports in the BI folder.
    5. Reloads erp_orders_baseline.csv by calling erp_sync.py --initial-load.

Only use this on a demo or test sheet. Deleted rows cannot be recovered by this script.

Setup is the same as erp_sync.py: put SMARTSHEET_ACCESS_TOKEN and SHEET_ID in .env
(see .env.example) or export them in your shell.

Usage:
    python reset_demo.py          # asks for confirmation
    python reset_demo.py --yes    # no prompt (for scripted resets)
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

import smartsheet

import demo_env

demo_env.load()   # read SMARTSHEET_ACCESS_TOKEN / SHEET_ID from .env if present

# Use the operating system's certificate store (needed behind TLS-inspecting proxies).
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

HERE = Path(__file__).parent

# delete_rows takes the row IDs in the URL query string, so keep each request short
# enough not to exceed URL length limits.
BATCH = 200


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sheet-id", type=int, default=os.getenv("SHEET_ID"),
                    help="target sheet (defaults to the SHEET_ID environment variable)")
    ap.add_argument("--baseline", type=Path, default=HERE / "erp_orders_baseline.csv",
                    help="extract to reload after the wipe")
    ap.add_argument("--export-dir", type=Path, default=HERE / "bi_folder",
                    help="generated exports in this folder are removed (default: ./bi_folder)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    if not args.sheet_id:
        sys.exit("Set SHEET_ID or pass --sheet-id")
    if not os.getenv("SMARTSHEET_ACCESS_TOKEN"):
        sys.exit("Set SMARTSHEET_ACCESS_TOKEN")

    # errors_as_exceptions: a failed API call raises instead of failing silently.
    client = smartsheet.Smartsheet()
    client.errors_as_exceptions(True)

    # Step 1: look at the sheet before touching it.
    sheet = client.Sheets.get_sheet(args.sheet_id)
    ids = [r.id for r in sheet.rows]
    print(f"Sheet '{sheet.name}' has {len(ids)} rows.")

    # Step 2: make the user type the sheet name. Typing the name (not just "y") makes it
    # hard to wipe the wrong sheet by habit.
    if not args.yes:
        if input(f"Delete all {len(ids)} rows and reload the baseline? Type the sheet name to confirm: ") != sheet.name:
            sys.exit("Aborted.")

    # Step 3: delete every row, in batches.
    for i in range(0, len(ids), BATCH):
        client.Sheets.delete_rows(args.sheet_id, ids[i:i + BATCH])
    print(f"Deleted {len(ids)} rows.")

    # Step 4: remove generated exports so the BI folder starts empty for the next take.
    if args.export_dir.is_dir():
        for f in args.export_dir.glob("orders_*.csv"):
            f.unlink()
        print(f"Cleared exports in {args.export_dir}")

    # Step 5: reload the baseline using the normal sync script. --initial-load skips
    # highlighting and the BI export, because this is just restoring the starting state.
    return subprocess.call([sys.executable, str(HERE / "erp_sync.py"), str(args.baseline),
                            "--sheet-id", str(args.sheet_id), "--initial-load"])


if __name__ == "__main__":
    sys.exit(main())
