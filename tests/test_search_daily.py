"""The daily guard never equates a desired pointer with loaded consumers."""
import io
import json
import unittest
import urllib.error
from unittest.mock import MagicMock, call, patch

from scripts import search_daily
from scripts.search_publication_pointer import make_pointer
from tests.test_search_adopt import SearchAdoptionTests
from tests.test_search_artifact_store import MemoryStore


class BoundedGetTests(unittest.TestCase):
    url = search_daily.BUCKET_RESOLVE + "published/current.json"

    def response(self, status=200, raw=b"{}"):
        response = MagicMock(status=status)
        response.__enter__.return_value = response
        response.read.return_value = raw
        return response

    def test_http_503_recovers_on_third_attempt(self):
        errors = [urllib.error.HTTPError(self.url, 503, "unavailable", {}, io.BytesIO())
                  for _ in range(2)]
        response = self.response()
        with patch.object(search_daily.urllib.request, "urlopen", side_effect=[*errors, response]) as get, \
                patch.object(search_daily.time, "sleep") as sleep:
            self.assertEqual(search_daily.bounded_get(self.url, 2), b"{}")
        self.assertEqual(get.call_count, 3)
        self.assertEqual(get.call_args_list, [get.call_args_list[0]] * 3)
        self.assertEqual(get.call_args.args[0].get_method(), "GET")
        self.assertEqual(get.call_args.kwargs, {"timeout": 40})
        response.read.assert_called_once_with(3)
        self.assertTrue(all(error.fp.closed for error in errors))
        self.assertEqual(sleep.call_args_list, [call(0.5), call(1.0)])

    def test_http_503_exhaustion_preserves_final_error(self):
        errors = [urllib.error.HTTPError(self.url, 503, "unavailable", {}, io.BytesIO())
                  for _ in range(3)]
        with patch.object(search_daily.urllib.request, "urlopen", side_effect=errors) as get, \
                patch.object(search_daily.time, "sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                search_daily.bounded_get(self.url)
        self.assertIs(raised.exception, errors[-1])
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_args_list, [call(0.5), call(1.0)])
        errors[-1].close()

    def test_permanent_http_errors_are_not_retried(self):
        for status in (400, 401, 403, 404, 422, 501):
            error = urllib.error.HTTPError(self.url, status, "rejected", {}, io.BytesIO())
            with self.subTest(status=status), \
                    patch.object(search_daily.urllib.request, "urlopen", side_effect=error) as get, \
                    patch.object(search_daily.time, "sleep") as sleep:
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    search_daily.bounded_get(self.url)
                self.assertIs(raised.exception, error)
                get.assert_called_once()
                sleep.assert_not_called()
            error.close()

    def test_oversized_response_is_not_retried(self):
        response = self.response(raw=b"1234")
        with patch.object(search_daily.urllib.request, "urlopen", return_value=response) as get, \
                patch.object(search_daily.time, "sleep") as sleep:
            with self.assertRaisesRegex(ValueError, "oversized search response"):
                search_daily.bounded_get(self.url, 3)
        get.assert_called_once()
        response.read.assert_called_once_with(4)
        response.__exit__.assert_called_once()
        sleep.assert_not_called()

    def test_only_exact_200_is_accepted(self):
        for status in (201, 204, 206, 302):
            response = self.response(status=status)
            with self.subTest(status=status), \
                    patch.object(search_daily.urllib.request, "urlopen", return_value=response) as get, \
                    patch.object(search_daily.time, "sleep") as sleep:
                with self.assertRaisesRegex(RuntimeError, f"search metadata HTTP {status}"):
                    search_daily.bounded_get(self.url)
                get.assert_called_once()
                response.read.assert_not_called()
                sleep.assert_not_called()

    def test_returned_503_response_is_also_bounded(self):
        response = self.response(status=503)
        with patch.object(search_daily.urllib.request, "urlopen", return_value=response) as get, \
                patch.object(search_daily.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "search metadata HTTP 503"):
                search_daily.bounded_get(self.url)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(response.__exit__.call_count, 3)
        response.read.assert_not_called()
        self.assertEqual(sleep.call_args_list, [call(0.5), call(1.0)])


