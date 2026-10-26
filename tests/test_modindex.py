"""
Tests for tools/modindex.py, the repository's validation rules and index
compiler. Pure and offline: structural validation and the build/compile path
touch no network, and the download/VirusTotal steps are exercised only with
the network stubbed out. Run from the repo root:  python -m unittest -v  or
python tests/test_modindex.py
"""

import argparse
import contextlib
import copy
import glob
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
import modindex as mi

FIXTURES = os.path.join(HERE, "fixtures", "v2")


def artifact(owner="Owner", repo="Repo", version="1.0.0", targets=("pc", "server"),
             asset=None, **over):
    """One entry of `artifacts`, a release asset under owner/repo by default."""
    a = {
        "targets": list(targets),
        "download_url": (f"https://github.com/{owner}/{repo}/releases/download/"
                         f"v{version}/{asset or repo + '.dll'}"),
        "sha256": "a" * 64,
        "library_dependencies": [],
    }
    a.update(over)
    return a


def library(name="ExampleLib", sha="b" * 64, **over):
    lib = {"name": name, "download_url": f"https://example.com/downloads/{name}.dll",
           "sha256": sha, "filename": f"{name}.dll"}
    lib.update(over)
    return lib


def manifest(mod_id="Owner.Repo", version="1.0.0", targets=("pc", "server"),
             artifacts=None, **over):
    """A valid v2 manifest. `targets` shapes the single default artifact;
    `artifacts` replaces the list outright."""
    # tolerate a deliberately malformed id (no dot) so tests can build one
    author, repo = (mod_id.split(".", 1) + ["Repo"])[:2]
    d = {
        "manifest_version": 2,
        "id": mod_id,
        "name": repo,
        "version": version,
        "author": author,
        "description": "",
        "parity_required": True,
        "dependencies": {},
        "artifacts": (artifacts if artifacts is not None
                      else [artifact(author, repo, version, targets)]),
    }
    d.update(over)
    return d


def with_artifact(m, i=0, **over):
    """m with artifacts[i] updated (keys set to None are removed)."""
    m = copy.deepcopy(m)
    for k, v in over.items():
        if v is None:
            m["artifacts"][i].pop(k, None)
        else:
            m["artifacts"][i][k] = v
    return m


def errors_about(errs, *needles):
    return [e for e in errs if all(n in e for n in needles)]


