#!/usr/bin/env bash
#
# register-gateway.sh
#
# One-time post-deploy step. There is NO CloudFormation/CDK resource for a
# Bedrock AgentCore Gateway or its targets yet, so this wires them up via the
# CLI using the deployed stack's outputs.
#
# It:
#   1. reads CfnOutputs from the deployed CDK stack
#   2. creates an AgentCore Gateway (authorizerType AWS_IAM, protocolType MCP),
#      OR reuses an existing one when EXISTING_GATEWAY_ID is set
#   3. creates a lambda gateway-target with GATEWAY_IAM_ROLE credentials and the
#      inline tool schema from gateway-tools.json
#   4. waits until the gateway is READY and prints the gateway URL + ids
#
# Requirements: aws cli v2 (with the bedrock-agentcore-control service), jq.
# Re-running is safe-ish: create calls that hit "already exists" surface an
# error; delete first with deregister-gateway.sh if you need a clean slate.
#
# Reuse an existing gateway (one gateway, many targets) instead of creating a
# dedicated one — point several tool stacks at the same MCP endpoint so an agent
# sees all their tools at one URL:
#   EXISTING_GATEWAY_ID=my-shared-gw-abc123 scripts/register-gateway.sh
# Only a lambda target is added; the gateway (and its IAM role) are left intact,
# and deregister-gateway.sh will only remove this stack's target, not the gateway.
#
set -euo pipefail

STACK_NAME="${STACK_NAME:-CodebuildIosMcpStack}"
GATEWAY_NAME="${GATEWAY_NAME:-codebuild-ios-mcp-gw}"
TARGET_NAME="${TARGET_NAME:-codebuild-ios-mcp}"
EXISTING_GATEWAY_ID="${EXISTING_GATEWAY_ID:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TOOLS_JSON="${TOOLS_JSON:-$HERE/../gateway-tools.json}"

command -v jq >/dev/null 2>&1 || { echo "jq is required" >&2; exit 1; }
[ -f "$TOOLS_JSON" ] || { echo "tool schema not found: $TOOLS_JSON" >&2; exit 1; }

echo "Reading outputs from stack '$STACK_NAME'..."
OUTPUTS="$(aws cloudformation describe-stacks --stack-name "$STACK_NAME" \
  --query 'Stacks[0].Outputs' --output json)"

get_output() { jq -r --arg k "$1" '.[] | select(.OutputKey==$k) | .OutputValue' <<<"$OUTPUTS"; }

REGION="$(get_output StackRegion)"
LAMBDA_ARN="$(get_output LambdaArn)"
GATEWAY_ROLE_ARN="$(get_output GatewayInvokeRoleArn)"

[ -n "$REGION" ] && [ -n "$LAMBDA_ARN" ] && [ -n "$GATEWAY_ROLE_ARN" ] || {
  echo "Missing required outputs (StackRegion, LambdaArn, GatewayInvokeRoleArn). Is the stack deployed?" >&2
  exit 1
}

export AWS_REGION="$REGION"
echo "Region=$REGION"
echo "Lambda=$LAMBDA_ARN"
echo "GatewayRole=$GATEWAY_ROLE_ARN"

# ----------------------------------------------------------------------------- #
# 1. Create the gateway (AWS_IAM auth, MCP protocol), or reuse an existing one.
# ----------------------------------------------------------------------------- #
if [ -n "$EXISTING_GATEWAY_ID" ]; then
  echo "Reusing existing gateway '$EXISTING_GATEWAY_ID' (skipping create)..."
  GW_JSON="$(aws bedrock-agentcore-control get-gateway \
    --gateway-identifier "$EXISTING_GATEWAY_ID" --output json)" || {
    echo "Could not find gateway '$EXISTING_GATEWAY_ID' in region $REGION." >&2
    exit 1
  }
  GATEWAY_ID="$(jq -r '.gatewayId' <<<"$GW_JSON")"
  GATEWAY_URL="$(jq -r '.gatewayUrl' <<<"$GW_JSON")"
  GATEWAY_ARN="$(jq -r '.gatewayArn' <<<"$GW_JSON")"
  GW_AUTH="$(jq -r '.authorizerType' <<<"$GW_JSON")"
  if [ "$GW_AUTH" != "AWS_IAM" ]; then
    echo "WARNING: gateway authorizerType is '$GW_AUTH', not AWS_IAM. The Lambda" >&2
    echo "         target still works, but callers must use this gateway's auth." >&2
  fi
