# SPDX-License-Identifier: GPL-3.0-or-later
import http.client
import io
import json
import unittest

from smoke import MAX_STATS_RESPONSE_BYTES, STATS_SUBMIT_FIELDS, decode_stats, read_stats


def fixture():
    return {
        "schema_version": 1,
        "instance_id": "0123456789abcdef0123456789abcdef",
        "created_at": "2026-11-10T00:00:01Z",
        # A clock step backward is legal descriptive metadata.
        "observed_at": "2026-11-10T00:00:00.123456789Z",
        "submit": dict.fromkeys(STATS_SUBMIT_FIELDS, 0),
        "http": {"concurrency_rejected_total": 0},
    }


class StatsSmokeDecoderTest(unittest.TestCase):
    def test_valid_schema_and_exact_large_integers(self):
        value = fixture()
        self.assertEqual(decode_stats(json.dumps(value)), value)
        for large in ((1 << 53) + 1, (1 << 64) - 1):
            value["submit"]["accepted_cached_retry_total"] = large
            self.assertEqual(decode_stats(json.dumps(value)), value)

    def test_missing_extra_null_fields_fail(self):
        mutations = []
        for key in fixture():
            value = fixture()
            del value[key]
            mutations.append(value)
            value = fixture()
            value[key] = None
            mutations.append(value)
        value = fixture()
        value["extra"] = 0
        mutations.append(value)
        value = fixture()
        value["submit"]["extra"] = 0
        mutations.append(value)
        value = fixture()
        del value["submit"]["other_total"]
        mutations.append(value)
        for value in mutations:
            with self.subTest(value=value), self.assertRaises(ValueError):
                decode_stats(json.dumps(value))

    def test_invalid_counter_types_and_ranges_fail(self):
        for wrong in (True, False, None, "0", 0.0, -1, 1 << 64, float("nan"), float("inf")):
            for group, key in (("submit", "other_total"), ("http", "concurrency_rejected_total")):
                value = fixture()
                value[group][key] = wrong
                with self.subTest(wrong=wrong, group=group), self.assertRaises(ValueError):
                    decode_stats(json.dumps(value))

    def test_invalid_version_identity_or_time_fail(self):
        for key, wrong in (
            ("schema_version", 2), ("schema_version", True), ("schema_version", "1"),
            ("instance_id", "short"), ("instance_id", "A" * 32),
            ("created_at", "0001-01-01T00:00:00Z"),
            ("observed_at", "2026-11-10T00:00:00+00:00"),
            ("observed_at", "2026-99-10T00:00:00Z"),
        ):
            value = fixture()
            value[key] = wrong
            with self.subTest(key=key, wrong=wrong), self.assertRaises(ValueError):
                decode_stats(json.dumps(value))

    def test_duplicate_truncated_trailing_or_nonobject_json_fail(self):
        original = json.dumps(fixture())
        cases = [original[:-1], original + " {}", "[]", "null", "{}",
                 original[:-1] + ', "schema_version": 1}',
                 original.replace('"other_total": 0', '"other_total": 0, "other_total": 0')]
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                decode_stats(payload)


class MemorySocket:
    """Only HTTPResponse's socket makefile surface; no network is opened."""
    def __init__(self, wire):
        self.wire = wire

    def makefile(self, mode):
        assert mode == "rb"
        return io.BytesIO(self.wire)


class MemoryOpener:
    def __init__(self, body, framing=()):
        headers = ["HTTP/1.1 200 OK", "Content-Type: application/json",
                   "Cache-Control: no-store", "X-Content-Type-Options: nosniff",
                   "Connection: close", *framing]
        self.wire = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body

    def open(self, url, timeout):
        assert url == "http://127.0.0.1/v1/stats" and timeout == 2
        response = http.client.HTTPResponse(MemorySocket(self.wire))
        response.begin()
        return response


class StatsSmokeHTTPFramingTest(unittest.TestCase):
    def setUp(self):
        self.value = fixture()
        self.body = json.dumps(self.value).encode("ascii")

    def read(self, body, framing=()):
        return read_stats(MemoryOpener(body, framing), "http://127.0.0.1")

    def test_real_http_response_valid_length_and_eof(self):
        for framing in ((), (f"Content-Length: {len(self.body)}",),
                        (f"Content-Length: \t00{len(self.body)} \t",)):
            with self.subTest(framing=framing):
                self.assertEqual(self.read(self.body, framing), self.value)

    def test_valid_json_short_content_length_body_is_rejected(self):
        # Regression: HTTPResponse bounded read returns this complete valid JSON
        # despite the wire closing ten bytes before the declared body length.
        with self.assertRaisesRegex(ValueError, "incomplete.*Content-Length"):
            self.read(self.body, (f"Content-Length: {len(self.body) + 10}",))

    def test_invalid_or_oversized_length_is_rejected(self):
        for length in ("-1", f"+{len(self.body)}", "", "oops", "1, 1", "1.0",
                       str(MAX_STATS_RESPONSE_BYTES + 1), "9" * 100):
            with self.subTest(length=length), self.assertRaises(ValueError):
                self.read(self.body, (f"Content-Length: {length}",))

    def test_duplicate_lengths_and_transfer_coding_are_rejected(self):
        length = len(self.body)
        cases = [
            (f"Content-Length: {length}", f"content-length: {length}"),
            (f"Content-Length: {length}", f"Content-Length: {length + 1}"),
            (f"Content-Length: {length}", "Transfer-Encoding: chunked"),
            (f"Content-Length: {length}", "Transfer-Encoding:"),
            ("Transfer-Encoding: chunked",),
            ("Transfer-Encoding: identity",),
        ]
        for framing in cases:
            with self.subTest(framing=framing), self.assertRaises(ValueError):
                self.read(self.body, framing)

    def test_bounded_declared_and_eof_body_edges(self):
        at_bound = self.body + b" " * (MAX_STATS_RESPONSE_BYTES - len(self.body))
        for framing in ((), (f"Content-Length: {MAX_STATS_RESPONSE_BYTES}",)):
            with self.subTest(framing=framing):
                self.assertEqual(self.read(at_bound, framing), self.value)
        with self.assertRaises(AssertionError):
            self.read(at_bound + b" ")

    def test_zero_length_does_not_default_to_zero_counters(self):
        with self.assertRaises(ValueError):
            self.read(b"", ("Content-Length: 0",))

    def test_matching_length_does_not_hide_invalid_json(self):
        for body in (self.body[:-1], self.body + b" {}", b"null"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.read(body, (f"Content-Length: {len(body)}",))
