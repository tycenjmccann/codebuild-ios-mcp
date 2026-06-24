# CodeBuild iOS Test MCP Server — Specification

## Overview

An MCP server that gives AI agents a closed-loop interface to AWS CodeBuild macOS fleets for iOS build + test execution. The agent triggers a build, waits for results, and receives structured test outcomes + artifact URLs — no webhook plumbing or manual log parsing required.

**Protocol:** Model Context Protocol (MCP) over stdio
**Language:** Python 3.11+ (or TypeScript — your preference)
**Dependencies:** `boto3`, `mcp` SDK, `pydantic`

---

## Architecture

```
┌──────────────────────┐         ┌─────────────────────────┐
│  Agent (AgentCore    │  MCP    │  codebuild-ios-mcp      │
│  Runtime / local)    │◄──────►│  server (stdio)          │
│                      │         │                          │
│  "run tests on       │         │  ┌─────────────────┐    │
│   branch fix/123"    │         │  │ boto3 CodeBuild  │    │
│                      │         │  │ boto3 S3         │    │
└──────────────────────┘         │  │ boto3 CloudWatch │    │
                                 │  └────────┬────────┘    │
                                 └───────────┼─────────────┘
                                             │
                              ┌──────────────▼──────────────┐
                              │  AWS CodeBuild macOS Fleet   │
                              │  (Apple M2/M4, Xcode 26)    │
                              │                              │
                              │  buildspec.yaml:             │
                              │    xcodebuild test           │
                              │    xcrun simctl screenshot   │
                              │    upload xcresult to S3     │
                              └──────────────────────────────┘
```

---

## MCP Tools

### 1. `ios_test`

**Description:** Trigger an iOS build + test run on CodeBuild macOS and return structured results.

**Input Schema:**

```json
{
  "type": "object",
  "properties": {
    "branch": {
      "type": "string",
      "description": "Git branch or commit SHA to test"
    },
    "scheme": {
      "type": "string",
      "description": "Xcode scheme to build and test"
    },
    "device": {
      "type": "string",
      "default": "iPhone 16",
      "description": "Simulator device name (e.g. 'iPhone 16', 'iPad Pro 13-inch')"
    },
    "os_version": {
      "type": "string",
      "default": "latest",
      "description": "iOS version for Simulator (e.g. '18.2', 'latest')"
    },
    "test_plan": {
      "type": "string",
      "default": "",
      "description": "Optional: specific test plan to run (empty = default)"
    },
    "timeout_minutes": {
      "type": "integer",
      "default": 30,
      "description": "Max wait time before timing out"
    }
  },
  "required": ["branch", "scheme"]
}
```

**Output Schema:**

```json
{
  "type": "object",
  "properties": {
    "status": { "type": "string", "enum": ["SUCCEEDED", "FAILED", "TIMED_OUT", "BUILD_ERROR"] },
    "build_id": { "type": "string" },
    "duration_seconds": { "type": "integer" },
    "test_summary": {
      "type": "object",
      "properties": {
        "total": { "type": "integer" },
        "passed": { "type": "integer" },
        "failed": { "type": "integer" },
        "skipped": { "type": "integer" }
      }
    },
    "failures": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "test_name": { "type": "string" },
          "class_name": { "type": "string" },
          "message": { "type": "string" },
          "duration_ms": { "type": "number" }
        }
      }
    },
    "artifacts": {
      "type": "object",
      "properties": {
        "xcresult_url": { "type": "string", "description": "S3 presigned URL to .xcresult bundle" },
        "screenshots": { "type": "array", "items": { "type": "string" }, "description": "S3 presigned URLs to captured screenshots" },
        "logs_url": { "type": "string", "description": "CloudWatch Logs deep link" }
      }
    },
    "build_errors": {
      "type": "array",
      "items": { "type": "string" },
      "description": "Compiler errors if build failed before tests ran"
    }
  }
}
```

---

### 2. `ios_build_status`

**Description:** Check status of a running or completed build (for async patterns).

**Input:**

```json
{
  "build_id": { "type": "string", "description": "CodeBuild build ID from ios_test" }
}
```

**Output:** Same as `ios_test` output.

---

### 3. `list_schemes`

**Description:** List available Xcode schemes in the repo (helps agent pick the right one).

**Input:**

