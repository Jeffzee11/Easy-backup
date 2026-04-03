import json
import os
from copy import deepcopy


DEFAULT_CONFIG = {
    "backup_location": "",
    "source_directories": [],
    "source_items": [],
    "destination_type": "path",
    # Enables that destination type should be available for backups.
    "path_enabled": False,
    "selected_destination_types": ["path"],
    "path_destinations": [],
    "destination_path": "",
    "ftp_enabled": False,
    "ftp_destinations": [],
    "ftp_host": "",
    "ftp_port": 21,
    "ftp_username": "",
    "ftp_password": "",
    "ftp_remote_dir": "",
    "ftp_use_tls": False,
    "ftp_passive_mode": True,
    "ftp_keep_local_copy": True,
    "cloud_enabled": False,
    "cloud_provider": "aws_s3",
    "cloud_bucket": "",
    "cloud_region": "",
    "cloud_prefix": "",
    "cloud_keep_local_copy": True,
    "git_enabled": False,
    "git_repo_path": "",
    "git_branch": "main",
    "git_auto_commit": True,
    "git_auto_push": False,
    "git_remote_name": "origin",
    "git_backup_subdir": "backups",
    "retention_limit": 20,
    "auto_backup_enabled": False,
    "scheduled_backup_enabled": False,
    "scheduled_interval_minutes": 60,
    "calendar_scheduler_enabled": False,
    "calendar_schedule_slots": [],
    "dark_mode": False,
    "organize_by_date": True,
    "include_patterns": ["*"],
    "exclude_patterns": ["*.pyc", "__pycache__", ".git", "*.tmp"],
    "backup_history": [],
    "version_counters": {},
    "last_snapshots": {},
    # JSON Lines audit log next to backup_config.json (for AI / tooling).
    "audit_log_file": "easy_backup_audit.jsonl",
    # Main window: show activity text log expanded by default.
    "show_activity_log": True,
}


class ConfigManager:
    def __init__(self, config_path="backup_config.json"):
        self.config_path = config_path
        self.config = self._load()

    def _load(self):
        if not os.path.exists(self.config_path):
            return deepcopy(DEFAULT_CONFIG)

        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            merged = deepcopy(DEFAULT_CONFIG)
            merged.update(data)
            # Backward compatibility: map legacy source_directories to source_items.
            if not merged.get("source_items") and merged.get("source_directories"):
                merged["source_items"] = list(merged.get("source_directories", []))
            # Backward compatibility for backup_location setting.
            if not merged.get("destination_path") and merged.get("backup_location"):
                merged["destination_path"] = merged.get("backup_location")
            if not merged.get("path_destinations"):
                if merged.get("destination_path"):
                    merged["path_destinations"] = [merged.get("destination_path")]
                elif merged.get("backup_location"):
                    merged["path_destinations"] = [merged.get("backup_location")]

            if not merged.get("ftp_destinations"):
                if merged.get("ftp_host"):
                    merged["ftp_destinations"] = [
                        {
                            "host": merged.get("ftp_host", ""),
                            "port": int(merged.get("ftp_port", 21)),
                            "remote_dir": merged.get("ftp_remote_dir", ""),
                            "username": merged.get("ftp_username", ""),
                            "slot": 1,
                        }
                    ]
            else:
                # Ensure destination objects have expected keys.
                normalized = []
                for idx, dest in enumerate(merged.get("ftp_destinations", []), start=1):
                    normalized.append(
                        {
                            "host": dest.get("host", ""),
                            "port": int(dest.get("port", 21)),
                            "remote_dir": dest.get("remote_dir", ""),
                            "username": dest.get("username", merged.get("ftp_username", "")),
                            "slot": int(dest.get("slot", idx)),
                        }
                    )
                merged["ftp_destinations"] = normalized

            # Backward compatibility for legacy single destination_type:
            # If the old config had `destination_type` but did not have `selected_destination_types`,
            # convert it to `path_enabled` + a default multi-destination selection list.
            if merged.get("selected_destination_types") is None:
                merged["selected_destination_types"] = []
            if merged.get("path_enabled") is None:
                merged["path_enabled"] = False

            # Normalize calendar schedule slots.
            slots = merged.get("calendar_schedule_slots", [])
            normalized_slots = []
            for slot in slots:
                if not isinstance(slot, dict):
                    continue
                date_val = str(slot.get("date", "")).strip()
                time_val = str(slot.get("time", "")).strip()
                if not date_val or not time_val:
                    continue
                normalized_slots.append(
                    {
                        "date": date_val,
                        "time": time_val,
                        "enabled": bool(slot.get("enabled", True)),
                        "last_run": str(slot.get("last_run", "")).strip(),
                    }
                )
            merged["calendar_schedule_slots"] = normalized_slots

            legacy_type = data.get("destination_type") if "selected_destination_types" not in data else None
            if legacy_type:
                if legacy_type == "path":
                    merged["path_enabled"] = True
                    merged["ftp_enabled"] = False
                    merged["cloud_enabled"] = False
                    merged["selected_destination_types"] = ["path"]
                elif legacy_type == "ftp":
                    merged["path_enabled"] = False
                    merged["ftp_enabled"] = True
                    merged["cloud_enabled"] = False
                    merged["selected_destination_types"] = ["ftp"]
                elif legacy_type == "cloud":
                    merged["path_enabled"] = False
                    merged["ftp_enabled"] = False
                    merged["cloud_enabled"] = True
                    merged["selected_destination_types"] = ["cloud"]

            return merged
        except Exception:
            return deepcopy(DEFAULT_CONFIG)

    def save(self):
        with open(self.config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)

    def get(self, key, default=None):
        return self.config.get(key, default)

    def set(self, key, value):
        self.config[key] = value
        self.save()

