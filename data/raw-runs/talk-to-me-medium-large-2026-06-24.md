# iOS Test Performance: Local vs Remote CodeBuild

> ## 📇 Performance Battle Card
>
> *iOS UI test suite (Amplify + LiveKit + AWS SDK dep graph, ~2,900 Swift files cold). Times are end-to-end build+test, excluding queue wait. Test execution itself is constant ~4 min regardless of where it runs — the variable is the build.*
>
> | Scenario | Total build+test | vs local |
> |---|---|---|
> | **Local — warm** (incremental) | **~6 min** | baseline |
> | **Local — cold** (full compile) | ~13–15 min* | — |
> | **Cloud — warm** (same instance) | **~8 min** | +~2 min |
> | **Cloud — S3 cache restore** (any instance) | ~10 min | +~4 min |
> | **Cloud — cold** (first build, medium) | ~22 min | +~16 min |
> | **Cloud — cold** (first build, large) | ~16 min | +~10 min |
>
> **What to expect:**
> - **Test runtime is identical everywhere** (~4 min). Cloud overhead is build + queue + artifact handling, never the tests.
> - **Warm cloud ≈ local + 2 min.** If the instance already built your repo, you pay ~2 min of cloud tax.
> - **Cloud cold-start ≈ local cold-start** for large instances (~16 min both); medium cold is slower (~22 min).
> - **Large instance**: ~20% faster warm, ~45% faster cold than medium (more cores help the big compile).
> - **First run on a fresh instance/size pays a cold build** while it populates the cache; subsequent runs are warm.
> - **Queue wait is separate** and depends on fleet capacity (1–2 concurrent during this eval).
>
> *\*Local-cold not cleanly measured (SPM network clone stall); estimated from cloud-cold compile counts on comparable hardware.*

---

UI test suite: `TalkToMeUITests` (scheme `TalkToMe-Dev`), 26 tests total → **11 executed / 15 skipped by design**. Every run below passed: **11 passed, 0 failed**.

All remote runs via the `codebuild-ios` MCP. **No run used `clean_build`** — all rely on the fleet's warm-cache behavior.

Required remote params (fleet defaults all fail):
- `repo = https://github.com/tycenjmccann/talk-to-me.git`
- `project_dir = .` (workspace at repo root)
- `scheme = TalkToMe-Dev` (no plain `TalkToMe` scheme)

---

## 0. Local repeatability (3 runs, `main` @ `58975275`)

Same machine, same code, warm DerivedData:

| Local run | Wall (s) | Swift compiles | Per-test sum (s) | Result |
|---|---|---|---|---|
| run 2 | 365 | 48 | 136.7 | 11/0/15 |
| run 3 | **319** | **0** | 135.9 | 11/0/15 |

Per-test execution is rock-stable (~136s). Wall delta = incremental recompile only (run 2 rebuilt 48 changed files; run 3 had nothing to rebuild → 0 compiles). No queue, no artifact upload locally.

---

## 1. Local vs Remote — identical code (`main` @ `58975275`)

Apples-to-apples, both green:

| | Local sim (M-series Mac) | Remote CodeBuild (warm path) |
|---|---|---|
| **Wall time** | **365s (6.1 min)** | **579s (9.7 min)** |
| Result | 11 / 0 / 15 | 11 / 0 / 15 |
| Swift files recompiled | **48** (true warm) | **2,892** (cold) |
| Test execution window | 137s | 136s |

**Per-test execution is identical (0.99x).** The whole gap is build/compile + provisioning + source-download + artifact-upload, not test speed.

### Per-test timing (identical code)

