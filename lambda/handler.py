"""codebuild-ios-mcp — AgentCore Gateway Lambda target.

Hosts six MCP tools (ios_test, ios_build_status, list_schemes, get_test_logs,
get_build_log, ios_cancel) behind a single Lambda. Gateway invokes it per tool
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
# Fleet ARNs for per-call compute_size routing. The project binds to MEDIUM by
# default; LARGE is reached via StartBuild fleetOverride. LARGE is empty when the
# large fleet is not enabled in the stack.
FLEET_MEDIUM_ARN = os.environ.get("FLEET_MEDIUM_ARN", "")
FLEET_LARGE_ARN = os.environ.get("FLEET_LARGE_ARN", "")

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
    # Per-call clean build: force a cold compile for this run (throwaway
    # DerivedData) without disturbing the warm build state on the reserved Mac.
    # Use when an incremental build is suspect or for a guaranteed-clean run.
    if args.get("clean_build"):
        env.append({"name": "CLEAN_BUILD", "value": "true", "type": "PLAINTEXT"})
    # Per-call session video: record the whole simulator display to session.mp4
    # (OS-level, independent of any XCUITest capture). Off by default.
    if args.get("record_session"):
        env.append({"name": "RECORD_SESSION", "value": "true", "type": "PLAINTEXT"})
    # Per-call subdir override (multi-app on one shared project: each repo's
    # .xcworkspace/.xcodeproj may live in a different subdir).
    if args.get("project_dir"):
        env.append({"name": "PROJECT_DIR", "value": args["project_dir"], "type": "PLAINTEXT"})

    start = {
        "projectName": PROJECT,
        "sourceVersion": branch,
        "environmentVariablesOverride": env,
    }
    # Multi-app: point one shared project at any GitHub repo per call. Omit `repo`
    # to use the project's configured source (single-app / per-app-project setups).
    repo = args.get("repo")
    if repo:
        start["sourceTypeOverride"] = "GITHUB"
        start["sourceLocationOverride"] = repo

    # Per-call compute size. MEDIUM (default) uses the project's bound fleet; LARGE
    # routes this one build to the large fleet via StartBuild fleetOverride. Both
    # are MAC_ARM with the same image, so only the fleet + compute type change.
    size = (args.get("compute_size") or "medium").lower()
    if size == "large":
        if not FLEET_LARGE_ARN:
            return {
                "status": "ERROR",
                "message": "compute_size=large requested but no large fleet is "
                           "enabled (deploy with enableLarge). Retry with "
                           "compute_size=medium.",
            }
        start["fleetOverride"] = {"fleetArn": FLEET_LARGE_ARN}
        start["computeTypeOverride"] = "BUILD_GENERAL1_LARGE"

    resp = codebuild.start_build(**start)
    build_id = resp["build"]["id"]
    return {
        "status": "IN_PROGRESS",
        "build_id": build_id,
        "message": f"Build started on branch '{branch}'"
                   + (f" (repo {repo})" if repo else "")
                   + f" ({size})"
                   + ". Poll ios_build_status with this build_id.",
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
    phases = _phase_timeline(build)

    if cb_status == "IN_PROGRESS":
        return {"status": "IN_PROGRESS", "build_id": build_id,
                "current_phase": build.get("currentPhase", ""),
                "phases": phases}

    duration = _duration(build)
    summary, failures = _get_test_results(build_id)
    artifacts = _get_artifact_urls(build_id)

    build_errors = []
    status = "SUCCEEDED" if cb_status == "SUCCEEDED" else "FAILED"
    if status == "FAILED" and summary["total"] == 0:
        # Build failed before any test ran. Surface the real cause: CodeBuild
        # phase contexts (e.g. exit 65) PLUS the captured error tail from the
        # build log, so the agent gets actual xcodebuild/clone/dep errors and
        # not just "COMMAND_EXECUTION: exit status 65".
        build_errors = _extract_build_errors(build)
        tail = _get_error_tail(build_id)
        if tail:
            build_errors.append(tail)
        status = "BUILD_ERROR"
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
        "phases": phases,
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
# Tool: get_build_log — raw build output for ANY build, including ones that
# failed before tests ran (compile error, dep resolution, scheme/sim not found,
# bad project_dir). This is the escape hatch get_test_logs can't cover: it keys
# off named test failures, which don't exist when the build never reached tests.
# --------------------------------------------------------------------------- #
def get_build_log(args: dict) -> dict:
    build_id = args["build_id"]
    # Live tail: while the build is running, the final build_output.log/error_tail
    # don't exist yet, so read the tail of the CloudWatch log stream directly. This
    # turns the poll loop into real progress instead of an opaque wait.
    builds = codebuild.batch_get_builds(ids=[build_id]).get("builds", [])
    build = builds[0] if builds else {}
    is_running = build.get("buildStatus") == "IN_PROGRESS"
    if is_running:
        live = _tail_cloudwatch(build, int(args.get("lines", 100)))
        return {
            "build_id": build_id,
            "status": "IN_PROGRESS",
            "current_phase": build.get("currentPhase", ""),
            "live_tail": live,      # most recent CloudWatch log lines, live
            "full_log_url": "",     # not uploaded to S3 until the build ends
        }

    # Completed: serve the focused error tail + presigned full log from S3.
    tail = _get_error_tail(build_id)
    prefix = f"builds/{build_id}"
    full_log_url = ""
    try:
        s3.head_object(Bucket=BUCKET, Key=f"{prefix}/build_output.log")
        full_log_url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": BUCKET, "Key": f"{prefix}/build_output.log"},
            ExpiresIn=PRESIGN_TTL,
        )
    except Exception:
        pass
    if not tail and not full_log_url:
        # Build ended but diagnostics never uploaded (e.g. killed in PROVISIONING).
        # Fall back to whatever CloudWatch captured.
        live = _tail_cloudwatch(build, int(args.get("lines", 100)))
        return {"build_id": build_id, "error_tail": live, "full_log_url": "",
                "message": "No S3 build log; showing CloudWatch tail instead."}
    return {
        "build_id": build_id,
        "error_tail": tail,        # error: lines + last 100 log lines
        "full_log_url": full_log_url,  # presigned full build_output.log
    }


# --------------------------------------------------------------------------- #
# Tool: ios_cancel — stop a running build (StopBuild). Frees the warm fleet when
# the agent realizes a build is wrong/runaway instead of waiting out the 40-min
# timeout. No-op (with a clear message) if the build already finished.
# --------------------------------------------------------------------------- #
def ios_cancel(args: dict) -> dict:
    build_id = args["build_id"]
    builds = codebuild.batch_get_builds(ids=[build_id]).get("builds", [])
    if not builds:
        return {"build_id": build_id, "stopped": False,
                "message": f"No build found for id {build_id}."}
    if builds[0].get("buildStatus") != "IN_PROGRESS":
        return {"build_id": build_id, "stopped": False,
                "status": builds[0].get("buildStatus"),
                "message": "Build already finished; nothing to stop."}
    resp = codebuild.stop_build(id=build_id)
    return {"build_id": build_id, "stopped": True,
            "status": resp.get("build", {}).get("buildStatus", "STOPPED"),
            "message": "Stop requested."}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _duration(build: dict) -> int:
    start = build.get("startTime")
    end = build.get("endTime")
    if start and end:
        return int(end.timestamp() - start.timestamp())
    return 0


def _phase_timeline(build: dict):
    """Flatten CodeBuild's phases[] into a compact, agent-friendly timeline.

    Every build moves SUBMITTED -> QUEUED -> PROVISIONING -> DOWNLOAD_SOURCE ->
    INSTALL -> PRE_BUILD -> BUILD -> POST_BUILD -> UPLOAD_ARTIFACTS -> FINALIZING.
    Returning this lets the agent see WHERE a slow build is (e.g. stuck cloning vs
    compiling) and which phase failed, with per-phase durations.
    """
    out = []
    for p in build.get("phases", []):
        ptype = p.get("phaseType", "")
        item = {
            "phase": ptype,
            "status": p.get("phaseStatus", "IN_PROGRESS"),
            "duration_seconds": p.get("durationInSeconds", 0),
        }
        contexts = [c.get("message") for c in p.get("contexts", []) if c.get("message")]
        if contexts:
            item["context"] = "; ".join(contexts)
        out.append(item)
    return out


def _tail_cloudwatch(build: dict, lines: int = 100) -> str:
    """Return the most recent CloudWatch log lines for a build, live.

    CodeBuild streams logs in real time; the build's logs.groupName/streamName
    point at them. We read the tail so an agent polling mid-build sees progress
    instead of waiting for the end-of-build S3 upload.
    """
    info = build.get("logs", {}) or {}
    group = info.get("groupName")
    stream = info.get("streamName")
    if not group or not stream:
        return ""
    lines = max(1, min(int(lines), 500))
    try:
        resp = logs.get_log_events(
            logGroupName=group,
            logStreamName=stream,
            limit=lines,
            startFromHead=False,
        )
        return "".join(e.get("message", "") for e in resp.get("events", [])).strip()
    except Exception as e:
        return f"(could not read CloudWatch logs: {e})"


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


def _presign_if_exists(key: str) -> str:
    """Presigned GET URL for an S3 key, or '' if the object isn't there."""
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
    except Exception:
        return ""
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": BUCKET, "Key": key}, ExpiresIn=PRESIGN_TTL,
    )


