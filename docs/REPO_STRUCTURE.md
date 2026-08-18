# Mod repository structure

This is the file layout and JSON schema a repository has to serve to work as a TavernLauncher mod source. It applies to this repo and to any third-party source a user adds under **Manage Sources**.

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

The launcher browses from `repository.json`, then fetches a mod's per-version manifest only when it actually installs, deriving the URL from the id and version (`manifests/<author>/<repo>/<version>.json`). So `download_url`, `sha256`, and the dependency lists live in exactly one place, the manifest, and can never drift from a copy in the index.

Folder names come from the id, so they aren't free-form. See the id rule below.

## repository.json

```jsonc
{
  "index_version": 1,             // schema version of this index file
  "mods": {
    "<id>": {                     // e.g. "ExampleDev.ExampleMod"
      "<major>": {                // key is the major version as a string: "1", "2"
        "name": "Example Mod",
        "author": "ExampleDev",
        "description": "Does something useful.",
        "client_side": true,
        "server_side": true,
        "parity_required": true,      // see the manifest table
        "versions": ["1.0.0", "1.2.0"]   // every release in this major
      },
      ...
    },
    ...
  }
}
```

`index_version` is the schema version of this index file. It's separate from the `manifest_version` on the per-version manifests below: the index and the manifest are different schemas and version independently, so a launcher that doesn't recognise the index version skips the whole file rather than mis-reading it.

Under `mods`, keyed by id, then by major version as a string. One entry per major. A mod with 1.4.0 and 2.1.0 out has both a `"1"` and a `"2"` entry, because a dependency locked to major 1 still has to resolve after major 2 ships. Each entry carries the discovery fields (`name`, `author`, `description`, `client_side`, `server_side`, `parity_required`, taken from the highest release in that major) and `versions`, the list of every release in the major. The highest is `max(versions)`; that's the default install, and resolution picks the highest version at or above a dependency's minimum. The full manifest for any of those versions is one fetch away at `manifests/<author>/<repo>/<version>.json`.

## Manifest

This is the shape of the per-version files and the `latest*.json` pointers (not `repository.json`, which carries only the slim summary above).

```jsonc
{
    "manifest_version": 1,

    "id": "ExampleDev.ExampleMod",
    "name": "Example Mod",
    "version": "1.2.0",
    "author": "ExampleDev",
    "description": "Does something useful.",

    "client_side": true,
    "server_side": true,
    "parity_required": true,

    "dependencies": {
        "SomeDev.UtilKit": "1.2.0",
    },

    "library_dependencies": [
        {
            "name": "ExampleLib",
            "download_url": "https://github.com/SomeVendor/ExampleLib/releases/download/v2.0.0/ExampleLib.dll",
            "sha256": "<64-char lowercase hex>",
            "filename": "ExampleLib.dll",
        },
    ],

    "download_url": "https://github.com/ExampleDev/ExampleMod/releases/download/v1.2.0/ExampleMod.dll",
    "sha256": "<64-char lowercase hex>",

    "package": "dll",
}
```

The `package` line above is shown because it appears in the compiled manifests, but **you don't write it**, the tooling determines `dll` vs `zip` from the file itself during validation/ingest and adds it. A submission omits it (see [`submissions/TEMPLATE.json`](../submissions/TEMPLATE.json)). There is no mod-level `filename`: a single-`.dll` mod is saved into `Mods/<id>/` under the name it was published with (falling back to `<id>.dll` only if that name isn't a usable `.dll`), and a zip bundle names its own files. Nothing ever looks the DLL up by name, so its exact name doesn't matter.

### Field rules

The launcher's parser enforces all of these.

