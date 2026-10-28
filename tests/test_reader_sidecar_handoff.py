from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import build_reader_assets_v2_sidecar as sidecar
from scripts.reader_lifecycle import processing_record


class ReaderSidecarHandoffTests(unittest.TestCase):
    def test_office_producer_releases_writer_lock_before_unify_runs(self):
        root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
        source = (root / "reader-office-chapters.yml").read_text(encoding="utf-8")
        self.assertIn('group: reader-sidecar', source)
        self.assertIn('operation=unify -f apply=true', source)
        self.assertNotIn('gh run watch', source)

    def test_sidecar_handoff_scans_all_ebook_chapter_indexes_including_chm(self):
        indexes = sidecar.ebook_chapter_indexes("vomebook/reader-assets-v2")
        self.assertIn(("vomebook/reader-assets-v2", "chapters/ebook/chm/index.json"), indexes)
        self.assertEqual(len(indexes), 5)

    def test_stream_upload_roots_are_included_in_catalog_handoff(self):
        indexes = sidecar.stream_processing_indexes("vomebook/reader-assets-v2")
        self.assertEqual(len(indexes), 11)
        for path in ("documents/web/html/index.json", "documents/spreadsheet/csv/index.json", "media/audio/index.json"):
            self.assertIn(("vomebook/reader-assets-v2", path), indexes)

    def test_static_pdf_conversion_indexes_are_included_in_handoff(self):
        indexes=sidecar.static_pdf_processing_indexes('vomebook/reader-assets-v2')
        self.assertEqual(len(indexes),5)
        for extension in ('ppt','pptx','pps','wps','ps'):
            self.assertIn(('vomebook/reader-assets-v2',f'documents/pdf/{extension}/index.json'),indexes)

    def test_asx_audio_uses_converter_mode_not_video_category(self):
        entry = {"key": "repo\0book.asx", "mode": "audio", "object": "media/audio/asx/hash/audio.mp3"}
        def read(_fs, _bucket, path):
            if path in {"render", "ocr"}:
                return {"files": {}}
            if path == "media/video/index.json":
                return {"files": [entry]}
            if path in {"pages/image/index.json", "pages/image/index-v2.json"}:
                return {"files": []}
            return {"files": []}
        with patch.object(sidecar, "read_json", side_effect=read):
            result = sidecar.build(None, "vomebook/reader-assets-v2", "vomebook/pdf-pages-v2", "derived", "render", "ocr")
        self.assertEqual(result["f"][entry["key"]]["m"], "a")

    def test_chm_sidecar_supports_new_chapter_only_and_retains_legacy_fallback(self):
        bucket='vomebook/reader-assets-v2'
        root='chapters/ebook/chm/'+'a'*64+'/'+'b'*16
        fallback='native/ebook/chm/'+'a'*64+'/document.epub'
        old={'key':'repo\0old.chm','extension':'chm','manifest':root+'/chapter-manifest.json',
             'fallback':fallback,'object':fallback}
        new={'key':'repo\0new.chm','extension':'chm','manifest':root+'/chapter-manifest.json'}
        files={}
        sidecar.add_index(files,{'files':[old,new]},{'chm':'e'},bucket,chapters=True)
        self.assertEqual(files[old['key']]['f'],fallback)
        self.assertNotIn('f',files[new['key']])
        for entry in (old,new):
            self.assertEqual(files[entry['key']]['p'],entry['manifest'])
            self.assertEqual(files[entry['key']]['c'],entry['manifest'])

    def test_chm_reconversion_does_not_create_fallback_retirement_hint(self):
        from scripts import reader_lifecycle,reader_gc_retirement, pilot_epub_chapters
        bucket='vomebook/reader-assets-v2';fallback='native/ebook/chm/'+'a'*64+'/document.epub'
        old={'key':'repo\0book.chm','extension':'chm','manifest':'chapters/ebook/chm/'+'b'*64+'/'+'c'*16+'/chapter-manifest.json',
             'fallback':fallback,'object':fallback}
        new={'key':old['key'],'extension':'chm','manifest':'chapters/ebook/chm/'+'b'*64+'/'+'d'*16+'/chapter-manifest.json'}
        new=pilot_epub_chapters.preserve_existing_chm_fallback({'files':[old]},new)
        before={};after={}
        sidecar.add_index(before,{'files':[old]},{'chm':'e'},bucket,chapters=True)
        sidecar.add_index(after,{'files':[new]},{'chm':'e'},bucket,chapters=True)
        hint=reader_gc_retirement.record(reader_lifecycle.sidecar_references({'f':before}),
            reader_lifecycle.sidecar_references({'f':after}),'e'*32,'catalog-replacement')
        self.assertIsNotNone(hint)
        self.assertNotIn(fallback,[item['path'] for item in hint[1]['removed']])
        self.assertEqual(after[old['key']]['f'],fallback)

    def test_only_new_processing_roots_are_published_as_released(self):
        bucket = "vomebook/reader-assets-v2"
        root = "chapters/ebook/epub/" + "a" * 64 + "/" + "b" * 16
        path = "reader-index/processing/office-chapters-" + "c" * 32 + ".json"
        entry = {"key": "VoiceOfML/Test\0book.docx", "manifest": root + "/chapter-manifest.json",
                 "processing_roots": [path]}
        record = processing_record([{"bucket": bucket, "root": root}], "office-chapter-upload", {})
        values = {path: record}
        written = []
        count = sidecar.release_processing_roots(
            [{"files": [entry]}], "d" * 32, values.__getitem__,
            lambda name, value: (written.append(name), values.__setitem__(name, value)))
        self.assertEqual(count, 1)
        self.assertIn(path, written)
        receipt = next(name for name in written if name.startswith('reader-index/gc/retirements/'))
        self.assertEqual(values[receipt]['roots'], [{'bucket': bucket, 'root': root.rsplit('/', 1)[0]}])
        self.assertEqual(values[path]["status"], "released")
        self.assertEqual(values[path]["generation"], "d" * 32)

        written.clear()
        count = sidecar.release_processing_roots(
            [{"files": [entry]}], "e" * 32, values.__getitem__,
            lambda name, value: written.append(name))
        self.assertEqual(count, 0)
        self.assertEqual(written, [])

    def test_chapter_only_upload_handoff_cannot_touch_old_native_fallback(self):
        bucket='vomebook/reader-assets-v2'
        root='chapters/ebook/chm/'+'a'*64+'/'+'b'*16
        old='native/ebook/chm/'+'c'*64+'/document.epub'
        entry={'key':'VoiceOfML/Test\0book.chm','root':root,'manifest':root+'/chapter-manifest.json'}
        from scripts import reader_stream_index
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            result=reader_stream_index.publish_checkpoint(bucket,'chapters/ebook/chm/index.json',
                {'files':[{'key':'other','fallback':old}]},[entry],[],{},Path(directory),None,
                apply=False,kind='ebook-chapter-stream-index')
            current=next(x for x in result['files'] if x['key']==entry['key'])
            path=current['processing_roots'][0]
            record=processing_record([{'bucket':bucket,'root':root}],'reader-stream-upload',{})
            saved=[]
            sidecar.release_processing_roots([result],'d'*32,lambda _:record,lambda name,value:saved.append((name,value)))
            hints=[value for name,value in saved if name.startswith('reader-index/gc/retirements/')]
            self.assertEqual(len(hints),1)
            self.assertNotIn('native/ebook/',str(hints))
            self.assertEqual(next(x for x in result['files'] if x['key']=='other')['fallback'],old)

    def test_verified_text_roots_use_the_same_explicit_catalog_handoff(self):
        bucket = "vomebook/reader-assets-v2"
        root = "documents/text/txt/" + "a" * 64
        path = "reader-index/processing/text-" + "b" * 32 + ".json"
        entry = {"key": "VoiceOfML/Test\0book.txt", "object": root + "/document.txt",
                 "processing_roots": [path]}
        record = processing_record([{"bucket": bucket, "root": root}], "text-utf8-upload", {})
        written = []
        count = sidecar.release_processing_roots(
            [{"files": [entry]}], "c" * 32, lambda _: record,
            lambda name, value: written.append((name, value)))
        self.assertEqual(count, 1)
        self.assertEqual(next(value for name, value in written if name == path)['status'], 'released')


if __name__ == "__main__":
    unittest.main()
