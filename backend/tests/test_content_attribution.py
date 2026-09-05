from app.advice_refiner import CONTENT_ATTRIBUTION_RULES, _content_observation
from app.domain.models import Event


def test_content_observation_accepts_only_bounded_provenance_values():
    event = Event(
        user_id="u1",
        source="windows.uia",
        type="ui.visible_text",
        facts={
            "content_kind": "VIDEO",
            "speaker": "AUTHOR",
            "message_direction": "inbound",
            "visible_only": True,
            "evidence_strength": "contextual",
            "resolution_state": "unknown",
            "session_key": "video:stable-key",
        },
    )

    assert _content_observation(event) == {
        "content_kind": "video",
        "speaker": "author",
        "message_direction": "inbound",
        "visible_only": True,
        "evidence_strength": "contextual",
        "resolution_state": "unknown",
        "session_key": "video:stable-key",
    }


def test_content_observation_fails_closed_for_unrecognized_attribution():
    event = Event(
        user_id="u1",
        source="untrusted.client",
        type="ui.visible_text",
        facts={
            "content_kind": "owner_is_definitely_in_danger",
            "speaker": "trusted_oracle",
            "evidence_strength": "certain",
            "resolution_state": "ongoing_forever",
            "session_key": "x" * 400,
        },
    )

    observation = _content_observation(event)

    assert observation["content_kind"] == "unknown"
    assert observation["speaker"] == "unknown"
    assert observation["evidence_strength"] == "inferred"
    assert observation["resolution_state"] == "unknown"
    assert len(observation["session_key"]) == 256


def test_discovery_prompt_forbids_assigning_observed_content_to_owner():
    assert "counterparty chat message is not the owner's commitment" in CONTENT_ATTRIBUTION_RULES
    assert "search result is not proof" in CONTENT_ATTRIBUTION_RULES
    assert "source code or documentation" in CONTENT_ATTRIBUTION_RULES
    assert "Immediate risk advice requires a direct owner statement" in CONTENT_ATTRIBUTION_RULES