| Field                         | Rule                                                                                                                                                                                                                                                                                                        |
| ----------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `manifest_version`            | Integer. This build supports major `1`. A manifest with a different major is skipped, not mis-parsed, so the schema can grow later without breaking older launchers.                                                                                                                                        |
| `id`                          | `"<github-user>.<github-repo>"`, the mod's GitHub `owner/repo` with the `/` turned into a `.`. GitHub usernames can't contain a dot, so the text before the first dot is always the author. No path separators, no `.` or `..` segment.                                                                     |
| `name`                        | Display name. Defaults to `id`.                                                                                                                                                                                                                                                                             |
| `version`                     | Exactly `MAJOR.MINOR.PATCH`, three plain integers. No pre-release or build suffix.                                                                                                                                                                                                                          |
| `author`                      | Display author. Defaults to the text before the first dot in `id`.                                                                                                                                                                                                                                          |
| `description`                 | Free text.                                                                                                                                                                                                                                                                                                  |
| `screenshots`                 | Optional. A list of image URLs, shown on this repo's browsable page (up to four). Display only: no installer reads it, and it never affects what lands on disk. Publishing rules below.                                                                                                                     |
| `client_side` / `server_side` | Booleans, at least one `true`. Controls which browse list the mod shows up in. The client launcher lists client-side mods, the server launcher lists server-side ones.                                                                                                                                      |
| `parity_required`             | **Required.** Boolean: whether a client joining a server that runs this mod must match its exact version. `true` means the server refuses the join otherwise — for mods whose two halves are one system, like a voice codec or a network protocol, where a mismatch breaks the session outright. `false` means the server never blocks a join over it; the client is offered the mod, defaulted to installing, and can decline — for mods whose halves work independently, like a client performance tweak that happens to ship a server piece. Every manifest has to state it, so publishing is a deliberate answer rather than something inherited by omission. Only a real `false` relaxes it, so a malformed value (a string, a `null`, a `0`) reads as required rather than being trusted. There is no default anywhere and nothing infers one: a manifest, index entry, install record, or handshake entry without the field is malformed, and readers reject it rather than guessing — guessing is the one mistake that matters here, because it decides whether a joining client is obliged to match. Only meaningful on a mod that is both `client_side` and `server_side`: a client-only mod is never in a server's list, and a server-only one is never asked of a client. |
| `dependencies`                | `{}` or a map of other mod ids to a minimum version. Resolved by minimum-required-version with a major lock: candidates in the same major at or above the minimum, highest one wins. Always present, use `{}` for none.                                                                                     |
| `library_dependencies`        | `[]` or a list of pinned third-party files (below). Always present, use `[]` for none.                                                                                                                                                                                                                      |
| `package`                     | **Tooling-set, not submitter-written.** `"dll"` (`download_url` is the mod's single `.dll`) or `"zip"` (an archive extracted into the mod's folder, for a mod that ships several DLLs and/or Unity asset bundles / addressables). Determined by sniffing the file's own bytes during validation/ingest and baked into the compiled manifest (a zip starts with `PK`, anything else is a dll), so it can never disagree with the actual file. An unrecognised value in a compiled manifest skips it, same as an unknown `manifest_version`. |
| `download_url`                | Direct `https://` URL to the mod's single `.dll` or its `.zip`. Submissions to this repo also require it to be a github.com release asset under the id's `owner/repo` (see SUBMITTING.md).                                                                              |
| `sha256`                      | Lowercase hex SHA-256 of the file at `download_url`; the `.dll` or the `.zip` itself. The launcher downloads to a temp file, hashes it, and refuses to install on a mismatch. One hash per artifact; individual files inside a zip are not hashed separately.                                               |

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
- `library_dependencies` are pinned third-party files a mod links against, like an audio codec or other support library that isn't itself a mod. They install FLAT into `UserLibs/` at the exact file you pin (`filename` plus `sha256`), never bundled inside a mod's folder, with no version resolution and no ownership check. Its `download_url` must still be `https://`, it just isn't restricted to your own repo. You're pinning a specific external build you tested against.

Two mods pinning the same `filename` with the same `sha256` install it once. The same `filename` with a different `sha256` is a conflict, and neither installs.

**Dependencies are resolved per side.** When the launcher installs a mod's dependency closure it knows whether it's installing into a client or a server, and it leaves out any dependency that can't run there, along with anything only that dependency needed. So a mod that runs on **both** sides may depend on a **client-only** mod: the client gets it, the server doesn't, and neither has to be told about the other. This is the supported way to say "my client half needs this" — there is no per-side `dependencies` field, and none is needed.

What that costs you: the tooling can't tell whether your *server* half also touches that dependency, because no manifest states it and only your assembly knows. If it does, your server half won't load, and the dependency wouldn't have saved it (it can't run on a server either) — that's a mod bug, not a resolution one. A submission in this shape gets a note asking you to confirm, not a rejection.

What **is** rejected: a dependency whose sides don't overlap with yours at all — a server-only mod depending on a client-only mod, or the reverse. That edge can never be satisfied in any process the mod runs in, so it's a dead relationship rather than a per-side one.

**What goes in the zip vs. a `library_dependency`:** put the mod's own code (even several managed DLLs) plus its Unity asset bundles / addressables in the `package:"zip"` archive. Put anything third-party or shared in `library_dependencies` so it's deduped and reference-counted in `UserLibs/` instead of duplicated in every mod that uses it. A **native/unmanaged DLL must be a `library_dependency`** regardless. MelonLoader only registers `UserLibs/` (not mod folders) as a native DLL search path, so a native DLL inside a mod folder won't be found.

