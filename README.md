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
    runs-on: ubuntu-latest
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
      # Default: self-hosted runner labels [self-hosted, amos].
      runner: self-hosted
      # JSON object only; use Docker BuildKit secrets in a dedicated workflow
      # rather than putting secrets in build arguments.
      build-args-json: '{"APP_ENV":"production"}'
```

The caller must grant `contents: read` and `packages: write`. No PAT, deploy key, cloud credential, or `secrets: inherit` is accepted. For a private calling repository, its `GITHUB_TOKEN` creates or updates only that repository-associated GHCR package under the calling owner.

### Contract

Inputs are typed by `workflow_call` and validated before registry login:

| Input | Type | Default | Rules |
| --- | --- | --- | --- |
| `image` | string | required | Lowercase `ghcr.io/...` path |
| `context` | string | `.` | Repository-relative path; cannot escape checkout |
| `dockerfile` | string | `Dockerfile` | Repository-relative file; cannot escape checkout |
| `platforms` | string | `linux/amd64` | Unique list of `linux/amd64`, `linux/arm64` |
| `build-args-json` | string | `{}` | JSON object with uppercase environment-style keys and scalar, single-line values |
| `runner` | string | `self-hosted` | `self-hosted` (`[self-hosted, amos]`) or `ubuntu-latest` |
| `provenance` | boolean | `true` | BuildKit provenance attestation |
| `sbom` | boolean | `true` | BuildKit SBOM attestation |

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
5. Fast-forward the protected `v1` branch to the tested `main` commit. Never force-push or retarget `v1` to an unreviewed commit.
6. Inspect the fixture package's two tags, digest, labels, provenance, and SBOM.

Required repository/organization settings:

- Actions must be allowed to run pinned GitHub-owned and Docker actions.
- Private caller repositories must be allowed to use public reusable workflows (GitHub's normal default).
- Keep workflow token permissions restricted by default; callers explicitly request `packages: write`.
- Protect `main` and `v1`; disallow force pushes. Configure GHCR package visibility deliberately (private by default, public only when intended).
