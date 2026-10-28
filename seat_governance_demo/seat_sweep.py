#!/usr/bin/env python3
"""Nightly sweep: catch what the real-time listener cannot.

This is the "scheduled" half of Scenario 2. Two jobs, both safe by default:

  1. INACTIVE USERS. List users with their last login. Anyone inactive for INACTIVE_DAYS
     (default 45) or more is deactivated, which frees the seat and blocks sign-in while
     leaving their permissions on shared items unchanged. Reversible: reactivate_user.

  2. LEAVERS. Read the Leavers (HR Feed) sheet. For each person whose Status is "Pending",
     remove them from the account and transfer everything they owned to a dedicated service
     account, so no sheet is orphaned. NOT reversible: the user is deleted.

Every decision, including dry-run ones, is written to the Seat Governance Log sheet.

Safety rails (all enforced in seat_common.check_target):
  * dry run unless you pass --apply
  * only users on the ALLOWLIST_EMAILS list are ever acted on
  * System Admins and the account running the script are never touched
  * a leaver is never removed unless a service account is configured to receive the assets
  * at most MAX_ACTIONS destructive actions per run (default 5)

Run:
    python seat_sweep.py                   # dry run: lists what it would do
    python seat_sweep.py --apply           # does it
    python seat_sweep.py --only inactive   # or: --only leavers
    python seat_sweep.py --days 0          # treat anyone with a login on record as inactive (demo)
"""
import argparse
import sys
from datetime import datetime, timedelta, timezone

import smartsheet

import seat_common as sc

# ANSI colors for the screen recording
G, Y, C, D, R = "\033[92m", "\033[93m", "\033[96m", "\033[2m", "\033[0m"


def all_users(client):
    """Every user in the account, with last login, fetched page by page.

    lastLogin is only returned when include=lastLogin is set, the caller is a System Admin,
    and a page holds at most 100 users - so we page with page_size=100 rather than using
    includeAll. Many accounts have no login on record for a user at all (lastLogin is empty).
    """
    page = 1
    while True:
        result = client.Users.list_users(include="lastLogin", page_size=100, page=page)
        yield from result.data
        if page >= result.total_pages:
            return
        page += 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="really perform the actions (default: dry run)")
    ap.add_argument("--only", choices=["inactive", "leavers"], help="run just one of the two jobs")
    ap.add_argument("--days", type=int, help="inactivity threshold in days (default: INACTIVE_DAYS or 45)")
    ap.add_argument("--no-log", action="store_true", help="do not write to the log sheet")
    args = ap.parse_args()
    run_sweep(sc.get_client(), sc.load_settings(), apply=args.apply, only=args.only,
              days_arg=args.days, log=not args.no_log)
    return 0


