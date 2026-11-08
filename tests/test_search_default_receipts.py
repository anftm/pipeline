"""A build-default receipt observes only the exact configured variable."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from scripts import search_default_receipts as defaults

G = "a" * 64


class DefaultReceiptTests(unittest.TestCase):
    def test_pages_requires_successful_exact_variable_response(self):
        response = Mock()
        response.json.return_value = {"name": defaults.VARIABLE, "value": G}
        session = Mock()
        session.get.return_value = response
        result = json.loads(defaults.observe_pages(G, session=session, timestamp="2026-01-01T00:00:00Z"))
        self.assertEqual(result["site"], "pages")
        self.assertEqual(result["generation"], G)
        response.raise_for_status.assert_called_once()
        self.assertIn("/repos/vomebook/search/actions/variables/SEARCH_DATA_GENERATION", session.get.call_args.args[0])
        response.json.return_value = {"name": defaults.VARIABLE, "value": "b" * 64}
        with self.assertRaisesRegex(ValueError, "does not match"):
            defaults.observe_pages(G, session=session)
        response.json.return_value = {}
        with self.assertRaisesRegex(ValueError, "invalid Pages"):
            defaults.observe_pages(G, session=session)

    def test_space_requires_explicit_matching_variable(self):
        api = Mock()
        api.get_space_variables.return_value = {defaults.VARIABLE: SimpleNamespace(value=G)}
        result = json.loads(defaults.observe_space(G, api=api, timestamp="2026-01-01T00:00:00Z"))
        self.assertEqual(result["site"], "space")
        self.assertEqual(result["generation"], G)
        api.get_space_variables.assert_called_once_with(defaults.SPACE)
        api.get_space_variables.return_value = {}
        with self.assertRaisesRegex(ValueError, "missing"):
            defaults.observe_space(G, api=api)
        api.get_space_variables.return_value = {defaults.VARIABLE: SimpleNamespace(value="b" * 64)}
        with self.assertRaisesRegex(ValueError, "does not match"):
            defaults.observe_space(G, api=api)

    def test_invalid_site_generation_or_timestamp_is_not_a_receipt(self):
        with self.assertRaises(ValueError):
            defaults.from_observation("bad", G, G)
        with self.assertRaises(ValueError):
            defaults.from_observation("pages", G, "bad")
        with self.assertRaises(ValueError):
            defaults.from_observation("space", G, G, timestamp="x" * 65)


if __name__ == "__main__":
    unittest.main()