```json
{
  "branch": { "type": "string", "default": "main" }
}
```

**Output:**

```json
{
  "schemes": ["MyApp", "MyAppTests", "MyAppUITests"],
  "default_scheme": "MyApp"
}
```

---

### 4. `get_test_logs`

**Description:** Fetch detailed logs for a specific failed test (agent uses this to understand *why* a test failed).

**Input:**

```json
{
  "build_id": { "type": "string" },
  "test_name": { "type": "string" }
}
```

**Output:**

```json
{
  "test_name": "testLoginFlow",
  "class_name": "AuthenticationTests",
  "full_output": "XCTAssertEqual failed: (\"Welcome\") is not equal to (\"Welcome Back\") - ...",
  "stack_trace": "AuthenticationTests.swift:47 ...",
  "screenshots": ["s3://bucket/builds/123/screenshots/testLoginFlow_failure.png"]
}
```

---

## Implementation Details

### CodeBuild Project Setup (prerequisite)

The customer creates a CodeBuild project with:

```yaml
# buildspec.yaml (lives in their iOS repo)
version: 0.2

env:
  variables:
    SCHEME: "MyApp"           # overridden by MCP tool via env-vars-override
    DEVICE: "iPhone 16"
    OS_VERSION: "18.2"
    ARTIFACTS_BUCKET: "my-ios-test-artifacts"

phases:
  install:
    commands:
      - xcrun simctl boot "$DEVICE"
  
  pre_build:
    commands:
      - xcodebuild -resolvePackageDependencies -scheme "$SCHEME"
  
  build:
    commands:
      - |
        xcodebuild test \
          -scheme "$SCHEME" \
          -destination "platform=iOS Simulator,name=$DEVICE,OS=$OS_VERSION" \
          -resultBundlePath "./TestResults.xcresult" \
          -enableCodeCoverage YES \
          2>&1 | tee build_output.log
  
  post_build:
    commands:
      # Capture screenshots
      - xcrun simctl io booted screenshot "screenshot_final.png" || true
      # Upload xcresult + screenshots to S3
      - aws s3 cp ./TestResults.xcresult s3://$ARTIFACTS_BUCKET/builds/$CODEBUILD_BUILD_ID/TestResults.xcresult --recursive
      - aws s3 cp ./screenshot_final.png s3://$ARTIFACTS_BUCKET/builds/$CODEBUILD_BUILD_ID/screenshots/ || true
      # Export JUnit XML for CodeBuild Test Reports
      - xcresulttool get --format json --path ./TestResults.xcresult > test_results.json

reports:
  ios-test-report:
    files:
      - "test_results.json"
    file-format: "GENERICJSONREPORT"

artifacts:
  files:
    - "build_output.log"
    - "screenshot_final.png"
  base-directory: "."
```

### CodeBuild Fleet Config

```
Fleet type:       Reserved capacity
OS:               macOS
Compute:          Apple M2 (or M4 when available)
Image:            aws/codebuild/macos-arm-base:14 (Xcode 26 preinstalled)
Capacity:         1-3 instances (scale based on agent concurrency)
```

---

### MCP Server Core Logic (Python pseudocode)

