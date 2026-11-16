# Bundled integration

The app image carries the Codex integration of the same release in this folder, and
`integration_sync.py` installs it in `/config/custom_components/codex_cli` when the app starts.

The integration's source is `custom_components/codex_cli` at the top of the repository. It is not kept
here: the build copies it in just before `docker build`, as the build and verification workflows do:

```sh
rm -rf codex-cli-worker/integration/codex_cli
cp -R custom_components/codex_cli codex-cli-worker/integration/codex_cli
```

An image built without that step has no bundled integration; the app then installs nothing and logs
`no_bundle` at start.
