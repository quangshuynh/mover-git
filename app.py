import os
import shutil
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
    human_size,
    make_batches,
    make_commit_message,
    validate_paths,
)
from mover_git.git import (
    check_staging_support,
    finish_pending_git,
    normalize_github_url,
    stage_commit_push,
)
from mover_git.media import (
    MEDIA_FILE_TYPES,
    build_target_path,
    get_media_datetime,
    is_supported_media,
    sanitize_timestamp_filename,
)

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

        self._build_ui()


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

        ttk.Checkbutton(
            options_frame,
            text="Commit/push after each 2 GB batch",
            variable=self.commit_each_batch_var,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=4)

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
        ttk.Button(buttons_frame, text="Clear Log", command=self.clear_log).pack(side="left", padx=8)
        ttk.Button(buttons_frame, text="Show GitHub Link", command=self.show_github_link).pack(side="left", padx=8)

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
        if self.is_busy:
            return
        self.set_busy(True, "Scanning")
        threading.Thread(target=self._scan_worker, daemon=True).start()


    def move_in_thread(self) -> None:
        """
        start the move and push process in a background thread
        :returns: nothing
        """
        if self.is_busy:
            return
        self.set_busy(True, "Moving files")
        threading.Thread(target=self._move_worker, daemon=True).start()

    def _scan_worker(self) -> None:
        """
        run a scan and restore the idle interface state
        :returns: nothing
        """
        try:
            self.scan_and_preview()
        finally:
            self.set_busy(False, "Scan complete")

    def _move_worker(self) -> None:
        """
        run a move operation and restore the idle interface state
        :returns: nothing
        """
        try:
            self.move_and_push()
        finally:
            self.set_busy(False)

    def set_busy(self, busy: bool, status: str | None = None) -> None:
        """
        update action availability and status text
        :param busy: whether a background operation is active
        :param status: optional status text to display
        :returns: nothing
        """
        self.is_busy = busy
        state = "disabled" if busy else "normal"
        self.ui(self.scan_button.configure, state=state)
        self.ui(self.move_button.configure, state=state)
        if status is not None:
            self.set_status(status)

    def set_status(self, status: str) -> None:
        """
        display the current application operation
        :param status: status text to display
        :returns: nothing
        """
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

        self.valid_files = []
        self.skipped_files = []
        self.batches = []
        self.total_valid_size = 0
        self.total_skipped_size = 0

        self.ui(self.valid_text.delete, "1.0", "end")
        self.ui(self.skipped_text.delete, "1.0", "end")
        self.ui(self.batch_text.delete, "1.0", "end")

        self.log("Scanning source folder...")

        all_dirs = set()
        for root_dir, _dirnames, filenames in os.walk(src):
            root_path = Path(root_dir)
            rel_dir = root_path.relative_to(src)
            all_dirs.add(rel_dir)

            for filename in filenames:
                full_path = root_path / filename
                rel_path = full_path.relative_to(src)

                if full_path.is_symlink():
                    self.skipped_files.append((rel_path, 0, "Skipped symlink"))
                    continue

                try:
                    size = full_path.stat().st_size
                except OSError as exc:
                    self.skipped_files.append((rel_path, 0, f"Unreadable: {exc}"))
                    continue

                if size > MAX_FILE_SIZE_BYTES:
                    self.skipped_files.append((rel_path, size, "Over 100 MB"))
                    self.total_skipped_size += size
                    continue

                entry = FileEntry(src_path=full_path, rel_path=rel_path, size=size)
                self.valid_files.append(entry)
                self.total_valid_size += size

        self.valid_files.sort(key=lambda e: str(e.rel_path).lower())
        self.skipped_files.sort(key=lambda x: str(x[0]).lower())
        self.batches = make_batches(self.valid_files)

        self.write_preview(all_dirs, dst)
        self.log("Scan complete.")

        # initialize progress after scan
        self.total_files_pushed = 0
        self.total_bytes_pushed = 0
        self.current_batch = 0
        self.total_batches = len(self.batches)
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
                    f"Batch: {self.current_batch}/{self.total_batches}\n"
                    f"Batch Files: {self.current_batch_files}/{self.current_batch_total}\n"
                    f"\n"
                    f"Files Pushed: {self.total_files_pushed}/{len(self.valid_files)}\n"
                    f"Data Pushed: {self.human_size(self.total_bytes_pushed)} / "
                    f"{self.human_size(self.total_valid_size)}"
                )
            )
            total = len(self.valid_files)
            self.progress_bar.configure(value=(self.total_files_pushed / total * 100) if total else 0)

        self.root.after(0, _update)


    def make_batches(self, files: list[FileEntry]) -> list[list[FileEntry]]:
        """
        divide valid files into batches that do not exceed the maximum batch size
        :param files: list of valid files to organize into batches
        :returns: list of file batches
        """
        return make_batches(files)


    def write_preview(self, all_dirs: set[Path], dst: Path) -> None:
        """
        display preview information for valid files skipped files batches and summary
        :param all_dirs: set of discovered directories from the source folder
        :param dst: destination folder for preview paths
        :returns: nothing
        """
        used_preview_paths: set[Path] = set()
        preview_paths = {
            entry.src_path: self.build_target_path_preview(dst, entry, used_preview_paths)
            for entry in self.valid_files
        }
        valid_lines = []
        for entry in self.valid_files:
            target_preview = preview_paths[entry.src_path]
            valid_lines.append(
                f"{entry.rel_path}  ->  {target_preview.relative_to(dst) if target_preview.is_relative_to(dst) else target_preview}  |  {self.human_size(entry.size)}"
            )

        self.ui(
            self.valid_text.insert,
            "1.0",
            "\n".join(valid_lines) if valid_lines else "No valid files found."
        )
        skipped_lines = []
        for rel_path, size, reason in self.skipped_files:
            size_text = self.human_size(size) if size else "0 B"
            skipped_lines.append(f"{rel_path}  |  {size_text}  |  {reason}")

        self.ui(
            self.skipped_text.insert,
            "1.0",
            "\n".join(skipped_lines) if skipped_lines else "No skipped files."
        )

        batch_lines = []
        for i, batch in enumerate(self.batches, start=1):
            batch_size = sum(x.size for x in batch)
            batch_lines.append(f"Batch {i}: {len(batch)} files, {self.human_size(batch_size)}")
            for entry in batch:
                target_preview = preview_paths[entry.src_path]
                shown_target = target_preview.relative_to(dst) if target_preview.is_relative_to(dst) else target_preview
                batch_lines.append(
                    f"    {entry.rel_path}  ->  {shown_target}  |  {self.human_size(entry.size)}"
                )
            batch_lines.append("")

        self.ui(
            self.batch_text.insert,
            "1.0",
            "\n".join(batch_lines) if batch_lines else "No batches created."
        )

        dir_count = len([d for d in all_dirs if str(d) != "."])
        skipped_count = len(self.skipped_files)
        valid_count = len(self.valid_files)
        batch_count = len(self.batches)

        organize_text = "On" if self.organize_media_var.get() else "Off"

        self.ui(
            self.summary_label.config,
            text=(
                f"Valid files: {valid_count} ({self.human_size(self.total_valid_size)})\n"
                f"Skipped files: {skipped_count} ({self.human_size(self.total_skipped_size)})\n"
                f"Folders discovered: {dir_count}\n"
                f"Git batches needed: {batch_count}\n"
                f"Target inside repo: {self.dest_subfolder_var.get().strip() or '.'}\n"
                f"Media organization: {organize_text}\n"
                f"Limits: file <= 100 MB, batch <= 2 GB"
            )
        )


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
        self.set_status("Validating folders and repository")
        validated = self.validate_paths()
        if not validated:
            self.set_status("Validation failed")
            return
        src, repo_root, dst = validated

        try:
            check_staging_support(repo_root)
            if finish_pending_git(repo_root):
                self.cleanup_empty_source_dirs(src)
                self.scan_and_preview()
        except Exception as exc:
            self.log(f"ERROR: {exc}")
            self.set_status("Git work pending; retry Move + Git Push after fixing the error")
            self.show_error("Git recovery", str(exc))
            return

        if not self.valid_files:
            self.log("No scan data found. Running scan first...")
            self.scan_and_preview()
            if not self.valid_files:
                self.log("Nothing to move.")
                self.set_status("No files to move")
                return

        self.set_status("Checking Git repository status")
        existing_status = self.run_command(
            ["git", "status", "--porcelain"], repo_root, capture_output=True
        )
        if existing_status.strip():
            self.log("Destination repository has pre-existing changes; move cancelled.")
            self.show_error(
                "Repository has changes",
                "Commit, stash, or remove existing repository changes before moving files.",
            )
            self.set_status("Move cancelled because the repository has changes")
            return

        confirm = self.ask_yes_no(
            "Confirm move",
            "This will move valid files to the destination repo and run git add/commit/push. Continue?",
        )
        if not confirm:
            self.set_status("Move cancelled")
            return
        
        # init progress variables
        self.total_files_pushed = 0
        self.total_bytes_pushed = 0
        self.current_batch = 0
        self.total_batches = len(self.batches)
        self.current_batch_files = 0
        self.current_batch_total = 0
        self.update_progress()
        
        #automatically switch to log tab
        self.ui(self.notebook.select, 3)
        self.ui(self.root.update_idletasks)

        try:
            if self.include_empty_dirs_var.get():
                self.create_empty_directories(src, dst)

            if self.commit_each_batch_var.get():
                for batch_index, batch in enumerate(self.batches, start=1):
                    self.set_status(f"Moving files in batch {batch_index} of {len(self.batches)}")
                    used_move_paths = set()
                    moved_paths: list[Path] = []
                    # progress update for current batch
                    self.current_batch = batch_index
                    self.current_batch_files = 0
                    self.current_batch_total = len(batch)
                    self.update_progress()
                    self.log(f"Starting batch {batch_index}/{len(self.batches)}...")
                    moved_count = 0
                    moved_bytes = 0

                    for entry in batch:
                        target_path = self.build_target_path(dst, entry, used_move_paths)
                        target_path.parent.mkdir(parents=True, exist_ok=True)

                        if target_path.exists():
                            raise FileExistsError(
                                f"Destination already contains {target_path.relative_to(dst)}. "
                                f"Remove or rename {target_path.relative_to(dst)} from source before moving."
                            )

                        shutil.move(str(entry.src_path), str(target_path))
                        moved_paths.append(target_path)
                        moved_count += 1
                        moved_bytes += entry.size

                        # progress update after each file move
                        self.current_batch_files += 1
                        self.total_files_pushed += 1
                        self.total_bytes_pushed += entry.size
                        self.update_progress()

                        self.log(f"Moved: {entry.rel_path} -> {target_path.relative_to(dst)}")

                    self.current_batch_files = self.current_batch_total
                    self.update_progress()

                    commit_message = self.make_commit_message(batch_index, len(self.batches), moved_count, moved_bytes)
                    self.run_git_sequence(repo_root, commit_message, moved_paths)
                    self.cleanup_empty_source_dirs(src)
                    self.update_progress()
                    self.log(f"Finished batch {batch_index}.")
                    self.current_batch = batch_index
                    self.current_batch_files = self.current_batch_total
                    self.update_progress()
            else:
                self.log("Commit-each-batch disabled. Moving all files first, then committing once...")

                moved_count = 0
                moved_bytes = 0
                moved_paths = []

                for batch_index, batch in enumerate(self.batches, start=1):
                    self.set_status(f"Moving files in batch {batch_index} of {len(self.batches)}")
                    used_move_paths = set()
                    self.current_batch = batch_index
                    self.current_batch_files = 0
                    self.current_batch_total = len(batch)
                    self.update_progress()
                    self.log(f"Moving batch {batch_index}/{len(self.batches)}...")
                    for entry in batch:
                        target_path = self.build_target_path(dst, entry, used_move_paths)
                        target_path.parent.mkdir(parents=True, exist_ok=True)

                        if target_path.exists():
                            raise FileExistsError(
                                f"Destination already contains {target_path.relative_to(dst)}. "
                                f"Remove or rename it before moving."
                            )

                        shutil.move(str(entry.src_path), str(target_path))
                        moved_paths.append(target_path)
                        moved_count += 1
                        moved_bytes += entry.size

                        # progress update after each file move
                        self.current_batch_files += 1
                        self.total_files_pushed += 1
                        self.total_bytes_pushed += entry.size
                        self.update_progress()

                        self.log(f"Moved: {entry.rel_path} -> {target_path.relative_to(dst)}")

                commit_message = self.make_commit_message(1, 1, moved_count, moved_bytes)
                self.run_git_sequence(repo_root, commit_message, moved_paths)
                self.cleanup_empty_source_dirs(src)
                self.update_progress()
                self.log("Finished single commit/push for all moved files.")
            
            self.current_batch = self.total_batches

            if self.batches:
                self.current_batch_total = len(self.batches[-1])
                self.current_batch_files = self.current_batch_total

            self.update_progress()
            self.log("All batches completed successfully.")
            self.set_status("Move, commit, and push completed")
            self.show_github_link(log_only=True)
            self.show_info("Done", "Move and git push completed.")
        except Exception as exc:
            self.log(f"ERROR: {exc}")
            self.set_status(f"Error: {exc}")
            self.show_error("Error", str(exc) + "\nRetry Move + Git Push to resume pending Git work.")


    def create_empty_directories(self, src: Path, dst: Path) -> None:
        """
        recreate empty source directories in the destination folder
        :param src: source folder
        :param dst: destination folder
        :returns: nothing
        """
        for root_dir, dirnames, filenames in os.walk(src):
            root_path = Path(root_dir)
            rel_dir = root_path.relative_to(src)
            target_dir = dst / rel_dir
            if not filenames and not dirnames:
                target_dir.mkdir(parents=True, exist_ok=True)
                self.log(f"Created empty folder: {rel_dir}")


    def cleanup_empty_source_dirs(self, src: Path) -> None:
        """
        remove empty directories from the source folder
        :param src: source folder
        :returns: nothing
        """
        for root_dir, _dirnames, _filenames in os.walk(src, topdown=False):
            root_path = Path(root_dir)
            if root_path == src:
                continue
            try:
                if not any(root_path.iterdir()):
                    root_path.rmdir()
                    self.log(f"Removed empty source folder: {root_path.relative_to(src)}")
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
