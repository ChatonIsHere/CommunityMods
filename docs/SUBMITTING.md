# Submitting a mod

This is about this repo, the default index every launcher trusts. It covers how a submission gets checked, who signs off, and how to get your mod in.

If you'd rather host your own source that users add by hand, you don't need any of this, just serve the layout in [REPO_STRUCTURE.md](REPO_STRUCTURE.md). This repo is one such source, it only adds a review process on top.

## Why there's a review

A mod is a `.dll` that runs in the game with full access to the player's machine. Every launcher trusts the default source, so anything merged here is something we're vouching for. Two layers guard that:

1. Automated validation catches the mechanical mistakes.
2. A human reviews it before merge for everything automation can't.

A source a user adds themselves gets neither, which is why the launcher flags it as unverified. The point of the default repo is that it's been through both.

## Layer 1: automated validation

When you open a submission the tooling checks it and rejects anything that fails:

- **PR shape**: a submission opened from a fork must **add exactly one file**, your new manifest under `submissions/<author>.<repo>/<version>.json`, and change nothing else. A PR that also edits tooling, workflows, `repository.json`, or another mod's manifest is rejected automatically. To submit or update several mods, open a separate PR for each.

It then checks the manifest against the schema in [REPO_STRUCTURE.md](REPO_STRUCTURE.md) and rejects anything that fails:

