"""Unit tests for handler.py's TEAM-4921 additions (capacity preflight, queue
timing, queued-timeout mapping, short-SHA guard).

stdlib unittest (not pytest) — pytest is not assumed to be installed; this file
still collects fine under pytest if a dev has it. Run with:
    python3 -m unittest discover -s lambda -p 'test_*.py' -v
"""

import datetime
import json
import os
import sys
import unittest
from unittest.mock import MagicMock

os.environ.setdefault("CODEBUILD_PROJECT", "ios-agent-tests")
os.environ.setdefault("ARTIFACTS_BUCKET", "test-bucket")
sys.path.insert(0, os.path.dirname(__file__))
import handler as h  # noqa: E402

NOW = datetime.datetime.now(datetime.timezone.utc)


def queued_build(build_id, minutes_queued, size="BUILD_GENERAL1_MEDIUM"):
    """A build IN_PROGRESS, sitting in QUEUED for `minutes_queued` so far."""
    return {
        "id": build_id,
        "buildStatus": "IN_PROGRESS",
        "currentPhase": "QUEUED",
        "environment": {"computeType": size},
        "startTime": NOW - datetime.timedelta(minutes=minutes_queued),
        "phases": [
            {"phaseType": "SUBMITTED", "phaseStatus": "SUCCEEDED", "durationInSeconds": 0},
            {"phaseType": "QUEUED", "startTime": NOW - datetime.timedelta(minutes=minutes_queued)},
        ],
    }


class NoSuchKey(Exception):
    pass


def mock_s3_no_artifacts():
    """s3 client stub for tests that only care about ios_build_status's queue/
    phase handling, not test_summary/artifacts (those already have coverage)."""
    s3 = MagicMock()
    s3.exceptions.NoSuchKey = NoSuchKey
    s3.get_object.side_effect = NoSuchKey()
    s3.list_objects_v2.return_value = {}
    s3.head_object.side_effect = Exception("no object")
    return s3


class IosTestShortSha(unittest.TestCase):
    def setUp(self):
        h.codebuild = MagicMock()
        h.FLEET_MEDIUM_ARN = "arn:aws:codebuild:us-east-1:1:fleet/med"
        h.FLEET_LARGE_ARN = ""

    def test_abbreviated_sha_rejected(self):
        result = h.ios_test({"branch": "08d9c28", "scheme": "X"})
        self.assertEqual(result["reason"], "SHORT_SHA")
        self.assertEqual(result["status"], "ERROR")
        h.codebuild.start_build.assert_not_called()

    def test_branch_name_starts_build(self):
        h.codebuild.batch_get_fleets.return_value = {
            "fleets": [{"status": {"statusCode": "ACTIVE"}}]
        }
        h.codebuild.list_builds_for_project.return_value = {"ids": []}
        h.codebuild.start_build.return_value = {
            "build": {"id": "p:1", "source": {}, "sourceVersion": "main", "environment": {}}
        }
        result = h.ios_test({"branch": "main", "scheme": "X"})
        self.assertEqual(result["status"], "IN_PROGRESS")
        h.codebuild.start_build.assert_called_once()

    def test_full_40_hex_sha_starts_build(self):
        h.codebuild.batch_get_fleets.return_value = {
            "fleets": [{"status": {"statusCode": "ACTIVE"}}]
        }
        h.codebuild.list_builds_for_project.return_value = {"ids": []}
        sha = "a" * 40
        h.codebuild.start_build.return_value = {
            "build": {"id": "p:2", "source": {}, "sourceVersion": sha, "environment": {}}
        }
        result = h.ios_test({"branch": sha, "scheme": "X"})
        self.assertEqual(result["status"], "IN_PROGRESS")
        h.codebuild.start_build.assert_called_once()


class IosTestCapacityPreflight(unittest.TestCase):
    def setUp(self):
        h.codebuild = MagicMock()
        h.FLEET_MEDIUM_ARN = "arn:aws:codebuild:us-east-1:1:fleet/med"
        h.FLEET_LARGE_ARN = ""
        h.FLEET_STALL_MINUTES = 20

    def test_stalled_fleet_refuses_and_lists_ids(self):
        h.codebuild.batch_get_fleets.return_value = {
            "fleets": [{"status": {"statusCode": "ACTIVE"}}]
        }
        h.codebuild.list_builds_for_project.return_value = {"ids": ["a", "b", "c"]}
        h.codebuild.batch_get_builds.return_value = {
            "builds": [queued_build("a", 45), queued_build("b", 30), queued_build("c", 25)]
        }
        result = h.ios_test({"branch": "main", "scheme": "X"})
        self.assertEqual(result["reason"], "INSUFFICIENT_CAPACITY")
        self.assertEqual(sorted(result["stalled_builds"]), ["a", "b", "c"])
        self.assertGreater(result["oldest_queued_seconds"], 20 * 60)
        h.codebuild.start_build.assert_not_called()

    def test_force_bypasses_stall_refusal(self):
        h.codebuild.batch_get_fleets.return_value = {
            "fleets": [{"status": {"statusCode": "ACTIVE"}}]
        }
        h.codebuild.list_builds_for_project.return_value = {"ids": ["a"]}
        h.codebuild.batch_get_builds.return_value = {"builds": [queued_build("a", 45)]}
        h.codebuild.start_build.return_value = {
            "build": {"id": "p:3", "source": {}, "sourceVersion": "main", "environment": {}}
        }
        result = h.ios_test({"branch": "main", "scheme": "X", "force": True})
        self.assertEqual(result["status"], "IN_PROGRESS")
        h.codebuild.start_build.assert_called_once()

    def test_create_failed_fleet_refuses(self):
        h.codebuild.batch_get_fleets.return_value = {
            "fleets": [{"status": {"statusCode": "CREATE_FAILED", "message": "boom"}}]
        }
        result = h.ios_test({"branch": "main", "scheme": "X"})
        self.assertEqual(result["reason"], "INSUFFICIENT_CAPACITY")
        self.assertEqual(result["fleet_status"], "CREATE_FAILED")
        h.codebuild.start_build.assert_not_called()

    def test_fleet_not_found_refuses(self):
        h.codebuild.batch_get_fleets.return_value = {"fleets": [], "fleetsNotFound": ["x"]}
        result = h.ios_test({"branch": "main", "scheme": "X"})
        self.assertEqual(result["reason"], "INSUFFICIENT_CAPACITY")
        self.assertEqual(result["fleet_status"], "NOT_FOUND")
        h.codebuild.start_build.assert_not_called()

    def test_batch_get_fleets_denied_proceeds(self):
        h.codebuild.batch_get_fleets.side_effect = Exception("AccessDeniedException")
        h.codebuild.list_builds_for_project.return_value = {"ids": []}
        h.codebuild.start_build.return_value = {
            "build": {"id": "p:4", "source": {}, "sourceVersion": "main", "environment": {}}
        }
        result = h.ios_test({"branch": "main", "scheme": "X"})
        self.assertEqual(result["status"], "IN_PROGRESS")
        h.codebuild.start_build.assert_called_once()