**The zip must have at least one `.dll` at its root.** The archive root becomes `Mods/<id>/`, and MelonLoader loads the assemblies sitting directly in that folder (`Mods/<id>/*.dll`), it does **not** recurse into subfolders looking for a mod to load. So your mod's main assembly (and any managed DLL you want MelonLoader to load) must be at the top level of the zip, not tucked inside a subfolder; asset-bundle subfolders alongside them are fine. The submission validator rejects a bundle whose only `.dll`s are in subfolders, because it would install cleanly and then silently never load.

### Install destinations

Destinations are structural, never manifest-supplied. Every mod installs into its own folder `Mods/<id>/`; libraries install flat into `UserLibs/`. For `package:"dll"` the launcher writes the single DLL into `Mods/<id>/` under the name it was published with (the `download_url`'s basename); for `package:"zip"` it extracts the archive into `Mods/<id>/`. Nothing ever looks the DLL up by name (every operation works on the folder), so its exact name doesn't matter, but two guards still apply: the name is run through the same bare-basename check as everything else (so a hostile `download_url` can't traverse out with a `..` or a separator), and it must end in `.dll` or MelonLoader won't load it, a name failing either check falls back to `<id>.dll`. Zip extraction is hardened against zip-slip the same way: any archive member with an absolute path, a drive letter, a `..` that escapes the folder, or a symlink is refused and nothing is installed. A `library_dependencies` file is the one thing whose name genuinely *matters* and *is* manifest-supplied (a bare `filename`, checked the same way), because libraries are resolved by exact name in `UserLibs/`.

A mod folder that grows beyond one file needs no schema change: the same `Mods/<id>/` holds the DLL(s) and any bundled assets, and the mod loads its own asset bundles / addressables relative to its DLL's own folder.

## Sidecar files

When an installer puts something on disk it writes a small record beside it, so anything that reads that game folder later can tell what's present without re-reading any repo. Two installers write these — the launcher (Python) and TavernLib's native one on headless servers (C#) — and they write byte-for-byte the same shape, because either one may be reading the other's records:

```
<game>/Mods/<id>/manifest.json         the fetched manifest verbatim + { source_repo, package, libraries, files }
<game>/UserLibs/<filename>.meta.json   { filename, sha256, download_url }   for each installed library
```

A mod's record is named `manifest.json` on purpose: recent MelonLoader only scans a `Mods/` subfolder that contains a file by that name (it checks existence only, never the content), so the record doubles as the marker that makes the folder load. The record does **not** store the placed DLL's filename, every operation works on the `Mods/<id>/` folder, so nothing needs to find the file by name. `libraries` is the list of library `filename`s that mod pins; the launcher uses it to reference-count `UserLibs/` on uninstall. A library is removed only when no remaining installed mod still lists it, so a shared library stays until its last user is gone. Uninstalling a mod deletes its whole `Mods/<id>/` folder.

`files` is `{relative path: sha256}` for everything the install placed in that folder — hashed out of the staging directory just before the record is written, so the record is never a member of its own map. Keys always use forward slashes and hashes are lowercase hex, on every platform and in both implementations; that's what lets one installer's record be verified by the other. It's what turns "the version number says 1.2.0" into "these exact bytes are still here", so a mod half-eaten by antivirus or left truncated by a cut-short write reads as **damaged** instead of *Up to date*. A record without the key (written before it existed) simply has no damage detection — never "damaged", since there'd be no evidence either way. If you write your own installer, writing this map is optional; matching its shape if you do write it is not.

**Disabling** a mod without uninstalling it renames its record from `manifest.json` to `manifest.disabled.json`. The folder and all its files stay, but with no `manifest.json` present MelonLoader stops scanning the folder, so the mod no longer loads; enabling renames it back. The folder itself is never renamed, so the mod stays addressable by `<id>` and keeps its place in the installed list.

You never write these, they're an install artifact, documented here so the on-disk footprint is covered.

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

The invariant that makes this safe: **nothing automatic ever enables or disables an untracked mod.** Not a headless server's reconcile pass, not a modlist import, not the per-server render a client does when joining. Only a person does, from the launcher's Mod Manager or the server console. A mod you hand-installed stays exactly as you left it.

On a headless server, `modmanager list` shows them tagged alongside managed mods, and `modmanager enable` / `modmanager disable` act on them by name. Those two commands are deliberately restricted to untracked mods: a managed mod's state comes from the configured mods list, so disabling one by hand would be undone by the next reconcile — untracked mods are the ones reconcile never touches, which is exactly why toggling one sticks.

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
