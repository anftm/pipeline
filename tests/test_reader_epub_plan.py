"""Exact EPUB plans preserve old routes and reject unknown provenance."""
import copy
import unittest

from scripts import reader_epub_plan as subject
from scripts import reader_stream_index

REPO, OLD, NEW, SHA0, SHA1 = 'VoiceOfML/Test', 'a'*40, 'b'*40, '1'*64, '2'*64


def data():
    source = {'v': 2, 'rp': [REPO], 'fd': [[]], 'rc': [[0, 'book', 'epub', 0, 10, 0]]}
    key = REPO + '\0book.epub'
    entry = {'key': key, 'source_revision': OLD, 'source_sha256': SHA0,
             'manifest': 'chapters/ebook/epub/old/chapter-manifest.json'}
    summary = {'version': 1, 'kind': 'reader-format-backlog', 'remote_writes': 0,
               'publication_ready': False, 'external_writers_verified': False,
               'search_generation': '3'*64, 'reader_generation': '4'*64, 'threshold': 0, 'formats': [{
                'extension': 'epub', 'sources': 1, 'all_source_identities_checked': True, 'threshold_exceeded': True,
                'recorded_failure_current': 0, 'pending_shard_checkpoint': 0,
                'not_mapped': 0, 'source_content_changed': 1, 'definite_backlog': 1}]}
    details = {'version': 1, 'kind': 'reader-epub-content-backlog', 'remote_writes': 0,
               'publication_ready': False, 'search_generation': '3'*64, 'reader_generation': '4'*64,
               'rows': [{'key': key, 'source_revision': NEW, 'previous_revision': OLD,
                         'previous_source_sha256': SHA0, 'current_source_sha256': SHA1,
                         'state': 'content_changed', 'proof': 'current-lfs-sha256',
                         'recorded_failure': False}]}
    canonical = {'version': 1, 'kind': 'ebook-chapter-stream-index', 'files': [entry], 'failures': []}
    shards = {i: {} for i in range(8)}
    return summary, details, source, {REPO: NEW}, canonical, shards, reader_stream_index.shard_for(key, 8)


