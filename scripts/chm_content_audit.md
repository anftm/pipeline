# CHM content audit and recovery

This is explicit corpus work, outside the fast default test tier. It downloads
originals and Reader Assets and scans every chapter. Do not use conversion status
or a non-empty title/TOC as evidence of complete content.

`audit_chm_content.py` accepts a pinned snapshot containing `manifest.json`,
`production-records.json`, `jobs.json` (entries plus all source keys sharing each
asset), and `books/<source_sha256>/book.chm` plus `book.epub`. The latter cache name
also stores an HTML artifact; the manifest's `reader_mode` defines its real format.
Both downloaded byte streams must match the manifest SHA-256 digests. Retain the
Reader Assets revision and the production search generation with the snapshot.

```sh
python3 -B scripts/audit_chm_content.py \
  --snapshot /tmp/opencode/chm-content \
  --reader-root /sdcard/Download/压书/huggingface-Search
```

The browser audit compares full whitespace-normalized EPUB spine text, including
punctuation and ordering, against Reader normalization. Source comparison reports
unmatched pages/fragments; it is a review queue, not a count of lost body text.
Navigation, software advertisements, duplicate TXT exports, damaged encodings and
real missing chapters must be distinguished. This check does not execute source
JavaScript or prove every image rendered. Reader changes need separate real
Foliate browser tests, search/CFI and navigation regression, and live acceptance.

For a confirmed defective conversion, `recover_chm.py` builds an independently
reviewable EPUB from all HTML/TXT/MHT source pages, with HHC order followed by
unlisted pages. It parses top-level literal `document.write` calls without
execution; unsupported script-backed TXT fails explicitly. Source controls and
malformed HTML attributes are normalized. Per-page text digests must survive
sanitization and EPUB packaging. Local image resources are packaged; absent
original image files are listed rather than claimed to be recovered.

```sh
python3 -B scripts/recover_chm.py source.chm document.epub --title 'Book title'
PYTHONPATH=/tmp/opencode/chm-deps python3 -B -m unittest \
  tests.test_recover_chm tests.test_reader_assets.ConverterTests
```

Review `document.audit.json`, original-to-output text comparisons, missing-image
reports and real browser chapter checks before publication. The manual recovery
profile is `manual-chm-complete-v1`, which the incremental scanner preserves.
Publish only the reviewed EPUBs plus an atomic manifest/index generation through
`publish_reader_assets.build_publish`; do not upload audit JSON, temporary files or
unselected candidate builds. Protect the current remote parent and preserve other
asset families and concurrent publication state. Reader code and Reader Assets
are separate deployment/acceptance layers.

The normal converter's XML cleanup preserves text following removed elements and
unwraps old presentational containers. Its extracted-HTML fallback appends pages
absent from the HHC inventory, and supports literal script-backed TXT. These
changes alone do not prove that a Calibre native conversion is complete; the
explicit source audit remains required when repairing known defective books.
