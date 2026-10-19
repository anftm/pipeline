"""The pinned fulltext builder must produce reproducible bytes for one corpus."""
import sqlite3
import tempfile
import unicodedata
import unittest
from pathlib import Path

from scripts import build_ccrd_ocr_release as builder, ccrd_ocr_release as contract


def make_database(path: Path, order: list[bytes], texts: dict[int, str]) -> None:
    """Mimic the pinned builder schema, inserting postings in set order."""
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("CREATE TABLE documents (doc_id TEXT PRIMARY KEY, summary TEXT NOT NULL)")
        connection.execute("CREATE TABLE postings (token_hash BLOB PRIMARY KEY, docs BLOB NOT NULL, doc_count INTEGER NOT NULL)")
        connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO metadata (key, value) VALUES ('tokenizer_version', ?)", (contract.TOKENIZER,))
        connection.execute("CREATE VIRTUAL TABLE content_fts USING fts5(content, tokenize='trigram', detail='full')")
        connection.execute(
            "CREATE TABLE snippet_anchors (token_hash BLOB NOT NULL, doc_number INTEGER NOT NULL, "
            "char_offset INTEGER NOT NULL, PRIMARY KEY (token_hash, doc_number)) WITHOUT ROWID"
        )
        for number, text in texts.items():
            doc_id = f"OCR:{number}"
            content = unicodedata.normalize("NFKC", text).casefold().replace("\0", " ")
            connection.execute("INSERT INTO documents (doc_id, summary) VALUES (?, ?)", (doc_id, text[:20]))
            connection.execute("INSERT INTO content_fts (rowid, content) VALUES (?, ?)", (number, content))
            connection.execute("INSERT INTO snippet_anchors (token_hash, doc_number, char_offset) VALUES (?, ?, ?)",
                               (number.to_bytes(16, "big"), number, 0))
        connection.executemany(
            "INSERT INTO postings (token_hash, docs, doc_count) VALUES (?, ?, ?)",
            [(token, b"\x01", 1) for token in order],
        )
        connection.commit()


class BuildCcrdOcrReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_postings_normalization_makes_identical_content_byte_identical(self):
        # The pinned builder inserts postings in Python set/dict order, which
        # changes with PYTHONHASHSEED and breaks resuming an interrupted upload.
        first, second = self.root / "first.sqlite3", self.root / "second.sqlite3"
        tokens = [bytes([number]) for number in (3, 1, 4, 2, 5)]
        texts = {1: "手机原始全文", 2: "第二本书"}
        make_database(first, tokens, texts)
        make_database(second, list(reversed(tokens)), texts)
        self.assertNotEqual(first.read_bytes(), second.read_bytes())
        builder.normalize_database(first)
        builder.normalize_database(second)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        with sqlite3.connect(first) as connection:
            rows = connection.execute("SELECT token_hash FROM postings ORDER BY token_hash").fetchall()
        self.assertEqual([row[0] for row in rows], [bytes([number]) for number in range(1, 6)])

    def test_normalization_keeps_the_semantics_the_consumer_validates(self):
        path = self.root / "OCR.sqlite3"
        texts = {1: "手机原始全文", 2: "手。机末页 ABC"}
        make_database(path, [b"\x03", b"\x01", b"\x02"], texts)
        for number, body in texts.items():
            normalized = unicodedata.normalize("NFKC", body).casefold().replace("\0", " ")
            (self.root / f"{number}.txt").write_text(normalized, encoding="utf-8")
        records = [{"doc_id": f"OCR:{number}", "storage_rel_path": f"{number}.txt"} for number in texts]
        builder.normalize_database(path)
        contract.validate_database(path, records, self.root)


if __name__ == "__main__":
    unittest.main()
