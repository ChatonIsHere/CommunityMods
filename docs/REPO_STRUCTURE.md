# Mod repository structure

This is the file layout and JSON schema a repository has to serve to work as a TavernLauncher mod source. It applies to this repo and to any third-party source a user adds under **Manage Sources**. It also documents what the two installers (the launcher and TavernLib) put on disk and say to each other, because both have to agree on it exactly.

There's no build server and no required GitHub Actions. A repository is just static JSON files at stable URLs. Generate them however you like, with a script, a CI job, or by hand. How this repo generates and checks them is a separate doc, [SUBMITTING.md](SUBMITTING.md).

## A repository is one base URL

Every source is identified by one base URL, the raw-content root its files hang off. For a GitHub repo served over raw.githubusercontent.com that's:

```
https://raw.githubusercontent.com/<user>/<repo>/<branch>
```

Everything else is a fixed path under that base. The base URL is exactly what a user pastes into **Add source**.

## Files the launcher reads

```
<base>/repository.json                                the compiled index (required)
<base>/manifests/<author>/<repo>/<version>.json       one per published version
<base>/manifests/<author>/<repo>/latest.json          highest version overall
<base>/manifests/<author>/<repo>/latest.<major>.json  highest version in that major
```

`repository.json` is the only file fetched during normal browsing, the whole catalogue in one request, so static hosting never needs a directory listing. It's a slim discovery index: enough to list mods and pick a version, but no install-critical data. The per-version files are the authoritative full record and are immutable, keep the old ones forever. The `latest` files are pointers, each a copy of one `<version>.json`, so a mod can be fetched by id with a plain GET and no API call.

The launcher browses from `repository.json`, then fetches a mod's per-version manifest only when it actually installs, deriving the URL from the id and version (`manifests/<author>/<repo>/<version>.json`). So download URLs, hashes, and the dependency lists live in exactly one place, the manifest, and can never drift from a copy in the index.

Folder names come from the id, so they aren't free-form. See the id rule below.

## Targets

A **target** is a kind of process a mod's files are built for. There are three:

