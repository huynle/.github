import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate_inputs.py"


def run_validator(**overrides):
    payload = {
        "image": "ghcr.io/huynle/example-app",
        "context": ".",
        "dockerfile": "Dockerfile",
        "platforms": "linux/amd64",
        "build_args_json": '{"APP_ENV":"production","REVISION":"abc-123"}',
        "runner": "self-hosted",
        "sha": "0123456789abcdef0123456789abcdef01234567",
    }
    payload.update(overrides)
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "output"
        result = subprocess.run(
            [sys.executable, str(VALIDATOR)],
            input=json.dumps(payload),
            text=True,
            capture_output=True,
            env={"GITHUB_OUTPUT": str(output)},
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
