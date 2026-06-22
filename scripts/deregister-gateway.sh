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
# Requirements: aws cli v2, jq.
#
set -euo pipefail

command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 1; }

GATEWAY_ID="${GATEWAY_ID:-}"
GATEWAY_NAME="${GATEWAY_NAME:-codebuild-ios-mcp-gw}"

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

echo "Deleting targets..."
TARGET_IDS="$(aws bedrock-agentcore-control list-gateway-targets \
  --gateway-identifier "$GATEWAY_ID" \
  --query 'items[].targetId' --output text 2>/dev/null || echo "")"

for tid in $TARGET_IDS; do
  echo "  deleting target $tid"
  aws bedrock-agentcore-control delete-gateway-target \
    --gateway-identifier "$GATEWAY_ID" --target-id "$tid" >/dev/null
done

echo "Deleting gateway $GATEWAY_ID..."
aws bedrock-agentcore-control delete-gateway --gateway-identifier "$GATEWAY_ID" >/dev/null
echo "Done."
