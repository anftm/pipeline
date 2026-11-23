import unittest
from unittest.mock import Mock

from scripts.reader_ebook_rebuild_commit import commit


class EbookCommitTests(unittest.TestCase):
    def fixture(self):
        target = {'generation': '2' * 64}
        review = dict(kind='ebook-rebuild-review', remote_writes=0, reader_generation='1'*64,
                      target_pointer=target, changes=[{'key': 'one'}], target_index_sha256='index',
                      target_sidecar_sha256='routes', legacy={'target_sha256':'catalog'})
        receipt = {'complete': True, 'generation': target['generation'], 'consumers': {
            surface: {'generation': target['generation'], 'projection_verified': True} for surface in ('hf','pages')}}
        args = dict(review=review, guard=Mock(return_value=True),
                    stage=Mock(return_value=dict(members_verified=True, both_roots_protected=True)),
                    canonical=Mock(return_value={'sha256':'index'}),
                    legacy=Mock(return_value={'sha256':'routes','catalog_sha256':'catalog'}),
                    promote=Mock(return_value=dict(confirmed=True, pointer=target)),
                    publish_pages=Mock(return_value={'run_id':123}), verify=Mock(return_value=receipt))
        return args, target

    def test_requires_consumer_verification_after_exact_protected_write_order(self):
        args, target = self.fixture()
        self.assertEqual(commit(**args)['changed_rows'], 1)
        self.assertEqual([call.args[0] for call in args['guard'].call_args_list],
                         ['stage','canonical','legacy','v3','pages','verify'])
        args['verify'].assert_called_once_with(target['generation'], {'run_id':123})

    def test_unknown_or_bad_readback_never_replays_or_moves_to_next_phase(self):
        for stage, after in [('stage','canonical'), ('canonical','legacy'), ('legacy','promote'),
                             ('promote','publish_pages'), ('publish_pages','verify')]:
            with self.subTest(stage=stage):
                args, _ = self.fixture()
                args[stage].side_effect = TimeoutError('write response unknown')
                with self.assertRaises(TimeoutError):
                    commit(**args)
                args[stage].assert_called_once()
                args[after].assert_not_called()
        args, _ = self.fixture()
        args['canonical'].return_value = {'sha256':'different'}
        with self.assertRaisesRegex(RuntimeError, 'successor not observed'):
            commit(**args)
        args['legacy'].assert_not_called()