class QueuedTimeoutMapping(unittest.TestCase):
    def setUp(self):
        h.codebuild = MagicMock()
        h.s3 = mock_s3_no_artifacts()

    def test_queued_timeout_reports_build_error_first(self):
        build = {
            "id": "p:5",
            "buildStatus": "FAILED",
            "environment": {"computeType": "BUILD_GENERAL1_MEDIUM"},
            "startTime": NOW - datetime.timedelta(minutes=61),
            "endTime": NOW,
            "source": {},
            "phases": [
                {"phaseType": "SUBMITTED", "phaseStatus": "SUCCEEDED", "durationInSeconds": 0},
                {
                    "phaseType": "QUEUED",
                    "phaseStatus": "TIMED_OUT",
                    "durationInSeconds": 3600,
                    "endTime": NOW,
                    "contexts": [{"message": "QUEUED timed out"}],
                },
                {"phaseType": "COMPLETED"},
            ],
        }
        h.codebuild.batch_get_builds.return_value = {"builds": [build]}
        result = h.ios_build_status({"build_id": "p:5"})
        self.assertEqual(result["status"], "BUILD_ERROR")
        self.assertEqual(result["queued_seconds"], 3600)
        self.assertIn("Timed out in QUEUED", result["build_errors"][0])

    def test_normal_failure_is_not_relabeled(self):
        build = {
            "id": "p:6",
            "buildStatus": "FAILED",
            "environment": {"computeType": "BUILD_GENERAL1_MEDIUM"},
            "startTime": NOW - datetime.timedelta(minutes=5),
            "endTime": NOW,
            "source": {},
            "phases": [
                {"phaseType": "SUBMITTED", "phaseStatus": "SUCCEEDED", "durationInSeconds": 0},
                {"phaseType": "QUEUED", "phaseStatus": "SUCCEEDED", "durationInSeconds": 5},
                {"phaseType": "BUILD", "phaseStatus": "FAILED", "durationInSeconds": 60,
                 "contexts": [{"message": "exit status 65"}]},
            ],
        }
        h.codebuild.batch_get_builds.return_value = {"builds": [build]}
        result = h.ios_build_status({"build_id": "p:6"})
        self.assertEqual(result["status"], "BUILD_ERROR")
        self.assertNotIn("Timed out in QUEUED", result["build_errors"][0])


class QueuedSecondsHelper(unittest.TestCase):
    def test_in_progress_measures_against_now(self):
        build = queued_build("a", 10)
        seconds = h._queued_seconds(build)
        self.assertAlmostEqual(seconds, 600, delta=5)

    def test_completed_phase_passes_through_duration(self):
        build = {
            "phases": [
                {"phaseType": "SUBMITTED", "phaseStatus": "SUCCEEDED", "durationInSeconds": 0},
                {"phaseType": "QUEUED", "phaseStatus": "SUCCEEDED",
                 "durationInSeconds": 42, "endTime": NOW},
            ]
        }
        self.assertEqual(h._queued_seconds(build), 42)

    def test_no_queued_phase_is_zero(self):
        build = {"phases": [{"phaseType": "SUBMITTED", "phaseStatus": "SUCCEEDED"}]}
        self.assertEqual(h._queued_seconds(build), 0)


class GatewayToolsSchema(unittest.TestCase):
    def setUp(self):
        path = os.path.join(os.path.dirname(__file__), "..", "gateway-tools.json")
        with open(path) as f:
            self.tools = json.load(f)

    def test_parses_and_has_seven_tools(self):
        self.assertEqual(len(self.tools), 7)

    def test_ios_test_has_force_and_full_sha_note(self):
        ios_test = next(t for t in self.tools if t["name"] == "ios_test")
        props = ios_test["inputSchema"]["properties"]
        self.assertIn("force", props)
        self.assertEqual(props["force"]["type"], "boolean")
        self.assertIn("40-hex", props["branch"]["description"])


if __name__ == "__main__":
    unittest.main()