class Structure(unittest.TestCase):
    def test_valid_manifest_passes(self):
        self.assertEqual(mi.validate_structure(manifest()), [])

    def test_rules_reject(self):
        cases = {
            "future manifest_version": manifest(manifest_version=3),
            "boolean manifest_version": manifest(manifest_version=True),
            "bad id": manifest("noseparator"),
            "bad version": manifest(version="1.0"),
            "version segment over int32": manifest(version="1.2147483648.0"),
            "short sha": with_artifact(manifest(), sha256="abc"),
            "uppercase sha": with_artifact(manifest(), sha256="A" * 64),
            "bad lib filename": with_artifact(manifest(), library_dependencies=[
                library(filename="sub/y.dll")]),
            "lib with no name": with_artifact(manifest(), library_dependencies=[
                library(name="")]),
            "non-https artifact": with_artifact(manifest(), download_url=(
                "http://github.com/Owner/Repo/releases/download/v1/x.dll")),
            "file url lib": with_artifact(manifest(), library_dependencies=[
                library(download_url="file:///etc/passwd")]),
            "no dependencies": {k: v for k, v in manifest().items() if k != "dependencies"},
            "no library_dependencies": with_artifact(manifest(), library_dependencies=None),
            "no download_url": with_artifact(manifest(), download_url=None),
            "not an object": [manifest()],
        }
        for label, m in cases.items():
            with self.subTest(label):
                self.assertTrue(mi.validate_structure(m), f"{label} should have failed")

    def test_package_is_tooling_set_but_validated_if_present(self):
        # Submitters omit package (the tooling sniffs and sets it); absent is fine.
        self.assertEqual(mi.validate_structure(manifest()), [])
        # If present it must still be valid, and survives canonical re-ordering.
        self.assertEqual(mi.validate_structure(with_artifact(manifest(), package="zip")), [])
        errs = mi.validate_structure(with_artifact(manifest(), package="rar"))
        self.assertTrue(errors_about(errs, "artifacts[0].package"), errs)
        ordered = mi.ordered_manifest(with_artifact(manifest(), package="zip"))
        self.assertEqual(ordered["artifacts"][0]["package"], "zip")

    def test_sniff_package(self):
        self.assertEqual(mi._sniff_package(b"PK\x03\x04rest"), "zip")
        self.assertEqual(mi._sniff_package(b"MZ\x90\x00rest"), "dll")
        self.assertEqual(mi._sniff_package(b""), "dll")

    def test_mod_has_no_filename_field(self):
        # A mod-level filename is neither required nor rejected, it's simply not
        # part of the schema (a dll artifact keeps its published name).
        self.assertEqual(mi.validate_structure(manifest()), [])
        self.assertNotIn("filename", mi.ordered_manifest(manifest()))

    def test_library_filename_rejects_colons(self):
        """Matches modmanager's _safe_basename and TavernLib's SafeBasename: a
        colon is the drive separator, and on NTFS the alternate-data-stream
        separator, so "ExampleLib.dll:payload" names a hidden stream."""
        for bad in ("C:ExampleLib.dll", "ExampleLib.dll:payload"):
            m = with_artifact(manifest(), library_dependencies=[library(filename=bad)])
            self.assertTrue(mi.validate_structure(m), bad)

    def test_parse_id(self):
        self.assertEqual(mi.parse_id("User.Repo"), ("User", "Repo"))
        self.assertEqual(mi.parse_id("User.dotted.repo.name"), ("User", "dotted.repo.name"))
        for bad in ("nodot", ".Repo", "-bad.Repo", "User./evil"):
            with self.assertRaises(mi.ValidationError):
                mi.parse_id(bad)

    def test_valid_dependencies_pass(self):
        self.assertEqual(mi.validate_structure(
            manifest(dependencies={"Other.Kit": "1.2.0", "Third.Party": "2.0.1"})), [])

    def test_dependency_entries_are_validated_not_just_the_container(self):
        # Both halves of every entry: the key feeds by-id manifest resolution,
        # the value feeds major-lock version resolution. A malformed one has to
        # fail here, where the submitter sees it, not at install time on a user's
        # machine as an unresolvable mod with no clue which manifest is at fault.
        cases = {
            "id with no dot": manifest(dependencies={"nodot": "1.0.0"}),
            "id with traversal": manifest(dependencies={"../../evil": "1.0.0"}),
            "id with a path separator": manifest(dependencies={"Owner./evil": "1.0.0"}),
            "two-segment version": manifest(dependencies={"Other.Kit": "1.2"}),
            "non-numeric version": manifest(dependencies={"Other.Kit": "banana"}),
            "prerelease version": manifest(dependencies={"Other.Kit": "1.2.0-rc1"}),
            "non-string version": manifest(dependencies={"Other.Kit": 1}),
            "segment over int32": manifest(dependencies={"Other.Kit": "9999999999.0.0"}),
        }
        for label, m in cases.items():
            with self.subTest(label):
                self.assertTrue(mi.validate_structure(m), f"{label} should have failed")

    def test_self_dependency_rejected(self):
        errs = mi.validate_structure(manifest("Owner.Repo", dependencies={"Owner.Repo": "1.0.0"}))
        self.assertTrue(any("own id" in e for e in errs), errs)

    # parity_required: whether a client joining a server running this mod
    # must match its exact version.

    def test_parity_required_must_be_stated(self):
        """Mandatory, so publishing is a deliberate answer to 'must a joining
        client match this exactly?' rather than something inherited silently."""
        m = manifest()
        m.pop("parity_required", None)
        errs = mi.validate_structure(m)
        self.assertTrue(any("parity_required is required" in e for e in errs), errs)

    def test_both_boolean_values_accepted(self):
        for value in (True, False):
            self.assertEqual(mi.validate_structure(manifest(parity_required=value)), [], value)

    def test_non_boolean_parity_required_rejected(self):
        for bad in ("required", "false", 0, 1, None, []):
            errs = mi.validate_structure(manifest(parity_required=bad))
            self.assertTrue(any("parity_required" in e for e in errs), (bad, errs))

    def test_false_is_accepted_whatever_the_targets(self):
        """v2 dropped v1's 'false needs both sides' rule: the spec only says
        where the value matters, and a pc-only mod with false is valid (the
        reference fixtures carry one)."""
        for targets in (["pc"], ["server"], ["quest"], ["pc", "server"]):
            self.assertEqual(mi.validate_structure(
                manifest(targets=targets, parity_required=False)), [], targets)

    def test_reader_only_relaxes_on_a_real_false(self):
        """Fail closed: a missing key, a null, or a string all read as required,
        so nothing malformed can quietly drop a guarantee a server depends on."""
        self.assertTrue(mi.parity_required({}))
        for bad in ("false", "", None, 0, "optional"):
            self.assertTrue(mi.parity_required({"parity_required": bad}), bad)
        self.assertFalse(mi.parity_required({"parity_required": False}))

    def test_summary_still_defaults_a_missing_value_to_required(self):
        """summary_entry runs on already-validated manifests, so this can't
        normally happen - but the index must never emit a relaxed value it
        wasn't told to, so the reader stays fail-closed underneath."""
        m = manifest()
        m.pop("parity_required", None)
        self.assertIs(mi.summary_entry(m, ["1.0.0"])["parity_required"], True)

    def test_summary_carries_parity_through(self):
        entry = mi.summary_entry(manifest(parity_required=False), ["1.0.0"])
        self.assertIs(entry["parity_required"], False)

    # scenes: which of the normal/quest/tutorial scenes a mod works in.

    def test_scenes_are_optional(self):
        self.assertEqual(mi.validate_structure(manifest()), [])
        self.assertEqual(mi.validate_structure(manifest(scenes=None)), [])

    def test_a_subset_of_scenes_is_accepted(self):
        for scenes in (["normal"], ["quest", "tutorial"], ["tutorial"]):
            self.assertEqual(mi.validate_structure(manifest(scenes=scenes)), [], scenes)

    def test_malformed_scenes_rejected(self):
        """The launcher and TavernLib keep scene names they don't know, so a
        typo would otherwise ship and quietly match no server at all."""
        for bad in ([], "quest", ["Quest"], ["normal", "arena"], ["quest", "quest"], [1], {"quest": 1}):
            errs = mi.validate_structure(manifest(scenes=bad))
            self.assertTrue(any("scene" in e for e in errs), (bad, errs))

    def test_listing_every_scene_is_rejected_in_favour_of_leaving_it_out(self):
        errs = mi.validate_structure(manifest(scenes=["normal", "quest", "tutorial"]))
        self.assertTrue(any("leave it out" in e for e in errs), errs)

    def test_summary_carries_scenes_only_when_set(self):
        self.assertEqual(mi.summary_entry(manifest(scenes=["quest"]), ["1.0.0"])["scenes"], ["quest"])
        self.assertNotIn("scenes", mi.summary_entry(manifest(), ["1.0.0"]))

    def test_scenes_keep_their_place_in_a_rewritten_manifest(self):
        keys = list(mi.ordered_manifest(manifest(scenes=["quest"])).keys())
        self.assertEqual(keys[keys.index("parity_required") + 1], "scenes")


