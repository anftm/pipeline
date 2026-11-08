"""Durable promotion intents do not infer deployment, recovery, or writer fencing."""
import hashlib
import unittest
from unittest.mock import Mock

from scripts import search_snapshot
from scripts import search_transition as tx
from scripts.search_publication_pointer import make_pointer

A, B, C = "a" * 64, "b" * 64, "c" * 64
T1, T2 = "1" * 32, "2" * 32
REV, WHEN = "d" * 40, "2026-11-08T00:00:00Z"


def receipt(site, generation, tag="observation"):
    return search_snapshot.canonical({"version": 1, "kind": "search-consumer-observation", "site": site,
                                      "generation": generation, "records": 1,
                                      "observed_at": "2026-01-01T00:00:00Z", "details": {"tag": tag}})


def default_receipt(site, generation):
    return search_snapshot.canonical({"version": 1, "kind": "search-default-observation", "site": site,
                                      "generation": generation, "observed_at": "2026-01-01T00:00:00Z",
                                      "details": {"variable": "SEARCH_DATA_GENERATION"}})


class MemoryStore:
    def __init__(self):
        self.head = None
        self.etag = None
        self.pointer_etag = '"pointer0"'
        self.pointer_writes = 0
        self.lose_pointer_response = False
        self.records = {}
        self.receipts = {}
        self.pointer = make_pointer(A, None, "0" * 32)
        self.writes = 0
        self.creates = 0
        self.fail_head = None
        self.fail_record = None

    def read_head(self):
        return self.head, self.etag

    def read_pointer(self):
        return self.pointer

    def read_for_update(self):
        return self.pointer, self.pointer_etag

    def compare_and_swap(self, *, expected_etag, raw):
        self.pointer_writes += 1
        if expected_etag != self.pointer_etag:
            raise ValueError("pointer CAS conflict")
        self.pointer, self.pointer_etag = raw, f'"pointer{self.pointer_writes}"'
        if self.lose_pointer_response:
            raise TimeoutError("lost pointer response")

    def read_record(self, key):
        return self.records.get(key)

    def read_receipt(self, key):
        return self.receipts.get(key)

    def create_receipt(self, key, raw):
        if key in self.receipts:
            raise ValueError("duplicate receipt")
        self.receipts[key] = raw

    def create_record(self, key, raw):
        self.creates += 1
        if key in self.records:
            raise ValueError("duplicate")
        if self.fail_record == "before":
            raise TimeoutError("before create")
        self.records[key] = raw
        if self.fail_record == "after":
            raise TimeoutError("after create")

    def write_head(self, raw, etag):
        self.writes += 1
        if etag != self.etag:
            raise ValueError("CAS conflict")
        if self.fail_head == "before":
            raise TimeoutError("before CAS")
        self.head = raw
        self.etag = f'"{self.writes}"'
        if self.fail_head == "after":
            raise TimeoutError("after CAS")


