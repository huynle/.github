import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/container-release.yml").read_text()


def inline_validator():
    match = re.search(r"python3 - <<'PY'\n(?P<body>.*?)\n\s+PY$", WORKFLOW, re.MULTILINE | re.DOTALL)
    if not match:
        raise AssertionError("inline validator heredoc not found")
    lines = match.group("body").splitlines()
    indentation = min(len(line) - len(line.lstrip()) for line in lines if line.strip())
    return "\n".join(line[indentation:] for line in lines) + "\n"


def run_validator(**overrides):
    payload = {
        "INPUT_IMAGE": "ghcr.io/huynle/example-app",
        "INPUT_CONTEXT": ".",
        "INPUT_DOCKERFILE": "Dockerfile",
        "INPUT_PLATFORMS": "linux/amd64",
        "INPUT_BUILD_ARGS_JSON": '{"APP_ENV":"production","REVISION":"abc-123"}',
        "INPUT_RUNNER": "self-hosted",
        "INPUT_SHA": "0123456789abcdef0123456789abcdef01234567",
    }
    payload.update({f"INPUT_{key.upper()}": value for key, value in overrides.items()})
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "output"
        result = subprocess.run(
            [sys.executable, "-c", inline_validator()],
            text=True,
            capture_output=True,
            env={**os.environ, **payload, "GITHUB_OUTPUT": str(output)},
        )
        values = output.read_text() if output.exists() else ""
    return result, values


class ValidateInputsTests(unittest.TestCase):
    def test_valid_input_emits_normalized_values(self):
        result, output = run_validator()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("short_sha=0123456789ab", output)
        self.assertIn("build_args<<", output)
        self.assertIn("APP_ENV=production", output)

    def test_rejects_unsafe_paths(self):
        for field, value in (("context", "/tmp/build"), ("context", "../app"), ("dockerfile", "app/../../Dockerfile")):
            with self.subTest(field=field, value=value):
                result, _ = run_validator(**{field: value})
                self.assertNotEqual(result.returncode, 0)

    def test_rejects_malformed_image_platform_and_runner(self):
        cases = (
            {"image": "docker.io/huynle/app"},
            {"image": "ghcr.io/Huynle/App"},
            {"platforms": "linux/amd64; id"},
            {"platforms": "linux/amd64,linux/arm/v7"},
            {"runner": '["self-hosted","prod"]'},
        )
        for case in cases:
            with self.subTest(case=case):
                result, _ = run_validator(**case)
                self.assertNotEqual(result.returncode, 0)

    def test_rejects_unstructured_or_multiline_build_args(self):
        cases = ("FOO=bar", '["FOO=bar"]', '{"BAD-KEY":"x"}', '{"FOO":"ok\\nbad"}', '{"FOO":{"nested":true}}', '{"FOO":"$(id)"}', '{"FOO":"`id`"}')
        for value in cases:
            with self.subTest(value=value):
                result, _ = run_validator(build_args_json=value)
                self.assertNotEqual(result.returncode, 0)

    def test_accepts_empty_build_args_and_hosted_runner(self):
        result, output = run_validator(build_args_json="{}", runner="ubuntu-latest")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("build_args= ", output)


if __name__ == "__main__":
    unittest.main()
