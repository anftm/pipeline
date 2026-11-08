"""Durable journal for coordinating one search data promotion.

Each record is content-addressed and written once.  The mutable head is a
single-object CAS, not a distributed lock or a cross-site transaction.  Its
sequence detects an ABA only when every writer obeys this protocol; callers
must coordinate all known writers and treat unknown writers as a real risk.
Consumer observations must be obtained and validated by the caller, not guessed
from this journal or the desired pointer.  This module never deploys a site.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Protocol

try:
    from . import search_snapshot
    from .reader_bucket_store import S3BucketStore, S3NotFound
    from .reader_page_cas import strong_etag
    from .search_publication_pointer import (
        BUCKET,
        _generation,
        apply_pointer,
        decode_pointer,
        make_pointer,
    )
except ImportError:
    import search_snapshot
    from reader_bucket_store import S3BucketStore, S3NotFound
    from reader_page_cas import strong_etag
    from search_publication_pointer import (
        BUCKET,
        _generation,
        apply_pointer,
        decode_pointer,
        make_pointer,
    )


HEAD_KEY = "published/transition.json"
RECORD_ROOT = "published/transitions/"
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
MAX_JSON = 128 * 1024
STATES = frozenset({"prepared", "applying", "verified", "publishing", "pointer-observed", "complete",
                    "rolling-back", "rolled-back"})
SITES = frozenset({"pages", "space"})


class TransitionStore(Protocol):
    def read_head(self) -> tuple[bytes | None, str | None]: ...
    def write_head(self, raw: bytes, etag: str | None) -> None: ...
    def read_record(self, key: str) -> bytes | None: ...
    def create_record(self, key: str, raw: bytes) -> None: ...
    def read_pointer(self) -> bytes: ...
    def create_receipt(self, key: str, raw: bytes) -> None: ...
    def read_receipt(self, key: str) -> bytes | None: ...


def _json(raw: bytes) -> dict:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_JSON:
        raise ValueError("invalid or oversized transition JSON")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid transition JSON") from error
    if not isinstance(value, dict) or search_snapshot.canonical(value) != raw:
        raise ValueError("transition JSON is not a canonical object")
    return value


def _exact(value: object, fields: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("invalid transition schema")
    return value


def _digest(value: object, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise ValueError("invalid transition digest")
    return value


def _id(value: object) -> str:
    if not isinstance(value, str) or not HEX32.fullmatch(value):
        raise ValueError("invalid transition identifier")
    return value


def _pointer(value: object) -> dict:
    if not isinstance(value, dict):
        raise TypeError("invalid expected parent pointer")
    return decode_pointer(search_snapshot.canonical(value))


def _evidence(value: object) -> None:
    obj = _exact(value, {"generation", "receipt_sha256"})
    _generation(obj["generation"])
    _digest(obj["receipt_sha256"])


def validate_head(raw: bytes) -> dict:
    value = _exact(_json(raw), {"version", "kind", "sequence", "record", "transition"})
    if (type(value["version"]) is not int or value["version"] != 1
            or value["kind"] != "search-transition-head" or type(value["sequence"]) is not int
            or value["sequence"] < 0):
        raise ValueError("invalid transition head")
    _digest(value["record"])
    _id(value["transition"])
    return value


def validate_record(raw: bytes) -> dict:
    fields = {"version", "kind", "sequence", "transition", "prior", "prior_transition", "state", "parent_pointer",
              "candidate", "source_revisions_sha256", "observed", "defaults", "pending"}
    value = _exact(_json(raw), fields)
    if (type(value["version"]) is not int or value["version"] != 1
            or value["kind"] != "search-transition-record" or type(value["sequence"]) is not int
            or value["sequence"] < 0 or value["sequence"] > 1_000_000
            or not isinstance(value["state"], str) or value["state"] not in STATES):
        raise ValueError("invalid transition record")
    _id(value["transition"])
    _digest(value["prior"], optional=True)
    if value["prior_transition"] is not None:
        _id(value["prior_transition"])
    parent = _pointer(value["parent_pointer"])
    _generation(value["candidate"])
    if parent["desired"] == value["candidate"]:
        raise ValueError("transition candidate equals parent")
    if parent["transition"] == value["transition"]:
        raise ValueError("transition identity equals parent pointer identity")
    _digest(value["source_revisions_sha256"])
    observed = _exact(value["observed"], {"pages", "space"})
    for site in SITES:
        _evidence(observed[site])
        if observed[site]["generation"] not in {parent["desired"], value["candidate"]}:
            raise ValueError("site observation is outside this transition")
    defaults = _exact(value["defaults"], {"pages", "space"})
    for site in SITES:
        _evidence(defaults[site])
        if defaults[site]["generation"] not in {parent["desired"], value["candidate"]}:
            raise ValueError("default observation is outside this transition")
    pending = value["pending"]
    if pending is not None:
        pending = _exact(pending, {"site", "operation", "operation_id"})
        if (not isinstance(pending["site"], str) or pending["site"] not in SITES
                or not isinstance(pending["operation"], str)
                or pending["operation"] not in {"apply", "rollback", "set-default"}):
            raise ValueError("invalid pending site operation")
        expected_state = {"apply": "applying", "rollback": "rolling-back", "set-default": "pointer-observed"}
        if value["state"] != expected_state[pending["operation"]]:
            raise ValueError("pending operation does not match transition state")
        _id(pending["operation_id"])
        target = parent["desired"] if pending["operation"] == "rollback" else value["candidate"]
        evidence = defaults if pending["operation"] == "set-default" else observed
        if evidence[pending["site"]]["generation"] == target:
            raise ValueError("pending operation already observed its target")
    if value["state"] == "prepared" and any(
            observed[s]["generation"] != parent["desired"] or defaults[s]["generation"] != parent["desired"]
            for s in SITES):
        raise ValueError("prepared transition must start from the parent")
    if (value["state"] in {"verified", "publishing", "pointer-observed", "complete"}
            and any(observed[site]["generation"] != value["candidate"] for site in SITES)):
        raise ValueError("candidate is not observed by both consumers")
    if (value["state"] == "rolled-back"
            and any(observed[site]["generation"] != parent["desired"] for site in SITES)):
        raise ValueError("parent is not observed by both consumers")
    if value["state"] not in {"pointer-observed", "complete"} and any(
            defaults[s]["generation"] != parent["desired"] for s in SITES):
        raise ValueError("site defaults changed before pointer publication")
    if value["state"] == "complete" and any(defaults[s]["generation"] != value["candidate"] for s in SITES):
        raise ValueError("candidate is not configured as both site defaults")
    if ((value["sequence"] == 0) != (value["prior"] is None)
            or (value["prior"] is None) != (value["prior_transition"] is None)):
        raise ValueError("invalid transition predecessor")
    return value


def record_key(value: dict, raw: bytes) -> str:
    return f"{RECORD_ROOT}{value['transition']}/{hashlib.sha256(raw).hexdigest()}.json"


def validate_advance(previous: dict, value: dict, *, prior_digest: str) -> None:
    """Validate a successor independently of the command that proposed it."""
    if (value["sequence"] != previous["sequence"] + 1 or value["prior"] != prior_digest
            or value["prior_transition"] != previous["transition"]):
        raise ValueError("transition predecessor changed")
    common = {"version", "kind", "transition", "parent_pointer", "candidate", "source_revisions_sha256"}
    if value["transition"] != previous["transition"]:
        expected = (previous["candidate"] if previous["state"] == "complete"
                    else previous["parent_pointer"]["desired"])
        if (previous["state"] not in {"complete", "rolled-back"} or value["state"] != "prepared"
                or value["parent_pointer"]["desired"] != expected):
            raise ValueError("invalid next transition")
        if previous["state"] == "rolled-back" and value["parent_pointer"] != previous["parent_pointer"]:
            raise ValueError("rolled-back parent identity changed")
        if previous["state"] == "complete" and (
                value["parent_pointer"]["transition"] != previous["transition"]
                or value["parent_pointer"]["previous"] != previous["parent_pointer"]["desired"]):
            raise ValueError("completed parent identity changed")
        return
    if any(value[k] != previous[k] for k in common):
        raise ValueError("transition immutable fields changed")
    a, b = previous["state"], value["state"]
    before, after = previous["observed"], value["observed"]
    defaults_before, defaults_after = previous["defaults"], value["defaults"]
    old_pending, new_pending = previous["pending"], value["pending"]
    if old_pending is None and new_pending is not None:
        allowed = {"apply": {"prepared", "applying"},
                   "rollback": {"prepared", "applying", "verified", "rolling-back"},
                   "set-default": {"pointer-observed"}}[new_pending["operation"]]
        if a not in allowed or before != after or defaults_before != defaults_after:
            raise ValueError("invalid site-operation intent")
        return
    if old_pending is not None and new_pending is None:
        site = old_pending["site"]
        if old_pending["operation"] == "set-default":
            if (b != "pointer-observed" or before != after or defaults_after[site]["generation"] != value["candidate"]
                    or any(defaults_after[s] != defaults_before[s] for s in SITES - {site})):
                raise ValueError("invalid default-configuration observation")
            return
        target = value["candidate"] if old_pending["operation"] == "apply" else value["parent_pointer"]["desired"]
        expected_state = ("verified" if all(after[s]["generation"] == target for s in SITES) else "applying") \
            if old_pending["operation"] == "apply" else (
                "rolled-back" if all(after[s]["generation"] == target for s in SITES) else "rolling-back")
        if (after[site]["generation"] != target or any(after[s] != before[s] for s in SITES - {site})
                or defaults_before != defaults_after or b != expected_state):
            raise ValueError("invalid site-operation observation")
        return
    if old_pending is None and new_pending is None:
        if (a, b) == ("verified", "publishing") and defaults_before == defaults_after and all(
                after[s]["generation"] == before[s]["generation"] == value["candidate"] for s in SITES):
            return
        if (a, b) == ("publishing", "pointer-observed") and before == after and defaults_before == defaults_after:
            return
        if ((a, b) == ("pointer-observed", "complete") and before == after and defaults_before == defaults_after
                and all(defaults_after[s]["generation"] == value["candidate"] for s in SITES)):
            return
    raise ValueError("invalid transition state change")


def _store_receipt(store: TransitionStore, transition: str, receipt: bytes) -> str:
    if not isinstance(receipt, bytes) or not 0 < len(receipt) <= MAX_JSON:
        raise ValueError("invalid consumer receipt bytes")
    digest = hashlib.sha256(receipt).hexdigest()
    key = f"{RECORD_ROOT}{_id(transition)}/receipts/{digest}.json"
    previous = store.read_receipt(key)
    if previous is None:
        try:
            store.create_receipt(key, receipt)
        except Exception:
            previous = store.read_receipt(key)
            if previous != receipt:
                raise
        else:
            previous = store.read_receipt(key)
    if previous != receipt:
        raise ValueError("transition immutable receipt differs")
    return digest


def _observed(generation: str, digest: str) -> dict:
    _generation(generation)
    _digest(digest)
    return {"generation": generation, "receipt_sha256": digest}


def _check_default(raw: bytes, *, site: str, generation: str) -> None:
    value = _exact(_json(raw), {"version", "kind", "site", "generation", "observed_at", "details"})
    if (type(value["version"]) is not int or value["version"] != 1
            or value["kind"] != "search-default-observation" or value["site"] != site
            or value["generation"] != generation or not isinstance(value["observed_at"], str)
            or not value["observed_at"] or not isinstance(value["details"], dict)):
        raise ValueError("default receipt identity does not match the site and generation")


def _check_receipt(raw: bytes, *, site: str, generation: str) -> None:
    value = _exact(_json(raw), {"version", "kind", "site", "generation", "records", "observed_at", "details"})
    if (type(value["version"]) is not int or value["version"] != 1
            or value["kind"] != "search-consumer-observation" or value["site"] != site
            or value["generation"] != generation or type(value["records"]) is not int or value["records"] < 1
            or not isinstance(value["observed_at"], str) or not value["observed_at"]
            or not isinstance(value["details"], dict)):
        raise ValueError("consumer receipt identity does not match the site and generation")


@dataclass(frozen=True)
class Current:
    head: dict
    head_raw: bytes
    etag: str
    record: dict
    record_raw: bytes


def load(store: TransitionStore) -> Current | None:
    raw, etag = store.read_head()
    if raw is None:
        if etag is not None:
            raise ValueError("absent transition head has a validator")
        return None
    strong_etag(etag)
    head = validate_head(raw)
    key = f"{RECORD_ROOT}{head['transition']}/{head['record']}.json"
    record_raw = store.read_record(key)
    if record_raw is None or hashlib.sha256(record_raw).hexdigest() != head["record"]:
        raise ValueError("transition head points to a missing or altered record")
    record = validate_record(record_raw)
    if record["transition"] != head["transition"] or record["sequence"] != head["sequence"]:
        raise ValueError("transition head does not match its record")
    _verify_receipts(store, record, error="transition observation receipt is missing or altered")
    if record["prior"] is not None:
        prior_key = f"{RECORD_ROOT}{record['prior_transition']}/{record['prior']}.json"
        prior_raw = store.read_record(prior_key)
        if prior_raw is None or hashlib.sha256(prior_raw).hexdigest() != record["prior"]:
            raise ValueError("transition predecessor is missing or altered")
        prior = validate_record(prior_raw)
        validate_advance(prior, record, prior_digest=record["prior"])
    return Current(head, raw, etag, record, record_raw)


def audit_history(store: TransitionStore, current: Current, *, max_records: int = 10_000) -> int:
    """Verify all linked records and receipts; a cap is never treated as complete."""
    if type(max_records) is not int or not 1 <= max_records <= 1_000_000:
        raise ValueError("invalid history audit limit")
    record, count = current.record, 0
    while True:
        count += 1
        if count > max_records:
            raise ValueError("transition history exceeds audit limit")
        _verify_receipts(store, record, error="transition history has a missing or altered receipt")
        if record["prior"] is None:
            if record["sequence"] != 0 or record["state"] != "prepared":
                raise ValueError("invalid first transition record")
            return count
        key = f"{RECORD_ROOT}{record['prior_transition']}/{record['prior']}.json"
        raw = store.read_record(key)
        if raw is None or hashlib.sha256(raw).hexdigest() != record["prior"]:
            raise ValueError("transition history has a missing or altered record")
        previous = validate_record(raw)
        validate_advance(previous, record, prior_digest=record["prior"])
        record = previous


def _verify_receipts(store: TransitionStore, record: dict, *, error: str) -> None:
    for field, check in (("observed", _check_receipt), ("defaults", _check_default)):
        for site, evidence in record[field].items():
            key = f"{RECORD_ROOT}{record['transition']}/receipts/{evidence['receipt_sha256']}.json"
            raw = store.read_receipt(key)
            if raw is None or hashlib.sha256(raw).hexdigest() != evidence["receipt_sha256"]:
                raise ValueError(error)
            check(raw, site=site, generation=evidence["generation"])


def _put_record(store: TransitionStore, raw: bytes) -> str:
    value = validate_record(raw)
    key = record_key(value, raw)
    old = store.read_record(key)
    if old is None:
        try:
            store.create_record(key, raw)
        except Exception:
            old = store.read_record(key)
            if old != raw:
                raise
        else:
            old = store.read_record(key)
    if old != raw:
        raise ValueError("transition immutable record differs")
    return hashlib.sha256(raw).hexdigest()


def _commit(store: TransitionStore, value: dict, current: Current | None) -> Current:
    raw = search_snapshot.canonical(value)
    validate_record(raw)
    if current is None:
        if value["sequence"] != 0 or value["state"] != "prepared":
            raise ValueError("first transition must be prepared")
    else:
        validate_advance(current.record, value, prior_digest=current.head["record"])
    digest = _put_record(store, raw)
    head = {"version": 1, "kind": "search-transition-head", "sequence": value["sequence"],
            "transition": value["transition"], "record": digest}
    head_raw = search_snapshot.canonical(head)
    validate_head(head_raw)
    try:
        store.write_head(head_raw, current.etag if current else None)
    except Exception:
        # Lost responses must be reconciled by exact bytes, never replayed.
        result = load(store)
        if result is not None and result.head_raw == head_raw:
            return result
        raise
    result = load(store)
    if result is None or result.head_raw != head_raw:
        raise RuntimeError("transition head readback differs")
    return result


def begin(store: TransitionStore, *, parent_pointer: bytes, candidate: str, source_revisions_sha256: str,
          pages_receipt: bytes, space_receipt: bytes, pages_default: bytes, space_default: bytes,
          transition: str | None = None) -> Current:
    previous = load(store)
    if previous is not None and previous.record["state"] not in {"complete", "rolled-back"}:
        raise ValueError("transition journal has unfinished work; inspect before beginning")
    if previous is not None:
        audit_history(store, previous)
    parent = decode_pointer(parent_pointer)
    if store.read_pointer() != parent_pointer:
        raise ValueError("actual pointer differs from expected parent")
    if previous is not None:
        expected = (previous.record["candidate"] if previous.record["state"] == "complete"
                    else previous.record["parent_pointer"]["desired"])
        if parent["desired"] != expected:
            raise ValueError("parent differs from the preceding terminal transition")
        if previous.record["state"] == "complete" and (
                parent["transition"] != previous.record["transition"]
                or parent["previous"] != previous.record["parent_pointer"]["desired"]):
            raise ValueError("completed transition pointer identity differs")
        if previous.record["state"] == "rolled-back" and parent != previous.record["parent_pointer"]:
            raise ValueError("rolled-back transition parent pointer differs")
    _generation(candidate)
    if candidate == parent["desired"]:
        raise ValueError("transition candidate equals parent")
    _digest(source_revisions_sha256)
    transition = _id(transition or uuid.uuid4().hex)
    if parent["transition"] == transition or (previous is not None and previous.record["transition"] == transition):
        raise ValueError("new transition must have a different identity")
    _check_receipt(pages_receipt, site="pages", generation=parent["desired"])
    _check_receipt(space_receipt, site="space", generation=parent["desired"])
    _check_default(pages_default, site="pages", generation=parent["desired"])
    _check_default(space_default, site="space", generation=parent["desired"])
    pages_digest = _store_receipt(store, transition, pages_receipt)
    space_digest = _store_receipt(store, transition, space_receipt)
    pages_default_digest = _store_receipt(store, transition, pages_default)
    space_default_digest = _store_receipt(store, transition, space_default)
    value = {"version": 1, "kind": "search-transition-record",
             "sequence": previous.record["sequence"] + 1 if previous else 0,
             "transition": transition,
             "prior": previous.head["record"] if previous else None,
             "prior_transition": previous.head["transition"] if previous else None, "state": "prepared",
             "parent_pointer": parent, "candidate": candidate, "source_revisions_sha256": source_revisions_sha256,
             "observed": {"pages": _observed(parent["desired"], pages_digest),
                          "space": _observed(parent["desired"], space_digest)},
             "defaults": {"pages": _observed(parent["desired"], pages_default_digest),
                          "space": _observed(parent["desired"], space_default_digest)}, "pending": None}
    return _commit(store, value, previous)


def _next(current: Current, **changes) -> dict:
    return {**current.record, **changes, "sequence": current.record["sequence"] + 1,
            "prior": current.head["record"], "prior_transition": current.head["transition"]}


def start_site(store: TransitionStore, current: Current, *, site: str, operation_id: str,
               rollback: bool = False) -> Current:
    if site not in SITES:
        raise ValueError("invalid site")
    _id(operation_id)
    state = current.record["state"]
    allowed = {"prepared", "applying", "verified", "rolling-back"} if rollback else {"prepared", "applying"}
    if state not in allowed or current.record["pending"] is not None:
        raise ValueError("transition is not ready for a site operation")
    target = current.record["parent_pointer"]["desired"] if rollback else current.record["candidate"]
    if current.record["observed"][site]["generation"] == target:
        raise ValueError("site already has the requested observed generation")
    if store.read_pointer() != search_snapshot.canonical(current.record["parent_pointer"]):
        raise ValueError("actual pointer changed before site operation")
    pending = {"site": site, "operation": "rollback" if rollback else "apply", "operation_id": operation_id}
    return _commit(store, _next(current, state="rolling-back" if rollback else "applying", pending=pending), current)


def observe_site(store: TransitionStore, current: Current, *, site: str, generation: str, receipt: bytes) -> Current:
    pending = current.record["pending"]
    if not isinstance(pending, dict) or pending["site"] != site or pending["operation"] not in {"apply", "rollback"}:
        raise ValueError("site has no matching pending operation")
    expected = current.record["candidate"] if pending["operation"] == "apply" else current.record["parent_pointer"]["desired"]
    if generation != expected:
        raise ValueError("actual site generation does not match pending operation")
    if store.read_pointer() != search_snapshot.canonical(current.record["parent_pointer"]):
        raise ValueError("actual pointer changed before site observation")
    _check_receipt(receipt, site=site, generation=generation)
    digest = _store_receipt(store, current.record["transition"], receipt)
    observed = {**current.record["observed"], site: _observed(generation, digest)}
    if pending["operation"] == "apply":
        state = "verified" if all(observed[s]["generation"] == expected for s in SITES) else "applying"
    else:
        state = "rolled-back" if all(observed[s]["generation"] == expected for s in SITES) else "rolling-back"
    return _commit(store, _next(current, observed=observed, pending=None, state=state), current)


def mark_publishing(store: TransitionStore, current: Current, *, pages_receipt: bytes, space_receipt: bytes) -> Current:
    if current.record["state"] != "verified":
        raise ValueError("both sites must be verified before pointer publication")
    if store.read_pointer() != search_snapshot.canonical(current.record["parent_pointer"]):
        raise ValueError("actual pointer changed before pointer publication")
    candidate = current.record["candidate"]
    _check_receipt(pages_receipt, site="pages", generation=candidate)
    _check_receipt(space_receipt, site="space", generation=candidate)
    observed = {"pages": _observed(candidate, _store_receipt(store, current.record["transition"], pages_receipt)),
                "space": _observed(candidate, _store_receipt(store, current.record["transition"], space_receipt))}
    return _commit(store, _next(current, state="publishing", observed=observed), current)


def publish_pointer(store: TransitionStore, pointer_store, current: Current, *,
                    pages_receipt: bytes, space_receipt: bytes) -> Current:
    """Publish only after fresh observations; uncertain writes are read, never replayed."""
    if current.record["state"] != "publishing":
        raise ValueError("transition is not publishing")
    active = load(store)
    if active is None or active.head_raw != current.head_raw or active.etag != current.etag:
        raise ValueError("transition head changed before pointer publication")
    audit_history(store, current)
    candidate = current.record["candidate"]
    _check_receipt(pages_receipt, site="pages", generation=candidate)
    _check_receipt(space_receipt, site="space", generation=candidate)
    parent = search_snapshot.canonical(current.record["parent_pointer"])
    intended = make_pointer(candidate, current.record["parent_pointer"]["desired"], current.record["transition"])
    if store.read_pointer() != parent:
        raise ValueError("actual pointer differs from expected parent; inspect before resuming")
    apply_pointer(pointer_store, desired=candidate, expected=current.record["parent_pointer"]["desired"],
                  transition=current.record["transition"], expected_pointer=parent)
    return record_pointer(store, current, observed_pointer=intended,
                          pages_receipt=pages_receipt, space_receipt=space_receipt)


def record_pointer(store: TransitionStore, current: Current, *, observed_pointer: bytes,
                   pages_receipt: bytes, space_receipt: bytes) -> Current:
    if current.record["state"] != "publishing":
        raise ValueError("transition is not publishing")
    active = load(store)
    if active is None or active.head_raw != current.head_raw or active.etag != current.etag:
        raise ValueError("transition head changed before pointer observation")
    audit_history(store, current)
    _check_receipt(pages_receipt, site="pages", generation=current.record["candidate"])
    _check_receipt(space_receipt, site="space", generation=current.record["candidate"])
    if store.read_pointer() != observed_pointer:
        raise ValueError("observed pointer is not the current pointer")
    pointer = decode_pointer(observed_pointer)
    if (pointer["desired"] != current.record["candidate"]
            or pointer["previous"] != current.record["parent_pointer"]["desired"]
            or pointer["transition"] != current.record["transition"]):
        raise ValueError("actual pointer does not match publication intent")
    return _commit(store, _next(current, state="pointer-observed"), current)


def start_default(store: TransitionStore, current: Current, *, site: str, operation_id: str) -> Current:
    if site not in SITES or current.record["state"] != "pointer-observed" or current.record["pending"] is not None:
        raise ValueError("transition is not ready for a default-configuration operation")
    _id(operation_id)
    if current.record["defaults"][site]["generation"] == current.record["candidate"]:
        raise ValueError("site already has the requested observed default")
    intended = make_pointer(current.record["candidate"], current.record["parent_pointer"]["desired"],
                            current.record["transition"])
    if store.read_pointer() != intended:
        raise ValueError("actual pointer changed before default-configuration operation")
    pending = {"site": site, "operation": "set-default", "operation_id": operation_id}
    return _commit(store, _next(current, pending=pending), current)


def observe_default(store: TransitionStore, current: Current, *, site: str, receipt: bytes) -> Current:
    pending = current.record["pending"]
    if (current.record["state"] != "pointer-observed" or not isinstance(pending, dict)
            or pending["site"] != site or pending["operation"] != "set-default"):
        raise ValueError("site has no matching pending default-configuration operation")
    candidate = current.record["candidate"]
    intended = make_pointer(candidate, current.record["parent_pointer"]["desired"], current.record["transition"])
    if store.read_pointer() != intended:
        raise ValueError("actual pointer changed before default observation")
    _check_default(receipt, site=site, generation=candidate)
    digest = _store_receipt(store, current.record["transition"], receipt)
    defaults = {**current.record["defaults"], site: _observed(candidate, digest)}
    return _commit(store, _next(current, defaults=defaults, pending=None), current)


def finish(store: TransitionStore, current: Current, *, pages_receipt: bytes, space_receipt: bytes,
           pages_default: bytes, space_default: bytes) -> Current:
    if current.record["state"] != "pointer-observed" or current.record["pending"] is not None:
        raise ValueError("transition is not ready for completion")
    audit_history(store, current)
    candidate = current.record["candidate"]
    intended = make_pointer(candidate, current.record["parent_pointer"]["desired"], current.record["transition"])
    if store.read_pointer() != intended:
        raise ValueError("actual pointer changed before completion")
    for site, actual, configured in (("pages", pages_receipt, pages_default),
                                     ("space", space_receipt, space_default)):
        _check_receipt(actual, site=site, generation=candidate)
        _check_default(configured, site=site, generation=candidate)
        if current.record["defaults"][site]["generation"] != candidate:
            raise ValueError("site default has not been durably observed")
    return _commit(store, _next(current, state="complete"), current)


class S3TransitionStore:
    """CAS adapter limited to the transition head and content-addressed records."""
    def __init__(self, store=None):
        if store is None:
            from botocore.config import Config
            store = S3BucketStore()
            store._config = store._config.merge(Config(
                retries={"total_max_attempts": 1}, request_checksum_calculation="when_required",
                response_checksum_validation="when_required"))
        self.store = store

    def _read(self, key: str) -> bytes | None:
        try:
            raw = self.store.read_bytes(BUCKET, key)
        except S3NotFound:
            return None
        if not isinstance(raw, bytes) or len(raw) > MAX_JSON:
            raise ValueError("oversized transition object")
        return raw

    def read_pointer(self) -> bytes:
        raw = self._read("published/current.json")
        if raw is None:
            raise ValueError("search publication pointer is missing")
        decode_pointer(raw)
        return raw

    def read_head(self) -> tuple[bytes | None, str | None]:
        raw = self._read(HEAD_KEY)
        return (raw, strong_etag(self.store.read_etags.get((BUCKET, HEAD_KEY)))) if raw is not None else (None, None)

    def write_head(self, raw: bytes, etag: str | None) -> None:
        validate_head(raw)
        namespace, name = self.store._location(BUCKET)
        if (namespace, name) != ("vomebook", "search-data-v1"):
            raise ValueError("unexpected search bucket mapping")
        condition = {"IfNoneMatch": "*"} if etag is None else {"IfMatch": strong_etag(etag)}
        self.store._client(namespace).put_object(Bucket=name, Key=HEAD_KEY, Body=raw,
                                                  ContentType="application/json", **condition)

    @staticmethod
    def _check_record(key: str) -> None:
        if not re.fullmatch(r"published/transitions/[0-9a-f]{32}/[0-9a-f]{64}\.json", key):
            raise ValueError("invalid transition record key")

    def read_record(self, key: str) -> bytes | None:
        self._check_record(key)
        return self._read(key)

    def _create(self, key: str, raw: bytes) -> None:
        namespace, name = self.store._location(BUCKET)
        if (namespace, name) != ("vomebook", "search-data-v1"):
            raise ValueError("unexpected search bucket mapping")
        self.store._client(namespace).put_object(Bucket=name, Key=key, Body=raw,
                                                  ContentType="application/json", IfNoneMatch="*")

    def create_record(self, key: str, raw: bytes) -> None:
        self._check_record(key)
        value = validate_record(raw)
        if key != record_key(value, raw):
            raise ValueError("transition record key differs from bytes")
        self._create(key, raw)

    @staticmethod
    def _check_receipt(key: str) -> None:
        if not re.fullmatch(r"published/transitions/[0-9a-f]{32}/receipts/[0-9a-f]{64}\.json", key):
            raise ValueError("invalid transition receipt key")

    def read_receipt(self, key: str) -> bytes | None:
        self._check_receipt(key)
        return self._read(key)

    def create_receipt(self, key: str, raw: bytes) -> None:
        self._check_receipt(key)
        if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_JSON:
            raise ValueError("invalid transition receipt bytes")
        if hashlib.sha256(raw).hexdigest() != key.rsplit("/", 1)[-1].removesuffix(".json"):
            raise ValueError("transition receipt key differs from bytes")
        self._create(key, raw)