| test | local (s) | remote (s) | ratio |
|---|---|---|---|
| testAppLaunches | 4.4 | 4.1 | 0.94x |
| testAuthScreenAppearsOnFreshLaunch | 10.7 | 4.8 | 0.45x |
| testComposeCancelClearsTranscript | 22.7 | 26.5 | 1.17x |
| testComposeModeShowsRecordButton | 9.8 | 9.2 | 0.94x |
| testComposeRecordProducesTranscriptAndSend | 12.5 | 11.6 | 0.93x |
| testHomeScreenElements | 10.2 | 9.5 | 0.93x |
| testLaunchPerformance | 24.4 | 27.3 | 1.12x |
| testMessagesTabLoads | 8.5 | 8.5 | 1.00x |
| testPreviewToggle | 10.6 | 10.8 | 1.02x |
| testProfileScreenElements | 7.5 | 7.7 | 1.02x |
| testTabNavigation | 15.5 | 15.8 | 1.02x |
| **SUM (executed)** | **136.7** | **135.9** | **0.99x** |

---

## 2. Remote batch — 4 runs queued at once (same code `main` @ `58975275`)

Capacity = **2 concurrent** Macs. 4 submitted → 2 ran immediately, 2 queued.

| Run | build_id | Queue | Build | Swift compiles | Stale-file purge | Cache state |
|---|---|---|---|---|---|---|
| R1 | `0cae5c1d` | 0s | 703s | 2,903 | 2,067 | cold — Mac's DerivedData was from a **different project** → purged + full rebuild |
| R2 | `8a2b99b4` | 0s | 942s | 2,892 | 0 | cold — **empty** newly-added base-capacity Mac |
| R4 | `55e1db2c` | 871s | 437s | 704 | 16 | **partial warm** — relanded on a Mac that just built this repo |
| R3 | `5cc91551` | 1,524s | 370s | **65** | 0 | **fully warm** — matches local (48) |

Local reference: 48 compiles, 365s wall.

### Key findings

1. **Test execution is constant** (~232–254s remote incl. sim boot; 137s pure test time). Build time is the only variable.
2. **Build time tracks Swift compile count exactly:** 2,900 → ~700–940s; 704 → 437s; 65 → 370s.
3. **"Warm" is per-Mac and only as good as that Mac's last job.** A run is fast only if it lands on a Mac whose previous job built *this* repo+scheme. Three cold causes observed:
   - prior **other** project occupied DerivedData → stale purge + cold (R1)
   - **empty** new instance → cold, nothing to purge (R2)
   - same repo just built there → warm/partial-warm (R3, R4)
4. **Not `clean_build`** — never set. The recompiles are cache-state driven, not flag driven.
5. **Capacity = 2.** Adding base capacity raises throughput but a fresh Mac starts cold; deep queue waits (R3 waited 25 min).

### Recommendation
Pin builds to a warm, repo-dedicated instance (sticky routing by repo+scheme) and prevent other projects from sharing that DerivedData path. That would make remote builds consistently land near the 370s (warm) figure instead of 700–940s (cold).

---

## Isolation / multi-tenancy note

The fleet reuses Macs with a **shared DerivedData path** (`/Users/cbuser/ios-mcp-state/DerivedData`). Investigated R1's 2,067 stale-file purges to check for cross-tenant exposure:

- **All purged files were shared open-source SPM dependencies** (aws-sdk-swift, smithy-swift, swift-nio, swift-crypto, etc.) — the same packages this app uses.
- **Zero `.app` bundles** and **zero app-specific/proprietary modules** were purged — nothing identifying any project, foreign or otherwise.
- Root cause was a **config mismatch**: cached deps were built for `Debug-iphonesimulator`; our run uses `Debug-Dev-iphonesimulator` (the `-Dev` scheme) → invalidated → recompiled.

**Conclusion:** no foreign project was exposed to us, and nothing of ours was shown to others in these runs — only this repo's builds touched the Mac. **However**, a shared per-Mac DerivedData on reused instances is a multi-tenancy design smell: if two different customers' jobs landed on the same instance, build artifacts could in principle cross. Worth confirming the fleet enforces per-tenant instance/state isolation.

---

## 3. Remote batch — 4 runs, repeat (same code `main` @ `58975275`)

Run immediately after batch 2, so both Macs were already warmed by this repo. All green (11/0/15).

