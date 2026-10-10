# v3 Reading Publication

Implementation, rehearsal and single-book production acceptance: 2026-10-10.
The central workflow and HF/Pages consumers are deployed. One 68-page generation
was promoted and acknowledged by both surfaces; see `V3_DEPLOYMENT.md` for exact
workflow and endpoint evidence. The earlier memory rehearsal had no remote writes.

## Protocol

`scripts/publish_reader_v3.py` owns the v3-specific entrypoint. The current v2
sidecar remains the base for other formats; v3 overrides are resolved from one
pointer in `vomebook/reader-assets-v2/reader-index/v3/current.json`.

1. `build-stage` optionally generates one pinned book from its PDF and complete
   OCR manifest. It reuses recognition, generates full PNG/WebP previews, and
   backfills the independent text layer. `stage` accepts an already-built bundle.
2. Before any upload, verify every reading component and traverse raw/review
   dependencies with qualified bucket, digest and byte counts. Only dependencies
   actually referenced by the candidate are uploaded from the workspace.
3. Write processing protection roots before content upload. Never overwrite
   different bytes at an existing immutable key. Verify remote readback without
   relying on the local workspace, then write an immutable candidate record.
4. `promote` merges the selected book into the current v3 catalog. It requires
   the expected parent and verifies resources again. Write an immutable catalog
   and durable promotion record, then replace the single current pointer.
5. HF refresh and Pages builds independently read that pointer and its checked
   immutable catalog, merging its book entries onto their v2 base. Broken v3
   metadata preserves HF's last complete snapshot or fails the Pages build;
   it cannot silently produce an empty/partial projection.
6. `ack` reads the documented deployed status and projection. It verifies current
   generation, catalog checksum and every projected reading path. Pages additionally
   checks the deployed sidecar against its build receipt. Only then persist the
   consumer acknowledgment. This is metadata acceptance, not multilingual OCR
   quality or a replacement for post-deployment browser smoke.

The Hub batch API has no supported conditional-write parameter. This is a
serialized single-writer protocol, **not CAS**. Apply is restricted to
`anftm/pipeline`'s `reader-v3-publish.yml` on main, under `reader-sidecar`.
Expected-parent checks detect stale plans; they do not protect against unrelated
writers that bypass this protocol. The v3 pointer must only be written here.
Existing v2 producers remain independently coordinated, so global deleting GC
is still disabled.

## Operations

The central manual workflow is `Publish v3 Reading Generation`; default operation
is `inspect`, default `apply` is false. Operations are `build-stage`, `stage`,
`promote`, `rollback`, `ack` and `inspect`.

For `build-stage`, the input JSON contains `source_key`, `source_sha256`,
`primary` and `ocr_manifest`. Both resources use `{bucket,path,sha256,bytes,role}`;
the primary is a public runtime PDF and the OCR manifest is provenance. Optional
`dpi` and `max_pixels` control preview generation. The input is a pinned single
book, not an arbitrary URL or shell command. The workflow has no schedule and
does not consume additional account runners automatically.

When the public primary PDF does not yet exist, optional `primary_source` supplies
`{repo,revision,path}` for the original VoiceOfML dataset PDF. The revision must be
an immutable 40-character commit; the repo/path must exactly match `source_key`.
The import verifies remote size before download, then exact bytes and source
SHA-256 before the candidate's ordinary protected upload/readback. Moving refs,
arbitrary URLs and mismatched source identities are rejected.

For `stage`, supply the qualified reading-manifest resource. The remote workflow
expects its objects already present; the Python API can stage from a local bundle.
For `promote`, supply the candidate resource from the stage report and the current
generation as `expected_parent` (`none` only for the first promotion).

For `rollback`, supply a retained immutable catalog resource and the current
expected parent. Rollback verifies its reading objects and creates a new catalog
generation with `rollback_target`; it does not erase forward history or mutate
an old generation. Retrying identical promotion preserves the current generation.

`promote`/`rollback` then run `publish_search_reader_index.py` under the same
workflow lock. That script merges v3 before projecting to Pages. Its canonical
bucket read is fail-closed; it no longer recovers a failed current-bucket read
from a stale legacy dataset. A failed Pages projection leaves the v3 pointer
and old deployed projection available for retry; no consumer ack is inferred.

Local read-only inspection examples:

```bash
python3 -B scripts/publish_reader_v3.py inspect
python3 -B scripts/publish_reader_v3.py stage --resource /tmp/opencode/reading-resource.json --bundle /tmp/opencode/v3-candidate
python3 -B scripts/publish_reader_v3.py promote --resource /tmp/opencode/candidate-resource.json --expected-parent CURRENT_GENERATION
python3 -B scripts/publish_reader_v3.py ack --surface hf
```

Those commands omit `--apply`. Remote mutations require the actual central
workflow context; do not forge that context locally to bypass writer ownership.

## Retention And Gaps

All v3 candidates, promotion records and old catalogs remain durable GC roots in
this first implementation. Processing records remain protected after staging;
they are not automatically retired or deleted. Promotion records start with both
acknowledgments false and record a 30-day minimum retention policy. Age/ack-based
retirement, producer-wide leases and collection are still pending. This deliberately
does not claim completed storage reclamation.

The catalog binds all v3 book components via an immutable reading manifest.
Other v2 formats are merged from their existing sidecar, not migrated into this
catalog. Cross-format global atomic publication remains pending.

## Rehearsal Evidence

The real 68-page native candidate passed a memory-store publication rehearsal:
280 resources verified, immutable upload/readback, generation promotion, HF Python
projection, Pages Node projection and a complete GC reference graph with no
candidate objects. Remote dependencies were read and checksummed; remote writes
were zero. Unit tests cover collisions, corrupted dependencies, stale parents,
two-book merge, idempotency, interrupted pointer publication, rollback, current
consumer receipts and central workflow ownership.

Rehearsal exposed a stale local `PDF_OCR_INPUT_BUCKET=pdf-jxl` environment value.
The three-bucket contract now pins `melsm/pdf-archive-v2`, and local candidates
were regenerated with verified current-bucket evidence. This was a local candidate
defect, not evidence of production corruption. Temporary evidence stays in
`/tmp/opencode/` and is not part of deployment uploads.