| Target   | What runs it |
| -------- | ------------ |
| `pc`     | The game on a PC, as a player: SteamVR, a Meta headset over Link (the launcher's "Quest" platform option), or flycam. All of these are the same Windows build. |
| `server` | A game server. Servers only ever run the PC build, launcher-run or headless. |
| `quest`  | The standalone Quest headset (the Android build). Accepted and validated now so the format is ready, but nothing installs a `quest` file yet. |

A target is a binary, not a game mode: which **scene** a server runs (normal, quest, tutorial) is a separate question, answered by `scenes`. A PC player joining a quest-scene server still gets the `pc` files.

## repository.json

```jsonc
{
  "index_version": 2,             // schema version of this index file
  "mods": {
    "<id>": {                     // e.g. "ExampleDev.ExampleMod"
      "<major>": {                // key is the major version as a string: "1", "2"
        "name": "Example Mod",
        "author": "ExampleDev",
        "description": "Does something useful.",
        "parity_required": true,      // see the manifest table
        "targets": ["pc", "server"],  // every target the release has a file for, sorted
        "versions": ["1.0.0", "1.2.0"]   // every release in this major
      },
      ...
    },
    ...
  }
}
```

`index_version` is the schema version of this index file. It's separate from the `manifest_version` on the per-version manifests below: the index and the manifest are different schemas and version independently, so a launcher that doesn't recognise the index version skips the whole file rather than mis-reading it. This build reads `2` only.

Under `mods`, keyed by id, then by major version as a string. One entry per major. A mod with 1.4.0 and 2.1.0 out has both a `"1"` and a `"2"` entry, because a dependency locked to major 1 still has to resolve after major 2 ships. Each entry carries the discovery fields (`name`, `author`, `description`, `parity_required`, `targets`, and `scenes` when that release names any, all taken from the highest release in that major) and `versions`, the list of every release in the major. `targets` is the sorted union of the targets of that release's artifacts. The highest version is `max(versions)`; that's the default install, and resolution picks the highest version at or above a dependency's minimum. The full manifest for any of those versions is one fetch away at `manifests/<author>/<repo>/<version>.json`.

## Manifest

This is the shape of the per-version files and the `latest*.json` pointers (not `repository.json`, which carries only the slim summary above).

```jsonc
{
    "manifest_version": 2,

    "id": "ExampleDev.ExampleMod",
    "name": "Example Mod",
    "version": "1.2.0",
    "author": "ExampleDev",
    "description": "Does something useful.",

    "parity_required": true,
    "scenes": ["normal", "quest"],    // optional

    "dependencies": {
        "SomeDev.UtilKit": "1.2.0",
    },

    "artifacts": [
        {
            "targets": ["pc", "server"],
            "download_url": "https://github.com/ExampleDev/ExampleMod/releases/download/v1.2.0/ExampleMod.dll",
            "sha256": "<64-char lowercase hex>",
            "package": "dll",
            "library_dependencies": [
                {
                    "name": "ExampleLib",
                    "download_url": "https://github.com/SomeVendor/ExampleLib/releases/download/v2.0.0/ExampleLib.dll",
                    "sha256": "<64-char lowercase hex>",
                    "filename": "ExampleLib.dll",
                },
            ],
        },
        {
            "targets": ["quest"],
            "download_url": "https://github.com/ExampleDev/ExampleMod/releases/download/v1.2.0/ExampleMod-quest.zip",
            "sha256": "<64-char lowercase hex>",
            "package": "zip",
            "library_dependencies": [],
        },
    ],
}
```

Each `package` line above is shown because it appears in the compiled manifests, but **you don't write it**, the tooling determines `dll` vs `zip` from each file itself during validation/ingest and adds it. A submission omits it (see [`submissions/TEMPLATE.json`](../submissions/TEMPLATE.json)). There is no mod-level `filename`: a single-`.dll` artifact is saved under the name it was published with (falling back to `<id>.dll` only if that name isn't a usable `.dll`), and a zip names its own files. Nothing ever looks the DLL up by name, so its exact name doesn't matter.

Compiled manifests are written with their keys in the order shown: `manifest_version`, `id`, `name`, `version`, `author`, `description`, `screenshots`, `parity_required`, `scenes`, `dependencies`, `artifacts`; inside an artifact `targets`, `download_url`, `sha256`, `package`, `library_dependencies`; inside a library `name`, `download_url`, `sha256`, `filename`.

### Field rules

The launcher's parser and TavernLib's enforce all of these; this repo's validator enforces them and a few more, noted below.

| Field                         | Rule |
| ----------------------------- | ---- |
| `manifest_version`            | Integer. This build supports major `2`. A manifest with a different major is skipped, not mis-parsed, so the schema can grow later without breaking older launchers. |
| `id`                          | `"<github-user>.<github-repo>"`, the mod's GitHub `owner/repo` with the `/` turned into a `.`. GitHub usernames can't contain a dot, so the text before the first dot is always the author. No path separators, no `.` or `..` segment. |
| `name`                        | Display name. Defaults to `id`. |
| `version`                     | Exactly `MAJOR.MINOR.PATCH`: three segments of ASCII digits, each at most 2147483647. No pre-release or build suffix. |
| `author`                      | Display author. Defaults to the text before the first dot in `id`. |
| `description`                 | Free text. |
| `screenshots`                 | Optional. A list of image URLs, shown on this repo's browsable page (up to four). Display only: no installer reads it, and it never affects what lands on disk. Publishing rules below. |
| `parity_required`             | **Required.** Boolean: whether a client joining a server that runs this mod must match its exact version. `true` means the server refuses the join otherwise — for mods whose two halves are one system, like a voice codec or a network protocol, where a mismatch breaks the session outright. `false` means the server never blocks a join over it; the client is offered the mod, defaulted to installing, and can decline — for mods whose halves work independently, like a client performance tweak that happens to ship a server piece. Every manifest has to state it, so publishing is a deliberate answer rather than something inherited by omission. The launcher and TavernLib refuse a missing or non-boolean value rather than guess. It only matters for a mod with a client target (`pc` or `quest`) that a server also runs; see "Joining a server". |
| `scenes`                      | Optional. The scenes a server can run that the mod works in: a non-empty list of distinct names out of `"normal"`, `"quest"` and `"tutorial"`. Leave it out (or `null`) for a mod that works in all of them; listing all three is rejected for the same reason. A headless server skips a listed mod that doesn't work in the scene it's launched into (and disables an installed copy); the server launcher asks before installing one and warns at start; a joining client leaves out its own pinned mods that don't work in the server's scene. Mods a server runs are never second-guessed by its clients. Not part of the handshake fingerprint. The launcher and TavernLib keep names they don't know, so a scene added later doesn't break older builds; this repo's validator is what rejects a misspelt one. |
| `dependencies`                | `{}` or a map of other mod ids to a minimum version. Resolved by minimum-required-version with a major lock: candidates in the same major at or above the minimum, highest one wins. Always present, use `{}` for none. Resolved per target; see below. |
| `artifacts`                   | **Required.** A non-empty list of the files the mod ships, one entry per distinct build (below). |

