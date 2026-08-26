# Release checklist

## Before publishing

- Confirm repository name, public owner, and Christopher-only Git identity.
- Review trademark wording and independent-project disclaimer.
- Run Python tests with at least 80% package coverage.
- Run pinned Ruff and Pyright against the pinned Hermes source.
- Run `go test -race ./...`, `go vet ./...`, and `go build .` from `sidecar/`.
- Run `hermes plugins doctor . --ci`.
- Build wheel and source distribution.
- Build and install the sidecar from the exact release source.
- Run read-only SDK identity and watch-ready smoke tests.
- Scan tracked files for secrets and real account, sender, thread, message, or event data.
- Verify GitHub Actions, setup actions, Hermes checkout, and SDK dependency are pinned.
- Review the complete staged diff with an independent reviewer.
- Verify Author and Committer are `Christopher <82179548+chriopter@users.noreply.github.com>`.
- Verify release commits and the release tag are intentionally unsigned: no GPG/SSH signature and no GitHub-generated `Verified` web-merge commit.

## Publish

```bash
VERSION=0.2.0
RELEASE_BRANCH=refactor/sdk-sidecar
git checkout "$RELEASE_BRANCH"
git rebase origin/main
git push --force-with-lease origin "$RELEASE_BRANCH"
# Open a PR for review and CI, but do not merge it in the GitHub web UI.
git checkout main
git pull --ff-only origin main
git merge --ff-only "$RELEASE_BRANCH"
git tag "v${VERSION}" # lightweight and unsigned; do not use -s
git push origin main "v${VERSION}"
```

The release identity policy is deliberate: Christopher authors and commits locally, then fast-forwards `main` and creates a lightweight unsigned tag. GitHub's web merge would create a server-signed `Verified` commit with a different committer path, so it must not be used. Close the reviewed PR after the fast-forward push if GitHub does not close it automatically.

## Post-release

- Install plugin and sidecar from the public release in an isolated profile.
- Verify plugin discovery and SDK identity/account checks.
- Verify a clean first-run cursor baseline does not replay history.
- Send one new synthetic authorized email containing an image and a thread with a Collab comment.
- Read back one visible same-thread email reply and one Collab-comment response; verify each used only its matching transport and pending state cleared only afterward.
