# tools/

The automation for this repo. Standard library only, no `pip install`, so it runs in CI and in any fork with a bare Python 3.

You don't need any of this to host your own source, a third-party repo only has to serve the layout in [../docs/REPO_STRUCTURE.md](../docs/REPO_STRUCTURE.md). See [../docs/SUBMITTING.md](../docs/SUBMITTING.md) for how these checks fit the review process.

## modindex.py

```
python tools/modindex.py fill-hashes [paths...]
python tools/modindex.py validate [paths...] [--vt-max-malicious N]
python tools/modindex.py build [--ingest] [--prune]
```

### fill-hashes

A convenience for submitters. Downloads the files a manifest points at (the mod plus every library) and writes their real `sha256` in, so you don't have to compute them by hand. Default target is everything under `submissions/`. Run it after your release is published, since it fetches the actual files. The hashes stay yours to commit; `validate` still re-verifies them.

### validate

Layer 1, runs on every submission PR. Validates submission manifests (default: everything under `submissions/`). For each one it checks the schema, the id and ownership rules, filename safety, version format, and the client/server sides, then downloads and hashes the mod and every library against the manifest's `sha256`. With a `VIRUSTOTAL_API_KEY` set it also runs each file through VirusTotal (hash lookup first, upload only if VirusTotal hasn't seen the file) and reports the result. Pass `--vt-max-malicious N` to also fail when the malicious count goes over N. Exits non-zero on any hard failure.

### build

The index compiler, runs on merge to `main`. Reads every `manifests/**/<version>.json` and regenerates each mod's `latest.json` / `latest.<major>.json` pointers and the top-level `repository.json` (a slim summary per major with a `versions` list, no urls/hashes/deps). With `--ingest` it first validates and moves `submissions/**/*.json` into `manifests/<author>/<repo>/<version>.json`, and `--prune` then deletes the moved files. It's idempotent, running it with no new submissions rewrites the same bytes.

## Keeping the rules in sync

The validation rules here mirror the launcher's parser (`TavernLauncher/modmanager.py`) and the schema in `docs/REPO_STRUCTURE.md`. Change one, change all three. The client and the repo have to agree on what a valid manifest is.

## GitHub Actions

- `.github/workflows/validate.yml` runs `validate` on every submission PR and posts the result (pass or fail) as a PR comment. It uses `pull_request_target` so the VirusTotal scan and the comment work even for PRs from forks: the job runs in the base-repo context (where the secret and a write token exist), but it checks out base `main` and overlays only the PR's `submissions/` data, so no PR-supplied code ever runs with that access. It also gates on shape first: a fork PR must add exactly one file under `submissions/<author>.<repo>/<version>.json` and change nothing else, so a submission can't smuggle in edits to tooling, workflows, or the index. The full reasoning is in the workflow's header comment.
- `.github/workflows/publish.yml` runs `build --ingest --prune` on merge to `main` and commits the regenerated index.

Because validation runs where secrets live, the URLs a submission names are fetched with a guard: `_assert_safe_to_fetch` refuses anything but `https://` and rejects hosts that resolve to loopback, private, link-local, or other non-public addresses. That closes `file://` local reads and SSRF against the runner. `validate_structure` also rejects any non-`https` `download_url` up front.