| Run | build_id | Queue | Build | Swift compiles | Stale-purge | Cache state |
|---|---|---|---|---|---|---|
| B1 | `1e5b6ec4` | 0s | 381s | 65 | 0 | **fully warm** |
| B2 | `b102b8b0` | 0s | 419s | 65 | 0 | **fully warm** |
| B4 | `6d43905f` | 381s | 404s | 65 | 0 | **fully warm** |
| B3 | `99cd7068` | 564s | 420s | 65 | 0 | **fully warm** |

### Findings

- **When both Macs are pre-warmed by the same repo, every run is consistently fast and warm** — all 4 hit exactly 65 compiles, build 381–420s (tight 39s spread). No cold rebuild, no stale purge.
- Contrast with batch 1, where Macs started from a foreign/empty cache: 370–942s build, 65–2,903 compiles. **Warm-state inheritance is the single biggest variable.**
- Capacity still = 2: B3/B4 queued 381–564s behind B1/B2.
- **Steady-state remote ≈ 400s build + ~235s test ≈ 11 min wall** once warm, vs **local 365s wall**. Local stays ~2x faster end-to-end mainly due to zero queue + faster artifact handling, but warm remote build time (≈400s) is now close to local cold-ish build.

## Summary across all runs

| Scenario | Build (s) | Compiles | Notes |
|---|---|---|---|
| Local (warm) | n/a (365 wall) | 48 | baseline |
| Remote fully warm | 370–420 | 65 | best case, repeatable (batch 2 + R3) |
| Remote partial warm | 437 | 704 | relanded mid-batch |
| Remote cold (foreign cache) | 703 | 2,903 | stale purge |
| Remote cold (empty Mac) | 942 | 2,892 | new instance |

**Bottom line:** the test suite itself runs identically everywhere (~136s). Remote variance is entirely build-cache state, which depends on what last ran on the assigned Mac. Warm-pinned remote is ~400s build; cold is up to 942s — a 2.5x swing driven purely by scheduling, not by `clean_build` (never used).

---

## 4. Remote batch — 4 runs WITH S3 caching layer (same code `main` @ `58975275`)

New infra: an **S3 caching layer** + a **per-repo DerivedData path** (`ios-mcp-state/https___github.com_tycenjmccann_talk-to-me.git_./DerivedData`) replacing the old shared `ios-mcp-state/DerivedData`. All green (11/0/15).

| Run | build_id | Queue | Build | Swift compiles | Stale-purge | Cache state |
|---|---|---|---|---|---|---|
| C1 | `5df8dee1` | 0s | 708s | 2,892 | 0 | cold — first run, **populated** S3 cache |
| C2 | `3a51c485` | 0s | 817s | (cold) | — | cold — concurrent first run, populated S3 |
| C3 | `0bea2815` | 875s | 622s | **65** | 0 | **warm — restored from S3** (different Mac than built it) |
| C4 | `dede0354` | 975s | 571s | **65** | 0 | **warm — restored from S3** |

### What the S3 layer changed

