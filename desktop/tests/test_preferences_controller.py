"""AI替身偏好 form-controller tests (headless).

Covers the mandated desktop behaviors: global/goal switching, inherit vs
override semantics, the explicit empty event list, building save requests,
restore-to-global, unsaved-change confirmation, failures keeping every input,
goal-list refresh preserving selection and unsaved text, user switching
clearing the cache, and async responses never polluting a form the user has
since navigated away from.
"""

from __future__ import annotations

from mouchen_desktop.preferences import PreferencesController


CATALOG = [
    {"id": "notification.posted", "label": "通知"},
    {"id": "ui.visible_text", "label": "屏幕内容"},
    {"id": "mail.received", "label": "邮件"},
    {"id": "app.foreground_session", "label": "应用使用"},
]
POLICIES = {
    "active": {"label": "积极", "max_per_window": 8},
    "balanced": {"label": "均衡", "max_per_window": 3},
    "paused": {"label": "暂停主动建言", "max_per_window": 0},
}
ALL_IDS = [item["id"] for item in CATALOG]


def snapshot(
    *,
    global_direction: str = "",
    global_frequency: str = "active",
    global_events: list[str] | None = None,
    global_revision: int = 0,
    goals: list[dict] | None = None,
    overrides: list[dict] | None = None,
) -> dict:
    return {
        "global": {
            "user_id": "u1",
            "scope_key": "global",
            "goal_id": None,
            "direction": global_direction,
            "direction_mode": "replace",
            "frequency_mode": global_frequency,
            "event_types": list(global_events if global_events is not None else ALL_IDS),
            "revision": global_revision,
        },
        "goal_overrides": list(overrides or []),
        "goals": list(goals or []),
        "event_type_catalog": CATALOG,
        "frequency_policies": POLICIES,
        "direction_modes": ["inherit", "append", "replace"],
    }


def goal_entry(goal_id: str = "goal-1", title: str = "发布AI替身") -> dict:
    return {"goal_id": goal_id, "title": title, "domain": "work", "effective": {}}


def override_entry(goal_id: str = "goal-1", **fields) -> dict:
    base = {
        "user_id": "u1",
        "scope_key": f"goal:{goal_id}",
        "goal_id": goal_id,
        "direction": "",
        "direction_mode": "inherit",
        "frequency_mode": None,
        "event_types": None,
        "revision": 1,
    }
    base.update(fields)
    return base


def make_controller(**kwargs) -> PreferencesController:
    controller = PreferencesController()
    controller.load_snapshot(snapshot(**kwargs))
    return controller


def test_global_form_reflects_snapshot_and_builds_concrete_save():
    controller = make_controller(
        global_direction="盯现金流", global_frequency="balanced", global_revision=4
    )
    assert controller.form.scope_key == "global"
    assert controller.form.direction == "盯现金流"
    assert controller.form.frequency_mode == "balanced"
    assert controller.is_dirty() is False

    controller.form.direction = "盯现金流和渠道"
    assert controller.is_dirty() is True
    request = controller.build_save_request()
    assert request["scope_key"] == "global"
    assert request["goal_id"] is None
    assert request["revision"] == 4
    # The global scope always serializes concrete values, never inherit nulls.
    assert request["body"]["frequency_mode"] == "balanced"
    assert request["body"]["event_types"] == ALL_IDS
    assert request["body"]["revision"] == 4


def test_goal_scope_inherit_and_override_serialization():
    controller = make_controller(goals=[goal_entry()], global_frequency="balanced")
    assert controller.select_scope("goal", "goal-1") == "ok"
    form = controller.form
    # Without an override the form shows inherited values for display...
    assert form.inherit_frequency is True
    assert form.frequency_mode == "balanced"
    assert form.inherit_events is True
    assert form.event_types == ALL_IDS
    assert form.has_override is False
    # ...and serializes them as inherit (None), never as copied values.
    body = controller.build_save_request()["body"]
    assert body["frequency_mode"] is None
    assert body["event_types"] is None

    controller.form.inherit_frequency = False
    controller.form.frequency_mode = "paused"
    controller.form.inherit_events = False
    controller.form.event_types = []
    body = controller.build_save_request()["body"]
    assert body["frequency_mode"] == "paused"
    assert body["event_types"] == []  # 明确空数组，绝不是 None，也绝不回落默认


