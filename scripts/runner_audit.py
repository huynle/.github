#!/usr/bin/env python3
"""Weekly runner-policy audit for the huynle organization.

Policy (Brain infra decision 8hrn3rpu, checklist 5p4j8gna):
- private repositories pick every job's runner from the org Actions variable CI_RUNNER:
  runs-on: ${{ vars.CI_RUNNER && fromJSON(vars.CI_RUNNER) || 'ubuntu-latest' }}
- public repositories stay on GitHub-hosted runners; above all, no self-hosted runner on events that people
  outside the organization can trigger (pull_request from forks, pull_request_target, issue_comment, ...).

The script reads the default branch of every repository it can list, prints Markdown, and exits 0. It never
writes to the audited repositories. Standard library plus PyYAML only.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

import yaml

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")

# Events that someone without write access can cause to run code (or that chain from such an event).
EXTERNAL_EVENTS = {
    "pull_request",
    "pull_request_target",
    "pull_request_review",
    "pull_request_review_comment",
    "issue_comment",
    "issues",
    "discussion",
    "discussion_comment",
    "fork",
    "watch",
    "workflow_run",
}
# Labels that only exist on huynle self-hosted runners.
SELF_HOSTED_LABELS = {"self-hosted", "amos", "ci", "github-ci", "omni-build", "omni-light"}
CONTAINER_RELEASE = re.compile(r"^huynle/\.github/\.github/workflows/container-release\.ya?ml@")


@dataclass
class Finding:
    repo: str
    workflow: str
    job: str
    kind: str
    detail: str


@dataclass
class Report:
    org: str
    private_listed: bool
    repos: int = 0
    findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def triggers(document: dict) -> set[str]:
    # PyYAML (YAML 1.1) reads the bare key `on` as boolean True.
    value = document.get("on", document.get(True))
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return {str(item) for item in value}
    if isinstance(value, dict):
        return {str(key) for key in value}
    return set()


def uses_ci_runner(value) -> bool:
    return "vars.CI_RUNNER" in json.dumps(value)


def self_hosted(value) -> bool:
    """True when a runs-on value can select a self-hosted runner."""
    if isinstance(value, str):
        if "${{" in value:
            return "self-hosted" in value or "vars.CI_RUNNER" in value
        return value in SELF_HOSTED_LABELS
    if isinstance(value, list):
        return any(self_hosted(item) for item in value)
    if isinstance(value, dict):
        return self_hosted(value.get("labels")) or "group" in value
    return False


def matrix_values(job: dict, runs_on) -> list:
    """Resolve `${{ matrix.<key> }}` to the literal values the matrix can produce."""
    if not isinstance(runs_on, str):
        return [runs_on]
    match = re.fullmatch(r"\$\{\{\s*matrix\.([A-Za-z0-9_-]+)\s*\}\}", runs_on.strip())
    matrix = (job.get("strategy") or {}).get("matrix")
    if not match or not isinstance(matrix, dict):
        return [runs_on]
    key = match.group(1)
    values = list(matrix.get(key) or []) if isinstance(matrix.get(key), list) else []
    values += [entry[key] for entry in matrix.get("include") or [] if isinstance(entry, dict) and key in entry]
    return values or [runs_on]


def audit_workflow(repo: str, private: bool, path: str, text: str) -> list[Finding]:
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        return [Finding(repo, path, "-", "parse-error", str(error).splitlines()[0])]
    if not isinstance(document, dict):
        return []
    events = triggers(document)
    if events == {"workflow_call"}:
        # A reusable workflow resolves runs-on in each caller's context; the callers are audited instead.
        return []
    external = sorted(events & EXTERNAL_EVENTS)
    findings = []
    for name, job in (document.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        uses = job.get("uses")
        if isinstance(uses, str):
            if not CONTAINER_RELEASE.match(uses):
                continue
            # container-release@v1 defaults to runner: self-hosted ([self-hosted, amos]).
            runner = str((job.get("with") or {}).get("runner", "self-hosted"))
            if private:
                if runner not in ("ci-runner", "") and "vars.CI_RUNNER" not in runner:
                    findings.append(Finding(repo, path, name, "hard-coded", f"container-release runner: {runner}"))
            elif runner in ("self-hosted", "github-ci") or ("${{" in runner and "self-hosted" in runner):
                kind = "public-self-hosted-external" if external else "public-self-hosted"
                findings.append(Finding(repo, path, name, kind,
                                        f"container-release runner: {runner}; events: {', '.join(sorted(events))}"))
            continue
        runs_on = job.get("runs-on")
        if runs_on is None:
            continue
        if private:
            if not uses_ci_runner(runs_on):
                findings.append(Finding(repo, path, name, "hard-coded", f"runs-on: {json.dumps(runs_on)}"))
            continue
        if any(self_hosted(value) for value in matrix_values(job, runs_on)):
            kind = "public-self-hosted-external" if external else "public-self-hosted"
            findings.append(Finding(repo, path, name, kind,
                                    f"runs-on: {json.dumps(runs_on)}; events: {', '.join(sorted(events))}"))
    return findings


class GitHub:
    def __init__(self, token: str):
        self.token = token

    def get(self, path: str, accept: str = "application/vnd.github+json"):
        request = urllib.request.Request(API + path, headers={
            "Accept": accept,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "huynle-runner-audit",
            **({"Authorization": f"Bearer {self.token}"} if self.token else {}),
        })
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
        return body.decode() if accept.endswith("raw") else json.loads(body)

    def paged(self, path: str):
        page = 1
        while True:
            separator = "&" if "?" in path else "?"
            items = self.get(f"{path}{separator}per_page=100&page={page}")
            yield from items
            if len(items) < 100:
                return
            page += 1


def run(org: str, token: str) -> Report:
    github = GitHub(token)
    repos = [repo for repo in github.paged(f"/orgs/{org}/repos?type=all") if not repo.get("archived")]
    report = Report(org, private_listed=any(repo.get("private") for repo in repos))
    for repo in sorted(repos, key=lambda item: item["name"].lower()):
        name, ref = repo["full_name"], repo.get("default_branch") or "main"
        report.repos += 1
        quoted_ref = urllib.parse.quote(ref, safe="")
        try:
            entries = github.get(f"/repos/{name}/contents/.github/workflows?ref={quoted_ref}")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                report.errors.append(f"{name}: listing workflows failed (HTTP {error.code})")
            continue
        try:
            states = {item["path"]: item["state"] for item in github.get(f"/repos/{name}/actions/workflows?per_page=100")
                      .get("workflows", [])}
        except urllib.error.HTTPError:
            states = None  # the token lacks Actions: read; do not guess workflow states
        for entry in entries if isinstance(entries, list) else []:
            if entry.get("type") != "file" or not re.search(r"\.ya?ml$", entry["name"]):
                continue
            try:
                text = github.get(f"/repos/{name}/contents/{urllib.parse.quote(entry['path'])}?ref={quoted_ref}",
                                  accept="application/vnd.github.raw")
            except urllib.error.HTTPError as error:
                report.errors.append(f"{name}/{entry['path']}: read failed (HTTP {error.code})")
                continue
            found = audit_workflow(name, bool(repo.get("private")), entry["path"], text)
            state = "active" if states is None else states.get(entry["path"], "not registered")
            for finding in found:
                if state != "active":
                    finding.detail += f" (workflow {state.replace('_', ' ')})"
            report.findings += found
    return report


def escape(text: str) -> str:
    return text.replace("|", "\\|")


def markdown(report: Report) -> str:
    groups = (
        ("public-self-hosted-external", "Public repositories: self-hosted runner on an externally triggerable event"),
        ("public-self-hosted", "Public repositories: self-hosted runner (policy: GitHub-hosted only)"),
        ("hard-coded", "Private repositories: runner hard-coded instead of CI_RUNNER"),
        ("parse-error", "Workflows that could not be parsed"),
    )
    lines = [f"Runner policy audit of `{report.org}`: {report.repos} repositories, {len(report.findings)} findings.",
             "", "Policy: Brain infra decision 8hrn3rpu. Private jobs use "
             "`runs-on: ${{ vars.CI_RUNNER && fromJSON(vars.CI_RUNNER) || 'ubuntu-latest' }}`; public repositories "
             "stay on GitHub-hosted runners.", ""]
    if not report.private_listed:
        lines += ["> [!WARNING]", "> No private repository was listed: the token cannot read private repositories "
                  "(set the `RUNNER_AUDIT_TOKEN` secret, see README). Only public repositories were audited.", ""]
    for kind, title in groups:
        rows = [finding for finding in report.findings if finding.kind == kind]
        if not rows:
            continue
        lines += [f"### {title} ({len(rows)})", "", "| Repository | Workflow | Job | Detail |", "| --- | --- | --- | --- |"]
        lines += [f"| {f.repo} | `{f.workflow}` | `{f.job}` | {escape(f.detail)} |" for f in rows]
        lines.append("")
    if report.errors:
        lines += ["### Errors", ""] + [f"- {error}" for error in report.errors] + [""]
    if not report.findings and not report.errors:
        lines.append("No findings.")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org", default=os.environ.get("GITHUB_REPOSITORY_OWNER", "huynle"))
    parser.add_argument("--json-out", metavar="PATH", help="also write the findings as JSON to PATH")
    args = parser.parse_args()
    report = run(args.org, os.environ.get("AUDIT_TOKEN", ""))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as stream:
            json.dump({"private_listed": report.private_listed, "repos": report.repos, "errors": report.errors,
                       "findings": [finding.__dict__ for finding in report.findings]}, stream, indent=2)
    sys.stdout.write(markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
