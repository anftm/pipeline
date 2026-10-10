# v3 Production Gray Release

Date: 2026-10-10. These are authenticated workflow results and read-only
observations of the deployed HF and Pages endpoints, not local-snapshot counts.

## Published Generation

- Catalog generation: `e4422817612ebb0b321dc8ac532a7f9323d19adf606757615383c42187ec9cde`.
- Catalog SHA-256: `9dbb0d63abfa26089c77165db0376254dfbe21d4b665591b51af2f6263b03d53`.
- Scope: one 68-page original PDF, Reader ID `3fqdpnhdye05f`.
- Source SHA-256: `e54b9669611eee0bfd44c7ef6f078df4f3adffa81fa042b6dba7e8570230a7d4`.
- Reading manifest version directory: `752747f25a5a4132` in `vomebook/pdf-pages-v2`.
- Components: document, full preview and independent text ready; correction
  pending; embedded searchable PDF skipped. Existing native extraction was reused.

The central runtime was deployed at `90a062983d1baaadd2015ddcbcd924f3aa267a0d`.
HF v3 consumers initially deployed at `d4712406`, with the legacy native-PDF text
compatibility fix at `a62a220e`. Pages consumers initially deployed at `d5b3f5c0`,
with that fix at `a1d3e87b`. Subsequent unrelated Reader releases preserved v3.
The post-promotion Pages projection deployed from `92efe7b9`.

## Workflow Evidence

| Operation | Run | Result |
| --- | --- | --- |
| Build and stage | [38020683339](https://github.com/anftm/pipeline/actions/runs/38020683339) | 68 pages, 144 local objects, 280 dependencies verified and remote-read back |
| Promote and project | [38021612404](https://github.com/anftm/pipeline/actions/runs/38021612404) | One current catalog pointer; Pages projection succeeded |
| HF acknowledgment | [38022288488](https://github.com/anftm/pipeline/actions/runs/38022288488) | Current generation and deployed paths verified |
| Pages acknowledgment | [38022303195](https://github.com/anftm/pipeline/actions/runs/38022303195) | Current generation, deployed paths and sidecar receipt verified; both acks true |

The first build attempt failed before upload because the scoped release omitted
the new `ocr_layout.arrange(include_writing_modes=...)` dependency. The dependency
was deployed and the successful replacement run above completed. Original-source
import was also added for native PDFs without an existing public primary object:
fixed dataset commit, matching source key, remote size and downloaded SHA-256 are
all required. No local writer-context override was used.

## Production Acceptance

- Both deployed Readers opened the uploaded candidate before promotion, using real
  PDF.js and the actual HF bucket proxy without Playwright request routing.
- After promotion, both opened by original-source Reader ID at 1200x850 and
  390x844. Page 60 had a nonblank PDF canvas and 48 independent text runs whose
  complete text matched the verified book index. All four had no page errors.
- Full-book search for `苏联` returned exactly 176 hits on both surfaces, matching
  the verified complete index. This is native-text consistency, not OCR accuracy.
- HF adopted the catalog through its normal 300-second metadata refresh. Pages'
  build receipt bound the same generation and catalog digest to deployed sidecar
  bytes. Metadata acknowledgments were persisted only after these checks.
- HF production smoke passed all four cases; Pages production smoke passed.
  Reader CI run [38021563328](https://github.com/vomebook/search/actions/runs/38021563328)
  passed after the native-PDF compatibility fix and test mock synchronization.

Direct deployed Readers:

- [HF](https://voiceofml-search.hf.space/static/reader.html?id=3fqdpnhdye05f)
- [Pages](https://vomebook.github.io/search/static/reader.html?id=3fqdpnhdye05f)

Temporary screenshots, pinned specs and complete operator reports remain under
`/tmp/opencode/v3-production-release/`; they are not uploaded with source code.

## Remaining Scope

This is a single-book production acceptance, not a full-corpus v3 migration.
Automatic account-worker-to-v3 admission, correction-provider execution,
multilingual OCR quality acceptance, SVG/HTML representations and optional PDF
exports remain pending. Candidate/history retention stays conservative and GC
remains report-only. The central single-writer protocol still is not Hub CAS.
