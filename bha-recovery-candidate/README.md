# BHA Space recovery candidate — not deployed

This directory is an **offline snapshot**, not the Space's current source and
not an instruction to deploy it. It is based on Space commit
`6705f4f15bee3319a21379a8689e757c78e56fd3`, whereas the deployed Space is
`decd6c30a1fe768ccbce2710b03c4160f442fbb4`. Its older `app/main.py`,
`scripts/start.sh`, `app/bucket_snapshot.py`, and associated tests must **not**
overwrite the current Space without a careful three-way review. The current
Space has additional startup/preservation protections absent from this tree.

The `app/`, `static/`, `scripts/`, `typings/`, and `tests/` files were copied
from the previously tested local candidate, excluding caches, data directories,
credentials, local dependencies, and synthetic storage. `tests/__init__.py`
is added for standalone unittest imports, as the Dockerfile does at build time.
One trailing space was removed from `static/style.css`; otherwise the copied
candidate files are byte-identical to the local candidate at handoff.
This tree retains the original offline submission pause defaults. It changes the
preview hash format; deploying that change may affect drafts in old open tabs.

At handoff, this isolated copy passed 327 application/frontend tests; after the
CSS whitespace change, 24 frontend/static tests and pyright/TypeScript checks
passed. The first static rerun failed because `node_modules` was deliberately
not copied; rerunning against the already installed test dependencies passed.
No production request or Docker build was performed for this snapshot.

The application includes no production durable submission inbox or receipt.
Tests with fake stores, workers or GitHub responses are **not** proof of real
delivery. The corresponding central worker in the parent repository remains
hard-paused. Do not remove either pause or enable workflows based on this copy.
Read `../docs/bha-recovery-handoff-20261106.md` before any further work.

For reproducible local checks, install the pinned test and coverage requirements,
use a disposable `DATA_ROOT` and no production credentials, then from this directory
run the test list in `Dockerfile` or, for a focused regression:

```sh
APP_ROOT="$PWD" DATA_ROOT="/tmp/opencode/bha-candidate-data" \
  python -B -m unittest -q tests.test_api tests.test_bucket_snapshot \
    tests.test_parse_boundaries tests.test_proofread_boundaries \
    tests.test_submission_pause tests.test_reindex_scheduler \
    tests.test_parse_core tests.test_document_identity tests.test_frontend
```

Do not set `BHA_BUCKET_DIR` or use live credentials for these tests. The local
experiment/bridge scripts outside this directory are not included, so the
previous 333-test figure also covered local, separately maintained test modules
and should not be attributed to this isolated copy without running them.
