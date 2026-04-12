import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse


MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024          # 100 MB
MAX_BATCH_SIZE_BYTES = 2 * 1024 * 1024 * 1024    # 2 GB


@dataclass
class FileEntry:
    src_path: Path
    rel_path: Path
    size: int


class FileMoverGitApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("File Mover + Git Push")
        self.root.geometry("980x760")

        self.source_var = tk.StringVar()
        self.dest_var = tk.StringVar()
        self.dest_subfolder_var = tk.StringVar(value="")
        self.commit_prefix_var = tk.StringVar(value="")
        self.remote_branch_var = tk.StringVar(value="main")
        self.commit_each_batch_var = tk.BooleanVar(value=True)
        self.include_empty_dirs_var = tk.BooleanVar(value=True)

        self.valid_files: list[FileEntry] = []
        self.skipped_files: list[tuple[Path, int, str]] = []
        self.batches: list[list[FileEntry]] = []
        self.total_valid_size = 0
        self.total_skipped_size = 0

        self._build_ui()

    def _build_ui(self) -> None:
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

        buttons_frame = ttk.Frame(outer)
        buttons_frame.pack(fill="x", pady=(10, 0))

        ttk.Button(buttons_frame, text="1) Scan and Preview", command=self.scan_in_thread).pack(side="left", padx=(0, 8))
        ttk.Button(buttons_frame, text="2) Move + Git Push", command=self.move_in_thread).pack(side="left", padx=8)
        ttk.Button(buttons_frame, text="Clear Log", command=self.clear_log).pack(side="left", padx=8)
        ttk.Button(buttons_frame, text="Show GitHub Link", command=self.show_github_link).pack(side="left", padx=8)

        summary_frame = ttk.LabelFrame(outer, text="Summary", padding=10)
        summary_frame.pack(fill="x", pady=(10, 0))
        self.summary_label = ttk.Label(summary_frame, text="No scan yet.", justify="left")
        self.summary_label.pack(anchor="w")

        preview_frame = ttk.LabelFrame(outer, text="Preview", padding=10)
        preview_frame.pack(fill="both", expand=True, pady=(10, 0))

        notebook = ttk.Notebook(preview_frame)
        notebook.pack(fill="both", expand=True)

        self.valid_text = self._make_text_tab(notebook, "Valid files")
        self.skipped_text = self._make_text_tab(notebook, "Skipped files")
        self.batch_text = self._make_text_tab(notebook, "Batches")
        self.log_text = self._make_text_tab(notebook, "Log")

    def _make_text_tab(self, notebook: ttk.Notebook, title: str) -> tk.Text:
        frame = ttk.Frame(notebook)
        notebook.add(frame, text=title)

        text = tk.Text(frame, wrap="word", height=10)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)

        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        return text

    def log(self, message: str) -> None:
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.root.update_idletasks()

    def clear_log(self) -> None:
        self.log_text.delete("1.0", "end")

    def pick_source(self) -> None:
        folder = filedialog.askdirectory(title="Select source folder")
        if folder:
            self.source_var.set(folder)

    def pick_dest(self) -> None:
        folder = filedialog.askdirectory(title="Select destination repo folder")
        if folder:
            self.dest_var.set(folder)

    def pick_dest_subfolder(self) -> None:
        repo_root = self.dest_var.get().strip()
        if not repo_root:
            messagebox.showerror("Pick repo first", "Please choose the destination repo folder first.")
            return

        chosen = filedialog.askdirectory(title="Select a subfolder inside the repo", initialdir=repo_root)
        if chosen:
            try:
                relative = Path(chosen).resolve().relative_to(Path(repo_root).resolve())
                self.dest_subfolder_var.set("" if str(relative) == "." else str(relative))
            except ValueError:
                messagebox.showerror("Invalid subfolder", "The selected folder must be inside the destination repo.")

    def scan_in_thread(self) -> None:
        threading.Thread(target=self.scan_and_preview, daemon=True).start()

    def move_in_thread(self) -> None:
        threading.Thread(target=self.move_and_push, daemon=True).start()

    def validate_paths(self) -> tuple[Path, Path, Path] | None:
        src = Path(self.source_var.get().strip())
        repo_root = Path(self.dest_var.get().strip())
        subfolder_text = self.dest_subfolder_var.get().strip()

        if not src.exists() or not src.is_dir():
            messagebox.showerror("Invalid source", "Please choose a valid source folder.")
            return None

        if not repo_root.exists() or not repo_root.is_dir():
            messagebox.showerror("Invalid destination", "Please choose a valid destination folder.")
            return None

        git_dir = repo_root / ".git"
        if not git_dir.exists() or not git_dir.is_dir():
            messagebox.showerror("Not a Git repo", "Destination folder must already contain a .git folder.")
            return None

        try:
            dest_subfolder = (repo_root / subfolder_text).resolve() if subfolder_text else repo_root.resolve()
        except OSError as exc:
            messagebox.showerror("Invalid subfolder", f"Could not resolve subfolder: {exc}")
            return None

        try:
            dest_subfolder.relative_to(repo_root.resolve())
        except ValueError:
            messagebox.showerror("Invalid subfolder", "Subfolder must stay inside the destination repo.")
            return None

        try:
            src.resolve().relative_to(dest_subfolder)
            messagebox.showerror("Invalid folders", "Source folder cannot be inside destination target folder.")
            return None
        except ValueError:
            pass

        try:
            dest_subfolder.relative_to(src.resolve())
            messagebox.showerror("Invalid folders", "Destination target folder cannot be inside source folder.")
            return None
        except ValueError:
            pass

        return src, repo_root, dest_subfolder

    def scan_and_preview(self) -> None:
        validated = self.validate_paths()
        if not validated:
            return
        src, _, _ = validated

        self.valid_files = []
        self.skipped_files = []
        self.batches = []
        self.total_valid_size = 0
        self.total_skipped_size = 0

        self.valid_text.delete("1.0", "end")
        self.skipped_text.delete("1.0", "end")
        self.batch_text.delete("1.0", "end")

        self.log("Scanning source folder...")

        all_dirs = set()
        for root_dir, dirnames, filenames in os.walk(src):
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
        self.batches = self.make_batches(self.valid_files)

        self.write_preview(all_dirs)
        self.log("Scan complete.")

    def make_batches(self, files: list[FileEntry]) -> list[list[FileEntry]]:
        batches: list[list[FileEntry]] = []
        current_batch: list[FileEntry] = []
        current_size = 0

        for entry in files:
            if entry.size > MAX_BATCH_SIZE_BYTES:
                continue

            if current_batch and current_size + entry.size > MAX_BATCH_SIZE_BYTES:
                batches.append(current_batch)
                current_batch = []
                current_size = 0

            current_batch.append(entry)
            current_size += entry.size

        if current_batch:
            batches.append(current_batch)

        return batches

    def write_preview(self, all_dirs: set[Path]) -> None:
        valid_lines = []
        for entry in self.valid_files:
            valid_lines.append(f"{entry.rel_path}  |  {self.human_size(entry.size)}")
        self.valid_text.insert("1.0", "\n".join(valid_lines) if valid_lines else "No valid files found.")

        skipped_lines = []
        for rel_path, size, reason in self.skipped_files:
            size_text = self.human_size(size) if size else "0 B"
            skipped_lines.append(f"{rel_path}  |  {size_text}  |  {reason}")
        self.skipped_text.insert("1.0", "\n".join(skipped_lines) if skipped_lines else "No skipped files.")

        batch_lines = []
        for i, batch in enumerate(self.batches, start=1):
            batch_size = sum(x.size for x in batch)
            batch_lines.append(f"Batch {i}: {len(batch)} files, {self.human_size(batch_size)}")
            for entry in batch:
                batch_lines.append(f"    {entry.rel_path}  |  {self.human_size(entry.size)}")
            batch_lines.append("")
        self.batch_text.insert("1.0", "\n".join(batch_lines) if batch_lines else "No batches created.")

        dir_count = len([d for d in all_dirs if str(d) != "."])
        skipped_count = len(self.skipped_files)
        valid_count = len(self.valid_files)
        batch_count = len(self.batches)

        self.summary_label.config(
            text=(
                f"Valid files: {valid_count} ({self.human_size(self.total_valid_size)})\n"
                f"Skipped files: {skipped_count} ({self.human_size(self.total_skipped_size)})\n"
                f"Folders discovered: {dir_count}\n"
                f"Git batches needed: {batch_count}\n"
                f"Target inside repo: {self.dest_subfolder_var.get().strip() or '.'}\n"
                f"Limits: file <= 100 MB, batch <= 2 GB"
            )
        )

    def move_and_push(self) -> None:
        validated = self.validate_paths()
        if not validated:
            return
        src, repo_root, dst = validated

        if not self.valid_files:
            self.log("No scan data found. Running scan first...")
            self.scan_and_preview()
            if not self.valid_files:
                self.log("Nothing to move.")
                return

        confirm = messagebox.askyesno(
            "Confirm move",
            "This will move valid files to the destination repo and run git add/commit/push. Continue?",
        )
        if not confirm:
            return

        try:
            if self.include_empty_dirs_var.get():
                self.create_empty_directories(src, dst)

            for batch_index, batch in enumerate(self.batches, start=1):
                self.log(f"Starting batch {batch_index}/{len(self.batches)}...")
                moved_count = 0
                moved_bytes = 0

                for entry in batch:
                    target_path = dst / entry.rel_path
                    target_path.parent.mkdir(parents=True, exist_ok=True)

                    if target_path.exists():
                        raise FileExistsError(
                            f"Destination already contains {entry.rel_path}. Remove or rename it before moving."
                        )

                    shutil.move(str(entry.src_path), str(target_path))
                    moved_count += 1
                    moved_bytes += entry.size
                    self.log(f"Moved: {entry.rel_path}")

                self.cleanup_empty_source_dirs(src)

                commit_message = self.make_commit_message(batch_index, len(self.batches), moved_count, moved_bytes)
                self.run_git_sequence(repo_root, commit_message)
                self.log(f"Finished batch {batch_index}.")

                if not self.commit_each_batch_var.get() and batch_index < len(self.batches):
                    self.log("Commit-each-batch disabled, but batches still commit individually to respect 2 GB limit.")

            self.log("All batches completed successfully.")
            self.show_github_link(log_only=True)
            messagebox.showinfo("Done", "Move and git push completed.")
        except Exception as exc:
            self.log(f"ERROR: {exc}")
            messagebox.showerror("Error", str(exc))

    def create_empty_directories(self, src: Path, dst: Path) -> None:
        for root_dir, dirnames, filenames in os.walk(src):
            root_path = Path(root_dir)
            rel_dir = root_path.relative_to(src)
            target_dir = dst / rel_dir
            if not filenames and not dirnames:
                target_dir.mkdir(parents=True, exist_ok=True)
                self.log(f"Created empty folder: {rel_dir}")

    def cleanup_empty_source_dirs(self, src: Path) -> None:
        for root_dir, dirnames, filenames in os.walk(src, topdown=False):
            root_path = Path(root_dir)
            if root_path == src:
                continue
            try:
                if not any(root_path.iterdir()):
                    root_path.rmdir()
                    self.log(f"Removed empty source folder: {root_path.relative_to(src)}")
            except OSError:
                pass

    def run_git_sequence(self, repo_path: Path, commit_message: str) -> None:
        self.run_command(["git", "status", "--short"], repo_path)
        self.run_command(["git", "add", "."], repo_path)

        status_after_add = self.run_command(["git", "status", "--short"], repo_path, capture_output=True)
        if not status_after_add.strip():
            self.log("No git changes detected after add; skipping commit/push.")
            return

        self.run_command(["git", "commit", "-m", commit_message], repo_path)
        self.run_command(
            ["git", "push", "-u", "origin", self.remote_branch_var.get().strip() or "main"],
            repo_path,
        )

    def run_command(self, command: list[str], cwd: Path, capture_output: bool = False) -> str:
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

    def make_commit_message(self, batch_index: int, total_batches: int, file_count: int, moved_bytes: int) -> str:
        now = datetime.now()
        timestamp = now.strftime("[%m][%d][%Y] [%H:%M:%S]")
        prefix = self.commit_prefix_var.get().strip()

        # Default fallback message if user provides nothing
        base_message = prefix if prefix else "update"

        # If multiple batches, increment message: update, update 2, update 3...
        if total_batches > 1:
            if batch_index == 1:
                batch_suffix = ""
            else:
                batch_suffix = f" {batch_index}"
        else:
            batch_suffix = ""

        final_message = f"{base_message}{batch_suffix}"

        details = f"- {file_count} files - {self.human_size(moved_bytes)}"

        return f"{timestamp} {final_message} {details}"

    def show_github_link(self, log_only: bool = False) -> None:
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
                    messagebox.showinfo("GitHub Link", http_url)
            else:
                self.log(f"Remote origin URL: {remote_url}")
                if not log_only:
                    messagebox.showinfo("Remote URL", remote_url)
        except Exception as exc:
            self.log(f"Could not determine GitHub link: {exc}")
            if not log_only:
                messagebox.showerror("GitHub link error", str(exc))

    @staticmethod
    def normalize_github_url(remote_url: str) -> str | None:
        remote_url = remote_url.strip()
        if not remote_url:
            return None

        if remote_url.startswith("git@github.com:"):
            path = remote_url.split(":", 1)[1]
            if path.endswith(".git"):
                path = path[:-4]
            return f"https://github.com/{path}"

        if remote_url.startswith("https://") or remote_url.startswith("http://"):
            parsed = urlparse(remote_url)
            if "github.com" in parsed.netloc:
                path = parsed.path[:-4] if parsed.path.endswith(".git") else parsed.path
                return f"https://github.com{path}"

        return None

    @staticmethod
    def human_size(size_bytes: int) -> str:
        units = ["B", "KB", "MB", "GB", "TB"]
        size = float(size_bytes)
        for unit in units:
            if size < 1024 or unit == units[-1]:
                return f"{size:.2f} {unit}"
            size /= 1024
        return f"{size_bytes} B"


def main() -> None:
    root = tk.Tk()
    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")
    app = FileMoverGitApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