- **id format**: `"<github-user>.<github-repo>"` matching your real GitHub `owner/repo`, with no path-dangerous segment (`/`, `\`, `.`, `..`). Every by-id lookup and the on-disk path come from the id, so this is a hard gate.
- **manifest_version** is `2`. A version 1 manifest (the old `client_side` / `server_side` shape with one top-level `download_url`) is rejected with a message explaining the move, and so is any v1 field left at the top level of a v2 manifest (`client_side`, `server_side`, `download_url`, `sha256`, `package`, `library_dependencies`): those now live in `artifacts`, with the two sides replaced by each artifact's `targets`.
- **version** is exactly three integers, no pre-release or build suffix.
- **parity_required** is present and a boolean. It's mandatory, so you answer it deliberately rather than inheriting a default: `true` means a client joining a server that runs your mod must have this exact version or the join is refused. Set it `false` when your mod's client and server builds work independently, so a player without it costs nobody else anything, and the launcher will offer it rather than insist on it. It only matters for a mod with a client target (`pc` or `quest`) that also has a `server` build. Setting it `false` when the two halves actually have to agree produces broken sessions rather than a clear rejection, so only reach for it when you're sure. If you're not, `true` is the safe answer.
- **scenes**, if present, is a non-empty list of distinct scene names out of `normal`, `quest` and `tutorial`, naming the scenes your mod works in. Leave it out if it works in all of them; that's the default, and listing all three is rejected in favour of leaving it out. Only set it when you know your mod breaks in a scene: a headless server running a scene you didn't list will skip your mod, and players won't keep it pinned on for that server.
- **dependencies** is present (`{}` for none), and every entry is a valid mod id with a `MAJOR.MINOR.PATCH` minimum.
- **artifacts** is a non-empty list, one entry per distinct build your mod ships. Each artifact lists the **targets** it's built for, out of `pc` (the game on a PC: SteamVR, a Quest over Link, or flycam), `server` (a game server, which always runs the PC build) and `quest` (the standalone Quest headset; accepted, but nothing installs it yet). A mod that ships one DLL for players and servers has a single artifact with `"targets": ["pc", "server"]`; a mod with a separate server build has two. Any other target name is rejected here, because the installers silently skip names they don't know, so a typo would ship a build nothing ever installs. A target may appear in only one artifact. A target no artifact lists is one your mod doesn't support.
- **Each artifact** is checked on its own, and a failure names the artifact it's about:
  - **download_url** must be `https://` and a GitHub release asset under your own repo. You can't ship someone else's binary as any build of your mod.
  - **sha256** must be 64 lowercase hex characters, and the tool downloads the file and checks it. A mismatch is rejected. This is also where the real hashes get confirmed, see below.
  - **package** isn't something you write, leave it out. The tooling sniffs each file's own bytes during validation and ingest (a zip starts with `PK`, anything else is a dll) and writes `package` into the compiled manifest for you, so it can never disagree with the actual file.
  - **library_dependencies** is present (`[]` for none). It's per artifact because native libraries differ by platform. Each entry needs a `name`, a bare-basename `filename` (no directory part, no colon), an `https://` `download_url` and a `sha256`, which is downloaded and checked like the artifact's own.
  - **zip contents** (`package:"zip"` only): once the archive is downloaded and hash-verified, its contents are checked. It must be a valid zip; contain **at least one `.dll` at the archive root**, since the root becomes the build's folder and the mod's main assembly belongs there (MelonLoader would also load DLLs in subfolders, but a zip whose only DLLs are tucked into subfolders is almost always a packaging mistake, so it's a hard reject; several root `.dll`s are fine); and stay within safety caps: no member with an absolute path, a colon, a `..` that escapes the folder, or a symlink; at most a couple thousand entries; and a bounded total decompressed size and per-file compression ratio, so a decompression bomb is rejected. These mirror the guards the launcher re-applies when it extracts, so an unreviewed third-party repo gets the same protection.
- **dependency targets** must overlap with your own. Every `dependencies` entry that this repo can resolve is checked against the version the launcher would actually install (highest in the named major, at or above your minimum), comparing every target either mod has a build for. A dependency that shares no target with your mod — a `server`-only mod depending on a `pc`-only mod, or the reverse — is rejected: installers resolve dependencies per target and leave out one with no build for it, so that edge could never be satisfied anywhere your mod runs. Targets that merely *differ* are fine and expected: a `pc` + `server` mod depending on a `pc`-only mod is how you say "my client build needs this", and installers simply leave that dependency out on servers. That draws a note asking you to confirm your server build really doesn't touch it, since no manifest can state that and only your code knows. A dependency this repo doesn't carry can't be checked at all, so it draws a note too — users will need that other source added before they can install you.
- **malware scan**: files go through VirusTotal (around 70 antivirus engines). Every single-file (`package:"dll"`) artifact and every library is scanned whole. For a `package:"zip"` artifact, only the **code-bearing members** are scanned: each `.dll`/`.exe`/script (identified by extension *or* by magic bytes, so a renamed executable is still caught), each uploaded individually. The bulky inert parts of a bundle (3D models, textures, audio, Unity asset bundles) aren't executed and aren't uploaded, which keeps every scan small and well under VirusTotal's size limit, a big bundle scanned as one archive could exceed it and get no scan at all. Each distinct file is scanned **once**, however many artifacts or libraries it appears in: files are matched by sha256, so a library both your builds pin costs one scan, not two. The result is reported for the maintainer to weigh, not an automatic gate. Game mods trip AV heuristics all the time because they patch memory at runtime, so a hit means a closer look rather than an instant no. A maintainer can set a hard threshold, but by default the scan reports and the human decides. Runs when the repo has a `VIRUSTOTAL_API_KEY` set, everything else runs regardless. The free VirusTotal key allows only 4 requests/minute, and a submission with several files or zip members can need more than that, so the check **paces itself**, it waits for the next slot rather than skipping a scan, and if VirusTotal still says "too many requests" it waits and retries. This can make the scan step take a few minutes on a busy submission; that's expected, not a hang. (A maintainer with a higher-tier key can raise the ceiling via the `VT_MAX_PER_MINUTE` env var.)

Libraries are treated differently on purpose: a `library_dependencies` download_url isn't ownership-checked, because the whole point is pinning a third-party project nobody here owns (a shared codec, say). It still has to be `https://`, and its `sha256` is still verified.

Either way, the checks post a comment on your PR with the full result, pass or fail, so you always see the outcome without opening the Actions log. A failure lists exactly what to fix. Push an update and the checks (and the comment) refresh automatically.

## Layer 2: human review

Automation can confirm a hash, not whether the mod or its dependencies are trustworthy. That's a judgement call:

> Every merge to `main` needs a maintainer's review and approval.

No submission merges by automation alone. A maintainer looks at what the mod does, where its dependencies come from, and whether the library URLs point somewhere sensible. Passing Layer 1 is necessary, not enough.

## How to submit

1. **Publish your mod** as a GitHub release on your own repo, with each build (a single `.dll`, or a `.zip` for a build with several DLLs or asset bundles) as a release asset at a stable URL. One file can serve several targets; a mod that runs the same DLL for players and servers needs only one. If a build needs any pinned libraries, upload those to the release too (see [REPO_STRUCTURE.md](REPO_STRUCTURE.md) on `library_dependencies`) or locate the published .dll in another GitHub repository.
2. **Write your manifest** following [REPO_STRUCTURE.md](REPO_STRUCTURE.md). Copy [`submissions/TEMPLATE.json`](../submissions/TEMPLATE.json) as a starting point and move it to `submissions/<author>.<repo>/<version>.json`. Your `id` is your `owner/repo` with the `/` turned into a `.`. List one entry in `artifacts` per build, with the `targets` it serves, and fill in the `download_url` for each artifact and each library; you can leave the `sha256` fields as a placeholder for the next step.
3. **Fill in the hashes.** From a clone of this repo, run:
    ```
    python tools/modindex.py fill-hashes submissions/<id>/<version>.json
    ```
    It downloads each file your manifest points at (every artifact and every library) and writes its real `sha256` in. (You can also compute them by hand if you prefer, e.g. `sha256sum`.) Do this _after_ the release is published, since it fetches the actual files.
4. **Open a pull request** that adds it under `submissions/<id>/<version>.json`, e.g. `submissions/ExampleDev.ExampleMod/1.2.0.json`. Drop only your manifest, don't touch `repository.json`, `manifests/`, or the pointers. Those are generated.
5. **Automated validation runs** on the PR. It re-downloads every file and verifies the hashes, so if you change a build, re-run `fill-hashes` and push again. Fix anything it flags.
6. **A maintainer reviews, approves, and merges.**
7. **On merge** the compiler moves your manifest into `manifests/<author>/<repo>/<version>.json`, regenerates the `latest.json` / `latest.<major>.json` pointers, and rebuilds `repository.json`. Your mod is live for every launcher on the next index refresh.

### About the hashes

The `sha256` values are yours to commit: they pin the exact bytes that ship, and a maintainer approves that specific hash before it merges. Validation re-downloads and verifies them, so a manifest with a wrong or stale hash can't pass and nothing ships unverified. `fill-hashes` just saves you computing them by hand.

### Updating a mod

Same flow with a new `version`. Old per-version manifests stay forever, since a server pinned to an older version still has to resolve it. The pointers and `repository.json` move forward to your newest release.

---

The validation and compiler tooling lives in [tools/modindex.py](../tools/modindex.py) and runs from [.github/workflows/](../.github/workflows/): validate on every submission PR (Layer 1), build on merge to `main` (the compiler). A third-party source needs none of it, only the layout in [REPO_STRUCTURE.md](REPO_STRUCTURE.md). The policy here, that schema plus mandatory human review, is fixed regardless.
