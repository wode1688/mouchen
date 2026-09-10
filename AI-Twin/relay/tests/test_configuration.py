"""Check that each computer resolves its own configuration and storage paths."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import configuration


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        # Use the real platform's path semantics; isolate both platform defaults.
        self.enterContext(patch.dict(os.environ, {
            "LOCALAPPDATA": str(self.root / "current-user-data"),
            "XDG_DATA_HOME": str(self.root / "current-user-data"),
            "AI_TWIN_RELAY_CONFIG": "",
        }))

    def write_config(self, content, encoding="utf-8"):
        path = self.root / "configuration" / "client-config.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(content, ensure_ascii=False), encoding=encoding)
        return path

    def test_default_paths_belong_to_current_user(self):
        expected = self.root / "current-user-data" / "Mouchen" / "Relay"
        self.assertEqual(configuration.default_state_dir(), expected)
        self.assertEqual(configuration.default_client_config_path(), expected / "client-config.json")
        path = self.write_config({"url": "https://relay.example.invalid", "token": "test-only-token"})
        config = configuration.load_client_config(path)
        self.assertEqual(Path(config["state_dir"]), expected)
        self.assertEqual(Path(config["archive_dir"]), expected / "Archive")
        self.assertEqual(Path(config["outbox_dir"]), expected / "Outbox")
        self.assertEqual(
            Path(config["source_database"]),
            self.root / "current-user-data" / "Mouchen" / "Desktop" / "mouchen-desktop.db",
        )
        self.assertIs(config["export_existing_records"], False)

    def test_relative_paths_resolve_from_configuration_directory(self):
        path = self.write_config({
            "state_dir": "state",
            "archive_dir": "saved",
            "outbox_dir": "outgoing",
            "source_database": "app/data.db",
            "export_existing_records": True,
            "label": "设备示例",
        }, encoding="utf-8-sig")
        config = configuration.load_client_config(path)
        for key, suffix in (
            ("state_dir", "state"),
            ("archive_dir", "saved"),
            ("outbox_dir", "outgoing"),
            ("source_database", "app/data.db"),
        ):
            with self.subTest(key=key):
                self.assertEqual(Path(config[key]), (path.parent / suffix).resolve())
        self.assertEqual(config["label"], "设备示例")
        self.assertIs(config["export_existing_records"], True)

    def test_environment_can_select_an_external_configuration(self):
        path = self.write_config({"device": "test-device"})
        with patch.dict(os.environ, {"AI_TWIN_RELAY_CONFIG": str(path)}):
            self.assertEqual(configuration.default_client_config_path(), path)
            self.assertEqual(configuration.load_client_config()["device"], "test-device")

    def test_default_archive_and_outbox_follow_custom_state_directory(self):
        path = self.write_config({"state_dir": "custom-state"})
        config = configuration.load_client_config(path)
        state = path.parent / "custom-state"
        self.assertEqual(Path(config["archive_dir"]), state / "Archive")
        self.assertEqual(Path(config["outbox_dir"]), state / "Outbox")

    def test_non_object_configuration_is_rejected(self):
        path = self.write_config(["invalid", "configuration"])
        with self.assertRaisesRegex(ValueError, "must be a JSON object"):
            configuration.load_client_config(path)


if __name__ == "__main__":
    unittest.main()
