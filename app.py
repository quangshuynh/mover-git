import json
import os
import sqlite3
import subprocess
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from pillow_heif import register_heif_opener

from mover_git.core import (
    MAX_FILE_SIZE_BYTES,
    FileEntry,
    batch_sizes,
    human_size,
    make_batches,
    make_commit_message,
    validate_paths,
)
from mover_git.discard import discard_job, recover_discard
from mover_git.git import (
    check_staging_support,
    normalize_github_url,
    pending_git_file,
    stage_commit_push,
    write_journal,
)
from mover_git.media import (
    MEDIA_FILE_TYPES,
    build_target_path,
    get_media_datetime,
    is_supported_media,
    sanitize_timestamp_filename,
)
from mover_git.storage import DiskSet, FileInventory, walk_entries
from mover_git.transfer import BatchOperation, operation_file, resume_transfers, transfer_directory

register_heif_opener()


class FileMoverGitApp:
    def __init__(self, root: tk.Tk) -> None:
        """
        initialize the application and create its state
        :param root: main Tkinter window for the application
        :returns: nothing
        """
        self.root = root
        self.root.title("File Mover + Git Push")
        self.root.geometry("1040x860")

        self.source_var = tk.StringVar()
        self.dest_var = tk.StringVar()
        self.dest_subfolder_var = tk.StringVar(value="")
        self.commit_prefix_var = tk.StringVar(value="")
        self.remote_branch_var = tk.StringVar(value="main")
        self.commit_each_batch_var = tk.BooleanVar(value=True)
        self.include_empty_dirs_var = tk.BooleanVar(value=True)

        # New media organization options 
        self.organize_media_var = tk.BooleanVar(value=False)
        self.media_use_date_folder_var = tk.BooleanVar(value=True)
        self.media_rename_to_timestamp_var = tk.BooleanVar(value=True)
        self.media_only_for_supported_types_var = tk.BooleanVar(value=True)

        self.valid_files: list[FileEntry] = []
        self.skipped_files: list[tuple[Path, int, str]] = []
        self.batches: list[list[FileEntry]] = []
        self.total_valid_size = 0
        self.total_skipped_size = 0

        # progress variables
        self.total_files_pushed = 0
        self.total_bytes_pushed = 0
        self.current_batch = 0
        self.total_batches = 0

        # current batch progress
        self.current_batch_files = 0
        self.current_batch_total = 0
        self.is_busy = False
        self.last_status = "Ready"
        self.progress_total_files = 0
        self.operation = None
        self.max_files = None
        self.pause_event = threading.Event()
        self.close_after_pause = False
        self.pending_operation = False
        self.max_files_var = tk.StringVar()
        self.session_file = Path.home() / ".git-mover" / "session.json"

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.request_close)
        self.root.after(0, self.restore_session)


    def _build_ui(self) -> None:
        """
        create and arrange all graphical interface widgets for the application
        :returns: nothing
        """
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        source_frame = ttk.LabelFrame(outer, text="Folders", padding=10)
        source_frame.pack(fill="x")

        ttk.Label(source_frame, text="Source folder:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(source_frame, textvariable=self.source_var, width=85).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(source_frame, text="Browse", command=self.pick_source).grid(row=0, column=2, padx=4)

        ttk.Label(source_frame, text="Destination repo folder:").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(source_frame, textvariable=self.dest_var, width=85).grid(row=1, column=1, sticky="ew", padx=8)
        ttk.Button(source_frame, text="Browse", command=self.pick_dest).grid(row=1, column=2, padx=4)

        ttk.Label(source_frame, text="Optional subfolder inside repo:").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(source_frame, textvariable=self.dest_subfolder_var, width=85).grid(row=2, column=1, sticky="ew", padx=8)
        ttk.Button(source_frame, text="Choose", command=self.pick_dest_subfolder).grid(row=2, column=2, padx=4)

        source_frame.columnconfigure(1, weight=1)

        options_frame = ttk.LabelFrame(outer, text="Git options", padding=10)
        options_frame.pack(fill="x", pady=(10, 0))

        ttk.Label(options_frame, text="Optional commit prefix:").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(options_frame, textvariable=self.commit_prefix_var, width=30).grid(row=0, column=1, sticky="w", padx=8)

        ttk.Label(options_frame, text="Branch:").grid(row=0, column=2, sticky="w", pady=4)
        ttk.Entry(options_frame, textvariable=self.remote_branch_var, width=12).grid(row=0, column=3, sticky="w", padx=8)

        ttk.Label(options_frame, text="Push and verify every batch (maximum 200 MiB)").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=4)
        ttk.Label(options_frame, text="Maximum files (blank = unlimited):").grid(
            row=2, column=0, sticky="w", pady=4)
        self.max_files_entry = ttk.Entry(options_frame, textvariable=self.max_files_var, width=16)
        self.max_files_entry.grid(row=2, column=1, sticky="w", padx=8)
        self.limit_button = ttk.Button(options_frame, text="Apply", command=self.apply_limit)
        self.limit_button.grid(row=2, column=2, sticky="w")

        ttk.Checkbutton(
            options_frame,
            text="Recreate empty directories when possible",
            variable=self.include_empty_dirs_var,
        ).grid(row=1, column=2, columnspan=2, sticky="w", pady=4)

        media_frame = ttk.LabelFrame(outer, text="Optional media organization", padding=10)
        media_frame.pack(fill="x", pady=(10, 0))

        ttk.Checkbutton(
            media_frame,
            text="Organize media by date taken / modified",
            variable=self.organize_media_var,
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=4)

        ttk.Checkbutton(
            media_frame,
            text="Place organized media into YYYY-MM-DD subfolders",
            variable=self.media_use_date_folder_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=4)

        ttk.Checkbutton(
            media_frame,
            text="Rename organized media to timestamp",
            variable=self.media_rename_to_timestamp_var,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=4)

        ttk.Checkbutton(
            media_frame,
            text="Only apply organization to supported media types",
            variable=self.media_only_for_supported_types_var,
        ).grid(row=3, column=0, columnspan=2, sticky="w", pady=4)

        ttk.Label(
            media_frame,
            text="Supported media types: " + ", ".join(MEDIA_FILE_TYPES),
            foreground="gray"
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))

        buttons_frame = ttk.Frame(outer)
        buttons_frame.pack(fill="x", pady=(10, 0))

        self.scan_button = ttk.Button(buttons_frame, text="1) Scan and Preview", command=self.scan_in_thread)
        self.scan_button.pack(side="left", padx=(0, 8))
        self.move_button = ttk.Button(buttons_frame, text="2) Move + Git Push", command=self.move_in_thread)
        self.move_button.pack(side="left", padx=8)
        self.pause_button = ttk.Button(buttons_frame, text="Pause After Current Batch",
                                       command=self.request_pause, state="disabled")
        self.pause_button.pack(side="left", padx=8)
        self.resume_button = ttk.Button(buttons_frame, text="Resume",
                                        command=self.resume_in_thread, state="disabled")
        self.resume_button.pack(side="left", padx=8)
        ttk.Button(buttons_frame, text="Clear Log", command=self.clear_log).pack(side="left", padx=8)
        ttk.Button(buttons_frame, text="Show GitHub Link", command=self.show_github_link).pack(side="left", padx=8)

        cancel_frame = ttk.Frame(outer)
        cancel_frame.pack(fill="x", pady=(6, 0))
        self.cancel_batch_button = ttk.Button(cancel_frame, text="Cancel Current Batch",
                                              command=lambda: self.request_cancel("batch"), state="disabled")
        self.cancel_batch_button.pack(side="left", padx=8)
        self.cancel_transfer_button = ttk.Button(cancel_frame, text="Cancel Entire Transfer",
                                                 command=lambda: self.request_cancel("transfer"), state="disabled")
        self.cancel_transfer_button.pack(side="left", padx=8)
        self.cleanup_button = ttk.Button(cancel_frame, text="Clean Up Verified Copies",
                                          command=self.cleanup_in_thread, state="disabled")
        self.cleanup_button.pack(side="left", padx=8)
        self.discard_button = ttk.Button(cancel_frame, text="Discard Canceled Job",
                                         command=self.discard_in_thread, state="disabled")
        self.discard_button.pack(side="left", padx=8)

        summary_frame = ttk.LabelFrame(outer, text="Summary", padding=10)
        summary_frame.pack(fill="x", pady=(10, 0))
        summary_container = ttk.Frame(summary_frame)
        summary_container.pack(fill="x")

        summary_container.columnconfigure(0, weight=1)
        summary_container.columnconfigure(1, weight=0)
        # left side summmary label
        self.summary_label = ttk.Label(
            summary_container,
            justify="left",
            text="No scan yet.",
            anchor="nw"
        )
        self.summary_label.grid(row=0,column=0,sticky="nw")

        #  right side progress label
        self.progress_label = ttk.Label(
            summary_container,
            justify="left",
            anchor="ne",
            font=("Segoe UI", 10, "bold")
        )
        self.progress_label.grid(row=0,column=1,sticky="ne",padx=(30,0))
        self.progress_bar = ttk.Progressbar(summary_frame, mode="determinate", maximum=100)
        self.progress_bar.pack(fill="x", pady=(8, 0))
        self.status_label = ttk.Label(
            summary_frame,
            text="Status: Ready",
            anchor="w",
            font=("Segoe UI", 10, "bold"),
        )
        self.status_label.pack(fill="x", pady=(6, 0))

        preview_frame = ttk.LabelFrame(outer, text="Preview", padding=10)
        preview_frame.pack(fill="both", expand=True, pady=(10, 0))

        self.notebook = ttk.Notebook(preview_frame)
        self.notebook.pack(fill="both", expand=True)

        self.valid_text = self._make_text_tab(self.notebook, "Valid files")
        self.skipped_text = self._make_text_tab(self.notebook, "Skipped files")
        self.batch_text = self._make_text_tab(self.notebook, "Batches")
        self.log_text = self._make_text_tab(self.notebook, "Log")
        self.unsafe_widgets = []

        def collect(parent):
            for child in parent.winfo_children():
                if isinstance(child, ttk.Entry | ttk.Checkbutton | ttk.Button):
                    self.unsafe_widgets.append(child)
                collect(child)

        for frame in (source_frame, options_frame, media_frame):
            collect(frame)
        self.unsafe_widgets.remove(self.max_files_entry)
        self.unsafe_widgets.remove(self.limit_button)

    def apply_limit(self):
        try:
            raw = self.max_files_var.get().strip().replace(",", "")
            value = int(raw) if raw else None
            BatchOperation.validate_limit(value)
            self.max_files = value
            operation = self.operation
            if operation:
                operation.set_limit(value)
            elif self.pending_operation and operation_file(getattr(
                    self, "saved_operation_repo", Path(self.dest_var.get()))).exists():
                operation = BatchOperation(getattr(self, "saved_operation_repo", Path(self.dest_var.get())))
                try:
                    operation.set_limit(value)
                finally:
                    operation.close()
            elif self.valid_files and not self.is_busy:
                self.total_batches = sum(1 for _ in batch_sizes(self.valid_files, value))
                self.update_progress()
            self.log(f"Maximum files: {value or 'unlimited'}; applies to unstarted batches.")
            return True
        except (ValueError, OSError, RuntimeError, sqlite3.Error) as exc:
            self.show_error("Invalid file limit", str(exc))
            return False

    def request_cancel(self, mode):
        operation = self.operation
        owned = operation is None
        previous_status = self.last_status
        try:
            if owned:
                operation = BatchOperation(self.saved_operation_repo)
            if mode == "transfer" and not messagebox.askyesno(
                    "Cancel Entire Transfer", operation.cancellation_summary() +
                    "\nStop safely and prevent remaining batches from starting?"):
                return
            self.set_status("Cancel Requested")
            operation.request_cancel(mode)
            if owned:
                self.set_busy(True, "Finalizing")
                self.recovery_requested = True
                threading.Thread(target=self._move_worker, daemon=False).start()
            else:
                self.root.after(100, self.finish_requested_cancel)
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            self.set_status(previous_status)
            self.show_error("Cancellation unavailable", str(exc))
        finally:
            if owned and operation:
                operation.close()

    def finish_requested_cancel(self):
        # A confirmation dialog can outlive the worker it originally referred to.
        if self.last_status not in ("Cancel Requested", "Finalizing"):
            return
        if self.is_busy:
            self.root.after(100, self.finish_requested_cancel)
            return
        self.pending_operation = True
        self.recovery_requested = True
        self.set_busy(True, "Finalizing")
        threading.Thread(target=self._move_worker, daemon=False).start()

    def cleanup_in_thread(self):
        if self.is_busy or not messagebox.askyesno(
                "Clean Up Verified Copies", "Delete only journal-owned, uncommitted copies whose "
                "original sources and destination hashes still match? Uncertain files are preserved."):
            return
        repo = self.saved_operation_repo
        self.set_busy(True, "Finalizing")

        def worker():
            operation = None
            try:
                operation = BatchOperation(repo)
                operation.cleanup(self.log)
                self.set_status(operation.state["status"])
            except Exception as exc:
                self.log(f"Cleanup preserved files requiring review: {exc}")
                self.set_status("Canceled")
                self.show_error("Cleanup unavailable", str(exc))
            finally:
                if operation:
                    operation.close()
                self.set_busy(False)
                if self.close_after_pause:
                    self.ui(self.root.destroy)
        threading.Thread(target=worker, daemon=False).start()

    def discard_in_thread(self):
        if self.is_busy or not self.pending_operation or not self.cancellation_finalized:
            return
        repo = self.saved_operation_repo
        previous_status = self.cancellation_status
        self.set_busy(True, "Reviewing Discard")

        def worker():
            try:
                archive = discard_job(repo, lambda summary: self.ask_yes_no(
                    "Discard Canceled Job — Abandon Transfer", summary))
                if archive is None:
                    self.set_status(previous_status)
                    return
                self.pending_operation = False
                self.cancellation_finalized = False
                self.recovery_requested = False
                self.pause_event.clear()
                self.set_status("Ready")
                self.log(f"Canceled job discarded. Recovery records: {archive}")
                self.show_info("Canceled job discarded", f"Recovery records preserved at:\n{archive}"
                               "\nCorrect the folders and scan to start a new transfer.")
            except Exception as exc:
                self.set_status(previous_status)
                self.show_error("Discard unavailable", str(exc))
            finally:
                self.set_busy(False)
                if self.close_after_pause:
                    self.ui(self.root.destroy)
        threading.Thread(target=worker, daemon=False).start()

    def request_pause(self):
        self.pause_event.set()
        self.set_status("Pause Requested")
        self.pause_button.configure(state="disabled")

    def request_close(self):
        if self.last_status in ("Cancel Requested", "Finalizing", "Reviewing Discard") and self.is_busy:
            self.close_after_pause = True
        elif not self.is_busy:
            self.root.destroy()
        elif messagebox.askyesno("Operation running", "Pause safely and close when the current "
                                "batch is verified? Choose No to continue working."):
            self.close_after_pause = True
            self.request_pause()

    def restore_session(self):
        try:
            if not self.session_file.exists():
                return
            saved = json.loads(self.session_file.read_text(encoding="utf-8"))
            self.source_var.set(saved["source"])
            self.dest_var.set(saved["repo"])
            self.detect_pending()
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            self.set_status("Error")
            self.show_error("Recovery unavailable", str(exc))

    def detect_pending(self):
        repo = Path(self.dest_var.get())
        archive = recover_discard(repo)
        if archive:
            self.log(f"Completed confirmed discard. Recovery records: {archive}")
        self.pending_operation = False
        self.cancellation_finalized = False
        self.saved_operation_repo = repo.resolve()
        if operation_file(repo).exists():
            operation = BatchOperation(repo)
            try:
                self.saved_operation_repo = repo.resolve()
                canceled = bool(operation.cancel_mode())
                saved_status = operation.state["status"]
                self.pending_operation = operation.state["status"] != "Complete"
                self.remote_branch_var.set(operation.state["branch"])
                self.max_files = operation.max_files
                self.max_files_var.set(str(self.max_files or ""))
            finally:
                operation.close()
            if self.pending_operation:
                if canceled:
                    if saved_status in ("Canceled", "Cleanup Available"):
                        self.set_busy(False, saved_status)
                    else:
                        self.recovery_requested = True
                        self.set_busy(True, "Finalizing")
                        threading.Thread(target=self._move_worker, daemon=False).start()
                    return
                self.set_busy(False, "Paused")
                if messagebox.askyesno("Pending transfer", "A saved transfer is pending. Resume "
                                      "after checking local and remote Git state?"):
                    self.resume_in_thread()
        elif pending_git_file(repo).exists() or any(transfer_directory(repo).glob("*.json")):
            journal = pending_git_file(repo)
            if not journal.exists():
                journal = next(transfer_directory(repo).glob("*.json"))
            saved = json.loads(journal.read_text(encoding="utf-8"))
            self.remote_branch_var.set(saved["branch"])
            self.pending_operation = True
            self.set_busy(False, "Paused")
            if messagebox.askyesno("Pending transfer", "An earlier transfer needs recovery. "
                                  "Verify and resume its Git work?"):
                self.resume_in_thread()
        else:
            self.set_busy(False, "Ready")

    def resume_in_thread(self):
        if self.is_busy or not self.apply_limit():
            return
        repo = getattr(self, "saved_operation_repo", Path(self.dest_var.get()))
        if operation_file(repo).exists():
            operation = BatchOperation(repo)
            try:
                if operation.cancel_mode():
                    if not messagebox.askyesno("Resume saved transfer", operation.cancellation_summary() +
                                              "\nResume these saved paths? Current folder selections are ignored."):
                        return
                    operation.resume()
            finally:
                operation.close()
        self.recovery_requested = True
        self.pause_event.clear()
        self.set_busy(True, "Resuming")
        threading.Thread(target=self._move_worker, daemon=False).start()


    def _make_text_tab(self, notebook: ttk.Notebook, title: str) -> tk.Text:
        """
        create a new text tab with a vertical scrollbar
        :param notebook: notebook widget that will contain the new tab
        :param title: title displayed on the new tab
        :returns: text widget created for the tab
        """
        frame = ttk.Frame(notebook)
        notebook.add(frame, text=title)

        text = tk.Text(frame, wrap="word", height=10)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)

        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return text


    def log(self, message: str) -> None:
        """
        display a message in the log tab
        :param message: message to display in the log
        :returns: nothing
        """
        def _log():
            """
            insert one message on the interface thread
        :returns: nothing
            """
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            if int(self.log_text.index("end-1c").split(".")[0]) > 2000:
                self.log_text.delete("1.0", "501.0")
        self.root.after(0, _log)


    def clear_log(self) -> None:
        """
        remove all messages from the log tab
        :returns: nothing
        """
        self.ui(self.log_text.delete, "1.0", "end")


    def pick_source(self) -> None:
        """
        open a dialog to select the source folder
        :returns: nothing
        """
        folder = filedialog.askdirectory(title="Select source folder")
        if folder:
            self.source_var.set(folder)


    def pick_dest(self) -> None:
        """
        open a dialog to select the destination repository folder
        :returns: nothing
        """
        folder = filedialog.askdirectory(title="Select destination repo folder")
        if folder:
            self.dest_var.set(folder)
            try:
                self.detect_pending()
            except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
                self.show_error("Recovery unavailable", str(exc))


    def pick_dest_subfolder(self) -> None:
        """
        open a dialog to select a subfolder inside the destination repository
        :returns: nothing
        """
        repo_root = self.dest_var.get().strip()
        if not repo_root:
            self.show_error("Pick repo first", "Please choose the destination repo folder first.")
            return

        chosen = filedialog.askdirectory(title="Select a subfolder inside the repo", initialdir=repo_root)
        if chosen:
            try:
                relative = Path(chosen).resolve().relative_to(Path(repo_root).resolve())
                self.dest_subfolder_var.set("" if str(relative) == "." else str(relative))
            except ValueError:
                self.show_error("Invalid subfolder", "The selected folder must be inside the destination repo.")


    def scan_in_thread(self) -> None:
        """
        start the scan process in a background thread
        :returns: nothing
        """
        if self.is_busy or (self.pending_operation and not getattr(self, "cancellation_finalized", False)):
            return
        self.set_busy(True, "Scanning")
        threading.Thread(target=self._scan_worker, daemon=False).start()


    def move_in_thread(self) -> None:
        """
        start the move and push process in a background thread
        :returns: nothing
        """
        if self.is_busy or (self.pending_operation and not getattr(self, "cancellation_finalized", False)) or not self.apply_limit():
            return
        self.pause_event.clear()
        self.set_busy(True, "Running")
        threading.Thread(target=self._move_worker, daemon=False).start()

    def _scan_worker(self) -> None:
        """
        run a scan and restore the idle interface state
        :returns: nothing
        """
        try:
            self.scan_and_preview()
            self.set_status("Scan complete")
        except Exception as exc:
            self.set_status("Error")
            self.show_error("Scan failed", str(exc))
        finally:
            self.set_busy(False)
            if self.close_after_pause:
                self.ui(self.root.destroy)

    def _move_worker(self) -> None:
        """
        run a move operation and restore the idle interface state
        :returns: nothing
        """
        try:
            self.move_and_push()
        finally:
            self.set_busy(False)
            if self.close_after_pause and self.last_status in ("Paused", "Complete", "Canceled", "Cleanup Available"):
                self.ui(self.root.destroy)
            self.close_after_pause = False

    def set_busy(self, busy: bool, status: str | None = None) -> None:
        """
        update action availability and status text
        :param busy: whether a background operation is active
        :param status: optional status text to display
        :returns: nothing
        """
        self.is_busy = busy
        current_status = status or self.last_status
        canceled = current_status in ("Canceled", "Cleanup Available") or getattr(self, "cancellation_finalized", False)
        state = "disabled" if busy or (self.pending_operation and not canceled) else "normal"
        self.ui(self.scan_button.configure, state=state)
        self.ui(self.move_button.configure, state=state)
        for widget in self.unsafe_widgets:
            self.ui(widget.configure, state=state)
        self.ui(self.pause_button.configure, state="normal" if busy else "disabled")
        self.ui(self.resume_button.configure,
                state="normal" if self.pending_operation and not busy else "disabled")
        for widget in (self.max_files_entry, self.limit_button):
            self.ui(widget.configure, state="disabled" if busy and status == "Scanning" else "normal")
        self.update_cancel_controls(current_status)
        if status is not None:
            self.set_status(status)

    def update_cancel_controls(self, status):
        processing = status in ("Cancel Requested", "Finalizing", "Reviewing Discard")
        available = ((getattr(self, "operation", None) is not None or
                      (self.pending_operation and not self.is_busy))
                     and not getattr(self, "cancellation_finalized", False))
        for name in ("cancel_batch_button", "cancel_transfer_button"):
            if hasattr(self, name):
                self.ui(getattr(self, name).configure,
                        state="normal" if available and not processing and status not in
                        ("Canceled", "Cleanup Available") else "disabled")
        if hasattr(self, "cleanup_button"):
            self.ui(self.cleanup_button.configure, state="normal" if
                    not self.is_busy and (status == "Cleanup Available" or
                    (getattr(self, "cancellation_finalized", False) and
                     getattr(self, "cancellation_status", None) == "Cleanup Available")) else "disabled")
        if hasattr(self, "discard_button"):
            self.ui(self.discard_button.configure, state="normal" if
                    not self.is_busy and self.pending_operation and
                    getattr(self, "cancellation_finalized", False) else "disabled")
        if processing:
            for widget in (self.pause_button, self.resume_button, self.max_files_entry,
                           self.limit_button):
                self.ui(widget.configure, state="disabled")

    def set_status(self, status: str) -> None:
        """
        display the current application operation
        :param status: status text to display
        :returns: nothing
        """
        self.last_status = status
        if status in ("Canceled", "Cleanup Available"):
            self.cancellation_finalized = True
            self.cancellation_status = status
        elif status in ("Cancel Requested", "Finalizing", "Resuming", "Complete"):
            self.cancellation_finalized = False
        self.update_cancel_controls(status)
        self.ui(self.status_label.configure, text=f"Status: {status}")
        self.ui(self.root.title, f"File Mover + Git Push | {status}")


    def validate_paths(self) -> tuple[Path, Path, Path] | None:
        """
        validate the selected source destination and subfolder paths
        :returns: tuple containing validated source repository and destination paths or None
        """
        try:
            return validate_paths(
                Path(self.source_var.get().strip()),
                Path(self.dest_var.get().strip()),
                self.dest_subfolder_var.get().strip(),
            )
        except (OSError, ValueError) as exc:
            self.show_error("Invalid folders", str(exc))
            return None


    def scan_and_preview(self) -> None:
        """
        scan the source folder organize valid files create batches and update the preview
        :returns: nothing
        """
        validated = self.validate_paths()
        if not validated:
            return
        src, _, dst = validated

        if isinstance(self.valid_files, FileInventory):
            self.valid_files.close()
        self.valid_files = FileInventory()
        self.skipped_count = 0
        self.skipped_files = []
        self.batches = []
        self.total_valid_size = 0
        self.total_skipped_size = 0

        self.ui(self.valid_text.delete, "1.0", "end")
        self.ui(self.skipped_text.delete, "1.0", "end")
        self.ui(self.batch_text.delete, "1.0", "end")

        self.log("Scanning source folder...")

        all_dirs = 0
        for full_path, is_directory in walk_entries(src):
            if is_directory:
                all_dirs += 1
                continue
            rel_path = full_path.relative_to(src)

            if full_path.is_symlink():
                self.record_skipped((rel_path, 0, "Skipped symlink"))
                continue

            try:
                size = full_path.stat().st_size
            except OSError as exc:
                self.record_skipped((rel_path, 0, f"Unreadable: {exc}"))
                continue

            if size > MAX_FILE_SIZE_BYTES:
                self.record_skipped((rel_path, size, "Exceeds GitHub 100 MiB standard Git limit (no automatic LFS)"))
                self.total_skipped_size += size
                continue

            entry = FileEntry(src_path=full_path, rel_path=rel_path, size=size)
            self.valid_files.append(entry)
            self.total_valid_size += size

        self.valid_files.sort(key=lambda e: str(e.rel_path).lower())
        self.progress_total_files = len(self.valid_files)
        self.skipped_files.sort(key=lambda x: str(x[0]).lower())
        self.total_batches = sum(1 for _ in batch_sizes(self.valid_files, self.max_files))

        self.write_preview(all_dirs, dst)
        self.log("Scan complete.")

        # initialize progress after scan
        self.total_files_pushed = 0
        self.total_bytes_pushed = 0
        self.current_batch = 0
        self.current_batch_files = 0
        self.current_batch_total = 0
        self.update_progress()


    def update_progress(self) -> None:
        """
        update the progress information displayed in the application
        :returns: nothing
        """
        def _update():
            """
            refresh progress widgets on the interface thread
            :returns: nothing
            """
            self.progress_label.config(
                text=(
                    f"Batch: {self.current_batch}\n"
                    f"Batch Files: {self.current_batch_files}/{self.current_batch_total}\n"
                    f"\n"
                    f"Files Processed: {self.total_files_pushed}/{self.progress_total_files}\n"
                    f"Data Processed: {self.human_size(self.total_bytes_pushed)} / "
                    f"{self.human_size(self.total_valid_size)}"
                )
            )
            total = self.progress_total_files
            self.progress_bar.configure(value=(self.total_files_pushed / total * 100) if total else 0)

        self.root.after(0, _update)


    def make_batches(self, files: list[FileEntry]) -> list[list[FileEntry]]:
        """
        divide valid files into batches that do not exceed the maximum batch size
        :param files: list of valid files to organize into batches
        :returns: list of file batches
        """
        return make_batches(files)


    def record_skipped(self, item):
        self.skipped_count += 1
        if len(self.skipped_files) < 500:
            self.skipped_files.append(item)

    def write_preview(self, all_dirs: int, dst: Path) -> None:
        # Keep Tk's text buffers and preview planning bounded as well as the queue.
        valid_lines = []
        with DiskSet() as used:
            for index, entry in enumerate(self.valid_files):
                if index >= 500:
                    valid_lines.append("Preview limited to 500 files; all files remain queued.")
                    break
                target = self.build_target_path_preview(dst, entry, used)
                valid_lines.append(f"{entry.rel_path} -> {target} | {human_size(entry.size)}")
        self.ui(self.valid_text.insert, "1.0", "\n".join(valid_lines) or "No valid files.")
        self.ui(self.skipped_text.insert, "1.0", "\n".join(
            f"{path} | {human_size(size)} | {reason}" for path, size, reason in self.skipped_files
        ) or "No skipped files.")
        lines = []
        for index, (count, size) in enumerate(batch_sizes(self.valid_files, self.max_files), 1):
            if index > 500:
                lines.append("Preview limited to 500 batches.")
                break
            lines.append(f"Batch {index}: {count} files, {human_size(size)}")
        self.ui(self.batch_text.insert, "1.0", "\n".join(lines) or "No batches.")
        self.ui(self.summary_label.config, text=(
            f"Valid files: {len(self.valid_files)} ({human_size(self.total_valid_size)})\n"
            f"Skipped files: {self.skipped_count} ({human_size(self.total_skipped_size)})\n"
            f"Folders discovered: {all_dirs}\nGit batches needed: {self.total_batches}\n"
            f"Limits: file <= 100 MiB, batch <= 200 MiB; "
            f"maximum files: {self.max_files or 'unlimited'}"
        ))


    @staticmethod
    def is_supported_media(path: Path) -> bool:
        """
        determine whether a file is a supported media type
        :param path: file path to check
        :returns: whether the file is a supported media type
        """
        return is_supported_media(path)


    @staticmethod
    def sanitize_timestamp_filename(dt: datetime) -> str:
        """
        format a datetime value into a filename safe timestamp
        :param dt: datetime value to format
        :returns: formatted timestamp string
        """
        return sanitize_timestamp_filename(dt)


    def get_media_datetime(self, file_path: Path) -> datetime:
        """
        retrieve the date and time associated with a media file
        :param file_path: path to the media file
        :returns: datetime associated with the media file
        """
        return get_media_datetime(file_path)


    def build_target_path(self, dst: Path, entry: FileEntry, used_paths: set[Path],) -> Path:
        """
        build the destination path for a file while avoiding filename conflicts
        :param dst: destination folder
        :param entry: file entry being processed
        :param used_paths: set of destination paths already assigned
        :returns: final destination path for the file
        """
        return build_target_path(
            dst, entry.src_path, entry.rel_path, used_paths,
            self.organize_media_var.get(),
            self.media_only_for_supported_types_var.get(),
            self.media_use_date_folder_var.get(),
            self.media_rename_to_timestamp_var.get(),
        )


    def build_target_path_preview(self, dst: Path, entry: FileEntry, used_paths: set[Path]) -> Path:
        """
        build a preview destination path that matches the move logic
        :param dst: destination folder
        :param entry: file entry being processed
        :param used_paths: set of preview paths already assigned
        :returns: preview destination path for the file
        """
        return self.build_target_path(dst, entry, used_paths)
    

    def show_info(self, title, message):
        """
        display an informational message dialog
        :param title: title of the dialog
        :param message: message displayed in the dialog
        :returns: nothing
        """
        self.root.after(
            0,
            lambda: messagebox.showinfo(title, message)
        )


    def show_error(self, title, message):
        """
        display an error message dialog
        :param title: title of the dialog
        :param message: message displayed in the dialog
        :returns: nothing
        """
        self.root.after(
            0,
            lambda: messagebox.showerror(title, message)
        )


    def ask_yes_no(self, title, message):
        """
        display a confirmation dialog and return the user response
        :param title: title of the dialog
        :param message: message displayed in the dialog
        :returns: boolean indicating the user response
        """
        result = []
        event = threading.Event()

        def ask():
            """
            display confirmation on the interface thread
            :returns: nothing
            """
            result.append(messagebox.askyesno(title, message))
            event.set()

        self.root.after(0, ask)

        event.wait()
        return result[0]


    def move_and_push(self) -> None:
        """
        move files to the destination repository and perform Git operations
        :returns: nothing
        """
        operation = None
        try:
            repo_root = Path(self.dest_var.get()).resolve()
            recovering = getattr(self, "recovery_requested", False)
            if recovering:
                repo_root = getattr(self, "saved_operation_repo", repo_root)
                self.recovery_requested = False
            recover_discard(repo_root)
            saved = operation_file(repo_root)
            if saved.exists():
                operation = BatchOperation(repo_root)
                if operation.state["status"] == "Complete":
                    operation.close()
                    operation = None
                    saved.unlink()
                elif operation.cancel_mode() and not recovering:
                    self.set_status(operation.state["status"])
                    self.show_info("Canceled job", "Choose Resume to recover the saved transfer, "
                                   "Cleanup to remove verified copies, or Discard Canceled Job "
                                   "to abandon it and start a new transfer.")
                    return
            if operation is None:
                if recovering or (getattr(self, "pending_operation", False) and not hasattr(self, "saved_operation_repo")):
                    resume_transfers(repo_root, self.remote_branch_var.get(), self.log)
                    self.pending_operation = False
                    self.set_status("Complete")
                    return
                validated = self.validate_paths()
                if not validated:
                    self.set_status("Error")
                    return
                src, repo_root, dst = validated
                branch = self.remote_branch_var.get().strip() or "main"
                check_staging_support(repo_root)
                resume_transfers(repo_root, branch, self.log)
                # Re-scan so changed paths/options cannot reuse an obsolete preview.
                self.scan_and_preview()
                if not self.valid_files:
                    self.set_status("Complete")
                    return
                if not self.ask_yes_no("Confirm move", "Copy, verify, commit and push files, "
                                       "then remove verified sources?"):
                    self.set_status("Paused")
                    return
                if self.include_empty_dirs_var.get():
                    self.create_empty_directories(src, dst)
                self.session_file.parent.mkdir(parents=True, exist_ok=True)
                write_journal(self.session_file, {"source": str(src), "repo": str(repo_root)})
                with DiskSet() as used_paths:
                    pairs = ((entry.src_path, self.build_target_path(dst, entry, used_paths))
                             for entry in self.valid_files)
                    operation = BatchOperation.create(repo_root, pairs, branch,
                                                       self.commit_prefix_var.get() or "update",
                                                       self.max_files)
                operation.state["source_root"] = str(src)
                operation.save()
            self.pending_operation = True
            self.operation = operation
            self.saved_operation_repo = repo_root
            operation.pause = self.pause_event
            operation.set_limit(self.max_files)
            self.progress_total_files, self.total_valid_size = operation.db.execute(
                "SELECT count(*),coalesce(sum(size),0) FROM queue").fetchone()

            def status(value):
                self.total_files_pushed = operation.state["completed"]
                self.total_bytes_pushed = operation.state["bytes"]
                active = operation.state["active"]
                self.current_batch = operation.state["batch"] + bool(active)
                self.current_batch_total = active["count"] if active else 0
                self.current_batch_files = 0
                self.update_progress()
                self.set_status(value)

            def progress(message):
                if self.pause_event.is_set() and not operation.cancel_mode():
                    self.set_status("Finishing Batch")
                self.log(message)

            operation.run(progress, status)
            if operation.state["status"] == "Complete":
                self.pending_operation = False
                if operation.state.get("source_root"):
                    self.cleanup_empty_source_dirs(Path(operation.state["source_root"]))
                self.show_info("Done", "Move and verified git push completed.")
        except Exception as exc:
            self.log(f"ERROR: {exc}")
            if operation and operation.cancel_mode():
                self.set_status(operation.state["status"])
            else:
                self.set_status("Error")
            self.show_error("Error", str(exc) + "\nUse Resume to retry safely.")
        finally:
            self.operation = None
            if operation:
                operation.close()


    def create_empty_directories(self, src: Path, dst: Path) -> None:
        """
        recreate empty source directories in the destination folder
        :param src: source folder
        :param dst: destination folder
        :returns: nothing
        """
        for path, is_directory in walk_entries(src):
            if is_directory:
                with os.scandir(path) as entries:
                    empty = next(entries, None) is None
                if empty:
                    (dst / path.relative_to(src)).mkdir(parents=True, exist_ok=True)
                    self.log(f"Created empty folder: {path.relative_to(src)}")


    def cleanup_empty_source_dirs(self, src: Path) -> None:
        """
        remove empty directories from the source folder
        :param src: source folder
        :returns: nothing
        """
        if not src.is_dir():
            return
        for path, is_directory in walk_entries(src, bottom_up=True):
            if is_directory:
                try:
                    path.rmdir()
                    self.log(f"Removed empty source folder: {path.relative_to(src)}")
                except OSError:
                    pass


    def run_git_sequence(
        self, repo_path: Path, commit_message: str, moved_paths: list[Path]
    ) -> None:
        """
        run the Git add commit and push commands for the repository
        :param repo_path: path to the Git repository
        :param commit_message: commit message to use
        :param moved_paths: paths moved by the application
        :returns: nothing
        """
        self.set_status("Staging, committing, and pushing moved files")
        stage_commit_push(
            repo_path, moved_paths, commit_message,
            self.remote_branch_var.get().strip() or "main",
        )


    def run_command(self, command: list[str], cwd: Path, capture_output: bool = False) -> str:
        """
        execute a command and optionally return its output
        :param command: command to execute
        :param cwd: working directory for the command
        :param capture_output: whether command output should be returned
        :returns: command output or an empty string
        """
        self.log(f"Running: {' '.join(command)}")
        result = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            shell=False,
            check=False,
        )

        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if stdout:
            self.log(stdout)
        if stderr:
            self.log(stderr)

        if result.returncode != 0:
            raise RuntimeError(
                f"Command failed ({result.returncode}): {' '.join(command)}\n{stderr or stdout or 'No output'}"
            )

        return stdout if capture_output else ""
    

    def ui(self, func, *args, **kwargs):
        """
        schedule a function to run on the main user interface thread
        :param func: function to execute
        :param args: positional arguments for the function
        :param kwargs: keyword arguments for the function
        :returns: nothing
        """
        self.root.after(0, lambda: func(*args, **kwargs))


    def make_commit_message(self, batch_index: int, total_batches: int, file_count: int, moved_bytes: int) -> str:
        """
        create a commit message describing the completed batch
        :param batch_index: current batch number
        :param total_batches: total number of batches
        :param file_count: number of files moved
        :param moved_bytes: total number of bytes moved
        :returns: formatted commit message
        """
        return make_commit_message(
            self.commit_prefix_var.get(),
            batch_index,
            total_batches,
            file_count,
            moved_bytes,
            datetime.now().strftime("[%m][%d][%Y] [%H:%M:%S]"),
        )


    def show_github_link(self, log_only: bool = False) -> None:
        """
        display or log the GitHub repository link
        :param log_only: whether to only write the link to the log
        :returns: nothing
        """
        validated = self.validate_paths()
        if not validated:
            return
        _, repo_root, _ = validated

        try:
            remote_url = self.run_command(["git", "remote", "get-url", "origin"], repo_root, capture_output=True)
            http_url = self.normalize_github_url(remote_url)
            if http_url:
                self.log(f"GitHub link: {http_url}")
                if not log_only:
                    self.show_info("GitHub Link", http_url)
            else:
                self.log(f"Remote origin URL: {remote_url}")
                if not log_only:
                    self.show_info("Remote URL", remote_url)
        except Exception as exc:
            self.log(f"Could not determine GitHub link: {exc}")
            if not log_only:
                self.show_error("GitHub link error", str(exc))


    @staticmethod
    def normalize_github_url(remote_url: str) -> str | None:
        """
        convert a Git remote URL into a standard GitHub web address
        :param remote_url: git remote URL
        :returns: normalized GitHub URL or None
        """
        return normalize_github_url(remote_url)


    @staticmethod
    def human_size(size_bytes: int) -> str:
        """
        convert a file size into a readable string
        :param size_bytes: file size in bytes
        :returns: human readable file size
        """
        return human_size(size_bytes)
        


def main() -> None:
    """
    create and start the application
    :returns: nothing
    """
    root = tk.Tk()
    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")
    FileMoverGitApp(root)
    root.mainloop()

# main guard
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
