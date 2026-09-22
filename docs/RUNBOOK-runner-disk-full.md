# Runbook: runner disk full / stalled fleet (TEAM-4921, TEAM-4953)

A reserved `MAC_ARM` instance has a persistent disk with several unbounded
consumers: one warm-state tree per repo+subdir+size, the SwiftPM global cache,
leaked `clean_build` scratch dirs, and stale CodeBuild source roots. Left
unchecked, the disk fills, every build step ENOSPCs, and the fleet can end up
wedged with nothing running. This is what happened to build
`ios-agent-tests:08d9c28a` (medium fleet, repo `tycenjmccann/talk-to-me`).

Incident notes:

- `ios-agent-tests:08d9c28a` — the original fill: ENOSPC on everything for ~6 min,
  then the run published its own wreckage as the warm cache.
- `ios-agent-tests:fd17a151` (2026-09-21, ~22:5x UTC) — a **fresh** medium
  instance restored that poisoned cache and SwiftPM died on the half-written bare
  repos inside the restored `SourcePackages`: repeated `error: packfile
  .../SourcePackages/repositories/aws-sdk-swift-<hash>/objects/pack/pack-<id>.pack
  does not match index`, then `fatal: update_ref failed ... nonexistent object`,
  surfacing as `xcodebuild: error: Could not resolve package dependencies`. So a
  poisoned restore is not just "cold" — its `SourcePackages` are corrupt, which is
  why the build now discards them (below). Until that fix is deployed or the key
  is deleted, `ios_test(clean_build: true)` bypasses the problem: a clean build
  resolves into throwaway `/tmp` `SourcePackages` and never reads the poison.
- `ios-agent-tests:a77946b2` (medium) and `bec57a76` (large), 2026-09-22 — **not a
  disk problem at all.** `a77946b2` sat `QUEUED` 39 min while two *newer* medium
  builds (e.g. `917d4503`, queued 91 s) were picked up and completed around it, and
  the instance then idled ~8 min without taking it; `bec57a76` rode the same pattern
  to the 52-min `queuedTimeout` and came back as `BUILD_ERROR`. `ios_cancel` plus the
  same `ios_test` call again → `91cfdc39`, picked up in under 60 s with
  `queued_seconds=0`. The `stalled: true` signal was correct (nothing was going to
  pick that build up) but the old "wedged instance / recycle" diagnosis was wrong:
  the instance was alive and had just finished builds. The build was **starved** by
  CodeBuild's fleet scheduler, and the free fix is step 3 below.

## Symptoms

- `build_output.log` / `error_tail.txt` contains `No space left on device`.
- `ios_build_status` / `get_build_log` `build_errors` contains
  `ERROR: runner disk full ... - recycle the fleet instance`.
- `ios_list_builds` shows `fleets.<size>.stalled: true` (builds `QUEUED`,
  nothing `running`, oldest queued past `FLEET_STALL_MINUTES`, default 20 min).
  That means nothing is going to pick those builds up, from **two different
  causes**, and `fleets.<size>.stall_kind` names which:
  - `"starved"` — the instance is **alive**: `last_finished_seconds_ago` is inside
    `FLEET_STALL_MINUTES`, i.e. the same fleet actually ran a build that recently, and
    `builds[]` shows a `SUCCEEDED`/`FAILED`/`STOPPED` build of the same `compute_size`
    with a recent end. CodeBuild's scheduler simply skipped the queued build. Go to
    step 3 — it is free.
  - `"wedged"` — nothing of that size has run in a long time
    (`last_finished_seconds_ago` is null or older than `FLEET_STALL_MINUTES`): disk
    full or unhealthy. That is the recycle case, step 4.
  - Only a build that got *past* `QUEUED` counts as "the fleet ran something", so a
    build you cancelled while it was still queued does not make a wedged fleet look
    starved.
- `ios_test` returns `reason: "INSUFFICIENT_CAPACITY"` — either the fleet is
  not `ACTIVE`, or the queue looks stalled. The stall response also carries
  `stall_kind` and `last_finished_seconds_ago`, and its `remediation` string already
  names the right fix for that cause.
- `ios_build_status` returns `status: "BUILD_ERROR"` with a `build_errors[0]`
  starting `Timed out in QUEUED` — no instance ever picked the build up before
  the project's `queuedTimeout` (default 60 min). Either cause can do this, so
  resubmit (step 3) before recycling (step 4).
- The build dies immediately with `ERROR: could not fetch
  s3://<bucket>/tooling/ios-build.sh` (followed by a `df -Pk` dump, in the
  CloudWatch log / `get_build_log` tail — no `build_output.log` is uploaded in
  this case). The buildspec is only a stub; the build body is fetched from S3.
  Two causes: the tooling was never deployed (run `cdk deploy`, which uploads
  `tooling/` via the BucketDeployment), or the runner is so full it cannot hold a
  31 KB download — which is past what the in-script guard can fix, so go to step 4.

## What the guard now does

`tooling/ios-build.sh`'s disk guard runs on every build (not just when disk state
already existed), measures `$HOME` (the Data volume — `/` is the sealed APFS
System volume and under-reports), and reclaims in tiers, cheapest loss first,
re-measuring between each and stopping as soon as it's healthy:

