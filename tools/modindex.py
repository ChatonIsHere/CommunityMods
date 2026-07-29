#!/usr/bin/env python3
"""
modindex, the CommunityMods repository's own tooling.

Validates submitted manifests (Layer 1) and compiles the static index the
launcher reads. A third-party source doesn't need this tool, only the file
layout in docs/REPO_STRUCTURE.md.

Stdlib only, no pip install, so it runs in CI and in any fork with bare Python.

Subcommands
-----------
  fill-hashes [paths]   Download the files a manifest points at and write their
                        real sha256 in (mod + every library). Defaults to every
                        submissions/**/*.json.

  validate [paths...]   Layer-1 validation of submission manifest(s). Checks
                        schema, id/ownership, filename safety, version format,
                        sides, and downloads + hashes every file. Runs a
                        VirusTotal scan when VIRUSTOTAL_API_KEY is set (gate
                        with --vt-max-malicious). Defaults to every
                        submissions/**/*.json. Exits non-zero on failure.

  build [--ingest]      Compile the index. Reads every manifests/**/<version>.json,
        [--prune]       regenerates each mod's latest.json / latest.<major>.json
                        pointers, and rebuilds repository.json. With --ingest,
                        first validates and moves submissions/**/*.json into
                        manifests/<author>/<repo>/<version>.json; --prune then
                        deletes the moved submission files.

Keep these rules in sync with the launcher's parser (TavernLauncher/modmanager.py)
and docs/REPO_STRUCTURE.md.
"""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from urllib.parse import urlparse

# Schema majors this tool speaks, kept in sync with the launcher's parser.
# The manifest and index schemas version independently.
SUPPORTED_MANIFEST_MAJOR = 1     # modmanager.SUPPORTED_MANIFEST_MAJOR
SUPPORTED_INDEX_MAJOR = 1        # modmanager.SUPPORTED_INDEX_MAJOR

# GitHub's own identifier rules, so an id can never name a repo/user that can't
# exist (and the derived on-disk path is therefore always safe).
GITHUB_USER_RE = re.compile(r"^[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}$")
GITHUB_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Canonical field order for anything we (re)write, so generated files diff
# cleanly against hand-written ones and against each other.
_MANIFEST_ORDER = [
    "manifest_version", "id", "name", "version", "author", "description",
    "client_side", "server_side", "dependencies", "library_dependencies",
    "download_url", "sha256", "package",
]
_LIB_ORDER = ["name", "download_url", "sha256", "filename"]

# A mod's download_url is a single assembly ("dll") or an archive extracted into
# Mods/<id>/ ("zip"). Must match modmanager.ModManifest.package. Set by the
# tooling from the file's own bytes during validation/ingest (see
# _sniff_package), never written by submitters.
_PACKAGE_TYPES = ("dll", "zip")


def _sniff_package(head):
    """dll vs zip from a file's leading bytes: a zip starts with 'PK', a .NET
    assembly (or anything else) does not. Unambiguous for the two supported types."""
    return "zip" if head[:2] == b"PK" else "dll"

# Zip-bundle safety limits, enforced when the archive is downloaded during
# validation. Sized to allow a real mod's Unity asset bundles while still
# rejecting a decompression bomb. The launcher's extractor
# (modmanager._safe_extract_zip) applies the same limits at install time, for
# third-party sources that skip this validator.
ZIP_MAX_ENTRIES = 2000
ZIP_MAX_TOTAL_UNCOMPRESSED = 500 * 1024 * 1024   # 500 MB extracted, summed
ZIP_MAX_RATIO = 200                              # per-entry uncompressed/compressed
ZIP_RATIO_MIN_SIZE = 1 * 1024 * 1024             # ignore the ratio below this size

_VT_BASE = "https://www.virustotal.com/api/v3"
_VT_SIMPLE_UPLOAD_MAX = 32 * 1024 * 1024   # files larger than this need an upload URL