class ManifestVersion(unittest.TestCase):
    """v2 is the only schema this repo takes, and the move from v1 is spelled
    out rather than left to a pile of field-level errors."""

    def _v1(self):
        return {
            "manifest_version": 1, "id": "Owner.Repo", "name": "Repo",
            "version": "1.0.0", "author": "Owner", "description": "",
            "client_side": True, "server_side": False, "parity_required": True,
            "dependencies": {}, "library_dependencies": [],
            "download_url": "https://github.com/Owner/Repo/releases/download/v1.0.0/Repo.dll",
            "sha256": "a" * 64,
        }

    def test_v1_manifest_gets_one_message_saying_what_changed(self):
        errs = mi.validate_structure(self._v1())
        self.assertEqual(len(errs), 1, errs)
        for needle in ("manifest_version 2", "client_side", "artifacts", "targets",
                       "TEMPLATE.json"):
            self.assertIn(needle, errs[0])

    def test_leftover_top_level_fields_point_at_artifacts(self):
        old = {"client_side": True, "server_side": True,
               "download_url": "https://github.com/Owner/Repo/releases/download/v1/x.dll",
               "sha256": "a" * 64, "package": "dll", "library_dependencies": []}
        for key, value in old.items():
            with self.subTest(key):
                errs = mi.validate_structure(manifest(**{key: value}))
                self.assertEqual(len(errs), 1, errs)
                self.assertIn(f"top-level {key}", errs[0])
                self.assertIn("artifacts", errs[0])


class Artifacts(unittest.TestCase):
    """The per-build list that replaced client_side/server_side."""

    def _split(self):
        """Different pc and server builds, the shape a single artifact can't say."""
        return manifest(artifacts=[
            artifact(targets=["pc"], asset="Repo-pc.dll"),
            artifact(targets=["server"], asset="Repo-server.zip"),
        ])

    def test_shapes_that_pass(self):
        cases = {
            "one build for pc and server": manifest(),
            "split pc and server builds": self._split(),
            "pc only": manifest(targets=["pc"]),
            "server only": manifest(targets=["server"]),
            "quest only": manifest(targets=["quest"]),
            "all three across two builds": manifest(artifacts=[
                artifact(targets=["pc", "server"]),
                artifact(targets=["quest"], asset="Repo-quest.zip")]),
        }
        for label, m in cases.items():
            with self.subTest(label):
                self.assertEqual(mi.validate_structure(m), [])

    def test_artifacts_must_be_a_non_empty_list(self):
        for bad in ([], None, {}, "Repo.dll"):
            with self.subTest(bad=bad):
                m = manifest()
                m["artifacts"] = bad
                errs = mi.validate_structure(m)
                self.assertTrue(errors_about(errs, "artifacts must be a non-empty list"), errs)
        m = manifest()
        del m["artifacts"]
        self.assertTrue(errors_about(mi.validate_structure(m), "artifacts"))

    def test_an_artifact_must_be_an_object(self):
        errs = mi.validate_structure(manifest(artifacts=[artifact(), "Repo.dll"]))
        self.assertTrue(errors_about(errs, "artifacts[1] must be an object"), errs)

    def test_targets_must_be_a_non_empty_list(self):
        for bad in ([], None, "pc"):          # None: the key left out
            with self.subTest(bad=bad):
                errs = mi.validate_structure(with_artifact(manifest(), targets=bad))
                self.assertTrue(errors_about(errs, "artifacts[0].targets"), errs)

    def test_unknown_target_rejected(self):
        """Installers skip names they don't know, so a typo would ship a build
        nothing ever installs. This is where it gets caught."""
        for bad in ("android", "PC", "client", "", 1):
            with self.subTest(bad=bad):
                errs = mi.validate_structure(manifest(targets=["pc", bad]))
                self.assertTrue(errors_about(errs, "isn't a known target"), errs)

    def test_target_in_two_artifacts_rejected(self):
        m = manifest(artifacts=[
            artifact(targets=["pc", "server"]),
            artifact(targets=["server"], asset="Repo-server.zip")])
        errs = mi.validate_structure(m)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("artifacts[1].targets lists 'server'", errs[0])
        self.assertIn("artifacts[0] already lists", errs[0])

    def test_target_twice_in_one_artifact_rejected(self):
        errs = mi.validate_structure(manifest(targets=["pc", "pc"]))
        self.assertTrue(errors_about(errs, "it already lists"), errs)

    def test_ownership_is_checked_per_artifact(self):
        # The second build pointing at someone else's repo fails, and the error
        # names that build rather than the mod as a whole.
        m = with_artifact(self._split(), 1, download_url=(
            "https://github.com/someoneelse/thing/releases/download/v1/x.zip"))
        errs = mi.validate_structure(m)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("artifacts[1].download_url", errs[0])
        self.assertIn("Owner/Repo", errs[0])
        # not a release asset host at all
        m = with_artifact(manifest(), download_url="https://example.com/Owner/Repo/x.dll")
        self.assertTrue(errors_about(mi.validate_structure(m), "artifacts[0].download_url"))

    def test_library_is_not_ownership_checked(self):
        # a library may point anywhere https (third-party, unowned)
        self.assertEqual(mi.validate_structure(with_artifact(
            manifest(), library_dependencies=[library()])), [])

    def test_sha_is_checked_per_artifact(self):
        m = with_artifact(self._split(), 1, sha256="not-a-hash")
        errs = mi.validate_structure(m)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("artifacts[1].sha256", errs[0])

    def test_library_errors_name_their_artifact(self):
        m = with_artifact(self._split(), 1,
                          library_dependencies=[library(), library("Bad", sha="x")])
        errs = mi.validate_structure(m)
        self.assertEqual(errs, ["artifacts[1].library_dependencies[1].sha256 must be "
                                "64 lowercase hex characters"])

    def test_check_urls_false_keeps_every_shape_rule(self):
        """Only where files live is set aside, not what the manifest says."""
        local = "http://127.0.0.1:8765/files/Repo.dll"
        m = with_artifact(manifest(), download_url=local,
                          library_dependencies=[library(download_url=local)])
        self.assertTrue(mi.validate_structure(m))
        self.assertEqual(mi.validate_structure(m, check_urls=False), [])
        for broken in (with_artifact(m, targets=["android"]), with_artifact(m, sha256="x"),
                       manifest(artifacts=[])):
            self.assertTrue(mi.validate_structure(broken, check_urls=False))

    def test_manifest_targets_is_the_sorted_union(self):
        m = manifest(artifacts=[artifact(targets=["server"]),
                                artifact(targets=["quest", "pc"])])
        self.assertEqual(mi.manifest_targets(m), ["pc", "quest", "server"])
        self.assertEqual(mi.manifest_targets(manifest(targets=["server"])), ["server"])


