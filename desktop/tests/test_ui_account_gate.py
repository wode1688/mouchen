from __future__ import annotations

from types import SimpleNamespace

from mouchen_desktop.settings import AppSettings
from mouchen_desktop.ui import AccountDialog, MouchenWindow, _present_startup_dialog


class Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeRoot:
    def __init__(self, *, viewable: bool):
        self.viewable = viewable

    def winfo_viewable(self):
        return self.viewable


class FakeDialog:
    def __init__(self):
        self.transient_parent = None
        self.actions = []

    def transient(self, parent):
        self.transient_parent = parent

    def update_idletasks(self):
        self.actions.append("update")

    def deiconify(self):
        self.actions.append("deiconify")

    def lift(self):
        self.actions.append("lift")


def test_startup_dialog_is_not_bound_to_a_withdrawn_root():
    root = FakeRoot(viewable=False)
    dialog = FakeDialog()

    _present_startup_dialog(dialog, root)

    assert dialog.transient_parent is None
    assert dialog.actions == ["update", "deiconify", "lift"]


def test_startup_dialog_uses_visible_root_as_owner():
    root = FakeRoot(viewable=True)
    dialog = FakeDialog()

    _present_startup_dialog(dialog, root)

    assert dialog.transient_parent is root
    assert dialog.actions == ["update", "deiconify", "lift"]


def legacy_window():
    window = MouchenWindow.__new__(MouchenWindow)
    window.root = object()
    window.agent = SimpleNamespace(
        settings=AppSettings(
            backend_url="https://mouchen.example.com",
            auth_mode="legacy",
            user_id="legacy-owner",
            bearer_token="legacy-token",
        )
    )
    return window


def test_legacy_gate_requires_an_explicit_choice_before_continuing(monkeypatch):
    window = legacy_window()
    monkeypatch.setattr(
        "mouchen_desktop.ui.LegacyOwnerAccountGate",
        lambda root: SimpleNamespace(show=lambda: "continue"),
    )
    monkeypatch.setattr(
        "mouchen_desktop.ui.AccountDialog",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("claim dialog must not open for an explicit one-run continuation")
        ),
    )

    assert window._ensure_account() is True


def test_closing_legacy_gate_stops_startup(monkeypatch):
    window = legacy_window()
    monkeypatch.setattr(
        "mouchen_desktop.ui.LegacyOwnerAccountGate",
        lambda root: SimpleNamespace(show=lambda: "exit"),
    )

    assert window._ensure_account() is False


def test_legacy_claim_opens_identity_pinned_account_dialog(monkeypatch):
    window = legacy_window()
    observed = {}
    monkeypatch.setattr(
        "mouchen_desktop.ui.LegacyOwnerAccountGate",
        lambda root: SimpleNamespace(show=lambda: "claim"),
    )

    def account_dialog(root, agent, initial_error="", **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(show=lambda: True)

    monkeypatch.setattr("mouchen_desktop.ui.AccountDialog", account_dialog)

    assert window._ensure_account() is True
    assert observed == {"claim_legacy_owner": True}


def test_owner_claim_requires_code_and_pins_original_tenant():
    calls = []
    settings = AppSettings(
        backend_url="https://mouchen.example.com",
        auth_mode="legacy",
        user_id="legacy-owner",
        bearer_token="legacy-token",
    )
    agent = SimpleNamespace(
        settings=settings,
        update_settings=lambda next_settings: None,
        authenticate=lambda *args, **kwargs: calls.append((args, kwargs)),
    )
    dialog = AccountDialog.__new__(AccountDialog)
    dialog.agent = agent
    dialog.claim_legacy_owner = True
    dialog.expected_legacy_user_id = "legacy-owner"
    dialog.accepted = False
    dialog.backend = Value(settings.backend_url)
    dialog.username = Value("owner")
    dialog.password = Value("correct horse battery staple")
    dialog.confirm = Value("correct horse battery staple")
    dialog.registration_code = Value("")
    dialog.status = Value()
    dialog.window = SimpleNamespace(update_idletasks=lambda: None, destroy=lambda: None)
    dialog._set_busy = lambda busy, text: None

    dialog._submit(register=True)

    assert calls == []
    assert dialog.status.get() == "请输入主人领取码"

    dialog.registration_code.set("owner-bootstrap-code")
    dialog._submit(register=True)

    assert calls == [
        (
            ("owner", "correct horse battery staple"),
            {
                "register": True,
                "registration_code": "owner-bootstrap-code",
                "expected_user_id": "legacy-owner",
            },
        )
    ]
    assert dialog.accepted is True