# Which members of a zip bundle get VirusTotal-scanned. Only code-bearing files
# are scanned individually rather than the whole archive, since a bulky mod is
# mostly inert assets (3D models, textures, audio, Unity asset bundles) and a
# whole-archive upload could exceed VT's size limit. A member counts as
# code-bearing by extension or by leading magic bytes, so a renamed executable
# is still caught.
_CODE_EXTENSIONS = {
    ".dll", ".exe", ".so", ".dylib", ".netmodule",           # managed / native
    ".bat", ".cmd", ".com", ".scr", ".msi", ".jar",          # binaries / installers
    ".ps1", ".psm1", ".vbs", ".vbe", ".js", ".jse",          # scripts
    ".wsf", ".wsh", ".hta", ".py", ".sh",
}
_CODE_MAGICS = (
    b"MZ",                 # PE (Windows .dll/.exe)
    b"\x7fELF",            # ELF (Linux)
    b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",   # Mach-O 32/64
    b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",   # Mach-O byte-swapped
    b"\xca\xfe\xba\xbe",   # Mach-O fat / Java .class
    b"#!",                 # shebang script
)


class ValidationError(Exception):
    """A hard failure that must block a submission or a build."""


# -- small helpers -----------------------------------------------------------

def repo_root(explicit=None):
    if explicit:
        return os.path.abspath(explicit)
    # tools/modindex.py -> repo root is the parent of tools/
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _is_safe_basename(name):
    """A bare filename: no directory part, not '.'/'..'/empty, no separators."""
    return (
        isinstance(name, str)
        and name not in ("", ".", "..")
        and "/" not in name
        and "\\" not in name
        and os.path.basename(name) == name
    )


def parse_id(mod_id):
    """id is '<github-user>.<github-repo>'. The author is everything before the
    first dot (GitHub usernames can't contain one); the repo is the rest.
    Returns (author, repo) or raises ValidationError."""
    if not isinstance(mod_id, str) or "." not in mod_id:
        raise ValidationError(f"id {mod_id!r} must be '<user>.<repo>'")
    author, repo = mod_id.split(".", 1)
    if not GITHUB_USER_RE.match(author):
        raise ValidationError(f"id author segment {author!r} is not a valid GitHub username")
    if not GITHUB_REPO_RE.match(repo):
        raise ValidationError(f"id repo segment {repo!r} is not a valid GitHub repo name")
    for seg in (author, repo):
        if seg in (".", "..") or "/" in seg or "\\" in seg:
            raise ValidationError(f"id segment {seg!r} is path-unsafe")
    return author, repo


def _ordered(d, order):
    out = {k: d[k] for k in order if k in d}
    # keep any unknown keys rather than silently dropping them
    for k, v in d.items():
        if k not in out:
            out[k] = v
    return out


def ordered_manifest(m):
    out = _ordered(m, _MANIFEST_ORDER)
    if isinstance(out.get("library_dependencies"), list):
        out["library_dependencies"] = [_ordered(lib, _LIB_ORDER) for lib in out["library_dependencies"]]
    return out


def summary_entry(highest_manifest, versions_in_major):
    """The slim repository.json entry for one (id, major): discovery fields taken
    from the highest release in that major, plus the list of every version in it.
    No download_url/sha256/dependencies here; those live only in the per-version
    manifest, so the install-critical data has one home."""
    m = highest_manifest
    return {
        "name": m.get("name", m["id"]),
        "author": m.get("author", m["id"].split(".", 1)[0]),
        "description": m.get("description", ""),
        "client_side": bool(m.get("client_side", False)),
        "server_side": bool(m.get("server_side", False)),
        "versions": versions_in_major,
    }


def _write_json(path, obj):
    # newline="\n" forces LF regardless of OS, so these generated files don't get
    # rewritten with CRLF on Windows and dirty the diff against the repo's LF
    # convention (.gitattributes eol=lf).
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


# -- structural validation (no network) --------------------------------------