class CanonicalOrder(unittest.TestCase):
    """Compiled manifests are written with keys in the spec's order, at every
    level, whatever order the submission used."""

    MANIFEST = ["manifest_version", "id", "name", "version", "author", "description",
                "screenshots", "parity_required", "scenes", "dependencies", "artifacts"]
    ARTIFACT = ["targets", "download_url", "sha256", "package", "library_dependencies"]
    LIBRARY = ["name", "download_url", "sha256", "filename"]

    def _scrambled(self):
        m = manifest(scenes=["quest"], screenshots=[], artifacts=[
            artifact(package="dll", library_dependencies=[library()])])
        m = dict(reversed(list(m.items())))
        a = m["artifacts"][0]
        m["artifacts"][0] = dict(reversed(list(a.items())))
        lib = m["artifacts"][0]["library_dependencies"][0]
        m["artifacts"][0]["library_dependencies"][0] = dict(reversed(list(lib.items())))
        return m

    def test_every_level_is_reordered(self):
        out = mi.ordered_manifest(self._scrambled())
        self.assertEqual(list(out), self.MANIFEST)
        self.assertEqual(list(out["artifacts"][0]), self.ARTIFACT)
        self.assertEqual(list(out["artifacts"][0]["library_dependencies"][0]), self.LIBRARY)

    def test_unknown_keys_are_kept_at_the_end(self):
        m = manifest(extra=1)
        m["artifacts"][0] = {"x_note": "hi", **m["artifacts"][0]}
        out = mi.ordered_manifest(m)
        self.assertEqual(list(out)[-1], "extra")
        self.assertEqual(list(out["artifacts"][0])[-1], "x_note")

    def test_summary_entry_order(self):
        entry = mi.summary_entry(manifest(scenes=["quest"]), ["1.0.0"])
        self.assertEqual(list(entry), ["name", "author", "description", "parity_required",
                                       "targets", "scenes", "versions"])


class InspectZipBundle(unittest.TestCase):
    """The package="zip" content checks, run on each downloaded archive."""

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
        errs = mi.inspect_zip_bundle(self._zip([
            ("Bundle.dll", b"MZ"), ("Content/data.bundle", b"assets")]))
        self.assertEqual(errs, [])

    def test_bad_zip_rejected(self):
        tmp = tempfile.NamedTemporaryFile(prefix="nz_", suffix=".zip", delete=False)
        tmp.write(b"not a zip"); tmp.close()
        self.addCleanup(lambda: os.remove(tmp.name))
        self.assertTrue(mi.inspect_zip_bundle(tmp.name))

    def test_no_dll_is_error(self):
        errs = mi.inspect_zip_bundle(self._zip([("readme.txt", b"hi")]))
        self.assertTrue(any("no .dll" in e for e in errs))

    def test_dll_only_in_subfolder_is_error(self):
        # MelonLoader would load it, but a zip whose only DLLs sit in subfolders
        # is almost always a packaging mistake, so it's a hard fail.
        errs = mi.inspect_zip_bundle(self._zip([("sub/Mod.dll", b"MZ")]))
        self.assertTrue(any("archive root" in e for e in errs))

    def test_traversal_and_absolute_and_symlink_rejected(self):
        self.assertTrue(mi.inspect_zip_bundle(self._zip([("../evil.dll", b"x")])))
        self.assertTrue(mi.inspect_zip_bundle(self._zip([("/abs.dll", b"x")])))
        self.assertTrue(mi.inspect_zip_bundle(
            self._zip([("link", b"/etc/passwd", 0o120777 << 16)])))

    def test_colon_entries_rejected(self):
        """A drive-relative name, and an NTFS alternate data stream: on the
        launcher's platform "Mod.dll:payload" writes a hidden stream hanging off
        Mod.dll rather than a file. Neither contains a separator or a "..", so
        the traversal checks above read both as ordinary relative names."""
        for name in ("C:evil.dll", "Mod.dll:payload"):
            errs = mi.inspect_zip_bundle(self._zip([("Mod.dll", b"MZ"), (name, b"x")]))
            self.assertTrue(any("colon" in e for e in errs), name)

    def test_too_many_entries_rejected(self):
        saved = mi.ZIP_MAX_ENTRIES
        mi.ZIP_MAX_ENTRIES = 3
        self.addCleanup(lambda: setattr(mi, "ZIP_MAX_ENTRIES", saved))
        errs = mi.inspect_zip_bundle(self._zip(
            [("m.dll", b"MZ")] + [(f"f{i}", b"x") for i in range(5)]))
        self.assertTrue(any("entries" in e for e in errs))

    def test_decompression_ratio_flagged(self):
        saved = mi.ZIP_RATIO_MIN_SIZE
        mi.ZIP_RATIO_MIN_SIZE = 1024
        self.addCleanup(lambda: setattr(mi, "ZIP_RATIO_MIN_SIZE", saved))
        # 2 MB of zeros compresses tiny -> huge ratio.
        errs = mi.inspect_zip_bundle(self._zip([
            ("m.dll", b"MZ"), ("big.bin", b"\0" * (2 * 1024 * 1024))]))
        self.assertTrue(any("ratio" in e for e in errs))


