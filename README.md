# CommunityMods

The default mod index for TavernLauncher: static JSON telling the launcher where each mod's files are (in the mod's own GitHub releases) and how to install them.

- **Players:** nothing to set up. Open **Mod Manager → Community Mods** in the launcher. You can add other sources under **Manage Sources**, but nobody reviews them, so only add ones you trust.
- **Mod authors:** see [docs/SUBMITTING.md](docs/SUBMITTING.md).
- **Hosting your own source:** serve the layout in [docs/REPO_STRUCTURE.md](docs/REPO_STRUCTURE.md) from any static host. Players add it by its base URL under **Manage Sources**; a headless server with `modmanager addrepo <url>`. For a browsable page, copy `tools/page.html`, `tools/modindex.py` and `.github/workflows/pages.yml`, and set **Settings → Pages → Source** to *GitHub Actions*.

## Trust

- This repo: automated checks, a VirusTotal scan and a maintainer's review before anything merges. Trusted by default.
- A source you add: unreviewed, shown as unverified.
- Every install, from any source, is hash-checked against its manifest and lands only in `Mods/` or `UserLibs/`.
