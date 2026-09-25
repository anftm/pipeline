# PDF rendering and image OCR

`Render PDF OCR Inputs` (`pdf-render-inputs.yml`) supplies the durable inputs
for `Build PDF OCR Assets` (`pdf-ocr-assets.yml`). The latter also runs on render
workflow completion and can be dispatched independently to drain its backlog.
The existing large-PDF WebP worker is not the supplier of OCR images: its
100 MiB policy and lossy delivery images are unsuitable for that purpose.

## Rendering

- The source inventory includes original PDFs and generated Reader PDFs of all
  sizes. Already delivered OCR books are skipped when their source/profile is
  current; they are not rebuilt solely to populate the PNG cache.
- Pure native-text PDFs retain their PDF text layer. Mixed PDFs get extracted
  native text JSON on native pages and PNG inputs for recognition on scan pages.
- Each rendered page retains a high-quality PNG, nominally 300 DPI, bounded by
  the configured maximum pixel count. The Reader WebP is a separate resized,
  quality-85 derivative. Optional JXL is encoded directly from the PNG.
- PNG is retained on native pages of mixed books too, for later encoding use.
  No automatic PNG deletion is currently performed.
- `pdf_render_manifest.json` contains only the descriptors for completed image
  bundles and rendering failures; each checksummed `render-manifest.json`
  describes ordered page objects and the compact Reader page manifest.
- Upload objects first, then atomically publish rendering metadata and the
  Reader stream mapping. OCR state `rendered` makes that stream survive other
  publishers rebuilding the sidecar. It does not advertise an OCR text layer.

## Recognition

- The worker consumes only PNG objects from completed render manifests. It
  neither downloads PDFs nor invokes Poppler or cjxl. Path, byte length, digest,
  page number, dimensions, source identity and generation are checked.
- `target_pages` defaults to **500 actual recognition pages per shard**. Native
  pages do not count. Large books may span workers; small books are packed
  together. Pixel dimensions and text density still affect processing time, so
  equal page counts do not guarantee equal duration.
- Eight OCR workers may run concurrently. A worker keeps its model loaded and
  checkpoints uploads every 25 pages. Failed pages remain pending and successful
  pages are retained in `pdf_ocr_progress.json`, keyed by render generation.
- Publication can proceed after some workers fail. A book is marked `ready`
  only when every native and OCR page is present and verified. Incomplete books
  keep their page progress and their already published Reader stream.
- JSON/text and page images retain the existing Reader wire format. Turning on
  JXL is a render-workflow input, not an OCR-worker operation.

## API quota

Do not sync a book to `hf://buckets/vomebook/pdf-pages` at the Bucket root.
`sync_bucket` lists the remote prefix recursively before applying include
filters. Doing that per book across workers repeatedly enumerates the entire
Bucket and can exhaust the shared 1,000 API requests / 5 minutes allowance.

Uploads are scoped to `objects/<prefix>/<source-sha>/<book-profile>/`. Public
image/text downloads use `/buckets/.../resolve/...` URLs directly instead of
per-page `HfFileSystem` metadata lookups. These changes reduce API requests;
they do not guarantee zero rate limits, particularly when other workflows use
the same account. Legacy large-PDF publishers still have separate upload code.

## Verification

Fast, offline, no model download:

```sh
python3 -m unittest -v tests/test_pdf_ocr.py tests/test_run_pdf_ocr.py tests/test_pdf_ocr_stages.py tests/test_reader_asset_concurrency.py
```

Explicit local Poppler integration (Pillow and `pdftocairo`/`pdftotext` required):

```sh
python3 -m unittest -v tests/test_pdf_ocr_render_integration.py
```

This creates a small scanned PDF and verifies an actual 300 DPI PNG and WebP
are produced before OCR. Unit tests use a mocked recognizer, not a performance
benchmark or validation of the live Paddle model's recognition quality.

After deployment, confirm the render run publishes `pdf_render_manifest.json`,
then its triggered OCR run plans from those PNG descriptors. Verify at least
one completed book's manifests/objects and the published Pages sidecar. A
workflow starting is not proof that the full rendering/OCR publication passed.
