# PDF rendering and image OCR

`Render PDF OCR Inputs` (`pdf-render-inputs.yml`) supplies the durable inputs
for `Build PDF OCR Assets` (`pdf-ocr-assets.yml`). The latter also runs on render
workflow completion and can be dispatched independently to drain its backlog.
The existing large-PDF WebP worker is not the supplier of OCR images: its
100 MiB policy and lossy delivery images are unsuitable for that purpose.
The image OCR concurrency group is `pdf-image-ocr-assets`; legacy monolithic
runs may finish independently. New rendering/OCR publication jobs both use
the shared `reader-assets` publication lock.

## Rendering

- The source inventory includes original PDFs and generated Reader PDFs of all
  sizes. Already delivered OCR books are skipped when their source/profile is
  current; they are not rebuilt solely to populate the PNG cache.
- Pure native-text PDFs retain their PDF text layer. Mixed PDFs get extracted
  native text JSON on native pages and PNG inputs for recognition on scan pages.
- When structural PDF optimization has failed, native-text PDFs also receive a
  Reader page stream while keeping the extracted native text and v2 full-text
  index; these pages are not sent to OCR. Previously completed text-only render
  ranges without images are rerendered for this case.
- Each rendered page retains a high-quality OCR PNG, nominally 300 DPI, bounded by
  the configured 50-million-pixel maximum; unusually large pages use a lower
  rendering DPI. Reader WebP (quality 85) and optional JXL are encoded from a
  separate reader image capped at 1800 pixels on its longest edge. For pages
  with no extracted text identified as a single nearly full-page raster scan,
  both reading formats are also capped at the source image's pixel dimensions
  on each axis; the high-resolution OCR input is unaffected. Pages with
  extracted text or multiple embedded images retain the normal reading cap.
  A single full-page image check cannot rule out every vector overlay; such
  pages need visual spot checks. JXL is encoded from
  the resized lossless reader image, never from the lossy WebP.
- PNG is retained on native pages of mixed books too, for later encoding use.
  No automatic PNG deletion is currently performed.
- `pdf_render_manifest.json` contains only the descriptors for completed image
  bundles and rendering failures; each checksummed `render-manifest.json`
  describes ordered page objects and the compact Reader page manifest.
- Upload objects first, then atomically publish rendering metadata and the
  Reader stream mapping. OCR state `rendered` makes that stream survive other
  publishers rebuilding the sidecar. It does not advertise an OCR text layer.
- Result discovery handles both flat single-artifact downloads and nested
  multi-artifact downloads. Legacy runs with no result files fail explicitly;
  range runs record incomplete books and retry missing ranges on the next run.
  `recover_run` republishes validated artifacts from a completed main-branch
  render run without recomputing or reuploading its PNG images.
- Books over 500 pages are scheduled in 250-page ranges; smaller books remain
  single tasks. Ten render workers may run concurrently. Each range uploads
  its immutable pages and checksummed range descriptor before the worker writes
  its result artifact. `pdf_render_progress.json` tracks completed ranges by
  source SHA, profile and book identity. Later batches validate and reuse those
  descriptors, rerendering only missing/invalid ranges. A whole-book manifest
  and Reader mapping are published only when all ranges form a contiguous,
  verified book. Partial books remain pending without exposing partial streams.

## Recognition

- The worker consumes only PNG objects from completed render manifests. It
  neither downloads PDFs nor invokes Poppler or cjxl. Path, byte length, digest,
  page number, dimensions, source identity and generation are checked.
 - OCR language is selected by the `lang` input of `Build PDF OCR Assets` and
  defaults to `auto`. In automatic mode, the planner scores the book key and
  available extracted text by Unicode script and selects a language per book;
  Arabic-script books with Persian/Iran markers use `fa`, otherwise `ar`.
  The language and model version participate in the OCR
  profile and are recorded in the book-text and OCR manifests. PP-OCRv6 is
  used for its supported Chinese, English, Japanese and Latin-language set;
  Arabic (`ar`), Persian (`fa`, the primary Iranian language), Korean and
  Cyrillic languages use PP-OCRv5. Changing language reuses published PNG
  renders and reruns recognition without rerendering the PDF.
- The `backend` input selects `paddle_static` (the compatibility default),
  `paddle_onnxruntime`, or `rapidocr_onnxruntime`. It defaults to RapidOCR
  ONNX Runtime. Backend, language and model version are all part of the OCR
  profile. RapidOCR currently uses its ONNX Runtime path for `ch`/`en`; Paddle's ONNX Runtime path remains the
  multilingual option for `fa`, `ar`, Korean and other profiles.
- `target_pages` defaults to **2,000 actual recognition pages per shard**. Native
  pages do not count. Large books may span workers; small books are packed
  together. Pixel dimensions and text density still affect processing time, so
  equal page counts do not guarantee equal duration.
- Automatic OCR runs now plan up to **100 rendered books** per successful render
  workflow, matching the render batch size. The 8-worker shard limit remains;
  this removes the previous 20-book automatic backlog cap.