def test_goal_override_with_empty_event_list_round_trips():
    controller = make_controller(
        goals=[goal_entry()],
        overrides=[override_entry(event_types=[], frequency_mode="paused", revision=3)],
    )
    controller.select_scope("goal", "goal-1")
    form = controller.form
    assert form.inherit_events is False
    assert form.event_types == []  # [] survives loading, not treated as absent
    assert form.inherit_frequency is False
    assert form.frequency_mode == "paused"
    assert form.revision == 3
    assert form.has_override is True


def test_scope_switch_requires_confirmation_when_dirty():
    controller = make_controller(goals=[goal_entry()])
    controller.form.direction = "改了但没保存"
    assert controller.select_scope("goal", "goal-1") == "confirm_required"
    # Declined: nothing changed, the unsaved text is intact.
    assert controller.scope == "global"
    assert controller.form.direction == "改了但没保存"
    # Confirmed: switch proceeds and the goal form is fresh.
    assert controller.select_scope("goal", "goal-1", confirmed=True) == "ok"
    assert controller.scope == "goal"
    assert controller.form.direction == ""


def test_save_success_for_current_scope_updates_baseline():
    controller = make_controller()
    controller.form.direction = "新的全局方向"
    request = controller.build_save_request()
    applied = controller.apply_save_success(
        request["scope_key"],
        {
            "status": "saved",
            "preference": {
                "scope_key": "global",
                "goal_id": None,
                "direction": "新的全局方向",
                "direction_mode": "replace",
                "frequency_mode": "active",
                "event_types": ALL_IDS,
                "revision": 1,
            },
        },
    )
    assert applied is True
    assert controller.form.direction == "新的全局方向"
    assert controller.form.revision == 1
    assert controller.is_dirty() is False
    assert controller.status_level == "ok"


def test_stale_async_response_never_touches_the_new_form():
    controller = make_controller(goals=[goal_entry("goal-1"), goal_entry("goal-2", "第二目标")])
    controller.select_scope("goal", "goal-1")
    controller.form.direction = "goal-1 的方向"
    submitted = controller.build_save_request()
    # The user switches to another goal while the request is in flight and
    # starts typing something new there.
    controller.select_scope("goal", "goal-2", confirmed=True)
    controller.form.direction = "goal-2 输入中"

    applied = controller.apply_save_success(
        submitted["scope_key"],
        {
            "status": "saved",
            "preference": override_entry("goal-1", direction="goal-1 的方向", revision=1),
        },
    )
    assert applied is False  # 旧响应绝不覆盖新表单
    assert controller.form.goal_id == "goal-2"
    assert controller.form.direction == "goal-2 输入中"
    # The cache still absorbed the result for goal-1:
    stored = {
        item["scope_key"]: item for item in controller.snapshot["goal_overrides"]
    }
    assert stored["goal:goal-1"]["direction"] == "goal-1 的方向"

    failure_applied = controller.apply_save_failure(submitted["scope_key"], "超时")
    assert failure_applied is False
    assert controller.form.direction == "goal-2 输入中"