def validate_structure(m):
    """Every mechanical rule from docs/REPO_STRUCTURE.md section 4. Returns a list of
    error strings (empty == structurally valid). Does not touch the network."""
    errs = []

    if m.get("manifest_version") != SUPPORTED_MANIFEST_MAJOR:
        errs.append(
            f"manifest_version must be {SUPPORTED_MANIFEST_MAJOR}, got "
            f"{m.get('manifest_version')!r}")

    mod_id = m.get("id")
    author = repo = None
    try:
        author, repo = parse_id(mod_id)
    except ValidationError as e:
        errs.append(str(e))

    ver = m.get("version")
    if not isinstance(ver, str) or not VERSION_RE.match(ver):
        errs.append(f"version {ver!r} must be exactly MAJOR.MINOR.PATCH (three integers)")

    if not (m.get("client_side") is True or m.get("server_side") is True):
        errs.append("at least one of client_side / server_side must be true")

    for key in ("client_side", "server_side"):
        if key in m and not isinstance(m[key], bool):
            errs.append(f"{key} must be a boolean")

    if not isinstance(m.get("dependencies", {}), dict):
        errs.append("dependencies must be an object (use {} when none)")

    # A mod has no `filename`, it installs as Mods/<id>/<id>.dll (single dll) or
    # the zip names its own files. Only `library_dependencies[].filename` matters
    # (checked below); it's resolved by exact name in UserLibs/.

    # `package` is tooling-set (ingest sniffs it), so a submission normally omits it.
    # If a value is present it must still be valid; ingest overwrites it from the
    # file's bytes regardless.
    pkg = m.get("package")
    if pkg is not None and pkg not in _PACKAGE_TYPES:
        errs.append(f'package {pkg!r} must be one of {_PACKAGE_TYPES} '
                    "(normally omitted, the tooling sets it)")

    if not isinstance(m.get("download_url"), str) or not m["download_url"]:
        errs.append("download_url is required")
    elif not _is_https_url(m["download_url"]):
        errs.append("download_url must be an https:// URL")

    if not isinstance(m.get("sha256"), str) or not SHA256_RE.match(m.get("sha256", "")):
        errs.append("sha256 must be 64 lowercase hex characters")

    libs = m.get("library_dependencies", [])
    if not isinstance(libs, list):
        errs.append("library_dependencies must be a list (use [] when none)")
    else:
        for i, lib in enumerate(libs):
            if not isinstance(lib, dict):
                errs.append(f"library_dependencies[{i}] must be an object")
                continue
            if not _is_safe_basename(lib.get("filename")):
                errs.append(f"library_dependencies[{i}].filename must be a bare filename")
            if not isinstance(lib.get("download_url"), str) or not lib.get("download_url"):
                errs.append(f"library_dependencies[{i}].download_url is required")
            elif not _is_https_url(lib["download_url"]):
                errs.append(f"library_dependencies[{i}].download_url must be an https:// URL")
            if not SHA256_RE.match(str(lib.get("sha256", ""))):
                errs.append(f"library_dependencies[{i}].sha256 must be 64 lowercase hex characters")

    # ownership: the mod's own binary must live under the submitter's repo.
    if author and repo and isinstance(m.get("download_url"), str):
        if not _url_owned_by(m["download_url"], author, repo):
            errs.append(
                f"download_url {m['download_url']!r} must be a GitHub release asset "
                f"under {author}/{repo} (the repo the id names). A mod can't ship "
                f"someone else's binary as itself.")
    return errs


def _url_owned_by(url, author, repo):
    """True if url is a github.com release asset under <author>/<repo>
    (case-insensitively, GitHub owner/repo are case-insensitive)."""
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme != "https" or p.netloc.lower() != "github.com":
        return False
    parts = [seg for seg in p.path.split("/") if seg]
    return len(parts) >= 2 and parts[0].lower() == author.lower() and parts[1].lower() == repo.lower()


def _is_https_url(url):
    """Pure check: url is a well-formed https:// URL with a host. The block on
    private/internal addresses happens at fetch time (see _assert_safe_to_fetch),
    which needs DNS and so can't run in this network-free validation pass."""
    try:
        p = urlparse(url)
    except Exception:
        return False
    return p.scheme == "https" and bool(p.hostname)


# -- file download + hashing -------------------------------------------------

