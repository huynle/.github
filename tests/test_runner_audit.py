import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import runner_audit  # noqa: E402

CI_RUNNER = "${{ vars.CI_RUNNER && fromJSON(vars.CI_RUNNER) || 'ubuntu-latest' }}"
AUDIT_WORKFLOW = (ROOT / ".github/workflows/runner-audit.yml").read_text()


def kinds(private, text):
    return [(finding.job, finding.kind) for finding in runner_audit.audit_workflow("huynle/x", private, "ci.yml", text)]


class PrivateRepositoryTests(unittest.TestCase):
    def test_ci_runner_expression_passes(self):
        self.assertEqual(kinds(True, f"on: push\njobs:\n  test:\n    runs-on: {CI_RUNNER}\n"), [])

    def test_hard_coded_runners_are_flagged(self):
        for runs_on in ("ubuntu-latest", "[self-hosted, amos]", "${{ matrix.os }}"):
            with self.subTest(runs_on=runs_on):
                text = f"on: push\njobs:\n  test:\n    runs-on: {runs_on}\n"
                self.assertEqual(kinds(True, text), [("test", "hard-coded")])

    def test_container_release_runner_input(self):
        call = "on: push\njobs:\n  release:\n    uses: huynle/.github/.github/workflows/container-release.yml@v1\n"
        self.assertEqual(kinds(True, call), [("release", "hard-coded")], "the v1 default is self-hosted")
        self.assertEqual(kinds(True, call + "    with:\n      runner: ubuntu-latest\n"), [("release", "hard-coded")])
        for runner in ("ci-runner", "''", "${{ vars.CI_RUNNER && 'self-hosted' || 'ubuntu-latest' }}"):
            with self.subTest(runner=runner):
                self.assertEqual(kinds(True, call + f"    with:\n      runner: {runner}\n"), [])

    def test_other_reusable_calls_are_not_judged(self):
        text = "on: push\njobs:\n  fleet:\n    uses: huynle/.github/.github/workflows/fleet-bundles.yml@abc\n"
        self.assertEqual(kinds(True, text), [])


class PublicRepositoryTests(unittest.TestCase):
    def test_hosted_runners_pass(self):
        self.assertEqual(kinds(False, "on: [pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"), [])

    def test_self_hosted_on_pull_request_is_external(self):
        for event in ("pull_request", "pull_request_target", "issue_comment", "workflow_run"):
            with self.subTest(event=event):
                text = f"on:\n  {event}:\njobs:\n  test:\n    runs-on: [self-hosted, amos]\n"
                self.assertEqual(kinds(False, text), [("test", "public-self-hosted-external")])

    def test_self_hosted_on_trusted_events_is_still_reported(self):
        text = "on:\n  schedule:\n    - cron: '0 9 * * *'\njobs:\n  sync:\n    runs-on: [self-hosted, amos]\n"
        self.assertEqual(kinds(False, text), [("sync", "public-self-hosted")])

    def test_self_hosted_expressions_and_matrix_values(self):
        expression = "${{ vars.USE_VPS_RUNNER == 'true' && fromJSON('[\"self-hosted\",\"omni-build\"]') || 'ubuntu-latest' }}"
        self.assertEqual(kinds(False, f"on: pull_request\njobs:\n  build:\n    runs-on: {expression}\n"),
                         [("build", "public-self-hosted-external")])
        matrix = ("on: pull_request\njobs:\n  build:\n    runs-on: ${{ matrix.runner }}\n    strategy:\n      matrix:\n"
                  "        include:\n          - runner: ubuntu-24.04\n          - runner: [self-hosted, amos]\n")
        self.assertEqual(kinds(False, matrix), [("build", "public-self-hosted-external")])
        hosted_matrix = matrix.replace("[self-hosted, amos]", "ubuntu-24.04-arm")
        self.assertEqual(kinds(False, hosted_matrix), [])

    def test_container_release_default_is_self_hosted(self):
        call = "on: push\njobs:\n  release:\n    uses: huynle/.github/.github/workflows/container-release.yml@v1\n"
        self.assertEqual(kinds(False, call), [("release", "public-self-hosted")])
        for runner in ("ubuntu-latest", "ci-runner"):
            with self.subTest(runner=runner):
                self.assertEqual(kinds(False, call + f"    with:\n      runner: {runner}\n"), [])

    def test_reusable_workflow_definitions_are_skipped(self):
        text = "on:\n  workflow_call:\njobs:\n  release:\n    runs-on: [self-hosted, amos]\n"
        self.assertEqual(kinds(False, text), [])


class ReportTests(unittest.TestCase):
    def test_markdown_warns_when_private_repositories_were_not_listed(self):
        report = runner_audit.Report("huynle", private_listed=False, repos=3)
        text = runner_audit.markdown(report)
        self.assertIn("RUNNER_AUDIT_TOKEN", text)
        self.assertIn("No findings.", text)

    def test_markdown_escapes_table_cells(self):
        report = runner_audit.Report("huynle", private_listed=True, repos=1, findings=[
            runner_audit.Finding("huynle/x", "ci.yml", "build", "hard-coded", "a || b")])
        self.assertIn("a \\|\\| b", runner_audit.markdown(report))


class AuditWorkflowTests(unittest.TestCase):
    def test_audit_never_runs_on_pull_requests_or_self_hosted(self):
        triggers = re.search(r"^on:\n(?P<body>(?:  .*\n|\s*\n)+)", AUDIT_WORKFLOW, re.MULTILINE).group("body")
        self.assertNotIn("pull_request", triggers)
        self.assertIn("schedule:", triggers)
        self.assertIn("workflow_dispatch:", triggers)
        self.assertEqual(re.findall(r"runs-on: (.*)", AUDIT_WORKFLOW), ["ubuntu-latest"])

    def test_audit_token_is_only_read_by_the_audit_step(self):
        self.assertEqual(AUDIT_WORKFLOW.count("secrets.RUNNER_AUDIT_TOKEN"), 1)
        self.assertIn("persist-credentials: false", AUDIT_WORKFLOW)


if __name__ == "__main__":
    unittest.main()
