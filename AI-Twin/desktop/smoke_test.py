from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

DESKTOP_ROOT = Path(__file__).resolve().parent
if str(DESKTOP_ROOT) not in sys.path:
    sys.path.insert(0, str(DESKTOP_ROOT))

from mouchen_desktop.agent import DesktopAgent
from mouchen_desktop.backend import BackendClient
from mouchen_desktop.security import IdentityProtector
from mouchen_desktop.settings import AppSettings, SettingsRepository
from mouchen_desktop.store import EventStore


def main() -> None:
    run_id = uuid.uuid4().hex[:10]
    runtime = DESKTOP_ROOT / ".runtime" / "smoke"
    runtime.mkdir(parents=True, exist_ok=True)
    settings_repository = SettingsRepository(runtime / f"settings-{run_id}.json", IdentityProtector())
    settings = AppSettings(
        backend_url=os.environ.get("MOUCHEN_BACKEND_URL", "http://127.0.0.1:8787"),
        user_id=f"desktop-smoke-{run_id}",
        collection_enabled=False,
        proactive_cloud_enabled=False,
        sync_seconds=2,
    )
    settings_repository.save(settings)
    store = EventStore(runtime / f"desktop-{run_id}.db", IdentityProtector())
    client = BackendClient(settings)
    client.create_goal(
        {
            "domain": "work",
            "title": "Ship the Windows alpha",
            "quote": "The Windows private alpha must remain operational this week",
            "target": {"keywords": ["deployment", "production", "Windows"]},
            "is_redline": False,
        }
    )
    received: list[dict] = []
    agent = DesktopAgent(settings_repository, store, on_advice=received.append)
    try:
        agent.start()
        agent.submit_problem("Production deployment failed with a connection timeout", "work")
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline and not received:
            time.sleep(0.2)
        if not received:
            raise RuntimeError("Desktop event reached no proactive advice within 12 seconds")
        advice = received[0]
        agent.feedback(str(advice["id"]), "adopted", "desktop smoke test")
        print(
            json.dumps(
                {
                    "health": client.health().get("status"),
                    "event_synced": store.stats()["pending"] == 0,
                    "advice_id": advice["id"],
                    "level": advice.get("effective_level"),
                    "feedback": "adopted",
                },
                ensure_ascii=False,
            )
        )
    finally:
        agent.stop()
        store.close()


if __name__ == "__main__":
    main()