class EpubPlanTests(unittest.TestCase):
    def make(self, values):
        summary, details, corpus, revisions, canonical, shards, shard = values
        return subject.create(summary, details, corpus, revisions, canonical, shards, shard=shard, operation='f'*32)

    def test_changed_content_keeps_exact_old_route_and_does_not_write(self):
        values = data(); plan = self.make(values)
        self.assertEqual(plan['sources'][0]['reason'], 'content_changed')
        self.assertEqual(plan['sources'][0]['previous_manifest'], values[4]['files'][0]['manifest'])
        self.assertEqual(plan['sources'][0]['current_source_sha256'], SHA1)
        self.assertEqual(plan['remote_writes'], 0)
        self.assertFalse(plan['publication_ready'])
        self.assertFalse(plan['external_writers_verified'])

    def test_unknown_identity_bad_proof_or_changed_parent_stop_before_plan(self):
        for change in ('source', 'proof', 'count', 'uncertain', 'parent', 'reader', 'shards'):
            values = list(copy.deepcopy(data()))
            if change == 'source': values[1]['rows'][0]['source_revision'] = 'd'*40
            elif change == 'proof': values[1]['rows'][0]['current_source_sha256'] = SHA0
            elif change == 'count': values[0]['formats'][0]['source_content_changed'] = 2
            elif change == 'uncertain': values[0]['formats'][0]['all_source_identities_checked'] = False
            elif change == 'parent': values[4]['files'][0]['source_revision'] = 'e'*40
            elif change == 'reader': values[1]['reader_generation'] = '5'*64
            else: values[5].pop(0)
            with self.subTest(change=change), self.assertRaises(ValueError): self.make(values)

    def test_one_source_mode_is_exact_prefix_with_distinct_hash(self):
        from unittest.mock import patch
        original=self.make(data())
        with patch.object(subject,'inspect',return_value=original) as inspect:
            one=subject.inspect_one(hub='hub',api='api',fs='fs',client='client',shard=original['shard'],
                                    operation=original['operation'])
        self.assertEqual(one['sources'],original['sources'][:1])
        self.assertEqual(one['max_attempts'],1)
        self.assertNotEqual(one['plan_sha256'],original['plan_sha256'])
        inspect.assert_called_once()

    def test_current_failure_or_unpublished_shard_blocks(self):
        values = list(data()); key = values[1]['rows'][0]['key']
        values[5][values[6]] = {'files': [], 'failures': [{'key': key}]}
        with self.assertRaisesRegex(ValueError, 'prior failure'): self.make(values)
        values = list(data());values[0]['formats'][0]['pending_shard_checkpoint'] = 1
        with self.assertRaisesRegex(ValueError, 'unresolved'): self.make(values)

    def test_unmapped_lfs_identity_is_bound_before_source_selection(self):
        from unittest.mock import Mock
        values = list(copy.deepcopy(data()))
        summary, details, corpus, revisions, canonical, shards, shard = values
        summary['formats'][0].update(not_mapped=1, source_content_changed=0)
        old = details['rows'][0]
        details['rows'] = [{'key': old['key'], 'source_revision': NEW, 'state': 'not_mapped', 'recorded_failure': False}]
        canonical['files'] = []
        records = subject.backlog.source_records(corpus, revisions)
        api = Mock()
        api.get_paths_info.return_value = [{'path':'book.epub', 'lfs':{'sha256':SHA1}, 'blob_id':'c'*40}]
        bound = subject.bind_unmapped_sources(details, records, api)
        self.assertNotIn('proof', details['rows'][0])
        self.assertEqual(bound['rows'][0]['proof'], 'unmapped-lfs-sha256')
        self.assertEqual(bound['rows'][0]['current_source_sha256'], SHA1)
        values[1] = bound
        source = self.make(values)['sources'][0]
        self.assertEqual(source['proof'], 'unmapped-lfs-sha256')
        self.assertEqual(source['current_source_sha256'], SHA1)
        self.assertIsNone(source['previous_manifest'])
        api.get_paths_info.assert_called_once_with(REPO, ['book.epub'], repo_type='dataset', revision=NEW, expand=True)

    def test_unmapped_unknown_and_ordinary_git_remain_backlog_but_not_selected(self):
        from unittest.mock import Mock
        values = list(copy.deepcopy(data()))
        summary, details, corpus, revisions, canonical, shards, shard = values
        summary['formats'][0].update(not_mapped=1, source_content_changed=0)
        details['rows'] = [{'key': details['rows'][0]['key'], 'source_revision':NEW,
                            'state':'not_mapped', 'recorded_failure':False}]
        canonical['files'] = []
        records = subject.backlog.source_records(corpus, revisions)
        for metadata in ([], [{'path':'book.epub', 'blob_id':'c'*40, 'lfs':None}]):
            api = Mock();api.get_paths_info.return_value = metadata
            values[1] = subject.bind_unmapped_sources(details, records, api)
            with self.subTest(metadata=metadata), self.assertRaisesRegex(ValueError, 'no source with an admissible'):
                self.make(values)
        self.assertEqual(summary['formats'][0]['definite_backlog'], 1)
        api.get_paths_info.return_value = [{'path':'book.epub', 'lfs':{'sha256':'bad'}}]
        with self.assertRaisesRegex(ValueError, 'malformed source LFS'):
            subject.bind_unmapped_sources(details, records, api)

    def test_existing_unproven_legacy_unmapped_candidate_is_skipped_not_trusted(self):
        values = list(copy.deepcopy(data()))
        summary, details, _, _, canonical, _, _ = values
        summary['formats'][0].update(not_mapped=1, source_content_changed=0)
        details['rows'] = [{'key':details['rows'][0]['key'], 'source_revision':NEW,
                            'state':'not_mapped', 'recorded_failure':False}]
        canonical['files'] = []
        with self.assertRaisesRegex(ValueError, 'no source with an admissible'):
            self.make(values)
        details['rows'][0].update(proof='unmapped-lfs-sha256', current_source_sha256='wrong')
        with self.assertRaisesRegex(ValueError, 'invalid unmapped EPUB LFS proof'):
            self.make(values)

    def test_workflow_is_explicitly_readonly_and_bounded(self):
        import yaml
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / '.github/workflows/reader-epub-exact-plan.yml'
        flow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(set(flow['on']), {'workflow_dispatch'})
        self.assertEqual(flow['permissions'], {'contents': 'read'})
        self.assertEqual(flow['concurrency']['queue'], 'max')
        commands = '\n'.join(x.get('run', '') for x in flow['jobs']['plan']['steps'])
        self.assertIn('scripts/reader_epub_plan.py --shard', commands)
        for forbidden in ('--apply', 'git push', 'workflow run', 'delete'):
            self.assertNotIn(forbidden, commands)

    def test_ordinary_git_blob_change_requires_two_different_full_ids(self):
        values = list(data());row = values[1]['rows'][0]
        row.update(proof='exact-revision-git-blobs', previous_git_blob='7'*40, current_git_blob='8'*40)
        row.pop('current_source_sha256')
        self.assertEqual(self.make(values)['sources'][0]['current_git_blob'], '8'*40)
        row['current_git_blob'] = '7'*40
        with self.assertRaisesRegex(ValueError, 'Git blob'): self.make(values)


if __name__ == '__main__': unittest.main()