def test_save_failure_keeps_every_input_and_conflict_adopts_server_revision():
    controller = make_controller(goals=[goal_entry()])
    controller.select_scope("goal", "goal-1")
    controller.form.direction = "写了很多但失败"
    controller.form.inherit_events = False
    controller.form.event_types = ["mail.received"]
    request = controller.build_save_request()

    applied = controller.apply_save_failure(request["scope_key"], "网络错误")
    assert applied is True
    assert controller.form.direction == "写了很多但失败"
    assert controller.form.event_types == ["mail.received"]
    assert controller.status_level == "error"

    conflict = controller.apply_save_failure(
        request["scope_key"],
        "revision conflict",
        conflict_current=override_entry("goal-1", direction="别处保存的", revision=7),
    )
    assert conflict is True
    assert controller.form.direction == "写了很多但失败"  # 输入仍然保留
    assert controller.form.revision == 7  # 明确重试时使用最新版本
    assert controller.status_level == "warn"


def test_restore_global_deletes_override_and_reinherits():
    controller = make_controller(
        goals=[goal_entry()],
        overrides=[override_entry(frequency_mode="paused", direction="目标方向")],
        global_frequency="balanced",
    )
    controller.select_scope("goal", "goal-1")
    assert controller.form.has_override is True
    request = controller.build_delete_request()
    assert request == {"scope_key": "goal:goal-1", "goal_id": "goal-1"}

    applied = controller.apply_delete_success(
        request["scope_key"], {"status": "deleted", "effective": {}}
    )
    assert applied is True
    assert controller.form.has_override is False
    assert controller.form.inherit_frequency is True
    assert controller.form.frequency_mode == "balanced"  # 立即回到全局
    assert controller.build_delete_request() is not None  # 仍在目标模式
    assert controller.snapshot["goal_overrides"] == []


def test_goal_refresh_preserves_selection_and_unsaved_text():
    controller = make_controller(goals=[goal_entry("goal-1")])
    controller.select_scope("goal", "goal-1")
    controller.form.direction = "还没保存的目标方向"

    refreshed = snapshot(
        goals=[goal_entry("goal-1"), goal_entry("goal-2", "新目标")],
        global_revision=2,
    )
    controller.refresh_goals(refreshed)

    assert controller.scope == "goal"
    assert controller.selected_goal_id == "goal-1"
    assert controller.form.direction == "还没保存的目标方向"  # 未保存文本保留
    assert [identifier for identifier, _ in controller.goal_options()] == [
        "goal-1",
        "goal-2",
    ]


def test_goal_refresh_falls_back_to_global_when_goal_disappears():
    controller = make_controller(goals=[goal_entry("goal-1")])
    controller.select_scope("goal", "goal-1")
    controller.form.direction = "即将失效"

    controller.refresh_goals(snapshot(goals=[goal_entry("goal-2", "另一目标")]))

    assert controller.scope == "global"
    assert controller.selected_goal_id is None
    assert controller.is_dirty() is False


def test_no_goals_still_allows_global_editing_with_empty_goal_state():
    controller = make_controller(goals=[])
    assert controller.goal_options() == []
    assert controller.form.scope_key == "global"
    controller.form.direction = "没有目标也能设全局"
    request = controller.build_save_request()
    assert request["scope_key"] == "global"
    # Switching to goal mode without goals is a normal empty state, not a crash.
    assert controller.select_scope("goal", None, confirmed=True) == "ok"
    assert controller.selected_goal_id is None
    assert controller.build_delete_request() is None


def test_user_change_clears_cache_completely():
    controller = make_controller(
        goals=[goal_entry()], overrides=[override_entry(direction="旧用户的")]
    )
    controller.select_scope("goal", "goal-1")
    controller.form.direction = "旧用户没保存的输入"

    controller.reset_for_user_change()

    assert controller.loaded is False
    assert controller.snapshot == {}
    assert controller.goals == []
    assert controller.scope == "global"
    assert controller.form.direction == ""
    assert controller.is_dirty() is False


def test_event_toggle_keeps_catalog_order():
    controller = make_controller()
    controller.form.event_types = []
    controller.toggle_event_type("mail.received", True)
    controller.toggle_event_type("notification.posted", True)
    assert controller.form.event_types == ["notification.posted", "mail.received"]
    controller.toggle_event_type("mail.received", False)
    assert controller.form.event_types == ["notification.posted"]
