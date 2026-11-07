# huynle organization workflows

Public, organization-wide GitHub configuration and reusable workflows. The release workflow contains **build credentials only**: it receives the calling repository's `GITHUB_TOKEN` and never receives infrastructure credentials.

## Container release v1

Call `.github/workflows/container-release.yml@v1` only after repository-specific tests pass:

```yaml
permissions:
  contents: read
  packages: write

jobs:
  test:
    runs-on: ${{ vars.CI_RUNNER && fromJSON(vars.CI_RUNNER) || 'ubuntu-latest' }}
    steps:
      - uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4.2.2
      - run: ./scripts/test

  release:
    needs: test
    uses: huynle/.github/.github/workflows/container-release.yml@v1
    with:
      image: ghcr.io/huynle/my-app
      context: .
      dockerfile: Dockerfile
      # Private repositories: the org variable CI_RUNNER (see "Runner selection");
      # `runner: ci-runner` once v1 contains it. Public repositories: ubuntu-latest.
      runner: ${{ vars.CI_RUNNER && 'self-hosted' || 'ubuntu-latest' }}
      # JSON object only; use Docker BuildKit secrets in a dedicated workflow
      # rather than putting secrets in build arguments.
      build-args-json: '{"APP_ENV":"production"}'
```

The caller must grant `contents: read` and `packages: write`. No PAT, deploy key, cloud credential, or `secrets: inherit` is accepted. For a private calling repository, its `GITHUB_TOKEN` creates or updates only that repository-associated GHCR package under the calling owner.

### Contract

Inputs are typed by `workflow_call` and validated before registry login:

The validator is inline in the reusable workflow. This is intentional: in an external reusable call, caller-scoped SHA contexts identify the caller repository, not `huynle/.github`. Keeping validation in the workflow selected by `@v1` avoids a second checkout and ensures validation changes follow the same reviewed version boundary as the release contract.

