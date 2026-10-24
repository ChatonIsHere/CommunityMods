# submissions/

Drop folder for new mod submissions. To submit or update a mod, open a pull request that adds only your manifest here:

```
submissions/<author>.<repo>/<version>.json
```

for example:

```
submissions/ExampleDev.ExampleMod/1.2.0.json
```

Copy [`TEMPLATE.json`](TEMPLATE.json) in this folder as a starting point: move it to `submissions/<author>.<repo>/<version>.json` and fill in the fields. It's a manifest_version 2 file with one artifact serving `["pc", "server"]`, the shape for a mod that runs the same DLL for players and servers; add an artifact per build if yours differ, and drop a target you don't support. `TEMPLATE.json` itself is reserved and ignored by the checks, so leave it in place.

Don't edit `repository.json`, `manifests/`, or any `latest*.json` pointer. Those get generated when your submission is approved and merged.

See [../docs/SUBMITTING.md](../docs/SUBMITTING.md) for the full process and [../docs/REPO_STRUCTURE.md](../docs/REPO_STRUCTURE.md) for the manifest schema.