Inside each artifact:

| Field                  | Rule |
| ---------------------- | ---- |
| `targets`              | A non-empty list of target names. A target may appear in only one artifact of a manifest; a manifest that lists one twice is refused. The launcher and TavernLib ignore names they don't know (so a target added later doesn't break them) and an artifact left with no known target; this repo's validator rejects unknown names. |
| `download_url`         | Direct `https://` URL to this build's single `.dll` or its `.zip`. Submissions to this repo also require it to be a github.com release asset under the id's `owner/repo` (see SUBMITTING.md). |
| `sha256`               | SHA-256 of the file at `download_url`, 64 hex characters (lowercase in compiled manifests; readers lowercase it). Anything else refuses the whole manifest. The installer downloads to a temp file, hashes it, and refuses to install on a mismatch. Files inside a zip are not hashed separately. |
| `package`              | **Tooling-set, not submitter-written.** `"dll"` (the artifact is a single `.dll`) or `"zip"` (an archive extracted into the target folder, for a build that ships several DLLs and/or Unity asset bundles / addressables). Determined by sniffing the file's own bytes (a zip starts with `PK`, anything else is a dll), so it can never disagree with the actual file. Matched exactly: an artifact whose value is anything but `"dll"` or `"zip"` (another spelling, missing, not a string) is ignored, like an unknown target. |
| `library_dependencies` | `[]` or a list of pinned third-party files this build links against (below). Always present. Per artifact because native libraries differ by platform. |

A target that no artifact lists is a target the mod doesn't support. There is no other way to say where a mod runs.

### screenshots

Optional and display-only — the launcher never fetches one, so a mod with none is in no way second-class. They exist for the browsable page this repo publishes, which bakes them in at build time rather than loading them from wherever a manifest points at render time.

A screenshot URL is only published if it is:

- **https**, and hosted on `github.com` or `raw.githubusercontent.com`;
- **under the mod's own `owner/repo`** — the same trust boundary `download_url` already has, so a manifest can't point the page at another author's repository or at an arbitrary host that would see every visitor's IP;
- **at an immutable address** — a release asset, or a raw URL pinning a 40-character commit sha. A branch-relative raw URL is repointable after review, so approving one would bind nothing;
- **a raster image**: `.png`, `.jpg`, `.jpeg`, `.webp`, `.gif`. No `.svg` — script inside an SVG doesn't execute via `<img>`, but excluding the format removes the question instead of depending on how a page embeds it.

Anything else is skipped, with the reason printed in the build log rather than silently dropped. Note what the extension rule does *not* claim: a `.png` URL can still serve something that isn't an image. That's tolerable because the page only ever puts it in an `<img>`, where a non-image simply fails to render — which is why the rules that matter are about *where it comes from*, not what it's called.

### dependencies vs library_dependencies

Two separate things:

