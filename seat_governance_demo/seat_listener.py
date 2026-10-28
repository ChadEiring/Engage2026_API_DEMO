#!/usr/bin/env python3
"""Webhook listener: react to seat-type changes in real time.

This is the "real time" half of Scenario 2. Smartsheet calls this server whenever a user's
seat type changes anywhere in the plan, and the server applies policy:

    seat type changed
        -> not a billable seat?                     ignore (log it)
        -> user is in the Seat Approvals sheet?     keep the seat (log it)
        -> otherwise                                downgrade to CONTRIBUTOR (log it)

Every decision is written to the Seat Governance Log sheet, which is your audit trail.

How Smartsheet talks to this server:
  1. VERIFICATION. When you enable the webhook (webhook_admin.py enable), Smartsheet sends a
     request whose body has a "challenge" value. We must answer 200 and echo that value in
     the "Smartsheet-Hook-Response" header. Only then does the webhook turn on.
  2. EVENTS. Afterwards Smartsheet POSTs a JSON body with an "events" array. Seat changes
     are events with objectType "user.seatType" and eventType "updated".
  3. We always answer 200 quickly. Anything else makes Smartsheet retry, and repeated
     failures disable the webhook. Our own errors are therefore logged, never returned.

Safe by default: without --apply nothing is changed in Smartsheet except the log sheet.

Run:
    python seat_listener.py                 # dry run: decisions are logged, no downgrades
    python seat_listener.py --apply         # really downgrades
Then expose it publicly over HTTPS for Smartsheet, for example:  ngrok http 8080
(valid callback ports are 443, 8000, 8008, 8080 and 8443; the certificate must be valid).
"""
import argparse
import hashlib
import hmac

from flask import Flask, make_response, request

import seat_common as sc
import seat_policy

app = Flask(__name__)

# Filled in by main(): kept at module level so the Flask route handlers can reach them.
SETTINGS: sc.Settings
CONTEXT: seat_policy.Context
APPLY = False

# Smartsheet can deliver the same event more than once (retries, duplicate delivery).
# Remember what we already handled so we never act or log twice. In production use a
# shared store (Redis, a database table) instead of process memory.
SEEN: set = set()


def seen_before(key: tuple) -> bool:
    """True if we already handled this event; otherwise remember it and return False."""
    if key in SEEN:
        return True
    SEEN.add(key)
    return False


@app.get("/")
def health():
    """Handy for checking the tunnel from a browser."""
    return f"seat listener up ({'APPLY' if APPLY else 'dry run'})\n"


@app.post("/webhook")
def webhook():
    raw = request.get_data()

    body = request.get_json(silent=True) or {}

    # --- 1. verification handshake ------------------------------------------
    challenge = body.get("challenge") or request.headers.get("Smartsheet-Hook-Challenge")
    if challenge:
        resp = make_response("Webhook verification successful.", 200)
        resp.headers["Smartsheet-Hook-Response"] = challenge
        print("verification challenge answered")
        return resp

    # Optional: authenticate the caller. If the webhook has a shared secret, Smartsheet signs
    # each callback body with HMAC-SHA256 in the "Smartsheet-Hmac-SHA256" header. Set
    # WEBHOOK_SHARED_SECRET to switch this on; leave it unset while you are getting started.
    if SETTINGS.shared_secret:
        expected = hmac.new(SETTINGS.shared_secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, request.headers.get("Smartsheet-Hmac-SHA256", "")):
            return "bad signature", 401

    # --- 2. plan events -------------------------------------------------------
    # The decision logic lives in seat_policy.py so the AWS Lambda can share it.
    seat_policy.handle_plan_callback(CONTEXT, body)
    return "ok", 200


def main() -> None:
    global SETTINGS, CONTEXT, APPLY
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="really downgrade users (default: dry run)")
    ap.add_argument("--port", type=int, default=8080, help="port to listen on (default 8080)")
    args = ap.parse_args()

    SETTINGS = sc.load_settings()
    APPLY = args.apply
    CONTEXT = seat_policy.Context(sc.get_client(), SETTINGS, APPLY, seen_before)
    print(f"Listening on :{args.port}  mode={'APPLY' if APPLY else 'dry run'}  "
          f"allowlist={len(SETTINGS.allowlist)} user(s)")
    app.run(host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
