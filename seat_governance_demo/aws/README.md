# Demo 2 on AWS (Lambda)

The same seat-governance demo, running in AWS instead of on a laptop:

```
Smartsheet --HTTPS--> Lambda Function URL --> listener Lambda --> Smartsheet API
                                                   +--> DynamoDB (duplicate-event filter)
EventBridge schedule (nightly) --> sweep Lambda --> Smartsheet API
Secrets Manager: API token, webhook shared secret
```

You get a permanent public HTTPS address (so no ngrok), a nightly schedule (so no cron), and the
token in a secret store instead of a file. The decision logic is the **same code** as the local
version (`../seat_policy.py`, `../seat_sweep.py`); only the thin handlers here are AWS-specific.

Safe by default: both functions are deployed in **dry-run** mode and only log what they would do.
Real downgrades need `cdk deploy -c apply=true`; real sweep actions need `-c sweep_apply=true`.
The allowlist in `../.env` still applies.

> **Gotcha:** these flags are not remembered. Every `cdk deploy` without `-c apply=true` puts the
> listener back in dry run. Check with the health URL, which says `seat listener up (APPLY)` or `(dry run)`.

## Deploy

Needs the AWS CLI signed in, the CDK CLI, and Python 3.12+. `aws-cdk-lib` is pinned in `requirements.txt`
to a version your installed CDK CLI can read; if `cdk` reports a "cloud assembly schema version" mismatch,
upgrade the CLI or change the pin.

```bash
cd aws
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
export AWS_PROFILE=<your profile> AWS_REGION=us-west-2

./build.sh                                                   # package the Lambda code
cdk bootstrap --toolkit-stack-name CDKToolkit-seatdemo --qualifier seatdemo \
  --tags Project=engage-2026-seat-governance-demo           # once per account/region
cdk deploy                                                   # prints the webhook callback URL
```

Store the Smartsheet token (a System Admin's) in Secrets Manager. This reads it from `../.env` without
printing it:

```bash
aws secretsmanager create-secret --name seatdemo/smartsheet-token \
  --secret-string "$(grep '^SMARTSHEET_ACCESS_TOKEN=' ../.env | cut -d= -f2-)" \
  --tags Key=Project,Value=engage-2026-seat-governance-demo
```

Check it is alive: opening the callback URL (without `/webhook`) in a browser should say
`seat listener up (dry run)`.

## Create the Smartsheet webhook

```bash
cd ..
aws/.venv/bin/python webhook_admin.py create --url <WebhookCallbackUrl from cdk deploy> \
  --store-secret seatdemo/webhook-shared-secret
aws/.venv/bin/python webhook_admin.py enable <webhook id>
```

The create response includes a `sharedSecret`; `--store-secret` writes it straight into Secrets Manager
without printing it (this needs boto3, which `aws/.venv` has). The listener keeps looking for that secret
until it finds it, so you do not need to redeploy afterwards. Until it exists, the listener logs a warning
and accepts unsigned calls. `enable` makes Smartsheet verify the endpoint and switch the webhook on.

Once the secret is stored, any callback without Smartsheet's signature is dropped. Note that
`webhook_admin.py` uses plain HTTP because the Python SDK cannot create plan-level webhooks.

## Watch it

Lines you will see in the listener log: `verification challenge answered`, then one line per decision, e.g.
`user: MEMBER -> Downgraded (...)`. `ignored: bad signature | body_len=... expected=... got=...` means an
event was dropped; the values (never the secret) show why. Smartsheet disables a webhook that keeps getting
error replies, so a mismatch is answered with 200 and ignored instead of returning 401.

```bash
aws logs tail /aws/lambda/<ListenerFunction from the deploy output> --follow
aws lambda invoke --function-name <SweepFunction> --payload '{"apply": false, "days": 0}' /dev/stdout
```

## Clean up (when the demo is done)

```bash
cd aws && .venv/bin/python teardown.py
```

It deletes, in order: the Smartsheet webhook, the stack (Lambdas, public URL, table, schedule, logs),
the two secrets, and the CDK bootstrap resources (`--keep-bootstrap` to leave those), then lists anything
in the account still tagged `Project=engage-2026-seat-governance-demo`. It does not touch the Smartsheet
sheets or users.

Everything this demo creates carries the tags `Project`, `Owner`, `Purpose` and `TeardownBy`.