def _get_artifact_urls(build_id: str) -> dict:
    prefix = f"builds/{build_id}"
    logs_url = (f"https://{REGION}.console.aws.amazon.com/cloudwatch/home?region="
                f"{REGION}#logsV2:log-groups/log-group/$252Faws$252Fcodebuild$252F{PROJECT}")
    return {
        # xcresult is uploaded as a zip for presignability (a .xcresult is a dir).
        "xcresult_url": _presign_if_exists(f"{prefix}/TestResults.xcresult.zip"),
        "screenshots": _list_presigned(f"{prefix}/screenshots/"),
        # one bundle of all visual evidence (extracted images + session video):
        # the agent downloads + unzips this to view what ran, no Mac tooling needed.
        "assets_url": _presign_if_exists(f"{prefix}/assets.zip"),
        # whole-session video, only present when ios_test set record_session=true.
        "session_video_url": _presign_if_exists(f"{prefix}/session.mp4"),
        "logs_url": logs_url,
        "build_log_url": _presign_if_exists(f"{prefix}/build_output.log"),
    }


def _get_error_tail(build_id: str, limit: int = 6000) -> str:
    """Read the focused error tail the buildspec wrote (error: lines + last
    100 log lines). Present even when the build failed before any test ran."""
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=f"builds/{build_id}/error_tail.txt")
        text = obj["Body"].read().decode("utf-8", "replace").strip()
        return text[-limit:] if len(text) > limit else text
    except s3.exceptions.NoSuchKey:
        return ""
    except Exception as e:
        return f"(could not read error_tail.txt: {e})"


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
    "get_build_log": get_build_log,
    "ios_cancel": ios_cancel,
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