```python
import boto3
import time
import json
from mcp.server import Server
from mcp.types import Tool, TextContent
from pydantic import BaseModel

app = Server("codebuild-ios-mcp")
codebuild = boto3.client("codebuild")
s3 = boto3.client("s3")

CODEBUILD_PROJECT = "ios-agent-tests"  # from env/config
ARTIFACTS_BUCKET = "my-ios-test-artifacts"

@app.tool()
async def ios_test(branch: str, scheme: str, device: str = "iPhone 16",
                   os_version: str = "latest", test_plan: str = "",
                   timeout_minutes: int = 30) -> dict:
    """Trigger iOS build+test on CodeBuild macOS and return structured results."""
    
    # 1. Start the build
    env_overrides = [
        {"name": "SCHEME", "value": scheme, "type": "PLAINTEXT"},
        {"name": "DEVICE", "value": device, "type": "PLAINTEXT"},
        {"name": "OS_VERSION", "value": os_version, "type": "PLAINTEXT"},
    ]
    if test_plan:
        env_overrides.append({"name": "TEST_PLAN", "value": test_plan, "type": "PLAINTEXT"})
    
    response = codebuild.start_build(
        projectName=CODEBUILD_PROJECT,
        sourceVersion=branch,
        environmentVariablesOverride=env_overrides
    )
    build_id = response["build"]["id"]
    
    # 2. Poll for completion
    start_time = time.time()
    timeout = timeout_minutes * 60
    
    while True:
        builds = codebuild.batch_get_builds(ids=[build_id])
        build = builds["builds"][0]
        status = build["buildStatus"]
        
        if status != "IN_PROGRESS":
            break
        
        if time.time() - start_time > timeout:
            return {"status": "TIMED_OUT", "build_id": build_id}
        
        time.sleep(10)  # poll every 10s
    
    # 3. Fetch test report
    test_summary, failures = _get_test_results(build_id)
    
    # 4. Generate presigned URLs for artifacts
    artifacts = _get_artifact_urls(build_id)
    
    # 5. Check for build errors (compilation failures)
    build_errors = []
    if status == "FAILED" and test_summary["total"] == 0:
        build_errors = _extract_build_errors(build)
    
    duration = int(build.get("endTime", time.time()) - build["startTime"].timestamp())
    
    return {
        "status": "BUILD_ERROR" if build_errors else status,
        "build_id": build_id,
        "duration_seconds": duration,
        "test_summary": test_summary,
        "failures": failures,
        "artifacts": artifacts,
        "build_errors": build_errors
    }


def _get_test_results(build_id: str) -> tuple[dict, list]:
    """Fetch structured test results from CodeBuild Test Reports API."""
    
    # List reports for this build
    reports = codebuild.list_reports_for_report_group(
        reportGroupArn=f"arn:aws:codebuild:REGION:ACCOUNT:report-group/ios-test-report"
    )
    
    # Find the report for this build
    for report_arn in reports.get("reports", []):
        report = codebuild.describe_report(reportArn=report_arn)
        if build_id in report_arn:
            break
    
    # Get test cases
    test_cases = codebuild.describe_test_cases(reportArn=report_arn)
    
    summary = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
    failures = []
    
    for tc in test_cases.get("testCases", []):
        summary["total"] += 1
        if tc["status"] == "SUCCEEDED":
            summary["passed"] += 1
        elif tc["status"] == "FAILED":
            summary["failed"] += 1
            failures.append({
                "test_name": tc["name"],
                "class_name": tc.get("prefix", ""),
                "message": tc.get("message", ""),
                "duration_ms": tc.get("durationInNanoSeconds", 0) / 1_000_000
            })
        else:
            summary["skipped"] += 1
    
    return summary, failures


def _get_artifact_urls(build_id: str) -> dict:
    """Generate presigned S3 URLs for test artifacts."""
    prefix = f"builds/{build_id}"
    
    # List screenshots
    screenshots = []
    resp = s3.list_objects_v2(Bucket=ARTIFACTS_BUCKET, Prefix=f"{prefix}/screenshots/")
    for obj in resp.get("Contents", []):
        url = s3.generate_presigned_url("get_object",
            Params={"Bucket": ARTIFACTS_BUCKET, "Key": obj["Key"]},
            ExpiresIn=3600)
        screenshots.append(url)
    
    # xcresult URL
    xcresult_url = s3.generate_presigned_url("get_object",
        Params={"Bucket": ARTIFACTS_BUCKET, "Key": f"{prefix}/TestResults.xcresult"},
        ExpiresIn=3600)
    
    # CloudWatch logs
    logs_url = f"https://console.aws.amazon.com/cloudwatch/home?region=REGION#logsV2:log-groups/log-group/codebuild/log-events/{build_id}"
    
    return {
        "xcresult_url": xcresult_url,
        "screenshots": screenshots,
        "logs_url": logs_url
    }


def _extract_build_errors(build: dict) -> list[str]:
    """Parse compilation errors from build phases."""
    errors = []
    for phase in build.get("phases", []):
        if phase.get("phaseStatus") == "FAILED":
            for ctx in phase.get("contexts", []):
                errors.append(ctx.get("message", "Unknown error"))
    return errors
```

---

## Configuration

### Environment Variables (MCP server)

