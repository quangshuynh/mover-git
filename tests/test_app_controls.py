import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import FileMoverGitApp


def controls():
    app = object.__new__(FileMoverGitApp)
    app.root = SimpleNamespace(destroy=Mock(), title=Mock())
    app.ui = lambda func, *args, **kwargs: func(*args, **kwargs)
    app.is_busy = True
    app.pending_operation = True
    app.pause_event = threading.Event()
    app.close_after_pause = False
    app.last_status = "Running"
    app.unsafe_widgets = [Mock(), Mock()]
    for name in ("scan_button", "move_button", "pause_button", "resume_button", "status_label",
                 "max_files_entry", "limit_button"):
        setattr(app, name, Mock())
    return app


def test_active_and_paused_controls_lock_unsafe_settings():
    app = controls()
    app.set_busy(True, "Running")
    for widget in [app.scan_button, app.move_button, app.resume_button, *app.unsafe_widgets]:
        widget.configure.assert_called_with(state="disabled")
    app.limit_button.configure.assert_called_with(state="normal")
    app.set_busy(False, "Paused")
    app.resume_button.configure.assert_called_with(state="normal")
    app.move_button.configure.assert_called_with(state="disabled")


@pytest.mark.parametrize("choice", [True, False])
def test_window_close_never_terminates_active_batch(monkeypatch, choice):
    app = controls()
    monkeypatch.setattr("app.messagebox.askyesno", lambda *args: choice)
    app.request_close()
    app.root.destroy.assert_not_called()
    assert app.pause_event.is_set() == choice
    assert app.close_after_pause == choice
    if choice:
        assert app.last_status == "Pause Requested"


@pytest.mark.parametrize("result,closes", [("Paused", True), ("Complete", True), ("Error", False)])
def test_close_waits_for_safe_worker_result(result, closes):
    app = controls()
    app.close_after_pause = True
    app.move_and_push = lambda: app.set_status(result)
    app._move_worker()
    assert app.root.destroy.called == closes
    assert not app.is_busy


def test_restore_session_detects_pending_without_starting_unapproved_work(tmp_path):
    app = controls()
    app.session_file = tmp_path / "session.json"
    app.session_file.write_text(json.dumps({"source": "source", "repo": "repository"}))
    app.source_var = Mock()
    app.dest_var = Mock()
    app.detect_pending = Mock()
    app.restore_session()
    app.source_var.set.assert_called_once_with("source")
    app.dest_var.set.assert_called_once_with("repository")
    app.detect_pending.assert_called_once()


def test_tk_controls_initialize_and_apply_limit(monkeypatch):
    import tkinter as tk

    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("Tk display unavailable")
    root.withdraw()
    monkeypatch.setattr(FileMoverGitApp, "restore_session", lambda self: None)
    try:
        app = FileMoverGitApp(root)
        root.update()
        assert app.max_files_var.get() == ""
        assert str(app.resume_button["state"]) == "disabled"
        app.max_files_var.set("10,000")
        assert app.apply_limit()
        assert app.max_files == 10000
        app.set_busy(True, "Running")
        root.update()
        assert str(app.move_button["state"]) == "disabled"
        assert str(app.limit_button["state"]) == "normal"
    finally:
        root.destroy()