| Input | Type | Default | Rules |
| --- | --- | --- | --- |
| `image` | string | required | Lowercase `ghcr.io/...` path |
| `context` | string | `.` | Repository-relative path; cannot escape checkout |
| `dockerfile` | string | `Dockerfile` | Repository-relative file; cannot escape checkout |
| `platforms` | string | `linux/amd64` | Unique list of `linux/amd64`, `linux/arm64` |
| `build-args-json` | string | `{}` | JSON object with uppercase environment-style keys and scalar, single-line values |
| `runner` | string | `self-hosted` | `ci-runner` (caller's `vars.CI_RUNNER`, else `ubuntu-latest`; an empty string means the same), `self-hosted` (`[self-hosted, amos]`), `github-ci` (`[self-hosted, github-ci]`), or `ubuntu-latest` |
| `provenance` | boolean | `true` | BuildKit provenance attestation |
| `sbom` | boolean | `true` | BuildKit SBOM attestation |

### Runner selection (CI_RUNNER)

Policy: Brain infra decision 8hrn3rpu and checklist 5p4j8gna. Private huynle repositories select every runner
from the organization Actions variable `CI_RUNNER` (visibility: private repositories), a JSON label list such as
`["self-hosted","amos","ci"]` for the shared `huynle-ci` runner on amos. When it is unset, jobs run GitHub-hosted:

```yaml
runs-on: ${{ vars.CI_RUNNER && fromJSON(vars.CI_RUNNER) || 'ubuntu-latest' }}
```

A reusable workflow sees the **caller's** variables: "For reusable workflows, the variables from the caller
workflow's repository are used. Variables from the repository that contains the called workflow are not made
available to the caller workflow." ([Variables reference](https://docs.github.com/en/actions/reference/workflows-and-actions/variables);
`vars` is allowed in `jobs.<job_id>.runs-on`, [Contexts reference](https://docs.github.com/en/actions/reference/workflows-and-actions/contexts)).
So `runner: ci-runner` resolves `CI_RUNNER` as the calling repository sees it. Public callers never see the
private-only org variable and therefore get `ubuntu-latest`, as the policy requires.

| Caller passes | Runs on |
| --- | --- |
| `runner: ci-runner` or `runner: ''` | caller's `CI_RUNNER` labels; unset: `ubuntu-latest` |
| nothing (default `self-hosted`) | `[self-hosted, amos]`, unchanged v1 behavior; public repositories should pass `ubuntu-latest` |
| `self-hosted`, `github-ci`, `ubuntu-latest` | unchanged v1 behavior |

`ci-runner` is additive. Until `v1` contains it (see "Updating pinned actions and v1"), private callers use the
expression that every `v1` accepts:

```yaml
      runner: ${{ vars.CI_RUNNER && 'self-hosted' || 'ubuntu-latest' }}
```

The default stays `self-hosted`: changing it would move existing callers that pass no `runner`.

`fleet-bundles.yml` keeps `ubuntu-latest` for now: its jobs install packages with `sudo apt-get` and `pip` outside
the workspace and its publish job holds the tenant signing key, so it is not host-safe on the shared runner yet.

Jobs on the shared runner must be host-safe: Docker-assigned host ports, container names that include
`github.run_id`, `trap` cleanup, no kind or other clusters, and writes only inside the workspace.

Outputs are `digest`, `git-tag`, and `sha-tag`. Every successful build publishes exactly two immutable identities:

- `git-<UTC YYYYMMDDHHMMSS>-<12-char-sha>` — sortable Flux promotion tag.
- `sha-<full-40-char-sha>` — exact source identity.

Images include OCI source, revision, and version labels. The workflow does not publish or deploy `latest`.

`v1` is an additive compatibility line: existing inputs, output names, validation semantics, tags, and labels cannot be removed or changed incompatibly. Sensitive callers may pin the workflow to a full commit SHA instead.

## Verification fixture

`fixture/.github/workflows/release.yml` is a copyable external caller contract. It gates release on a fixture test, uses `@v1`, and publishes `ghcr.io/huynle/container-release-fixture`. `tests/` provides offline contract and negative-input checks. A fixture run should verify:

1. both immutable tags resolve to the reported `digest`;
2. `docker buildx imagetools inspect` shows source, revision, and version OCI labels;
3. the GHCR package displays provenance and SBOM attestations;
4. an invalid path, image, platform, runner, or build-args object fails before login/build;
5. a failed caller test skips the release job and creates no image.

## Fleet bundles v1

`.github/workflows/fleet-bundles.yml` renders, signs and pushes one fleet bundle per affected machine of a fleet
tenant repository (huynle/infra is the home tenant; contract: huynle/fleet `docs/contract.md` §4). Each tenant repo
calls it with its own tenant id and holds its own signing key; this workflow holds no key.

```yaml
permissions:
  contents: read

jobs:
  bundles:
    permissions:
      contents: read
      packages: write
    uses: huynle/.github/.github/workflows/fleet-bundles.yml@<reviewed v1 commit> # v1
    with:
      tenant: home
```

| Event | What runs |
| --- | --- |
| `pull_request` | Render dry-run of the affected machines: throwaway TEST root and tenant key made in the job, sign, pack twice (byte-identical), verify, and `ansible-playbook --syntax-check -c local -i localhost, -e @vars.yml` per layer (how fleet-agent runs it). No secret, no registry. |
| `push` to `main` | Render; then job `publish` in the caller's environment `fleet-signing` signs with `FLEET_SIGNING_KEY`, verifies against `<fleet-dir>/keys/platform-root*.pub` and pushes. |
| anything else | Nothing is signed or pushed. |

Skips with a notice (not a failure) while `<fleet-dir>/keys/tenant-key-cert.json` is not committed or the
environment has no `FLEET_SIGNING_KEY`. Machines the renderer cannot build yet (exit 3: Windows, platform roles
without the platform artifact) are skipped with a warning.

| Input | Default | Rules |
| --- | --- | --- |
| `tenant` | required | DNS label |
| `fleet-dir` | `fleet` | Repository-relative path |
| `registry-repository` | `ghcr.io/<lowercase owner>/fleet-bundles` | Lowercase `ghcr.io/...` repository |
| `render`, `affected`, `test-keys` | `scripts/fleet/{render,affected,test-keys}` | Repository-relative paths in the caller |

Registry layout (contract §4): `<registry>:<tenant>-<machine>-<sequence>` and the moving tag
`<tenant>-<machine>-current`, which only ever moves to a higher sequence. An existing sequence tag is never
overwritten with a different signed manifest. Layers: `bundle.tar.zst`, `manifest.json`, `manifest.json.minisig`,
`tenant-key-cert.json`, `tenant-key-cert.json.minisig`; annotations `org.huynle.fleet.{tenant,machine,sequence,
manifest-sha256}`. The sequence is the first-parent commit count of the rendered commit on `main`.

Caller environment `fleet-signing`: deployment branches `main` only, required reviewers, secret `FLEET_SIGNING_KEY`
(the unencrypted minisign tenant secret key, `minisign -G -W`). Key handling: huynle/infra `docs/runbooks/fleet.md`.

## Workflow templates

`workflow-templates/huynle-ci.yml` appears under **Actions → New workflow** in every huynle repository. It carries
the `CI_RUNNER` header, a host-safe container smoke test and a `container-release@v1` call. Public repositories
replace `runs-on` with `ubuntu-latest`.

## Runner policy audit

`.github/workflows/runner-audit.yml` runs weekly (Mondays 06:23 UTC) and on manual dispatch, on `ubuntu-latest`,
with no `pull_request` trigger. `scripts/runner_audit.py` reads every non-archived repository's default-branch
workflows and lists:

- private repositories with a job whose `runs-on` does not use `vars.CI_RUNNER`, or a `container-release` call
  whose `runner` is fixed (including the `self-hosted` default);
- public repositories with self-hosted runners, separately those on externally triggerable events
  (`pull_request`, `pull_request_target`, `issue_comment`, `workflow_run`, ...).

It opens or updates one issue titled **Runner policy audit** (closed when there are no findings) and writes the
same report to the job summary. Run it locally with `AUDIT_TOKEN=$(gh auth token) python3 scripts/runner_audit.py`.

`GITHUB_TOKEN` cannot list another repository's private content, so without a token the audit covers public
repositories only and says so. Owner setup for private repositories:

1. Org huynle → Settings → Personal access tokens → Settings: allow fine-grained tokens (approval required is fine).
2. Your account → Settings → Developer settings → Fine-grained tokens → Generate: resource owner **huynle**,
   repository access **All repositories**, repository permissions **Contents: Read-only** (Metadata: Read-only is
   added automatically), expiration up to 1 year. Approve the request under Org → Personal access tokens if asked.
   Actions: Read-only is optional; it only marks disabled workflows.
3. huynle/.github → Settings → Environments → New environment **runner-audit** → Deployment branches: selected
   branch `main` only → Add environment secret **RUNNER_AUDIT_TOKEN** with the token.
4. Actions → Runner policy audit → Run workflow; the summary must not show the "No private repository was listed"
   warning. Renew the token before it expires.

## Maintainer checks

```bash
python3 -m unittest discover -s tests -v
actionlint
```

## Updating pinned actions and v1

1. Review upstream release notes and resolve the desired release tag to its commit SHA with the GitHub API.
2. Replace the 40-character SHA and adjacent version comment in the workflow and fixture.
3. Run all maintainer checks and the fixture workflow from the feature branch.
4. Merge through a reviewed PR.
5. Fast-forward the protected `v1` branch to the tested `main` commit (`git push origin <main-sha>:refs/heads/v1`, a fast-forward). Never force-push or retarget `v1` to an unreviewed commit. Additive inputs such as `runner: ci-runner` reach `@v1` callers only after this step.
6. Inspect the fixture package's two tags, digest, labels, provenance, and SBOM.

Required repository/organization settings:

- Actions must be allowed to run pinned GitHub-owned and Docker actions.
- Private caller repositories must be allowed to use public reusable workflows (GitHub's normal default).
- Keep workflow token permissions restricted by default; callers explicitly request `packages: write`.
- Protect `main` and `v1`; disallow force pushes. Configure GHCR package visibility deliberately (private by default, public only when intended).