| Variable | Description | Example |
|----------|-------------|---------|
| `CODEBUILD_PROJECT` | CodeBuild project name | `ios-agent-tests` |
| `ARTIFACTS_BUCKET` | S3 bucket for test artifacts | `my-ios-test-artifacts` |
| `AWS_REGION` | AWS region | `us-west-2` |
| `POLL_INTERVAL_SEC` | Seconds between status checks | `10` |
| `DEFAULT_TIMEOUT_MIN` | Default timeout if not specified | `30` |

### MCP Client Config (for the agent)

```json
{
  "mcpServers": {
    "codebuild-ios": {
      "command": "python",
      "args": ["-m", "codebuild_ios_mcp"],
      "env": {
        "CODEBUILD_PROJECT": "ios-agent-tests",
        "ARTIFACTS_BUCKET": "my-ios-test-artifacts",
        "AWS_REGION": "us-west-2"
      }
    }
  }
}
```

---

## Agent Feedback Loop — Full Cycle

```
Agent receives bug report
    │
    ▼
Agent reads code, writes fix, commits to branch "fix/BUG-123"
    │
    ▼
Agent calls: ios_test(branch="fix/BUG-123", scheme="MyApp")
    │
    ▼ (waits 3-8 min for CodeBuild)
    │
Agent receives structured result:
    ├─ SUCCEEDED → agent collects screenshots as evidence, closes bug
    │
    └─ FAILED → agent reads failures[].message + get_test_logs()
         │
         ▼
    Agent analyzes failure, writes another fix, commits, calls ios_test() again
         │
         ▼ (loop until pass or max retries)
```

---

## IAM Permissions Required

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "codebuild:StartBuild",
        "codebuild:BatchGetBuilds",
        "codebuild:ListReportsForReportGroup",
        "codebuild:DescribeReport",
        "codebuild:DescribeTestCases"
      ],
      "Resource": "arn:aws:codebuild:*:*:project/ios-agent-tests"
    },
    {
      "Effect": "Allow",
      "Action": [
        "codebuild:DescribeReport",
        "codebuild:DescribeTestCases"
      ],
      "Resource": "arn:aws:codebuild:*:*:report-group/ios-test-report"
    },
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::my-ios-test-artifacts",
        "arn:aws:s3:::my-ios-test-artifacts/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": "logs:GetLogEvents",
      "Resource": "arn:aws:logs:*:*:log-group:/aws/codebuild/ios-agent-tests:*"
    }
  ]
}
```

---

## Enhancements (v2 ideas)

- **EventBridge integration:** Replace polling with EventBridge → SQS push for instant notification
- **Parallel test sharding:** Split test plan across multiple CodeBuild instances
- **Screenshot diff:** Compare screenshots against baseline images, return visual diff
- **Code coverage delta:** Return coverage % change vs main branch
- ~~**Caching:** Persist DerivedData between builds for faster subsequent runs~~ — DONE: warm DerivedData + resolved SPM persist in `$HOME/ios-mcp-state` on the always-on reserved Mac (no CodeBuild cache feature; see buildspec)
- **XCUITest video recording:** `xcrun simctl io booted recordVideo` for full test session recording
- **AgentCore Gateway registration:** Register this MCP server as an AgentCore Gateway tool for multi-agent access

---

## File Structure

```
codebuild-ios-mcp/
├── pyproject.toml
├── README.md
├── src/
│   └── codebuild_ios_mcp/
│       ├── __init__.py
│       ├── __main__.py          # entry point
│       ├── server.py            # MCP server + tool definitions
│       ├── codebuild_client.py  # boto3 wrapper
│       ├── artifacts.py         # S3 presigned URL generation
│       ├── test_parser.py       # xcresult / test report parsing
│       └── config.py            # env var loading + validation
├── tests/
│   ├── test_server.py
│   ├── test_codebuild_client.py
│   └── fixtures/
│       └── sample_test_report.json
└── buildspec.yaml               # Reference buildspec for customer's repo
```

---

## Quick Start (local testing)

```bash
# 1. Clone and install
git clone <your-repo>
cd codebuild-ios-mcp
pip install -e .

# 2. Set env vars
export CODEBUILD_PROJECT=ios-agent-tests
export ARTIFACTS_BUCKET=my-ios-test-artifacts
export AWS_REGION=us-west-2

# 3. Test with MCP inspector
npx @modelcontextprotocol/inspector python -m codebuild_ios_mcp

# 4. Or wire directly to Claude Code / Kiro
# Add to .mcp.json in your project root
```