class SearchDailyTests(SearchAdoptionTests):
    def urls_for_published(self):
        urls = dict(self.urls)
        urls[search_daily.BUCKET_RESOLVE + "published/current.json"] = make_pointer(self.generation, None, "1" * 32)
        urls[search_daily.BUCKET_RESOLVE + f"generations/{self.generation}/manifest.json"] = self.hub.objects[
            f"generations/{self.generation}/manifest.json"]
        return urls

    def test_both_consumers_must_acknowledge_pointer(self):
        urls = self.urls_for_published()
        result = search_daily.inspect_published(get=lambda url, _limit: urls[url])
        self.assertEqual(result["generation"], self.generation)
        self.assertTrue(result["both_consumers_observed"])
        self.assertEqual(result["record_count"], 1)
        for url in (search_daily.PAGES_MANIFEST, search_daily.SPACE_RECEIPT):
            saved = urls[url]
            altered = json.loads(saved)
            altered["generation"] = "b" * 64
            urls[url] = compact(altered)
            with self.subTest(url=url), self.assertRaises(ValueError):
                search_daily.inspect_published(get=lambda value, _limit: urls[value])
            urls[url] = saved

    def test_pointer_or_manifest_unavailable_never_reports_synced(self):
        urls = self.urls_for_published()
        with self.assertRaises(KeyError):
            search_daily.inspect_published(get=lambda _url, _limit: (_ for _ in ()).throw(KeyError("not found")))
        key = search_daily.BUCKET_RESOLVE + f"generations/{self.generation}/manifest.json"
        value = json.loads(urls[key])
        value["record_count"] = 123
        urls[key] = compact(value)
        with self.assertRaisesRegex(ValueError, "identity mismatch"):
            search_daily.inspect_published(get=lambda url, _limit: urls[url])

    def test_bad_pages_corpus_or_initial_payload_never_counts_as_observed(self):
        urls = self.urls_for_published()
        for suffix in ("/data/search_data.json.gz", "/data/initial/global.json"):
            url = search_daily.PAGES_MANIFEST.removesuffix("/data/generation-manifest.json") + suffix
            before = urls[url]
            urls[url] = b"{}"
            with self.subTest(url=url), self.assertRaises(ValueError):
                search_daily.inspect_published(get=lambda value, _limit: urls[value])
            urls[url] = before

    def test_pointer_changing_during_observation_is_rejected(self):
        urls = self.urls_for_published()
        pointer_url = search_daily.BUCKET_RESOLVE + "published/current.json"
        reads = []

        def get(url, _limit):
            if url == pointer_url:
                reads.append(url)
                if len(reads) == 2:
                    return make_pointer(self.generation, None, "2" * 32)
            return urls[url]

        with self.assertRaisesRegex(ValueError, "pointer changed"):
            search_daily.inspect_published(get=get)
        self.assertEqual(len(reads), 2)

    def test_receipt_and_baseline_are_written_exclusively_after_verification(self):
        urls = self.urls_for_published()
        baseline = self.root / "baseline.json"
        receipt = self.root / "receipt.json"
        expected = search_daily.inspect_published(get=lambda url, _limit: urls[url])
        with patch.object(search_daily, "inspect_published", return_value=expected):
            self.assertEqual(search_daily.main(["inspect", "--baseline-output", str(baseline),
                                               "--receipt-output", str(receipt)]), 0)
            self.assertEqual(json.loads(baseline.read_text()), self.manifest["source_revisions"])
            self.assertEqual(json.loads(receipt.read_text()), expected)
            self.assertEqual(search_daily.main(["inspect", "--expected-receipt", str(receipt)]), 0)
            with self.assertRaises(FileExistsError):
                search_daily.main(["inspect", "--baseline-output", str(baseline)])
        changed = {**expected, "transition": "2" * 32}
        with patch.object(search_daily, "inspect_published", return_value=changed), \
                self.assertRaisesRegex(ValueError, "changed during the run"):
            search_daily.main(["inspect", "--expected-receipt", str(receipt)])

    def test_failed_consumer_verification_never_writes_baseline(self):
        baseline = self.root / "baseline.json"
        with patch.object(search_daily, "inspect_published", side_effect=ValueError("no consumer")), \
                self.assertRaisesRegex(ValueError, "no consumer"):
            search_daily.main(["inspect", "--baseline-output", str(baseline)])
        self.assertFalse(baseline.exists())

    def test_stage_does_not_write_mutable_pointer(self):
        store = MemoryStore()
        self.root.joinpath("second").mkdir()
        source, tree, browser, revisions = [self.root / n for n in ("source", "tree", "browser", "revisions")]
        result = search_daily.stage(source, tree, browser, revisions, self.root / "second/output", store)
        self.assertTrue(result["staged"])
        self.assertFalse(result["pointer_written"])
        self.assertFalse(result["consumers_verified"])
        self.assertNotIn("published/current.json", store.files)


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


if __name__ == "__main__":
    unittest.main()
