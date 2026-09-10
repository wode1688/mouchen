"""Test archive and receipt behavior without contacting a relay."""

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from client import Relay, digest_file, write_json


class OfflineRelay(Relay):
    """A deterministic relay transport with no URL or credentials."""

    def __init__(self, content=b"archived example"):
        self.content = content
        self.claim = {
            "id": "a" * 32,
            "source": "desktop",
            "filename": "example.txt",
            "project": "ai-twin",
            "category": "files",
            "created_at": 1704067200,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "lease_token": "test-only-lease-token-123456789",
            "state": "ready",
        }
        self.calls = []
        self.before_ack = None
        self.ack_error = None

    def request(self, method, path, value=None):
        self.calls.append((method, path, value))
        if path == "/v1/inbox":
            return {"items": [{"id": self.claim["id"]}]}
        if path.endswith("/claim"):
            return dict(self.claim)
        if path.endswith("/ack"):
            if self.before_ack:
                self.before_ack()
            if self.ack_error:
                raise self.ack_error
            return {"state": "delivered"}
        if path.endswith("/release"):
            return {"released": True}
        raise AssertionError(f"Unexpected offline request: {method} {path}")

    def open(self, method, path, body=None, headers=None):
        self.calls.append((method, path, headers))
        if method != "GET" or not path.endswith("/content"):
            raise AssertionError(f"Unexpected offline download: {method} {path}")
        return io.BytesIO(self.content)


class ClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.archive = Path(temporary.name).resolve() / "archive"
        self.relay = OfflineRelay()

    def archived_file(self):
        return self.archive / "ai-twin" / "files" / "2024-01-01" / ("a" * 32 + "_example.txt")

    def receipt_file(self):
        return self.archived_file().with_name("a" * 32 + ".receipt.json")

    def test_saves_verified_content_and_receipt_before_ack(self):
        def assert_saved():
            self.assertEqual(self.archived_file().read_bytes(), self.relay.content)
            self.assertEqual(digest_file(self.archived_file()), self.relay.claim["sha256"])
            manifest = json.loads(self.receipt_file().read_text(encoding="utf-8"))
            self.assertNotIn("lease_token", manifest)
            self.assertEqual(manifest["sha256"], self.relay.claim["sha256"])
            self.assertIn("archived_at", manifest)

        self.relay.before_ack = assert_saved
        received = self.relay.pull(self.archive)
        self.assertEqual(received, [{
            "file": str(self.archived_file()),
            "id": self.relay.claim["id"],
            "server_state": "delivered",
        }])
        self.assertFalse(list(self.archive.rglob("*.part")))

    def test_corrupt_download_releases_without_acknowledging(self):
        self.relay.content = b"incorrect digest"
        with self.assertRaisesRegex(ValueError, "checksum verification"):
            self.relay.pull(self.archive)
        paths = [call[1] for call in self.relay.calls]
        self.assertTrue(paths[-1].endswith("/release"))
        self.assertFalse(any(path.endswith("/ack") for path in paths))
        self.assertFalse(self.archived_file().exists())
        self.assertFalse(self.receipt_file().exists())
        self.assertFalse(list(self.archive.rglob("*.part")))

    def test_saved_copy_survives_failed_ack_and_retry(self):
        self.relay.ack_error = OSError("simulated offline receipt")
        with self.assertRaisesRegex(OSError, "simulated offline receipt"):
            self.relay.pull(self.archive)
        self.assertEqual(self.archived_file().read_bytes(), self.relay.content)
        self.assertTrue(self.receipt_file().exists())
        self.assertTrue(self.relay.calls[-1][1].endswith("/release"))

        self.relay.ack_error = None
        self.relay.calls.clear()
        received = self.relay.pull(self.archive)
        self.assertEqual(len(received), 1)
        self.assertEqual(len(list(self.archive.rglob("*_example.txt"))), 1)
        # The retry must complete a download under its new lease before receipt.
        paths = [call[1] for call in self.relay.calls]
        content = next(i for i, path in enumerate(paths) if path.endswith("/content"))
        ack = next(i for i, path in enumerate(paths) if path.endswith("/ack"))
        self.assertLess(content, ack)

    def test_untrusted_archive_category_cannot_escape_archive(self):
        self.relay.claim["project"] = "../outside"
        with self.assertRaisesRegex(ValueError, "Invalid archive category"):
            self.relay.pull(self.archive)
        paths = [call[1] for call in self.relay.calls]
        self.assertTrue(paths[-1].endswith("/release"))
        self.assertFalse(any(path.endswith("/content") or path.endswith("/ack") for path in paths))
        self.assertFalse((self.archive.parent / "outside").exists())

    def test_json_writer_preserves_unicode_and_replaces_existing_file(self):
        path = self.archive / "settings.json"
        write_json(path, {"name": "first"})
        write_json(path, {"name": "示例"})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"name": "示例"})
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_pause_before_next_claim_leaves_file_in_cloud(self):
        received = self.relay.pull(self.archive, should_stop=lambda: True)
        self.assertEqual(received, [])
        self.assertFalse(any(call[1].endswith('/claim') for call in self.relay.calls))

    def test_client_rejects_insecure_or_credential_bearing_urls(self):
        for url in (
            "http://relay.example.invalid",
            "https://user:password@relay.example.invalid",
            "https://relay.example.invalid?secret=value",
            "https://relay.example.invalid#fragment",
            "https:///",
            "https://relay.example.invalid/unexpected-path",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                Relay({"url": url, "token": "test-only-token"})


if __name__ == "__main__":
    unittest.main()
