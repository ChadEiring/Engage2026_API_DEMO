"""AWS Lambda version of the webhook listener (behind a public HTTPS Function URL).

Same job as seat_listener.py - answer Smartsheet's verification handshake, then apply the seat
policy to each seat-type event - but running on AWS instead of on your laptop behind a tunnel.
The decision logic is not duplicated here: it lives in seat_policy.py, which both share.

What is different on AWS:
  * the Smartsheet API token comes from AWS Secrets Manager, not a .env file
  * duplicate events are filtered with a DynamoDB table (memory does not survive between
    Lambda invocations or span several copies running at once)
  * the endpoint is public, so we verify Smartsheet's signature on every event once the
    webhook's shared secret has been stored

Lambda Function URL events use the "payload format 2.0" shape: headers are lower-cased and the
body may be base64 encoded.
"""
import base64
import hashlib
import hmac
import json
import os
import time

import boto3
from botocore.exceptions import ClientError

import seat_common as sc
import seat_policy

_secrets = boto3.client("secretsmanager")
_dynamo = boto3.client("dynamodb")
_state: dict = {}          # filled once per cold start, reused by later invocations


def _secret(secret_id: str, required: bool = True) -> str | None:
    try:
        return _secrets.get_secret_value(SecretId=secret_id)["SecretString"].strip()
    except ClientError:
        if required:
            raise
        return None


def seen_before(key: tuple) -> bool:
    """True if this event was already handled. A conditional write makes 'check and remember' atomic."""
    pk = "|".join(str(part) for part in key)
    try:
        _dynamo.put_item(
            TableName=os.environ["DEDUPE_TABLE"],
            Item={"pk": {"S": pk}, "ttl": {"N": str(int(time.time()) + 86400)}},   # forget after a day
            ConditionExpression="attribute_not_exists(pk)",
        )
        return False
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return True
        raise


def _init() -> dict:
    """Load settings and secrets once per cold start."""
    if not _state:
        os.environ["SMARTSHEET_ACCESS_TOKEN"] = _secret(os.environ["TOKEN_SECRET_ID"])
        settings = sc.load_settings()
        # Only exists after the webhook has been created (the create call returns it).
        settings.shared_secret = _secret(os.environ["WEBHOOK_SECRET_ID"], required=False)
        apply = os.environ.get("APPLY", "false").lower() == "true"
        _state["settings"] = settings
        _state["ctx"] = seat_policy.Context(sc.get_client(), settings, apply, seen_before)
    return _state


def _reply(status: int, body: str = "", headers: dict | None = None) -> dict:
    return {"statusCode": status, "headers": headers or {}, "body": body}


def handler(event, context):
    state = _init()
    method = event.get("requestContext", {}).get("http", {}).get("method", "POST")
    if method == "GET":                                   # handy health check from a browser
        return _reply(200, f"seat listener up ({'APPLY' if state['ctx'].apply else 'dry run'})\n")

    raw = event.get("body") or ""
    raw = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        return _reply(400, "invalid JSON")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    # 1. Verification handshake: echo the challenge in a response header. Harmless to answer,
    #    so it comes before the signature check.
    challenge = body.get("challenge") or headers.get("smartsheet-hook-challenge")
    if challenge:
        print("verification challenge answered")
        return _reply(200, "Webhook verification successful.", {"Smartsheet-Hook-Response": challenge})

    # 2. Authenticate events: Smartsheet signs the body with the webhook's shared secret
    #    (HMAC-SHA256, hex) in the Smartsheet-Hmac-SHA256 header. The secret only exists after the
    #    webhook is created, which can be after this Lambda started, so keep looking for it until
    #    it is found (one cheap lookup per event; after that it is cached).
    if not state["settings"].shared_secret:
        state["settings"].shared_secret = _secret(os.environ["WEBHOOK_SECRET_ID"], required=False)
    secret = state["settings"].shared_secret
    if secret:
        expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        got = headers.get("smartsheet-hmac-sha256", "")
        if not hmac.compare_digest(expected, got):
            # Facts only, never the secret: enough to work out WHY a signature did not match
            # (body encoding, a missing header, a different content type...).
            print(f"ignored: bad signature | body_len={len(raw)} base64={bool(event.get('isBase64Encoded'))} "
                  f"header_len={len(got)} expected={expected[:8]} got={got[:8]} "
                  f"content_type={headers.get('content-type')} events={len(body.get('events', []))}")
            # Answer 200, not 401. Smartsheet DISABLES a webhook whose callbacks keep failing, so a
            # signature check that is ever wrong must not be able to switch the webhook off.
            # The event is dropped (never acted on) and the log line above records it.
            return _reply(200, "ignored")
    else:
        print("WARNING: no webhook shared secret stored; callbacks are not being authenticated")

    # 3. Apply the policy to every seat-type event. Always answer 200 so Smartsheet does not retry.
    handled = seat_policy.handle_plan_callback(state["ctx"], body)
    return _reply(200, f"handled {handled}")
