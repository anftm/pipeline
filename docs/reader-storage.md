# Reader storage: roles, paths, and safe operations

This is the storage contract and an operations map, not a deletion list.
Object identity is always **bucket + full path**. A path's existence, prefix,
age, or absence from one index does not prove that it can be collected. Sizes
reported by listing are logical bytes and may differ from provider billing.

## Source versus generated resources

| Resource | Intended owner / location | Use and constraint |
|---|---|---|
| Original PDF | Source dataset | Existing native PDF reading uses the original dataset URL and PDF.js ranges. No new bucket copy should be needed merely for viewing. OCR may have independent bucket artifacts. |
| Original DjVu | Source dataset | Target is browser decoding of the original. No bucket copy or reading page-image stream should be required. The direct-source DjVu change and its deployment are separate work; do not claim it is already online. |
| Other original formats | Source dataset | Original files are authoritative and are not GC targets. |
| CHM chapter stream | `vomebook/reader-assets-v2:chapters/ebook/chm/` | Indexed chapter manifest, search index, XHTML and resources. Conversion may create an EPUB locally, but new producers should not publish it as a second reading object. |
| Historical CHM EPUB fallback | `vomebook/reader-assets-v2:native/ebook/chm/` | Retain old index/sidecar references and compatibility. The current inspected Reader code selects chapter manifests and does not expose this fallback as an automatic reading fallback. This is not proof that no other consumer uses it. |
| Converted PS/Office PDF | `vomebook/reader-assets-v2:documents/pdf/` | Browser-usable conversion of a non-PDF original; it is not an original PDF. Each document manifest records source and output identity. |
| Migrated/repaired PDF | `vomebook/pdf-pages-v2:derived/` and some `objects/` | A derived PDF can be a current Reader input or historical provenance. A `derived/` name does not mean page images or garbage. |
| OCR/reading data | `vomebook/pdf-pages-v2:objects/` | Depending on manifest/profile, contains OCR outputs, text/search partitions, review records, page maps and sometimes converted documents. Historical reading visuals may still exist. |
| OCR inputs | `melsm/pdf-archive-v2:objects/.../ocr-input/` | Lossless rendered pages for OCR or review, separately bucket-qualified. Keep according to active and retained evidence and lifecycle rules. |
| OCR search/release | `vomebook/ccrd-index` | Search data, OCR text exports, release/intent records, stable IDs and independent retention. This is a separate collection tier. |

Original PDF and DjVu sources differ from converted PDFs: a PS, CAJ, KDH or
other unsupported source may still need a converted PDF to read, and a
document-specific repair may be a legitimate separate artifact. Do not relabel
either as the original. No cleanup or migration is implied by this document.

## Layout and identity

* `chapters/ebook/<format>/<source-sha>/<bundle-id>/...`: the chapter
  producer uses the original source SHA and a 16-hex identity of the complete
  chapter bundle. The manifest controls chapter/resource identity. Different
  source records may refer to the same bundle.
* `native/ebook/chm/<id>/document.epub`: legacy fallbacks are mixed. Some IDs
  are the original CHM SHA; others are the EPUB content SHA. Never infer the
  EPUB's checksum from the directory name without verifying its convention.
  `repair_chapter_search.py` records an observed digest but does not invent
  an independent checksum for a source-addressed legacy fallback.
* `documents/pdf/<category>/<source-sha>/document.pdf`: a converted document
  and its `document-manifest.json`. The output PDF's SHA is separate from the
  source SHA. `ps-rebuild-20261025` is an existing distinct rebuild namespace,
  not an automatically equivalent copy of `ps`.
* `derived/<generation>/<identity>/document.pdf` and
  `derived-manifest.json`: existing converted PDF and provenance. In the
  migration producer, identity includes source key, revision, SHA and profile;
  it is not the PDF's content hash. Newer font repairs may be under
  `objects/` while retaining previous PDF references for provenance.
* `objects/<first-two>/<source-sha>/<identity>/...`: the 16-hex identity is
  determined by the particular producer and recipe. In OCR it may cover the
  source key plus render/recognition/text generation; for other producers it
  can mean another processing identity. Older formats also used named profile
  segments. Two identities under one SHA are not necessarily duplicates.
* The same relative `objects/` path in two buckets is not one object. Page
  source evidence and text outputs must retain their explicit bucket names.

## Indexes and ownership

1. Format `index.json` records the source-to-output mapping; the compact
   `reader-index/reader_assets.json.gz` is the Reader sidecar. A pinned v3
   catalog may overlay the base sidecar; inspecting only the mutable sidecar
   does not prove what both live consumers have loaded.