- A reader-image-only render-profile change reuses previously recognized page
  objects when the source, recognition profile and each OCR-input PNG checksum
  match. The worker refreshes the v2 book index and page paths without rerunning
  recognition; changed inputs still enter the normal OCR queue.
- RapidOCR ONNX tasks use the configured target (2,000 by default). Paddle
  multilingual tasks are capped at 1,200 pages per task because their CPU
  recognition rate is lower. This reduces repeated model downloads without
  letting a slow language task approach the runner timeout.
- Eight OCR workers may run concurrently. A worker keeps its model loaded and
  checkpoints uploads every 25 pages. Failed pages remain pending and successful
  pages are retained in `pdf_ocr_progress.json`, keyed by render generation.
- Publication can proceed after some workers fail. A book is marked `ready`
  only when every native and OCR page is present and verified. Incomplete books
  keep their page progress and their already published Reader stream.
- Dispatch with `retry_failed_only=true` to resume only books whose published
  OCR state is `failed`. Normal automatic planning still favors untouched books;
  recovery reads matching generation progress and retries only missing pages.
- JSON/text and page images retain the existing Reader wire format. Turning on
  JXL is a render-workflow input, not an OCR-worker operation.
- The published per-book text file is now strict `version: 2`, `kind:
  pdf-book-text`, and `complete: true`. It contains every page, ordered text,
  `text_spans`, normalized layout metadata, source SHA-256, page count, and
  Unicode-codepoint offsets. Readers and the API reject the old v1
  `pdf-ocr-book-text` file; source books are requeued until the v2 index exists.

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

## Reading order and search mapping

`ocr_layout.py` emits `text`, `blocks`, `raw_blocks`, `text_spans` and `layout`.
Tall text columns can be ordered vertically (right column first by default);
horizontal columns use whitespace cuts, with separated spanning headings
handled before the column body. These geometric rules are conservative
heuristics, not a trained document-layout classifier. Flags in `layout.review`
indicate assumed directions and unverified within-block character order.

`state/pdf_ocr_layout.json` provides book/page overrides keyed by the same
`repo\u0000path` identifier as the manifests. For example:

```json
{
  "namespace/dataset\u0000old-book.pdf": {
    "default": {"writing_mode": "vertical-rl", "join_soft_lines": true},
    "pages": {
      "1": {"writing_mode": "horizontal-rtl"},
      "2": {"rotation": 90},
      "3": {"regions": [
        {"box": [0, 0, 1, 0.15], "writing_mode": "horizontal-ltr"},
        {"box": [0, 0.15, 1, 1], "writing_mode": "vertical-rl"}
      ]}
    }
  }
}
```

Rotation is clockwise, applied to the PNG before recognition. Region boxes
are normalized coordinates on that oriented image; emitted boxes/polygons
are mapped back to the original PNG. Automatic 90-degree page orientation is
not enabled. Right-to-left overrides order boxes, never blindly reverse the
characters inside a recognized string.

Soft-line joining requires compatible geometry and CJK characters on both
sides; punctuation is never removed. Region/paragraph gaps and indentation
retain separators. `join_soft_lines: false` disables joining where a book's
layout is ambiguous. All joins are recorded in `layout.boundaries`.

Text offsets use **Unicode code points**, not JavaScript UTF-16 code units.
Each span maps a character range to a real OCR block/polygon, with
`precision: block`. These are not fabricated per-character boxes. A Reader
must consume that offset contract before claiming exact character highlights;
the existing Reader implementation is outside this pipeline change.

Layout version/options participate in the OCR identity and progress generation,
so changing them reruns recognition from saved PNGs without rerendering PDFs.
Synthetic vertical/RTL/multicolumn fixtures verify ordering and punctuation;
actual old-book recognition accuracy still requires a representative labeled
scan set. The live end-to-end smoke validates the transfer/recognition/publication
chain, not all layout classes.

## Verification

Fast, offline, no model download:

```sh
python3 -m unittest -v tests/test_pdf_ocr.py tests/test_run_pdf_ocr.py tests/test_pdf_ocr_stages.py tests/test_ocr_layout.py tests/test_reader_asset_concurrency.py
```

Explicit local Poppler integration (Pillow and `pdftocairo`/`pdftotext` required):

```sh
python3 -m unittest -v tests/test_pdf_ocr_render_integration.py
```

This creates real scanned PDFs at 100 and 150 DPI, verifying that the OCR PNG
retains its rendering resolution while reader WebP/JXL respect the original
scan pixels or the reading cap. JXL encoding is mocked unless `cjxl` is
installed. These tests are not a performance benchmark or a validation of the
live recognition model's accuracy.

After deployment, confirm the render run publishes `pdf_render_manifest.json`,
then its triggered OCR run plans from those PNG descriptors. Verify at least
one completed book's manifests/objects and the published Pages sidecar. A
workflow starting is not proof that the full rendering/OCR publication passed.
