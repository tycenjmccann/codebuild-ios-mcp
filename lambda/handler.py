"""codebuild-ios-mcp — AgentCore Gateway Lambda target.

Hosts seven MCP tools (ios_test, ios_build_status, ios_list_builds, list_schemes,
get_test_logs, get_build_log, ios_cancel) behind a single Lambda. Gateway invokes
it per tool call, passing the tool name in the client context and the tool
arguments as the event payload.

Async by design: ios_test starts a CodeBuild run and returns the build_id
immediately. Agents poll ios_build_status until status != IN_PROGRESS. This
keeps every invocation well under Lambda/Gateway timeouts regardless of how long
the macOS build takes.
"""

import json
import os
import re
from datetime import datetime, timezone

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
# A build queued longer than this with NOTHING running on the same fleet is not a
# deep queue: one reserved Mac finishes a build in ~10-15 min, so nothing should
# wait 20 min behind an idle fleet. Two causes, told apart by
# last_finished_seconds_ago: the build was STARVED (the fleet actually ran a build
# within this window, so the instance is alive and CodeBuild's scheduler simply
# skipped the queued build - cancel + resubmit, free) or the instance is WEDGED
# (nothing ran in a long time: unhealthy / out of disk - recycle it). Used by the
# ios_test preflight and the ios_list_builds fleet view. See TEAM-4953.
FLEET_STALL_MINUTES = int(os.environ.get("FLEET_STALL_MINUTES", "20"))

codebuild = boto3.client("codebuild", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)
logs = boto3.client("logs", region_name=REGION)


