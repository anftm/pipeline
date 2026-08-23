# Reader Assets Pipeline

Reader Assets converts source formats that the browser Reader cannot open directly. It is independent from normal search generation and does not place converted files in the HF Search Space.

## Current Scope

The first conversion set is deliberately limited to:

| Source | Profile | Reader output |
| --- | --- | --- |
| DOC, DOCX | `libreoffice-pdf-v1` | PDF |
| MOBI, AZW3 | `calibre-epub-v1` | EPUB |
| TIF, TIFF | `pillow-pdf-v1` | PDF |

PDG is intentionally excluded. It must not be added to the scanner or workflow until its separate implementation is ready.

## Storage Contract

The default dataset is `vomebook/Reader-Assets`:

```text
manifest.json
reader_assets.json.gz
objects/ab/<source-sha256>/document.pdf
objects/cd/<source-sha256>/book.epub
```

Manifest keys use `<source-repo>\0<relative-path>`. Ready entries include the pinned source revision, source and output SHA-256 values, byte sizes, versioned conversion profile, Reader mode, and content-addressed output path. Failed entries contain only a stable error class, not commands or internal paths.

An existing ready entry is retained when a retry fails. Artifacts, the manifest, and the compact sidecar are committed atomically.

## Workflow

Run `Build Reader Assets` manually. Inputs are:

```text
repo             exact VoiceOfML source dataset, or blank
extension        doc, docx, mobi, azw3, tif, tiff, or blank
limit            maximum files in this batch
retry_failed     retry failures for the current profile
force            rebuild ready entries
dry_run          scan and print the queue only
```

Start with a dry run and a small `limit`. A non-dry run installs LibreOffice and Calibre, builds the incremental queue, converts files, publishes Reader Assets, then copies only `reader_assets.json.gz` to the GitHub Pages data directory. It never commits asset updates or the sidecar to the HF Search Space, so conversion batches do not trigger Space rebuilds.

## Local Verification

```bash
python3 -m unittest tests.test_reader_assets -v
python3 -m compileall -q scripts tests
```

Individual stages can be inspected without publishing:

```bash
python3 scripts/scan_reader_assets.py --manifest /path/to/manifest.json --limit 20
python3 scripts/convert_reader_assets.py --dry-run
python3 scripts/build_reader_assets_index.py /path/to/manifest.json
```

Search frontends must not consume converted mappings until a real first batch has passed dataset URL, CORS, Range, PDF, EPUB, and deployment-cache acceptance. Publishing the sidecar alone does not alter current frontend Reader behavior. HF Search should eventually read the remote Reader-Assets sidecar at runtime with a bounded cache; it must not receive a Space repository commit for every conversion batch.
