import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from test_fleet_bundles import JOBS, TEXT, WORKFLOW, run_validator, step

FETCH = "Fetch signed platform"
REQUIRES = shutil.which("zstd") and shutil.which("tar")


class PlatformFetchWorkflowTests(unittest.TestCase):
    def test_plan_and_render_fetch_the_same_signed_platform(self):
        plan, render = step("plan", FETCH), step("render", FETCH)
        self.assertEqual(plan["run"], render["run"], "the two jobs must not drift apart")
        script = plan["run"]
        for required in ("oras pull", "fleet-bundle verify-platform", "platform-release", "0.0.0", "tar --zstd --no-same-owner -xf"):
            self.assertIn(required, script)
        self.assertNotIn("${{", script)
        self.assertEqual(plan["id"], "platform")
        self.assertEqual(render["id"], "platform")
        for job in ("plan", "render"):
            env = step(job, FETCH)["env"]
            self.assertEqual(env["FLEET_DIR"], "${{ inputs.fleet-dir }}")
            self.assertEqual(env["TENANT"], "${{ inputs.tenant }}")
        self.assertEqual(step("plan", FETCH)["env"]["PLATFORM_REPOSITORY"], "${{ steps.validate.outputs.platform_repository }}")
        self.assertEqual(step("render", FETCH)["env"]["PLATFORM_REPOSITORY"], "${{ needs.plan.outputs.platform }}")

    def test_fetch_happens_after_its_tools_exist_and_before_use(self):
        for job, tool_step, use_step in (("plan", "Install renderer dependencies", "Find affected machines"),
                                         ("render", "Install platform fetch tools", "Render")):
            names = [c.get("name") for c in JOBS[job]["steps"]]
            self.assertLess(names.index(tool_step), names.index(FETCH), job)
            self.assertLess(names.index(FETCH), names.index(use_step), job)

    def test_affected_and_render_receive_the_platform_directory(self):
        affected = step("plan", "Find affected machines")
        self.assertEqual(affected["env"]["PLATFORM_DIR"], "${{ steps.platform.outputs.dir }}")
        self.assertIn('--platform "$PLATFORM_DIR"', affected["run"])
        self.assertGreaterEqual(affected["run"].count("platform_args"), 4)
        render = step("render", "Render")
        self.assertEqual(render["env"]["PLATFORM_DIR"], "${{ steps.platform.outputs.dir }}")
        self.assertIn('--platform "$PLATFORM_DIR"', render["run"])
        # every renderer invocation passes it, including the reproducibility re-render
        self.assertEqual(len(re.findall(r'"\./\$RENDER" bundle ', render["run"])),
                         len(re.findall(r'"\./\$RENDER" bundle [^\n]*\n?[^\n]*platform_args', render["run"])))

    def test_pull_request_planning_still_avoids_the_bundle_registry(self):
        script = step("plan", "Find affected machines")["run"]
        pr_branch = script.split('elif [ "$EVENT" = push ]', 1)[0]
        self.assertNotIn("oras ", pr_branch)
        self.assertNotIn("GITHUB_TOKEN", pr_branch)
        self.assertNotIn("GITHUB_TOKEN", step("plan", FETCH)["run"])

    def test_render_job_has_no_new_permissions_and_checksums_oras(self):
        self.assertEqual(JOBS["render"]["permissions"], {"contents": "read"})
        tools = step("render", "Install platform fetch tools")["run"]
        self.assertIn("sha256sum --check", tools)
        self.assertIn("go -C .fleet-verifier build", tools)
        self.assertIn("./cmd/fleet-bundle", tools)

    def test_platform_repository_input_is_validated(self):
        inputs = WORKFLOW["on"]["workflow_call"]["inputs"]
        self.assertEqual(inputs["platform-repository"]["default"], "ghcr.io/huynle/fleet-platform")
        result, output = run_validator()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("platform_repository=ghcr.io/huynle/fleet-platform", output)
        for bad in ("docker.io/huynle/fleet-platform", "ghcr.io/HuyNle/fleet-platform", "ghcr.io/x;id", "", "ghcr.io"):
            with self.subTest(bad=bad):
                result, _ = run_validator(platform=bad)
                self.assertNotEqual(result.returncode, 0, bad)