# --------------------------------------------------------------------------- #
# Tool: ios_test — start a build, return immediately (async).
# --------------------------------------------------------------------------- #
def ios_test(args: dict) -> dict:
    branch = args["branch"]
    scheme = args["scheme"]
    # CodeBuild's DOWNLOAD_SOURCE can fetch a branch/tag or a FULL 40-hex commit
    # SHA; an abbreviated SHA dies with the opaque "git fetch failed with exit
    # status 128" after burning a build slot (TEAM-4921). Reject it here, for free.
    # This also rejects an all-hex branch name (e.g. `deadbeef`) — rare, and the
    # fix for that is an explicit ref, not a looser pattern.
    if re.fullmatch(r"[0-9a-fA-F]{7,39}", branch or ""):
        return {
            "status": "ERROR",
            "reason": "SHORT_SHA",
            "message": f"'{branch}' looks like an abbreviated commit SHA. CodeBuild can "
                       "only check out a branch/tag name or a FULL 40-hex SHA — pass the "
                       "branch name, or the complete SHA (git rev-parse <short>).",
        }
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
    # Test platform: auto (default) picks iOS Simulator when the scheme has one,
    # else native macOS (SwiftUI/AppKit Mac apps). Force with "ios" or "macos".
    if args.get("platform"):
        env.append({"name": "PLATFORM", "value": args["platform"], "type": "PLAINTEXT"})
    # Per-repo warm-cache save floor: skip the S3 re-upload when this run recompiled
    # fewer than N files (just the repo's fixed asset-catalog churn floor, not a real
    # source change). Layer 2 of the save gate; Layer 1 (source hash) runs always.
    if args.get("cache_save_threshold") is not None:
        env.append({"name": "CACHE_SAVE_THRESHOLD",
                    "value": str(int(args["cache_save_threshold"])), "type": "PLAINTEXT"})

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
                "reason": "NO_LARGE_FLEET",
                "message": "compute_size=large requested but no large fleet is "
                           "enabled (deploy with enableLarge). Retry with "
                           "compute_size=medium.",
            }
        start["fleetOverride"] = {"fleetArn": FLEET_LARGE_ARN}
        start["computeTypeOverride"] = "BUILD_GENERAL1_LARGE"
    # Tell the buildspec which fleet this run lands on so it scopes the S3 warm
    # cache key by size. medium and large are separate fleets with separate Macs;
    # a shared cache key makes them restore each other's DerivedData, which fails
    # Swift's incremental validity check -> full recompile + mutual clobber.
    env.append({"name": "COMPUTE_SIZE", "value": size, "type": "PLAINTEXT"})

    # Capacity preflight. A non-ACTIVE or stalled fleet still ACCEPTS StartBuild and
    # then never runs it, so the agent gets a build_id it can poll for hours
    # (TEAM-4921 left running:0 queued:3). Refuse up front instead, and say what to
    # do about it. force=true enqueues anyway; the check is best-effort and never
    # blocks a build because of a read permission or a throttle.
    if not args.get("force"):
        err = _capacity_preflight(size)
        if err:
            return err

    resp = codebuild.start_build(**start)
    build = resp["build"]
    build_id = build["id"]
    # Echo the RESOLVED source so a caller never has to guess what the run
    # actually checked out. `repo`/`project_dir` are optional and fall back to
    # deployment-level defaults that do NOT adapt per-caller; surfacing the
    # resolved values here (and in BUILD_ERROR output) is what turns a silent
    # cross-repo/wrong-subdir mismatch into an obvious one. See issue #2.
    resolved_repo, resolved_dir, resolved_branch = _resolved_source(build)
    return {
        "status": "IN_PROGRESS",
        "build_id": build_id,
        "repo": resolved_repo,
        "project_dir": resolved_dir,
        "branch": resolved_branch,
        "compute_size": size,
        "message": f"Build started: branch '{resolved_branch}', repo {resolved_repo}, "
                   f"project_dir '{resolved_dir}' ({size}). "
                   "Poll ios_build_status with this build_id.",
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
                "compute_size": _compute_size(build),
                "current_phase": build.get("currentPhase", ""),
                # How long this build has waited for a fleet instance. Still rising
                # while current_phase is QUEUED; a value climbing past a few minutes
                # with no other build running means the fleet is stalled, not busy —
                # ios_list_builds' fleets.<size>.stall_kind says starved vs wedged.
                "queued_seconds": _queued_seconds(build),
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
        # Lead with the resolved source. The two most common pre-test failures
        # are a branch that doesn't exist on the (defaulted) repo and a
        # PROJECT_DIR that isn't in the tree; CodeBuild's own messages name
        # neither, so we prepend what was actually attempted. See issue #2.
        resolved_repo, resolved_dir, resolved_branch = _resolved_source(build)
        build_errors.insert(
            0,
            f"Attempted: checkout branch '{resolved_branch}' on repo {resolved_repo}, "
            f"build project_dir '{resolved_dir}'. If this repo/branch/subdir is wrong, "
            f"pass repo / branch / project_dir explicitly to ios_test.",
        )
        status = "BUILD_ERROR"
    if cb_status == "TIMED_OUT":
        status = "TIMED_OUT"
    # Queued timeout: with a queuedTimeout on the project, a build that no fleet
    # instance ever picked up ends with QUEUED as its last non-succeeded phase. That
    # is an infrastructure fault, not a test/compile failure, so report it as
    # BUILD_ERROR and lead with what to do. Keyed on the PHASE rather than on
    # TIMED_OUT, since CodeBuild may label the overall build FAILED instead.
    if status != "SUCCEEDED" and _last_unsucceeded_phase(build) == "QUEUED":
        status = "BUILD_ERROR"
        build_errors.insert(
            0,
            f"Timed out in QUEUED after {_queued_seconds(build) // 60} min: no fleet "
            "instance picked the build up. Resubmit with ios_test first - the build may "
            "simply have been starved (the scheduler skipped it while the instance was "
            "alive), and a resubmit is normally picked up within a minute. Only if the "
            "resubmit also never starts is the instance wedged (unhealthy / disk full) "
            "and in need of recycling - see docs/RUNBOOK-runner-disk-full.md. "
            "ios_list_builds shows the fleet's queue state, including "
            "fleets.<size>.stall_kind.",
        )

    return {
        "status": status,
        "build_id": build_id,
        "compute_size": _compute_size(build),
        "duration_seconds": duration,
        "queued_seconds": _queued_seconds(build),
        "test_summary": summary,
        "failures": failures,
        "artifacts": artifacts,
        "build_errors": build_errors,
        "phases": phases,
        # Self-reported build performance (compile count, cache hit/miss, restore
        # + save seconds, per-phase secs) from the buildspec's metrics.json. Lets
        # a consumer read warm/cold + cache outcome as structured fields instead
        # of grepping the raw log. Empty {} when the build predates metrics.json.
        "metrics": _get_metrics(build_id),
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
# Tool: ios_list_builds — pool/queue visibility. CodeBuild has no per-build
# "which instance" view, so an agent driving several builds is otherwise blind
# to what's RUNNING vs QUEUED on each size's fleet. Returns the most recent
# builds with status/phase/size/timing so the caller can see queue depth.
# --------------------------------------------------------------------------- #
def ios_list_builds(args: dict) -> dict:
    limit = min(int(args.get("limit", 20)), 50)
    # Omit sortOrder: the API already defaults to descending (newest-first), and
    # PASSING sortOrder errors once a project has >100 builds. This stack
    # accumulates many runs, so we rely on the default order.
    ids = codebuild.list_builds_for_project(
        projectName=PROJECT).get("ids", [])[:50]
    if not ids:
        return {"builds": [], "running": 0, "queued": 0, "fleets": _fleets_summary([])}
    builds = codebuild.batch_get_builds(ids=ids).get("builds", [])
    # Per-fleet queue + health view, computed over ALL sizes from this same fetch and
    # independent of `limit`/`compute_size` — so a stalled fleet is visible even when
    # the caller asked for a narrow slice. One BatchGetBuilds either way.
    fleets = _fleets_summary(builds)
    want = args.get("compute_size")  # optional filter: "medium" | "large"
    out, running, queued = [], 0, 0
    for b in builds[:limit]:
        size = _compute_size(b)
        if want and size != want:
            continue
        st = b.get("buildStatus")
        phase = b.get("currentPhase", "")
        if st == "IN_PROGRESS":
            if phase == "QUEUED":
                queued += 1
            else:
                running += 1
        out.append({
            "build_id": b.get("id", ""),
            "status": st,
            "current_phase": phase,
            "compute_size": size,
            "duration_seconds": _duration(b),
            "queued_seconds": _queued_seconds(b),
        })
    # running/queued keep their existing meaning (this slice, after the filter);
    # `fleets` is the whole-project, per-size view.
    return {"builds": out, "running": running, "queued": queued, "fleets": fleets}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _resolved_source(build: dict):
    """Resolve the (repo, project_dir, branch) a build actually used.

    `repo` and `project_dir` are optional on ios_test and fall back to
    deployment-level defaults (a fixed GitHub source + the project's PROJECT_DIR,
    typically '.'), which do NOT adapt to the caller. batch_get_builds /
    start_build both report the effective source after overrides are applied, so
    we read it back rather than echo the request. See issue #2.
    """
    source = build.get("source", {}) or {}
    repo = source.get("location") or "(project default repo)"
    branch = build.get("sourceVersion") or build.get("resolvedSourceVersion") or "(default branch)"
    # PROJECT_DIR shows up in the resolved env only when overridden per-call;
    # otherwise the buildspec's own default ('.') applies.
    project_dir = "(project default, '.' unless set in buildspec)"
    for var in build.get("environment", {}).get("environmentVariables", []):
        if var.get("name") == "PROJECT_DIR" and var.get("value"):
            project_dir = var["value"]
            break
    return repo, project_dir, branch


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


def _compute_size(build: dict) -> str:
    """Map the build's resolved compute type to the friendly size the caller
    passed to ios_test, so status echoes WHERE it ran (medium vs large)."""
    ct = (build.get("environment", {}) or {}).get("computeType", "")
    return {"BUILD_GENERAL1_LARGE": "large",
            "BUILD_GENERAL1_MEDIUM": "medium"}.get(ct, ct or "unknown")


def _fleet_arn(size: str) -> str:
    """Fleet ARN for a friendly size. '' when that fleet isn't deployed."""
    return FLEET_LARGE_ARN if size == "large" else FLEET_MEDIUM_ARN


def _fleet_status(arn: str):
    """(statusCode, message) for one fleet ARN; (None, msg) when it no longer exists.

    Needs codebuild:BatchGetFleets. statusCode is ACTIVE for a healthy fleet;
    CREATING/UPDATING/ROTATING are transient, CREATE_FAILED /
    UPDATE_ROLLBACK_FAILED / PENDING_DELETION / DELETING are not.
    """
    fleets = codebuild.batch_get_fleets(names=[arn]).get("fleets", [])
    if not fleets:
        return None, f"fleet not found ({arn})"
    st = fleets[0].get("status", {}) or {}
    msg = "; ".join(x for x in (st.get("context"), st.get("message")) if x)
    return st.get("statusCode", ""), msg


def _queued_seconds(build: dict) -> int:
    """How long a build waited (or has been waiting) in QUEUED.

    CodeBuild reports durationInSeconds once the phase ends; while the build is
    still sitting there the phase has no end, so measure against its startTime.
    0 when the build never queued.
    """
    for p in build.get("phases", []):
        if p.get("phaseType") != "QUEUED":
            continue
        if p.get("endTime"):
            return int(p.get("durationInSeconds") or 0)
        start = p.get("startTime") or build.get("startTime")
        if not start:
            return 0
        return max(0, int(datetime.now(timezone.utc).timestamp() - start.timestamp()))
    return 0


def _ran_on_instance(build: dict) -> bool:
    """True when a build actually left QUEUED and executed on a fleet instance.

    An `endTime` alone does NOT prove the Mac was alive: a build cancelled with
    ios_cancel, or killed by queuedTimeout, ends while still QUEUED and never
    touches the instance. Counting those as "the fleet finished work recently"
    would relabel a genuinely wedged fleet as merely starved right after an
    operator cancels the stalled builds — exactly when the diagnosis matters. Any
    phase past SUBMITTED/QUEUED means CodeBuild handed the build to an instance.
    """
    for p in build.get("phases", []):
        if p.get("phaseType") not in ("SUBMITTED", "QUEUED"):
            return True
    return False


def _queue_snapshot(builds=None) -> dict:
    """Per-size queue state: running, queued, oldest_queued_seconds, stalled,
    stall_kind, last_finished_seconds_ago, queued_build_ids.

    `stalled` = builds are QUEUED, NOTHING is running on that size, and the oldest
    has waited longer than FLEET_STALL_MINUTES — i.e. nothing is going to pick these
    builds up. `stall_kind` says why: "starved" when the fleet demonstrably RAN a
    build within FLEET_STALL_MINUTES (instance alive, CodeBuild's scheduler skipped
    the queued build — cancel + resubmit fixes it), else "wedged" (unhealthy / out of
    disk — recycle it); None when not stalled. `last_finished_seconds_ago` is the age
    of the most recent finished build of that size and counts only builds that got
    past QUEUED (see _ran_on_instance) — a build cancelled or timed out while still
    QUEUED proves nothing about the Mac. Pass an already-fetched build list to skip a
    second ListBuildsForProject + BatchGetBuilds round trip; shared by the ios_test
    preflight and the ios_list_builds fleet view.
    """
    snap = {}
    for size in ("medium", "large"):
        if size == "large" and not FLEET_LARGE_ARN:
            continue    # not deployed; don't report a fleet that doesn't exist
        snap[size] = {"running": 0, "queued": 0, "oldest_queued_seconds": 0,
                      "stalled": False, "stall_kind": None,
                      "last_finished_seconds_ago": None, "queued_build_ids": []}
    if builds is None:
        ids = codebuild.list_builds_for_project(projectName=PROJECT).get("ids", [])[:50]
        builds = codebuild.batch_get_builds(ids=ids).get("builds", []) if ids else []
    for b in builds:
        entry = snap.get(_compute_size(b))
        if entry is None:
            continue
        if b.get("buildStatus") != "IN_PROGRESS":
            # The most recent build that actually RAN on this size is proof the
            # instance was alive that recently — the whole starved-vs-wedged signal,
            # read off the BatchGetBuilds response we already have (no extra call).
            end = b.get("endTime")
            if end and _ran_on_instance(b):
                ago = max(0, int(datetime.now(timezone.utc).timestamp() - end.timestamp()))
                prev = entry["last_finished_seconds_ago"]
                entry["last_finished_seconds_ago"] = ago if prev is None else min(prev, ago)
            continue
        if b.get("currentPhase") == "QUEUED":
            entry["queued"] += 1
            entry["queued_build_ids"].append(b.get("id", ""))
            entry["oldest_queued_seconds"] = max(entry["oldest_queued_seconds"],
                                                 _queued_seconds(b))
        else:
            entry["running"] += 1
    for entry in snap.values():
        entry["stalled"] = (entry["queued"] > 0 and entry["running"] == 0
                            and entry["oldest_queued_seconds"] > FLEET_STALL_MINUTES * 60)
        last = entry["last_finished_seconds_ago"]
        entry["stall_kind"] = (None if not entry["stalled"] else
                               "starved" if last is not None
                               and last <= FLEET_STALL_MINUTES * 60
                               else "wedged")
    return snap


def _fleets_summary(builds=None) -> dict:
    """_queue_snapshot plus each fleet's own health, keyed by size.

    fleet_status is 'unreadable' when codebuild:BatchGetFleets is denied — the queue
    numbers still hold, since they need no permission beyond what status already uses.
    """
    snap = _queue_snapshot(builds)
    for size, entry in snap.items():
        arn = _fleet_arn(size)
        if not arn:
            continue
        try:
            code, _msg = _fleet_status(arn)
            entry["fleet_status"] = code or "NOT_FOUND"
        except Exception as e:
            print(f"fleet status unreadable for {size}: {type(e).__name__}: {e}")
            entry["fleet_status"] = "unreadable"
    return snap


def _capacity_preflight(size: str):
    """Error dict when starting a build on `size` would just park it in QUEUED
    forever, else None.

    Best-effort by design: this tool must never become unusable because a read
    permission is missing or an API throttled, so each check swallows its own
    exceptions (logged to the Lambda log) and the build proceeds. The two checks are
    independent — a denied BatchGetFleets still leaves the stall detection working.
    """
    try:
        arn = _fleet_arn(size)
        if arn:
            code, msg = _fleet_status(arn)
            if code != "ACTIVE":
                shown = code or "NOT_FOUND"
                transient = shown in ("CREATING", "UPDATING", "ROTATING")
                return {
                    "status": "ERROR",
                    "reason": "INSUFFICIENT_CAPACITY",
                    "compute_size": size,
                    "fleet_status": shown,
                    "message": f"The {size} MAC_ARM fleet is not ACTIVE (status {shown})"
                               + (f": {msg}" if msg else "")
                               + ". A build started now would sit in QUEUED instead of running.",
                    "remediation": ("The fleet is still coming up - retry in a few minutes."
                                    if transient else
                                    "The fleet needs operator attention (recycle or recreate it); "
                                    "see docs/RUNBOOK-runner-disk-full.md.")
                                   + " Pass force=true to enqueue anyway.",
                }
    except Exception as e:
        print(f"fleet status check skipped: {type(e).__name__}: {e}")

    try:
        queue = _queue_snapshot().get(size) or {}
    except Exception as e:
        print(f"queue stall check skipped: {type(e).__name__}: {e}")
        return None
    if queue.get("stalled"):
        # Same refusal either way — the build would never start — but the CAUSE decides
        # the fix, and they cost wildly different amounts. starved: the Mac is alive
        # (it ran a build within FLEET_STALL_MINUTES) and the scheduler skipped these
        # builds, so cancel + resubmit, free. wedged: nothing has run in a long time,
        # so the instance needs recycling (new ~24h lease, human-run cdk deploy).
        last = queue.get("last_finished_seconds_ago")
        starved = queue.get("stall_kind") == "starved"
        preamble = (f"The {size} fleet looks stalled: {queue['queued']} build(s) QUEUED, "
                    f"none running, oldest waiting {queue['oldest_queued_seconds'] // 60} min "
                    f"(> {FLEET_STALL_MINUTES} min). ")
        if starved:
            detail = (f"The fleet finished a build {(last or 0) // 60} min ago, so the instance "
                      "is alive: CodeBuild's scheduler skipped the queued build(s) (starved), "
                      "and this build would queue behind them indefinitely.")
            remediation = ("Cancel the starved build(s) with ios_cancel and resubmit with "
                           "ios_test - they are normally picked up within a minute. Recycle the "
                           "instance only if a resubmitted build also stalls - see "
                           "docs/RUNBOOK-runner-disk-full.md. Pass force=true to enqueue anyway.")
        else:
            detail = ("The instance is wedged (unhealthy or out of disk), so this build would "
                      "queue behind them indefinitely.")
            remediation = ("Inspect the stalled builds with get_build_log, cancel them with "
                           "ios_cancel, and recycle the fleet instance - see "
                           "docs/RUNBOOK-runner-disk-full.md. Pass force=true to enqueue anyway.")
        return {
            "status": "ERROR",
            "reason": "INSUFFICIENT_CAPACITY",
            "compute_size": size,
            "fleet_status": "ACTIVE",
            "stalled_builds": queue.get("queued_build_ids", []),
            "oldest_queued_seconds": queue.get("oldest_queued_seconds", 0),
            "stall_kind": queue.get("stall_kind"),
            "last_finished_seconds_ago": last,
            "message": preamble + detail,
            "remediation": remediation,
        }
    return None


def _last_unsucceeded_phase(build: dict) -> str:
    """phaseType of the last phase that did not SUCCEED, '' when all did.

    Identifies WHERE a build died without depending on how CodeBuild labels the
    overall buildStatus (a queued timeout can surface as FAILED or TIMED_OUT).
    """
    for p in reversed(build.get("phases", [])):
        st = p.get("phaseStatus")
        if st and st != "SUCCEEDED":
            return p.get("phaseType", "")
    return ""


def _get_metrics(build_id: str) -> dict:
    """Read the buildspec's self-reported metrics.json (compile count, cache
    state, restore/save secs, per-phase secs, test timing). Empty when absent
    (build predates metrics, or failed before writing it)."""
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=f"builds/{build_id}/metrics.json")
        return json.loads(obj["Body"].read())
    except s3.exceptions.NoSuchKey:
        return {}
    except Exception as e:
        return {"error": str(e)}


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
    "ios_list_builds": ios_list_builds,
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
