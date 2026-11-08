"""CCRD activation metadata is not a consumer or publication receipt."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from scripts import ccrd_activation_inspection as activation

G = 'ccrd-four-' + 'a' * 64
H, REV = 'b' * 64, 'c' * 40


def raw(**changes):
    return json.dumps({'version': 1, 'kind': 'ccrd-index-activation', 'bucket': 'vomebook/ccrd-index',
                       'path': f'generations/{G}/manifest.json', 'sha256': H, **changes}).encode()


def api_mock():
    api = Mock(); api.space_info.return_value = SimpleNamespace(sha=REV)
    api.get_space_runtime.return_value = SimpleNamespace(stage='RUNNING', raw={'sha': REV})
    return api


class CcrdActivationTests(unittest.TestCase):
    def test_exact_activation_and_running_revision_are_observed_twice(self):
        api = api_mock(); urls = []
        result = activation.observe(api, generation=G, manifest_sha256=H,
                                    get=lambda url, _limit: urls.append(url) or raw())
        self.assertEqual(api.space_info.call_count, 2); self.assertEqual(api.get_space_runtime.call_count, 2)
        self.assertTrue(result['runtime_observed_twice']); self.assertEqual(result['generation'], G)
        self.assertIn('/resolve/' + REV + '/data/index-release.json', urls[0])
        self.assertIn('not proven', result['scope'])

    def test_invalid_activation_or_unknown_runtime_never_counts(self):
        for changes in ({'kind': 'other'}, {'bucket': 'other'}, {'path': 'current.json'}, {'sha256': 'f' * 64},
                        {'version': True}, {'unexpected': 1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                activation.decode(raw(**changes), G, H)
        api = api_mock(); api.get_space_runtime.return_value.stage = 'RUNNING_BUILDING'
        with self.assertRaisesRegex(ValueError, 'not running'):
            activation.observe(api, generation=G, manifest_sha256=H, get=lambda *_: raw())

    def test_changed_space_or_runtime_is_not_accepted(self):
        api = api_mock(); api.space_info.side_effect = [SimpleNamespace(sha=REV), SimpleNamespace(sha='d' * 40)]
        with self.assertRaisesRegex(RuntimeError, 'repository changed'):
            activation.observe(api, generation=G, manifest_sha256=H, get=lambda *_: raw())
        api = api_mock(); api.get_space_runtime.side_effect = [SimpleNamespace(stage='RUNNING', raw={'sha': REV}),
                                                               SimpleNamespace(stage='BUILDING', raw={'sha': REV})]
        with self.assertRaisesRegex(RuntimeError, 'runtime changed'):
            activation.observe(api, generation=G, manifest_sha256=H, get=lambda *_: raw())


if __name__ == '__main__':
    unittest.main()
