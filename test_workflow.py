import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from storage import ApprovalError, TicketStore
from workflow import ApprovalWorkflow


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.store = TicketStore(self.directory / "tickets.sqlite3")
        self.workflow = ApprovalWorkflow(self.directory, self.store)
        self.store.create("T-042", "Rotate the synthetic demo banner")

    def test_rejection_does_not_apply(self):
        self.workflow.request("T-042", "reject-1")
        self.assertEqual(self.workflow.decide("reject-1", 1, False)["status"], "rejected")
        self.assertIsNone(self.store.get("T-042")["applied_text"])

    def test_stale_approval_requires_a_new_review(self):
        self.workflow.request("T-042", "review-1")
        self.store.revise("T-042", "Replace the synthetic demo banner")
        self.assertEqual(self.workflow.decide("review-1", 1, True)["status"], "stale")
        self.assertEqual(self.store.get("T-042")["application_count"], 0)

        current = self.workflow.request("T-042", "review-2")
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["draft"], "Replace the synthetic demo banner")
        self.assertEqual(self.workflow.decide("review-2", 2, True)["status"], "applied")
        self.assertEqual(self.store.get("T-042")["application_count"], 1)

    def test_wrong_revision_leaves_review_pending(self):
        self.workflow.request("T-042", "review-1")
        with self.assertRaisesRegex(ApprovalError, "does not match"):
            self.workflow.decide("review-1", 2, True)
        self.assertEqual(self.workflow.status("review-1")["status"], "awaiting_review")

    def test_repeated_decision_and_second_run_do_not_duplicate_application(self):
        self.workflow.request("T-042", "review-1")
        self.workflow.request("T-042", "review-2")
        self.assertEqual(self.workflow.decide("review-1", 1, True)["status"], "applied")
        self.assertEqual(self.workflow.decide("review-1", 1, True)["status"], "applied")
        self.assertEqual(self.workflow.decide("review-2", 1, True)["status"], "already_applied")
        with self.assertRaisesRegex(ApprovalError, "different decision"):
            self.workflow.decide("review-1", 1, False)
        self.assertEqual(self.store.get("T-042")["application_count"], 1)

    def test_recovers_after_database_commit_without_second_application(self):
        self.workflow.request("T-042", "review-1")
        real_apply = self.store.apply

        def fail_after_commit(*args):
            real_apply(*args)
            raise RuntimeError("simulated interruption after commit")

        with patch.object(self.store, "apply", side_effect=fail_after_commit):
            with self.assertRaisesRegex(RuntimeError, "after commit"):
                self.workflow.decide("review-1", 1, True)

        self.assertEqual(self.workflow.status("review-1")["status"], "needs_recovery")
        self.assertEqual(self.store.get("T-042")["application_count"], 1)
        self.store.revise("T-042", "A newer draft that has not been approved")
        self.assertEqual(self.workflow.recover("review-1")["status"], "applied")
        ticket = self.store.get("T-042")
        self.assertEqual(ticket["application_count"], 1)
        self.assertEqual(ticket["applied_text"], "Rotate the synthetic demo banner")

    def test_cli_resumes_from_disk_in_a_new_process(self):
        cli = Path(__file__).with_name("approval.py")
        args = [sys.executable, str(cli), "--data-dir", str(self.directory)]

        def invoke(*command):
            result = subprocess.check_output(
                [*args, *command], encoding="utf-8",
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
            return json.loads(result)

        self.assertEqual(invoke("request", "T-042", "cli-1")["status"], "awaiting_review")
        self.assertEqual(invoke("status", "cli-1")["revision"], 1)
        self.assertEqual(invoke("decide", "cli-1", "1", "approve")["status"], "applied")
        self.assertEqual(invoke("show", "T-042")["application_count"], 1)


if __name__ == "__main__":
    unittest.main()
