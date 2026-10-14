import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/container-release.yml").read_text()
FIXTURE = (ROOT / "fixture/.github/workflows/release.yml").read_text()
LIVE_FIXTURE = (ROOT / ".github/workflows/fixture-release.yml").read_text()


def step_script(step_name):
    match = re.search(
        rf"- name: {re.escape(step_name)}\n(?P<step>.*?)(?=\n\s{{6}}- name:|\Z)",
        WORKFLOW,
        re.DOTALL,
    )
    if not match:
        raise AssertionError(f"workflow step not found: {step_name}")
    script = re.search(r"\n\s+run: \|\n(?P<body>.*)", match.group("step"), re.DOTALL)
    if not script:
        raise AssertionError(f"run script not found: {step_name}")
    lines = script.group("body").splitlines()
    indentation = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
    return "\n".join(line[indentation:] for line in lines) + "\n"


class WorkflowContractTests(unittest.TestCase):
    def test_actions_are_pinned_to_full_commit_shas(self):
        uses = re.findall(r"uses:\s+([^\s#]+)", WORKFLOW + "\n" + FIXTURE + "\n" + LIVE_FIXTURE)
        remote = [value for value in uses if not value.startswith("./") and not value.startswith("huynle/.github/")]
        self.assertTrue(remote)
        for value in remote:
            self.assertRegex(value, r"^[^@]+@[0-9a-f]{40}$")

    def test_contract_has_immutable_tags_labels_and_attestations(self):
        for required in (
            "git-${timestamp}-${SHORT_SHA}",
            "sha-${FULL_SHA}",
            "org.opencontainers.image.source",
            "org.opencontainers.image.revision",
            "org.opencontainers.image.version",
            "provenance: ${{ inputs.provenance }}",
            "sbom: ${{ inputs.sbom }}",
            "digest: ${{ steps.build.outputs.digest }}",
        ):
            self.assertIn(required, WORKFLOW)
        self.assertNotRegex(WORKFLOW, r"type=raw,value=latest|\n\s*latest\s*$")

    def test_release_tag_uses_the_exact_emitted_short_sha_output(self):
        emitted = set(re.findall(r'emit\("([a-z_]+)"', WORKFLOW))
        validate_references = set(re.findall(r"steps\.validate\.outputs\.([a-z_-]+)", WORKFLOW))
        self.assertIn("short_sha", emitted)
        self.assertIn("short_sha", validate_references)
        self.assertNotIn("short-sha", validate_references)
        self.assertIn("build_args", validate_references)
        self.assertNotIn("build-args", validate_references)
        self.assertTrue(validate_references <= emitted)

        script = step_script("Create immutable release identity")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "output"
            result = subprocess.run(
                ["bash", "-c", script],
                capture_output=True,
                text=True,
                env={
                    "GITHUB_OUTPUT": str(output),
                    "PATH": "/usr/bin:/bin",
                    "SHORT_SHA": "0123456789ab",
                    "FULL_SHA": "0123456789abcdef0123456789abcdef01234567",
                },
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        self.assertRegex(values["git_tag"], r"^git-[0-9]{14}-[0-9a-f]{12}$")
        self.assertEqual(values["sha_tag"], "sha-0123456789abcdef0123456789abcdef01234567")

    def test_job_and_workflow_outputs_use_consistent_expression_keys(self):
        self.assertIn("git-tag: ${{ steps.release.outputs.git_tag }}", WORKFLOW)
        self.assertIn("sha-tag: ${{ steps.release.outputs.sha_tag }}", WORKFLOW)
        self.assertIn("value: ${{ jobs.release.outputs['git-tag'] }}", WORKFLOW)
        self.assertIn("value: ${{ jobs.release.outputs['sha-tag'] }}", WORKFLOW)

    def test_security_boundary_uses_only_caller_token(self):
        self.assertIn("${{ secrets.GITHUB_TOKEN }}", WORKFLOW)
        self.assertNotIn("secrets: inherit", WORKFLOW)
        self.assertNotRegex(WORKFLOW, r"INFRA|PAT|SSH_KEY|DEPLOY_KEY")
        self.assertIn("packages: write", WORKFLOW)
        self.assertIn("contents: read", WORKFLOW)

    def test_external_call_does_not_checkout_validator_at_caller_sha(self):
        """Reusable-call github.workflow_sha belongs to the caller repository."""
        self.assertNotIn("github.workflow_sha", WORKFLOW)
        self.assertNotIn("repository: huynle/.github", WORKFLOW)
        self.assertNotIn("Check out release validator", WORKFLOW)
        self.assertEqual(WORKFLOW.count("actions/checkout@"), 1)
        self.assertIn("name: Validate release inputs", WORKFLOW)
        self.assertIn("python3 - <<'PY'", WORKFLOW)

    def test_fixture_releases_only_after_test(self):
        self.assertIn("needs: test", FIXTURE)
        self.assertIn("@v1", FIXTURE)
        self.assertNotIn("continue-on-error", FIXTURE)
        self.assertIn("needs: test", LIVE_FIXTURE)
        self.assertIn("uses: ./.github/workflows/container-release.yml", LIVE_FIXTURE)


if __name__ == "__main__":
    unittest.main()
