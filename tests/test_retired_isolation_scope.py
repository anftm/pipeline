"""Retired OCR routing is not an admission or restart mechanism."""
import unittest
from pathlib import Path
from unittest.mock import Mock

from scripts import (auto_ccrd_ocr, pin_ccrd_newspaper_consumer, publish_ccrd_newspapers,
                     pdf_worker_dispatch, pdf_worker_lanes, reader_producer_scope, reader_gc_fence)


class RetiredIsolationScopeTests(unittest.TestCase):
    def test_scope_has_exact_historical_repositories_but_no_matrix(self):
        value=reader_producer_scope.load_config()
        self.assertEqual(value['kind'],'reader-producer-isolation-scope')
        self.assertEqual(value['status'],'isolation-only')
        self.assertEqual(reader_gc_fence.producer_repositories(),sorted(value['repositories']))
        self.assertEqual(len(value['repositories']),8)
        self.assertFalse({'native_lanes','converted_lane','legacy_configs'} & set(value))
        self.assertFalse((Path(__file__).resolve().parents[1]/'pdf-worker-lanes.json').exists())
        with self.assertRaisesRegex(RuntimeError,'routing has been retired'):
            pdf_worker_lanes.load_config()

    def test_even_supplied_legacy_routing_cannot_dispatch(self):
        api=Mock(side_effect=AssertionError('no API request allowed'))
        with self.assertRaisesRegex(RuntimeError,'dispatch is retired'):
            pdf_worker_dispatch.watchdog({},apply=True,api=api)
        with self.assertRaisesRegex(RuntimeError,'dispatch is retired'):
            pdf_worker_dispatch.handoff({},'a/b',1,2,apply=True,api=api)
        api.assert_not_called()

    def test_retired_ccrd_cli_entrypoints_cannot_use_external_pins(self):
        for module in (auto_ccrd_ocr, pin_ccrd_newspaper_consumer, publish_ccrd_newspapers):
            with self.subTest(module=module.__name__), self.assertRaisesRegex(RuntimeError,'is retired|are retired'):
                module.main()


if __name__ == '__main__':
    unittest.main()
