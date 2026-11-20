"""The same generated-catalogue flow publishes all twelve Search sources."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import fetch_and_parse as fetch, search_source_registry as sources, search_source_watch as watch, update_dirs
from scripts import search_generation, reader_format_backlog

REV='a'*40
OLD={k:REV for k in sources.PREVIOUS_SOURCES}
NEW={**OLD, **{k:'b'*40 for k in sources.ADDED_SOURCES}}


class SourceAdditionsTests(unittest.TestCase):
    def test_accepts_only_exact_ten_or_twelve_published_heads(self):
        for value in (OLD,NEW):
            d=watch.inspect(observe=lambda: {'generation':'1'*64,'source_revisions':value}, revision=lambda repo,_:NEW[repo],token='')
            self.assertEqual(d['changed'], [] if value==NEW else sorted(sources.ADDED_SOURCES))
        for value in ({**OLD,sources.ADDED_SOURCES[0]:REV},{**OLD,'VoiceOfML/Unknown':REV},
                      dict(list(OLD.items())[:-1]),{**OLD,next(iter(OLD)):'x'*40}):
            with self.assertRaisesRegex(ValueError,'revision set'):
                watch.inspect(observe=lambda:{'source_revisions':value},token='')

    def test_every_source_uses_the_same_directory_update_and_readback(self):
        with TemporaryDirectory() as d:
            p=Path(d)/'revisions.json'
            with patch.dict(os.environ,{'DIRECTORY_REVISIONS_FILE':str(p)},clear=True), \
                    patch.object(update_dirs,'HF_TOKEN','test-only'), \
                    patch.object(update_dirs,'process_repo',side_effect=lambda repo:NEW[repo.removeprefix('datasets/')]) as update:
                self.assertEqual(update_dirs.main(),0)
            self.assertEqual(json.loads(p.read_text()),NEW)
            self.assertEqual([a.args[0] for a in update.call_args_list],['datasets/'+r for r in sources.SOURCES])

    def test_old_parent_builds_twelve_pinned_catalogues_and_reader_keys(self):
        with TemporaryDirectory() as d:
            out=Path(d); baseline=out/'baseline.json'; revisions=out/'source-revisions.json'
            baseline.write_text(json.dumps(OLD)); revisions.write_text(json.dumps(NEW))
            def catalogue(url, _):
                for repo,rev in NEW.items():
                    if f'/datasets/{repo}/resolve/{rev}/' in url:
                        return './资料/扫描.PDF\n'
                raise AssertionError('unrecognized source pin')
            with patch.dict(os.environ,{'SEARCH_BASELINE_FILE':str(baseline),'DIRECTORY_REVISIONS_FILE':str(revisions)},clear=True), \
                 patch.object(fetch,'STATE_FILE',out/'state.json'), patch.object(fetch,'OUTPUT_DIR',out), \
                 patch.object(fetch,'OUTPUT_FILE',out/'search.json'), patch.object(fetch,'FOLDER_TREE_FILE',out/'tree.json'), \
                 patch.object(fetch,'FOLDER_BROWSER_FILE',out/'browser.json'), patch.object(fetch.time,'sleep'), \
                 patch.object(fetch,'get_repo_sha',side_effect=lambda repo,_:NEW[repo]), \
                 patch.object(fetch,'tree_records') as trees, \
                 patch.object(fetch,'http_get_text',side_effect=catalogue) as text, \
                 patch.object(fetch,'batch_get_sizes',return_value={'资料/扫描.PDF':456}):
                self.assertEqual(fetch.main(),0)
            trees.assert_not_called(); self.assertEqual(text.call_count,12)
            self.assertEqual(json.loads(baseline.read_text()),OLD)
            compact=json.loads((out/'search.json').read_text());self.assertEqual(set(compact['rp']),set(NEW))
            decoded=reader_format_backlog.source_records(compact,NEW)
            for repo in sources.ADDED_SOURCES:
                x=decoded[repo+'\0资料/扫描.PDF'];self.assertEqual((x['extension'],x['bytes'],x['revision']),('pdf',456,NEW[repo]))
            stage=search_generation.build_generation(out/'search.json',out/'tree.json',out/'browser.json',revisions,out/'stage')
            self.assertEqual(stage['source_revisions'],NEW); self.assertEqual(stage['record_count'],12)