class SearchTransitionTests(unittest.TestCase):
    def setUp(self):
        self.s = MemoryStore()

    def begin(self):
        return tx.begin(self.s, parent_pointer=self.s.pointer, candidate=B, source_revisions_sha256=C,
                        pages_receipt=receipt("pages", A, "parent"), space_receipt=receipt("space", A, "parent"),
                        pages_default=default_receipt("pages", A), space_default=default_receipt("space", A),
                        transition=T1)

    def observe(self, current, site, generation, tag):
        return tx.observe_site(self.s, current, site=site, generation=generation,
                               receipt=receipt(site, generation, tag.decode()))

    def start_site(self, current, **kwargs):
        if kwargs.get("site") == "space":
            kwargs["config_before"] = {"generation": B if kwargs.get("rollback") else A, "updated_at": WHEN}
        return tx.start_site(self.s, current, expected_revision=REV, created_at=WHEN, **kwargs)

    def start_default(self, current, **kwargs):
        return tx.start_default(self.s, current, expected_revision=REV, created_at=WHEN, **kwargs)

    def ready(self):
        c = self.begin()
        c = self.start_site(c, site="pages", operation_id="3" * 32)
        c = self.observe(c, "pages", B, b"pages-new")
        c = self.start_site(c, site="space", operation_id="4" * 32)
        return self.observe(c, "space", B, b"space-new")

    def test_full_transition_requires_each_intent_observation_and_exact_pointer(self):
        c = self.ready()
        self.assertEqual(c.record["state"], "verified")
        self.assertEqual(c.record["sequence"], 4)
        pages_receipt = receipt("pages", B, "pages-new")
        digest = hashlib.sha256(pages_receipt).hexdigest()
        self.assertEqual(c.record["observed"]["pages"]["receipt_sha256"], digest)
        self.assertEqual(self.s.receipts[f"{tx.RECORD_ROOT}{T1}/receipts/{digest}.json"], pages_receipt)
        c = tx.mark_publishing(self.s, c, pages_receipt=receipt("pages", B, "fresh"),
                               space_receipt=receipt("space", B, "fresh"))
        self.assertEqual(c.record["state"], "publishing")
        intended = make_pointer(B, A, T1)
        with self.assertRaisesRegex(ValueError, "not the current pointer"):
            tx.record_pointer(self.s, c, observed_pointer=intended,
                              pages_receipt=receipt("pages", B), space_receipt=receipt("space", B))
        self.s.pointer = intended
        c = tx.record_pointer(self.s, c, observed_pointer=intended,
                              pages_receipt=receipt("pages", B), space_receipt=receipt("space", B))
        self.assertEqual(c.record["state"], "pointer-observed")
        with self.assertRaisesRegex(ValueError, "durably observed"):
            tx.finish(self.s, c, pages_receipt=receipt("pages", B), space_receipt=receipt("space", B),
                      pages_default=default_receipt("pages", B), space_default=default_receipt("space", B))
        for site in ("pages", "space"):
            c = self.start_default(c, site=site, operation_id="7" * 32)
            c = tx.observe_default(self.s, c, site=site, receipt=default_receipt(site, B))
        c = tx.finish(self.s, c, pages_receipt=receipt("pages", B), space_receipt=receipt("space", B),
                      pages_default=default_receipt("pages", B), space_default=default_receipt("space", B))
        self.assertEqual(c.record["state"], "complete")
        self.assertEqual(c.record["sequence"], 11)
        self.assertEqual(tx.load(self.s), c)
        self.assertEqual(tx.audit_history(self.s, c), 12)
        with self.assertRaisesRegex(ValueError, "audit limit"):
            tx.audit_history(self.s, c, max_records=11)
        nxt = tx.begin(self.s, parent_pointer=self.s.pointer, candidate=C, source_revisions_sha256=A,
                       pages_receipt=receipt("pages", B, "parent2"), space_receipt=receipt("space", B, "parent2"),
                       pages_default=default_receipt("pages", B), space_default=default_receipt("space", B),
                       transition=T2)
        self.assertEqual(nxt.record["prior"], c.head["record"])
        self.assertEqual(nxt.record["sequence"], 12)
        self.assertEqual(tx.audit_history(self.s, nxt), 13)
        self.assertNotEqual(nxt.record["transition"], c.record["transition"])

    def test_pointer_write_lost_response_is_read_back_without_replay(self):
        c = self.ready()
        c = tx.mark_publishing(self.s, c, pages_receipt=receipt("pages", B), space_receipt=receipt("space", B))
        self.s.lose_pointer_response = True
        done = tx.publish_pointer(self.s, self.s, c, pages_receipt=receipt("pages", B, "fresh"),
                                  space_receipt=receipt("space", B, "fresh"))
        self.assertEqual(done.record["state"], "pointer-observed")
        self.assertEqual(self.s.pointer_writes, 1)
        self.assertEqual(self.s.pointer, make_pointer(B, A, T1))

    def test_resume_after_pointer_write_does_not_write_it_again(self):
        c = self.ready()
        c = tx.mark_publishing(self.s, c, pages_receipt=receipt("pages", B), space_receipt=receipt("space", B))
        self.s.pointer = make_pointer(B, A, T1)
        with self.assertRaisesRegex(ValueError, "inspect before resuming"):
            tx.publish_pointer(self.s, self.s, c, pages_receipt=receipt("pages", B, "fresh"),
                               space_receipt=receipt("space", B, "fresh"))
        self.assertEqual(self.s.pointer_writes, 0)
        # Reconciliation observes the exact intended pointer; it does not replay the write.
        done = tx.record_pointer(self.s, c, observed_pointer=self.s.pointer,
                                 pages_receipt=receipt("pages", B), space_receipt=receipt("space", B))
        self.assertEqual(done.record["state"], "pointer-observed")

    def test_default_update_uncertain_requires_exact_observation_before_next_action(self):
        c = self.ready()
        c = tx.mark_publishing(self.s, c, pages_receipt=receipt("pages", B), space_receipt=receipt("space", B))
        c = tx.publish_pointer(self.s, self.s, c, pages_receipt=receipt("pages", B), space_receipt=receipt("space", B))
        c = self.start_default(c, site="pages", operation_id="8" * 32)
        self.assertEqual(tx.load(self.s).record["pending"]["operation"], "set-default")
        with self.assertRaisesRegex(ValueError, "not ready"):
            self.start_default(c, site="space", operation_id="9" * 32)
        with self.assertRaisesRegex(ValueError, "default receipt identity"):
            tx.observe_default(self.s, c, site="pages", receipt=default_receipt("pages", A))
        with self.assertRaisesRegex(ValueError, "matching pending"):
            self.observe(c, "pages", B, b"not-a-default")
        c = tx.observe_default(self.s, c, site="pages", receipt=default_receipt("pages", B))
        self.assertEqual(c.record["defaults"]["pages"]["generation"], B)
        self.assertEqual(c.record["defaults"]["space"]["generation"], A)
        self.assertEqual(c.record["state"], "pointer-observed")
        with self.assertRaisesRegex(ValueError, "unfinished work"):
            tx.begin(self.s, parent_pointer=self.s.pointer, candidate=C, source_revisions_sha256=A,
                     pages_receipt=receipt("pages", B), space_receipt=receipt("space", B),
                     pages_default=default_receipt("pages", B), space_default=default_receipt("space", B))

    def test_invalid_fresh_receipt_or_foreign_pointer_never_writes_pointer(self):
        c = self.ready()
        c = tx.mark_publishing(self.s, c, pages_receipt=receipt("pages", B), space_receipt=receipt("space", B))
        with self.assertRaisesRegex(ValueError, "receipt identity"):
            tx.publish_pointer(self.s, self.s, c, pages_receipt=receipt("pages", A),
                               space_receipt=receipt("space", B))
        self.s.pointer = make_pointer(A, None, T2)
        with self.assertRaisesRegex(ValueError, "differs from expected parent"):
            tx.publish_pointer(self.s, self.s, c, pages_receipt=receipt("pages", B),
                               space_receipt=receipt("space", B))
        self.assertEqual(self.s.pointer_writes, 0)

    def test_cannot_publish_or_deploy_next_site_without_observed_receipt(self):
        c = self.begin()
        with self.assertRaisesRegex(ValueError, "both sites"):
            tx.mark_publishing(self.s, c, pages_receipt=receipt("pages", B), space_receipt=receipt("space", B))
        c = self.start_site(c, site="space", operation_id="3" * 32)
        with self.assertRaisesRegex(ValueError, "not ready"):
            self.start_site(c, site="pages", operation_id="4" * 32)
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.observe(c, "space", A, b"old")
        with self.assertRaisesRegex(ValueError, "unfinished work"):
            self.begin()
        self.assertEqual(tx.load(self.s), c)

    def test_recovery_then_explicit_rollback_for_one_or_both_sites(self):
        c = self.ready()
        for site, op in (("space", "5" * 32), ("pages", "6" * 32)):
            c = self.start_site(c, site=site, operation_id=op, rollback=True)
            self.assertEqual(c.record["state"], "rolling-back")
            c = self.observe(c, site, A, (site + "-rollback").encode())
        self.assertEqual(c.record["state"], "rolled-back")
        self.assertEqual(self.s.pointer, make_pointer(A, None, "0" * 32))
        nxt = tx.begin(self.s, parent_pointer=self.s.pointer, candidate=C, source_revisions_sha256=B,
                       pages_receipt=receipt("pages", A), space_receipt=receipt("space", A),
                       pages_default=default_receipt("pages", A), space_default=default_receipt("space", A),
                       transition=T2)
        self.assertEqual(nxt.record["sequence"], c.record["sequence"] + 1)

    def test_uncertain_remote_action_requires_observation_before_rollback(self):
        c = self.begin()
        c = self.start_site(c, site="space", operation_id="3" * 32)
        with self.assertRaisesRegex(ValueError, "not ready"):
            self.start_site(c, site="space", operation_id="4" * 32, rollback=True)
        c = self.observe(c, "space", B, b"actual-response")
        c = self.start_site(c, site="space", operation_id="5" * 32, rollback=True)
        c = self.observe(c, "space", A, b"rollback-response")
        self.assertEqual(c.record["state"], "rolled-back")

    def test_immutable_and_cas_lost_response_are_not_replayed(self):
        self.s.fail_record = "after"
        self.s.fail_head = "after"
        c = self.begin()
        self.assertEqual(c.record["state"], "prepared")
        self.assertEqual(self.s.creates, 1)
        self.assertEqual(self.s.writes, 1)
        self.s.fail_head = "before"
        with self.assertRaises(TimeoutError):
            self.start_site(c, site="pages", operation_id="3" * 32)
        self.assertEqual(self.s.writes, 2)
        self.assertEqual(tx.load(self.s), c)

    def test_stale_cas_does_not_erase_other_operation(self):
        initial = self.begin()
        live = self.start_site(initial, site="pages", operation_id="3" * 32)
        with self.assertRaisesRegex(ValueError, "CAS conflict"):
            self.start_site(initial, site="space", operation_id="4" * 32)
        self.assertEqual(tx.load(self.s), live)

    def test_pointer_changes_abort_before_site_write_or_publication(self):
        c = self.begin()
        self.s.pointer = make_pointer(C, A, T2)
        with self.assertRaisesRegex(ValueError, "pointer changed"):
            self.start_site(c, site="pages", operation_id="3" * 32)
        self.assertEqual(tx.load(self.s), c)

    def test_exact_parent_and_actual_pointer_required(self):
        with self.assertRaisesRegex(ValueError, "actual pointer"):
            tx.begin(self.s, parent_pointer=make_pointer(A, None, T2), candidate=B, source_revisions_sha256=C,
                     pages_receipt=receipt("pages", A), space_receipt=receipt("space", A),
                     pages_default=default_receipt("pages", A), space_default=default_receipt("space", A))
        self.assertEqual(self.s.writes, 0)
        with self.assertRaisesRegex(ValueError, "candidate equals parent"):
            tx.begin(self.s, parent_pointer=self.s.pointer, candidate=A, source_revisions_sha256=C,
                     pages_receipt=receipt("pages", A), space_receipt=receipt("space", A),
                     pages_default=default_receipt("pages", A), space_default=default_receipt("space", A))
        self.assertEqual(self.s.writes, 0)

    def test_forged_transition_and_corrupt_records_are_rejected(self):
        c = self.begin()
        self.s.records[tx.record_key(c.record, c.record_raw)] = b"corrupt"
        with self.assertRaisesRegex(ValueError, "altered record"):
            tx.load(self.s)
        with self.assertRaises(ValueError):
            tx.validate_head(b"{}")
        with self.assertRaises(ValueError):
            tx.validate_record(b"not json")
        with self.assertRaises(ValueError):
            tx.validate_record(search_snapshot.canonical({**c.record, "state": []}))
        with self.assertRaisesRegex(ValueError, "state change"):
            tx.validate_advance(c.record, {**c.record, "state": "complete", "sequence": 1,
                                          "prior": c.head["record"], "prior_transition": T1},
                                prior_digest=c.head["record"])

    def test_missing_predecessor_or_receipt_fails_closed(self):
        first = self.begin()
        current = self.start_site(first, site="pages", operation_id="3" * 32)
        prior_key = tx.record_key(first.record, first.record_raw)
        prior = self.s.records.pop(prior_key)
        with self.assertRaisesRegex(ValueError, "predecessor"):
            tx.load(self.s)
        self.s.records[prior_key] = prior
        self.assertEqual(tx.load(self.s), current)
        digest = current.record["observed"]["pages"]["receipt_sha256"]
        key = f"{tx.RECORD_ROOT}{T1}/receipts/{digest}.json"
        self.s.receipts[key] = b"corrupt"
        with self.assertRaisesRegex(ValueError, "receipt is missing or altered"):
            tx.load(self.s)

    def test_full_history_audit_detects_older_tampering(self):
        first = self.begin()
        current = self.start_site(first, site="pages", operation_id="3" * 32)
        current = self.observe(current, "pages", B, b"pages-new")
        self.assertEqual(tx.audit_history(self.s, current), 3)
        self.s.records.pop(tx.record_key(first.record, first.record_raw))
        # Current and direct predecessor remain readable; the full chain is not.
        self.assertEqual(tx.load(self.s), current)
        with self.assertRaisesRegex(ValueError, "missing or altered record"):
            tx.audit_history(self.s, current)

    def test_operation_intent_requires_revision_time_and_space_config_snapshot(self):
        c = self.begin()
        for revision, timestamp in (("main", WHEN), (REV, "yesterday"), (REV, "2026-11-08T00:00:00")):
            with self.subTest(revision=revision, timestamp=timestamp), self.assertRaises(ValueError):
                tx.start_site(self.s, c, site="pages", operation_id="3" * 32,
                              expected_revision=revision, created_at=timestamp)
        with self.assertRaises(ValueError):
            tx.start_site(self.s, c, site="space", operation_id="3" * 32,
                          expected_revision=REV, created_at=WHEN)
        with self.assertRaisesRegex(ValueError, "configured generation differs"):
            tx.start_site(self.s, c, site="space", operation_id="3" * 32,
                          expected_revision=REV, created_at=WHEN,
                          config_before={"generation": B, "updated_at": WHEN})
        current = self.start_site(c, site="space", operation_id="3" * 32)
        self.assertEqual(current.record["pending"]["expected_revision"], REV)
        self.assertEqual(current.record["pending"]["created_at"], WHEN)
        self.assertEqual(current.record["pending"]["config_before"]["generation"], A)

    def test_s3_adapter_confines_keys_and_conditions(self):
        backing = Mock()
        backing._location.return_value = ("vomebook", "search-data-v1")
        backing._client.return_value = Mock()
        s3 = tx.S3TransitionStore(backing)
        head = search_snapshot.canonical({"version": 1, "kind": "search-transition-head", "sequence": 0,
                                          "record": A, "transition": T1})
        s3.write_head(head, None)
        kwargs = backing._client.return_value.put_object.call_args.kwargs
        self.assertEqual(kwargs["Key"], tx.HEAD_KEY)
        self.assertEqual(kwargs["IfNoneMatch"], "*")
        s3.write_head(head, '"etag"')
        self.assertEqual(backing._client.return_value.put_object.call_args.kwargs["IfMatch"], '"etag"')
        with self.assertRaises(ValueError):
            s3.write_head(head, 'W/"weak"')
        with self.assertRaises(ValueError):
            s3.read_record("published/current.json")
        with self.assertRaises(ValueError):
            s3.create_record(f"published/transitions/{T1}/{A}.json", b"{}")
        with self.assertRaises(ValueError):
            s3.create_receipt(f"published/transitions/{T1}/receipts/{A}.json", b"wrong")
        with self.assertRaises(ValueError):
            s3.read_receipt("published/current.json")


if __name__ == "__main__":
    unittest.main()
