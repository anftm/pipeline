#!/usr/bin/env python3
"""Validate one accepted BHA reindex response and its observed completion.

These pure checks perform no HTTP calls and do not start/retry a reindex. The
caller must retain the 202 response from its own POST; matching old revisions or
an absent response cannot establish the identity of an accepted job. A ready
result checks snapshot manifest metadata, not complete object content, source
freshness, freshness of remote writers, or recoverability after a crash.
"""

import json
from pathlib import Path
import re
import sys


_JOB = re.compile(r"[0-9a-f]{32}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")


class InvalidResult(ValueError):
    pass


class JobFailed(RuntimeError):
    pass


def _archive_map(value):
    if (not isinstance(value, dict) or set(value) != {str(i) for i in range(32)}
            or any(not isinstance(sha, str) or not _SHA.fullmatch(sha) for sha in value.values())):
        raise InvalidResult("invalid archive identity")
    return value


def accepted_job(response):
    if (not isinstance(response, dict) or response.get("ok") is not True
            or response.get("status") != "accepted" or response.get("mode") != "incremental"
            or not isinstance(response.get("job_id"), str) or not _JOB.fullmatch(response["job_id"])):
        raise InvalidResult("reindex acceptance has no valid job identity")
    return response["job_id"]


def evaluate(response, expected_commits, expected_revisions, observation):
    job_id = accepted_job(response)
    commits, revisions = _archive_map(expected_commits), _archive_map(expected_revisions)
    if not isinstance(observation, dict) or not isinstance(observation.get("job"), dict):
        raise InvalidResult("reindex job status unavailable")
    job = observation["job"]
    if job.get("job_id") != job_id:
        raise InvalidResult("reindex job identity changed")
    if job.get("mode") != "incremental":
        raise InvalidResult("reindex mode changed")
    if _archive_map(job.get("target_archive_commits")) != commits:
        raise InvalidResult("reindex target commits changed")
    if _archive_map(job.get("target_archive_revisions")) != revisions:
        raise InvalidResult("reindex target revisions changed")
    state = job.get("state")
    if state == "failed":
        raise JobFailed("reindex job failed")
    if state in ("accepted", "running"):
        return "pending"
    if state != "succeeded":
        raise InvalidResult("unknown reindex job state")
    if observation.get("index_status") != "ready" or observation.get("switch_pending") is not False:
        raise InvalidResult("index is not ready or switch remains pending")
    if _archive_map(observation.get("archive_revisions")) != revisions:
        raise InvalidResult("serving revisions differ from accepted target")
    if not isinstance(observation.get("build_fingerprint"), str) or not observation["build_fingerprint"]:
        raise InvalidResult("serving build fingerprint unavailable")
    if observation.get("bucket_snapshot_configured") is not True:
        raise InvalidResult("snapshot bucket is not configured")
    marker = observation.get("bucket_snapshot_marker_present")
    snapshot_status = observation.get("bucket_snapshot")
    manifest = observation.get("snapshot_manifest")
    if marker is not True and marker is not False:
        raise InvalidResult("snapshot publication state unavailable")
    if snapshot_status is None:
        return "pending"
    if not isinstance(snapshot_status, dict):
        raise InvalidResult("snapshot publication status unavailable")
    publication_state = snapshot_status.get("state")
    if publication_state in ("publish_failed", "unavailable"):
        raise JobFailed("snapshot publication failed")
    if publication_state not in ("publishing", "pending_publish", "published", "restored"):
        raise InvalidResult("snapshot publication status is unknown")
    if marker or publication_state in ("publishing", "pending_publish"):
        return "pending"
    if manifest is None:
        return "pending"
    if not isinstance(manifest, dict):
        raise InvalidResult("snapshot manifest unavailable")
    return evaluate_snapshot(manifest, snapshot_status, revisions, observation["build_fingerprint"])


def evaluate_snapshot(manifest, snapshot_status, expected_revisions, expected_build):
    """Classify validated manifest/status metadata from one observation."""
    revisions = _archive_map(expected_revisions)
    if not isinstance(manifest, dict) or not isinstance(snapshot_status, dict):
        raise InvalidResult("snapshot metadata unavailable")
    snapshot, generation = manifest.get("snapshot"), manifest.get("generation")
    if (not isinstance(snapshot, str) or not re.fullmatch(r"bha-[a-z0-9-]+", snapshot)
            or generation != snapshot):
        raise InvalidResult("snapshot identity differs")
    # A previous valid snapshot may remain while the async publisher is being
    # scheduled. Do not treat it as failure or as completion.
    if _archive_map(manifest.get("archive_revisions")) != revisions:
        return "pending"
    if snapshot_status.get("snapshot") != snapshot:
        return "pending"
    if manifest.get("build_fingerprint") != expected_build:
        raise InvalidResult("snapshot and serving build differ")
    return "complete"


def evaluate_accepted_pair(response, expected_commits, expected_revisions, first, second):
    """Require a second matching status before reporting metadata complete."""
    if evaluate(response, expected_commits, expected_revisions, first) != "complete":
        return "pending"
    if evaluate(response, expected_commits, expected_revisions, second) != "complete":
        return "pending"
    def identity(value):
        job = value["job"]
        return (job["job_id"], value["build_fingerprint"], value["snapshot_manifest"]["snapshot"],
                value["snapshot_manifest"]["generation"])
    if identity(first) != identity(second):
        raise InvalidResult("serving or snapshot identity changed between observations")
    return "complete"


def main(argv):
    if len(argv) not in (5, 6):
        raise InvalidResult("expected acceptance, commit, revision and one or two status paths")
    values = [json.loads(Path(path).read_text(encoding="utf-8")) for path in argv[1:]]
    state = evaluate(*values) if len(values) == 4 else evaluate_accepted_pair(*values)
    print(json.dumps({"observed": state, "snapshot_metadata_matches": state == "complete",
                      "snapshot_contents_verified": False, "external_writers_fenced": False}, sort_keys=True))
    return 0 if state == "complete" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except (InvalidResult, JobFailed, OSError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