else
  echo "Creating gateway '$GATEWAY_NAME'..."
  GW_JSON="$(aws bedrock-agentcore-control create-gateway \
    --name "$GATEWAY_NAME" \
    --role-arn "$GATEWAY_ROLE_ARN" \
    --authorizer-type AWS_IAM \
    --protocol-type MCP \
    --description "codebuild-ios-mcp iOS build+test tools" \
    --output json)"

  GATEWAY_ID="$(jq -r '.gatewayId' <<<"$GW_JSON")"
  GATEWAY_URL="$(jq -r '.gatewayUrl' <<<"$GW_JSON")"
  GATEWAY_ARN="$(jq -r '.gatewayArn' <<<"$GW_JSON")"
fi
echo "GatewayId=$GATEWAY_ID"

# ----------------------------------------------------------------------------- #
# 2. Build the target configuration: lambda target + inline tool schema.
#    gateway-tools.json is an array of {name, description, inputSchema}.
# ----------------------------------------------------------------------------- #
TARGET_CONFIG="$(jq -n \
  --arg arn "$LAMBDA_ARN" \
  --slurpfile tools "$TOOLS_JSON" \
  '{ mcp: { lambda: { lambdaArn: $arn, toolSchema: { inlinePayload: $tools[0] } } } }')"

CRED_CONFIG='[{"credentialProviderType":"GATEWAY_IAM_ROLE"}]'

echo "Creating gateway target '$TARGET_NAME'..."
TGT_JSON="$(aws bedrock-agentcore-control create-gateway-target \
  --gateway-identifier "$GATEWAY_ID" \
  --name "$TARGET_NAME" \
  --target-configuration "$TARGET_CONFIG" \
  --credential-provider-configurations "$CRED_CONFIG" \
  --output json)"

TARGET_ID="$(jq -r '.targetId' <<<"$TGT_JSON")"
echo "TargetId=$TARGET_ID"

# ----------------------------------------------------------------------------- #
# 3. Wait for the gateway to become READY.
# ----------------------------------------------------------------------------- #
echo "Waiting for gateway to become READY..."
for _ in $(seq 1 30); do
  STATUS="$(aws bedrock-agentcore-control get-gateway --gateway-identifier "$GATEWAY_ID" \
    --query 'status' --output text 2>/dev/null || echo UNKNOWN)"
  echo "  status=$STATUS"
  [ "$STATUS" = "READY" ] && break
  [ "$STATUS" = "FAILED" ] && { echo "Gateway creation FAILED" >&2; exit 1; }
  sleep 5
done

cat <<EOF

Gateway registered.

  GATEWAY_ID:  $GATEWAY_ID
  GATEWAY_ARN: $GATEWAY_ARN
  GATEWAY_URL: $GATEWAY_URL
  TARGET_ID:   $TARGET_ID

Agents reach the four MCP tools at GATEWAY_URL using SigV4 (AWS_IAM auth).
EOF

# ----------------------------------------------------------------------------- #
# Paste-ready config for a LOCAL desktop MCP client (Claude Code/Desktop, Cursor,
# Q CLI). These clients can't SigV4-sign, so route through AWS Labs'
# mcp-proxy-for-aws (uvx-installable) which signs each call with your AWS creds.
# ----------------------------------------------------------------------------- #
CALLER_PROFILE="${AWS_PROFILE:-default}"
cat <<EOF
----------------------------------------------------------------------------
Test it locally from a desktop MCP client (Claude Code/Desktop, Cursor, Q CLI)
----------------------------------------------------------------------------
Desktop clients speak MCP but can't SigV4-sign, so route through AWS Labs'
signing proxy. Add this to ~/.claude.json (or .mcp.json / Cursor / Q CLI),
then ensure your creds resolve to THIS gateway's account ($REGION):

  "mcpServers": {
    "codebuild-ios": {
      "command": "uvx",
      "args": ["mcp-proxy-for-aws@latest", "$GATEWAY_URL",
               "--service", "bedrock-agentcore", "--region", "$REGION"],
      "env": { "AWS_PROFILE": "$CALLER_PROFILE" }
    }
  }

Your IAM principal needs 'bedrock-agentcore:InvokeGateway' on:
  $GATEWAY_ARN
EOF

if [ -n "$EXISTING_GATEWAY_ID" ]; then
  cat <<EOF
NOTE (reused gateway): targets use GATEWAY_IAM_ROLE credentials, so the EXISTING
gateway's own role invokes the Lambda — this stack's GatewayInvokeRole
($GATEWAY_ROLE_ARN) is NOT used. Grant the existing gateway's role
'lambda:InvokeFunction' on:
  $LAMBDA_ARN
otherwise tool calls fail with AccessDenied.

Tear down only this target (leaves the shared gateway intact):
  aws bedrock-agentcore-control delete-gateway-target \\
    --gateway-identifier $GATEWAY_ID --target-id $TARGET_ID
EOF
else
  cat <<EOF
Tear down with: GATEWAY_ID=$GATEWAY_ID scripts/deregister-gateway.sh
EOF
fi
