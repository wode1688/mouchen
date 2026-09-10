"""Exercise the relay API with a separate configuration and database per test."""

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class RelayTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.tokens = {
            name: "test-only-credential-" + name
            for name in ("desktop", "phone", "third")
        }
        config = {
            "data_dir": str(self.root / "data"),
            "quota_bytes": 1024,
            "max_file_bytes": 512,
            "minimum_free_bytes": 0,
            "completed_record_limit": 2,
            "devices": {
                name: hashlib.sha256(token.encode()).hexdigest()
                for name, token in self.tokens.items()
            },
        }
        config_path = self.root / "config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        spec = importlib.util.spec_from_file_location(
            "isolated_relay_server", Path(__file__).resolve().parents[1] / "server.py"
        )
        self.server = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {"RELAY_CONFIG": str(config_path)}):
            spec.loader.exec_module(self.server)
        self.client = self.enterContext(TestClient(self.server.app))

    def headers(self, who):
        return {"Authorization": "Bearer " + self.tokens[who]}

    def reserve(self, data=b"example", targets=None, key="request-one"):
        body = {
            "filename": "example.txt",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "targets": ["phone"] if targets is None else targets,
            "idempotency_key": key,
        }
        return self.client.post(
            "/v1/transfers", headers=self.headers("desktop"), json=body
        )

    def ready(self, data=b"example", targets=None, key="request-one"):
        response = self.reserve(data, targets, key)
        self.assertEqual(response.status_code, 200, response.text)
        item = response.json()
        response = self.client.put(
            f"/v1/transfers/{item['id']}/content",
            headers=self.headers("desktop"),
            content=data,
        )
        self.assertEqual(response.status_code, 200, response.text)
        return item

    def receive(self, item, who="phone"):
        url = f"/v1/transfers/{item['id']}"
        claim = self.client.post(url + "/claim", headers=self.headers(who))
        self.assertEqual(claim.status_code, 200, claim.text)
        token = claim.json()["lease_token"]
        body = self.client.get(
            url + "/content",
            headers=self.headers(who) | {"X-Claim-Token": token},
        )
        self.assertEqual(body.status_code, 200, body.text)
        self.assertEqual(hashlib.sha256(body.content).hexdigest(), item["sha256"])
        return {"lease_token": token, "sha256": item["sha256"]}

    def test_auth_and_recipient_isolation(self):
        self.assertEqual(self.client.get("/v1/inbox").status_code, 401)
        self.assertEqual(
            self.client.get(
                "/v1/inbox", headers={"Authorization": "Bearer invalid-test-token"}
            ).status_code,
            401,
        )
        item = self.ready()
        url = f"/v1/transfers/{item['id']}"
        self.assertEqual(
            self.client.get("/v1/inbox", headers=self.headers("third")).json()["items"],
            [],
        )
        self.assertEqual(
            self.client.get(url, headers=self.headers("third")).status_code, 404
        )
        self.assertEqual(
            self.client.post(url + "/claim", headers=self.headers("third")).status_code,
            404,
        )

    def test_only_source_can_upload(self):
        item = self.reserve().json()
        url = f"/v1/transfers/{item['id']}/content"
        for who in ("phone", "third"):
            with self.subTest(device=who):
                response = self.client.put(url, headers=self.headers(who), content=b"example")
                self.assertEqual(response.status_code, 404)
                self.assertFalse((self.server.BLOBS / item["id"]).exists())
        response = self.client.put(url, headers=self.headers("desktop"), content=b"example")
        self.assertEqual(response.status_code, 200, response.text)

    def test_ack_only_after_download_and_checksum_match(self):
        item = self.ready()
        url = f"/v1/transfers/{item['id']}"
        token = self.client.post(url + "/claim", headers=self.headers("phone")).json()["lease_token"]
        receipt = {"lease_token": token, "sha256": item["sha256"]}
        self.assertEqual(
            self.client.post(url + "/ack", headers=self.headers("phone"), json=receipt).status_code,
            409,
        )
        self.client.get(url + "/content", headers=self.headers("phone") | {"X-Claim-Token": token})
        self.assertEqual(
            self.client.post(
                url + "/ack", headers=self.headers("phone"), json=receipt | {"sha256": "0" * 64}
            ).status_code,
            422,
        )
        self.assertTrue((self.server.BLOBS / item["id"]).exists())
        result = self.client.post(url + "/ack", headers=self.headers("phone"), json=receipt)
        self.assertEqual(result.json()["state"], "delivered")
        self.assertFalse((self.server.BLOBS / item["id"]).exists())
        self.assertEqual(
            self.client.post(url + "/ack", headers=self.headers("phone"), json=receipt).status_code,
            200,
        )

    def test_multiple_recipients_prevent_early_delete(self):
        item = self.ready(targets=["phone", "third"])
        for who in ("phone", "third"):
            receipt = self.receive(item, who)
            response = self.client.post(
                f"/v1/transfers/{item['id']}/ack", headers=self.headers(who), json=receipt
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual((self.server.BLOBS / item["id"]).exists(), who == "phone")

    def test_bad_and_short_upload_retry(self):
        item = self.reserve().json()
        url = f"/v1/transfers/{item['id']}/content"
        for data in (b"bad", b"EXAMPLE", b"too-long-content"):
            with self.subTest(data=data):
                response = self.client.put(url, headers=self.headers("desktop"), content=data)
                self.assertEqual(response.status_code, 413 if len(data) > 7 else 422)
                self.assertFalse((self.server.BLOBS / (item["id"] + ".part")).exists())
        self.assertEqual(
            self.client.put(url, headers=self.headers("desktop"), content=b"example").status_code,
            200,
        )

    def test_declared_size_quota_and_deduplication(self):
        first = self.reserve(b"x" * 512)
        same = self.reserve(b"x" * 512)
        self.assertEqual(first.json()["id"], same.json()["id"])
        self.assertEqual(self.reserve(b"y" * 512).status_code, 409)
        self.assertEqual(self.reserve(b"x" * 512, key="request-two").status_code, 200)
        self.assertEqual(self.reserve(b"x", key="request-three").status_code, 507)
        self.assertEqual(self.reserve(b"x" * 513, key="oversized-file").status_code, 422)

    def test_expiry_and_stale_lease(self):
        item = self.ready()
        receipt = self.receive(item)
        with self.server.db(write=True) as connection:
            connection.execute("UPDATE deliveries SET lease_until=0 WHERE transfer_id=?", (item["id"],))
        self.assertEqual(
            self.client.post(
                f"/v1/transfers/{item['id']}/ack", headers=self.headers("phone"), json=receipt
            ).status_code,
            409,
        )
        self.assertEqual(
            len(self.client.get("/v1/inbox", headers=self.headers("phone")).json()["items"]), 1
        )
        with self.server.db(write=True) as connection:
            connection.execute("UPDATE transfers SET expires_at=0 WHERE id=?", (item["id"],))
        self.server.cleanup()
        self.assertFalse((self.server.BLOBS / item["id"]).exists())
        self.assertEqual(
            self.client.get("/v1/inbox", headers=self.headers("phone")).json()["items"], []
        )

    def test_metadata_bounds_and_filename_traversal(self):
        item = {
            "filename": "../bad", "size": 1, "sha256": "0" * 64,
            "targets": ["phone"], "idempotency_key": "valid-key",
        }
        for filename in ("../bad", "..\\bad", ".", "..", "bad\x00name"):
            with self.subTest(filename=filename):
                response = self.client.post(
                    "/v1/transfers", headers=self.headers("desktop"), json=item | {"filename": filename}
                )
                self.assertEqual(response.status_code, 422)
        self.assertEqual(
            self.client.post(
                "/v1/transfers", headers=self.headers("desktop"), content=b"x" * 17000
            ).status_code,
            413,
        )
        self.assertEqual(self.reserve(targets=["unknown-device"]).status_code, 422)

    def test_release_requires_matching_recipient_and_lease(self):
        item = self.ready(targets=["phone", "third"])
        receipt = self.receive(item)
        url = f"/v1/transfers/{item['id']}"
        for who, status in (("desktop", 404), ("third", 409)):
            with self.subTest(device=who):
                response = self.client.post(url + "/release", headers=self.headers(who), json=receipt)
                self.assertEqual(response.status_code, status)
        wrong_receipt = receipt | {"lease_token": "invalid-test-lease-token"}
        self.assertEqual(
            self.client.post(url + "/release", headers=self.headers("phone"), json=wrong_receipt).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(url + "/release", headers=self.headers("phone"), json=receipt).status_code,
            200,
        )
        items = self.client.get("/v1/inbox", headers=self.headers("phone")).json()["items"]
        self.assertEqual([entry["id"] for entry in items], [item["id"]])
        self.assertEqual(
            self.client.get(
                url + "/content", headers=self.headers("phone") | {"X-Claim-Token": receipt["lease_token"]}
            ).status_code,
            409,
        )
        self.assertTrue((self.server.BLOBS / item["id"]).exists())

    def test_empty_file(self):
        item = self.ready(b"")
        receipt = self.receive(item)
        response = self.client.post(
            f"/v1/transfers/{item['id']}/ack", headers=self.headers("phone"), json=receipt
        )
        self.assertEqual(response.json()["state"], "delivered")

    def test_completed_history_cap_preserves_pending_files(self):
        pending = self.ready(key="keep-pending")
        done = []
        for number in range(3):
            item = self.ready(key="complete-" + str(number))
            receipt = self.receive(item)
            response = self.client.post(
                f"/v1/transfers/{item['id']}/ack", headers=self.headers("phone"), json=receipt
            )
            self.assertEqual(response.status_code, 200, response.text)
            done.append(item["id"])
        self.server.cleanup()
        with self.server.db() as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM transfers WHERE state='delivered'").fetchone()[0], 2
            )
            self.assertIsNone(
                connection.execute("SELECT id FROM transfers WHERE id=?", (done[0],)).fetchone()
            )
        self.assertTrue((self.server.BLOBS / pending["id"]).exists())


if __name__ == "__main__":
    unittest.main()
