"""
Tests for tools/modindex.py, the repository's validation rules and index
compiler. Pure and offline: structural validation and the build/compile path
touch no network (only `validate`'s hash/VirusTotal steps do, and those are not
exercised here). Run from the repo root:  python -m unittest -v  or
python tests/test_modindex.py
"""

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
import modindex as mi


def manifest(mod_id="Owner.Repo", version="1.0.0", **over):
    # tolerate a deliberately malformed id (no dot) so tests can build one
    author, repo = (mod_id.split(".", 1) + ["Repo"])[:2]
    d = {
        "manifest_version": 1,
        "id": mod_id,
        "name": repo,
        "version": version,
        "author": author,
        "description": "",
        "client_side": True,
        "server_side": True,
        "dependencies": {},
        "library_dependencies": [],
        "download_url": f"https://github.com/{author}/{repo}/releases/download/v{version}/{repo}.dll",
        "sha256": "a" * 64,
    }
    d.update(over)
    return d


class Structure(unittest.TestCase):
    def test_valid_manifest_passes(self):
        self.assertEqual(mi.validate_structure(manifest()), [])

    def test_rules_reject(self):
        cases = {
            "manifest_version": manifest(manifest_version=2),
            "bad id": manifest("noseparator"),
            "bad version": manifest(version="1.0"),
            "no sides": manifest(client_side=False, server_side=False),
            "short sha": manifest(sha256="abc"),
            "uppercase sha": manifest(sha256="A" * 64),
            "bad lib": manifest(library_dependencies=[
                {"name": "x", "download_url": "https://x/y.dll",
                 "sha256": "a" * 64, "filename": "sub/y.dll"}]),
            "non-https mod": manifest(
                download_url="http://github.com/Owner/Repo/releases/download/v1/x.dll"),
            "file url lib": manifest(library_dependencies=[
                {"name": "x", "download_url": "file:///etc/passwd",
                 "sha256": "a" * 64, "filename": "y.dll"}]),
        }
        for label, m in cases.items():
            with self.subTest(label):
                self.assertTrue(mi.validate_structure(m), f"{label} should have failed")

    def test_package_is_tooling_set_but_validated_if_present(self):
        # Submitters omit package (the tooling sniffs and sets it); absent is fine.
        self.assertEqual(mi.validate_structure(manifest()), [])
        # If present it must still be valid, and survives canonical re-ordering.
        self.assertEqual(mi.validate_structure(manifest(package="zip")), [])
        self.assertTrue(mi.validate_structure(manifest(package="rar")))
        self.assertEqual(mi.ordered_manifest(manifest(package="zip"))["package"], "zip")

    def test_sniff_package(self):
        self.assertEqual(mi._sniff_package(b"PK\x03\x04rest"), "zip")
        self.assertEqual(mi._sniff_package(b"MZ\x90\x00rest"), "dll")
        self.assertEqual(mi._sniff_package(b""), "dll")

    def test_mod_has_no_filename_field(self):
        # A mod-level filename is neither required nor rejected, it's simply not
        # part of the schema anymore (the mod installs as <id>.dll).
        self.assertEqual(mi.validate_structure(manifest()), [])
        self.assertNotIn("filename", mi.ordered_manifest(manifest()))

    def test_ownership_enforced_for_mod_not_library(self):
        # mod binary must be under the id's owner/repo
        self.assertTrue(mi.validate_structure(
            manifest(download_url="https://github.com/someoneelse/thing/releases/download/v1/x.dll")))
        # a library may point anywhere (third-party, unowned), no ownership rule
        self.assertEqual(mi.validate_structure(manifest(library_dependencies=[
            {"name": "ExampleLib", "download_url": "https://example.com/downloads/ExampleLib.dll",
             "sha256": "b" * 64, "filename": "ExampleLib.dll"}])), [])

    def test_parse_id(self):
        self.assertEqual(mi.parse_id("User.Repo"), ("User", "Repo"))
        self.assertEqual(mi.parse_id("User.dotted.repo.name"), ("User", "dotted.repo.name"))
        for bad in ("nodot", ".Repo", "-bad.Repo", "User./evil"):
            with self.assertRaises(mi.ValidationError):
                mi.parse_id(bad)


