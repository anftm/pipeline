# PDF Processing

Current local entrypoints are `pdf-account-worker.yml` (render or OCR) and
`pdf-account-publish.yml` (central publication). Deploy the same scripts and lane
configuration to the account repositories before using them. Legacy unscoped
render/OCR/asset workflows and their planner/publisher entrypoints were removed.

## Stages

- Render planning pins source identity and lane ownership, probes native text
  and binary images, and schedules bounded time-weighted page ranges.
- Render workers emit checksummed lossless PNG inputs, Reader WebP pages where
  allowed, and native text/layout JSON. Bitonal books preserve the PDF and do
  not publish a recompressed image stream. Native text is not sent to OCR.
- Every object upload first publishes a unique processing-root record. Result
  artifacts carry its path. A transfer failure leaves the roots protected.
- Central render publication retains validated partial ranges and publishes a
  whole-book stream only after exact contiguous page coverage.
- OCR workers consume verified PNG objects, checkpoint recognized pages in
  bounded batches, and preserve unfinished progress. Backend/language/layout
  identities are independent of Reader-image encoding.
- Central OCR publication assembles every page into a complete `pdf-book-text`
  v2 index, then updates the OCR registry and sidecar in `reader-assets-v2`.
- Local OCR assembly additionally emits independent versioned text layers with
  Unicode-codepoint offsets, boxes/quadrilaterals, source/confidence metadata,
  writing mode, direction, review flags and raw-generation identity. These are
  intended overlay/correction inputs, not a claim of reviewed OCR accuracy.
- A content-addressed sidecar snapshot and catalog generation are published
  before releasing the corresponding processing roots. Retrying identical
  publication keeps the same generation and resumes handoff.

## Lifecycle Limits

`publish_reader_v3.py` provides staged upload/readback, immutable v3 catalogs,
serialized pointer promotion, rollback generations and deployed-projection
checks. Its deployed central manual workflow defaults to read-only. HF/Pages
consumers merge the pinned catalog over v2 routes. One 68-page generation is
promoted with both production acknowledgments (`V3_DEPLOYMENT.md`). Automatic
retirement remains pending; see `V3_PUBLICATION.md`. These operations do not
schedule additional account workers.

`scripts/pdf_reading_v3.py` provides offline text backfill, 128-page partitions,
resumable PNG/WebP preview generation and full candidate verification. It does
not publish a v3 resolver pointer. Embedded searchable PDF is optional export;
independent JSON is the primary text source. The central publication entrypoint
now promotes validated single-book candidates; remote correction acceptance
remains pending.

The HF/Pages checkouts now additionally accept v3 reading manifests, demand
preview/text partitions and render independent text over previews/PDF canvases.
Complete Worker search uses the matching text generation. Local browser, real
PDF.js and single-book production checks passed; both surfaces now consume the
promoted generation. Remote correction acceptance is still pending.

Catalog retention is at least 30 days after supersession and until matching
replacement acknowledgments exist for both consumers. Uploading an index or
pushing a Pages commit does not itself establish live deployment acceptance.
Both acknowledgment flags begin false. The older v2 catalog remains supplementary
to the existing sidecar; the v3 catalog is the actual v3 application resolver.

GC inventories all three current buckets. Complete reports can persist positive
grace-period orphan observations. No deletion endpoint exists yet: all producers
need a common remote mutation lock or equivalent conditional protocol, and
consumer acknowledgment must cover all relevant producers and consumers. v3's
two deployed consumer acknowledgments are now wired to production acceptance.

Processing records never expire by age. Failed/cancelled jobs require explicit
verified recovery or retirement, not a lease timeout interpreted as permission
to delete. Heartbeats, input retirement, optimized/searchable PDF building and
daily OpenCode correction are still pending in `PDF_READING_CYCLE_V3.md`.

Commands and acceptance tiers are in `TESTING.md`; lane accounts and credentials
are in `PDF_ACCOUNT_WORKERS.md`.