def _zip_bytes(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


class ZipMemberScanning(unittest.TestCase):
    """Only code-bearing members of a zip get VirusTotal-scanned, not the whole
    archive (a bulky mod is mostly inert assets)."""

    def _zip(self, entries):
        tmp = tempfile.NamedTemporaryFile(prefix="zm_", suffix=".zip", delete=False)
        tmp.write(_zip_bytes(entries)); tmp.close()
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


class _Validate(unittest.TestCase):
    """cmd_validate end to end, with the network stubbed: download_and_hash
    serves bytes from self.files by URL, and vt_scan records what it's asked."""

    CLEAN = {"malicious": 0, "suspicious": 0, "harmless": 1, "undetected": 60}

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="modindex_validate_")
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        self.files = {}           # url -> bytes
        self.scanned = []         # sha256 of every file handed to vt_scan
        self.stats = dict(self.CLEAN)

        def fake_download(url):
            data = self.files[url]
            fd, tmp = tempfile.mkstemp(prefix="modindex_test_")
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            return tmp, hashlib.sha256(data).hexdigest()

        def fake_vt(path, sha, api_key):
            self.scanned.append(sha)
            return self.stats

        for name, fake in (("download_and_hash", fake_download), ("vt_scan", fake_vt)):
            saved = getattr(mi, name)
            setattr(mi, name, fake)
            self.addCleanup(lambda n=name, s=saved: setattr(mi, n, s))
        saved_key = os.environ.get("VIRUSTOTAL_API_KEY")
        os.environ["VIRUSTOTAL_API_KEY"] = "KEY"
        self.addCleanup(lambda: os.environ.__setitem__("VIRUSTOTAL_API_KEY", saved_key)
                        if saved_key is not None else os.environ.pop("VIRUSTOTAL_API_KEY", None))

    def serve(self, url, data):
        self.files[url] = data
        return hashlib.sha256(data).hexdigest()

    def run_validate(self, *manifests, vt_max_malicious=None):
        paths = []
        for m in manifests:
            path = os.path.join(self.root, "submissions", m["id"], f'{m["version"]}.json')
            mi._write_json(path, m)
            paths.append(path)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mi.cmd_validate(argparse.Namespace(
                repo=self.root, paths=paths, vt_max_malicious=vt_max_malicious))
        return rc, out.getvalue()


class ValidatePerArtifact(_Validate):
    def _split(self, pc_bytes, server_bytes):
        pc = artifact(targets=["pc"], asset="Repo-pc.dll")
        server = artifact(targets=["server"], asset="Repo-server.zip")
        pc["sha256"] = self.serve(pc["download_url"], pc_bytes)
        server["sha256"] = self.serve(server["download_url"], server_bytes)
        return manifest(artifacts=[pc, server])

    def test_every_artifact_is_downloaded_and_verified(self):
        m = self._split(b"MZ pc", _zip_bytes([("Repo.dll", b"MZ server")]))
        rc, out = self.run_validate(m)
        self.assertEqual(rc, 0, out)
        self.assertIn("artifacts[1] (server): detected a zip bundle", out)

    def test_sha_mismatch_names_the_artifact(self):
        m = self._split(b"MZ pc", _zip_bytes([("Repo.dll", b"MZ server")]))
        m["artifacts"][1]["sha256"] = "c" * 64
        rc, out = self.run_validate(m)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  artifacts[1] (server): sha256 mismatch", out)

    def test_zip_rules_apply_per_artifact(self):
        m = self._split(b"MZ pc", _zip_bytes([("sub/Repo.dll", b"MZ server")]))
        rc, out = self.run_validate(m)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL  artifacts[1] (server): zip bundle has .dll files only in subfolders",
                      out)

    def test_v1_submission_fails_with_the_migration_message(self):
        rc, out = self.run_validate(ManifestVersion()._v1())
        self.assertEqual(rc, 1)
        self.assertIn("manifest_version 1 is the old format", out)


