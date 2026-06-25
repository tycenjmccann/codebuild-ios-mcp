# Performance: what to expect from the codebuild-ios MCP

iOS build + test on AWS CodeBuild reserved-capacity **MAC_ARM** fleets (Apple
M2). This is the published performance contract: representative timings, what
drives them, and how to size a fleet so you are not stuck in a queue.

> ## 📇 Battle card
>
> | Size | Cache state | Queue | Build | Test | **Total** |
> |---|---|---|---|---|---|
> | **Large** | Cold (first build) | 0 | ~12 min | ~4 min | **~16 min** |
> | **Large** | S3 cache restored | 0 | ~10 min | ~4 min | **~14 min** |
> | **Large** | Warm, same instance | 0 | ~8 min | ~4 min | **~12 min** |
> | **Medium** | Cold (first build) | 0 | ~22 min | ~4 min | **~26 min** |
> | **Medium** | S3 cache restored | 0 | ~10 min | ~4 min | **~14 min** |
> | **Medium** | Warm, same instance | 0 | ~9 min | ~4 min | **~13 min** |
> | Local (reference) | Warm | 0 | — | ~2 min | **~6 min** |
>
> Queue = 0 assumes a free Mac. On a busy fleet add **~13 min per build already
> ahead of you** (capacity = 1 → one build at a time). See
> [Capacity](#capacity--queueing-read-this).
>
> Reference workload: a real iOS UI test suite on a large app — Swift Package
> Manager dependency graph (Amplify + LiveKit + AWS SDK), **~2,900 Swift files
> on a cold compile**. Your numbers scale with your app's size and dependencies.
>
> **Why the rows differ:** test time is fixed (~4 min) everywhere — the build is
> the only variable. Cold recompiles all ~2,900 files; warm recompiles ~50–110.
> Large beats medium only on a cold compile (more cores); once warm they match.
>
> **What to expect**
> - **Test time is constant (~230–245s) no matter where it runs.** Cloud
>   overhead is build + queue, never the tests themselves.
> - **Warm ≈ local + a few minutes.** The first build on a given Mac/size is
>   cold; every build after that is warm.
> - **Large is faster than medium** — ~45% faster cold, ~15% warm (more cores
>   absorb the big compile). Test time is the same on both.
> - **Warm re-runs pay no cache tax.** A re-run with unchanged source uploads
>   nothing (0s), down from the ~160–200s every warm build used to spend.
> - **Queue wait is separate and is a function of YOUR fleet capacity**, not the
>   service. See below before you panic about a slow run.

---

## How warm builds work

CodeBuild checks each run out to a fresh `/tmp/codebuild-<uuid>/`. Swift bakes
the absolute working-directory path into its incremental state, so a moving
source directory means a full recompile every time — even with warm DerivedData.

The MCP buildspec fixes this in two layers:

1. **Stable path.** The checkout is mirrored (rsync `--checksum`, mtimes
   preserved) into a fixed per-repo path under `$HOME/ios-mcp-state`. Swift
   incremental survives across runs → only genuinely changed files recompile.
2. **S3-backed, size-scoped cache.** Warm state (DerivedData + resolved SPM) is
   tarred to `s3://<bucket>/warm-cache/<repo>_<size>.tar.gz`. A cold instance
   restores it; cross-instance warm works within a size. The key is scoped by
   compute size because medium and large are separate fleets — a large Mac
   restoring a medium tarball (or vice versa) fails Swift's validity check and
   recompiles everything, so each size keeps its own cache.

**Save gate (no warm-run tax).** The tar + upload costs ~160–200s, so it only
runs when it buys something. Two independent gates, either can veto:
- **Layer 1 — commit SHA.** The build is a checkout at a known git commit; if
  the commit is unchanged from the cached state, nothing is re-uploaded.
- **Layer 2 — compile floor (`cache_save_threshold`).** Optional per-repo
  backstop: if a warm run recompiled fewer files than the repo's known churn
  floor, skip the save even if Layer 1 disagreed.

Seeding always wins: an empty S3 cache is populated once so siblings can restore.

Across repeated warm runs on both fleet sizes, an unchanged commit recompiles
only a small floor (~100 files) and the save gate **skips the upload entirely**
(`source unchanged`, **0s**) — versus the ~160–200s every warm build spent
re-uploading the cache before the gate existed.

---

## Capacity & queueing (read this)

**The build is fast; the queue is whatever you provision.** A reserved fleet
with `baseCapacity = N` runs **exactly N builds at once** (1 build per dedicated
Mac). Build N+1 waits for a free instance.

Hard constraints specific to Mac (all documented AWS behavior, not bugs):
- **Mac cannot overflow to on-demand** — on-demand compute has no macOS, so
  excess builds **always queue**, they never burst onto extra capacity.
- **Mac scale-out is slow and leased.** Adding an instance takes ~6–20 min to
  launch and carries EC2 Mac's **24-hour minimum host lease** (billed even after
  scale-in) plus a 50 min–3 hr host scrub on teardown. You cannot burst Macs for
  short spikes — you provision steady-state.
- Default per-fleet Mac-ARM concurrency quota is **1** and is **adjustable on
  request**.

Once builds are actually lined up, dispatch latency between them is **~30–90s**
(prev build ends → next picks up on the same Mac). Hour-long waits come from
*queue depth* — many builds stacked on a 1-instance fleet — not from slow
dispatch.

### Sizing for a team

For ~5–6 engineers across several apps:
- **One shared fleet, `baseCapacity = 2–3`**, not six `baseCapacity = 1` fleets
  sitting idle. A shared fleet maximizes utilization; raise capacity via the
  adjustable Mac-ARM quota if the queue grows.
- **`overflowBehavior = QUEUE`** (Mac has no on-demand option regardless).
- **Keep builds short** — the warm cache above is the main lever; a shorter
  build frees the instance sooner, which is what actually shrinks the queue once
  capacity is fixed.
- **Cost:** Macs bill per-minute on the underlying Dedicated Host with a 24h
  minimum. Right-size to peak *concurrent* builds (not headcount); don't
  over-provision to absorb rare spikes — idle Macs still bill and can't be torn
  down quickly.

> MAC_ARM is offered in 5 regions (N. Virginia, Ohio, Oregon, Sydney,
> Frankfurt) and a reserved fleet pins to a single AZ. Rare regional
> `INSUFFICIENT_CAPACITY` on provisioning is outside your control — retry or
> shift region.

---

## Reading a run's performance

`ios_build_status` and each build's `metrics.json` report the signals that
explain a run:

- `compiles` — Swift files recompiled. The clearest warm/cold tell: warm is a
  small floor (≈50–110), cold is ~2,900.
- `cache_state` — `warm` / `partial` / `cold`.
- `cache_restored_from_s3` — whether this Mac warmed from S3.
- `cache_saved` + `save_reason` — whether this run paid the save tax and why.
- `sec_restore` / `sec_mirror` / `sec_test` / `sec_save` / `sec_total` — phase
  timing.
- test counts + total test duration.

Harvest across every run with `scripts/collect-metrics.py`.