def run_sweep(client, settings, *, apply: bool = False, only: str | None = None,
              days_arg: int | None = None, log: bool = True) -> int:
    """Run the sweep. Called by main() from the command line and by the AWS Lambda handler.

    Returns the number of destructive actions performed (or, in a dry run, planned).
    """
    days = settings.inactive_days if days_arg is None else days_arg
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    dry = not apply
    actions = 0   # destructive actions performed (or planned) this run

    def record(**kw):
        if log:
            sc.log_event(client, settings, source="Sweep", dry_run=dry, **kw)

    print(f"{C}Mode:{R} {'DRY RUN' if dry else 'APPLY'}   {C}allowlist:{R} {len(settings.allowlist)} user(s)   "
          f"{C}inactive after:{R} {days} days   {C}cap:{R} {settings.max_actions} actions")
    if not settings.allowlist:
        print(f"{Y}ALLOWLIST_EMAILS is empty, so every user will be skipped.{R}")

    # -------------------------------------------------------------------
    # Job 1: inactive users -> deactivate
    # -------------------------------------------------------------------
    if only in (None, "inactive"):
        print(f"\n{C}Inactive users{R}")
        # People on the pending-leavers list are handled by the leavers job below, which removes
        # them outright. Deactivating them first would be redundant, so leave them out here.
        leaving = {str(r.get("Email") or "").strip().lower()
                   for r in sc.rows_as_dicts(client, settings.leavers_sheet_id)
                   if r.get("Status") == "Pending"}
        for user in all_users(client):
            if user.status != "ACTIVE" or (user.email or "").lower() not in settings.allowlist:
                continue                                   # only look at allowlisted, active users
            if (user.email or "").lower() in leaving:
                print(f"  {D}SKIP     {user.email}  on the leavers list (handled below){R}")
                continue
            seat = sc.normalize_seat(user.seat_type) or "-"
            if user.last_login is None:
                # No login on record: we cannot prove inactivity, so we do not act.
                print(f"  {D}SKIP     {user.email}  no login on record{R}")
                continue
            idle = (datetime.now(timezone.utc) - user.last_login).days
            if user.last_login > cutoff:
                print(f"  {D}ok       {user.email}  last login {idle} days ago{R}")
                continue
            why = sc.check_target(client, settings, user)
            if why:
                print(f"  {Y}SKIP     {user.email}  {why}{R}")
                record(email=user.email, trigger="Inactive 45+ days", seat_type=seat,
                       decision="Skipped", reason=why)
                continue
            if actions >= settings.max_actions:
                print(f"  {Y}STOP     action cap reached ({settings.max_actions}){R}")
                break
            reason = f"last login {idle} days ago (threshold {days})"
            result = ""
            if not dry:
                client.Users.deactivate_user(user.id_)
                result = "OK"
            actions += 1
            print(f"  {G}DEACTIVATE{R} {user.email}  {D}{reason}{R}")
            record(email=user.email, trigger="Inactive 45+ days", seat_type=seat,
                   decision="Deactivated", reason=reason, result=result)

    # -------------------------------------------------------------------
    # Job 2: leavers -> remove, with assets transferred to the service account
    # -------------------------------------------------------------------
    if only in (None, "leavers"):
        print(f"\n{C}Leavers{R}")
        cols = {c.title: c.id_ for c in client.Sheets.get_sheet(settings.leavers_sheet_id, page_size=1).columns}
        service = None
        if settings.service_account_id:
            service = sc.find_user(client, user_id=settings.service_account_id)
        for row in sc.rows_as_dicts(client, settings.leavers_sheet_id):
            email = str(row.get("Email") or "").strip()
            if not email or row.get("Status") != "Pending":
                continue
            user = sc.find_user(client, email=email)
            why = sc.check_target(client, settings, user)
            if not why and service is None:
                why = "no SERVICE_ACCOUNT_USER_ID configured to receive the assets"
            if why:
                print(f"  {Y}SKIP     {email}  {why}{R}")
                record(email=email, trigger="Leaver", seat_type="-", decision="Skipped", reason=why)
                continue
            if actions >= settings.max_actions:
                print(f"  {Y}STOP     action cap reached ({settings.max_actions}){R}")
                break
            reason = f"on HR leavers list; assets go to {service.email}"
            result = ""
            if not dry:
                # transfer_sheets=True moves every sheet, report and dashboard the user owns
                # (and their groups) to transfer_to. remove_from_sharing=False leaves other
                # people's access to shared items as it is.
                client.Users.remove_user(user.id_, transfer_to=service.id_,
                                         transfer_sheets=True, remove_from_sharing=False)
                result = "OK"
                _mark_removed(client, settings, cols, row["_row_id"], service.email)
            actions += 1
            print(f"  {G}REMOVE   {R} {email}  {D}{reason}{R}")
            record(email=email, trigger="Leaver", seat_type="-",
                   decision="Removed + transferred", reason=reason, result=result)

    print(f"\n{Y if dry else G}{actions} action(s) {'planned (dry run, nothing changed)' if dry else 'performed'}{R}")
    return actions


def _mark_removed(client, settings, cols, row_id, service_email) -> None:
    """Close the loop in the HR sheet so a leaver is never processed twice."""
    row = smartsheet.models.Row()
    row.id = row_id
    for title, value in (("Status", "Removed"), ("Assets Transferred To", service_email)):
        cell = smartsheet.models.Cell()
        cell.column_id = cols[title]
        cell.value = value
        row.cells.append(cell)
    client.Sheets.update_rows(settings.leavers_sheet_id, [row])


if __name__ == "__main__":
    sys.exit(main())
