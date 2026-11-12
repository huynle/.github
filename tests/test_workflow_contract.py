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
    def test_optional_branch_head_guard_blocks_old_release_before_tagging(self):
        self.assertRegex(WORKFLOW, r"(?m)^      require-branch-head:\n(?:        .*\n)*?        default: false$")
        self.assertIn("value: ${{ jobs.release.outputs.published }}", WORKFLOW)
        self.assertIn("published: ${{ steps.build.outcome == 'success' }}", WORKFLOW)
        self.assertIn("git ls-remote --exit-code origin", step_script("Require current branch HEAD"))
        self.assertIn("refs/heads/", step_script("Require current branch HEAD"))
        self.assertIn("skipped:", step_script("Require current branch HEAD"))
        guarded = WORKFLOW.split("      - name: Require current branch HEAD", 1)[1]
        for step in ("Create immutable release identity", "Set up QEMU", "Set up Docker Buildx", "Log in to GHCR with caller token", "Generate OCI metadata", "Build and push immutable image", "Publish release summary"):
            match = re.search(r"      - name: " + re.escape(step) + r"\n(?P<body>.*?)(?=\n      - name:|\Z)", guarded, re.DOTALL)
            self.assertIsNotNone(match, step)
            self.assertIn("steps.guard.outputs.published == 'true'", match.group("body"), step)

    def test_branch_head_guard_matches_or_skips_a_local_branch_without_publishing(self):
        script = step_script("Require current branch HEAD")
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "caller"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "fixture@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "fixture"], check=True)
            (repo / "README").write_text("first\n")
            subprocess.run(["git", "-C", str(repo), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "first"], check=True)
            old = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(repo)], check=True)

            def guard(expected):
                output, summary = Path(tmp) / "output", Path(tmp) / "summary"
                output.write_text("")
                summary.write_text("")
                result = subprocess.run(["bash", "-c", script], cwd=repo, capture_output=True, text=True,
                    env={"PATH": "/usr/bin:/bin", "BRANCH_REF": "main", "EXPECTED_SHA": expected,
                         "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)})
                self.assertEqual(result.returncode, 0, result.stderr)
                return output.read_text(), summary.read_text(), result.stdout

            emitted, summary, notice = guard(old)
            self.assertIn("published=true", emitted)
            self.assertEqual(summary, "")
            self.assertNotIn("skipped:", notice)
            # ls-remote's pattern is a suffix match; a decoy ref must not override the exact branch HEAD.
            subprocess.run(["git", "-C", str(repo), "branch", "x/refs/heads/main"], check=True)
            emitted, summary, notice = guard(old)
            self.assertIn("published=true", emitted)
            self.assertEqual(summary, "")
            self.assertNotIn("skipped:", notice)
            (repo / "README").write_text("new\n")
            subprocess.run(["git", "-C", str(repo), "commit", "-qam", "new"], check=True)
            emitted, summary, notice = guard(old)
            self.assertIn("published=false", emitted)
            self.assertIn("skipped: main is at", summary)
            self.assertIn(old, summary)
            self.assertIn("::notice::skipped:", notice)

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

    def test_runner_input_keeps_v1_values_and_adds_ci_runner(self):
        runs_on = re.search(r"^    runs-on: (.*)$", WORKFLOW, re.MULTILINE).group(1)
        self.assertIn("(inputs.runner == 'ci-runner' || inputs.runner == '')", runs_on)
        self.assertIn("vars.CI_RUNNER && fromJSON(vars.CI_RUNNER) || 'ubuntu-latest'", runs_on)
        self.assertIn(
            "fromJSON(inputs.runner == 'ubuntu-latest' && '\"ubuntu-latest\"' || inputs.runner == 'github-ci' && "
            "'[\"self-hosted\",\"github-ci\"]' || '[\"self-hosted\",\"amos\"]')",
            runs_on,
        )
        default = re.search(r"      runner:\n(?:        .*\n)*?        default: (\S+)", WORKFLOW).group(1)
        self.assertEqual(default, "self-hosted", "changing the default would move existing v1 callers")

    def test_fixture_releases_only_after_test(self):
        self.assertIn("needs: test", FIXTURE)
        self.assertIn("@v1", FIXTURE)
        self.assertNotIn("continue-on-error", FIXTURE)
        self.assertIn("needs: test", LIVE_FIXTURE)
        self.assertIn("uses: ./.github/workflows/container-release.yml", LIVE_FIXTURE)


if __name__ == "__main__":
    unittest.main()
