from __future__ import annotations

import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import ccrd_source_override as override  # noqa: E402


def write_gz(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(value, ensure_ascii=False).encode("utf-8"), mtime=0))


def read_gz(path: Path):
    return json.loads(gzip.decompress(path.read_bytes()))


class CCRDSourceOverrideTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for relative in ("CCRD/0.txt", "CW/1/old.txt", "CW/1/legacy-source.txt",
                         "CW/2/0000/a.txt", "CW/2/191X/b.txt", "CW/2/S新编/drop.txt"):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative, encoding="utf-8")
        path = self.root / override.CORPUS_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(override.CORPUS_PATH), encoding="utf-8")
        legacy = override.LEGACY_CORPUS_PATH.as_posix()
        search = {
            "version": 1,
            "sources": [{"slug": "CCRD", "name": "CCRD", "count": 1},
                        {"slug": "CW", "name": "CW", "count": 5}],
            "records": [
                {"doc_id": "CCRD:0", "source": "CCRD", "source_name": "CCRD",
                 "storage_root": "CCRD", "storage_rel_path": "0.txt",
                 "display_dirs": [], "display_name": "0", "display_rel_path": "0.txt",
                 "size": 8, "extension": "txt", "has_preview": True, "has_fulltext": True},
                {"doc_id": "CW:0", "source": "CW", "source_name": "CW",
                 "storage_root": "CW", "storage_rel_path": legacy.removeprefix("CW/"),
                 "display_dirs": ["1"], "display_name": "legacy-source",
                 "display_rel_path": legacy.removeprefix("CW/"),
                 "size": 13, "extension": "txt", "has_preview": True, "has_fulltext": True},
                {"doc_id": "CW:1", "source": "CW", "source_name": "CW",
                 "storage_root": "CW", "storage_rel_path": "1/old.txt",
                 "display_dirs": ["1"], "display_name": "old", "display_rel_path": "1/old.txt",
                 "size": 3, "extension": "txt", "has_preview": True, "has_fulltext": True},
                {"doc_id": "CW:2", "source": "CW", "source_name": "CW",
                 "storage_root": "CW", "storage_rel_path": "2/0000/a.txt",
                 "display_dirs": ["2", "0000"], "display_name": "a", "display_rel_path": "2/0000/a.txt",
                 "size": 11, "extension": "txt", "has_preview": True, "has_fulltext": True},
                {"doc_id": "CW:3", "source": "CW", "source_name": "CW",
                 "storage_root": "CW", "storage_rel_path": "2/191X/b.txt",
                 "display_dirs": ["2", "191X"], "display_name": "b", "display_rel_path": "2/191X/b.txt",
                 "size": 12, "extension": "txt", "has_preview": True, "has_fulltext": True},
                {"doc_id": "CW:4", "source": "CW", "source_name": "CW",
                 "storage_root": "CW", "storage_rel_path": "2/S新编/drop.txt",
                 "display_dirs": ["2", "S新编"], "display_name": "drop",
                 "display_rel_path": "2/S新编/drop.txt",
                 "size": 4, "extension": "txt", "has_preview": True, "has_fulltext": True},
            ],
        }
        write_gz(self.root / "data/search_data.json.gz", search)
        write_gz(self.root / "data/folder_tree.json.gz", {"CCRD": [], "CW": []})
        write_gz(self.root / "data/folder_browser.json.gz", {"CCRD": {}, "CW": {}})
        write_gz(self.root / "data/sources.json.gz", search["sources"])

    def test_migrates_layout_and_metadata(self):
        counts = override.migrate_corpus(self.root)
        self.assertEqual(counts, {"CCRD": 1, "CW": 3})
        self.assertFalse((self.root / "CW/1").exists())
        self.assertFalse((self.root / "CW/2").exists())
        self.assertTrue((self.root / "CW/0000/a.txt").is_file())
        self.assertTrue((self.root / "CW/191X/b.txt").is_file())
        self.assertTrue((self.root / override.CORPUS_PATH).is_file())
        payload = read_gz(self.root / "data/search_data.json.gz")
        cw = [record for record in payload["records"] if record["source"] == "CW"]
        self.assertEqual([record["doc_id"] for record in cw], ["CW:0", "CW:2", "CW:3"])
        self.assertEqual(cw[0]["display_rel_path"], override.CORPUS_PATH.name)
        self.assertEqual(cw[0]["display_dirs"], [])
        self.assertEqual(cw[1]["display_dirs"], ["0000"])
        self.assertEqual(cw[2]["display_dirs"], ["191X"])
        tree = read_gz(self.root / "data/folder_tree.json.gz")
        self.assertEqual([child["name"] for child in tree["CW"][0]["children"]], ["0000", "191X"])
        browser = read_gz(self.root / "data/folder_browser.json.gz")
        self.assertEqual([item["name"] for item in browser["CW"][""]["files"]],
                         [override.CORPUS_PATH.stem])
        self.assertEqual([item["name"] for item in browser["CW"][""]["folders"]], ["0000", "191X"])

    def test_refuses_to_delete_cw_one_without_new_source(self):
        (self.root / override.CORPUS_PATH).unlink()
        with self.assertRaisesRegex(RuntimeError, "new source"):
            override.migrate_corpus(self.root)

    def test_migrates_space_payloads(self):
        counts = override.migrate_space(self.root)
        self.assertEqual(counts["CW"], 3)
        initial = json.loads((self.root / "data/initial/sources/CW.json").read_text(encoding="utf-8"))
        self.assertEqual(initial["total"], 3)
        self.assertEqual(initial["results"][0]["DisplayPath"], override.CORPUS_PATH.name)
        sidebar = json.loads((self.root / "data/sidebar/sources/CW.json").read_text(encoding="utf-8"))
        self.assertEqual([item["name"] for item in sidebar["folders"]], ["0000", "191X"])


if __name__ == "__main__":
    unittest.main()
