# Demo 2: seat governance, event-driven

Sample code from **From One to Many: Solving Scale with the Smartsheet API** (Scenario 2:
user lifecycle on autopilot).

* **Real time:** a *plan webhook* fires when someone's seat type changes. A small listener
  checks an approval sheet and downgrades any billable seat nobody approved.
* **Nightly:** a sweep deactivates users who have been inactive for 45+ days, and removes
  leavers while transferring everything they own to a service account (no orphaned sheets).
* **Audit trail:** every decision, including dry runs, lands in a Smartsheet log sheet.

Endpoints used: plan webhook (`POST /webhooks`, scope `plan`), downgrade/upgrade user,
list users (with last login), deactivate user, remove user (with transfer).

## Files

| File | Purpose |
|---|---|
| `seat_policy.py` | **The policy**: what to do when a seat changes. Start here; shared by the local listener and AWS. |
| `seat_listener.py` | Local webhook server (Flask): handshake, signature check, calls the policy. |
| `seat_sweep.py` | Nightly job: inactive users and leavers. Dry run by default. |
| `webhook_admin.py` | Create / enable / list / delete the plan webhook (plain HTTP; see Lessons learned). |
| `simulate_event.py` | Posts a fake plan event to the listener: rehearsal and demo fallback. |
| `reset_demo2.py` | Restores the starting state between takes (see section 4 for what it cannot undo). |
| `seat_common.py` | Settings, safety checks and sheet helpers shared by all scripts. |
| `demo_env.py` | Loads `.env`. |

## Safety first

These scripts downgrade, deactivate and **permanently remove** real accounts.

* Everything is a **dry run** unless you pass `--apply`.
* Only emails in `ALLOWLIST_EMAILS` can ever be acted on. Use dedicated test accounts.
* System Admins and the account running the script are never touched.
* A leaver is never removed without a service account to receive their assets.
* Destructive actions per run are capped (`MAX_ACTIONS`, default 5).
* Removing a user cannot be undone. Test on throwaway accounts in a non-production org.

## 1. Set up

You need a **System Admin** API token (plan webhooks, seat changes, deactivate and remove all
require it) and three sheets: **Seat Approvals** (columns Email, Name, Approved Seat Type,
Approved By, Approved On, Notes), **Seat Governance Log** (Event Time, Source, Email, Trigger,
Seat Type, Decision, Reason, Dry Run, API Result) and **Leavers (HR Feed)** (Email, Name,
Department, Last Day, Status = Pending/Removed, Assets Transferred To).

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env      # fill in token, plan ID, sheet IDs, allowlist, service account
```

Users managed by an identity provider (SSO/SCIM) cannot be deactivated or removed through the
API; use plain test accounts.

## 2. Real time: a seat change, handled before anyone notices

```bash
.venv/bin/python seat_listener.py --apply          # terminal 1 (drop --apply to rehearse)
ngrok http 8080                                    # terminal 2: public HTTPS URL for Smartsheet
.venv/bin/python webhook_admin.py create --url https://<tunnel>/webhook --save-env   # keeps the signing secret in .env
.venv/bin/python webhook_admin.py enable <webhook id>
```

`--save-env` appends the webhook's shared secret to `.env`; the listener then rejects any callback that
Smartsheet did not sign. Events normally arrive within a few seconds of the change.

Now give a test user a Member seat in Admin Center. The event arrives, the user is not in Seat
Approvals, so they are downgraded to Contributor and the log sheet records it. Add them to Seat
Approvals first and the same change is logged as *Approved - kept*.

No tunnel or live webhook? Rehearse with the simulator:

```bash
.venv/bin/python simulate_event.py --email <test user> --seat-type MEMBER
.venv/bin/python simulate_event.py --challenge abc123      # tests the verification handshake
```

## 3. Nightly: inactive users and leavers

```bash
.venv/bin/python seat_sweep.py               # dry run: lists who it would deactivate / remove
.venv/bin/python seat_sweep.py --apply       # does it
```

`--only inactive|leavers` runs one job; `--days N` changes the inactivity threshold. Users with
no last login on record are skipped, because inactivity cannot be proven. Leavers listed as
*Pending* on the HR sheet are removed with their assets transferred to the service account,
then marked *Removed*.

## 4. Between takes

```bash
.venv/bin/python reset_demo2.py --reinvite-leavers
```

Clears the log, resets leavers to Pending, reactivates deactivated test users and restores the demo
user's Member seat. What it cannot undo:

* **A removed user is gone for good.** `--reinvite-leavers` invites the address again, but that is a
  brand-new, empty account: it must accept the invitation and create a sheet or two before it can be
  removed again, and the sheets already transferred to the service account stay there.
* **A restored Member seat triggers the listener.** If the listener is in apply mode it will downgrade
  the user straight back unless they are in Seat Approvals. Restore seats with the listener in dry
  run, or approve the user first.

## Lessons learned (from testing against a real org)

* **The Python SDK cannot create plan webhooks.** Its webhook model only accepts `scope="sheet"`, so
  `webhook_admin.py` calls `POST /webhooks` directly with plain HTTP.
* **The SDK cannot represent a deactivated user.** Its status field is `None` in lists, and comparing it
  with `"DEACTIVATED"` raises `KeyError`. `seat_common.raw_status()` reads the plain JSON instead.
* **Smartsheet disables a webhook whose callbacks keep failing** (status `DISABLED_CALLBACK_FAILED`).
  A signature check that wrongly rejects one real event can therefore switch the webhook off. The AWS
  listener answers 200 and drops a mismatched event, logging the reason, rather than returning 401.
* **A downgrade fires another seat event.** The new Contributor event is ignored because it is not a
  billable seat; that is what stops an infinite loop.
* **Approvals are read live.** Adding or removing a row in Seat Approvals changes the next decision
  immediately, with no redeploy. The same Member change is either *Downgraded* or *Approved - kept*.
* **Users with no login on record cannot be swept.** In a typical org most users have none. A test
  account must log in once, and a fresh login only counts as inactive with `--days 0`.
* **Pending leavers are not also deactivated.** The inactivity job skips anyone on the leavers list,
  because the leavers job removes them outright.
* **Pending (not yet accepted) invitations are invisible to the email lookup**, so such an account
  cannot be acted on until it is active.

## Running it on AWS

The same code runs on AWS Lambda with a permanent public URL, a nightly schedule and the token in
Secrets Manager, all removable with one script. See [aws/README.md](aws/README.md).

## Scheduling the sweep

```
0 2 * * * cd /path/to/seat_governance_demo && .venv/bin/python seat_sweep.py --apply
```

## Making it your own

* **Policy:** edit `handle_seat_event()` in `seat_policy.py`; swap the Seat Approvals sheet for
  your directory or ITSM approval system.
* **Production:** put the listener behind a queue so slow work never delays the 200 reply, and
  keep de-duplication state in a shared store.
* **Seat type names:** VIEWER becomes CONTRIBUTOR on 2026-10-30; `normalize_seat()` handles both.
