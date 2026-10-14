import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/container-release.yml").read_text()
FIXTURE = (ROOT / "fixture/.github/workflows/release.yml").read_text()
LIVE_FIXTURE = (ROOT / ".github/workflows/fixture-release.yml").read_text()


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