- `dependencies` are other mods, by id. They're manifests in some repo, each installs into its own `Mods/<id>/` folder, and they get full version-range resolution and conflict detection. Use this to depend on another community mod.
- `library_dependencies` are pinned third-party files a build links against, like an audio codec or other support library that isn't itself a mod. They install FLAT into `UserLibs/` at the exact file you pin (`filename` plus `sha256`), never bundled inside a mod's folder, with no version resolution and no ownership check. Its `download_url` must still be `https://`, it just isn't restricted to your own repo. You're pinning a specific external build you tested against.

Two installed builds pinning the same `filename` with the same `sha256` install it once, even if their `download_url`s differ. The same `filename` with a different `sha256` is a conflict, and the install that would cause it is refused. That holds across everything installed in one game folder, whichever target it was installed for, since `UserLibs/` is shared.

**Dependencies are resolved per target.** An installer always installs a closure *for* one target (`pc` from the client launcher, `server` from the server launcher and TavernLib), and it leaves out any dependency with no artifact for that target, along with anything only that dependency needed, saying so in its log. So a mod with `pc` and `server` builds may depend on a `pc`-only mod: players get it, servers don't. This is the supported way to say "my client half needs this" — there is no per-target `dependencies` field, and none is needed.

What that costs you: the tooling can't tell whether your *server* build also touches that dependency, because no manifest states it and only your assembly knows. A submission in this shape gets a note asking you to confirm, not a rejection.

What **is** rejected: a dependency that shares no target with your mod at all — a `server`-only mod depending on a `pc`-only mod, or the reverse. That edge can never be satisfied in any process the mod runs in.

**What goes in the zip vs. a `library_dependency`:** put the build's own code (even several managed DLLs) plus its Unity asset bundles / addressables in the `package:"zip"` archive. Put anything third-party or shared in `library_dependencies` so it's deduped and reference-counted in `UserLibs/` instead of duplicated in every mod that uses it. A **native/unmanaged DLL must be a `library_dependency`** regardless. MelonLoader only registers `UserLibs/` (not mod folders) as a native DLL search path, so a native DLL inside a mod folder won't be found.

**A zip must have at least one `.dll` at its root.** The archive root becomes the target folder, and the mod's main assembly belongs there. (MelonLoader does load DLLs in subfolders of a loaded folder too, but a zip whose only DLLs are tucked into subfolders is almost always a packaging mistake, so the validator rejects it.)

### Install destinations

Destinations are structural, never manifest-supplied. Every mod gets its own folder `Mods/<id>/`, and each target's build installs into a dot-prefixed subfolder of it:

```
<game>/Mods/<id>/manifest.json      the install record (below)
<game>/Mods/<id>/.pc/               the pc build, if installed here
<game>/Mods/<id>/.server/           the server build, if installed here
<game>/UserLibs/<filename>          libraries, flat
```

The client launcher installs only `pc` builds and the server launcher and TavernLib only `server` builds. One game folder often serves both (a host plays from the same install their server runs from), so a mod can have both subfolders at once, each untouched by the other side's installs. Nothing is ever placed directly in `Mods/<id>/` besides the record.

For `package:"dll"` the single DLL is written into the target folder under the name it was published with (the `download_url`'s basename); for `package:"zip"` the archive is extracted into it. That name is run through the same bare-basename check as everything else (so a hostile `download_url` can't traverse out with a `..` or a separator), and it must end in `.dll`; a name failing either check falls back to `<id>.dll`. Zip extraction is hardened against zip-slip: any member with an absolute path, a colon (drive letter or NTFS stream), a `..` that escapes the folder, or a symlink is refused and nothing is installed. A member that resolves to the record's own name (`manifest.json` or `manifest.disabled.json` at the archive root, compared case-insensitively after resolving `.` and `..`) is skipped. A `library_dependencies` file is the one thing whose name genuinely *matters* and *is* manifest-supplied (a bare `filename`, checked the same way), because libraries are resolved by exact name in `UserLibs/`.

### Which build loads

MelonLoader 0.7 loads a `Mods/<name>/` folder that contains a `manifest.json` (an existence check; the content is never read) **and every subfolder beneath it**, except subfolders whose names start with `.` or `~` (and a few reserved names). So by default it loads neither `.pc/` nor `.server/`.