class VirusTotalDedupe(_Validate):
    """Each distinct file is scanned once per run, by sha256, wherever it
    appears: two artifacts, a library both builds pin, two manifests."""

    def test_same_bytes_across_artifacts_and_libraries_scanned_once(self):
        lib = b"MZ shared library"
        same = b"MZ identical build"
        pc = artifact(targets=["pc"], asset="Repo-pc.dll")
        server = artifact(targets=["server"], asset="Repo-server.dll")
        pc["sha256"] = self.serve(pc["download_url"], same)
        server["sha256"] = self.serve(server["download_url"], same)
        lib_sha = self.serve("https://example.com/downloads/ExampleLib.dll", lib)
        pc["library_dependencies"] = [library(sha=lib_sha)]
        server["library_dependencies"] = [library(sha=lib_sha)]
        rc, out = self.run_validate(manifest(artifacts=[pc, server]))
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(self.scanned), sorted({hashlib.sha256(same).hexdigest(),
                                                       hashlib.sha256(lib).hexdigest()}))
        self.assertEqual(out.count("already scanned"), 2, out)

    def test_zip_member_matching_a_library_is_scanned_once(self):
        lib = b"MZ shared library"
        art = artifact(targets=["pc", "server"], asset="Repo.zip")
        art["sha256"] = self.serve(art["download_url"], _zip_bytes(
            [("Repo.dll", b"MZ mod"), ("ExampleLib.dll", lib)]))
        lib_sha = self.serve("https://example.com/downloads/ExampleLib.dll", lib)
        art["library_dependencies"] = [library(sha=lib_sha)]
        rc, out = self.run_validate(manifest(artifacts=[art]))
        self.assertEqual(rc, 0, out)
        self.assertEqual(len(self.scanned), 2)          # Repo.dll + the library, once
        self.assertEqual(len(set(self.scanned)), 2)

    def test_a_flagged_file_fails_every_manifest_it_appears_in(self):
        self.stats = {"malicious": 3, "suspicious": 0, "harmless": 0, "undetected": 60}
        a = manifest("Owner.First")
        b = manifest("Owner.Second")
        for m in (a, b):
            m["artifacts"][0]["sha256"] = self.serve(m["artifacts"][0]["download_url"],
                                                     b"MZ same bytes")
        rc, out = self.run_validate(a, b, vt_max_malicious=0)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.scanned), 1)          # scanned once ...
        self.assertEqual(out.count("malicious=3 exceeds"), 2, out)   # ... gates both
        self.assertIn("2 manifest(s) checked, 2 failed.", out)

    def test_without_seen_every_call_scans(self):
        # The helpers stay usable one-off (seen=None), as before.
        tmp = tempfile.NamedTemporaryFile(delete=False); tmp.write(b"x"); tmp.close()
        self.addCleanup(lambda: os.remove(tmp.name))
        for _ in range(2):
            mi._vt_scan_path(tmp.name, "d" * 64, "KEY", None, "f", [], [])
        self.assertEqual(len(self.scanned), 2)


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
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        # Ingest sniffs each artifact's download to set `package`; stub that
        # network read (a .zip asset name sniffs as a zip).
        saved = mi._sniff_package_from_url
        mi._sniff_package_from_url = lambda url: "zip" if url.endswith(".zip") else "dll"
        self.addCleanup(lambda: setattr(mi, "_sniff_package_from_url", saved))

    def _submit(self, m):
        path = os.path.join(self.root, "submissions", m["id"], f'{m["version"]}.json')
        mi._write_json(path, m)

    def _build(self, ingest=True, prune=True):
        args = argparse.Namespace(repo=self.root, ingest=ingest, prune=prune)
        with contextlib.redirect_stdout(io.StringIO()):
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
        index = self._read("repository.json")
        self.assertEqual(index["index_version"], 2)
        entry = index["mods"]["Owner.Repo"]["1"]
        self.assertEqual(entry["versions"], ["1.0.0"])
        self.assertEqual(entry["name"], "Repo")
        self.assertEqual(entry["targets"], ["pc", "server"])
        for absent in ("stars", "downloads", "download_url", "sha256", "artifacts",
                       "dependencies", "library_dependencies", "version",
                       "client_side", "server_side"):
            self.assertNotIn(absent, entry)
        # the per-version manifest still has the full data
        full = self._read(base, "1.0.0.json")
        self.assertIn("download_url", full["artifacts"][0])
        self.assertIn("sha256", full["artifacts"][0])
        # submission consumed
        self.assertFalse(os.path.exists(os.path.join(self.root, "submissions", "Owner.Repo")))

    def test_ingest_sets_package_per_artifact_from_sniff(self):
        # Submitter provides no package; ingest sniffs each file and writes it.
        self._submit(manifest("Owner.Repo", "1.0.0", artifacts=[
            artifact(targets=["pc"], asset="Repo-pc.dll"),
            artifact(targets=["server"], asset="Repo-server.zip")]))
        self._build()
        arts = self._read("manifests", "Owner", "Repo", "1.0.0.json")["artifacts"]
        self.assertEqual([a["package"] for a in arts], ["dll", "zip"])

    def test_compiled_manifest_is_written_in_canonical_order(self):
        m = manifest("Owner.Repo", "1.0.0", scenes=["quest"], artifacts=[
            artifact(library_dependencies=[library()])])
        m = dict(reversed(list(m.items())))
        m["artifacts"][0] = dict(reversed(list(m["artifacts"][0].items())))
        lib = m["artifacts"][0]["library_dependencies"][0]
        m["artifacts"][0]["library_dependencies"][0] = dict(reversed(list(lib.items())))
        self._submit(m)
        self._build()
        for name in ("1.0.0.json", "latest.json", "latest.1.json"):
            out = self._read("manifests", "Owner", "Repo", name)
            self.assertEqual(list(out), [k for k in CanonicalOrder.MANIFEST if k in out], name)
            self.assertEqual(list(out["artifacts"][0]), CanonicalOrder.ARTIFACT, name)
            self.assertEqual(list(out["artifacts"][0]["library_dependencies"][0]),
                             CanonicalOrder.LIBRARY, name)

    def test_index_targets_come_from_the_highest_release_in_each_major(self):
        self._submit(manifest("Owner.Repo", "1.0.0", targets=["pc"]))
        self._submit(manifest("Owner.Repo", "1.1.0", artifacts=[
            artifact(version="1.1.0", targets=["server"], asset="Repo-server.dll"),
            artifact(version="1.1.0", targets=["pc"], asset="Repo-pc.dll")]))
        self._submit(manifest("Owner.Repo", "2.0.0", targets=["quest"]))
        self._build()
        mods = self._read("repository.json")["mods"]["Owner.Repo"]
        self.assertEqual(mods["1"]["targets"], ["pc", "server"])     # sorted union, 1.1.0
        self.assertEqual(mods["2"]["targets"], ["quest"])

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
                         {"index_version": 2, "mods": {}})

    def test_invalid_submission_blocks_build(self):
        self._submit(manifest("Owner.Repo", version="nope"))
        with self.assertRaises(mi.ValidationError):
            self._build()

    def test_v1_submission_blocks_build(self):
        self._submit(ManifestVersion()._v1())
        with self.assertRaises(mi.ValidationError) as cm:
            self._build()
        self.assertIn("manifest_version 1 is the old format", str(cm.exception))

    def test_compiled_manifest_without_package_blocks_build(self):
        # ingest always sets it, so one without it was hand-placed
        mi._write_json(os.path.join(self.root, "manifests", "Owner", "Repo", "1.0.0.json"),
                       manifest())
        with self.assertRaises(mi.ValidationError) as cm:
            self._build(ingest=False, prune=False)
        self.assertIn("no package", str(cm.exception))

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
        with contextlib.redirect_stdout(io.StringIO()):
            rc = mi.cmd_validate(argparse.Namespace(
                repo=self.root, paths=None, vt_max_malicious=None))
        self.assertEqual(rc, 0)

    def test_repo_template_is_a_v2_shape(self):
        """submissions/TEMPLATE.json is what submitters copy, so apart from its
        placeholder hash it has to be a valid v2 manifest."""
        path = os.path.join(os.path.dirname(HERE), "submissions", "TEMPLATE.json")
        with open(path, encoding="utf-8") as f:
            tmpl = json.load(f)
        errs = mi.validate_structure(tmpl)
        self.assertEqual(errs, ["artifacts[0].sha256 must be 64 lowercase hex characters"])
        self.assertEqual(tmpl["artifacts"][0]["targets"], ["pc", "server"])


