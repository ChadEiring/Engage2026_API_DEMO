#!/usr/bin/env bash
# Recreate the Demo 2 webhook handler on AWS from scratch, in one command.
#
#   ./redeploy.sh --profile <aws-profile>                 # deploy in DRY-RUN mode (safe: logs, changes nothing)
#   ./redeploy.sh --profile <aws-profile> --apply         # the listener really downgrades unapproved seats
#   ./redeploy.sh --profile <aws-profile> --plan          # only print what it would do
#
# What it does, in order (every step is safe to repeat):
#   1. checks your tools, your AWS login and the settings in ../.env
#   2. sets up this folder's Python environment (aws/.venv) if it is missing
#   3. packages the Lambda code                          (build.sh)
#   4. bootstraps the CDK under its own "seatdemo" name  (once per account and region)
#   5. deploys the stack: two Lambdas, a public HTTPS URL, a DynamoDB table, a nightly schedule
#   6. stores the Smartsheet API token in Secrets Manager (read from ../.env, never printed)
#   7. creates and enables the Smartsheet plan webhook, storing its signing secret in Secrets Manager
#   8. smoke-tests the public URL
# To remove everything again:  AWS_PROFILE=<profile> aws/.venv/bin/python teardown.py
set -euo pipefail
cd "$(dirname "$0")"

PROFILE=""; REGION="us-west-2"; APPLY="false"; PLAN="false"
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2;;
    --region)  REGION="$2";  shift 2;;
    --apply)   APPLY="true"; shift;;
    --plan)    PLAN="true";  shift;;
    -h|--help) sed -n '2,20p' "$0"; exit 0;;
    *) echo "unknown option: $1 (try --help)"; exit 2;;
  esac
done
[ -n "$PROFILE" ] || { echo "Give an AWS profile:  ./redeploy.sh --profile <name>"; exit 2; }

export AWS_PROFILE="$PROFILE" AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"
TOKEN_SECRET="seatdemo/smartsheet-token"; HOOK_SECRET="seatdemo/webhook-shared-secret"; STACK="SeatGovernanceDemo"
step() { printf '\n\033[96m[%s]\033[0m %s\n' "$1" "$2"; }
run()  { if [ "$PLAN" = "true" ]; then printf '   would run: %s\n' "$*"; else "$@"; fi; }

step 1 "Checking tools, login and settings"
for t in aws cdk python3; do command -v "$t" >/dev/null || { echo "missing tool: $t"; exit 1; }; done
[ -f ../.env ] || { echo "missing ../.env (copy ../.env.example and fill it in)"; exit 1; }
for k in SMARTSHEET_ACCESS_TOKEN PLAN_ID APPROVALS_SHEET_ID LOG_SHEET_ID LEAVERS_SHEET_ID; do
  grep -q "^$k=." ../.env || { echo "../.env has no value for $k"; exit 1; }
done
if [ "$PLAN" = "true" ]; then
  echo "   (plan only) profile=$PROFILE region=$REGION apply-mode=$APPLY"
else
  ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null) \
    || { echo "AWS login failed for profile '$PROFILE'. Run:  aws sso login --profile $PROFILE"; exit 1; }
  echo "   AWS account $ACCOUNT, region $REGION, listener mode: $([ "$APPLY" = true ] && echo APPLY || echo dry-run)"
fi

step 2 "Python environment"
if [ ! -x .venv/bin/python ]; then run python3 -m venv .venv; run .venv/bin/pip install -q -r requirements.txt; else echo "   aws/.venv already set up"; fi

step 3 "Packaging the Lambda code"
run env PIP=.venv/bin/pip ./build.sh

step 4 "Bootstrapping the CDK (own name: seatdemo)"
if [ "$PLAN" = "true" ]; then echo "   would run: cdk bootstrap --toolkit-stack-name CDKToolkit-seatdemo --qualifier seatdemo (skipped if already present)"
elif aws cloudformation describe-stacks --stack-name CDKToolkit-seatdemo >/dev/null 2>&1; then echo "   already bootstrapped"
else cdk bootstrap "aws://$ACCOUNT/$REGION" --toolkit-stack-name CDKToolkit-seatdemo --qualifier seatdemo \
       --tags Project=engage-2026-seat-governance-demo; fi

step 5 "Deploying the stack"
run cdk deploy "$STACK" --require-approval never -c "apply=$APPLY"

step 6 "Storing the Smartsheet token in Secrets Manager"
if [ "$PLAN" = "true" ]; then echo "   would create $TOKEN_SECRET from ../.env (value never printed)"
elif aws secretsmanager describe-secret --secret-id "$TOKEN_SECRET" >/dev/null 2>&1; then echo "   $TOKEN_SECRET already exists"
else aws secretsmanager create-secret --name "$TOKEN_SECRET" --tags Key=Project,Value=engage-2026-seat-governance-demo \
       --secret-string "$(grep '^SMARTSHEET_ACCESS_TOKEN=' ../.env | cut -d= -f2-)" --query Name --output text; fi

step 7 "Creating and enabling the Smartsheet webhook"
if [ "$PLAN" = "true" ]; then echo "   would create the plan webhook pointing at the new URL, store its signing secret as $HOOK_SECRET, then enable it"
else
  URL=$(aws cloudformation describe-stacks --stack-name "$STACK" --query "Stacks[0].Outputs[?OutputKey=='WebhookCallbackUrl'].OutputValue" --output text)
  echo "   callback URL: $URL"
  if (cd .. && aws/.venv/bin/python webhook_admin.py list) | grep -q "name='Seat governance demo'"; then
    echo "   a 'Seat governance demo' webhook already exists. If the URL changed, delete it first:"
    echo "     cd .. && aws/.venv/bin/python webhook_admin.py cleanup    then run this script again"
  else
    OUT=$(cd .. && aws/.venv/bin/python webhook_admin.py create --url "$URL" --store-secret "$HOOK_SECRET")
    echo "$OUT" | sed 's/^/   /'
    ID=$(echo "$OUT" | grep -o 'id=[0-9]*' | head -1 | cut -d= -f2)
    (cd .. && aws/.venv/bin/python webhook_admin.py enable "$ID") | sed 's/^/   /'
  fi
fi

step 8 "Smoke test"
if [ "$PLAN" = "true" ]; then echo "   would request the public URL and expect 'seat listener up'"
else sleep 3; echo "   $(curl -s -m 20 "${URL%webhook}")"; fi
printf '\nDone. To remove everything:  AWS_PROFILE=%s aws/.venv/bin/python teardown.py\n' "$PROFILE"
