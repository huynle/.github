#!/usr/bin/env python3
"""Validate and normalize container-release inputs without invoking a shell parser."""
import json
import os
import posixpath
import re
import sys

IMAGE_RE = re.compile(r"^ghcr\.io/[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?(?:/[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?)+$")
PLATFORM_RE = re.compile(r"^linux/(?:amd64|arm64)(?:,linux/(?:amd64|arm64))*$")
KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
ALLOWED_RUNNERS = {'self-hosted': '["self-hosted","amos"]', 'ubuntu-latest': '"ubuntu-latest"'}
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def fail(message):
    print(f"::error::{message}", file=sys.stderr)
    raise SystemExit(1)


def relative_path(value, name, allow_dot=False):
    if not value or value.startswith("/") or "\\" in value:
        fail(f"{name} must be a non-empty repository-relative POSIX path")
    normalized = posixpath.normpath(value)
    if normalized == ".." or normalized.startswith("../"):
        fail(f"{name} must not escape the repository")
    if normalized == "." and not allow_dot:
        fail(f"{name} must name a file")
    return normalized


def emit(name, value):
    output = os.environ.get("GITHUB_OUTPUT")
    if not output:
        fail("GITHUB_OUTPUT is not set")
    with open(output, "a", encoding="utf-8") as stream:
        if "\n" in value:
            marker = "__CONTAINER_RELEASE_OUTPUT__"
            stream.write(f"{name}<<{marker}\n{value}\n{marker}\n")
        else:
            stream.write(f"{name}={value}\n")


def main():
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        fail(f"invalid validator payload: {error}")

    image = data.get("image", "")
    if not IMAGE_RE.fullmatch(image):
        fail("image must be a lowercase ghcr.io path (for example ghcr.io/huynle/app)")

    context = relative_path(data.get("context", ""), "context", allow_dot=True)
    dockerfile = relative_path(data.get("dockerfile", ""), "dockerfile")
    platforms = data.get("platforms", "")
    if not PLATFORM_RE.fullmatch(platforms) or len(platforms.split(",")) != len(set(platforms.split(","))):
        fail("platforms must be a unique comma-separated list of linux/amd64 and/or linux/arm64")

    runner = data.get("runner", "")
    if runner not in ALLOWED_RUNNERS:
        fail('runner must be self-hosted or ubuntu-latest')

    sha = data.get("sha", "")
    if not SHA_RE.fullmatch(sha):
        fail("sha must be a lowercase 40-character Git commit SHA")

    try:
        build_args = json.loads(data.get("build_args_json", "{}"))
    except json.JSONDecodeError as error:
        fail(f"build_args_json must be a JSON object: {error}")
    if not isinstance(build_args, dict):
        fail("build_args_json must be a JSON object")

    normalized_args = []
    for key in sorted(build_args):
        value = build_args[key]
        if not KEY_RE.fullmatch(key):
            fail(f"invalid build argument name: {key}")
        if not isinstance(value, (str, int, float, bool)) or value is None:
            fail(f"build argument {key} must have a scalar value")
        text = str(value).lower() if isinstance(value, bool) else str(value)
        if any(character in text for character in "\r\n\x00") or any(fragment in text for fragment in ("$(", "${", "`")):
            fail(f"build argument {key} contains a forbidden control character or shell fragment")
        normalized_args.append(f"{key}={text}")

    emit("image", image)
    emit("context", context)
    emit("dockerfile", dockerfile)
    emit("platforms", platforms)
    emit("runner", ALLOWED_RUNNERS[runner])
    emit("short_sha", sha[:12])
    emit("build_args", "\n".join(normalized_args) if normalized_args else " ")


if __name__ == "__main__":
    main()
