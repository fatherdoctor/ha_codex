# Bundled integration

The app image carries the Codex integration of the same release in this folder, and
`integration_sync.py` installs it in `/config/custom_components/codex_cli` when the app starts.

The integration's source is `custom_components/codex_cli` at the top of the repository. It is not kept
here: the build copies it in just before `docker build`, as the build and verification workflows do:

```sh
python3 scripts/bundle_integration.py
```

Run it before every build by hand too. An image built without that step has no bundled integration; the app then
installs nothing and logs `no_bundle` at start. An image built from an old copy is worse: it carries a stale
integration with the same version number, and the app installs that copy in `/config`. To check a copy without
changing it, run `python3 scripts/bundle_integration.py --check` (exit 1 lists each missing, extra or changed file).
