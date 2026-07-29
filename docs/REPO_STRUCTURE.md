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
        "versions": ["1.0.0", "1.2.0"]   // every release in this major
      },
      ...
    },
    ...
  }
}
```

`index_version` is the schema version of this index file. It's separate from the `manifest_version` on the per-version manifests below: the index and the manifest are different schemas and version independently, so a launcher that doesn't recognise the index version skips the whole file rather than mis-reading it.

Under `mods`, keyed by id, then by major version as a string. One entry per major. A mod with 1.4.0 and 2.1.0 out has both a `"1"` and a `"2"` entry, because a dependency locked to major 1 still has to resolve after major 2 ships. Each entry carries the discovery fields (`name`, `author`, `description`, `client_side`, `server_side`, taken from the highest release in that major) and `versions`, the list of every release in the major. The highest is `max(versions)`; that's the default install, and resolution picks the highest version at or above a dependency's minimum. The full manifest for any of those versions is one fetch away at `manifests/<author>/<repo>/<version>.json`.

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
| `client_side` / `server_side` | Booleans, at least one `true`. Controls which browse list the mod shows up in. The client launcher lists client-side mods, the server launcher lists server-side ones.                                                                                                                                      |
| `dependencies`                | `{}` or a map of other mod ids to a minimum version. Resolved by minimum-required-version with a major lock: candidates in the same major at or above the minimum, highest one wins. Always present, use `{}` for none.                                                                                     |
| `library_dependencies`        | `[]` or a list of pinned third-party files (below). Always present, use `[]` for none.                                                                                                                                                                                                                      |
| `package`                     | **Tooling-set, not submitter-written.** `"dll"` (`download_url` is the mod's single `.dll`) or `"zip"` (an archive extracted into the mod's folder, for a mod that ships several DLLs and/or Unity asset bundles / addressables). Determined by sniffing the file's own bytes during validation/ingest and baked into the compiled manifest (a zip starts with `PK`, anything else is a dll), so it can never disagree with the actual file. An unrecognised value in a compiled manifest skips it, same as an unknown `manifest_version`. |
| `download_url`                | Direct `https://` URL to the mod's single `.dll` or its `.zip`. Submissions to this repo also require it to be a github.com release asset under the id's `owner/repo` (see SUBMITTING.md).                                                                              |
| `sha256`                      | Lowercase hex SHA-256 of the file at `download_url`; the `.dll` or the `.zip` itself. The launcher downloads to a temp file, hashes it, and refuses to install on a mismatch. One hash per artifact; individual files inside a zip are not hashed separately.                                               |

### dependencies vs library_dependencies

Two separate things:

- `dependencies` are other mods, by id. They're manifests in some repo, each installs into its own `Mods/<id>/` folder, and they get full version-range resolution and conflict detection. Use this to depend on another community mod.
- `library_dependencies` are pinned third-party files a mod links against, like an audio codec or other support library that isn't itself a mod. They install FLAT into `UserLibs/` at the exact file you pin (`filename` plus `sha256`), never bundled inside a mod's folder, with no version resolution and no ownership check. Its `download_url` must still be `https://`, it just isn't restricted to your own repo. You're pinning a specific external build you tested against.

Two mods pinning the same `filename` with the same `sha256` install it once. The same `filename` with a different `sha256` is a conflict, and neither installs.

**What goes in the zip vs. a `library_dependency`:** put the mod's own code (even several managed DLLs) plus its Unity asset bundles / addressables in the `package:"zip"` archive. Put anything third-party or shared in `library_dependencies` so it's deduped and reference-counted in `UserLibs/` instead of duplicated in every mod that uses it. A **native/unmanaged DLL must be a `library_dependency`** regardless. MelonLoader only registers `UserLibs/` (not mod folders) as a native DLL search path, so a native DLL inside a mod folder won't be found.

**The zip must have at least one `.dll` at its root.** The archive root becomes `Mods/<id>/`, and MelonLoader loads the assemblies sitting directly in that folder (`Mods/<id>/*.dll`), it does **not** recurse into subfolders looking for a mod to load. So your mod's main assembly (and any managed DLL you want MelonLoader to load) must be at the top level of the zip, not tucked inside a subfolder; asset-bundle subfolders alongside them are fine. The submission validator rejects a bundle whose only `.dll`s are in subfolders, because it would install cleanly and then silently never load.

### Install destinations

Destinations are structural, never manifest-supplied. Every mod installs into its own folder `Mods/<id>/`; libraries install flat into `UserLibs/`. For `package:"dll"` the launcher writes the single DLL into `Mods/<id>/` under the name it was published with (the `download_url`'s basename); for `package:"zip"` it extracts the archive into `Mods/<id>/`. Nothing ever looks the DLL up by name (every operation works on the folder), so its exact name doesn't matter, but two guards still apply: the name is run through the same bare-basename check as everything else (so a hostile `download_url` can't traverse out with a `..` or a separator), and it must end in `.dll` or MelonLoader won't load it, a name failing either check falls back to `<id>.dll`. Zip extraction is hardened against zip-slip the same way: any archive member with an absolute path, a drive letter, a `..` that escapes the folder, or a symlink is refused and nothing is installed. A `library_dependencies` file is the one thing whose name genuinely *matters* and *is* manifest-supplied (a bare `filename`, checked the same way), because libraries are resolved by exact name in `UserLibs/`.

A mod folder that grows beyond one file needs no schema change: the same `Mods/<id>/` holds the DLL(s) and any bundled assets, and the mod loads its own asset bundles / addressables relative to its DLL's own folder.

## Sidecar files

When the launcher installs something it writes a small record beside it, so it (and a future native installer) can tell what's present without re-reading any repo:

```
<game>/Mods/<id>/manifest.json         the fetched manifest verbatim + { source_repo, package, libraries }
<game>/UserLibs/<filename>.meta.json   { filename, sha256, download_url }   for each installed library
```

A mod's record is named `manifest.json` on purpose: recent MelonLoader only scans a `Mods/` subfolder that contains a file by that name (it checks existence only, never the content), so the record doubles as the marker that makes the folder load. The record does **not** store the placed DLL's filename, every operation works on the `Mods/<id>/` folder, so nothing needs to find the file by name. `libraries` is the list of library `filename`s that mod pins; the launcher uses it to reference-count `UserLibs/` on uninstall. A library is removed only when no remaining installed mod still lists it, so a shared library stays until its last user is gone. Uninstalling a mod deletes its whole `Mods/<id>/` folder.

**Disabling** a mod without uninstalling it renames its record from `manifest.json` to `manifest.disabled.json`. The folder and all its files stay, but with no `manifest.json` present MelonLoader stops scanning the folder, so the mod no longer loads; enabling renames it back. The folder itself is never renamed, so the mod stays addressable by `<id>` and keeps its place in the installed list.

You never write these, they're an install artifact, documented here so the on-disk footprint is covered.
