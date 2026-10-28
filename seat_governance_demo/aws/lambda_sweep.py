"""AWS Lambda version of the nightly sweep (run by an EventBridge schedule).

Calls seat_sweep.run_sweep(), the same function the command line uses. Dry run unless the stack
was deployed with `-c sweep_apply=true`. You can also invoke it by hand with options:

    aws lambda invoke --function-name <name> --payload '{"apply": false, "days": 0}' out.json
"""
import os

import boto3

import seat_common as sc
import seat_sweep

_state: dict = {}


def handler(event, context):
    event = event or {}
    if not _state:
        secrets = boto3.client("secretsmanager")
        os.environ["SMARTSHEET_ACCESS_TOKEN"] = secrets.get_secret_value(
            SecretId=os.environ["TOKEN_SECRET_ID"])["SecretString"].strip()
        _state["settings"] = sc.load_settings()
        _state["client"] = sc.get_client()

    apply = event.get("apply", os.environ.get("SWEEP_APPLY", "false").lower() == "true")
    actions = seat_sweep.run_sweep(_state["client"], _state["settings"], apply=bool(apply),
                                   only=event.get("only"), days_arg=event.get("days"))
    return {"apply": bool(apply), "actions": actions}