@unittest.skipUnless(REQUIRES, "zstd and tar are required")
class PlatformFetchScriptTests(unittest.TestCase):
    """Runs the real step script with stubbed oras and fleet-bundle."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        fleet = self.tmp / "repo" / "fleet"
        (fleet / "tenants").mkdir(parents=True)
        (fleet / "keys").mkdir()
        self.fleet = fleet
        # the platform artifact oras will "pull"
        tree = self.tmp / "artifact"
        (tree / "roles" / "linux" / "demo").mkdir(parents=True)
        (tree / "manifest.json").write_text("{}")
        (tree / "roles" / "linux" / "demo" / "main.yml").write_text("---\n")
        self.archive = self.tmp / "platform.tar.zst"
        subprocess.run(f"tar --create --file - -C {tree} . | zstd -q -o {self.archive}", shell=True, check=True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.calls = self.tmp / "calls.log"
        self.stub("oras", f'echo "oras $*" >>{self.calls}\n[ "${{STUB_ORAS_FAIL:-}}" = 1 ] && {{ echo denied >&2; exit 1; }}\n'
                          f'out=""; while [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\ncp {self.archive} "$out/platform.tar.zst"\n')
        self.stub("fleet-bundle", f'echo "fleet-bundle $*" >>{self.calls}\n[ "${{STUB_VERIFY_FAIL:-}}" = 1 ] && {{ echo bad signature >&2; exit 1; }}\nexit 0\n')

    def stub(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)

    def tenant(self, version):
        text = "id: home\nname: Home\n" + (f"platform_version: {version}\n" if version is not None else "")
        (self.fleet / "tenants" / "home.yaml").write_text(text)

    def run_step(self, **env):
        script = step("plan", FETCH)["run"]
        out = self.tmp / "output"
        out.write_text("")
        full = {"PATH": f"{self.bin}:/usr/bin:/bin", "FLEET_DIR": str(self.fleet), "TENANT": "home",
                "PLATFORM_REPOSITORY": "ghcr.io/huynle/fleet-platform", "RUNNER_TEMP": str(self.tmp / "runner"),
                "GITHUB_OUTPUT": str(out), **env}
        (self.tmp / "runner").mkdir(exist_ok=True)
        result = subprocess.run(["bash", "-c", script], text=True, capture_output=True, env=full, cwd=self.tmp)
        outputs = dict(line.split("=", 1) for line in out.read_text().splitlines() if "=" in line)
        calls = self.calls.read_text() if self.calls.exists() else ""
        return result, outputs, calls

    def keys(self, *names):
        for name in names:
            (self.fleet / "keys" / name).write_text("untrusted comment: x\nRWQ\n")

    def test_unpublished_platform_version_skips_without_touching_the_registry(self):
        for version in ("0.0.0", None):
            with self.subTest(version=version):
                self.tenant(version)
                result, outputs, calls = self.run_step()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(outputs.get("dir"), "")
                self.assertEqual(calls, "")

    def test_pulls_verifies_with_every_release_key_and_exposes_the_directory(self):
        self.tenant("0.1.0")
        self.keys("platform-release-1.pub", "platform-release-2.pub", "platform-root-1.pub", "ssh-ca-home.pub")
        result, outputs, calls = self.run_step()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("oras pull ghcr.io/huynle/fleet-platform:0.1.0 -o", calls)
        verify = [line for line in calls.splitlines() if line.startswith("fleet-bundle verify-platform")][0]
        self.assertIn("platform-release-1.pub", verify)
        self.assertIn("platform-release-2.pub", verify)
        self.assertNotIn("platform-root", verify, "root keys sign tenant certificates, not the platform")
        self.assertIn("-version 0.1.0", verify)
        directory = Path(outputs["dir"])
        self.assertTrue((directory / "roles" / "linux" / "demo" / "main.yml").is_file())
        self.assertTrue(verify.rstrip().endswith(str(directory)), verify)

    def test_refuses_to_continue_without_a_release_key(self):
        self.tenant("0.1.0")
        self.keys("platform-root-1.pub")
        result, outputs, calls = self.run_step()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("platform-release", result.stdout + result.stderr)
        self.assertNotIn("oras", calls)
        self.assertNotIn("dir", outputs)

    def test_failed_signature_check_exposes_nothing(self):
        self.tenant("0.1.0")
        self.keys("platform-release-1.pub")
        result, outputs, _ = self.run_step(STUB_VERIFY_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("dir", outputs)

    def test_pull_failure_explains_the_likely_cause(self):
        self.tenant("0.1.0")
        self.keys("platform-release-1.pub")
        result, outputs, calls = self.run_step(STUB_ORAS_FAIL="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("public", result.stdout + result.stderr)
        self.assertNotIn("verify-platform", calls)
        self.assertNotIn("dir", outputs)

    def test_malformed_versions_never_reach_the_registry(self):
        self.keys("platform-release-1.pub")
        for version in ("latest", "1.2", "v1.2.3", "1.2.3-rc1", "1.2.3; id", "../x", "01.2.3"):
            with self.subTest(version=version):
                (self.fleet / "tenants" / "home.yaml").write_text(f"id: home\nplatform_version: '{version}'\n")
                if self.calls.exists():
                    self.calls.unlink()
                result, _, calls = self.run_step()
                self.assertNotEqual(result.returncode, 0, version)
                self.assertEqual(calls, "", version)


if __name__ == "__main__":
    unittest.main()
