# CLAUDE.md

Guidance for working in this repository.

## What this project is

`codebuild-ios-mcp` — an AWS CodeBuild macOS (`MAC_ARM`) iOS build + test runner
exposed to AI agents as MCP tools through an Amazon Bedrock AgentCore Gateway.
Packaged as a CDK v2 (TypeScript) deployable. See `README.md` for the full
architecture, deploy steps, and cost warning.

## Layout

| Path | Role |
|------|------|
| `bin/app.ts` | CDK app entrypoint; reads context, instantiates the stack |
| `lib/codebuild-ios-mcp-stack.ts` | The stack: S3 bucket, MAC_ARM fleet, CodeBuild project, Lambda, gateway invoke role, outputs |
| `lambda/handler.py` | The seven MCP tools (`ios_test`, `ios_build_status`, `list_schemes`, `get_test_logs`, `get_build_log`, `ios_list_builds`, `ios_cancel`) |
| `tooling/xcresult_to_junit.py` | xcresult to JUnit converter, deployed to `s3://<bucket>/tooling/` |
| `buildspec.yaml` | Embedded inline into the CodeBuild project — single source of truth for build behavior |
| `gateway-tools.json` | Inline tool schema for the Gateway lambda target |
| `scripts/register-gateway.sh` | One-time post-deploy: create gateway + lambda target from stack outputs |
| `scripts/deregister-gateway.sh` | Delete target(s) + gateway |

## Key facts when editing

- **The buildspec is the source of truth.** It is read at synth time and embedded
  inline. Edit `buildspec.yaml` + `cdk deploy` to change build behavior; the iOS
  repo under test needs no buildspec.
- **MAC_ARM fleet uses `AWS::CodeBuild::Fleet` (`CfnFleet`)** — no L2/L1 construct
  exists. The Project L2 has no fleet prop, so the fleet ARN is applied via an
  escape hatch (`addPropertyOverride('Environment.Fleet.FleetArn', ...)`). Keep
  `Environment.Type/Image/ComputeType` set alongside it.
- **`ON_DEMAND` overflow is not supported for `MAC_ARM`** — the fleet uses `QUEUE`.
- **The AgentCore Gateway has no IaC resource yet.** The stack only creates the
  invoke role + Lambda resource permission; `scripts/register-gateway.sh` does the
  rest via CLI. Fold it into the stack when a CloudFormation resource ships.
- **The Lambda/buildspec contract:** `ios_test` overrides `SCHEME`, `DEVICE`,
  `OS_VERSION`, `TEST_PLAN` via `environmentVariablesOverride`; the project sets
  `ARTIFACTS_BUCKET` and `PROJECT_DIR`. Keep both sides in sync.
- **Least privilege, no hardcoding.** Account/region derive from
  `Stack.of(this)`; IAM scopes to specific project, report-group, and bucket
  ARNs. Do not introduce `*` resources or hardcoded account ids.
- **Do not use em dashes in AWS resource names** — use hyphens.

## Cost

A `MAC_ARM` reserved fleet bills continuously (~$25-30/day per instance, ~24h
minimum lease). Tear it down with `cdk destroy` when not in use.
