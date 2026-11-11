"""Smart File Organizer - standard-library desktop application (Python 3.10+)."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

CATEGORIES = {
    'Images': {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.svg', '.heic', '.tiff'},
    'Documents': {'.pdf', '.doc', '.docx', '.txt', '.rtf', '.odt', '.md', '.epub'},
    'Spreadsheets': {'.xls', '.xlsx', '.csv', '.ods'},
    'Presentations': {'.ppt', '.pptx', '.odp'},
    'Videos': {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.webm', '.m4v'},
    'Audio': {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a'},
    'Archives': {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2'},
    'Code': {'.py', '.js', '.ts', '.html', '.css', '.java', '.cpp', '.c', '.go', '.rs', '.json', '.xml', '.sql', '.ipynb'},
    'Applications': {'.exe', '.msi', '.dmg', '.apk', '.deb', '.rpm'},
    'Fonts': {'.ttf', '.otf', '.woff', '.woff2'},
}
APP_DIR = Path.home() / '.smart_file_organizer'
HISTORY_PATH = APP_DIR / 'history.json'
LOG_PATH = APP_DIR / 'activity.log'


def category_for(file: Path) -> str:
    extension = file.suffix.lower()
    return next((name for name, extensions in CATEGORIES.items() if extension in extensions), 'Others')


def available_path(path: Path, reserved: set[Path]) -> Path:
    candidate = path
    number = 1
    while candidate.exists() or candidate in reserved:
        candidate = path.with_name(f'{path.stem} ({number}){path.suffix}')
        number += 1
    reserved.add(candidate)
    return candidate


def make_plan(source: Path, destination: Path, by_date: bool) -> list[tuple[Path, Path]]:
    if source.resolve() == destination.resolve():
        raise ValueError('Source and destination must be different folders.')
    # Nonrecursive by design: prevents reprocessing existing organized folders.
    files = sorted((p for p in source.iterdir() if p.is_file() and not p.is_symlink()), key=lambda p: p.name.lower())
    reserved: set[Path] = set()
    plan = []
    for file in files:
        folder = destination / category_for(file)
        if by_date:
            folder = folder / datetime.fromtimestamp(file.stat().st_mtime).strftime('%Y') / datetime.fromtimestamp(file.stat().st_mtime).strftime('%m')
        target = available_path(folder / file.name, reserved)
        plan.append((file, target))
    return plan


def load_history() -> list[dict]:
    try:
        data = json.loads(HISTORY_PATH.read_text(encoding='utf-8'))
        return data if isinstance(data, list) else []
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []


def save_history(history: list[dict]) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    temporary = HISTORY_PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps(history[-50:], indent=2), encoding='utf-8')
    temporary.replace(HISTORY_PATH)


def log(message: str) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open('a', encoding='utf-8') as f:
        f.write(f'{datetime.now():%Y-%m-%d %H:%M:%S} {message}\n')


class OrganizerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title('Smart File Organizer')
        root.geometry('920x650')
        root.minsize(760, 520)
        self.source = tk.StringVar(value=str(Path.home() / 'Downloads'))
        self.destination = tk.StringVar(value=str(Path.home() / 'OrganizedFiles'))
        self.by_date = tk.BooleanVar(value=False)
        self.monitor = tk.BooleanVar(value=False)
        self.interval = tk.StringVar(value='30')
        self.busy = False
        self.monitor_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.build_ui()
        root.protocol('WM_DELETE_WINDOW', self.close)

    def build_ui(self):
        outer = ttk.Frame(self.root, padding=18)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='Smart File Organizer', font=('Arial', 22, 'bold')).pack(anchor='w')
        ttk.Label(outer, text='Preview, organize, monitor, and undo file movements.').pack(anchor='w', pady=(0, 16))
        for label, variable in [('Source folder', self.source), ('Destination folder', self.destination)]:
            row = ttk.Frame(outer)
            row.pack(fill='x', pady=5)
            ttk.Label(row, text=label, width=19).pack(side='left')
            ttk.Entry(row, textvariable=variable).pack(side='left', fill='x', expand=True, padx=6)
            ttk.Button(row, text='Browse', command=lambda v=variable: self.browse(v)).pack(side='left')
        settings = ttk.Frame(outer)
        settings.pack(fill='x', pady=12)
        ttk.Checkbutton(settings, text='Organize by year/month', variable=self.by_date).pack(side='left')
        ttk.Label(settings, text='Monitor interval (seconds):').pack(side='left', padx=(30, 5))
        ttk.Entry(settings, textvariable=self.interval, width=7).pack(side='left')
        actions = ttk.Frame(outer)
        actions.pack(fill='x', pady=5)
        self.preview_btn = ttk.Button(actions, text='Preview', command=self.preview)
        self.preview_btn.pack(side='left', padx=(0, 8))
        self.organize_btn = ttk.Button(actions, text='Organize Files', command=self.organize)
        self.organize_btn.pack(side='left', padx=8)
        self.undo_btn = ttk.Button(actions, text='Undo Last Batch', command=self.undo)
        self.undo_btn.pack(side='left', padx=8)
        self.monitor_btn = ttk.Button(actions, text='Start Monitoring', command=self.toggle_monitor)
        self.monitor_btn.pack(side='left', padx=8)
        self.status = tk.StringVar(value='Ready. Select folders and click Preview.')
        ttk.Label(outer, textvariable=self.status).pack(anchor='w', pady=(14, 5))
        table_frame = ttk.Frame(outer)
        table_frame.pack(fill='both', expand=True)
        self.table = ttk.Treeview(table_frame, columns=('file', 'category', 'destination'), show='headings')
        for column, title, width in [('file', 'File', 220), ('category', 'Category', 130), ('destination', 'Target', 470)]:
            self.table.heading(column, text=title)
            self.table.column(column, width=width, minwidth=80)
        scrollbar = ttk.Scrollbar(table_frame, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=scrollbar.set)
        self.table.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        ttk.Label(outer, text=f'Activity log: {LOG_PATH}', font=('Arial', 9)).pack(anchor='w', pady=(8, 0))

    def browse(self, variable: tk.StringVar):
        path = filedialog.askdirectory(initialdir=variable.get() if Path(variable.get()).is_dir() else str(Path.home()))
        if path:
            variable.set(path)

    def paths(self) -> tuple[Path, Path]:
        source = Path(self.source.get()).expanduser().resolve()
        destination = Path(self.destination.get()).expanduser().resolve()
        if not source.is_dir():
            raise ValueError('The source folder does not exist.')
        if source == destination:
            raise ValueError('Choose different source and destination folders.')
        if source in destination.parents or destination in source.parents:
            raise ValueError('For safety, source and destination must not be nested inside each other.')
        return source, destination

    def show_error(self, error: Exception):
        self.status.set(f'Error: {error}')
        messagebox.showerror('Smart File Organizer', str(error))

    def preview(self):
        try:
            source, destination = self.paths()
            plan = make_plan(source, destination, self.by_date.get())
            self.table.delete(*self.table.get_children())
            for file, target in plan:
                self.table.insert('', 'end', values=(file.name, category_for(file), str(target)))
            self.status.set(f'Preview: {len(plan)} file(s) ready to organize. No files moved.')
        except (OSError, ValueError) as exc:
            self.show_error(exc)

    def run_worker(self, task):
        if self.busy:
            return
        self.busy = True
        self.set_buttons(False)
        def work():
            try:
                result = task()
                self.root.after(0, lambda: self.finish(result, None))
            except Exception as exc:
                self.root.after(0, lambda e=exc: self.finish(None, e))
        threading.Thread(target=work, daemon=True).start()

    def set_buttons(self, enabled: bool):
        state = 'normal' if enabled else 'disabled'
        for button in (self.preview_btn, self.organize_btn, self.undo_btn):
            button.configure(state=state)

    def finish(self, result, error):
        self.busy = False
        self.set_buttons(True)
        if error:
            self.show_error(error)
        else:
            self.status.set(result)
            self.preview()
            self.status.set(result)

    def organize(self):
        try:
            source, destination = self.paths()
            by_date = self.by_date.get()
        except (OSError, ValueError) as exc:
            self.show_error(exc)
            return
        self.run_worker(lambda: self.organize_batch(source, destination, by_date))

    def organize_batch(self, source: Path, destination: Path, by_date: bool) -> str:
        with self.lock:
            plan = make_plan(source, destination, by_date)
            moved = []
            errors = 0
            for file, target in plan:
                try:
                    if not file.exists():
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    # Recheck for collisions (files may have appeared after preview).
                    target = available_path(target, set())
                    shutil.move(str(file), str(target))
                    moved.append({'from': str(file), 'to': str(target)})
                    log(f'MOVED {file} -> {target}')
                except OSError as exc:
                    errors += 1
                    log(f'ERROR {file}: {exc}')
            if moved:
                history = load_history()
                history.append({'id': str(uuid.uuid4()), 'timestamp': datetime.now().isoformat(), 'moves': moved})
                save_history(history)
            return f'Organized {len(moved)} file(s). Errors: {errors}. See activity log for details.'

    def undo(self):
        if self.monitor.get():
            messagebox.showwarning('Monitoring active', 'Stop monitoring before undoing a batch.')
            return
        if not messagebox.askyesno('Confirm undo', 'Restore files from the most recent batch?'):
            return
        self.run_worker(self.undo_batch)

    def undo_batch(self) -> str:
        with self.lock:
            history = load_history()
            if not history:
                return 'Nothing to undo.'
            batch = history[-1]
            restored, errors = 0, 0
            remaining = []
            for entry in reversed(batch['moves']):
                src, dst = Path(entry['to']), Path(entry['from'])
                if not src.is_file():
                    errors += 1
                    remaining.append(entry)
                    log(f'UNDO SKIPPED missing {src}')
                    continue
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    # Never overwrite an existing source file during undo.
                    safe_dst = available_path(dst, set())
                    shutil.move(str(src), str(safe_dst))
                    restored += 1
                    log(f'RESTORED {src} -> {safe_dst}')
                except OSError as exc:
                    errors += 1
                    remaining.append(entry)
                    log(f'UNDO ERROR {src}: {exc}')
            if remaining:
                batch['moves'] = list(reversed(remaining))
            else:
                history.pop()
            save_history(history)
            return f'Restored {restored} file(s). Skipped/errors: {errors}.'

    def toggle_monitor(self):
        if self.monitor.get():
            self.stop_event.set()
            self.monitor.set(False)
            self.monitor_btn.configure(text='Start Monitoring')
            self.status.set('Monitoring stopped.')
            return
        try:
            source, destination = self.paths()
            seconds = int(self.interval.get())
            if seconds < 5:
                raise ValueError('Monitoring interval must be at least 5 seconds.')
        except (OSError, ValueError) as exc:
            self.show_error(exc)
            return
        by_date = self.by_date.get()
        self.stop_event.clear()
        self.monitor.set(True)
        self.monitor_btn.configure(text='Stop Monitoring')
        self.status.set('Monitoring started. New files will be organized automatically.')
        def monitor_loop():
            # Wait for one interval before first pass, avoiding moving incomplete downloads immediately.
            while not self.stop_event.wait(seconds):
                if self.busy:
                    continue
                try:
                    result = self.organize_batch(source, destination, by_date)
                    self.root.after(0, lambda r=result: self.status.set('Monitoring: ' + r))
                except Exception as exc:
                    log(f'MONITOR ERROR: {exc}')
        self.monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        self.monitor_thread.start()

    def close(self):
        self.stop_event.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    OrganizerApp(root)
    root.mainloop()


if __name__ == '__main__':
    main()
