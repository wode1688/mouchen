"""Headless form logic for the AI替身偏好 tab.

The controller owns every rule the UI must obey — scope switching with
unsaved-change confirmation, inherit/override semantics (None vs [] for event
types), revision-carrying save requests, and the binding of async responses to
the scope_key/revision they were submitted for so a late response can never
overwrite a form the user has since switched away from. The Tk layer is a thin
projection of this state, which keeps all of it testable without a display.

The event-type catalog, Chinese labels, frequency policies and their numbers
all come from the backend snapshot; nothing is duplicated here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


GLOBAL_SCOPE_KEY = "global"


def goal_scope_key(goal_id: str) -> str:
    return f"goal:{goal_id}"


@dataclass
class PreferenceForm:
    scope_key: str = GLOBAL_SCOPE_KEY
    goal_id: str | None = None
    direction: str = ""
    direction_mode: str = "inherit"
    inherit_frequency: bool = True
    frequency_mode: str = "active"
    inherit_events: bool = True
    event_types: list[str] = field(default_factory=list)
    revision: int = 0
    has_override: bool = False

    def snapshot(self) -> tuple:
        return (
            self.scope_key,
            self.direction.strip(),
            self.direction_mode,
            self.inherit_frequency,
            self.frequency_mode,
            self.inherit_events,
            tuple(self.event_types),
        )


class PreferencesController:
    def __init__(self) -> None:
        self.loaded = False
        self.snapshot: dict[str, Any] = {}
        self.catalog: list[dict[str, str]] = []
        self.policies: dict[str, dict[str, Any]] = {}
        self.goals: list[dict[str, Any]] = []
        self.scope: str = "global"  # "global" | "goal"
        self.selected_goal_id: str | None = None
        self.form = PreferenceForm()
        self._baseline: tuple = self.form.snapshot()
        self.saving = False
        self.status_text = ""
        self.status_level = "info"

    # ------------------------------------------------------------------
    # snapshot loading / cache lifecycle
    # ------------------------------------------------------------------

    def load_snapshot(self, payload: dict[str, Any], *, keep_unsaved: bool = False) -> None:
        unsaved = self.form if (keep_unsaved and self.loaded and self.is_dirty()) else None
        self.snapshot = payload or {}
        self.catalog = list(self.snapshot.get("event_type_catalog") or [])
        self.policies = dict(self.snapshot.get("frequency_policies") or {})
        self.goals = list(self.snapshot.get("goals") or [])
        self.loaded = True
        if self.scope == "goal" and self.selected_goal_id is not None:
            known = {str(item.get("goal_id")) for item in self.goals}
            if self.selected_goal_id not in known:
                # The selected goal disappeared server-side: fall back to global.
                self.scope = "global"
                self.selected_goal_id = None
                unsaved = None
        if unsaved is not None and unsaved.scope_key == self._current_scope_key():
            # A goals refresh must not clobber unsaved text: keep the working
            # form and only refresh the revision baseline underneath it.
            stored = self._stored_form()
            unsaved.revision = stored.revision
            unsaved.has_override = stored.has_override
            self.form = unsaved
        else:
            self.reset_form()

    def reset_for_user_change(self) -> None:
        """Backend URL / User ID changed: the cache belongs to someone else."""

        self.__init__()

    def refresh_goals(self, payload: dict[str, Any]) -> None:
        self.load_snapshot(payload, keep_unsaved=True)

    # ------------------------------------------------------------------
    # scope selection
    # ------------------------------------------------------------------

    def _current_scope_key(self) -> str:
        if self.scope == "goal" and self.selected_goal_id:
            return goal_scope_key(self.selected_goal_id)
        return GLOBAL_SCOPE_KEY

    def is_dirty(self) -> bool:
        return self.form.snapshot() != self._baseline

    def select_scope(
        self,
        scope: str,
        goal_id: str | None = None,
        *,
        confirmed: bool = False,
    ) -> str:
        """Returns "ok" or "confirm_required" (unsaved edits need a decision)."""

        target_goal = str(goal_id) if goal_id else None
        if scope == self.scope and target_goal == self.selected_goal_id:
            return "ok"
        if self.is_dirty() and not confirmed:
            return "confirm_required"
        self.scope = "goal" if scope == "goal" else "global"
        self.selected_goal_id = target_goal if self.scope == "goal" else None
        self.reset_form()
        return "ok"

    # ------------------------------------------------------------------
    # form population
    # ------------------------------------------------------------------

    def _global_preference(self) -> dict[str, Any]:
        return dict(self.snapshot.get("global") or {})

    def _goal_override(self, goal_id: str) -> dict[str, Any] | None:
        for item in self.snapshot.get("goal_overrides") or []:
            if str(item.get("goal_id")) == goal_id:
                return dict(item)
        return None

    def _stored_form(self) -> PreferenceForm:
        global_pref = self._global_preference()
        global_frequency = str(global_pref.get("frequency_mode") or "active")
        global_events = list(global_pref.get("event_types") or [])
        if self.scope != "goal" or not self.selected_goal_id:
            return PreferenceForm(
                scope_key=GLOBAL_SCOPE_KEY,
                goal_id=None,
                direction=str(global_pref.get("direction") or ""),
                direction_mode="replace",
                inherit_frequency=False,
                frequency_mode=global_frequency,
                inherit_events=False,
                event_types=global_events,
                revision=int(global_pref.get("revision") or 0),
                has_override=True,
            )
        override = self._goal_override(self.selected_goal_id)
        if override is None:
            return PreferenceForm(
                scope_key=goal_scope_key(self.selected_goal_id),
                goal_id=self.selected_goal_id,
                direction="",
                direction_mode="inherit",
                inherit_frequency=True,
                frequency_mode=global_frequency,
                inherit_events=True,
                event_types=global_events,
                revision=0,
                has_override=False,
            )
        frequency_mode = override.get("frequency_mode")
        event_types = override.get("event_types")
        return PreferenceForm(
            scope_key=goal_scope_key(self.selected_goal_id),
            goal_id=self.selected_goal_id,
            direction=str(override.get("direction") or ""),
            direction_mode=str(override.get("direction_mode") or "inherit"),
            inherit_frequency=frequency_mode is None,
            frequency_mode=(
                str(frequency_mode) if frequency_mode is not None else global_frequency
            ),
            inherit_events=event_types is None,
            # event_types=[] is a REAL value (block every automatic event) and
            # must survive as-is; only None means "inherit the global list".
            event_types=(
                list(event_types) if event_types is not None else global_events
            ),
            revision=int(override.get("revision") or 0),
            has_override=True,
        )

    def reset_form(self) -> None:
        self.form = self._stored_form()
        self._baseline = self.form.snapshot()

    # ------------------------------------------------------------------
    # save / delete requests bound to scope_key + revision
    # ------------------------------------------------------------------

    def build_save_request(self) -> dict[str, Any]:
        form = self.form
        if form.scope_key == GLOBAL_SCOPE_KEY:
            body: dict[str, Any] = {
                "direction": form.direction.strip(),
                "direction_mode": "replace",
                "frequency_mode": form.frequency_mode,
                "event_types": list(form.event_types),
                "revision": form.revision,
            }
        else:
            body = {
                "direction": form.direction.strip(),
                "direction_mode": form.direction_mode,
                "frequency_mode": None if form.inherit_frequency else form.frequency_mode,
                "event_types": None if form.inherit_events else list(form.event_types),
                "revision": form.revision,
            }
        return {
            "scope_key": form.scope_key,
            "goal_id": form.goal_id,
            "revision": form.revision,
            "body": body,
        }

    def build_delete_request(self) -> dict[str, Any] | None:
        if self.form.scope_key == GLOBAL_SCOPE_KEY or not self.form.goal_id:
            return None
        return {"scope_key": self.form.scope_key, "goal_id": self.form.goal_id}

    # ------------------------------------------------------------------
    # async response application (stale responses never touch the form)
    # ------------------------------------------------------------------

    def _merge_saved_preference(self, preference: dict[str, Any]) -> None:
        scope_key = str(preference.get("scope_key") or "")
        if scope_key == GLOBAL_SCOPE_KEY:
            self.snapshot["global"] = dict(preference)
            return
        overrides = [
            item
            for item in self.snapshot.get("goal_overrides") or []
            if str(item.get("scope_key")) != scope_key
        ]
        overrides.append(dict(preference))
        self.snapshot["goal_overrides"] = overrides

    def apply_save_success(self, scope_key: str, response: dict[str, Any]) -> bool:
        """Returns True when the CURRENT form was updated; a response for a
        scope the user has left only refreshes the cached snapshot."""

        self.saving = False
        preference = (
            response.get("preference") if isinstance(response, dict) else None
        )
        if isinstance(preference, dict):
            self._merge_saved_preference(preference)
        if scope_key != self._current_scope_key():
            return False
        self.reset_form()
        self.status_text = "已保存"
        self.status_level = "ok"
        return True

    def apply_save_failure(
        self,
        scope_key: str,
        message: str,
        *,
        conflict_current: dict[str, Any] | None = None,
    ) -> bool:
        """Failure keeps every input; a revision conflict adopts the server's
        revision so an explicit re-save can succeed. Stale-scope failures only
        touch the cache."""

        self.saving = False
        if isinstance(conflict_current, dict):
            self._merge_saved_preference(conflict_current)
        if scope_key != self._current_scope_key():
            return False
        if isinstance(conflict_current, dict):
            self.form.revision = int(conflict_current.get("revision") or 0)
            self.status_text = "设置已在其他页面更新；输入已保留，再次保存将覆盖"
            self.status_level = "warn"
        else:
            self.status_text = f"保存失败:{message}(输入已保留)"
            self.status_level = "error"
        return True

    def apply_delete_success(self, scope_key: str, response: dict[str, Any]) -> bool:
        self.saving = False
        overrides = [
            item
            for item in self.snapshot.get("goal_overrides") or []
            if str(item.get("scope_key")) != scope_key
        ]
        self.snapshot["goal_overrides"] = overrides
        if scope_key != self._current_scope_key():
            return False
        self.reset_form()
        self.status_text = "已恢复全局设置"
        self.status_level = "ok"
        return True

    # ------------------------------------------------------------------
    # helpers for the widget layer
    # ------------------------------------------------------------------

    def frequency_options(self) -> list[tuple[str, str]]:
        return [
            (mode, str(policy.get("label") or mode))
            for mode, policy in self.policies.items()
        ]

    def catalog_options(self) -> list[tuple[str, str]]:
        return [
            (str(item.get("id")), str(item.get("label") or item.get("id")))
            for item in self.catalog
        ]

    def goal_options(self) -> list[tuple[str, str]]:
        return [
            (
                str(item.get("goal_id")),
                f"{item.get('domain', '')} · {item.get('title', '')}",
            )
            for item in self.goals
        ]

    def toggle_event_type(self, event_type: str, enabled: bool) -> None:
        current = [item for item in self.form.event_types if item != event_type]
        if enabled:
            ordered = [identifier for identifier, _ in self.catalog_options()]
            current.append(event_type)
            current = [item for item in ordered if item in set(current)]
        self.form.event_types = current
