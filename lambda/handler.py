"""codebuild-ios-mcp — AgentCore Gateway Lambda target.

Hosts four MCP tools (ios_test, ios_build_status, list_schemes, get_test_logs)
behind a single Lambda. AgentCore Gateway invokes this function once per tool
call, passing the tool name in the client context and the tool arguments as the
event payload.

Async by design: ios_test starts a CodeBuild run and returns the build_id
immediately. Agents poll ios_build_status until status != IN_PROGRESS. This
keeps every invocation well under Lambda/Gateway timeouts regardless of how long
the macOS build takes.
"""

import json
import os

import boto3

REGION = os.environ.get("AWS_REGION", "us-east-1")
PROJECT = os.environ["CODEBUILD_PROJECT"]
BUCKET = os.environ["ARTIFACTS_BUCKET"]
REPORT_GROUP_ARN = os.environ.get("REPORT_GROUP_ARN", "")
PRESIGN_TTL = int(os.environ.get("PRESIGN_TTL_SEC", "3600"))

codebuild = boto3.client("codebuild", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)


# --------------------------------------------------------------------------- #
# Tool: ios_test — start a build, return immediately (async).
# --------------------------------------------------------------------------- #
def ios_test(args: dict) -> dict:
    branch = args["branch"]
    scheme = args["scheme"]
    env = [
        {"name": "SCHEME", "value": scheme, "type": "PLAINTEXT"},
        {"name": "DEVICE", "value": args.get("device", "iPhone 17"), "type": "PLAINTEXT"},
        {"name": "OS_VERSION", "value": args.get("os_version", "latest"), "type": "PLAINTEXT"},
    ]
    if args.get("test_plan"):
        env.append({"name": "TEST_PLAN", "value": args["test_plan"], "type": "PLAINTEXT"})

    resp = codebuild.start_build(
        projectName=PROJECT,
        sourceVersion=branch,
        environmentVariablesOverride=env,
    )
    build_id = resp["build"]["id"]
    return {
        "status": "IN_PROGRESS",
        "build_id": build_id,
        "message": f"Build started on branch '{branch}'. Poll ios_build_status with this build_id.",
    }


# --------------------------------------------------------------------------- #
# Tool: ios_build_status — the workhorse. Resolves a build to structured result.
# --------------------------------------------------------------------------- #
def ios_build_status(args: dict) -> dict:
    build_id = args["build_id"]
    builds = codebuild.batch_get_builds(ids=[build_id]).get("builds", [])
    if not builds:
        return {"status": "BUILD_ERROR", "build_id": build_id,
                "build_errors": [f"No build found for id {build_id}"]}
    build = builds[0]
    cb_status = build["buildStatus"]  # IN_PROGRESS | SUCCEEDED | FAILED | FAULT | STOPPED | TIMED_OUT

    if cb_status == "IN_PROGRESS":
        return {"status": "IN_PROGRESS", "build_id": build_id,
                "current_phase": build.get("currentPhase", "")}

    duration = _duration(build)
    summary, failures = _get_test_results(build_id)
    artifacts = _get_artifact_urls(build_id)

    build_errors = []
    status = "SUCCEEDED" if cb_status == "SUCCEEDED" else "FAILED"
    if status == "FAILED" and summary["total"] == 0:
        build_errors = _extract_build_errors(build)
        status = "BUILD_ERROR" if build_errors else "FAILED"
    if cb_status == "TIMED_OUT":
        status = "TIMED_OUT"

    return {
        "status": status,
        "build_id": build_id,
        "duration_seconds": duration,
        "test_summary": summary,
        "failures": failures,
        "artifacts": artifacts,
        "build_errors": build_errors,
    }


# --------------------------------------------------------------------------- #
# Tool: list_schemes — enumerate schemes from a prior list-schemes build, or
# return the project default. Cheap heuristic: read schemes.json from S3 if a
# build published one; otherwise return the configured default scheme only.
# --------------------------------------------------------------------------- #
def list_schemes(args: dict) -> dict:
    branch = args.get("branch", "main")
    key = f"schemes/{branch}.json"
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=key)
        data = json.loads(obj["Body"].read())
        return {"schemes": data.get("schemes", []),
                "default_scheme": data.get("default_scheme", "")}
    except s3.exceptions.NoSuchKey:
        return {"schemes": [], "default_scheme": "",
                "message": f"No scheme manifest for '{branch}'. The buildspec "
                           "publishes schemes/<branch>.json on each run."}