class InspectZipBundle(unittest.TestCase):
    """The package="zip" content checks, run on the downloaded archive."""

    def _zip(self, entries):
        """entries: (arcname, data) or (arcname, data, external_attr)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for e in entries:
                if len(e) == 3:
                    zi = zipfile.ZipInfo(e[0]); zi.external_attr = e[2]
                    zf.writestr(zi, e[1])
                else:
                    zf.writestr(e[0], e[1])
        tmp = tempfile.NamedTemporaryFile(prefix="zt_", suffix=".zip", delete=False)
        tmp.write(buf.getvalue()); tmp.close()
        self.addCleanup(lambda p=tmp.name: os.path.exists(p) and os.remove(p))
        return tmp.name

    def test_valid_bundle_passes(self):
        errs, warns = mi.inspect_zip_bundle(self._zip([
            ("Bundle.dll", b"MZ"), ("Content/data.bundle", b"assets")]))
        self.assertEqual(errs, [])

    def test_bad_zip_rejected(self):
        tmp = tempfile.NamedTemporaryFile(prefix="nz_", suffix=".zip", delete=False)
        tmp.write(b"not a zip"); tmp.close()
        self.addCleanup(lambda: os.remove(tmp.name))
        self.assertTrue(mi.inspect_zip_bundle(tmp.name)[0])

    def test_no_dll_is_error(self):
        errs, _ = mi.inspect_zip_bundle(self._zip([("readme.txt", b"hi")]))
        self.assertTrue(any("no .dll" in e for e in errs))

    def test_dll_only_in_subfolder_is_error(self):
        # MelonLoader loads Mods/<id>/*.dll and does not recurse, so a nested-only
        # .dll would never load, hard fail, not a warning.
        errs, _ = mi.inspect_zip_bundle(self._zip([("sub/Mod.dll", b"MZ")]))
        self.assertTrue(any("archive root" in e for e in errs))

    def test_traversal_and_absolute_and_symlink_rejected(self):
        self.assertTrue(mi.inspect_zip_bundle(self._zip([("../evil.dll", b"x")]))[0])
        self.assertTrue(mi.inspect_zip_bundle(self._zip([("/abs.dll", b"x")]))[0])
        self.assertTrue(mi.inspect_zip_bundle(
            self._zip([("link", b"/etc/passwd", 0o120777 << 16)]))[0])

    def test_too_many_entries_rejected(self):
        saved = mi.ZIP_MAX_ENTRIES
        mi.ZIP_MAX_ENTRIES = 3
        self.addCleanup(lambda: setattr(mi, "ZIP_MAX_ENTRIES", saved))
        errs, _ = mi.inspect_zip_bundle(self._zip(
            [("m.dll", b"MZ")] + [(f"f{i}", b"x") for i in range(5)]))
        self.assertTrue(any("entries" in e for e in errs))

    def test_decompression_ratio_flagged(self):
        saved = mi.ZIP_RATIO_MIN_SIZE
        mi.ZIP_RATIO_MIN_SIZE = 1024
        self.addCleanup(lambda: setattr(mi, "ZIP_RATIO_MIN_SIZE", saved))
        # 2 MB of zeros compresses tiny -> huge ratio.
        errs, _ = mi.inspect_zip_bundle(self._zip([
            ("m.dll", b"MZ"), ("big.bin", b"\0" * (2 * 1024 * 1024))]))
        self.assertTrue(any("ratio" in e for e in errs))


class ZipMemberScanning(unittest.TestCase):
    """Only code-bearing members of a zip get VirusTotal-scanned, not the whole
    archive (a bulky mod is mostly inert assets)."""

    def _zip(self, entries):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in entries:
                zf.writestr(name, data)
        tmp = tempfile.NamedTemporaryFile(prefix="zm_", suffix=".zip", delete=False)
        tmp.write(buf.getvalue()); tmp.close()
        self.addCleanup(lambda p=tmp.name: os.path.exists(p) and os.remove(p))
        return tmp.name

    def _fake_vt(self, stats):
        """Replace mi.vt_scan with one that records the bytes it was handed."""
        seen = []
        def fake(path, sha, api_key):
            with open(path, "rb") as f:
                seen.append(f.read())
            return stats
        saved = mi.vt_scan
        mi.vt_scan = fake
        self.addCleanup(lambda: setattr(mi, "vt_scan", saved))
        return seen

    def test_is_code_bearing(self):
        self.assertTrue(mi._is_code_bearing("Mod.dll", b""))
        self.assertTrue(mi._is_code_bearing("run.ps1", b""))
        self.assertFalse(mi._is_code_bearing("model.fbx", b"asdf"))
        self.assertFalse(mi._is_code_bearing("tex.png", b"\x89PNG\r\n"))
        # a renamed executable is caught by magic bytes despite an inert extension
        self.assertTrue(mi._is_code_bearing("totally_a.png", b"MZ\x90\x00"))

    def test_only_code_members_scanned(self):
        seen = self._fake_vt({"malicious": 0, "suspicious": 0, "harmless": 1, "undetected": 60})
        z = self._zip([
            ("Mod.dll", b"MZ real-assembly"),
            ("Assets/model.fbx", b"\0" * 4096),            # inert, skipped
            ("Assets/tex.png", b"\x89PNG" + b"\0" * 100),  # inert, skipped
            ("sneaky.dat", b"MZ hidden-exe"),              # renamed exe, magic-caught
        ])
        errs, warns = [], []
        mi._vt_scan_zip_members(z, "APIKEY", 0, "mod", errs, warns)
        self.assertEqual(errs, [])
        self.assertEqual(len(seen), 2)                     # only the two code members
        self.assertIn(b"MZ real-assembly", seen)
        self.assertIn(b"MZ hidden-exe", seen)

    def test_malicious_member_gates(self):
        self._fake_vt({"malicious": 5, "suspicious": 0, "harmless": 0, "undetected": 60})
        z = self._zip([("Evil.dll", b"MZ bad")])
        errs, warns = [], []
        mi._vt_scan_zip_members(z, "APIKEY", 0, "mod", errs, warns)
        self.assertTrue(any("malicious=5" in e for e in errs))

    def test_no_code_members_uploads_nothing(self):
        seen = self._fake_vt({"malicious": 0, "suspicious": 0, "harmless": 0, "undetected": 1})
        z = self._zip([("Assets/model.fbx", b"\0" * 100), ("readme.txt", b"hi")])
        errs, warns = [], []
        mi._vt_scan_zip_members(z, "APIKEY", 0, "mod", errs, warns)
        self.assertEqual(seen, [])
        self.assertTrue(any("no executable members" in w for w in warns))


class _FakeResp:
    def __init__(self, data):
        self._data = data
    def read(self, *a):
        return self._data
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class VirusTotalRateLimit(unittest.TestCase):
    """Pacing + 429 retry so a submission needing >4 VT calls waits rather than
    getting throttled and skipping scans. sleep is stubbed so tests are instant."""

    def setUp(self):
        mi._vt_request_times.clear()
        self.slept = []
        saved_sleep = mi.time.sleep
        mi.time.sleep = lambda s: self.slept.append(s)
        self.addCleanup(lambda: setattr(mi.time, "sleep", saved_sleep))
        saved_urlopen = mi.urllib.request.urlopen
        self.addCleanup(lambda: setattr(mi.urllib.request, "urlopen", saved_urlopen))

    def _err(self, code, headers=None):
        return urllib.error.HTTPError("https://x/", code, "err", headers or {}, None)

    def test_retries_on_429_then_succeeds(self):
        calls = {"n": 0}
        def fake(req, timeout=120):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise self._err(429, {"Retry-After": "1"})
            return _FakeResp(b'{"ok": true}')
        mi.urllib.request.urlopen = fake
        self.assertEqual(mi._vt_request("https://x/", "KEY"), {"ok": True})
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(self.slept), 2)          # waited before each retry

    def test_gives_up_after_max_retries(self):
        # Persistent 429 eventually re-raises HTTPError after exhausting retries.
        calls = {"n": 0}
        def always_429(req, timeout=120):
            calls["n"] += 1
            raise self._err(429)
        mi.urllib.request.urlopen = always_429
        with self.assertRaises(urllib.error.HTTPError):
            mi._vt_request("https://x/", "KEY")
        self.assertEqual(calls["n"], mi._VT_MAX_RETRIES + 1)   # initial try + retries

    def test_non_429_propagates(self):
        mi.urllib.request.urlopen = lambda req, timeout=120: (_ for _ in ()).throw(self._err(500))
        with self.assertRaises(urllib.error.HTTPError):
            mi._vt_request("https://x/", "KEY")

    def test_throttle_sleeps_when_window_full(self):
        clock = {"t": 1000.0}
        saved_mono = mi.time.monotonic
        mi.time.monotonic = lambda: clock["t"]
        self.addCleanup(lambda: setattr(mi.time, "monotonic", saved_mono))
        mi.time.sleep = lambda s: (self.slept.append(s), clock.__setitem__("t", clock["t"] + s))
        mi._vt_request_times[:] = [1000.0] * 4        # window full at the default cap
        mi._vt_throttle()
        self.assertTrue(self.slept)                   # had to wait for a slot

    def test_throttle_no_wait_under_limit(self):
        mi._vt_throttle()
        mi._vt_throttle()
        self.assertEqual(self.slept, [])              # first calls never wait


class FetchSafety(unittest.TestCase):
    def test_is_https_url(self):
        self.assertTrue(mi._is_https_url("https://github.com/a/b"))
        for bad in ("http://github.com/a/b", "file:///etc/passwd",
                    "ftp://x/y", "not a url", ""):
            self.assertFalse(mi._is_https_url(bad))

    def test_assert_safe_rejects_unsafe(self):
        # scheme rejects and IP-literal / localhost lookups all resolve without
        # any external network, so this stays offline.
        for bad in ("http://example.com/x", "file:///etc/passwd", "ftp://x/y",
                    "https://127.0.0.1/x", "https://169.254.169.254/x",
                    "https://[::1]/x", "https://10.0.0.5/x", "https://localhost/x"):
            with self.subTest(bad):
                with self.assertRaises(mi.ValidationError):
                    mi._assert_safe_to_fetch(bad)

    def test_assert_safe_allows_public_host(self):
        saved = mi.socket.getaddrinfo
        mi.socket.getaddrinfo = lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))]
        try:
            mi._assert_safe_to_fetch("https://example.com/x")   # must not raise
        finally:
            mi.socket.getaddrinfo = saved


class Build(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="modindex_repo_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        # Ingest sniffs the download to set `package`; stub that network read.
        saved = mi._sniff_package_from_url
        mi._sniff_package_from_url = lambda url: "dll"
        self.addCleanup(lambda: setattr(mi, "_sniff_package_from_url", saved))

    def _submit(self, m):
        path = os.path.join(self.root, "submissions", m["id"], f'{m["version"]}.json')
        mi._write_json(path, m)

    def _build(self, ingest=True, prune=True):
        args = argparse.Namespace(repo=self.root, ingest=ingest, prune=prune)
        return mi.cmd_build(args)

    def _read(self, *parts):
        with open(os.path.join(self.root, *parts), encoding="utf-8") as f:
            return json.load(f)

    def test_ingest_generates_manifests_pointers_and_index(self):
        self._submit(manifest("Owner.Repo", "1.0.0"))
        self._build()
        base = os.path.join("manifests", "Owner", "Repo")
        self.assertTrue(os.path.isfile(os.path.join(self.root, base, "1.0.0.json")))
        self.assertEqual(self._read(base, "latest.json")["version"], "1.0.0")
        self.assertEqual(self._read(base, "latest.1.json")["version"], "1.0.0")
        # repository.json entry is a slim summary: versions list, no metrics,
        # and none of the install-critical data (that lives in the manifest).
        entry = self._read("repository.json")["mods"]["Owner.Repo"]["1"]
        self.assertEqual(entry["versions"], ["1.0.0"])
        self.assertEqual(entry["name"], "Repo")
        for absent in ("stars", "downloads", "download_url", "sha256",
                       "dependencies", "library_dependencies", "version"):
            self.assertNotIn(absent, entry)
        # the per-version manifest still has the full data
        full = self._read(base, "1.0.0.json")
        self.assertIn("download_url", full)
        self.assertIn("sha256", full)
        # submission consumed
        self.assertFalse(os.path.exists(os.path.join(self.root, "submissions", "Owner.Repo")))

    def test_ingest_sets_package_from_sniff(self):
        # Submitter provides no package; ingest sniffs the file and writes it.
        self._submit(manifest("Owner.Repo", "1.0.0"))          # no "package" key
        self._build()
        self.assertEqual(self._read("manifests", "Owner", "Repo", "1.0.0.json")["package"], "dll")

    def test_ingest_sets_package_zip_when_sniffed(self):
        mi._sniff_package_from_url = lambda url: "zip"
        self._submit(manifest("Owner.Repo", "1.0.0"))
        self._build()
        self.assertEqual(self._read("manifests", "Owner", "Repo", "1.0.0.json")["package"], "zip")

    def test_highest_per_major(self):
        for v in ("1.0.0", "1.2.0", "2.0.0"):
            self._submit(manifest("Owner.Repo", v))
        self._build()
        base = os.path.join("manifests", "Owner", "Repo")
        self.assertEqual(self._read(base, "latest.json")["version"], "2.0.0")
        self.assertEqual(self._read(base, "latest.1.json")["version"], "1.2.0")
        self.assertEqual(self._read(base, "latest.2.json")["version"], "2.0.0")
        repo = self._read("repository.json")
        self.assertEqual(set(repo["mods"]["Owner.Repo"].keys()), {"1", "2"})
        self.assertEqual(repo["mods"]["Owner.Repo"]["1"]["versions"], ["1.0.0", "1.2.0"])
        self.assertEqual(repo["mods"]["Owner.Repo"]["2"]["versions"], ["2.0.0"])

    def test_build_is_idempotent(self):
        self._submit(manifest("Owner.Repo", "1.0.0"))
        self._build()
        with open(os.path.join(self.root, "repository.json"), "rb") as f:
            first = f.read()
        self._build(ingest=True, prune=True)   # nothing left to ingest; rebuild
        with open(os.path.join(self.root, "repository.json"), "rb") as f:
            second = f.read()
        self.assertEqual(first, second)

    def test_empty_repo_writes_valid_empty_index(self):
        # a freshly published repo with no mods must still get a valid index
        self._build(ingest=True, prune=True)
        self.assertEqual(self._read("repository.json"),
                         {"index_version": 1, "mods": {}})

    def test_invalid_submission_blocks_build(self):
        self._submit(manifest("Owner.Repo", version="nope"))
        with self.assertRaises(mi.ValidationError):
            self._build()

    def test_template_is_ignored(self):
        # The reserved TEMPLATE.json holds placeholder (structurally invalid)
        # content. It must never block a build, land in the index, or fail
        # validation.
        tmpl = os.path.join(self.root, "submissions", "TEMPLATE.json")
        mi._write_json(tmpl, {"id": "YourGitHubUser.YourRepo", "version": "1.0.0"})
        self._submit(manifest("Owner.Repo", "1.0.0"))
        self._build()                                # must not raise on the template
        self.assertTrue(os.path.exists(tmpl))        # left in place, not pruned
        self.assertEqual(set(self._read("repository.json")["mods"]), {"Owner.Repo"})
        # only the template remains under submissions/; validate skips it and passes
        rc = mi.cmd_validate(argparse.Namespace(
            repo=self.root, paths=None, vt_max_malicious=None))
        self.assertEqual(rc, 0)


class FillHashes(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="modindex_fill_")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))

    def test_fill_writes_hashes_from_urls(self):
        # stub the network: hash is derived from the URL so we can assert it
        saved = mi._sha256_of_url
        mi._sha256_of_url = lambda url: hashlib.sha256(url.encode()).hexdigest()
        try:
            m = manifest(sha256="PLACEHOLDER", library_dependencies=[
                {"name": "L", "download_url": "https://x/L.dll",
                 "sha256": "PLACEHOLDER", "filename": "L.dll"}])
            path = os.path.join(self.root, "submissions", m["id"], f'{m["version"]}.json')
            mi._write_json(path, m)

            rc = mi.cmd_fill(argparse.Namespace(repo=self.root, paths=[path]))
            self.assertEqual(rc, 0)

            out = json.load(open(path, encoding="utf-8"))
            self.assertEqual(out["sha256"], hashlib.sha256(m["download_url"].encode()).hexdigest())
            self.assertEqual(out["library_dependencies"][0]["sha256"],
                             hashlib.sha256(b"https://x/L.dll").hexdigest())
            # a filled manifest then passes structural validation
            self.assertEqual(mi.validate_structure(out), [])
        finally:
            mi._sha256_of_url = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
