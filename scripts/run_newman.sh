#!/usr/bin/env bash
# Run the Postman collection under Newman.
#
# Usage:
#   bash scripts/run_newman.sh                                         # CI defaults
#   bash scripts/run_newman.sh postman/environments/Local.postman_environment.json
#
# Env overrides:
#   REPORTS_DIR    output dir for json+html reports (default: postman/reports)
#   NEWMAN_FLAGS   extra flags appended to the newman invocation
#
# Requires:
#   - node + newman (npm install -g newman)
#   - jq (for the html reporter config)

set -euo pipefail

ENV_FILE="${1:-postman/environments/CI.postman_environment.json}"
COLLECTION="${2:-postman/QlockCare_API.postman_collection.json}"
REPORTS_DIR="${REPORTS_DIR:-postman/reports}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: env file not found: $ENV_FILE" >&2
  exit 2
fi
if [[ ! -f "$COLLECTION" ]]; then
  echo "ERROR: collection not found: $COLLECTION" >&2
  exit 2
fi

mkdir -p "$REPORTS_DIR"

# In CI we --bail so a single failure surfaces fast.
# Locally, drop the flag (set NEWMAN_FLAGS="" or pass --bail=false) to see
# the full report of which requests failed.
BAIL_FLAG="--bail"
if [[ -n "${NEWMAN_NO_BAIL:-}" ]]; then
  BAIL_FLAG=""
fi

echo "Running Newman against $BASE_URL..."
echo "  collection : $COLLECTION"
echo "  environment: $ENV_FILE"
echo "  reports to : $REPORTS_DIR"
echo

# shellcheck disable=SC2086
npx --yes newman run "$COLLECTION" \
  --environment "$ENV_FILE" \
  $BAIL_FLAG \
  --reporters cli,json,html \
  --reporter-json-export "$REPORTS_DIR/run.json" \
  --reporter-html-export "$REPORTS_DIR/run.html" \
  --timeout-request 10000 \
  --timeout-script 5000 \
  --delay-request 50 \
  ${NEWMAN_FLAGS:-}