# --------------------------------------------------------------------------- #
# Tool: get_test_logs — detail for one failed test.
# --------------------------------------------------------------------------- #
def get_test_logs(args: dict) -> dict:
    build_id = args["build_id"]
    test_name = args["test_name"]
    _, failures = _get_test_results(build_id)
    match = next((f for f in failures if f["test_name"] == test_name), None)
    screenshots = _list_presigned(f"builds/{build_id}/screenshots/")
    if not match:
        return {"test_name": test_name, "full_output": "",
                "message": f"No failure record for '{test_name}' in build {build_id}.",
                "screenshots": screenshots}
    return {
        "test_name": match["test_name"],
        "class_name": match["class_name"],
        "full_output": match["message"],
        "duration_ms": match["duration_ms"],
        "screenshots": screenshots,
    }


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _duration(build: dict) -> int:
    start = build.get("startTime")
    end = build.get("endTime")
    if start and end:
        return int(end.timestamp() - start.timestamp())
    return 0


def _get_test_results(build_id: str):
    """Read the authoritative summary.json the buildspec wrote to S3.

    We deliberately do NOT use CodeBuild's Test Reports API: its JUnit parser
    silently ingests 0 cases for valid xcresult-derived files. summary.json is
    produced by tooling/xcresult_to_junit.py straight from the xcresult.
    """
    summary = {"total": 0, "passed": 0, "failed": 0, "skipped": 0}
    failures = []
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=f"builds/{build_id}/summary.json")
        data = json.loads(obj["Body"].read())
        for k in ("total", "passed", "failed", "skipped"):
            summary[k] = int(data.get(k, 0))
        failures = data.get("failures", [])
    except s3.exceptions.NoSuchKey:
        summary["error"] = "summary.json not found (build may have failed before tests ran)"
    except Exception as e:
        summary["error"] = str(e)
    return summary, failures


def _list_presigned(prefix: str):
    urls = []
    resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
    for obj in resp.get("Contents", []):
        urls.append(s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": obj["Key"]},
            ExpiresIn=PRESIGN_TTL,
        ))
    return urls


def _get_artifact_urls(build_id: str) -> dict:
    prefix = f"builds/{build_id}"
    xcresult_url = ""
    # xcresult is uploaded as a zip for presignability (a .xcresult is a dir).
    try:
        s3.head_object(Bucket=BUCKET, Key=f"{prefix}/TestResults.xcresult.zip")
        xcresult_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": f"{prefix}/TestResults.xcresult.zip"},
            ExpiresIn=PRESIGN_TTL,
        )
    except Exception:
        pass
    logs_url = (f"https://{REGION}.console.aws.amazon.com/cloudwatch/home?region="
                f"{REGION}#logsV2:log-groups/log-group/$252Faws$252Fcodebuild$252F{PROJECT}")
    return {
        "xcresult_url": xcresult_url,
        "screenshots": _list_presigned(f"{prefix}/screenshots/"),
        "logs_url": logs_url,
    }


def _extract_build_errors(build: dict):
    errors = []
    for phase in build.get("phases", []):
        if phase.get("phaseStatus") in ("FAILED", "FAULT"):
            for ctx in phase.get("contexts", []):
                msg = ctx.get("message")
                if msg:
                    errors.append(f"{phase.get('phaseType', '')}: {msg}")
    return errors


# --------------------------------------------------------------------------- #
# Gateway dispatch
# --------------------------------------------------------------------------- #
TOOLS = {
    "ios_test": ios_test,
    "ios_build_status": ios_build_status,
    "list_schemes": list_schemes,
    "get_test_logs": get_test_logs,
}


def _tool_name(context) -> str:
    """AgentCore Gateway passes the tool name in clientContext.custom.

    The name is prefixed with the target name: '<target>___<tool>'.
    """
    raw = ""
    cc = getattr(context, "client_context", None)
    if cc and getattr(cc, "custom", None):
        raw = cc.custom.get("bedrockAgentCoreToolName", "")
    return raw.split("___")[-1] if raw else raw


def handler(event, context):
    name = _tool_name(context)
    if name not in TOOLS:
        # Allow direct invocation/testing: {"tool": "...", "arguments": {...}}
        name = event.get("tool", name)
        args = event.get("arguments", event)
    else:
        args = event if isinstance(event, dict) else {}
    fn = TOOLS.get(name)
    if not fn:
        return {"error": f"Unknown tool '{name}'. Available: {list(TOOLS)}"}
    try:
        return fn(args)
    except KeyError as e:
        return {"error": f"Missing required argument: {e}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
