#!/usr/bin/env python3
"""Put Demo 2 back to its starting state so you can record another take.

What it restores:
  1. Clears the Seat Governance Log sheet.
  2. Sets every row on the Leavers (HR Feed) sheet back to Status = Pending.
  3. Reactivates allowlisted users the sweep deactivated.
  4. Gives DEMO_SEAT_USER_EMAIL a MEMBER seat again (the user whose downgrade you demo).
  5. With --reinvite-leavers: re-invites leavers who were removed, so you can remove them again.
     A removed user is deleted for good; inviting them again creates a fresh, empty account.

It never touches the Seat Approvals sheet (that is your policy, not demo state) and, like
the other scripts, only acts on allowlisted users.

Run:
    python reset_demo2.py                       # asks for confirmation
    python reset_demo2.py --yes --reinvite-leavers
"""
import argparse
import sys

import smartsheet

import seat_common as sc
import os


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    ap.add_argument("--reinvite-leavers", action="store_true", help="re-invite removed leavers")
    args = ap.parse_args()

    settings = sc.load_settings()
    client = sc.get_client()

    if not args.yes and input("Reset Demo 2 (clears the log, resets leavers, restores seats)? Type 'reset': ") != "reset":
        sys.exit("Aborted.")

    # 1. Clear the log: delete every row.
    log_rows = [r["_row_id"] for r in sc.rows_as_dicts(client, settings.log_sheet_id)]
    for i in range(0, len(log_rows), 200):
        client.Sheets.delete_rows(settings.log_sheet_id, log_rows[i:i + 200])
    print(f"cleared {len(log_rows)} log rows")

    # 2. Leavers back to Pending; remember who they are for step 5.
    cols = {c.title: c.id_ for c in client.Sheets.get_sheet(settings.leavers_sheet_id, page_size=1).columns}
    updates, leaver_emails = [], []
    for r in sc.rows_as_dicts(client, settings.leavers_sheet_id):
        if not r.get("Email"):
            continue
        leaver_emails.append(str(r["Email"]).strip())
        row = smartsheet.models.Row()
        row.id = r["_row_id"]
        for title, value in (("Status", "Pending"), ("Assets Transferred To", "")):
            cell = smartsheet.models.Cell()
            cell.column_id = cols[title]
            cell.value = value
            row.cells.append(cell)
        updates.append(row)
    if updates:
        client.Sheets.update_rows(settings.leavers_sheet_id, updates)
    print(f"reset {len(updates)} leaver row(s) to Pending")

    # 3. Reactivate allowlisted users that were deactivated.
    for email in sorted(settings.allowlist):
        user = sc.find_user(client, email=email)
        if user is not None and sc.raw_status(user.id_) == "DEACTIVATED" and not user.admin:
            client.Users.reactivate_user(user.id_)
            print(f"reactivated {email}")

    # 4. Restore the demo user's paid seat (upgrade is only valid from a lower seat type).
    demo_email = os.getenv("DEMO_SEAT_USER_EMAIL", "").strip()
    if demo_email:
        user = sc.find_user(client, email=demo_email)
        why = sc.check_target(client, settings, user)
        if why:
            print(f"left {demo_email} alone: {why}")
        else:
            try:
                client.Users.upgrade_user(user.id_, settings.plan_id, "MEMBER")
                print(f"upgraded {demo_email} to MEMBER")
            except smartsheet.exceptions.ApiError as exc:
                print(f"could not upgrade {demo_email}: {exc}")

    # 5. Optionally re-invite removed leavers so they can be removed again.
    if args.reinvite_leavers:
        for email in leaver_emails:
            if sc.find_user(client, email=email) is None and email.lower() in settings.allowlist:
                client.Users.add_user(smartsheet.models.User({"email": email, "admin": False,
                                                              "licensedSheetCreator": False}), send_email=False)
                print(f"re-invited {email}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