2. Producers establish processing roots before uploading immutable objects,
   verify uploaded bytes, then checkpoint indexes. A sidecar/catalog handoff
   releases only the matching processing roots. A retirement hint is **not**
   deletion authorization.
3. A live or retained CCRD release can pin Reader objects for runtime, review,
   or provenance. A provenance reference is not the same thing as a current
   browser fetch, but is still a reason to preserve its evidence until the
   complete lifecycle is verified.
4. Current indexes, rollback catalogs, incomplete uploads, accepted text,
   OCR/review evidence, audit receipts, and both deployed consumers all matter.
   A single index or a single dry-run is not a complete reference graph.

## Workflow map (state at the code revision, not live status)

* `.github/workflows/reader-chm-repair.yml`: plans CHM rebuilds and, when its
  scheduled/explicit apply job runs, writes chapter bundles and updates the
  sidecar under the writer lock. The intermediate EPUB is local only for new
  conversions. When a source already has a `native/` fallback, its exact
  index reference is carried forward through reconversion without uploading
  that EPUB again; it is not implicitly retired or removed. A legacy
  source-addressed fallback retains its original source SHA separately if the
  current source changes. That value identifies the old naming convention, not
  the fallback EPUB's independently verified content hash.
* `.github/workflows/reader-chapter-search-repair.yml`: explicit, pinned
  chapter selection; default is plan. It rebuilds search and a manifest from
  a verified chapter bundle, preserves the fallback reference, and reports
  whether a fallback's SHA was independently established.
* `.github/workflows/reader-static-pdf-stream.yml`: category workers convert
  formats such as PS/PPT/WPS. They use the same protected checkpoint protocol
  as other streams: immutable targets are compared with existing bytes,
  conflicts fail closed, and readback precedes index publication. Each source
  has its own bounded protection/checkpoint; an older conversion finishing
  after a newer checkpoint must not supersede it. The sidecar handoff scans
  their category indexes. This does not rename existing converted PDFs or
  prove any PS variant is equivalent. Stream workers and sidecar finalization
  share the `reader-sidecar` writer lock so a concurrent rebuild cannot hand
  off a root whose source mapping was absent from its snapshot.
* `.github/workflows/reader-gc.yml`: scheduled report-only graph scan; an
  explicit `record_observations` input can write observation metadata. A scan
  running with producers may be incomplete and is not deletion authority.
* `.github/workflows/reader-maintenance.yml`: **not report-only by default**.
  Scheduled maintenance uses the retired-assets scope; on Monday it chooses
  the unreferenced scope. Its dispatcher isolates producers and invokes the
  collector in apply mode unless `plan_only` is selected. Even plan-only
  maintenance changes producer workflow state and writes maintenance journals;
  it is not a read-only inspection.
* `.github/workflows/reader-gc-collect.yml`: separate collection scopes and
  explicit `apply`; a fenced maintenance invocation may set `apply=true`.
* `.github/workflows/reader-gc-ccrd.yml`: independent CCRD OCR / legacy-index
  tier; scheduled runs request apply. A successful legacy-index empty batch is
  not evidence that CCRD OCR GC passed. Verified reset tombstones permit only
  an evidenced empty OCR batch; unknown residue or pending retirement blocks.

Workflow definitions and deployment state may change independently. Before
reporting any workflow as paused, successful or deployed, inspect its current
state, exact run and artifacts; do not infer it from these source files.

## Operational safety

* Use immutable source revision, byte length and SHA for any new source
  reference; reject a changed source rather than silently reading another book.
  Preserve existing PDF direct-reading and range behavior.
* A candidate is not a deletion decision. Any eventual collection requires a
  complete current four-bucket reference closure, consumer acknowledgments,
  producer isolation, the scope-specific retention conditions, root rechecks,
  exact bounded targets and post-delete readback. Unavailable evidence blocks.
* Introducing dataset-source primaries requires the graph to fail closed until
  exact external source availability can be checked under the collection fence;
  four-bucket inventory alone cannot establish that a dataset revision exists.
  Dataset-primary stage/promotion defaults to disabled via
  `READER_DATASET_PRIMARY_ENABLED`; both deployed Reader surfaces and the
  pinned-origin proxy must be verified before that flag is explicitly enabled.
* Do not automatically publish OCR corrections, reset stable IDs, retire source
  evidence, enable a paused producer or clean historical objects as part of a
  naming/architecture refactor.

Relevant code: `pilot_epub_chapters.py`, `repair_chapter_search.py`,
`build_reader_assets_v2_sidecar.py`, `reader_stream_index.py`,
`migrate_pdf_derived_sources.py`, `pdf_ocr_stages.py`, `reader_lifecycle.py`,
`reader_gc_graph.py`, `reader_gc_collect.py`, and `reader_gc_ccrd.py`.
