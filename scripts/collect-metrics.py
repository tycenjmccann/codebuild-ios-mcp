#!/usr/bin/env python3
"""collect-metrics.py — harvest build + test performance across every run.

Reads all CodeBuild builds for the project in a time window, merges each build's
metrics.json (self-reported by the buildspec) + CodeBuild phase timings, and
writes a results.md table plus a results.csv. No live capture needed — run it
whenever, after any number of builds across any number of apps.

Each build self-reports (s3://<bucket>/builds/<id>/metrics.json):
  repo, compiles, stale, cache_state, cache_restored_from_s3,
  sec_restore/mirror/test/save/total, tests_total/passed/failed/skipped,
  test_total_ms.
CodeBuild adds: queue secs, provisioning secs, overall build secs, status.

Usage:
  AWS_PROFILE=tycenj-prod python3 scripts/collect-metrics.py [--hours 24] \
      [--project ios-agent-tests] [--bucket <name>] [--region us-east-1] \
      [--out results.md]

Requires: aws cli v2 creds, boto3.
"""
import argparse
import csv
import datetime as dt
import json
import subprocess
import sys


def aws_json(args):
    out = subprocess.run(["aws", *args], capture_output=True, text=True)
    if out.returncode != 0:
        sys.stderr.write(out.stderr)
        return None
    return json.loads(out.stdout or "null")


def phase_secs(phases, name):
    for p in phases or []:
        if p.get("phaseType") == name:
            return p.get("durationInSeconds")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--project", default="ios-agent-tests")
    ap.add_argument("--bucket", default="ios-agent-test-artifacts-838829463875")
    ap.add_argument("--region", default="us-east-1")
    ap.add_argument("--out", default="results.md")
    args = ap.parse_args()

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=args.hours)

    # 1) all build ids for the project (paginated)
    ids, token = [], None
    while True:
        a = ["codebuild", "list-builds-for-project", "--project-name", args.project,
             "--region", args.region, "--output", "json"]
        if token:
            a += ["--next-token", token]
        r = aws_json(a) or {}
        ids += r.get("ids", [])
        token = r.get("nextToken")
        if not token:
            break

    # 2) batch-get details (<=100 per call), filter by window
    rows = []
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        r = aws_json(["codebuild", "batch-get-builds", "--ids", *batch,
                      "--region", args.region, "--output", "json"]) or {}
        for b in r.get("builds", []):
            start = b.get("startTime")
            # startTime is epoch float in CLI json
            t = dt.datetime.fromtimestamp(start, dt.timezone.utc) if isinstance(start, (int, float)) else None
            if t and t < cutoff:
                continue
            bid = b["id"]
            phases = b.get("phases", [])
            # 3) merge self-reported metrics.json from S3 (stream body to stdout)
            key = f"builds/{bid}/metrics.json"
            body = subprocess.run(
                ["aws", "s3", "cp", f"s3://{args.bucket}/{key}", "-", "--region", args.region],
                capture_output=True, text=True)
            try:
                m = json.loads(body.stdout) if body.returncode == 0 else {}
            except Exception:
                m = {}
            rows.append({
                "build_id": bid.split(":")[-1][:8],
                "repo": m.get("repo", "?"),
                "start": t.strftime("%H:%M") if t else "?",
                "status": b.get("buildStatus"),
                "queue_s": phase_secs(phases, "QUEUED"),
                "prov_s": phase_secs(phases, "PROVISIONING"),
                "build_s": phase_secs(phases, "BUILD"),
                "compiles": m.get("compiles"),
                "stale": m.get("stale"),
                "cache": m.get("cache_state"),
                "from_s3": m.get("cache_restored_from_s3"),
                "test_s": m.get("sec_test"),
                "t_total": m.get("tests_total"),
                "t_pass": m.get("tests_passed"),
                "t_fail": m.get("tests_failed"),
                "t_skip": m.get("tests_skipped"),
            })

    rows.sort(key=lambda r: (r["repo"], r["start"]))

    # 4) write markdown + csv
    cols = ["repo", "build_id", "start", "status", "queue_s", "prov_s", "build_s",
            "compiles", "stale", "cache", "from_s3", "test_s",
            "t_total", "t_pass", "t_fail", "t_skip"]
    with open(args.out, "w") as f:
        f.write(f"# iOS CodeBuild results — last {args.hours:g}h "
                f"({len(rows)} builds, project {args.project})\n\n")
        f.write("| " + " | ".join(cols) + " |\n")
        f.write("|" + "|".join(["---"] * len(cols)) + "|\n")
        for r in rows:
            f.write("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |\n")

        # per-repo warm-hit rate + medians
        f.write("\n## Per-repo summary\n\n")
        f.write("| repo | builds | warm | partial | cold | warm-hit % | median build_s |\n")
        f.write("|---|---|---|---|---|---|---|\n")
        repos = {}
        for r in rows:
            d = repos.setdefault(r["repo"], [])
            d.append(r)
        for repo, rs in sorted(repos.items()):
            warm = sum(1 for r in rs if r["cache"] == "warm")
            part = sum(1 for r in rs if r["cache"] == "partial")
            cold = sum(1 for r in rs if r["cache"] == "cold")
            bts = sorted(r["build_s"] for r in rs if isinstance(r["build_s"], int))
            med = bts[len(bts) // 2] if bts else ""
            hit = f"{100 * warm / len(rs):.0f}" if rs else "0"
            f.write(f"| {repo} | {len(rs)} | {warm} | {part} | {cold} | {hit} | {med} |\n")

    with open(args.out.replace(".md", ".csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})

    print(f"Wrote {args.out} + .csv ({len(rows)} builds)")


if __name__ == "__main__":
    main()
