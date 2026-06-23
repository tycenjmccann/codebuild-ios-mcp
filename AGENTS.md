# AGENTS.md

Operational runbook for AI coding agents working in this repo. Human-oriented
detail lives in `README.md`; this file is the terse, do-this sequence.

## What this is

A CDK v2 (TypeScript) app that provisions an AWS CodeBuild **macOS (`MAC_ARM`)**
iOS build+test runner and exposes it to agents as four MCP tools through a
Bedrock AgentCore Gateway: `ios_test`, `ios_build_status`, `list_schemes`,
`get_test_logs`. Async contract: `ios_test` returns a `build_id`; poll
`ios_build_status` until `status != "IN_PROGRESS"`.

## Hard constraints (do not violate)

- **`MAC_ARM` fleet bills continuously** (~$25-30/day/instance, ~24h min lease).
  Never create it speculatively. Confirm with the user before `cdk deploy`, and
  surface teardown in the same breath.
- **macOS = reserved capacity only.** No `ON_DEMAND`; overflow must be `QUEUE`.
- **Region must support `MAC_ARM`:** us-east-1, us-east-2, us-west-2,
  eu-central-1, ap-southeast-2.
- **No `*` IAM resources, no hardcoded account ids/ARNs.** Derive from
  `Stack.of(this)` and resource refs. Keep least-privilege.
- **No em dashes in AWS resource names** — hyphens only.

## Source-of-truth files (edit these, not generated output)

- `buildspec.yaml` — build behavior. Read at synth time and embedded inline into
  the project; the iOS repo under test needs no buildspec. One shell block on
  purpose (CodeBuild runs each list item in a fresh CWD). Edit + redeploy.
- `lambda/handler.py` — the four tools. Structured results are read from
  `s3://<bucket>/builds/<id>/summary.json`, NOT the CodeBuild Test Reports API
  (its JUnit parser silently drops cases). Keep this.
- `tooling/xcresult_to_junit.py` — writes `summary.json` (authoritative) + JUnit
  (console only). Uploaded to `s3://<bucket>/tooling/` by a BucketDeployment.
- `lib/codebuild-ios-mcp-stack.ts` — the stack. `gateway-tools.json` — tool
  schema for the gateway target. `bin/app.ts` — context → props.

## Deploy sequence

```bash
npm install
npx cdk synth -c codebuild-ios-mcp:githubRepo=<repo> -c codebuild-ios-mcp:projectDir=<dir>  # validate first
npx cdk bootstrap                                  # once per account/region
npx cdk deploy -c codebuild-ios-mcp:githubRepo=<repo> -c codebuild-ios-mcp:projectDir=<dir>
./scripts/register-gateway.sh                      # creates gateway + target (no IaC for it yet)
```

Reuse an existing gateway (one gateway, many tool stacks → one MCP URL for the
agent) instead of creating a dedicated one:
`EXISTING_GATEWAY_ID=<gw-id> ./scripts/register-gateway.sh`. It adds only a lambda
target. Because targets use GATEWAY_IAM_ROLE creds, grant the EXISTING gateway's
role `lambda:InvokeFunction` on this stack's Lambda ARN (the script prints it),
else tool calls AccessDenied. Tear down just the target with
`KEEP_GATEWAY=1 TARGET_ID=<id> GATEWAY_ID=<gw> ./scripts/deregister-gateway.sh`.

Private repo: `aws codebuild import-source-credentials --server-type GITHUB
--auth-type PERSONAL_ACCESS_TOKEN --token <pat>` once per account/region first.

VPC (internal Nexus / validation services): add
`-c codebuild-ios-mcp:vpcId=… -c codebuild-ios-mcp:subnetIds=a,b -c codebuild-ios-mcp:securityGroupIds=sg`.
Endpoints (S3+Logs+CodeBuild) are auto-created for no-NAT subnets; add
`-c codebuild-ios-mcp:createVpcEndpoints=false` if the VPC already has a NAT.

## Connecting a consuming agent

Gateway auth is `AWS_IAM` → requests must be SigV4-signed. Use the ready client
`examples/connect_agent.py` (`python examples/connect_agent.py --list` proves
auth + tool discovery). For an **AgentCore Runtime** agent, no secrets: it signs
with its execution role's ambient creds. Just grant that role
`bedrock-agentcore:InvokeGateway` on the gateway ARN, then point it at
`GATEWAY_URL`.

## Verify

```bash
# Always synth before claiming a stack change works:
npx cdk synth -c codebuild-ios-mcp:githubRepo=https://github.com/x/y -c codebuild-ios-mcp:projectDir=ios >/dev/null && echo OK

# Smoke-test the Lambda directly (no gateway needed):
aws lambda invoke --function-name codebuild-ios-mcp \
  --cli-binary-format raw-in-base64-out \
  --payload '{"tool":"ios_test","arguments":{"branch":"main","scheme":"<Scheme>"}}' out.json && cat out.json
# then poll: --payload '{"tool":"ios_build_status","arguments":{"build_id":"<id>"}}'
```

## Known gotchas (already solved — don't reintroduce)

- L2 `codebuild.Project` rejects a Mac image at construct time. Pass a **Linux
  placeholder** `buildImage`, then escape-hatch `Environment.Type/Image/
  ComputeType/Fleet` to `MAC_ARM` on the `CfnProject`.
- VPC endpoints are built with **L1 `CfnVPCEndpoint`**, not
  `Vpc.fromVpcAttributes` (which demands subnet count = a multiple of AZ count).
- CLI `-c key=false` arrives as the **string `"false"`** (truthy). Booleans from
  context are coerced in `bin/app.ts` (`bool()`); reuse it.
- A VPC build with no NAT/endpoints reaches private hosts but **loses CloudWatch
  + S3** — hence the auto-created endpoints.

## Teardown (always offer after use)

```bash
GATEWAY_NAME=codebuild-ios-mcp-gw ./scripts/deregister-gateway.sh   # gateway first (not in stack)
npx cdk destroy                                                     # stops fleet billing
```
