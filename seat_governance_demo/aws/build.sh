#!/usr/bin/env bash
# Package the Lambda code: our modules plus the Smartsheet SDK, built for Lambda's Linux/ARM
# runtime. (boto3 is already provided by Lambda.) Output goes to aws/build/lambda.
set -euo pipefail
cd "$(dirname "$0")"
OUT=build/lambda
rm -rf build && mkdir -p "$OUT"
pip="${PIP:-.venv/bin/pip}"
"$pip" install -q --target "$OUT" --platform manylinux2014_aarch64 --python-version 3.12 \
  --implementation cp --only-binary=:all: smartsheet-python-sdk
cp ../seat_common.py ../seat_policy.py ../seat_sweep.py ../demo_env.py lambda_listener.py lambda_sweep.py "$OUT"/
echo "built $OUT ($(du -sh "$OUT" | cut -f1))"
