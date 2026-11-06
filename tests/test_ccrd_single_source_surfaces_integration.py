"""Combine the in-memory plan, index and caches using synthetic bytes only."""

from copy import deepcopy
import gzip
import json
import unittest

from scripts import ccrd_single_source_cw_shadow as index
from scripts import ccrd_single_source_plan as planner
from scripts import ccrd_single_source_surfaces as surface
from tests import test_ccrd_single_source_plan as plan_fixtures
from tests import test_ccrd_single_source_surfaces as surface_fixtures


class SingleSourceIntegrationTests(unittest.TestCase):
    def setUp(self):
        # Reuse fixture builders, not inherited test methods.
        self.plan_fixture = plan_fixtures.SingleSourcePlanTests('test_identical_txt_is_noop_even_if_input_gzip_is_not_canonical')
        self.plan_fixture.setUp()
        self.surface_fixture = surface_fixtures.SingleSourceSurfaceTests('test_unchanged_txt_keeps_all_original_bytes')
        self.surface_fixture.setUp()

    def test_plan_index_and_cached_search_agree_without_publication(self):
        fixture = self.plan_fixture
        args = fixture.arguments()
        plan = planner.plan_update(**args)
        documents = {'CW:0': fixture.current.decode(), 'CW:1': 'untouched 其他内容'}
        initial_index = index.build(documents)
        next_index = index.replace_target(initial_index, documents['CW:0'], fixture.new.decode(),
                                          expected_ids=set(documents))
        self.assertEqual(next_index, index.build({**documents, 'CW:0': fixture.new.decode()}))
        self.assertEqual(next_index['documents']['CW:1'], initial_index['documents']['CW:1'])

        cache_data = deepcopy(self.surface_fixture.data)
        target = cache_data[self.surface_fixture.initial]['results'][0]
        target['Size'] = len(fixture.current)
        target['snippet'] = fixture.current.decode()
        cache_data[self.surface_fixture.sidebar]['files'][0]['size'] = len(fixture.current)
        cache_data[self.surface_fixture.folder]['CW']['']['files'][0]['size'] = len(fixture.current)
        specs, payloads = self.surface_fixture.inventory(cache_data)
        result = surface.update_payloads(specs, payloads, fixture.current, fixture.new)
        initial = json.loads(result['payloads'][self.surface_fixture.initial])['results'][0]
        sidebar = json.loads(result['payloads'][self.surface_fixture.sidebar])['files'][0]
        folder = json.loads(gzip.decompress(result['payloads'][self.surface_fixture.folder]))['CW']['']['files'][0]
        self.assertEqual(initial['Size'], plan['next_txt_bytes'])
        self.assertEqual(sidebar['size'], plan['next_txt_bytes'])
        self.assertEqual(folder['size'], plan['next_txt_bytes'])
        self.assertEqual(initial['snippet'], next_index['documents']['CW:0'])
        self.assertTrue(all(value['publication_ready'] is False for value in (plan, next_index, result)))
        self.assertTrue(all(value['remote_writes'] == 0 for value in (plan, next_index, result)))

    def test_cache_rejects_target_changed_after_plan_was_captured(self):
        fixture = self.surface_fixture
        specs, payloads = fixture.inventory()
        result = surface.update_payloads(specs, payloads, fixture.old, fixture.new)
        changed = dict(payloads)
        changed[fixture.initial] = result['payloads'][fixture.initial]
        with self.assertRaisesRegex(ValueError, 'differ from the inventoried snapshot'):
            surface.update_payloads(specs, changed, fixture.old, fixture.new)


if __name__ == '__main__':
    unittest.main()
