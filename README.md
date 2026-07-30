# CommunityMods

The default mod index for The Modding Tavern modding through TavernLauncher. Every launcher trusts this repo out of the box, so you can browse and install what's here straight from the **Community Mods** menu with no setup.

Nothing here runs. It's a set of static JSON files that say where each mod's files live (in the mods' own GitHub releases) and how to install them. The launcher reads them directly, so any plain static host works and there's no backend to keep alive.

## Layout

```
repository.json                        the full catalogue, in one file
manifests/<author>/<repo>/
  <version>.json                       one immutable file per release
  latest.json                          pointer to the highest version overall
  latest.<major>.json                  pointer to the highest version in a major
submissions/                           where new-mod pull requests land
docs/                                  the schema and the submission process
```

## For players

You don't need anything here directly, the launcher already points at it. Open **Mods > Community Mods** and install. You can add other sources under **Manage Sources**, but nobody checks those, so the launcher warns you when you do: a mod is a `.dll` that runs with full access to your machine, so only add sources you trust.

## For mod authors

Want your mod in the default list? See [docs/SUBMITTING.md](docs/SUBMITTING.md) for the details. You publish your `.dll` and other necessary files in a GitHub release, write a manifest, and open a PR that drops it in `submissions/`. Automated checks validate the structure and hashes, then a maintainer reviews it before it merges. You never touch `repository.json` or the pointer files, those get generated on merge.

## For your own source

The launcher reads any repo that serves the layout in [docs/REPO_STRUCTURE.md](docs/REPO_STRUCTURE.md). No GitHub Actions, no special hosting, just static JSON at stable URLs. Users add your source by its raw-content base URL under **Manage Sources**. This repo is one such source with a review process on top and some automations to make it easier for all of us.

## Trust

- This repo: structural validation, a VirusTotal scan of every file, and a maintainer's review before anything merges. Trusted by launchers by default.
- A source you add yourself: no review, shown as unverified. You vet it.
- Every install, from any source, is hash-checked against the manifest before it's written, and can only ever land in `Mods/` or `UserLibs/`. The hash check is what ties the file you install to the one that was scanned and reviewed here.
