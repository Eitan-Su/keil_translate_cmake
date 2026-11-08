import io
import json
import os
from pathlib import Path
import stat
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from sdk_sources import (BatchError, Workspace, digest, exclusive_lock, load_catalog,
                         member_path, prepare_sdk, snapshot, unpack)


class SdkSourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.workspace = Workspace(self.root)

    def catalog(self, source, **fields):
        path = self.root / "catalog.json"
        path.write_text(json.dumps({"schema_version": 1, "sdks": [
            {"id": "demo", "version": "1.0", "source": source, **fields}
        ]}), encoding="utf-8")
        return path

    def test_catalog_local_override_and_relative_root(self):
        path = self.catalog({"type": "local", "root": "sdk"})
        self.assertEqual(load_catalog(path, [], {})[0]["source"]["root"], str(self.root / "sdk"))
        override = str(self.root / "override")
        self.assertEqual(load_catalog(path, ["demo"], {"demo": override})[0]["source"]["root"], override)
        with self.assertRaisesRegex(BatchError, "Unknown SDK"):
            load_catalog(path, ["missing"], {})

    def test_invalid_catalog_and_unpinned_downloads(self):
        sources = [None, {"type": "local"}, {"type": "archive", "url": "http://example.invalid/sdk.zip"},
                   {"type": "archive", "url": "https://example.invalid/sdk.zip", "sha256": "x"},
                   {"type": "archive", "url": "https://example.invalid/sdk.zip", "layers": {}},
                   {"type": "local", "root": 12}]
        for source in sources:
            with self.subTest(source=source), self.assertRaises(BatchError):
                load_catalog(self.catalog(source), [], {})
        with self.assertRaises(BatchError):
            load_catalog(self.catalog({"type": "local", "root": "sdk"}, include="*.uvprojx"), [], {})

    def test_boundary_ownership_and_atomic_root_json(self):
        for path in (self.root, self.root.parent / (self.root.name + "-other"), self.root / ".git" / "config"):
            with self.subTest(path=path), self.assertRaises(BatchError):
                self.workspace.output(path)
        self.workspace.json(self.root / "test.json", {"value": 1})
        self.assertEqual(json.loads((self.root / "test.json").read_text()), {"value": 1})
        occupied = self.workspace.mkdir(self.root / "occupied")
        (occupied / "user.txt").write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(BatchError, "empty or owned"):
            self.workspace.claim(occupied, "results")
        self.assertEqual((occupied / "user.txt").read_text(), "keep")
        output = self.root / "results"
        self.workspace.claim(output, "results")
        self.workspace.claim(output, "results")
        with self.assertRaisesRegex(BatchError, "different batch"):
            self.workspace.claim(output, "cache")

    def test_link_output_is_rejected(self):
        with patch("sdk_sources.linked", side_effect=lambda p: p == self.root):
            with self.assertRaisesRegex(BatchError, "reparse point"):
                self.workspace.output(self.root / "generated")

    def test_lock_rejects_a_second_holder_and_releases(self):
        lock = self.root / "lock"
        with exclusive_lock(self.workspace, lock):
            with self.assertRaisesRegex(BatchError, "Another batch"):
                with exclusive_lock(self.workspace, lock):
                    pass
        with exclusive_lock(self.workspace, lock):
            pass

    def test_unsafe_archive_names(self):
        for name in ("../outside", "/outside", "C:\\outside", "dir/../../x", "CON.txt", "dir/file.", "dir/file:stream", "dir/*.c"):
            with self.subTest(name=name), self.assertRaises(BatchError):
                member_path(name)
        self.assertEqual(member_path("./sdk/main.c"), Path("sdk/main.c"))

    def zip(self, entries):
        path = self.root / "sdk.zip"
        with zipfile.ZipFile(path, "w") as archive:
            for name, content in entries:
                archive.writestr(name, content)
        return path

    def test_zip_preflight_traversal_duplicates_and_links(self):
        link = zipfile.ZipInfo("symlink")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        for entries in ([('ok.c', 'ok'), ('../outside', 'bad')], [('A.c', '1'), ('a.c', '2')], [(link, '../outside')]):
            archive = self.zip(entries)
            with self.subTest(entries=entries), self.assertRaises(BatchError):
                unpack(self.workspace, archive, self.root / "unpacked", "zip")
            self.assertFalse((self.root / "unpacked").exists())

    def test_tar_root_directory_and_regular_files(self):
        archive = self.root / "sdk.tar"
        with tarfile.open(archive, "w") as stream:
            root = tarfile.TarInfo(".")
            root.type = tarfile.DIRTYPE
            stream.addfile(root)
            file = tarfile.TarInfo("./sdk/main.c")
            file.size = 3
            stream.addfile(file, io.BytesIO(b"abc"))
        unpack(self.workspace, archive, self.root / "unpacked", "tar")
        self.assertEqual((self.root / "unpacked/sdk/main.c").read_bytes(), b"abc")

    def test_tar_hardlink_rejected_before_extraction(self):
        archive = self.root / "sdk.tar"
        with tarfile.open(archive, "w") as stream:
            link = tarfile.TarInfo("link")
            link.type, link.linkname = tarfile.LNKTYPE, "../outside"
            stream.addfile(link)
        with self.assertRaises(BatchError):
            unpack(self.workspace, archive, self.root / "unpacked", "tar")
        self.assertFalse((self.root / "unpacked").exists())

    def test_nested_cache_reuse_and_tampering(self):
        inner = self.zip([("SDK/main.c", "int main(void) {}")])
        inner_hash, inner_bytes = digest(inner), inner.read_bytes()
        outer = self.zip([("inner.zip", inner_bytes)])
        source = {"type": "archive", "url": "https://example.invalid/sdk.zip", "format": "zip", "sha256": digest(outer),
                  "layers": [{"member": "inner.zip", "format": "zip", "sha256": inner_hash}], "root": "SDK"}
        entry = load_catalog(self.catalog(source), [], {})[0]
        response = io.BytesIO(outer.read_bytes())
        response.url = source["url"]
        with patch("sdk_sources.urllib.request.urlopen", return_value=response) as download:
            sdk = prepare_sdk(self.workspace, self.root / "cache", entry)
            self.assertEqual(prepare_sdk(self.workspace, self.root / "cache", entry), sdk)
            self.assertEqual(download.call_count, 1)
        self.assertEqual(len(snapshot(sdk)), 1)
        (sdk / "main.c").write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(BatchError, "cache changed"):
            prepare_sdk(self.workspace, self.root / "cache", entry)

    def test_wrong_download_checksum_retains_evidence(self):
        source = {"type": "archive", "url": "https://example.invalid/sdk.zip", "format": "zip", "sha256": "0" * 64}
        entry = load_catalog(self.catalog(source), [], {})[0]
        response = io.BytesIO(b"wrong content")
        response.url = source["url"]
        with patch("sdk_sources.urllib.request.urlopen", return_value=response), self.assertRaisesRegex(BatchError, "checksum mismatch"):
            prepare_sdk(self.workspace, self.root / "cache", entry)
        self.assertEqual(len(list((self.root / "cache/downloads").glob("*.part"))), 1)
        self.assertFalse(list((self.root / "cache").rglob("ready.json")))


if __name__ == "__main__":
    unittest.main()
