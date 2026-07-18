#!/bin/sh
# Rollback to the previous Fly.io deployment.
# Usage: sh scripts/rollback.sh
set -e

APP="ix-tips-predictions"

echo "Current release:"
flyctl releases --app "$APP" 2>/dev/null | head -5

PREVIOUS=$(flyctl releases --app "$APP" --json 2>/dev/null | \
  python3 -c "import sys,json; r=json.load(sys.stdin); print(r[1]['id'])" 2>/dev/null)

if [ -z "$PREVIOUS" ]; then
  echo "Could not determine previous release. List releases manually:"
  echo "  fly releases --app $APP"
  exit 1
fi

echo "Rolling back to release $PREVIOUS..."
flyctl deploy --app "$APP" --image "registry.fly.io/$APP:release-$PREVIOUS" --strategy immediate

echo "Rollback complete. Verify: curl https://$APP.fly.dev/health/"
