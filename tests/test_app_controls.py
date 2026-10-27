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


def test_cancel_controls_disable_processing_and_unlock_finalized_folders():
    app = controls()
    app.operation = Mock()
    for name in ('cancel_batch_button', 'cancel_transfer_button', 'cleanup_button'):
        setattr(app, name, Mock())
    app.set_busy(True, 'Cancel Requested')
    for widget in (app.cancel_batch_button, app.cancel_transfer_button, app.cleanup_button,
                   app.pause_button, app.resume_button, app.limit_button, *app.unsafe_widgets):
        widget.configure.assert_called_with(state='disabled')
    app.set_busy(False, 'Cleanup Available')
    app.cleanup_button.configure.assert_called_with(state='normal')
    app.resume_button.configure.assert_called_with(state='normal')
    for widget in app.unsafe_widgets:
        widget.configure.assert_called_with(state='normal')


def test_cancel_uses_saved_paths_with_wrong_folders_selected(repo, monkeypatch):
    from test_operations import create_operation

    from mover_git.transfer import BatchOperation

    operation, pairs = create_operation(repo)
    operation.close()
    app = controls()
    app.operation = None
    app.saved_operation_repo = repo
    app.source_var = SimpleNamespace(get=lambda: '/wrong/source')
    app.dest_var = SimpleNamespace(get=lambda: '/wrong/destination')
    app.show_error = Mock()
    launched = []
    monkeypatch.setattr('app.threading.Thread', lambda **kwargs: SimpleNamespace(
        start=lambda: launched.append(kwargs)))
    confirmations = []

    def confirm(title, message):
        confirmations.append(message)
        return True

    monkeypatch.setattr('app.messagebox.askyesno', confirm)
    app.request_cancel('transfer')
    assert str(pairs[0][0]) in confirmations[0]
    assert str(repo) in confirmations[0]
    assert 'Completed/pushed batches: 0' in confirmations[0]
    assert '/wrong/' not in confirmations[0]
    assert launched and app.recovery_requested
    app.show_error.assert_not_called()
    operation = BatchOperation(repo)
    assert operation.cancel_mode() == 'transfer'
    operation.run()
    assert all(source.exists() and not dest.exists() for source, dest in pairs)
    operation.close()


def test_cancel_confirmation_outliving_worker_still_finalizes(monkeypatch):
    app = controls()
    app.is_busy = False
    app.last_status = 'Cancel Requested'
    launched = []
    monkeypatch.setattr('app.threading.Thread', lambda **kwargs: SimpleNamespace(
        start=lambda: launched.append(kwargs)))
    app.finish_requested_cancel()
    assert launched and app.recovery_requested
    assert app.last_status == 'Finalizing'


def test_scan_after_cancel_keeps_folder_correction_available():
    app = controls()
    app.set_busy(False, 'Canceled')
    app.set_busy(True, 'Scanning')
    app.set_status('Scan complete')
    app.set_busy(False)
    for widget in app.unsafe_widgets:
        widget.configure.assert_called_with(state='normal')


def test_discard_is_separate_and_disabled_until_finalized():
    app = controls()
    app.discard_button = Mock()
    app.set_busy(True, 'Cancel Requested')
    app.discard_button.configure.assert_called_with(state='disabled')
    app.set_busy(False, 'Canceled')
    app.discard_button.configure.assert_called_with(state='normal')
    app.set_busy(True, 'Reviewing Discard')
    for widget in (app.discard_button, app.pause_button, app.resume_button, app.limit_button):
        widget.configure.assert_called_with(state='disabled')


@pytest.mark.parametrize('confirmed', [False, True])
def test_discard_ui_requires_abandonment_confirmation_and_unlocks(monkeypatch, tmp_path, confirmed):
    app = controls()
    app.set_busy(False, 'Canceled')
    app.saved_operation_repo = tmp_path
    app.ask_yes_no = Mock(return_value=confirmed)
    app.log, app.show_info, app.show_error = Mock(), Mock(), Mock()
    app.discard_button = Mock()
    monkeypatch.setattr('app.threading.Thread', lambda **kwargs: SimpleNamespace(start=kwargs['target']))

    def discard(repository, confirm):
        assert repository == tmp_path
        return tmp_path / 'archive' if confirm('Changed commits and remote status') else None

    monkeypatch.setattr('app.discard_job', discard)
    app.discard_in_thread()
    app.ask_yes_no.assert_called_once_with('Discard Canceled Job — Abandon Transfer',
                                          'Changed commits and remote status')
    assert app.pending_operation == (not confirmed)
    assert not app.is_busy
    app.show_error.assert_not_called()
    if confirmed:
        assert app.last_status == 'Ready'
        app.scan_button.configure.assert_called_with(state='normal')
        app.resume_button.configure.assert_called_with(state='disabled')
        app.discard_button.configure.assert_called_with(state='disabled')
        for widget in app.unsafe_widgets:
            widget.configure.assert_called_with(state='normal')
