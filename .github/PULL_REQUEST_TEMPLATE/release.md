<!-- Release pull request template. Open a release PR with ?template=release.md and a title of "Release <version>".
     Paste the evidence on each line; do not tick a box without it. -->
## Release

Version: `<version>`
Release branch and head commit: `<branch>` at `<sha>`

## Checklist

Do the items in this order. C1 to C8 run on the release head; the order of merge and tag is under C9.

- [ ] **C1 Tests and CI.** Full `pytest` passes on Python 3.14 and every GitHub check passes on the release head, with no skip added during the release. Evidence: the `pytest` summary line and the commit the checks ran on.
- [ ] **C2 Review.** Every pull request in the release passed its closer (a high-risk one also a second read), and one full review of the whole release diff against the last main is done, each finding fixed or declined in writing. Evidence: the list of closer verdicts and the link to the full review.
- [ ] **C3 Versions and changelog.** `codex-cli-worker/config.yaml` and `custom_components/codex_cli/manifest.json` carry the same new version, and the changelog heading no longer says `(unreleased)`. Evidence: both version lines, and the output of `python scripts/check_version_bump.py` and `python scripts/build_hacs_release.py v<version> <folder>` (both exit 0).
- [ ] **C4 Integration copies match.** `codex-cli-worker/integration/codex_cli` is identical to `custom_components/codex_cli`. Evidence: the passing integration-copy check, or an empty diff of the two folders.
- [ ] **C5 Docs and screenshots.** README, DOCS.md and the other files CONTRIBUTING.md lists say what this release does; screenshots are retaken or listed in `screenshots/NEEDED.md`. Evidence: the list of files checked.
- [ ] **C6 Lab pass.** The release installs on a clean test machine by following the README word for word. Evidence: the lab-pass record, committed, naming the tested commit; `python scripts/fork_release.py check-tag <version>` passes.
- [ ] **C7 Smoke test on a test install.** The automated smoke checks pass on the release image on a test Home Assistant install (a lab VM), never the maintainer's live home. Evidence: the lab-pass record and the result lines.
- [ ] **C8 Manual test.** The maintainer runs through the release's Do and Expect steps after C1 to C7 and says "ship it". Skip if the release has only docs or CI changes; C1-C7 are enough. Evidence: the maintainer's words and date, or `skipped: docs and CI only`.
- [ ] **C9 Merge and tag.** Merge to main the way the plan says (a merge commit or fast-forward of the tested head, so the tagged commit is the tested commit), then tag with `python scripts/fork_release.py tag <version> --push`. Never delete a tag; roll back with `fork_release.py rollback`. Evidence: the merge commit, the tag, and the commit the lab-pass record names.
- [ ] **C10 Carry forward.** Every fix made on the release branch after the freeze is also in the next branch before the tag. Evidence: the diff check output, empty or listing only intended differences.

## Notes

Anything a reviewer should know, such as a deferred item or an exception to the list above.