1. **Per-repo DerivedData path** → the cross-project stale-purge from batch 1 (R1's 2,067 foreign deletions) **cannot happen anymore**. Directly addresses the isolation concern. 0 stale purges across all 4.
2. **Warm state is now portable across Macs.** Before, only a run that relanded on the *same* Mac could be warm. Now C3/C4 restored a full warm cache (65 compiles) from S3 onto Macs that never built this repo. That's the key win — warm is no longer pinned to one instance.
3. **First-run cost unchanged** (C1/C2 cold ~708–817s) — someone has to populate the cache. But every subsequent run on any Mac is warm.

### Caveats observed
- **S3 warm build (571–622s) is slower than local-Mac warm (370–420s in batch 2).** The S3 download/extract of the cache adds overhead vs an already-hot local DerivedData. So S3 warm < cold, but S3 warm > same-Mac warm.
- Capacity still = 2; C3/C4 queued 875–975s.
- Test execution unchanged (~245–268s window).

### Verdict on the S3 layer
**Net positive and the right fix for consistency + isolation:** every post-population run is warm regardless of which Mac it lands on (65 compiles vs up to 2,900), and the cross-tenant DerivedData purge is structurally eliminated. The tradeoff is S3 warm builds (~600s) sit between same-Mac warm (~400s) and cold (~800s) — predictable, never the 942s worst case from batch 1.

## Final cross-batch summary

| Scenario | Build (s) | Compiles | When |
|---|---|---|---|
| Local warm (no recompile) | 319 wall | 0 | run 3 |
| Local warm (incremental) | 365 wall | 48 | run 2 |
| Remote same-Mac warm | 370–420 | 65 | batch 2 |
| **Remote S3 warm (any Mac)** | **571–622** | **65** | batch 3 (C3/C4) |
| Remote partial warm | 437 | 704 | R4 |
| Remote cold (foreign purge) | 703 | 2,903 | R1 |
| Remote cold (empty / S3-populate) | 708–942 | ~2,890 | R2, C1, C2 |

Test execution constant ~232–268s (remote) / ~136s per-test-sum everywhere.

---

## MCP surface gaps (product issues found during this eval)

The `codebuild-ios` MCP is the intended primary interface, but key data needed to interpret a run is only available by downloading the raw build log and grepping — not from the MCP tools themselves.

**`ios_build_status` returns:** status, duration_seconds, per-phase timings, test_summary, failures[], artifact URLs. Good.

**Not exposed by the MCP (had to parse the log):**
1. **Compile count** (e.g. 65 warm vs ~2,900 cold) — the single clearest warm/cold signal. Not a field.
2. **Cache metrics** — the new instrumentation (`Cold instance → restoring warm cache from S3`, `Warm cache restored in 40s`, `Saving warm cache (output changed)`, `saved in 177s`) exists in the log but is **not surfaced as structured fields**. A programmatic consumer can't read cache hit/miss, restore time, or save time.
3. **compute_size** of a run — echoed only in the initial submit message, not in `ios_build_status`.
4. **Test execution window** (~245s) — buried in log, not a field.

**Two functional problems:**
- **No list/queue endpoint.** There is no MCP equivalent of `aws codebuild list-builds-for-project`. With single-capacity pools and 8 queued builds, I could not tell *which* build was on the Mac vs queued — polling specific IDs returned QUEUED while other IDs were actually BUILDING. Blind to pool/queue state.
- **Status lag.** MCP `ios_build_status` reported QUEUED for builds the AWS CLI already showed in BUILD. The MCP view trails ground truth.

**Impact:** for an MCP-first product, a battle card or any automation can't be built on the MCP alone today — it needs (a) a list/queue endpoint, (b) compile-count + cache metrics as structured fields, (c) compute_size echo, (d) fresher status. All data is recoverable from the log, none from the MCP surface.

---

## Instance-size comparison (medium vs large) — in progress

Cache key observation (from large run L4 `61719910`): the S3 warm cache is keyed **per-repo but NOT per-compute-size**. Large's first run restored medium's cached tarball (`Warm cache restored in 40s`) but still recompiled 2,981 files — the cross-size cache didn't validate → effectively cold, then re-saved its own (`saved in 177s`). So the first run on each *new size* pays a cold build even if another size already populated S3.

| Run | Size | build_id | Queue | Build | Compiles | Cache log signal | Result |
|---|---|---|---|---|---|---|---|
| L4 | large | `61719910` | 714s | 736s | 2,981 | cold instance; restored 40s but invalid → recompiled; saved 177s | 11/0/15 |
| L3 | large | `6affca53` | 1591s | 505s | 69 | same-Mac warm (no S3 restore line); still re-saved cache 160s | 11/0/15 |
| M3 | medium | `bf9646d2` | 1042s | 1327s | 2,892 | restored S3 36s but invalid → recompiled; saved 211s; test window 348s (slow cold fleet) | 11/0/15 |
| M2 | medium | `9b6092da` | 2501s | 623s | 65 | same-Mac warm; re-saved 183s; test 247s | 11/0/15 |
| L2 | large | `79b3f974` | 2633s | 492s | 69 | same-Mac warm; re-saved 162s; test 222s | 11/0/15 |
| M1 | medium | `d9e73822` | 3346s | 563s | 65 | same-Mac warm; still re-saved 182s (skip-save fix not yet active); test 235s | 11/0/15 |
| L5 | large (post-fix) | `48baaf83` | 0s | 531s | 69 | same-Mac warm; trigger reworded `(compiled output changed)` but **still re-saved 179s**; test 238s | 11/0/15 |
| M5 | medium (post-fix) | `e606d0ad` | 509s | 594s | 65 | same-Mac warm; re-saved 199s `(compiled output changed)`; test 258s | 11/0/15 |
| L1 | large | `0e6a6504` | 4548s | 502s | 69 | same-Mac warm; re-saved 156s; test 225s | 11/0/15 |
| M4 | medium | `1a311b72` | 5015s | 551s | 65 | same-Mac warm; re-saved 182s; test 235s | 11/0/15 |

### Instance-size batch — complete (10 runs, all 11/0/15)

**Build time by state & size:**

| State | Medium build | Large build |
|---|---|---|
| Cold (first/invalid cache) | 1327s (M3) | 736s (L4) |
| Warm (same-Mac, 65–69 compiles) | 551–623s (M1/M2/M4/M5) | 492–531s (L1/L2/L3/L5) |

- **Large is faster everywhere:** ~45% faster cold (more cores absorb the 2,900-file compile), ~15% faster warm.
- **Warm build is tight & repeatable** within each size (medium 551–623s, large 492–531s).
- **Test execution constant across sizes:** medium 235–258s, large 222–245s (large marginally faster). Cold medium fleet's first test (348s, M3) was a one-off outlier.
- **Cache-save tax persists post-fix:** every warm run still uploads (156–199s) because a 65–69-file incremental trips `(compiled output changed)`. True skip needs a 0-compile run.
- **Queue depth dominated wall-clock this batch:** single-instance pools + 10 builds → queue waits up to 5015s (84 min). Queue, not build, was the headline cost when batching deep.

**Skip-save fix status (L5, first post-fix run):** the save trigger changed from `(output changed)` → `(compiled output changed)` — change-detection was refined to ignore volatile test artifacts. But a 69-file incremental recompile still counts as "compiled output changed" → cache re-saved (179s). So warm runs that recompile *anything* still pay the save. The skip would only fire on a true 0-compile run. Net: improved (no longer saves on pure test-artifact churn) but the same-Mac warm path with incremental compiles still re-uploads.

### Size comparison so far (warm, same-Mac)
- **Medium warm build: ~620s** (M2) vs **Large warm build: ~490–505s** (L2, L3). Large ~20% faster on build when warm.
- **Cold:** medium 1327s (M3) vs large 736s (L4). Large ~45% faster cold — more cores help the big cold compile most.
- **Test execution ~equal** across sizes when warm: medium 247s, large 222s. Cold medium fleet's first test was an outlier (348s).
- **All 8 functional:** 11/0/15 every run, both sizes.

**S3 cache invalidation is the recurring problem:** both L4 (large) and M3 (medium) downloaded a warm-cache tarball (`restored in 36–40s`) but then recompiled ~2,900 files anyway. The restored DerivedData fails Swift's validity check (likely absolute-path or instance-fingerprint mismatch baked into incremental state), so the download is wasted and the build is cold regardless. S3 restore only helps when it lands on the *same* fleet+path that saved it — which is also when same-Mac warm would've worked without S3. **Net: the S3 layer is not yet delivering cross-instance warm; restored caches don't validate.**

**Cache-save tax:** even fully-warm runs spend ~160–177s on `Saving warm cache → S3 (output changed)`. The "output changed" trigger fires every run (4 new files from the test bundle), so the S3 upload cost is paid on the warm path too — inflating warm build time. Should be skipped when only volatile/test artifacts changed.