TavernLib, which loads as a plugin before any mod, adds the right one back for the process it's running in: `.server/` in a server process (one started with the game's server argument, `/start_server`, which is also what puts TavernLib in server mode), `.pc/` in any other. It adds that folder and its own non-excluded subfolders to MelonLoader's mod folder list and assembly search path, for every mod whose record is enabled (`manifest.json`, not `manifest.disabled.json`), is ours (see "Sidecar files"), and lists that target under `installed`. It never adds anything for a mod that fails any of those.

The consequence is deliberate: without TavernLib, or if it can't reach MelonLoader's folder list, a managed mod simply doesn't load. It can never load two builds into one process.

## Sidecar files

When an installer puts something on disk it writes a small record beside it, so anything that reads that game folder later can tell what's present without re-reading any repo. Two installers write these — the launcher (Python) and TavernLib's native one on headless servers (C#) — and they write byte-for-byte the same bytes for the same state, because either one may be reading the other's records:

```
<game>/Mods/<id>/manifest.json         the install record
<game>/UserLibs/<filename>.meta.json   { filename, sha256, download_url }   for each installed library
```

A mod's record is named `manifest.json` on purpose: it's the marker MelonLoader checks before loading the folder at all.

### The install record

```jsonc
{
  "manifest_version": 2,
  "id": "ExampleDev.ExampleMod",
  "name": "Example Mod",
  "version": "1.2.0",
  "author": "ExampleDev",
  "description": "Does something useful.",
  "parity_required": true,
  "scenes": ["normal", "quest"],        // only when the manifest sets it
  "dependencies": { "SomeDev.UtilKit": "1.2.0" },
  "artifacts": [ ... ],                 // the manifest's artifacts, verbatim, in manifest order
  "source_repo": "https://raw.githubusercontent.com/.../main",
  "installed": {
    "pc": {
      "sha256": "<the installed artifact's sha256>",
      "libraries": ["ExampleLib.dll"],
      "files": { "ExampleMod.dll": "<sha256>" }
    },
    "server": { ... }
  }
}
```

Exact rules, which both installers follow:

- **Keys in exactly this order.** `scenes` only when set. Every artifact and library keeps the compiled-manifest key order above, and `screenshots` is never copied into a record.
- **`installed`** has one entry per target installed in this folder, keys sorted. Each holds the installed artifact's `sha256`; `libraries`, the library `filename`s that build pins, in manifest order (used to reference-count `UserLibs/`); and `files`, `{path: sha256}` for every file placed in the target folder, paths relative to it with forward slashes, hashes lowercase hex, **keys sorted by ordinal (code-unit) order**.
- **JSON formatting:** two-space indentation, `": "` between key and value, non-ASCII characters escaped as lowercase `\uXXXX`, CRLF line endings, no trailing newline.
- **An install leaves the mod enabled.** Afterwards the record is `manifest.json`, and any `manifest.disabled.json` is gone. Since enabling is one switch for the whole mod, this re-enables the other targets too.
- **One version per record.** Installing target T at the record's version replaces only `.T/` and `installed.T`. Installing T at any other version (or into a folder whose record has no `installed`) replaces the whole mod: the other targets' entries are dropped and their folders removed, best effort; a folder that can't be removed because a running game has it open is left behind, and since it's no longer under `installed`, nothing loads it. The record is always written last, after the target folder is in place.
- **"Is this folder ours"** is one predicate in both installers. The first of `manifest.json` and `manifest.disabled.json` that is strict UTF-8 (no BOM) holding a JSON object with a non-empty string `id` is the record, and it's ours only if its `parity_required` is a JSON boolean. A folder that fails is untracked (below), never deleted.
- **Damage detection:** a target whose `files` don't all match on disk is **damaged**. An installer repairs a damaged target the next time it would otherwise call it current.

`files` is what turns "the version number says 1.2.0" into "these exact bytes are still here", so a build half-eaten by antivirus or left truncated by a cut-short write reads as **damaged** instead of *Up to date*.

**Disabling** a mod without uninstalling it renames its record from `manifest.json` to `manifest.disabled.json`. The folders and their files stay, but MelonLoader stops scanning the folder, so no target loads; enabling renames it back. It's one switch for the whole mod, shared by every target installed in the folder. **Uninstalling** deletes the whole `Mods/<id>/` folder; libraries are removed only when no remaining record's `installed` still lists them.

You never write these, they're an install artifact, documented here so the on-disk footprint is covered.

## Joining a server

### What a server advertises

A server's auth port answers `ping` with `mods_hash` and `mods_count`, and a `mods_list` request with the mods it is running and its untracked mods. A mod counts as running on a server only if its record is enabled, ours, and lists `server` under `installed`; a mod installed only for `pc` in the same folder is invisible to the server. Each `mods` entry is:

```jsonc
{
  "id": "ExampleDev.ExampleMod",
  "version": "1.2.0",
  "parity_required": true,
  "source_repo": "https://raw.githubusercontent.com/.../main",   // a hint only, never a pull source
  "targets": ["pc", "server"],                                    // known targets of the record's usable artifacts, sorted
  "target_sha256": { "pc": "<sha>", "server": "<sha>" },          // per target, from the record's artifacts
  "scenes": ["normal"]                                            // only when set
}
```

`mods_hash` is unchanged: sha256 of the newline-joined lines `id@version@req|opt` for those mods, sorted by id, then `untracked:<name>@<mtime>` lines, sorted by lowercased name. Neither `targets` nor any hash is part of it.

### What a joining PC client gets

A client always installs the artifact for **its own** target, `pc`, whatever the server runs or which scene it's in. Of the server's entries, it considers only those whose `targets` include `pc`:

- `parity_required: true` → required: installed at that exact version, or the launch is refused.
- `parity_required: false` → offered, ticked by default; the player can decline.

Entries without `pc` (a server-only mod, or one built only for `quest`) are left alone. The launcher compares its installed `pc` build's sha256 with the server's `target_sha256.pc` and notes a mismatch (same version, different build) without blocking.

The game is launched with a `TavernMods` claim listing every enabled mod installed for `pc`, as `{id: version}`.

### What the server enforces

At join, TavernLib checks the claim against the mods it runs:

- **A join carrying a claim** must hold the exact version of every mod with `parity_required: true` whose `targets` include `pc`.
- **A join with no claim, or an unreadable one** (the standalone Quest app, TavernEssentials' in-game board, a direct connect) is refused if the server runs any mod with `parity_required: true` whose `targets` include `pc` or `quest`, and admitted otherwise.

Version parity guards against honest mistakes, not cheating: the claim is self-reported.

## Untracked mods

A record is what makes a mod *managed*. Anything else in `Mods/` that MelonLoader will still load is **untracked**, and both the launcher and a headless server report it as such rather than pretending it isn't there. Two shapes qualify:

- a loose `Mods/<name>.dll` — the classic drag-a-dll-in manual install. Nothing that scans for mods looks at root files, only subfolders, so this is invisible to the installed list.
- a `Mods/<name>/` folder carrying a `manifest.json` (or `manifest.disabled.json`) that this tooling didn't write.

A folder counts on the record file **existing**, not on it parsing. MelonLoader's folder marker is an existence check with the content unread, so a folder whose `manifest.json` is unparseable junk loads exactly like one with a valid manifest, and hiding it would hide a mod that is genuinely running. A folder with neither file isn't reported, because MelonLoader wouldn't load it either. Folders prefixed `.` or `~` are skipped throughout — those are install staging directories.

Untracked mods are **surfaced and toggled, never managed**. There's no manifest anyone trusts to resolve a version against, so they never appear anywhere near install, update, or uninstall:

|                       | Managed                        | Untracked                                    |
| --------------------- | ------------------------------ | -------------------------------------------- |
| Install / update      | yes                            | no — nothing to resolve a version against     |
| Enable / disable      | yes                            | yes                                           |
| Uninstall             | yes                            | no — we didn't place the files, so we can't promise a clean removal |
| In the join handshake | yes, enforced                  | yes, advisory only — never blocks a join      |
| In an exported modlist | yes, in `mods`                | yes, in `untracked` — listed, never installed |

**Enabling and disabling** works differently for the two shapes, because only one of them has a marker to hide:

- a **folder** uses the same rename as a managed mod — `manifest.json` ⇄ `manifest.disabled.json`. The foreign manifest is never read or rewritten, only renamed.
- a **loose dll** has no marker to rename, and MelonLoader loads any `Mods/*.dll` it finds, so the file itself moves: `Manual.dll` ⇄ `Manual.dll.disabled`. It's always referred to by its enabled name, so the same string toggles it either way. If both names are occupied at once the toggle is refused rather than resolved, since the rename would destroy one of the two files irrecoverably.

Either way the files stay on disk, and the change takes effect the next time the game or server starts — MelonLoader has already scanned `Mods/` by the time anything can toggle one.

The invariant that makes this safe: **nothing automatic ever enables or disables an untracked mod.** Not a headless server's reconcile pass, not a modlist import, not the per-server render a client does when joining. Only a person does: from the launcher's Mod Manager, or on a headless server by renaming the file or record by hand (`modmanager list` shows them, tagged). The one exception is a loose `Mods/<name>.dll` with the same filename as a DLL an install just placed: it's disabled by the same rename, because MelonLoader would otherwise load the mod twice.

### In the join handshake

A server reports its enabled untracked mods to a connecting client, in the `mods_list` reply's `untracked` field — a separate list from `mods`, each entry just `{name, kind}`.

It is **advisory and cannot block a join**. All a server can honestly say about one is its name and shape; there's no id, version, or source, so a client can display it but never resolve, install, or verify it. The server's parity check ignores untracked mods entirely, in both directions: it never requires one of a client, and it never inspects the client's own. What this replaces is the worse outcome — a mod affecting the session with no sign that it exists.

Untracked mods are part of the ping/pong `mods_hash` even though they aren't part of `mods_count`. A client caches the whole reply against that hash, so leaving them out would let an operator drop a new DLL into `Mods/` and have every client keep reporting the old set. The per-mod stamp folded into the hash is the file or folder's mtime, not a content hash — this runs on every ping, and an in-place edit preserving mtime costing one stale advisory line is a fair trade for not hashing every DLL in `Mods/` on each one.

`mods_count` stays managed-only: it's there so a client can sanity-check its cached `mods` list without decoding it, and that list is the managed one.

### In an exported modlist

An exported modlist records enabled untracked mods under a top-level `untracked` key, as `{name, kind}` entries — never as entries in `mods`. That separation is load-bearing rather than tidy: a launcher's exported modlist **is** a headless server's `/modlist` config, and everything in `mods` is parsed as `id` or `id@version`. A `"Manual.dll"` in there would be resolved against the trusted repos on every boot, failing each time, or worse matching an unrelated mod that happened to share the name. Kept in its own key, it's ignored by anything that didn't ask for it — including the headless config reader, which simply has no field for it.

An importer **shows the block and installs nothing from it**, so a shared pack is honest about the part you'll have to reproduce by hand instead of silently under-describing the machine it came from. An `untracked` block never makes an import fail, and importing a modlist never disables the importer's own untracked mods — that's the same never-automatic invariant as everywhere else.

### When an install needs an untracked folder's path

Installing `SomeDev.SomeMod` when `Mods/SomeDev.SomeMod/` already exists and isn't ours does **not** delete it. The existing folder is moved to `Mods/.displaced/<name>/` (suffixed `.2`, `.3`, … if that path is taken too) and the install proceeds. Dot-prefixed, so nothing loads from there and neither lister reports it.

Moving rather than deleting is what makes the collision safe to resolve automatically: a headless reconcile runs before the server is up, with no operator to prompt, and refusing the install instead would silently boot without a mod the server was configured to run. The launcher does the same on install and on restoring a cached version — the latter being the likelier of the two to meet one, since it runs on every join that switches a mod's version. Both log what moved and where. Cleaning out `.displaced/` is left to you.
