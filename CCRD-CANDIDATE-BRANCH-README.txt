CCRD candidate code transfer — 2026-11-06
=========================================

This branch preserves the local CCRD single-TXT models, read-only probes and
their synthetic tests for handoff. It is NOT the production branch and is NOT
the deployed automatic updater. Do not merge it wholesale or enable workflows.

The current development code was copied byte-for-byte from local commit
ca537983655954e4edf0edb54ebf7efd4334f860. The branch starts instead at the then
current central main 0a3b479ff6720c28c97f8e6127c1598bae1fdd30, so it does not
restore the legacy OCR workflows or overwrite the later central cleanup.
Future main changes still require an explicit path-by-path review.

Included: scripts/ccrd_single_source_*.py and ccrd_strict_json.py; read-only
TXT/target/search/streaming verification scripts; their tests; and a manual
read-only checker workflow. The workflow is restricted to anftm/pipeline main
and is kept here only to retain its source and contract test. It has NOT been
enabled, dispatched or deployed. The GET checks may contact the actual public
Space; check_ccrd_live_search_sample.py sends a read-only query via POST; the
CW streaming probe transfers about 796 MB if explicitly run with --live.
Unit tests use synthetic fixtures and do not launch these live checks.

The offline chain and manifest/reuse logic are models, not a real publisher,
durable journal, writer lock, multi-system recovery protocol or GC extension.
Their publication_ready=false and related limits are intentional. The one-hop
archive reuse shadow is NOT accepted by the existing production lifecycle.
Do not treat its synthetic GC test as permission to reuse or delete objects.

See CCRD-AUTOMATION-HANDOFF-20261106.txt at this branch's base for the user
goal, preservation boundaries, production blockers, prior verification and
other-session coordination. That main-branch document predates this code
transfer; its statement that only the document was published is historical.
