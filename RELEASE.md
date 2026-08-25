# Release checklist

## Before publishing

- [ ] Confirm repository name and public owner (`chriopter/hermes-hey`).
- [ ] Review trademark wording and independent-project disclaimer.
- [ ] Run the full test suite with at least 80% package coverage.
- [ ] Run `uvx ruff check .`.
- [ ] Run `uvx pyright` with the pinned Hermes source path.
- [ ] Run `hermes plugins doctor . --ci`.
- [ ] Build both wheel and source distribution.
- [ ] Run the read-only live HEY identity and watch-handshake smoke tests.
- [ ] Scan tracked files for secrets and real HEY account/sender/thread data.
- [ ] Verify GitHub Actions and the Hermes checkout use immutable SHAs.
- [ ] Review the complete staged diff with an independent reviewer.
- [ ] Verify Author and Committer are Christopher's public GitHub identity.

## Publish

```bash
VERSION=0.1.1
git push -u origin main
gh release create "v${VERSION}" "dist/hermes_hey-${VERSION}-py3-none-any.whl" \
  "dist/hermes_hey-${VERSION}.tar.gz" \
  --title "hermes-hey v${VERSION}" \
  --notes-file CHANGELOG.md
```

## Announce

- HEY CLI community
- Nous Research Discord `#plugins-skills-and-skins`
- Link to the official HEY CLI
- Clearly label the plugin as independent and technology preview

## Post-release

- [ ] Install from the public Git repository into an isolated Hermes profile.
- [ ] Verify plugin discovery and gateway connection from the release tag.
- [ ] Verify a clean install in a temporary Hermes profile.
- [ ] Send one synthetic authorized email and verify one same-thread reply.
