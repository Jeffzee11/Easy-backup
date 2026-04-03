import os
import sys
import threading
import traceback
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from backup_manager import BackupManager
from config_manager import ConfigManager
from file_watcher import FileWatcher
from secure_credentials import SecureCredentialStore

try:
    import pystray
    from PIL import Image, ImageDraw, ImageTk
except Exception:
    pystray = None
    Image = None
    ImageDraw = None
    ImageTk = None

try:
    from tkcalendar import Calendar
except Exception:
    Calendar = None


APP_TITLE = "Easy Backup by PromptITEasy.com"


def _safe_config_int(val, default, lo=None, hi=None):
    try:
        n = int(val)
        if lo is not None and n < lo:
            return default
        if hi is not None and n > hi:
            return default
        return n
    except (TypeError, ValueError):
        return default


class CalendarSchedulerDialog(tk.Toplevel):
    def __init__(self, parent, slots):
        super().__init__(parent)
        self.title("Calendar Scheduler")
        self.resizable(False, False)
        self.saved = False
        self.slots = list(slots or [])
        _bg = "#e8e8e8"
        _fg = "#101010"
        _ent = "#ffffff"
        self.configure(bg=_bg)
        btn_kw = {"bg": "#d0d0d0", "fg": _fg, "activebackground": "#bbbbbb", "activeforeground": _fg, "font": ("Segoe UI", 9)}
        spin_kw = {"bg": _ent, "fg": _fg, "buttonbackground": _bg, "highlightthickness": 1}

        frame = tk.Frame(self, bg=_bg, padx=10, pady=10)
        frame.pack(fill="both", expand=True)
        frame.grid_columnconfigure(2, weight=1)

        if Calendar is None:
            tk.Label(
                frame,
                text="Mini calendar requires tkcalendar.\nInstall with: py -m pip install tkcalendar",
                bg=_bg,
                fg="red",
                font=("Segoe UI", 9),
            ).pack(anchor="w")
            tk.Button(frame, text="Close", command=self.destroy, **btn_kw).pack(anchor="e", pady=(10, 0))
            return

        self.cal = Calendar(frame, date_pattern="yyyy-mm-dd", selectmode="day")
        self.cal.grid(row=0, column=0, columnspan=3, sticky="w")

        tk.Label(frame, text="Hour", bg=_bg, fg=_fg, font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.hour_var = tk.IntVar(value=9)
        tk.Spinbox(frame, from_=0, to=23, textvariable=self.hour_var, width=5, format="%02.0f", **spin_kw).grid(
            row=2, column=0, sticky="w"
        )
        tk.Label(frame, text="Minute", bg=_bg, fg=_fg, font=("Segoe UI", 9)).grid(row=1, column=1, sticky="w", pady=(8, 0))
        self.minute_var = tk.IntVar(value=0)
        tk.Spinbox(frame, from_=0, to=59, textvariable=self.minute_var, width=5, format="%02.0f", **spin_kw).grid(
            row=2, column=1, sticky="w"
        )
        tk.Button(frame, text="Add Slot", command=self._add_slot, **btn_kw).grid(row=2, column=2, sticky="e")

        tk.Label(frame, text="Scheduled Slots", bg=_bg, fg=_fg, font=("Segoe UI", 9)).grid(
            row=3, column=0, columnspan=3, sticky="w", pady=(10, 0)
        )
        self.listbox = tk.Listbox(frame, width=42, height=8, bg=_ent, fg=_fg, selectbackground="#b0c4de")
        self.listbox.grid(row=4, column=0, columnspan=3, sticky="we")

        btn_row = tk.Frame(frame, bg=_bg)
        btn_row.grid(row=5, column=0, columnspan=3, sticky="e", pady=(10, 0))
        tk.Button(btn_row, text="Remove Selected", command=self._remove_selected, **btn_kw).pack(side="left")
        tk.Button(btn_row, text="Cancel", command=self.destroy, **btn_kw).pack(side="left", padx=(8, 0))
        tk.Button(btn_row, text="Save", command=self._save, **btn_kw).pack(side="left", padx=(8, 0))

        self._refresh_list()
        self.transient(parent)
        self.grab_set()
        self.focus_set()
        try:
            self.lift(parent)
            self.focus_force()
        except Exception:
            pass

    def _refresh_list(self):
        self.listbox.delete(0, tk.END)
        self.slots = sorted(self.slots, key=lambda x: (x.get("date", ""), x.get("time", "")))
        for slot in self.slots:
            self.listbox.insert(tk.END, f"{slot.get('date', '')} {slot.get('time', '')}")

    def _add_slot(self):
        date_val = self.cal.get_date()
        time_val = f"{int(self.hour_var.get()):02d}:{int(self.minute_var.get()):02d}"
        key = f"{date_val} {time_val}"
        existing = {f"{s.get('date', '')} {s.get('time', '')}" for s in self.slots}
        if key in existing:
            return
        self.slots.append({"date": date_val, "time": time_val, "enabled": True, "last_run": ""})
        self._refresh_list()

    def _remove_selected(self):
        selected = list(self.listbox.curselection())
        for idx in reversed(selected):
            del self.slots[idx]
        self._refresh_list()

    def _save(self):
        self.saved = True
        self.destroy()


class SettingsDialog(tk.Toplevel):
    @staticmethod
    def _dest_label_from_key(key):
        mapping = {
            "path": "Local Vault (Path)",
            "ftp": "AirGap Backup (FTP)",
            "cloud": "Cloud Vault",
            "git": "Versioned Repo Backup (Git)",
        }
        return mapping.get(key, "Local Vault (Path)")

    @staticmethod
    def _dest_key_from_label(label):
        mapping = {
            "Local Vault (Path)": "path",
            "AirGap Backup (FTP)": "ftp",
            "Cloud Vault": "cloud",
            "Versioned Repo Backup (Git)": "git",
        }
        return mapping.get(label, "path")

    def __init__(self, parent, cfg, manager):
        super().__init__(parent)
        self.title("Settings")
        self.resizable(False, False)
        self.cfg = cfg
        self.manager = manager
        self.secure_store = SecureCredentialStore()
        self.saved = False

        # PyInstaller one-file EXE: tk + explicit colors (avoid invisible ttk / menu quirks).
        _bg = "#e8e8e8"
        _fg = "#101010"
        _ent = "#ffffff"
        self.configure(bg=_bg)

        _dest_labels = (
            "Local Vault (Path)",
            "AirGap Backup (FTP)",
            "Cloud Vault",
            "Versioned Repo Backup (Git)",
        )
        raw_type = cfg.get("destination_type", "path")
        if not isinstance(raw_type, str):
            raw_type = "path"
        _init_label = self._dest_label_from_key(raw_type)
        if _init_label not in _dest_labels:
            _init_label = "Local Vault (Path)"

        self.destination_type = tk.StringVar(value=_init_label)
        self.destination_path = tk.StringVar(value=cfg.get("destination_path", cfg.get("backup_location", "")))
        self.path_enabled = tk.BooleanVar(value=bool(cfg.get("path_enabled", False)))

        path_destinations = list(cfg.get("path_destinations", []))
        self.path_dest_1 = tk.StringVar(value=path_destinations[0] if len(path_destinations) > 0 else "")
        self.path_dest_2 = tk.StringVar(value=path_destinations[1] if len(path_destinations) > 1 else "")
        self.path_dest_3 = tk.StringVar(value=path_destinations[2] if len(path_destinations) > 2 else "")

        self.ftp_enabled = tk.BooleanVar(value=bool(cfg.get("ftp_enabled", False)))
        ftp_destinations = list(cfg.get("ftp_destinations", []))
        ftp_d1 = ftp_destinations[0] if len(ftp_destinations) > 0 else {}
        ftp_d2 = ftp_destinations[1] if len(ftp_destinations) > 1 else {}
        self.ftp1_host = tk.StringVar(value=ftp_d1.get("host", cfg.get("ftp_host", "")))
        self.ftp1_port = tk.IntVar(
            value=_safe_config_int(ftp_d1.get("port", cfg.get("ftp_port", 21)), 21, 1, 65535)
        )
        self.ftp1_remote_dir = tk.StringVar(value=ftp_d1.get("remote_dir", cfg.get("ftp_remote_dir", "")))
        self.ftp1_username = tk.StringVar(value=ftp_d1.get("username", cfg.get("ftp_username", "")))
        self.ftp1_password = tk.StringVar(value=self.secure_store.get_ftp_slot_password(1))
        self.ftp2_host = tk.StringVar(value=ftp_d2.get("host", ""))
        self.ftp2_port = tk.IntVar(value=_safe_config_int(ftp_d2.get("port", 21), 21, 1, 65535))
        self.ftp2_remote_dir = tk.StringVar(value=ftp_d2.get("remote_dir", ""))
        self.ftp2_username = tk.StringVar(value=ftp_d2.get("username", ""))
        self.ftp2_password = tk.StringVar(value=self.secure_store.get_ftp_slot_password(2))
        self.ftp_use_tls = tk.BooleanVar(value=bool(cfg.get("ftp_use_tls", False)))
        self.ftp_passive_mode = tk.BooleanVar(value=bool(cfg.get("ftp_passive_mode", True)))
        self.ftp_keep_local_copy = tk.BooleanVar(value=bool(cfg.get("ftp_keep_local_copy", True)))

        self.cloud_enabled = tk.BooleanVar(value=bool(cfg.get("cloud_enabled", False)))
        _cp = cfg.get("cloud_provider", "aws_s3")
        if not isinstance(_cp, str) or _cp not in ("aws_s3", "azure_blob", "google_cloud_storage"):
            _cp = "aws_s3"
        self.cloud_provider = tk.StringVar(value=_cp)
        self.cloud_bucket = tk.StringVar(value=cfg.get("cloud_bucket", ""))
        self.cloud_region = tk.StringVar(value=cfg.get("cloud_region", ""))
        self.cloud_prefix = tk.StringVar(value=cfg.get("cloud_prefix", ""))
        self.cloud_keep_local_copy = tk.BooleanVar(value=bool(cfg.get("cloud_keep_local_copy", True)))
        self.git_enabled = tk.BooleanVar(value=bool(cfg.get("git_enabled", False)))
        self.git_repo_path = tk.StringVar(value=cfg.get("git_repo_path", ""))
        self.git_branch = tk.StringVar(value=cfg.get("git_branch", "main"))
        self.git_auto_commit = tk.BooleanVar(value=bool(cfg.get("git_auto_commit", True)))
        self.git_auto_push = tk.BooleanVar(value=bool(cfg.get("git_auto_push", False)))
        self.git_remote_name = tk.StringVar(value=cfg.get("git_remote_name", "origin"))
        self.git_backup_subdir = tk.StringVar(value=cfg.get("git_backup_subdir", "backups"))

        aws_creds = self.secure_store.get_cloud_credentials("aws_s3")
        self.aws_access_key = tk.StringVar(value=aws_creds.get("access_key", ""))
        self.aws_secret_key = tk.StringVar(value=aws_creds.get("secret_key", ""))

        azure_creds = self.secure_store.get_cloud_credentials("azure_blob")
        self.azure_connection_string = tk.StringVar(value=azure_creds.get("connection_string", ""))

        gcs_creds = self.secure_store.get_cloud_credentials("google_cloud_storage")
        self.gcs_service_account_json = tk.StringVar(value=gcs_creds.get("service_account_json", ""))

        self.retention_limit = tk.IntVar(value=_safe_config_int(cfg.get("retention_limit", 20), 20, 1, 1000))
        self.auto_backup_enabled = tk.BooleanVar(value=bool(cfg.get("auto_backup_enabled", False)))
        self.scheduled_enabled = tk.BooleanVar(value=bool(cfg.get("scheduled_backup_enabled", False)))
        self.schedule_minutes = tk.IntVar(
            value=_safe_config_int(cfg.get("scheduled_interval_minutes", 60), 60, 1, 1440)
        )
        self.calendar_scheduler_enabled = tk.BooleanVar(value=bool(cfg.get("calendar_scheduler_enabled", False)))
        self.calendar_schedule_slots = list(cfg.get("calendar_schedule_slots", []))
        self.calendar_summary_var = tk.StringVar(value=f"{len(self.calendar_schedule_slots)} date/time slot(s)")
        self.organize_by_date = tk.BooleanVar(value=bool(cfg.get("organize_by_date", True)))

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)

        canvas = tk.Canvas(self, bg=_bg, highlightthickness=0)
        vsb = tk.Scrollbar(self, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        canvas.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        inner = tk.Frame(canvas, bg=_bg, padx=10, pady=10)
        inner_win = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _canvas_inner_width(event):
            try:
                canvas.itemconfigure(inner_win, width=event.width)
            except tk.TclError:
                pass

        def _inner_scrollregion(_event=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        canvas.bind("<Configure>", _canvas_inner_width)
        inner.bind("<Configure>", _inner_scrollregion)

        def _wheel(ev):
            canvas.yview_scroll(int(-1 * (ev.delta / 120)), "units")

        canvas.bind("<Enter>", lambda _e: canvas.bind_all("<MouseWheel>", _wheel))
        canvas.bind("<Leave>", lambda _e: canvas.unbind_all("<MouseWheel>"))

        def _on_destroy(_evt=None):
            try:
                canvas.unbind_all("<MouseWheel>")
            except tk.TclError:
                pass

        self.bind("<Destroy>", _on_destroy, add=True)

        inner.grid_columnconfigure(0, weight=1)

        chk_kw = {
            "bg": _bg,
            "fg": _fg,
            "selectcolor": _ent,
            "activebackground": _bg,
            "activeforeground": _fg,
            "anchor": "w",
        }
        btn_kw = {"bg": "#d0d0d0", "fg": _fg, "activebackground": "#bbbbbb", "activeforeground": _fg}
        entry_kw = {"bg": _ent, "fg": _fg, "insertbackground": _fg, "highlightthickness": 1}
        spin_kw = {"bg": _ent, "fg": _fg, "highlightthickness": 1}

        dest_box = tk.LabelFrame(inner, text="Destination type", bg=_bg, fg=_fg, padx=8, pady=6)
        dest_box.grid(row=0, column=0, columnspan=2, sticky="we", pady=(0, 6))
        for di, label in enumerate(_dest_labels):
            tk.Radiobutton(
                dest_box,
                text=label,
                variable=self.destination_type,
                value=label,
                **chk_kw,
            ).grid(row=di, column=0, sticky="w")

        self.path_label = tk.Label(inner, text="Path Destination (Local / UNC / NAS)", bg=_bg, fg=_fg)
        self.path_label.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.path_enabled_check = tk.Checkbutton(
            inner, text="Enable Path Destination", variable=self.path_enabled, **chk_kw
        )
        self.path_enabled_check.grid(row=1, column=1, sticky="w", pady=(8, 0))
        self.path_entry = tk.Entry(inner, textvariable=self.path_dest_1, width=42, **entry_kw)
        self.path_entry.grid(row=2, column=0, sticky="we", padx=(0, 6))
        self.path_browse_btn = tk.Button(
            inner, text="Browse 1", command=lambda: self._browse_backup_location(self.path_dest_1), **btn_kw
        )
        self.path_browse_btn.grid(row=2, column=1, sticky="e")
        self.path_entry_2 = tk.Entry(inner, textvariable=self.path_dest_2, width=42, **entry_kw)
        self.path_entry_2.grid(row=3, column=0, sticky="we", padx=(0, 6), pady=(4, 0))
        self.path_browse_btn_2 = tk.Button(
            inner, text="Browse 2", command=lambda: self._browse_backup_location(self.path_dest_2), **btn_kw
        )
        self.path_browse_btn_2.grid(row=3, column=1, sticky="e", pady=(4, 0))
        self.path_entry_3 = tk.Entry(inner, textvariable=self.path_dest_3, width=42, **entry_kw)
        self.path_entry_3.grid(row=4, column=0, sticky="we", padx=(0, 6), pady=(4, 0))
        self.path_browse_btn_3 = tk.Button(
            inner, text="Browse 3", command=lambda: self._browse_backup_location(self.path_dest_3), **btn_kw
        )
        self.path_browse_btn_3.grid(row=4, column=1, sticky="e", pady=(4, 0))

        self.ftp_frame = tk.LabelFrame(
            inner, text="AirGap Backup (FTP) Destination", bg=_bg, fg=_fg, padx=8, pady=8
        )
        self.ftp_frame.grid(row=5, column=0, columnspan=2, sticky="we", pady=(10, 0))
        self.ftp_frame.grid_columnconfigure(1, weight=1)
        tk.Checkbutton(
            self.ftp_frame, text="Enable AirGap Backup (FTP) Destination", variable=self.ftp_enabled, **chk_kw
        ).grid(row=0, column=0, sticky="w", columnspan=2)
        tk.Label(self.ftp_frame, text="FTP #1 Host", bg=_bg, fg=_fg).grid(row=1, column=0, sticky="w")
        tk.Entry(self.ftp_frame, textvariable=self.ftp1_host, width=30, **entry_kw).grid(row=1, column=1, sticky="we")
        tk.Label(self.ftp_frame, text="FTP #1 Port", bg=_bg, fg=_fg).grid(row=2, column=0, sticky="w")
        tk.Spinbox(self.ftp_frame, from_=1, to=65535, textvariable=self.ftp1_port, width=10, **spin_kw).grid(
            row=2, column=1, sticky="w"
        )
        tk.Label(self.ftp_frame, text="FTP #1 Username", bg=_bg, fg=_fg).grid(row=3, column=0, sticky="w")
        tk.Entry(self.ftp_frame, textvariable=self.ftp1_username, width=30, **entry_kw).grid(row=3, column=1, sticky="we")
        tk.Label(self.ftp_frame, text="FTP #1 Password", bg=_bg, fg=_fg).grid(row=4, column=0, sticky="w")
        tk.Entry(self.ftp_frame, textvariable=self.ftp1_password, show="*", width=30, **entry_kw).grid(row=4, column=1, sticky="we")
        tk.Label(self.ftp_frame, text="FTP #1 Remote Dir", bg=_bg, fg=_fg).grid(row=5, column=0, sticky="w")
        tk.Entry(self.ftp_frame, textvariable=self.ftp1_remote_dir, width=30, **entry_kw).grid(row=5, column=1, sticky="we")
        tk.Label(self.ftp_frame, text="FTP #2 Host", bg=_bg, fg=_fg).grid(row=6, column=0, sticky="w")
        tk.Entry(self.ftp_frame, textvariable=self.ftp2_host, width=30, **entry_kw).grid(row=6, column=1, sticky="we")
        tk.Label(self.ftp_frame, text="FTP #2 Port", bg=_bg, fg=_fg).grid(row=7, column=0, sticky="w")
        tk.Spinbox(self.ftp_frame, from_=1, to=65535, textvariable=self.ftp2_port, width=10, **spin_kw).grid(
            row=7, column=1, sticky="w"
        )
        tk.Label(self.ftp_frame, text="FTP #2 Username", bg=_bg, fg=_fg).grid(row=8, column=0, sticky="w")
        tk.Entry(self.ftp_frame, textvariable=self.ftp2_username, width=30, **entry_kw).grid(row=8, column=1, sticky="we")
        tk.Label(self.ftp_frame, text="FTP #2 Password", bg=_bg, fg=_fg).grid(row=9, column=0, sticky="w")
        tk.Entry(self.ftp_frame, textvariable=self.ftp2_password, show="*", width=30, **entry_kw).grid(row=9, column=1, sticky="we")
        tk.Label(self.ftp_frame, text="FTP #2 Remote Dir", bg=_bg, fg=_fg).grid(row=10, column=0, sticky="w")
        tk.Entry(self.ftp_frame, textvariable=self.ftp2_remote_dir, width=30, **entry_kw).grid(row=10, column=1, sticky="we")
        tk.Checkbutton(self.ftp_frame, text="Use TLS (FTPS)", variable=self.ftp_use_tls, **chk_kw).grid(
            row=11, column=0, sticky="w", columnspan=2
        )
        tk.Checkbutton(self.ftp_frame, text="Passive Mode", variable=self.ftp_passive_mode, **chk_kw).grid(
            row=12, column=0, sticky="w", columnspan=2
        )
        tk.Checkbutton(
            self.ftp_frame,
            text="Keep local copy (when path destination exists)",
            variable=self.ftp_keep_local_copy,
            **chk_kw,
        ).grid(row=13, column=0, sticky="w", columnspan=2)
        tk.Button(self.ftp_frame, text="Save FTP Password Securely", command=self._save_ftp_securely, **btn_kw).grid(
            row=14, column=0, sticky="w", pady=(8, 0)
        )
        tk.Button(self.ftp_frame, text="Test FTP Connection", command=self._test_ftp_connection, **btn_kw).grid(
            row=14, column=1, sticky="e", pady=(8, 0)
        )

        self.cloud_frame = tk.LabelFrame(
            inner, text="Cloud Destination", bg=_bg, fg=_fg, padx=8, pady=8
        )
        self.cloud_frame.grid(row=6, column=0, columnspan=2, sticky="we", pady=(10, 0))
        self.cloud_frame.grid_columnconfigure(1, weight=1)
        tk.Checkbutton(self.cloud_frame, text="Enable Cloud Destination", variable=self.cloud_enabled, **chk_kw).grid(
            row=0, column=0, sticky="w", columnspan=2
        )
        tk.Label(self.cloud_frame, text="Provider", bg=_bg, fg=_fg).grid(row=1, column=0, sticky="w")
        prov_f = tk.Frame(self.cloud_frame, bg=_bg)
        prov_f.grid(row=1, column=1, sticky="w")
        for pv, pl in (
            ("aws_s3", "Amazon S3"),
            ("azure_blob", "Azure Blob"),
            ("google_cloud_storage", "Google Cloud"),
        ):
            tk.Radiobutton(
                prov_f,
                text=pl,
                variable=self.cloud_provider,
                value=pv,
                **chk_kw,
            ).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(self.cloud_frame, text="Bucket / Container", bg=_bg, fg=_fg).grid(
            row=2, column=0, sticky="w"
        )
        tk.Entry(self.cloud_frame, textvariable=self.cloud_bucket, width=30, **entry_kw).grid(row=2, column=1, sticky="we")
        tk.Label(self.cloud_frame, text="Region (AWS optional)", bg=_bg, fg=_fg).grid(
            row=3, column=0, sticky="w"
        )
        tk.Entry(self.cloud_frame, textvariable=self.cloud_region, width=30, **entry_kw).grid(row=3, column=1, sticky="we")
        tk.Label(self.cloud_frame, text="Prefix (folder path)", bg=_bg, fg=_fg).grid(
            row=4, column=0, sticky="w"
        )
        tk.Entry(self.cloud_frame, textvariable=self.cloud_prefix, width=30, **entry_kw).grid(row=4, column=1, sticky="we")
        tk.Checkbutton(
            self.cloud_frame,
            text="Keep local copy (when path destination exists)",
            variable=self.cloud_keep_local_copy,
            **chk_kw,
        ).grid(row=5, column=0, sticky="w", columnspan=2)

        secure_frame = tk.LabelFrame(
            self.cloud_frame, text="Secure Credentials", bg=_bg, fg=_fg, padx=6, pady=6
        )
        secure_frame.grid(row=6, column=0, columnspan=2, sticky="we", pady=(8, 0))
        secure_frame.grid_columnconfigure(1, weight=1)
        tk.Label(secure_frame, text="AWS Access Key", bg=_bg, fg=_fg).grid(row=0, column=0, sticky="w")
        tk.Entry(secure_frame, textvariable=self.aws_access_key, width=28, **entry_kw).grid(row=0, column=1, sticky="we")
        tk.Label(secure_frame, text="AWS Secret Key", bg=_bg, fg=_fg).grid(row=1, column=0, sticky="w")
        tk.Entry(secure_frame, textvariable=self.aws_secret_key, show="*", width=28, **entry_kw).grid(row=1, column=1, sticky="we")
        tk.Label(secure_frame, text="Azure Connection String", bg=_bg, fg=_fg).grid(
            row=2, column=0, sticky="w"
        )
        tk.Entry(secure_frame, textvariable=self.azure_connection_string, show="*", width=28, **entry_kw).grid(
            row=2, column=1, sticky="we"
        )
        tk.Label(secure_frame, text="GCS Service Account JSON", bg=_bg, fg=_fg).grid(
            row=3, column=0, sticky="w"
        )
        tk.Entry(secure_frame, textvariable=self.gcs_service_account_json, show="*", width=28, **entry_kw).grid(
            row=3, column=1, sticky="we"
        )
        tk.Button(secure_frame, text="Save Cloud Credentials Securely", command=self._save_cloud_securely, **btn_kw).grid(
            row=4, column=0, sticky="w", pady=(8, 0)
        )
        tk.Button(self.cloud_frame, text="Test Cloud Connection", command=self._test_cloud_connection, **btn_kw).grid(
            row=7, column=1, sticky="e", pady=(8, 0)
        )

        self.git_frame = tk.LabelFrame(
            inner, text="Versioned Repo Backup (Git)", bg=_bg, fg=_fg, padx=8, pady=8
        )
        self.git_frame.grid(row=7, column=0, columnspan=2, sticky="we", pady=(10, 0))
        self.git_frame.grid_columnconfigure(1, weight=1)
        tk.Checkbutton(
            self.git_frame, text="Enable Versioned Repo Backup (Git)", variable=self.git_enabled, **chk_kw
        ).grid(row=0, column=0, sticky="w", columnspan=2)
        tk.Label(self.git_frame, text="Git Repository Path", bg=_bg, fg=_fg).grid(row=1, column=0, sticky="w")
        tk.Entry(self.git_frame, textvariable=self.git_repo_path, width=32, **entry_kw).grid(row=1, column=1, sticky="we")
        tk.Label(self.git_frame, text="Branch", bg=_bg, fg=_fg).grid(row=2, column=0, sticky="w")
        tk.Entry(self.git_frame, textvariable=self.git_branch, width=16, **entry_kw).grid(row=2, column=1, sticky="w")
        tk.Label(self.git_frame, text="Remote Name", bg=_bg, fg=_fg).grid(row=3, column=0, sticky="w")
        tk.Entry(self.git_frame, textvariable=self.git_remote_name, width=16, **entry_kw).grid(row=3, column=1, sticky="w")
        tk.Label(self.git_frame, text="Backup Subfolder", bg=_bg, fg=_fg).grid(row=4, column=0, sticky="w")
        tk.Entry(self.git_frame, textvariable=self.git_backup_subdir, width=16, **entry_kw).grid(row=4, column=1, sticky="w")
        tk.Checkbutton(self.git_frame, text="Auto Commit", variable=self.git_auto_commit, **chk_kw).grid(
            row=5, column=0, sticky="w"
        )
        tk.Checkbutton(self.git_frame, text="Auto Push", variable=self.git_auto_push, **chk_kw).grid(row=5, column=1, sticky="w")
        tk.Button(self.git_frame, text="Test Git Connection", command=self._test_git_connection, **btn_kw).grid(
            row=6, column=1, sticky="e", pady=(8, 0)
        )

        tk.Label(inner, text="Retention Limit (1-1000)", bg=_bg, fg=_fg).grid(
            row=8, column=0, sticky="w", pady=(10, 0)
        )
        tk.Spinbox(inner, from_=1, to=1000, textvariable=self.retention_limit, width=10, **spin_kw).grid(
            row=9, column=0, sticky="w"
        )
        tk.Checkbutton(inner, text="Enable Auto-Backup (file watcher)", variable=self.auto_backup_enabled, **chk_kw).grid(
            row=10, column=0, sticky="w", pady=(10, 0), columnspan=2
        )
        tk.Checkbutton(inner, text="Enable Scheduled Backup", variable=self.scheduled_enabled, **chk_kw).grid(
            row=11, column=0, sticky="w", columnspan=2
        )
        tk.Label(inner, text="Schedule Interval (1-1440 minutes)", bg=_bg, fg=_fg).grid(
            row=12, column=0, sticky="w", pady=(10, 0)
        )
        tk.Spinbox(inner, from_=1, to=1440, textvariable=self.schedule_minutes, width=10, **spin_kw).grid(
            row=13, column=0, sticky="w"
        )
        tk.Checkbutton(inner, text="Enable Calendar Scheduler", variable=self.calendar_scheduler_enabled, **chk_kw).grid(
            row=13, column=1, sticky="w"
        )
        tk.Button(inner, text="Open Mini Calendar Scheduler", command=self._open_calendar_scheduler, **btn_kw).grid(
            row=14, column=0, sticky="w", pady=(8, 0)
        )
        tk.Label(inner, textvariable=self.calendar_summary_var, bg=_bg, fg=_fg).grid(
            row=14, column=1, sticky="w", pady=(8, 0)
        )
        tk.Checkbutton(inner, text="Organize Backups by Date Folder", variable=self.organize_by_date, **chk_kw).grid(
            row=15, column=0, sticky="w", pady=(10, 0), columnspan=2
        )

        button_bar = tk.Frame(inner, bg=_bg)
        button_bar.grid(row=16, column=0, columnspan=2, sticky="e", pady=(15, 0))
        tk.Button(button_bar, text="Cancel", command=self.destroy, **btn_kw).pack(side="right", padx=4)
        tk.Button(button_bar, text="Save", command=self._save, **btn_kw).pack(side="right")

        self.update_idletasks()
        _mw = max(inner.winfo_reqwidth() + vsb.winfo_width() + 32, 560)
        _mh = min(max(inner.winfo_reqheight() + 56, 520), 900)
        self.geometry(f"{_mw}x{_mh}")
        self.minsize(500, 440)
        # Show only destination-specific settings for the selected destination type.
        self.destination_type.trace_add("write", self._sync_destination_ui)
        self._sync_destination_ui()

        self.transient(parent)
        self.grab_set()
        self.focus_set()
        try:
            self.lift(parent)
            self.focus_force()
        except Exception:
            pass

    def _open_calendar_scheduler(self):
        dlg = CalendarSchedulerDialog(self, self.calendar_schedule_slots)
        self.wait_window(dlg)
        if getattr(dlg, "saved", False):
            self.calendar_schedule_slots = dlg.slots
            self.calendar_summary_var.set(f"{len(self.calendar_schedule_slots)} date/time slot(s)")

    def _sync_destination_ui(self, *_args):
        destination_type = self._dest_key_from_label(self.destination_type.get())

        if destination_type == "path":
            self.path_label.grid()
            self.path_enabled_check.grid()
            self.path_entry.grid()
            self.path_entry_2.grid()
            self.path_entry_3.grid()
            self.path_browse_btn.grid()
            self.path_browse_btn_2.grid()
            self.path_browse_btn_3.grid()
            self.ftp_frame.grid_remove()
            self.cloud_frame.grid_remove()
            self.git_frame.grid_remove()
        elif destination_type == "ftp":
            self.path_label.grid_remove()
            self.path_enabled_check.grid_remove()
            self.path_entry.grid_remove()
            self.path_entry_2.grid_remove()
            self.path_entry_3.grid_remove()
            self.path_browse_btn.grid_remove()
            self.path_browse_btn_2.grid_remove()
            self.path_browse_btn_3.grid_remove()
            self.ftp_frame.grid()
            self.cloud_frame.grid_remove()
            self.git_frame.grid_remove()
        elif destination_type == "cloud":
            self.path_label.grid_remove()
            self.path_enabled_check.grid_remove()
            self.path_entry.grid_remove()
            self.path_entry_2.grid_remove()
            self.path_entry_3.grid_remove()
            self.path_browse_btn.grid_remove()
            self.path_browse_btn_2.grid_remove()
            self.path_browse_btn_3.grid_remove()
            self.ftp_frame.grid_remove()
            self.cloud_frame.grid()
            self.git_frame.grid_remove()
        elif destination_type == "git":
            self.path_label.grid_remove()
            self.path_enabled_check.grid_remove()
            self.path_entry.grid_remove()
            self.path_entry_2.grid_remove()
            self.path_entry_3.grid_remove()
            self.path_browse_btn.grid_remove()
            self.path_browse_btn_2.grid_remove()
            self.path_browse_btn_3.grid_remove()
            self.ftp_frame.grid_remove()
            self.cloud_frame.grid_remove()
            self.git_frame.grid()
        else:
            self.path_label.grid_remove()
            self.path_enabled_check.grid_remove()
            self.path_entry.grid_remove()
            self.path_entry_2.grid_remove()
            self.path_entry_3.grid_remove()
            self.path_browse_btn.grid_remove()
            self.path_browse_btn_2.grid_remove()
            self.path_browse_btn_3.grid_remove()
            self.ftp_frame.grid_remove()
            self.cloud_frame.grid_remove()
            self.git_frame.grid_remove()

    def _browse_backup_location(self, target_var):
        selected = filedialog.askdirectory(title="Select Backup Location")
        if selected:
            target_var.set(selected)

    def _build_ftp_settings(self):
        use_first = bool(self.ftp1_host.get().strip())
        host = self.ftp1_host.get().strip() if use_first else self.ftp2_host.get().strip()
        port = int(self.ftp1_port.get()) if use_first else int(self.ftp2_port.get())
        remote_dir = self.ftp1_remote_dir.get() if use_first else self.ftp2_remote_dir.get()
        username = self.ftp1_username.get() if use_first else self.ftp2_username.get()
        password = self.ftp1_password.get() if use_first else self.ftp2_password.get()
        return {
            "ftp_host": host,
            "ftp_port": port,
            "ftp_username": username,
            "ftp_password": password,
            "ftp_slot": (1 if use_first else 2),
            "ftp_remote_dir": remote_dir,
            "ftp_use_tls": bool(self.ftp_use_tls.get()),
            "ftp_passive_mode": bool(self.ftp_passive_mode.get()),
        }

    def _build_cloud_secure_credentials(self):
        provider = self.cloud_provider.get()
        if provider == "aws_s3":
            return {"access_key": self.aws_access_key.get(), "secret_key": self.aws_secret_key.get()}
        if provider == "azure_blob":
            return {"connection_string": self.azure_connection_string.get()}
        if provider == "google_cloud_storage":
            return {"service_account_json": self.gcs_service_account_json.get()}
        return {}

    def _build_cloud_settings(self):
        return {
            "cloud_provider": self.cloud_provider.get(),
            "cloud_bucket": self.cloud_bucket.get(),
            "cloud_region": self.cloud_region.get(),
            "secure_credentials": self._build_cloud_secure_credentials(),
        }

    def _save_ftp_securely(self):
        try:
            self.secure_store.save_ftp_slot_password(1, self.ftp1_password.get())
            self.secure_store.save_ftp_slot_password(2, self.ftp2_password.get())
            # Backward compatibility fallback key.
            self.secure_store.save_ftp_password(self.ftp1_password.get())
            messagebox.showinfo("Secure Save", "FTP passwords (slot 1/2) saved securely via system keyring.")
        except Exception as ex:
            messagebox.showerror("Secure Save", f"Could not save FTP password securely:\n{ex}")

    def _save_cloud_securely(self):
        provider = self.cloud_provider.get()
        creds = self._build_cloud_secure_credentials()
        try:
            self.secure_store.save_cloud_credentials(provider, creds)
            messagebox.showinfo("Secure Save", f"Cloud credentials for {provider} saved securely via system keyring.")
        except Exception as ex:
            messagebox.showerror("Secure Save", f"Could not save cloud credentials securely:\n{ex}")

    def _test_ftp_connection(self):
        try:
            self.manager.test_ftp_connection(self._build_ftp_settings())
            messagebox.showinfo("FTP Test", "FTP connection succeeded.")
        except Exception as ex:
            messagebox.showerror("FTP Test", f"FTP connection failed:\n{ex}")

    def _test_cloud_connection(self):
        try:
            self.manager.test_cloud_connection(self._build_cloud_settings())
            messagebox.showinfo("Cloud Test", "Cloud connection succeeded.")
        except Exception as ex:
            messagebox.showerror("Cloud Test", f"Cloud connection failed:\n{ex}")

    def _test_git_connection(self):
        try:
            self.manager.test_git_connection(
                {
                    "git_repo_path": self.git_repo_path.get(),
                    "git_branch": self.git_branch.get(),
                    "git_remote_name": self.git_remote_name.get(),
                }
            )
            messagebox.showinfo("Git Test", "Git repository connection succeeded.")
        except Exception as ex:
            messagebox.showerror("Git Test", f"Git repository connection failed:\n{ex}")

    def _save(self):
        selected_type = self._dest_key_from_label(self.destination_type.get())
        path_enabled_val = bool(self.path_enabled.get())
        ftp_enabled_val = bool(self.ftp_enabled.get())
        cloud_enabled_val = bool(self.cloud_enabled.get())

        path_destinations = [p.strip() for p in [self.path_dest_1.get(), self.path_dest_2.get(), self.path_dest_3.get()] if p.strip()]
        # De-duplicate while preserving order.
        unique_paths = []
        for p in path_destinations:
            if p not in unique_paths:
                unique_paths.append(p)
        path_destinations = unique_paths[:3]

        ftp_destinations = []
        if self.ftp1_host.get().strip():
            ftp_destinations.append(
                {
                    "host": self.ftp1_host.get().strip(),
                    "port": int(self.ftp1_port.get()),
                    "remote_dir": self.ftp1_remote_dir.get().strip(),
                    "username": self.ftp1_username.get().strip(),
                    "slot": 1,
                }
            )
        if self.ftp2_host.get().strip():
            ftp_destinations.append(
                {
                    "host": self.ftp2_host.get().strip(),
                    "port": int(self.ftp2_port.get()),
                    "remote_dir": self.ftp2_remote_dir.get().strip(),
                    "username": self.ftp2_username.get().strip(),
                    "slot": 2,
                }
            )
        ftp_destinations = ftp_destinations[:2]

        # If user is configuring a destination type in this dialog view, auto-enable it.
        if selected_type == "path":
            path_enabled_val = True
        elif selected_type == "ftp":
            ftp_enabled_val = True
        elif selected_type == "cloud":
            cloud_enabled_val = True

        # Validate only destinations that are enabled.
        if path_enabled_val and not path_destinations:
            messagebox.showerror("Settings", "Path destination is required when Path destination is enabled.")
            return
        if ftp_enabled_val and not ftp_destinations:
            messagebox.showerror("Settings", "FTP host is required when AirGap Backup (FTP) destination is enabled.")
            return
        if cloud_enabled_val and not self.cloud_bucket.get():
            messagebox.showerror("Settings", "Cloud bucket/container is required when Cloud destination is enabled.")
            return
        if self.git_enabled.get() and not self.git_repo_path.get().strip():
            messagebox.showerror("Settings", "Git repository path is required when Git destination is enabled.")
            return

        if int(self.retention_limit.get()) < 1:
            messagebox.showerror("Settings", "Retention must be >= 1.")
            return

        self.cfg.set("destination_type", selected_type)
        self.cfg.set("path_destinations", path_destinations)
        self.cfg.set("destination_path", path_destinations[0] if path_destinations else "")
        self.cfg.set("backup_location", path_destinations[0] if path_destinations else "")
        self.cfg.set("path_enabled", path_enabled_val)

        self.cfg.set("ftp_destinations", ftp_destinations)
        self.cfg.set("ftp_enabled", ftp_enabled_val)
        self.cfg.set("ftp_host", ftp_destinations[0]["host"] if ftp_destinations else "")
        self.cfg.set("ftp_port", int(ftp_destinations[0]["port"]) if ftp_destinations else 21)
        self.cfg.set("ftp_username", ftp_destinations[0]["username"] if ftp_destinations else "")
        self.cfg.set("ftp_password", "")
        self.cfg.set("ftp_remote_dir", ftp_destinations[0]["remote_dir"] if ftp_destinations else "")
        self.cfg.set("ftp_use_tls", bool(self.ftp_use_tls.get()))
        self.cfg.set("ftp_passive_mode", bool(self.ftp_passive_mode.get()))
        self.cfg.set("ftp_keep_local_copy", bool(self.ftp_keep_local_copy.get()))

        self.cfg.set("cloud_enabled", cloud_enabled_val)
        self.cfg.set("cloud_provider", self.cloud_provider.get())
        self.cfg.set("cloud_bucket", self.cloud_bucket.get())
        self.cfg.set("cloud_region", self.cloud_region.get())
        self.cfg.set("cloud_prefix", self.cloud_prefix.get())
        self.cfg.set("cloud_keep_local_copy", bool(self.cloud_keep_local_copy.get()))
        self.cfg.set("git_enabled", bool(self.git_enabled.get()))
        self.cfg.set("git_repo_path", self.git_repo_path.get().strip())
        self.cfg.set("git_branch", self.git_branch.get().strip() or "main")
        self.cfg.set("git_auto_commit", bool(self.git_auto_commit.get()))
        self.cfg.set("git_auto_push", bool(self.git_auto_push.get()))
        self.cfg.set("git_remote_name", self.git_remote_name.get().strip() or "origin")
        self.cfg.set("git_backup_subdir", self.git_backup_subdir.get().strip() or "backups")

        # Ensure selected destinations still make sense after enabling/disabling.
        enabled_types = []
        if path_enabled_val:
            enabled_types.append("path")
        if ftp_enabled_val:
            enabled_types.append("ftp")
        if cloud_enabled_val:
            enabled_types.append("cloud")
        if bool(self.git_enabled.get()):
            enabled_types.append("git")

        selected = list(self.cfg.get("selected_destination_types", []))
        if not selected:
            selected = enabled_types
        selected = [t for t in selected if t in enabled_types]
        self.cfg.set("selected_destination_types", selected)

        self.cfg.set("retention_limit", int(self.retention_limit.get()))
        self.cfg.set("auto_backup_enabled", bool(self.auto_backup_enabled.get()))
        self.cfg.set("scheduled_backup_enabled", bool(self.scheduled_enabled.get()))
        self.cfg.set("scheduled_interval_minutes", int(self.schedule_minutes.get()))
        self.cfg.set("calendar_scheduler_enabled", bool(self.calendar_scheduler_enabled.get()))
        self.cfg.set("calendar_schedule_slots", self.calendar_schedule_slots)
        self.cfg.set("organize_by_date", bool(self.organize_by_date.get()))

        self.saved = True
        self.destroy()


class BackupApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1020x860")
        self.minsize(960, 640)

        self.cfg = ConfigManager()
        self.manager = BackupManager(self.cfg, self.log)
        self.watcher = None
        self.schedule_job = None
        self.next_interval_due = None
        self.backup_running = False
        self.tray_icon = None
        self.tray_thread = None
        self.is_exiting = False
        self.sidebar_expanded = True

        self._init_theme()
        self._build_ui()
        self._load_items_to_ui()
        self.refresh_history()
        self.apply_background_services()
        self._setup_tray()
        self._update_tray_state()
        self._sync_destination_selection()

        self.protocol("WM_DELETE_WINDOW", self._on_close_requested)
        self.log("BackupLite GUI started.")
        try:
            self.manager._audit.append(
                {
                    "event": "app_session_start",
                    "schema_version": 1,
                    "app_title": APP_TITLE,
                    "python_version": sys.version.split()[0],
                    "cwd": os.getcwd(),
                    "config_path": os.path.abspath(self.cfg.config_path),
                    "audit_log_path": os.path.join(
                        os.path.dirname(os.path.abspath(self.cfg.config_path)),
                        self.cfg.get("audit_log_file", "easy_backup_audit.jsonl"),
                    ),
                }
            )
        except Exception:
            pass

    def _init_theme(self):
        self.dark_mode = bool(self.cfg.get("dark_mode", False))
        if self.dark_mode:
            self.colors = {
                "bg": "#0f172a",
                "card": "#111827",
                "text": "#e5e7eb",
                "muted": "#9ca3af",
                "blue": "#2563eb",
                "teal": "#0d9488",
                "line": "#334155",
                "safe": "#22c55e",
                "sidebar": "#020617",
                "sidebar_text": "#cbd5e1",
                "sidebar_active": "#1e293b",
            }
        else:
            self.colors = {
                "bg": "#f4f7fb",
                "card": "#ffffff",
                "text": "#1f2a37",
                "muted": "#5f6c7b",
                "blue": "#1d4ed8",
                "teal": "#0d9488",
                "line": "#d9e2ec",
                "safe": "#16a34a",
                "sidebar": "#0b1f33",
                "sidebar_text": "#dbeafe",
                "sidebar_active": "#113a5c",
            }
        self.configure(bg=self.colors["bg"])

    def _build_ui(self):
        self.activity_log_visible = bool(self.cfg.get("show_activity_log", True))
        self.root_frame = tk.Frame(self, bg=self.colors["bg"])
        self.root_frame.pack(fill="both", expand=True)

        self.sidebar = tk.Frame(self.root_frame, bg=self.colors["sidebar"], width=220)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        brand_row = tk.Frame(self.sidebar, bg=self.colors["sidebar"])
        brand_row.pack(fill="x", padx=8, pady=(8, 2))
        self.brand_label = tk.Label(
            brand_row,
            text="Easy Backup",
            bg=self.colors["sidebar"],
            fg=self.colors["sidebar_text"],
            font=("Segoe UI", 10, "bold"),
        )
        self.brand_label.pack(side="left")
        # Copy icon button (clipboard image)
        self.copy_site_img = None
        if ImageTk is not None:
            self.copy_site_img = ImageTk.PhotoImage(self._create_copy_icon(self.colors["sidebar_text"]))
        self.copy_site_btn = tk.Button(
            brand_row,
            image=self.copy_site_img,
            bg=self.colors["sidebar_active"],
            relief="flat",
            width=24,
            height=20,
            command=self._copy_website_to_clipboard,
        )
        self.copy_site_btn.pack(side="right", padx=(6, 0))

        self.sidebar_toggle = tk.Button(
            self.sidebar,
            text="<<",
            bg=self.colors["sidebar_active"],
            fg=self.colors["sidebar_text"],
            relief="flat",
            command=self._toggle_sidebar,
        )
        self.sidebar_toggle.pack(fill="x", padx=8, pady=(10, 12))

        self.nav_buttons = {}
        for key, label in [
            ("overview", "[O] Overview"),
            ("sources", "[S] Sources"),
            ("destinations", "[D] Destinations"),
            ("settings", "[C] Settings"),
        ]:
            btn = tk.Button(
                self.sidebar,
                text=label,
                anchor="w",
                relief="flat",
                bg=self.colors["sidebar"],
                fg=self.colors["sidebar_text"],
                activebackground=self.colors["sidebar_active"],
                activeforeground="#ffffff",
                command=(self.open_settings if key == "settings" else lambda k=key: self._set_active_nav(k)),
            )
            btn.pack(fill="x", padx=8, pady=4)
            self.nav_buttons[key] = btn
        self._set_active_nav("overview")

        self.main_area = tk.Frame(self.root_frame, bg=self.colors["bg"])
        self.main_area.pack(side="left", fill="both", expand=True, padx=16, pady=16)

        top_actions = tk.Frame(self.main_area, bg=self.colors["bg"])
        top_actions.pack(fill="x", pady=(0, 12))

        self.primary_backup_btn = tk.Button(
            top_actions,
            text="Create Backup",
            bg=self.colors["blue"],
            fg="#ffffff",
            relief="flat",
            padx=16,
            pady=8,
            command=self.create_backup,
        )
        self.primary_backup_btn.pack(side="left")

        self.settings_btn = tk.Button(
            top_actions,
            text="Settings",
            bg=self.colors["teal"],
            fg="#ffffff",
            relief="flat",
            padx=14,
            pady=8,
            command=self.open_settings,
        )
        self.settings_btn.pack(side="left", padx=8)

        self.dark_mode_btn = tk.Button(
            top_actions,
            text=("Light Mode" if self.dark_mode else "Dark Mode"),
            bg="#334155" if self.dark_mode else "#e2e8f0",
            fg="#ffffff" if self.dark_mode else "#0f172a",
            relief="flat",
            padx=12,
            pady=8,
            command=self._toggle_dark_mode,
        )
        self.dark_mode_btn.pack(side="left", padx=8)

        self.destination_summary_var = tk.StringVar(value="Active Destinations: none selected")
        tk.Label(
            top_actions,
            textvariable=self.destination_summary_var,
            bg=self.colors["bg"],
            fg=self.colors["muted"],
            font=("Segoe UI", 10),
            justify="left",
            anchor="w",
            wraplength=560,
        ).pack(side="left", padx=10, pady=2)

        self.dest_sel_path = tk.BooleanVar(value=False)
        self.dest_sel_ftp = tk.BooleanVar(value=False)
        self.dest_sel_cloud = tk.BooleanVar(value=False)
        self.dest_sel_git = tk.BooleanVar(value=False)
        dest_frame = tk.Frame(top_actions, bg=self.colors["bg"])
        dest_frame.pack(side="right")
        self.path_check = tk.Checkbutton(dest_frame, text="PATH", variable=self.dest_sel_path, bg=self.colors["bg"])
        self.ftp_check = tk.Checkbutton(dest_frame, text="AirGap (FTP)", variable=self.dest_sel_ftp, bg=self.colors["bg"])
        self.cloud_check = tk.Checkbutton(dest_frame, text="CLOUD", variable=self.dest_sel_cloud, bg=self.colors["bg"])
        self.git_check = tk.Checkbutton(dest_frame, text="GIT REPO", variable=self.dest_sel_git, bg=self.colors["bg"])
        self.path_check.pack(side="top", anchor="e", padx=4, pady=(0, 2))
        self.ftp_check.pack(side="top", anchor="e", padx=4, pady=(0, 2))
        self.cloud_check.pack(side="top", anchor="e", padx=4, pady=(0, 2))
        self.git_check.pack(side="top", anchor="e", padx=4, pady=(0, 2))
        self.dest_sel_path.trace_add("write", self._on_destination_selection_change)
        self.dest_sel_ftp.trace_add("write", self._on_destination_selection_change)
        self.dest_sel_cloud.trace_add("write", self._on_destination_selection_change)
        self.dest_sel_git.trace_add("write", self._on_destination_selection_change)

        cards = tk.Frame(self.main_area, bg=self.colors["bg"])
        cards.pack(fill="both", expand=True)

        # SYSTEM SECURE card
        self.status_card = tk.Frame(cards, bg=self.colors["card"], highlightbackground=self.colors["line"], highlightthickness=1)
        self.status_card.pack(fill="x", pady=(0, 12))
        shield = tk.Canvas(self.status_card, width=64, height=64, bg=self.colors["card"], highlightthickness=0)
        shield.pack(side="left", padx=14, pady=14)
        shield.create_polygon(32, 4, 56, 16, 50, 44, 32, 58, 14, 44, 8, 16, fill=self.colors["safe"], outline="")
        shield.create_text(32, 31, text="OK", fill="#ffffff", font=("Segoe UI", 11, "bold"))
        self.security_title = tk.Label(
            self.status_card,
            text="SYSTEM SECURE",
            bg=self.colors["card"],
            fg=self.colors["safe"],
            font=("Segoe UI", 18, "bold"),
        )
        self.security_title.pack(anchor="w", pady=(14, 4))
        self.security_subtitle = tk.Label(
            self.status_card,
            text="All selected sources and destinations are configured.",
            bg=self.colors["card"],
            fg=self.colors["muted"],
            font=("Segoe UI", 10),
        )
        self.security_subtitle.pack(anchor="w")

        row2 = tk.Frame(cards, bg=self.colors["bg"])
        row2.pack(fill="x", pady=(0, 12))

        self.sources_card = tk.Frame(row2, bg=self.colors["card"], highlightbackground=self.colors["line"], highlightthickness=1)
        self.sources_card.pack(side="left", fill="both", expand=True, padx=(0, 6))
        tk.Label(self.sources_card, text="Sources", bg=self.colors["card"], fg=self.colors["text"], font=("Segoe UI", 11, "bold")).pack(
            anchor="w", padx=12, pady=(10, 4)
        )
        self.sources_summary_var = tk.StringVar(value="0 Sources Protected")
        tk.Label(
            self.sources_card, textvariable=self.sources_summary_var, bg=self.colors["card"], fg=self.colors["teal"], font=("Segoe UI", 16, "bold")
        ).pack(anchor="w", padx=12)

        actions = tk.Frame(self.sources_card, bg=self.colors["card"])
        actions.pack(anchor="w", padx=12, pady=(8, 8))
        tk.Button(actions, text="Add Folder", bg="#e2e8f0", relief="flat", command=self.add_directory).pack(side="left")
        tk.Button(actions, text="Add File", bg="#e2e8f0", relief="flat", command=self.add_file).pack(side="left", padx=6)
        tk.Button(actions, text="Remove", bg="#f1f5f9", relief="flat", command=self.remove_selected).pack(side="left", padx=6)

        self.dir_list = tk.Listbox(self.sources_card, height=5, borderwidth=0, highlightthickness=0)
        self.dir_list.pack(fill="x", padx=12, pady=(0, 12))

        self.dest_card = tk.Frame(row2, bg=self.colors["card"], highlightbackground=self.colors["line"], highlightthickness=1)
        self.dest_card.pack(side="left", fill="both", expand=True, padx=(6, 0))
        tk.Label(
            self.dest_card, text="Destinations", bg=self.colors["card"], fg=self.colors["text"], font=("Segoe UI", 11, "bold")
        ).pack(anchor="w", padx=12, pady=(10, 6))
        self.dest_card_var = tk.StringVar(value="[DRV] Local Vault | [AIR] AirGap (FTP) | [CLD] Cloud Vault | [GIT] Repo")
        tk.Label(self.dest_card, textvariable=self.dest_card_var, bg=self.colors["card"], fg=self.colors["muted"]).pack(
            anchor="w", padx=12, pady=(0, 12)
        )

        self.vault_card = tk.Frame(cards, bg=self.colors["card"], highlightbackground=self.colors["line"], highlightthickness=1)
        self.vault_card.pack(fill="both", expand=True)
        tk.Label(self.vault_card, text="Vault Overview", bg=self.colors["card"], fg=self.colors["text"], font=("Segoe UI", 11, "bold")).pack(
            anchor="w", padx=12, pady=(10, 4)
        )
        self.vault_usage_var = tk.StringVar(value="0.0 GB / 10 GB (0%)")
        tk.Label(self.vault_card, textvariable=self.vault_usage_var, bg=self.colors["card"], fg=self.colors["blue"], font=("Segoe UI", 14, "bold")).pack(
            anchor="w", padx=12
        )
        self.vault_progress = ttk.Progressbar(self.vault_card, mode="determinate", maximum=100)
        self.vault_progress.pack(fill="x", padx=12, pady=(8, 8))
        self.timeline_var = tk.StringVar(value="Timeline: no backups yet")
        tk.Label(self.vault_card, textvariable=self.timeline_var, bg=self.colors["card"], fg=self.colors["muted"]).pack(
            anchor="w", padx=12, pady=(0, 8)
        )

        # Queue/status mini-metrics + simple activity graph.
        self.queue_files_var = tk.StringVar(value="Queue: 0 files (last scan)")
        tk.Label(self.vault_card, textvariable=self.queue_files_var, bg=self.colors["card"], fg=self.colors["text"], font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=12
        )

        self.next_backup_var = tk.StringVar(value="Next backup: not scheduled")
        tk.Label(self.vault_card, textvariable=self.next_backup_var, bg=self.colors["card"], fg=self.colors["muted"], font=("Segoe UI", 10)).pack(
            anchor="w", padx=12, pady=(2, 4)
        )

        self.avg_backup_var = tk.StringVar(value="Avg backup time: — (run at least one backup)")
        tk.Label(self.vault_card, textvariable=self.avg_backup_var, bg=self.colors["card"], fg=self.colors["teal"], font=("Segoe UI", 10)).pack(
            anchor="w", padx=12, pady=(0, 6)
        )

        _audit_rel = self.cfg.get("audit_log_file", "easy_backup_audit.jsonl")
        _audit_abs = os.path.join(os.path.dirname(os.path.abspath(self.cfg.config_path)), _audit_rel)
        self.audit_path_var = tk.StringVar(value=f"AI audit log (JSON Lines): {_audit_abs}")
        tk.Label(
            self.vault_card,
            textvariable=self.audit_path_var,
            bg=self.colors["card"],
            fg=self.colors["muted"],
            font=("Segoe UI", 8),
            wraplength=860,
            justify="left",
        ).pack(anchor="w", padx=12, pady=(0, 8))

        tk.Label(
            self.vault_card,
            text="Charts: change activity (top) · files in archive & duration per run (bottom)",
            bg=self.colors["card"],
            fg=self.colors["muted"],
            font=("Segoe UI", 9),
        ).pack(anchor="w", padx=12, pady=(0, 4))

        self.activity_canvas = tk.Canvas(
            self.vault_card,
            height=280,
            bg=self.colors["card"],
            highlightbackground=self.colors["line"],
            highlightthickness=1,
        )
        self.activity_canvas.pack(fill="x", padx=12, pady=(0, 10))

        self.log_toggle_btn = tk.Button(
            self.vault_card,
            text=("Hide Activity Log" if self.activity_log_visible else "Show Activity Log"),
            bg="#eef2ff",
            relief="flat",
            command=self._toggle_activity_log,
        )
        self.log_toggle_btn.pack(anchor="w", padx=12, pady=(0, 6))

        self.log_panel = tk.Frame(self.vault_card, bg=self.colors["card"])
        log_row = tk.Frame(self.log_panel, bg=self.colors["card"])
        log_row.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.status_text = tk.Text(
            log_row,
            height=14,
            wrap="word",
            borderwidth=1,
            relief="solid",
            font=("Consolas", 9),
            bg=self.colors["card"],
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
        )
        log_scroll = ttk.Scrollbar(log_row, orient="vertical", command=self.status_text.yview)
        self.status_text.configure(yscrollcommand=log_scroll.set)
        self.status_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

        if self.activity_log_visible:
            self.log_panel.pack(fill="both", expand=True)
        else:
            self.log_panel.pack_forget()

    def _set_active_nav(self, key):
        for nav_key, button in self.nav_buttons.items():
            button.configure(bg=self.colors["sidebar_active"] if nav_key == key else self.colors["sidebar"])

    def _toggle_sidebar(self):
        self.sidebar_expanded = not self.sidebar_expanded
        new_width = 220 if self.sidebar_expanded else 66
        self.sidebar.configure(width=new_width)
        self.sidebar_toggle.configure(text="<<" if self.sidebar_expanded else ">>")
        self.brand_label.configure(text=("Easy Backup" if self.sidebar_expanded else "EB"))
        labels = {
            "overview": "[O] Overview",
            "sources": "[S] Sources",
            "destinations": "[D] Destinations",
            "settings": "[C] Settings",
        }
        short = {"overview": "[O]", "sources": "[S]", "destinations": "[D]", "settings": "[C]"}
        for key, button in self.nav_buttons.items():
            button.configure(text=(labels[key] if self.sidebar_expanded else short[key]))

    def _toggle_activity_log(self):
        self.activity_log_visible = not self.activity_log_visible
        self.cfg.set("show_activity_log", bool(self.activity_log_visible))
        if self.activity_log_visible:
            self.log_panel.pack(fill="both", expand=True)
            self.log_toggle_btn.configure(text="Hide Activity Log")
        else:
            self.log_panel.pack_forget()
            self.log_toggle_btn.configure(text="Show Activity Log")

    def _copy_website_to_clipboard(self):
        website = "promptiteasy.com"
        try:
            self.clipboard_clear()
            self.clipboard_append(website)
            self.update_idletasks()
            self.log(f"Copied website to clipboard: {website}")
        except Exception as ex:
            self.log(f"Clipboard copy failed: {ex}")

    def _create_copy_icon(self, fg="#ffffff"):
        # Draw a simple clipboard/page icon on dark background-friendly tone
        img = Image.new("RGBA", (18, 18), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # Clipboard top
        d.rounded_rectangle((3, 2, 15, 16), radius=3, outline=fg, width=2)
        d.rectangle((6, 2, 12, 6), fill=fg)
        # Page lines
        d.line((6, 8, 12, 8), fill=fg, width=1)
        d.line((6, 11, 12, 11), fill=fg, width=1)
        d.line((6, 14, 12, 14), fill=fg, width=1)
        return img

    def _toggle_dark_mode(self):
        self.cfg.set("dark_mode", not self.dark_mode)
        self.dark_mode = bool(self.cfg.get("dark_mode", False))
        self.cfg.set("show_activity_log", bool(self.activity_log_visible))
        # Rebuild the main UI tree with the alternate palette.
        if hasattr(self, "root_frame") and self.root_frame.winfo_exists():
            self.root_frame.destroy()
        self._init_theme()
        self._build_ui()
        self._load_items_to_ui()
        self.refresh_history()
        self._sync_destination_selection()
        self._update_tray_state()
        self._update_security_card()

    def _get_selected_destination_types(self):
        selected = []
        if self.dest_sel_path.get():
            selected.append("path")
        if self.dest_sel_ftp.get():
            selected.append("ftp")
        if self.dest_sel_cloud.get():
            selected.append("cloud")
        if self.dest_sel_git.get():
            selected.append("git")
        return selected

    def _sync_destination_selection(self):
        path_enabled = bool(self.cfg.get("path_enabled", False))
        ftp_enabled = bool(self.cfg.get("ftp_enabled", False))
        cloud_enabled = bool(self.cfg.get("cloud_enabled", False))
        git_enabled = bool(self.cfg.get("git_enabled", False))

        enabled_types = {"path": path_enabled, "ftp": ftp_enabled, "cloud": cloud_enabled, "git": git_enabled}

        selected = list(self.cfg.get("selected_destination_types", []))
        if not selected:
            selected = [t for t, ok in enabled_types.items() if ok]

        selected = [t for t in selected if enabled_types.get(t, False)]

        # Update checkbox states and availability.
        self.dest_sel_path.set("path" in selected)
        self.dest_sel_ftp.set("ftp" in selected)
        self.dest_sel_cloud.set("cloud" in selected)
        self.dest_sel_git.set("git" in selected)

        self.path_check.configure(state=(tk.NORMAL if enabled_types["path"] else tk.DISABLED))
        self.ftp_check.configure(state=(tk.NORMAL if enabled_types["ftp"] else tk.DISABLED))
        self.cloud_check.configure(state=(tk.NORMAL if enabled_types["cloud"] else tk.DISABLED))
        self.git_check.configure(state=(tk.NORMAL if enabled_types["git"] else tk.DISABLED))

        self._update_destination_summary()

    def _on_destination_selection_change(self, *_args):
        # Persist selection so backups (manual/scheduled/watcher) use the same set.
        selected = self._get_selected_destination_types()
        self.cfg.set("selected_destination_types", selected)
        self._update_destination_summary()

    def _update_destination_summary(self):
        selected = self._get_selected_destination_types()
        if not selected:
            self.destination_summary_var.set("Active Destinations:\n- none selected")
            self.dest_card_var.set("[DRV] Local Vault: off | [AIR] AirGap (FTP): off | [CLD] Cloud Vault: off | [GIT] Repo: off")
            self._update_security_card()
            return

        lines = ["Active Destinations:"]
        card_parts = []
        if "path" in selected:
            path_list = self.cfg.get("path_destinations", [])
            if not path_list:
                fallback = self.cfg.get("destination_path", "") or self.cfg.get("backup_location", "")
                path_list = [fallback] if fallback else []
            if path_list:
                for dest in path_list[:3]:
                    lines.append(f"- PATH -> {dest}")
                card_parts.append(f"[DRV] {len(path_list[:3])} local")
            else:
                lines.append("- PATH -> not configured")
                card_parts.append("[DRV] not configured")
        if "ftp" in selected:
            ftp_destinations = self.cfg.get("ftp_destinations", [])
            if not ftp_destinations and self.cfg.get("ftp_host", ""):
                ftp_destinations = [
                    {
                        "host": self.cfg.get("ftp_host", ""),
                        "port": int(self.cfg.get("ftp_port", 21)),
                        "remote_dir": self.cfg.get("ftp_remote_dir", ""),
                    }
                ]
            if ftp_destinations:
                for d in ftp_destinations[:2]:
                    remote_dir = d.get("remote_dir", "") or "/"
                    remote_dir_norm = remote_dir if remote_dir.startswith("/") else "/" + remote_dir
                    lines.append(f"- AirGap Backup (FTP) -> {d.get('host', '')}:{d.get('port', 21)}{remote_dir_norm}")
                card_parts.append(f"[AIR] {len(ftp_destinations[:2])} endpoint(s)")
            else:
                lines.append("- AirGap Backup (FTP) -> not configured")
                card_parts.append("[AIR] not configured")
        if "cloud" in selected:
            provider = self.cfg.get("cloud_provider", "aws_s3")
            bucket = self.cfg.get("cloud_bucket", "") or "bucket/container missing"
            prefix = self.cfg.get("cloud_prefix", "")
            suffix = f"/{prefix}" if prefix else ""
            lines.append(f"- CLOUD({provider}) -> {bucket}{suffix}")
            card_parts.append(f"[CLD] {provider}")
        if "git" in selected:
            repo_path = self.cfg.get("git_repo_path", "") or "repo path missing"
            branch = self.cfg.get("git_branch", "main")
            lines.append(f"- Versioned Repo Backup (Git) -> {repo_path} [{branch}]")
            card_parts.append("[GIT] repo")

        self.destination_summary_var.set("\n".join(lines))
        self.dest_card_var.set(" | ".join(card_parts))
        self._update_security_card()

    def _update_security_card(self):
        items = self._get_source_items()
        selected = self._get_selected_destination_types()
        secure = bool(items) and bool(selected) and not self.backup_running
        if secure:
            self.security_title.configure(text="SYSTEM SECURE", fg=self.colors["safe"])
            self.security_subtitle.configure(text="Protection active. Sources and destinations are configured.", fg=self.colors["muted"])
        elif self.backup_running:
            self.security_title.configure(text="BACKUP IN PROGRESS", fg=self.colors["blue"])
            self.security_subtitle.configure(text="System is currently writing protected snapshots.", fg=self.colors["muted"])
        else:
            self.security_title.configure(text="ACTION NEEDED", fg="#b45309")
            self.security_subtitle.configure(text="Add sources and select at least one destination.", fg=self.colors["muted"])

    def _get_source_items(self):
        items = self.cfg.get("source_items", [])
        if items:
            return items
        return self.cfg.get("source_directories", [])

    def _load_items_to_ui(self):
        self.dir_list.delete(0, tk.END)
        for item in self._get_source_items():
            self.dir_list.insert(tk.END, item)
        self.sources_summary_var.set(f"{len(self._get_source_items())} Sources Protected")
        self._update_security_card()

    def _persist_items_from_ui(self):
        items = list(self.dir_list.get(0, tk.END))
        self.cfg.set("source_items", items)
        self.cfg.set("source_directories", [p for p in items if os.path.isdir(p)])
        self.sources_summary_var.set(f"{len(items)} Sources Protected")
        self._update_tray_state()
        self._update_security_card()

    def log(self, message):
        def append():
            w = getattr(self, "status_text", None)
            if w is None:
                return
            try:
                if not w.winfo_exists():
                    return
            except tk.TclError:
                return
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            w.insert(tk.END, f"[{ts}] {message}\n")
            w.see(tk.END)

        self.after(0, append)

    def add_directory(self):
        if self.dir_list.size() >= 5:
            messagebox.showwarning("Limit", "Maximum of 5 source items.")
            return
        path = filedialog.askdirectory(title="Select Source Folder")
        if not path:
            return
        existing = set(self.dir_list.get(0, tk.END))
        if path in existing:
            return
        self.dir_list.insert(tk.END, path)
        self._persist_items_from_ui()
        self.apply_background_services()

    def add_file(self):
        if self.dir_list.size() >= 5:
            messagebox.showwarning("Limit", "Maximum of 5 source items.")
            return
        path = filedialog.askopenfilename(title="Select Source File")
        if not path:
            return
        existing = set(self.dir_list.get(0, tk.END))
        if path in existing:
            return
        self.dir_list.insert(tk.END, path)
        self._persist_items_from_ui()
        self.apply_background_services()

    def remove_selected(self):
        selected = list(self.dir_list.curselection())
        for idx in reversed(selected):
            self.dir_list.delete(idx)
        self._persist_items_from_ui()
        self.apply_background_services()

    def clear_all(self):
        self.dir_list.delete(0, tk.END)
        self._persist_items_from_ui()
        self.apply_background_services()

    def refresh_history(self):
        history = self.cfg.get("backup_history", [])
        total_bytes = 0
        seen = set()
        for entry in history:
            path = entry.get("backup_path", "")
            if not path or path in seen:
                continue
            if os.path.exists(path) and os.path.isfile(path):
                seen.add(path)
                try:
                    total_bytes += os.path.getsize(path)
                except Exception:
                    pass

        capacity_gb = 10.0
        used_gb = total_bytes / (1024**3)
        pct = min(100, int((used_gb / capacity_gb) * 100)) if capacity_gb > 0 else 0
        self.vault_usage_var.set(f"{used_gb:.1f} GB / {capacity_gb:.0f} GB ({pct}%)")
        self.vault_progress["value"] = pct

        if history:
            latest = history[0]
            self.timeline_var.set(
                f"Timeline: {len(history)} snapshots, latest {latest.get('date', '')} {latest.get('time', '')}"
            )
        else:
            self.timeline_var.set("Timeline: no backups yet")

        # Update queue + next due indicators and the mini activity chart.
        self._refresh_queue_and_graph()

    def _get_total_files_in_queue(self):
        """
        "Queue files" is approximated using your last snapshot data.
        Since we don't maintain a pending-backup queue right now, this reflects how many
        files were present at the last completed scan for each configured source.
        """
        snapshots = self.cfg.get("last_snapshots", {}) or {}
        total = 0
        for source in self._get_source_items():
            snap = snapshots.get(source, {})
            if isinstance(snap, dict):
                total += len(snap)
        return total

    def _format_seconds(self, seconds):
        seconds = max(0, int(seconds))
        mins, sec = divmod(seconds, 60)
        hrs, mins = divmod(mins, 60)
        if hrs > 0:
            return f"{hrs}h {mins}m"
        if mins > 0:
            return f"{mins}m {sec}s"
        return f"{sec}s"

    def _get_next_backup_due_text(self):
        now_ts = datetime.now().timestamp()
        parts = []

        if bool(self.cfg.get("scheduled_backup_enabled", False)):
            if self.next_interval_due is not None:
                delta = self.next_interval_due - now_ts
                parts.append(f"Interval in {self._format_seconds(delta)}")
            else:
                parts.append("Interval enabled")

        if bool(self.cfg.get("calendar_scheduler_enabled", False)):
            slots = list(self.cfg.get("calendar_schedule_slots", []))
            due_ats = []
            now = datetime.now()
            for slot in slots:
                if not slot.get("enabled", True):
                    continue
                if slot.get("last_run"):
                    continue
                date_val = str(slot.get("date", "")).strip()
                time_val = str(slot.get("time", "")).strip()
                if not date_val or not time_val:
                    continue
                try:
                    due_at = datetime.strptime(f"{date_val} {time_val}", "%Y-%m-%d %H:%M")
                except Exception:
                    continue
                if due_at > now:
                    due_ats.append(due_at)
            if due_ats:
                due_at = min(due_ats)
                parts.append(f"Calendar at {due_at.strftime('%H:%M')}")
            else:
                parts.append("Calendar: none pending")

        if not parts:
            return "Next backup: not scheduled"
        return "Next backup: " + " | ".join(parts)

    def _refresh_queue_and_graph(self):
        # Queue files count.
        total_files = self._get_total_files_in_queue()
        self.queue_files_var.set(f"Queue: {total_files} files (last scan)")

        # Next due.
        self.next_backup_var.set(self._get_next_backup_due_text())

        # Average duration from deduped runs (one bar per backup run, not per destination).
        history = self.cfg.get("backup_history", []) or []
        runs = self._history_unique_runs(history)
        durs = []
        for e in runs[:25]:
            d = e.get("duration_seconds")
            if d is None:
                continue
            try:
                durs.append(float(d))
            except (TypeError, ValueError):
                continue
        if durs:
            avg = sum(durs) / len(durs)
            self.avg_backup_var.set(
                f"Avg backup time (last {len(durs)} run(s)): {avg:.2f}s · newest run: {durs[0]:.2f}s"
            )
        else:
            self.avg_backup_var.set("Avg backup time: — (needs a finished backup with timing data)")

        # Chart.
        try:
            self.update_idletasks()
        except Exception:
            pass
        self._draw_activity_graph()

    @staticmethod
    def _history_unique_runs(history):
        """One entry per backup run (newest first). Uses run_id when present."""
        seen = set()
        out = []
        for e in history or []:
            rid = e.get("run_id")
            if rid:
                k = rid
            else:
                k = (e.get("source_key"), e.get("created_at"))
            if k in seen:
                continue
            seen.add(k)
            out.append(e)
        return out

    def _draw_activity_graph(self):
        self.activity_canvas.delete("all")

        cw = int(self.activity_canvas.winfo_width() or 0)
        ch = int(self.activity_canvas.winfo_height() or 0)
        width = cw if cw > 50 else 560
        height = ch if ch > 50 else 280
        pad = 10
        w = max(1, width - pad * 2)
        mid_y = pad + int((height - 2 * pad) * 0.52)
        h_top = max(40, mid_y - pad - 28)
        y_bot = mid_y + 12
        h_bot = max(40, height - y_bot - pad - 18)

        runs = self._history_unique_runs(self.cfg.get("backup_history", []) or [])
        if not runs:
            self.activity_canvas.create_text(
                pad,
                height // 2,
                anchor="w",
                text="No backup history yet. Create Backup once to see charts & timing.",
                fill=self.colors["muted"],
                font=("Segoe UI", 10),
            )
            return

        added_c = self.colors["teal"]
        modified_c = self.colors["blue"]
        deleted_c = "#ef4444" if not self.dark_mode else "#fb7185"
        files_c = "#0ea5e9"
        dur_c = "#f97316"

        # --- Top: change deltas (oldest → newest left → right)
        self.activity_canvas.create_text(
            pad,
            pad,
            anchor="nw",
            text="Per run: added / modified / deleted files",
            fill=self.colors["muted"],
            font=("Segoe UI", 9),
        )
        recent_chg = list(reversed(runs[:6]))
        n1 = max(1, len(recent_chg))
        max_total = 1
        for entry in recent_chg:
            total = int(entry.get("added_count", 0)) + int(entry.get("modified_count", 0)) + int(entry.get("deleted_count", 0))
            if total > max_total:
                max_total = total

        y_base = pad + 22 + h_top
        self.activity_canvas.create_line(pad, y_base, pad + w, y_base, fill=self.colors["line"])
        gw1 = w / n1
        bar_w1 = max(4, int(gw1 * 0.2))
        off1 = [-bar_w1, 0, bar_w1]

        for idx, entry in enumerate(recent_chg):
            bx = pad + idx * gw1 + gw1 / 2
            added = int(entry.get("added_count", 0))
            modified = int(entry.get("modified_count", 0))
            deleted = int(entry.get("deleted_count", 0))
            vals = [added, modified, deleted]
            colors = [added_c, modified_c, deleted_c]
            for j in range(3):
                val = vals[j]
                bar_h = int((val / max_total) * h_top)
                x1 = int(bx + off1[j] - bar_w1 / 2)
                x2 = x1 + bar_w1
                self.activity_canvas.create_rectangle(x1, y_base - bar_h, x2, y_base, fill=colors[j], width=0)
            t = str(entry.get("time", "")).strip()
            if t:
                self.activity_canvas.create_text(int(bx), y_base + 10, text=t[-8:], fill=self.colors["muted"], font=("Segoe UI", 7))

        # --- Bottom: files in archive + duration
        self.activity_canvas.create_text(
            pad,
            y_bot - 2,
            anchor="nw",
            text="Per run: files packed (blue) vs seconds (orange)",
            fill=self.colors["muted"],
            font=("Segoe UI", 9),
        )
        recent_met = list(reversed(runs[:8]))
        n2 = max(1, len(recent_met))
        max_files = 1
        max_dur = 0.001
        for entry in recent_met:
            fi = int(entry.get("files_in_archive", 0) or 0)
            if fi > max_files:
                max_files = fi
            try:
                ds = float(entry.get("duration_seconds", 0) or 0)
            except (TypeError, ValueError):
                ds = 0.0
            if ds > max_dur:
                max_dur = ds

        y_base2 = y_bot + 14 + h_bot
        self.activity_canvas.create_line(pad, y_base2, pad + w, y_base2, fill=self.colors["line"])
        gw2 = w / n2
        bw_f = max(3, int(gw2 * 0.18))
        bw_d = max(3, int(gw2 * 0.18))
        for idx, entry in enumerate(recent_met):
            bx = pad + idx * gw2 + gw2 / 2
            fi = int(entry.get("files_in_archive", 0) or 0)
            try:
                ds = float(entry.get("duration_seconds", 0) or 0)
            except (TypeError, ValueError):
                ds = 0.0
            hf = int((fi / max_files) * h_bot)
            hd = int((ds / max_dur) * h_bot)
            x_f1 = int(bx - bw_f - 2)
            x_f2 = x_f1 + bw_f
            self.activity_canvas.create_rectangle(x_f1, y_base2 - hf, x_f2, y_base2, fill=files_c, width=0)
            x_d1 = int(bx + 2)
            x_d2 = x_d1 + bw_d
            self.activity_canvas.create_rectangle(x_d1, y_base2 - hd, x_d2, y_base2, fill=dur_c, width=0)

        self.activity_canvas.create_text(
            pad + w - 5,
            height - 6,
            anchor="e",
            text="Queue size (main UI) uses snapshot file count · charts use archive & measured duration per run",
            fill=self.colors["muted"],
            font=("Segoe UI", 7),
        )

    def create_backup(self):
        if self.backup_running:
            self.log("Backup already running. Please wait.")
            return

        selected = self._get_selected_destination_types()
        if not selected:
            messagebox.showwarning(
                "Destinations",
                "Select at least one backup destination (Local Vault / AirGap Backup (FTP) / Cloud Vault / Versioned Repo Backup (Git)).",
            )
            return

        self.backup_running = True
        self._update_tray_state()
        self._update_security_card()

        def worker():
            try:
                self.manager.backup_all(selected)
            finally:
                self.backup_running = False
                self.after(0, self.refresh_history)
                self._update_tray_state()
                self._update_security_card()

        threading.Thread(target=worker, daemon=True).start()

    def open_settings(self):
        self._set_active_nav("settings")
        try:
            dlg = SettingsDialog(self, self.cfg, self.manager)
            self.wait_window(dlg)
            if dlg.saved:
                self.log("Settings saved.")
                self._sync_destination_selection()
                self.apply_background_services()
                try:
                    self.manager._audit.append(
                        {
                            "event": "settings_saved",
                            "schema_version": 1,
                            "path_enabled": bool(self.cfg.get("path_enabled", False)),
                            "ftp_enabled": bool(self.cfg.get("ftp_enabled", False)),
                            "cloud_enabled": bool(self.cfg.get("cloud_enabled", False)),
                            "git_enabled": bool(self.cfg.get("git_enabled", False)),
                            "selected_destination_types": list(self.cfg.get("selected_destination_types", [])),
                            "path_destinations": list(self.cfg.get("path_destinations", []))[:3],
                            "cloud_provider": self.cfg.get("cloud_provider", ""),
                            "cloud_bucket": self.cfg.get("cloud_bucket", ""),
                            "retention_limit": int(self.cfg.get("retention_limit", 20)),
                            "auto_backup_enabled": bool(self.cfg.get("auto_backup_enabled", False)),
                            "scheduled_backup_enabled": bool(self.cfg.get("scheduled_backup_enabled", False)),
                            "scheduled_interval_minutes": int(self.cfg.get("scheduled_interval_minutes", 60)),
                            "calendar_scheduler_enabled": bool(self.cfg.get("calendar_scheduler_enabled", False)),
                            "calendar_slots_count": len(self.cfg.get("calendar_schedule_slots", []) or []),
                        }
                    )
                except Exception:
                    pass
        except Exception as ex:
            log_path = os.path.join(os.path.dirname(getattr(sys, "executable", "") or ".") or ".", "easybackup_settings_error.log")
            try:
                with open(log_path, "w", encoding="utf-8") as ef:
                    ef.write(traceback.format_exc())
            except OSError:
                log_path = "(could not write log file)"
            messagebox.showerror(
                "Settings",
                f"Could not open Settings.\n\n{ex}\n\nDetails saved to:\n{log_path}",
            )
        finally:
            self._set_active_nav("overview")

    def _on_watch_change(self):
        self.log("Detected changes. Running debounced auto-backup...")
        self.create_backup()

    def _start_watcher(self):
        enabled = self.cfg.get("auto_backup_enabled", False)
        sources = self._get_source_items()
        watch_targets = set()
        for source in sources:
            if os.path.isdir(source):
                watch_targets.add(source)
            elif os.path.isfile(source):
                parent = os.path.dirname(source)
                if parent:
                    watch_targets.add(parent)

        valid = [p for p in watch_targets if os.path.isdir(p)]
        if enabled and valid:
            self.watcher = FileWatcher(valid, self._on_watch_change, debounce_seconds=30)
            self.watcher.start()
            self.log("File watcher started.")

    def _stop_watcher(self):
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
            self.log("File watcher stopped.")

    def _cancel_schedule(self):
        if self.schedule_job:
            self.after_cancel(self.schedule_job)
            self.schedule_job = None

    def _run_calendar_schedule_if_due(self):
        if not self.cfg.get("calendar_scheduler_enabled", False):
            return

        slots = list(self.cfg.get("calendar_schedule_slots", []))
        if not slots:
            return

        now = datetime.now()
        changed = False
        due_triggered = False
        for slot in slots:
            if not slot.get("enabled", True):
                continue
            if slot.get("last_run"):
                continue
            date_val = str(slot.get("date", "")).strip()
            time_val = str(slot.get("time", "")).strip()
            if not date_val or not time_val:
                continue
            try:
                due_at = datetime.strptime(f"{date_val} {time_val}", "%Y-%m-%d %H:%M")
            except Exception:
                continue
            if now >= due_at:
                slot["last_run"] = now.isoformat()
                changed = True
                due_triggered = True

        if changed:
            self.cfg.set("calendar_schedule_slots", slots)
        if due_triggered:
            self.log("Calendar scheduler triggered backup.")
            self.create_backup()

    def _schedule_next(self):
        self._cancel_schedule()
        interval_enabled = bool(self.cfg.get("scheduled_backup_enabled", False))
        calendar_enabled = bool(self.cfg.get("calendar_scheduler_enabled", False))
        if not interval_enabled and not calendar_enabled:
            self.next_interval_due = None
            return

        minutes = int(self.cfg.get("scheduled_interval_minutes", 60))
        minutes = max(1, min(1440, minutes))
        interval_ms = minutes * 60 * 1000
        if interval_enabled and self.next_interval_due is None:
            self.next_interval_due = datetime.now().timestamp() + (interval_ms / 1000.0)
        # Poll calendar schedule every 30 seconds when enabled.
        delay_ms = 30_000 if calendar_enabled else interval_ms

        def run_scheduled():
            if interval_enabled and self.next_interval_due is not None:
                now_ts = datetime.now().timestamp()
                if now_ts >= self.next_interval_due:
                    self.log("Interval scheduler triggered backup.")
                    self.create_backup()
                    self.next_interval_due = now_ts + (interval_ms / 1000.0)
            if calendar_enabled:
                self._run_calendar_schedule_if_due()
            self._schedule_next()

        self.schedule_job = self.after(delay_ms, run_scheduled)
        if interval_enabled:
            self.log(f"Scheduled backup every {minutes} minute(s).")
        if calendar_enabled:
            self.log("Calendar scheduler active (30s polling).")

    def apply_background_services(self):
        self._stop_watcher()
        self._start_watcher()
        self._schedule_next()
        self._update_tray_state()

    def _create_tray_image(self, color):
        image = Image.new("RGB", (64, 64), (255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.ellipse((8, 8, 56, 56), fill=color, outline=(60, 60, 60), width=3)
        return image

    def _setup_tray(self):
        if pystray is None or Image is None:
            self.log("Tray support unavailable (missing pystray/Pillow).")
            return

        def on_backup(icon, item):
            self.after(0, self.create_backup)

        def on_show(icon, item):
            self.after(0, self.show_window)

        def on_exit(icon, item):
            self.after(0, self.exit_app)

        menu = pystray.Menu(
            pystray.MenuItem("Backup Now", on_backup, default=True),
            pystray.MenuItem("Show Window", on_show),
            pystray.MenuItem("Exit", on_exit),
        )
        self.tray_icon = pystray.Icon("EasyBackup", self._create_tray_image((220, 30, 30)), APP_TITLE, menu)

        def run_tray():
            self.tray_icon.run()

        self.tray_thread = threading.Thread(target=run_tray, daemon=True)
        self.tray_thread.start()

    def _update_tray_state(self):
        if not self.tray_icon:
            return
        items = self._get_source_items()
        ready = bool(items) and bool(self._get_selected_destination_types()) and not self.backup_running
        color = (30, 170, 60) if ready else (220, 30, 30)
        self.tray_icon.icon = self._create_tray_image(color)

    def show_window(self):
        self.deiconify()
        self.lift()
        self.focus_force()

    def _on_close_requested(self):
        if self.is_exiting:
            self.exit_app()
            return

        if self.tray_icon:
            self.withdraw()
            self.log("Window hidden to tray.")
            return

        self.exit_app()

    def exit_app(self):
        self.is_exiting = True
        self._stop_watcher()
        self._cancel_schedule()
        if self.tray_icon:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    app = BackupApp()
    app.mainloop()
