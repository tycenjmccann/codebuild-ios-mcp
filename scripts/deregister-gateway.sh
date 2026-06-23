#!/usr/bin/env bash
#
# deregister-gateway.sh
#
# Deletes the AgentCore Gateway target(s) and then the gateway created by
# register-gateway.sh. Targets must be deleted before the gateway.
#
# Provide the gateway id (printed by register-gateway.sh) or its name:
#   GATEWAY_ID=codebuild-ios-mcp-gw-abc1234567 scripts/deregister-gateway.sh
#   GATEWAY_NAME=codebuild-ios-mcp-gw          scripts/deregister-gateway.sh
#
# Shared gateway (registered with EXISTING_GATEWAY_ID): delete ONLY this stack's
# target and leave the gateway in place by setting KEEP_GATEWAY=1 (and ideally
# TARGET_ID so siblings on the same gateway are untouched):
#   KEEP_GATEWAY=1 TARGET_ID=ABC123 GATEWAY_ID=my-shared-gw scripts/deregister-gateway.sh
#
# Requirements: aws cli v2, jq.
#
set -euo pipefail

command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 1; }

GATEWAY_ID="${GATEWAY_ID:-}"
GATEWAY_NAME="${GATEWAY_NAME:-codebuild-ios-mcp-gw}"
TARGET_ID="${TARGET_ID:-}"
KEEP_GATEWAY="${KEEP_GATEWAY:-}"

# Resolve id from name if only the name was given.
if [ -z "$GATEWAY_ID" ]; then
  echo "Resolving gateway id for name '$GATEWAY_NAME'..."
  GATEWAY_ID="$(aws bedrock-agentcore-control list-gateways \
    --query "items[?name=='${GATEWAY_NAME}'].gatewayId | [0]" --output text 2>/dev/null || echo None)"
fi

[ -n "$GATEWAY_ID" ] && [ "$GATEWAY_ID" != "None" ] || {
  echo "Could not resolve a gateway id (set GATEWAY_ID or GATEWAY_NAME)." >&2
  exit 1
}
echo "GatewayId=$GATEWAY_ID"

# Guard: keeping a shared gateway means deleting exactly one target. Without
# TARGET_ID the else-branch below would bulk-delete EVERY target (siblings
# included) before the KEEP_GATEWAY guard runs — defeating the point. Fail fast.
if [ -n "$KEEP_GATEWAY" ] && [ -z "$TARGET_ID" ]; then
  echo "KEEP_GATEWAY is set but TARGET_ID is not. Refusing to bulk-delete targets" >&2
  echo "on a shared gateway. Set TARGET_ID=<id> to remove only this stack's target." >&2
  exit 1
fi

if [ -n "$TARGET_ID" ]; then
  # Targeted delete — only this one target. Used for shared gateways.
  echo "Deleting target $TARGET_ID..."
  aws bedrock-agentcore-control delete-gateway-target \
    --gateway-identifier "$GATEWAY_ID" --target-id "$TARGET_ID" >/dev/null
else
  echo "Deleting targets..."
  TARGET_IDS="$(aws bedrock-agentcore-control list-gateway-targets \
    --gateway-identifier "$GATEWAY_ID" \
    --query 'items[].targetId' --output text 2>/dev/null || echo "")"

  for tid in $TARGET_IDS; do
    echo "  deleting target $tid"
    aws bedrock-agentcore-control delete-gateway-target \
      --gateway-identifier "$GATEWAY_ID" --target-id "$tid" >/dev/null
  done
fi

if [ -n "$KEEP_GATEWAY" ]; then
  echo "KEEP_GATEWAY set — leaving gateway $GATEWAY_ID in place."
  echo "Done."
  exit 0
fi

echo "Deleting gateway $GATEWAY_ID..."
aws bedrock-agentcore-control delete-gateway --gateway-identifier "$GATEWAY_ID" >/dev/null
echo "Done."
