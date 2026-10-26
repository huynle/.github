import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / ".github/workflows/fleet-bundles.yml"
TEXT = PATH.read_text()
WORKFLOW = yaml.load(TEXT, Loader=yaml.BaseLoader)  # BaseLoader keeps "on" as a string key
JOBS = WORKFLOW["jobs"]


def step(job, name):
    for candidate in JOBS[job]["steps"]:
        if candidate.get("name") == name:
            return candidate
    raise AssertionError(f"step {name!r} not found in job {job}")


def run_validator(**overrides):
    env = {
        "INPUT_TENANT": "home",
        "INPUT_FLEET_DIR": "fleet",
        "INPUT_REGISTRY": "",
        "INPUT_RENDER": "scripts/fleet/render",
        "INPUT_AFFECTED": "scripts/fleet/affected",
        "INPUT_TEST_KEYS": "scripts/fleet/test-keys",
        "OWNER": "HuyNle",
    }
    env.update({f"INPUT_{k.upper()}": v for k, v in overrides.items() if k != "owner"})
    if "owner" in overrides:
        env["OWNER"] = overrides["owner"]
    script = step("plan", "Validate inputs")["run"]
    body = re.search(r"python3 - <<'PY'\n(?P<body>.*?)\nPY\n?$", script, re.DOTALL)
    assert body, "inline validator heredoc not found"
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "output"
        result = subprocess.run([sys.executable, "-c", body.group("body")], text=True, capture_output=True,
                                env={**os.environ, **env, "GITHUB_OUTPUT": str(output)})
        values = output.read_text() if output.exists() else ""
    return result, values


class FleetBundlesWorkflowTests(unittest.TestCase):
    def test_only_callable_as_reusable_workflow(self):
        self.assertEqual(list(WORKFLOW["on"]), ["workflow_call"])
        self.assertEqual(WORKFLOW["on"]["workflow_call"]["inputs"]["tenant"]["required"], "true")

    def test_actions_are_pinned_to_full_commit_shas(self):
        uses = re.findall(r"uses:\s+([^\s#]+)", TEXT)
        self.assertTrue(uses)
        for value in uses:
            self.assertRegex(value, r"^[^@]+@[0-9a-f]{40}$")

    def test_minimal_permissions(self):
        self.assertEqual(WORKFLOW["permissions"], {"contents": "read"})
        for name, job in JOBS.items():
            expected = {"contents": "read", "packages": "write"} if name == "publish" else {"contents": "read"}
            self.assertEqual(job.get("permissions"), expected, name)

    def test_signing_key_only_in_publish_job_on_main_push(self):
        for name, job in JOBS.items():
            uses_secrets = "secrets." in yaml.safe_dump(job)
            self.assertEqual(uses_secrets, name == "publish", name)
        publish = JOBS["publish"]
        self.assertEqual(publish["environment"], "fleet-signing")
        condition = " ".join(publish["if"].split())
        self.assertIn("github.event_name == 'push'", condition)
        self.assertIn("github.ref == 'refs/heads/main'", condition)
        self.assertNotIn("||", condition)
        self.assertNotIn("pull_request", condition)

    def test_publish_skips_cleanly_without_key(self):
        check = step("publish", "Check for the signing key")
        self.assertIn("present=false", check["run"])
        self.assertIn("::notice::", check["run"])
        for candidate in JOBS["publish"]["steps"][1:]:
            self.assertEqual(candidate.get("if"), "steps.key.outputs.present == 'true'", candidate.get("name"))

    def test_dry_run_uses_throwaway_test_keys(self):
        script = step("render", "Render")["run"]
        self.assertIn('"./$TEST_KEYS" "$RUNNER_TEMP/test-keys"', script)
        self.assertIn('cmp "$RUNNER_TEMP/$machine-1.tar.zst" "$RUNNER_TEMP/$machine-2.tar.zst"', script)
        self.assertIn('"./$RENDER" verify', script)

    def test_run_scripts_do_not_interpolate_expressions(self):
        for name, job in JOBS.items():
            for candidate in job["steps"]:
                if "run" in candidate:
                    self.assertNotIn("${{", candidate["run"], f"{name}: {candidate.get('name')}")

    def test_registry_tags_follow_the_contract(self):
        script = step("publish", "Push to the registry")["run"]
        self.assertIn('ref="$REGISTRY:$TENANT-$machine-$SEQUENCE"', script)
        self.assertIn('"$TENANT-$machine-current"', script)
        self.assertIn('[ "$at" -lt "$SEQUENCE" ]', script)
        self.assertIn("refusing to overwrite", script)
        for media_type in ("application/vnd.huynle.fleet.bundle.v1.tar+zstd",
                           "application/vnd.huynle.fleet.bundle.manifest.v1+json",
                           "application/vnd.huynle.fleet.minisig.v1"):
            self.assertIn(media_type, script)

    def test_oras_download_is_checksummed(self):
        script = step("publish", "Install tools")["run"]
        self.assertIn("sha256sum --check", script)
        self.assertRegex(WORKFLOW["env"]["ORAS_SHA256"], r"^[0-9a-f]{64}$")

    def test_validator_accepts_defaults_and_lowercases_owner(self):
        result, output = run_validator()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("registry=ghcr.io/huynle/fleet-bundles", output)

    def test_validator_rejects_bad_inputs(self):
        cases = (
            {"tenant": "Home"},
            {"tenant": "home;id"},
            {"fleet_dir": "../fleet"},
            {"fleet_dir": "/etc"},
            {"render": "scripts/fleet/render; id"},
            {"affected": ""},
            {"registry": "docker.io/huynle/fleet-bundles"},
            {"registry": "ghcr.io/HuyNle/fleet-bundles"},
        )
        for case in cases:
            with self.subTest(case=case):
                result, _ = run_validator(**case)
                self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
