#!/usr/bin/env python3
"""Parse an .xcresult bundle into a structured test summary.

Emits two files from one xcresulttool pass:
  1. summary.json  — the authoritative source the MCP Lambda reads. Shape:
       {"total","passed","failed","skipped",
        "failures":[{"test_name","class_name","message","duration_ms"}]}
  2. JUnit XML     — for the CodeBuild Test Reports console view (secondary).

We do NOT rely on CodeBuild's JUnit parser for correctness — it silently
ingests 0 cases for some valid files. summary.json is ground truth.

Usage: xcresult_to_junit.py <TestResults.xcresult> <out.xml> [<summary.json>]
Tolerant: on any parse trouble it still writes valid (possibly empty) files.
"""
import json
import re
import subprocess
import sys
from xml.sax.saxutils import escape


def _run(path):
    for cmd in (
        ["xcrun", "xcresulttool", "get", "test-results", "tests", "--path", path, "--format", "json"],
        ["xcrun", "xcresulttool", "get", "--format", "json", "--path", path],
    ):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
            return json.loads(out)
        except Exception:
            continue
    return {}


def _dur_ms(node):
    # durations look like "0.009s", "1.2s", or a number.
    d = node.get("duration") or node.get("durationInSeconds")
    if d is None:
        ns = node.get("durationInNanoSeconds")
        return (ns / 1_000_000) if ns else 0.0
    if isinstance(d, (int, float)):
        return float(d) * 1000.0
    m = re.search(r"[\d.]+", str(d))
    return float(m.group()) * 1000.0 if m else 0.0


def _walk(node, suite, cases):
    """Recurse, threading the nearest enclosing suite name as class_name."""
    if isinstance(node, dict):
        ntype = (node.get("nodeType") or "").lower()
        name = node.get("name", "")
        if "suite" in ntype or "bundle" in ntype:
            suite = name or suite
        if ntype in ("test case", "test"):
            result = (node.get("result") or "").lower()
            failed = result in ("failed", "expected failure")
            skipped = result in ("skipped",)
            msgs = []
            for child in node.get("children", []) or []:
                if isinstance(child, dict) and "fail" in (child.get("nodeType") or "").lower():
                    if child.get("name"):
                        msgs.append(child["name"])
            cases.append({
                "test_name": name,
                "class_name": suite,
                "failed": failed,
                "skipped": skipped,
                "message": " | ".join(msgs),
                "duration_ms": round(_dur_ms(node), 3),
            })
        for child in node.get("children", []) or []:
            _walk(child, suite, cases)
        for k, v in node.items():
            if k != "children" and isinstance(v, (list, dict)):
                _walk(v, suite, cases)
    elif isinstance(node, list):
        for item in node:
            _walk(item, suite, cases)


def main():
    src, xml_out = sys.argv[1], sys.argv[2]
    summary_out = sys.argv[3] if len(sys.argv) > 3 else "summary.json"

    cases = []
    try:
        _walk(_run(src), "", cases)
    except Exception as e:
        sys.stderr.write(f"converter warning: {e}\n")

    seen, uniq = set(), []
    for c in cases:
        key = (c["class_name"], c["test_name"])
        if key not in seen:
            seen.add(key)
            uniq.append(c)

    failed = sum(1 for c in uniq if c["failed"])
    skipped = sum(1 for c in uniq if c["skipped"] and not c["failed"])
    passed = len(uniq) - failed - skipped
    summary = {
        "total": len(uniq),
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "failures": [
            {"test_name": c["test_name"], "class_name": c["class_name"],
             "message": c["message"] or "test failed", "duration_ms": c["duration_ms"]}
            for c in uniq if c["failed"]
        ],
    }
    with open(summary_out, "w") as f:
        json.dump(summary, f)

    # Secondary: JUnit for the CodeBuild console.
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             f'<testsuites tests="{len(uniq)}" failures="{failed}">',
             f'<testsuite name="iOSTests" tests="{len(uniq)}" failures="{failed}" time="0">']
    for c in uniq:
        nm, cls = escape(c["test_name"]), escape(c["class_name"] or "iOSTests")
        t = c["duration_ms"] / 1000.0
        if c["failed"]:
            lines.append(f'<testcase classname="{cls}" name="{nm}" time="{t}">')
            lines.append(f'<failure message="{escape(c["message"] or "test failed")}"></failure>')
            lines.append("</testcase>")
        elif c["skipped"]:
            lines.append(f'<testcase classname="{cls}" name="{nm}" time="{t}"><skipped></skipped></testcase>')
        else:
            lines.append(f'<testcase classname="{cls}" name="{nm}" time="{t}"></testcase>')
    lines.append("</testsuite></testsuites>")
    with open(xml_out, "w") as f:
        f.write("\n".join(lines))

    sys.stderr.write(
        f"converter: {summary['total']} cases, {failed} failed, {skipped} skipped "
        f"-> {summary_out} + {xml_out}\n")


if __name__ == "__main__":
    main()