class FillHashes(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="modindex_fill_")
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))
        # stub the network: hash is derived from the URL so we can assert it
        saved = mi._sha256_of_url
        mi._sha256_of_url = lambda url: hashlib.sha256(url.encode()).hexdigest()
        self.addCleanup(lambda: setattr(mi, "_sha256_of_url", saved))

    def _fill(self, m):
        path = os.path.join(self.root, "submissions", m["id"], f'{m["version"]}.json')
        mi._write_json(path, m)
        with contextlib.redirect_stdout(io.StringIO()):
            rc = mi.cmd_fill(argparse.Namespace(repo=self.root, paths=[path]))
        with open(path, encoding="utf-8") as f:
            return rc, json.load(f)

    def test_fill_writes_every_artifact_and_library_hash(self):
        m = manifest(artifacts=[
            artifact(targets=["pc"], asset="Repo-pc.dll", sha256="PLACEHOLDER",
                     library_dependencies=[library("A", sha="PLACEHOLDER")]),
            artifact(targets=["server"], asset="Repo-server.zip", sha256="PLACEHOLDER",
                     library_dependencies=[library("B", sha="PLACEHOLDER")])])
        rc, out = self._fill(m)
        self.assertEqual(rc, 0)
        want = lambda url: hashlib.sha256(url.encode()).hexdigest()
        for art in out["artifacts"]:
            self.assertEqual(art["sha256"], want(art["download_url"]))
            for lib in art["library_dependencies"]:
                self.assertEqual(lib["sha256"], want(lib["download_url"]))
        # a filled manifest then passes structural validation
        self.assertEqual(mi.validate_structure(out), [])

    def test_v1_file_is_refused_and_left_alone(self):
        rc, out = self._fill(ManifestVersion()._v1())
        self.assertEqual(rc, 1)
        self.assertEqual(out, ManifestVersion()._v1())


class DependencyTargets(unittest.TestCase):
    """A dependency has to have a build for a target the mod requiring it has.

    The distinction this class exists to pin down: 'the targets differ' is
    normal and allowed, 'the targets don't overlap at all' is a dead edge and
    rejected."""

    def _known(self, *manifests):
        known = {}
        for m in manifests:
            known.setdefault(m["id"], {})[m["version"]] = m
        return known

    def _check(self, m, *known):
        return mi.validate_dependency_targets(m, self._known(*known))

    # -- the shape that must keep working ------------------------------------

    def test_pc_and_server_mod_may_depend_on_pc_only_mod(self):
        """The motivating case for per-target resolution. A mod with pc and
        server builds whose pc build needs a pc-only support mod is normal, not
        an error: installers leave the dependency out on the server."""
        dep = manifest("Dev.UIKit", "1.0.0", targets=["pc"])
        mod = manifest("Dev.Dual", "1.0.0", dependencies={"Dev.UIKit": "1.0.0"})
        errs, warns = self._check(mod, dep)
        self.assertEqual(errs, [])
        self.assertEqual(len(warns), 1)
        self.assertIn("server build will load without it", warns[0])

    def test_pc_and_server_mod_may_depend_on_server_only_mod(self):
        dep = manifest("Dev.DbTools", "1.0.0", targets=["server"])
        mod = manifest("Dev.Dual", "1.0.0", dependencies={"Dev.DbTools": "1.0.0"})
        errs, warns = self._check(mod, dep)
        self.assertEqual(errs, [])
        self.assertIn("pc build", warns[0])

    def test_split_builds_count_as_their_union(self):
        # Two artifacts are one mod for this rule: pc + server, then quest.
        dep = manifest("Dev.UIKit", "1.0.0", targets=["pc"])
        mod = manifest("Dev.Split", "1.0.0", dependencies={"Dev.UIKit": "1.0.0"},
                       artifacts=[artifact("Dev", "Split", targets=["server"]),
                                  artifact("Dev", "Split", targets=["pc", "quest"])])
        errs, warns = self._check(mod, dep)
        self.assertEqual(errs, [])
        self.assertIn("quest+server builds will load without it", warns[0])

    def test_matching_targets_are_silent(self):
        dep = manifest("Dev.Shared", "1.0.0")
        mod = manifest("Dev.Mod", "1.0.0", dependencies={"Dev.Shared": "1.0.0"})
        self.assertEqual(self._check(mod, dep), ([], []))

    def test_a_dependency_with_more_targets_is_silent(self):
        dep = manifest("Dev.Shared", "1.0.0", targets=["pc", "server", "quest"])
        mod = manifest("Dev.Mod", "1.0.0", targets=["pc"],
                       dependencies={"Dev.Shared": "1.0.0"})
        self.assertEqual(self._check(mod, dep), ([], []))

    # -- the dead edges ------------------------------------------------------

    def test_no_shared_target_is_rejected(self):
        for mine, theirs in ((["server"], ["pc"]), (["pc"], ["server"]),
                             (["quest"], ["pc", "server"]), (["pc", "server"], ["quest"])):
            with self.subTest(mine=mine, theirs=theirs):
                dep = manifest("Dev.Kit", "1.0.0", targets=theirs)
                mod = manifest("Dev.Mod", "1.0.0", targets=mine,
                               dependencies={"Dev.Kit": "1.0.0"})
                errs, warns = self._check(mod, dep)
                self.assertEqual(len(errs), 1)
                self.assertIn("share no target", errs[0])

    # -- resolution ----------------------------------------------------------

    def test_checks_the_version_the_launcher_would_install(self):
        """Major-locked, highest >= the minimum - so a later, fixed release in
        the same major is what gets judged, not the one the author named."""
        bad = manifest("Dev.Kit", "1.0.0", targets=["pc"])
        good = manifest("Dev.Kit", "1.5.0")          # pc + server, and higher
        mod = manifest("Dev.Srv", "1.0.0", targets=["server"],
                       dependencies={"Dev.Kit": "1.0.0"})
        self.assertEqual(self._check(mod, bad, good), ([], []))

    def test_other_major_is_not_consulted(self):
        other = manifest("Dev.Kit", "2.0.0")         # fine, but wrong major
        mod = manifest("Dev.Srv", "1.0.0", targets=["server"],
                       dependencies={"Dev.Kit": "1.0.0"})
        errs, warns = self._check(mod, other)
        self.assertEqual(errs, [])                   # unresolvable, so unjudged
        self.assertIn("isn't in this repository", warns[0])

    def test_dependency_from_another_source_is_a_note_not_an_error(self):
        mod = manifest("Dev.Mod", "1.0.0", dependencies={"Someone.Else": "1.0.0"})
        errs, warns = self._check(mod)
        self.assertEqual(errs, [])
        self.assertIn("isn't in this repository", warns[0])

    def test_malformed_version_is_left_to_validate_structure(self):
        mod = manifest("Dev.Mod", "1.0.0", dependencies={"Dev.Kit": "not-a-version"})
        self.assertEqual(self._check(mod), ([], []))
        self.assertTrue(mi.validate_structure(mod))   # reported there instead


