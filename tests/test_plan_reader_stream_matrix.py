import io
import unittest
from unittest.mock import Mock

from scripts.plan_reader_stream_matrix import read_json


class ReaderStreamMatrixIndexTests(unittest.TestCase):
    def test_only_a_missing_index_is_treated_as_empty(self):
        fs = Mock()
        fs.open.side_effect = FileNotFoundError("missing index")
        self.assertEqual(read_json(fs, "bucket", "chapters/ebook/epub/index.json"), {})

        for error in (PermissionError("denied"), OSError("network read failed")):
            fs = Mock()
            fs.open.side_effect = error
            with self.assertRaises(type(error)):
                read_json(fs, "bucket", "chapters/ebook/epub/index.json")

    def test_invalid_indexes_fail_closed(self):
        for raw in (b"[]", b"not-json"):
            fs = Mock()
            fs.open.return_value = io.BytesIO(raw)
            with self.assertRaises(ValueError):
                read_json(fs, "bucket", "chapters/ebook/epub/index.json")


if __name__ == "__main__":
    unittest.main()