| Tier | Reclaims | Cost of losing it |
| --- | --- | --- |
| 1 | Leaked `/tmp/ios-mcp-clean-*` from old `clean_build` runs | None — never reused |
| 2 | Stale `/tmp/codebuild-*` source roots (>60 min old, never this build's own) | None |
| 3 | SwiftPM + Xcode caches | One SPM re-fetch on the next cold resolve |
| 4 | Stock `~/Library/Developer/Xcode/DerivedData` | None — we never build there |
| 5 | Simulator caches/logs, `simctl delete unavailable` | None |
| 6 | Least-recently-built warm state (other repos), skipping this run's own | One cold compile for the evicted repo |

If that still isn't enough, the build **fails in seconds** with
`ERROR: runner disk full ... - recycle the fleet instance` instead of ENOSPCing
opaquely for minutes. A run that hit ENOSPC, or produced no
`TestResults.xcresult`, or ends with less free space than the floor, never
publishes its state as the warm cache — see the save-gate commit
(`fix(buildspec): never publish warm cache from an ENOSPC or result-less run`).

## Operator steps

### 1. Re-run one build

The guard now reclaims and self-heals on its own, and fails fast (seconds, not
minutes) if it genuinely can't. Try a normal `ios_test` call first — most of
the time this is enough and nothing else below is needed.

### 2. Delete a poisoned cache key (only if a bad tar is already in S3)

Before this fix, a broken run could publish its own wreckage as the warm
cache. The incident's exact poisoned keys, in bucket
`ios-agent-test-artifacts-<account>`:

```bash
aws s3 rm s3://ios-agent-test-artifacts-<account>/warm-cache/https___github.com_tycenjmccann_talk-to-me.git_._medium.tar.gz
aws s3 rm s3://ios-agent-test-artifacts-<account>/warm-cache/https___github.com_tycenjmccann_talk-to-me.git_._medium.hash
```

General rule for deriving the key for any repo/subdir/size:

```bash
printf '%s' "<repo-url>|<project_dir>|<size>" | tr -c 'A-Za-z0-9._-' '_'
```

e.g. `https://github.com/tycenjmccann/talk-to-me.git` + `.` + `medium` →
`https___github.com_tycenjmccann_talk-to-me.git_._medium`. Append `.tar.gz`
and `.hash` for the two objects under `warm-cache/`.

After this fix, a poisoned restore (no `DerivedData/Build` in the tar) is
detected and self-heals automatically: the build **deletes the restored
`SourcePackages` and `DerivedData`** — they are corrupt, not merely stale, and
SwiftPM fails outright on them (`packfile ... does not match index`, see
`fd17a151` above) — keeps `src/` as an rsync baseline, resolves and compiles
cold, and reseeds S3 even at the same commit. Manual deletion is only needed for
cache poisoned by an *older* build script, or to force a clean reseed sooner
than the next build.

### 3. Starved `QUEUED` build → `ios_cancel` the build(s) and resubmit with `ios_test`

The cheapest remediation on this page, and the right one whenever the instance is
still alive. **How to identify it:** a build sits `QUEUED` past
`FLEET_STALL_MINUTES` while *newer* builds on the same fleet were picked up and
completed, or the instance is demonstrably idle and simply isn't taking it —
`fleets.<size>.stall_kind` reads `"starved"` and
`fleets.<size>.last_finished_seconds_ago` is small. CodeBuild's fleet scheduler
skipped the build; nothing is wrong with the Mac.

The exact calls — cancel every id in `fleets.<size>.queued_build_ids` (the same ids
appear as `stalled_builds` in the `INSUFFICIENT_CAPACITY` refusal), then re-issue the
**same** `ios_test` call that produced the starved build:

```text
ios_cancel(build_id: "ios-agent-tests:a77946b2")
ios_test(branch: "...", scheme: "...", ...)   # identical arguments
```

Expected outcome: the resubmitted build is picked up in **under 60 s** with
`queued_seconds` ≈ 0 (this is exactly what `91cfdc39` did). The cancel is needed
first because the starved builds keep the fleet looking stalled and the preflight
will refuse the resubmit otherwise.

This costs **nothing** — no fleet recycle, no `cdk deploy`, no new ~24h lease, no
warm state lost. Escalate to step 4 only when **nothing** on that fleet has run in
more than `FLEET_STALL_MINUTES` (`stall_kind: "wedged"`) **and** a resubmitted build
also stalls.

### 4. Recycle the fleet instance (only if the guard's own fail-fast keeps firing)

There is **no documented per-instance reboot API for reserved `MAC_ARM`
fleets** — don't invent one. Two real options, in order of preference:

- `aws codebuild update-fleet` (e.g. changing `baseCapacity` or the image)
  *appears* to rotate instances (fleet goes `ROTATING`) — **verify in the
  console** before relying on this; it is not a documented guarantee.
- Delete and recreate the fleet. This definitely replaces the Mac, but starts
  a new ~24h minimum lease (cost) and requires `cdk deploy` — **the human
  operator's step, never run by an agent.**

### 5. Post-deploy check: is `BatchGetFleets` actually readable?

The Lambda's capacity preflight needs `codebuild:BatchGetFleets` on the fleet
ARNs (granted in `lib/codebuild-ios-mcp-stack.ts`). If that action turns out
to be `*`-only in this account's IAM, the grant fails closed and the
preflight falls back to stall detection alone (which needs no new
permission) — `ios_test`/`ios_list_builds` keep working, just with a less
precise signal. Confirm after deploying:

```bash
aws lambda invoke --function-name codebuild-ios-mcp \
  --cli-binary-format raw-in-base64-out \
  --payload '{"tool":"ios_list_builds","arguments":{}}' out.json && cat out.json
# fleets.<size>.fleet_status should read ACTIVE (or a real status code), not "unreadable"
```

## Cost note

Every fix above is free to *apply* (buildspec/Lambda/CDK changes cost nothing
until deployed), but recreating a fleet always restarts the ~24h minimum
lease at ~$25-30/day/instance. Steps 1, 2 and 3 are all free — work through them in
order (re-run, then poisoned-cache delete, then cancel + resubmit for a starved
build) before reaching for step 4. **Only the human operator runs `cdk deploy`** —
an agent proposing this runbook's steps should stop short of deploying and hand the
command back.