class BuildEnforcesDependencyTargets(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="modindex_targets_")
        self.addCleanup(lambda: shutil.rmtree(self.root, ignore_errors=True))

    def _write_manifest(self, m):
        author, repo = m["id"].split(".", 1)
        for a in m["artifacts"]:
            a.setdefault("package", "dll")          # as ingest would have set it
        mi._write_json(os.path.join(self.root, "manifests", author, repo,
                                    f'{m["version"]}.json'), m)

    def _build(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return mi.cmd_build(argparse.Namespace(repo=self.root, ingest=False, prune=False))

    def test_build_refuses_to_index_an_unsatisfiable_edge(self):
        self._write_manifest(manifest("Dev.UIKit", "1.0.0", targets=["pc"]))
        self._write_manifest(manifest("Dev.Srv", "1.0.0", targets=["server"],
                                      dependencies={"Dev.UIKit": "1.0.0"}))
        with self.assertRaises(mi.ValidationError) as cm:
            self._build()
        self.assertIn("Dev.Srv", str(cm.exception))
        self.assertFalse(os.path.isfile(os.path.join(self.root, "repository.json")))

    def test_build_allows_a_pc_and_server_mod_with_a_pc_only_dependency(self):
        self._write_manifest(manifest("Dev.UIKit", "1.0.0", targets=["pc"]))
        self._write_manifest(manifest("Dev.Dual", "1.0.0",
                                      dependencies={"Dev.UIKit": "1.0.0"}))
        self._build()
        self.assertTrue(os.path.isfile(os.path.join(self.root, "repository.json")))


class ReferenceFixtures(unittest.TestCase):
    """The three manifests and the index in tests/fixtures/v2/ were generated
    straight from docs/REPO_STRUCTURE.md by an independent script, for a local
    test repo (so their URLs are http:// on 127.0.0.1, which only the URL
    rules care about). They pin this tool to the same reading of the spec as
    the launcher's and TavernLib's implementations."""

    def _manifests(self):
        out = []
        for path in sorted(glob.glob(os.path.join(FIXTURES, "manifests", "*", "*", "*.json"))):
            with open(path, encoding="utf-8") as f:
                out.append(json.load(f))
        self.assertEqual(len(out), 3)
        return out

    def test_structure_passes_once_url_rules_are_set_aside(self):
        for m in self._manifests():
            with self.subTest(m["id"]):
                self.assertEqual(mi.validate_structure(m, check_urls=False), [])
                # and the URL rules are the ONLY thing they fail
                for e in mi.validate_structure(m):
                    self.assertIn("download_url must be an https:// URL", e)

    def test_written_in_canonical_order(self):
        for m in self._manifests():
            with self.subTest(m["id"]):
                self.assertEqual(json.dumps(mi.ordered_manifest(m)), json.dumps(m))

    def test_build_reproduces_the_reference_index(self):
        """Rehost each build on its own GitHub repo (a URL is the one thing the
        fixtures can't satisfy), compile, and compare to the reference index,
        key order included. Libraries go to an unowned https host, as allowed."""
        root = tempfile.mkdtemp(prefix="modindex_fixtures_")
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        for m in self._manifests():
            author, repo = mi.parse_id(m["id"])
            for a in m["artifacts"]:
                a["download_url"] = (f"https://github.com/{author}/{repo}/releases/download/"
                                     f"v{m['version']}/{a['download_url'].rsplit('/', 1)[1]}")
                for lib in a["library_dependencies"]:
                    lib["download_url"] = "https://example.com/" + lib["filename"]
            mi._write_json(os.path.join(root, "manifests", author, repo,
                                        f"{m['version']}.json"), m)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            mi.cmd_build(argparse.Namespace(repo=root, ingest=False, prune=False))
        # ExampleMod (pc+server, quest) depends on PcHelper (pc): a note, not an error
        self.assertIn("ExampleDev.PcHelper 1.0.0 has builds for pc only", out.getvalue())
        with open(os.path.join(root, "repository.json"), encoding="utf-8") as f:
            built = json.load(f)
        with open(os.path.join(FIXTURES, "repository.json"), encoding="utf-8") as f:
            reference = json.load(f)
        self.assertEqual(json.dumps(built, ensure_ascii=False),
                         json.dumps(reference, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