def _assert_safe_to_fetch(url):
    """Refuse anything but an https:// URL whose host resolves only to public
    addresses. Blocks file:// reads and SSRF to loopback/private/link-local
    addresses, since validation runs in a context holding secrets (see
    .github/workflows/validate.yml). Raises ValidationError if the URL is
    unsafe."""
    p = urlparse(url)
    if p.scheme != "https":
        raise ValidationError(f"refusing to fetch non-https URL {url!r}")
    host = p.hostname
    if not host:
        raise ValidationError(f"refusing to fetch URL with no host: {url!r}")
    try:
        infos = socket.getaddrinfo(host, p.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValidationError(f"cannot resolve host {host!r}: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise ValidationError(
                f"refusing to fetch {url!r}: host {host!r} resolves to "
                f"non-public address {ip}")


def download_and_hash(url):
    """Streams url to a temp file and returns (temp_path, sha256_hex). Caller
    deletes the temp file. Streams (not read-all) so a large asset can't blow up
    memory in CI."""
    _assert_safe_to_fetch(url)
    req = urllib.request.Request(url, headers={"User-Agent": "modindex"})
    fd, tmp = tempfile.mkstemp(prefix="modindex_", suffix=".bin")
    h = hashlib.sha256()
    try:
        with urllib.request.urlopen(req, timeout=120) as r, os.fdopen(fd, "wb") as out:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                h.update(chunk)
                out.write(chunk)
        return tmp, h.hexdigest()
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def _sha256_of_url(url):
    """The sha256 of the file at url, temp file cleaned up. Used by fill-hashes."""
    tmp, digest = download_and_hash(url)
    os.remove(tmp)
    return digest


def _sniff_package_from_url(url):
    """Determines dll vs zip from the first few bytes of the download, without
    pulling the whole file. Used by ingest to set `package` from the actual
    file."""
    _assert_safe_to_fetch(url)
    req = urllib.request.Request(url, headers={"User-Agent": "modindex"})
    with urllib.request.urlopen(req, timeout=60) as r:
        head = r.read(8)
    return _sniff_package(head)


# -- VirusTotal (Layer 1, when a key is configured) --------------------------

# VirusTotal's free API allows 4 requests/minute. A submission can need many
# more calls than that (lookup + upload + polling, per file scanned), so calls
# are paced to stay under the limit:
#   1. _vt_throttle keeps calls under _VT_MAX_PER_MIN in a rolling 60s window,
#      sleeping when the window is full.
#   2. _vt_request retries on HTTP 429, waiting out the window (honouring
#      Retry-After when present).
# Raise the cap via the VT_MAX_PER_MINUTE env var for a higher-tier key.
_VT_WINDOW_SECONDS = 60
_VT_MAX_RETRIES = 5
_vt_request_times = []       # monotonic timestamps of recent VT API calls


def _vt_max_per_min():
    try:
        return max(1, int(os.environ.get("VT_MAX_PER_MINUTE", "4")))
    except ValueError:
        return 4


def _vt_throttle():
    """Block until firing one more VT request stays within the per-minute cap:
    prune timestamps older than the window, sleep if it's still full, then record
    this call."""
    def _prune(now):
        cutoff = now - _VT_WINDOW_SECONDS
        while _vt_request_times and _vt_request_times[0] <= cutoff:
            _vt_request_times.pop(0)

    now = time.monotonic()
    _prune(now)
    if len(_vt_request_times) >= _vt_max_per_min():
        wait = _vt_request_times[0] + _VT_WINDOW_SECONDS - now + 0.5
        if wait > 0:
            print(f"  ..    VirusTotal rate limit reached; waiting {wait:.0f}s for "
                  f"the next slot", flush=True)
            time.sleep(wait)
        _prune(time.monotonic())
    _vt_request_times.append(time.monotonic())


def _vt_request(url, api_key, method="GET", data=None, content_type=None):
    headers = {"x-apikey": api_key, "Accept": "application/json", "User-Agent": "modindex"}
    if content_type:
        headers["Content-Type"] = content_type
    for attempt in range(_VT_MAX_RETRIES + 1):
        _vt_throttle()
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # Retry only on 429, and only while retries remain. Other errors
            # propagate to the caller.
            if e.code != 429 or attempt >= _VT_MAX_RETRIES:
                raise
            try:
                wait = float(e.headers.get("Retry-After")) if e.headers else 0
            except (TypeError, ValueError):
                wait = 0
            wait = max(wait, _VT_WINDOW_SECONDS)
            print(f"  ..    VirusTotal returned 429 (rate limited); waiting "
                  f"{wait:.0f}s then retrying ({attempt + 1}/{_VT_MAX_RETRIES})",
                  flush=True)
            time.sleep(wait)


def vt_lookup(sha256, api_key):
    """Return last_analysis_stats for a hash VT already knows, or None (404)."""
    try:
        j = _vt_request(f"{_VT_BASE}/files/{sha256}", api_key)
        return j["data"]["attributes"]["last_analysis_stats"]
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def vt_upload(path, api_key):
    """Upload a file for analysis, return its analysis id."""
    size = os.path.getsize(path)
    url = f"{_VT_BASE}/files"
    if size > _VT_SIMPLE_UPLOAD_MAX:
        url = _vt_request(f"{_VT_BASE}/files/upload_url", api_key)["data"]
    boundary = uuid.uuid4().hex
    fname = os.path.basename(path)
    with open(path, "rb") as f:
        content = f.read()
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{fname}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        content, b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    j = _vt_request(url, api_key, method="POST", data=body,
                    content_type=f"multipart/form-data; boundary={boundary}")
    return j["data"]["id"]


def vt_wait(analysis_id, api_key, timeout=600):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = _vt_request(f"{_VT_BASE}/analyses/{analysis_id}", api_key)
        attrs = j["data"]["attributes"]
        if attrs.get("status") == "completed":
            return attrs["stats"]
        time.sleep(15)
    raise ValidationError("VirusTotal analysis did not complete in time")


def vt_scan(path, sha256, api_key):
    """Hash-lookup first (free, instant if VT has seen it); upload only if not.
    Returns the analysis stats dict."""
    stats = vt_lookup(sha256, api_key)
    if stats is None:
        stats = vt_wait(vt_upload(path, api_key), api_key)
    return stats


# -- validate command --------------------------------------------------------

def _iter_json(paths):
    for p in paths:
        if os.path.isdir(p):
            for dirpath, _dirs, files in os.walk(p):
                for name in sorted(files):
                    if name.endswith(".json"):
                        yield os.path.join(dirpath, name)
        elif p.endswith(".json"):
            yield p


# Files under submissions/ that aren't submissions: folder READMEs and the
# copy-me TEMPLATE.json. Skipped by validate, fill-hashes, and ingest.
_NON_SUBMISSION_BASENAMES = {"readme.json", "template.json"}


def _is_submission_file(path):
    return os.path.basename(path).lower() not in _NON_SUBMISSION_BASENAMES


def inspect_zip_bundle(path):
    """Content checks for a package="zip" mod bundle, run on the downloaded,
    hash-verified archive. Mirrors the launcher's extract-time guards
    (modmanager._safe_extract_zip): no absolute paths, no '..' traversal, no
    symlinks. Also caps decompression size and confirms a loadable assembly is
    present. Returns (errs, warns).

    MelonLoader only scans a mod's own folder for assemblies (Mods/<id>/*.dll)
    and doesn't recurse into subfolders, so at least one .dll must sit at the
    archive root or the mod would extract fine and never load. Several root
    .dlls are fine. See docs/REPO_STRUCTURE.md."""
    errs, warns = [], []
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        return [f'package is "zip" but the download is not a valid .zip ({e})'], warns
    with zf:
        infos = zf.infolist()
        if len(infos) > ZIP_MAX_ENTRIES:
            errs.append(f"zip has {len(infos)} entries, over the {ZIP_MAX_ENTRIES} cap")
        total = 0
        any_dll = root_dll = 0
        for info in infos:
            name = info.filename
            if name.startswith(("/", "\\")) or (len(name) >= 2 and name[1] == ":"):
                errs.append(f"unsafe zip entry {name!r}: absolute path")
                continue
            norm = name.replace("\\", "/")
            if any(seg == ".." for seg in norm.split("/")):
                errs.append(f"unsafe zip entry {name!r}: path traversal")
                continue
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and (mode & 0o170000) == 0o120000:
                errs.append(f"unsafe zip entry {name!r}: symlink")
                continue
            total += info.file_size
            if info.file_size > ZIP_RATIO_MIN_SIZE and info.compress_size > 0:
                ratio = info.file_size / info.compress_size
                if ratio > ZIP_MAX_RATIO:
                    errs.append(f"zip entry {name!r} decompression ratio {ratio:.0f} "
                                f"exceeds {ZIP_MAX_RATIO} (possible zip bomb)")
            if norm.lower().endswith(".dll"):
                any_dll += 1
                if "/" not in norm.strip("/"):
                    root_dll += 1
        if total > ZIP_MAX_TOTAL_UNCOMPRESSED:
            errs.append(f"zip extracts to {total} bytes, over the "
                        f"{ZIP_MAX_TOTAL_UNCOMPRESSED}-byte cap")
        if any_dll == 0:
            errs.append('package is "zip" but the archive contains no .dll, '
                        "MelonLoader would have nothing to load")
        elif root_dll == 0:
            errs.append("zip bundle has .dll files only in subfolders, none at the "
                        "archive root, MelonLoader loads Mods/<id>/*.dll and does "
                        "not recurse into subfolders, so the mod would never load. "
                        "Put the mod assembly at the archive root.")
    return errs, warns


def _is_code_bearing(name, head):
    """True if a zip member could carry executable code, by extension OR by leading
    magic bytes, so a renamed executable is still caught. `head` is the first few
    bytes of the member."""
    if os.path.splitext(name)[1].lower() in _CODE_EXTENSIONS:
        return True
    return any(head.startswith(m) for m in _CODE_MAGICS)


def _vt_scan_path(path, sha, api_key, vt_max_malicious, label, errs, warns):
    """VT-scan one file at `path` (hash `sha`), appending a note to warns and a hard
    error to errs if malicious exceeds the threshold. A scan that can't run is a
    warning, not a gate (same policy as before)."""
    try:
        stats = vt_scan(path, sha, api_key)
    except Exception as e:
        warns.append(f"{label}: VirusTotal scan could not complete ({e}); not gating on it")
        return
    mal = stats.get("malicious", 0)
    sus = stats.get("suspicious", 0)
    summary = (f"malicious={mal} suspicious={sus} "
               f"harmless={stats.get('harmless', 0)} undetected={stats.get('undetected', 0)}")
    warns.append(f"{label}: VirusTotal "
                 f"{'flagged this file' if (mal or sus) else 'clean'}: {summary}")
    if vt_max_malicious is not None and mal > vt_max_malicious:
        errs.append(f"{label}: VirusTotal malicious={mal} exceeds "
                    f"--vt-max-malicious={vt_max_malicious}")


def _vt_scan_zip_members(zip_path, api_key, vt_max_malicious, label, errs, warns):
    """VT-scan only the code-bearing members of a zip bundle, each individually,
    instead of uploading the whole archive (which for a bulky asset mod would be
    huge and mostly inert). Inert members are read only far enough to check their
    magic bytes; a code-bearing one is extracted to a temp file and scanned."""
    scanned = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            with zf.open(info) as f:
                head = f.read(8)
                if not _is_code_bearing(info.filename, head):
                    continue                       # inert asset, not read past its head
                data = head + f.read()
            scanned += 1
            sha = hashlib.sha256(data).hexdigest()
            fd, mtmp = tempfile.mkstemp(prefix="modindex_member_")
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(data)
                _vt_scan_path(mtmp, sha, api_key, vt_max_malicious,
                              f"{label} :: {info.filename}", errs, warns)
            finally:
                if os.path.exists(mtmp):
                    os.remove(mtmp)
    if scanned == 0:
        warns.append(f"{label}: no executable members in the zip to VirusTotal-scan "
                     "(all inert assets); the archive as a whole was not uploaded")


def _check_file(url, expected_sha, api_key, vt_max_malicious, label, allow_zip=False):
    """Download url, verify its sha256 == expected, and scan it. For the mod
    (allow_zip=True) the file type is sniffed from its own bytes: a zip is inspected
    and its code-bearing members VT-scanned (not the whole archive, see
    _vt_scan_zip_members); a single dll is VT-scanned whole. Libraries
    (allow_zip=False) are always single files. Returns (hard_errors, warnings).
    Cleans up its temp file."""
    errs, warns = [], []
    try:
        tmp, actual = download_and_hash(url)
    except Exception as e:
        return [f"{label}: could not download {url} ({e})"], warns
    try:
        if actual.lower() != str(expected_sha).lower():
            errs.append(f"{label}: sha256 mismatch, manifest says {expected_sha}, file is {actual}")
            return errs, warns
        is_zip = False
        if allow_zip:
            with open(tmp, "rb") as f:
                is_zip = _sniff_package(f.read(8)) == "zip"
        if is_zip:
            warns.append(f"{label}: detected a zip bundle (package=zip)")
            ze, zw = inspect_zip_bundle(tmp)
            errs += [f"{label}: {e}" for e in ze]
            warns += [f"{label}: {w}" for w in zw]
            if errs:
                return errs, warns     # a structurally-broken bundle isn't worth scanning
            if api_key:
                _vt_scan_zip_members(tmp, api_key, vt_max_malicious, label, errs, warns)
            else:
                warns.append(f"{label}: VirusTotal skipped (no VIRUSTOTAL_API_KEY set)")
        elif api_key:
            _vt_scan_path(tmp, actual, api_key, vt_max_malicious, label, errs, warns)
        else:
            warns.append(f"{label}: VirusTotal skipped (no VIRUSTOTAL_API_KEY set)")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return errs, warns


def cmd_validate(args):
    root = repo_root(args.repo)
    targets = args.paths or [os.path.join(root, "submissions")]
    # README.json and the copy-me TEMPLATE.json are not submissions; an empty
    # submissions/ folder is also fine (no manifests yet).
    files = [f for f in _iter_json(targets) if _is_submission_file(f)]
    if not files:
        print("No submission manifests to validate.")
        return 0

    api_key = os.environ.get("VIRUSTOTAL_API_KEY", "").strip() or None
    failures = 0

    for path in files:
        rel = os.path.relpath(path, root)
        print(f"\n== {rel} ==")
        try:
            m = _load_json(path)
        except Exception as e:
            print(f"  FAIL  not valid JSON: {e}")
            failures += 1
            continue

        errs = validate_structure(m)
        warns = []

        if not errs:
            e, w = _check_file(m["download_url"], m["sha256"], api_key,
                               args.vt_max_malicious, "mod", allow_zip=True)
            errs += e
            warns += w
            for i, lib in enumerate(m.get("library_dependencies", [])):
                e, w = _check_file(lib["download_url"], lib["sha256"], api_key,
                                   args.vt_max_malicious, f"library[{i}] {lib.get('name', '')}")
                errs += e
                warns += w

        for w in warns:
            print(f"  note  {w}")
        if errs:
            failures += 1
            for e in errs:
                print(f"  FAIL  {e}")
        else:
            print("  OK    structurally valid, all hashes verified")

    print(f"\n{len(files)} manifest(s) checked, {failures} failed.")
    return 1 if failures else 0


# -- fill-hashes command -----------------------------------------------------

def _fill_one(obj, label):
    """Downloads obj['download_url'], writes its sha256 into obj['sha256'].
    Returns True on success, False if it couldn't be fetched."""
    url = obj.get("download_url")
    if not url:
        print(f"  skip  {label}: no download_url")
        return False
    try:
        digest = _sha256_of_url(url)
    except Exception as e:
        print(f"  FAIL  {label}: could not download {url} ({e})")
        return False
    old = obj.get("sha256")
    obj["sha256"] = digest
    print(f"  set   {label}: {digest}" + (f"  (was {old})" if old and old != digest else ""))
    return True


def cmd_fill(args):
    """Populate the sha256 fields in a manifest by downloading the files it points
    at. Run it after publishing your release and before you submit, so the hashes
    you commit are the real ones (validation still verifies them)."""
    root = repo_root(args.repo)
    targets = args.paths or [os.path.join(root, "submissions")]
    files = [f for f in _iter_json(targets) if _is_submission_file(f)]
    if not files:
        print("No manifest files to fill.")
        return 0

    failures = 0
    for path in files:
        print(f"\n== {os.path.relpath(path, root)} ==")
        try:
            m = _load_json(path)
        except Exception as e:
            print(f"  FAIL  not valid JSON: {e}")
            failures += 1
            continue
        ok = _fill_one(m, "mod")
        for i, lib in enumerate(m.get("library_dependencies", [])):
            ok = _fill_one(lib, f"library[{i}] {lib.get('name', '')}") and ok
        if ok:
            _write_json(path, m)     # rewrite in place, hashes filled in
            print(f"  wrote {os.path.relpath(path, root)}")
        else:
            failures += 1
            print("  left unchanged (a file couldn't be fetched)")

    print(f"\n{len(files)} manifest(s) processed, {failures} with problems.")
    return 1 if failures else 0


# -- build command -----------------------------------------------------------

def _vkey(version):
    return tuple(int(x) for x in version.split("."))


def _ingest_submissions(root):
    """Validate (structurally) and move submissions/<id>/<v>.json into
    manifests/<author>/<repo>/<v>.json. Returns list of source paths moved."""
    sub_dir = os.path.join(root, "submissions")
    man_dir = os.path.join(root, "manifests")
    moved = []
    for path in _iter_json([sub_dir]):
        if not _is_submission_file(path):
            continue
        m = _load_json(path)
        errs = validate_structure(m)
        if errs:
            raise ValidationError(
                f"{os.path.relpath(path, root)} is not structurally valid:\n  - "
                + "\n  - ".join(errs))
        # Determine dll vs zip from the actual file and bake it into the
        # compiled manifest; submitters never declare it.
        m["package"] = _sniff_package_from_url(m["download_url"])
        author, repo = parse_id(m["id"])
        dest = os.path.join(man_dir, author, repo, f'{m["version"]}.json')
        _write_json(dest, ordered_manifest(m))
        moved.append(path)
        print(f"ingested {os.path.relpath(path, root)} -> {os.path.relpath(dest, root)}")
    return moved


def _collect_manifests(root):
    """id -> {version: manifest} from every manifests/**/<version>.json,
    ignoring latest*.json pointers."""
    man_dir = os.path.join(root, "manifests")
    mods = {}
    for dirpath, _dirs, files in os.walk(man_dir):
        for name in sorted(files):
            if not name.endswith(".json") or name.startswith("latest"):
                continue
            m = _load_json(os.path.join(dirpath, name))
            errs = validate_structure(m)
            if errs:
                raise ValidationError(
                    f"{os.path.relpath(os.path.join(dirpath, name), root)} is invalid:\n  - "
                    + "\n  - ".join(errs))
            mods.setdefault(m["id"], {})[m["version"]] = m
    return mods


def cmd_build(args):
    root = repo_root(args.repo)
    moved = _ingest_submissions(root) if args.ingest else []

    mods = _collect_manifests(root)
    repo_index = {"index_version": SUPPORTED_INDEX_MAJOR, "mods": {}}

    # An empty manifests/ tree is valid (a freshly published repo with no mods),
    # so it still has to write an empty repository.json. Otherwise removing the
    # last mod would leave a stale index behind.
    for mod_id in sorted(mods):
        versions = mods[mod_id]
        author, repo = parse_id(mod_id)
        mod_dir = os.path.join(root, "manifests", author, repo)

        ordered_versions = sorted(versions, key=_vkey)
        highest_overall = ordered_versions[-1]

        # versions grouped per major, ascending (so the last in each is highest)
        versions_by_major = {}
        for v in ordered_versions:
            versions_by_major.setdefault(v.split(".")[0], []).append(v)

        # pointer files: overall latest, plus the highest in each major
        _write_json(os.path.join(mod_dir, "latest.json"),
                    ordered_manifest(versions[highest_overall]))
        for major, vs in versions_by_major.items():
            _write_json(os.path.join(mod_dir, f"latest.{major}.json"),
                        ordered_manifest(versions[vs[-1]]))

        # repository.json entry: one slim discovery summary per major
        repo_index["mods"][mod_id] = {}
        for major in sorted(versions_by_major, key=int):
            vs = versions_by_major[major]
            repo_index["mods"][mod_id][major] = summary_entry(versions[vs[-1]], vs)
        print(f"indexed {mod_id}: majors {sorted(versions_by_major, key=int)} "
              f"(latest {highest_overall})")

    _write_json(os.path.join(root, "repository.json"), repo_index)
    print(f"\nwrote repository.json ({len(repo_index['mods'])} mod(s))")

    if args.prune:
        for path in moved:
            os.remove(path)
            # tidy now-empty submission dirs (but never the submissions/ root)
            d = os.path.dirname(path)
            sub_root = os.path.join(root, "submissions")
            while os.path.abspath(d) != os.path.abspath(sub_root) and os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
                d = os.path.dirname(d)
        if moved:
            print(f"pruned {len(moved)} ingested submission file(s)")
    return 0


# -- entry point --------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(prog="modindex", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", help="repository root (default: parent of this tools/ dir)")
    sub = parser.add_subparsers(dest="command", required=True)

    f = sub.add_parser("fill-hashes", help="download a manifest's files and write their sha256 in")
    f.add_argument("paths", nargs="*", help="manifest files or dirs (default: submissions/)")
    f.set_defaults(func=cmd_fill)

    v = sub.add_parser("validate", help="Layer-1 validation of submission manifests")
    v.add_argument("paths", nargs="*", help="manifest files or dirs (default: submissions/)")
    v.add_argument("--vt-max-malicious", type=int, default=None,
                   help="fail if VirusTotal malicious count exceeds this (default: report only)")
    v.set_defaults(func=cmd_validate)

    b = sub.add_parser("build", help="compile pointers + repository.json")
    b.add_argument("--ingest", action="store_true",
                   help="first move validated submissions/ manifests into manifests/")
    b.add_argument("--prune", action="store_true",
                   help="with --ingest, delete the moved submission files afterwards")
    b.set_defaults(func=cmd_build)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ValidationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
