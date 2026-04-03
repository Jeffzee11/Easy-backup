import json
import os
from datetime import datetime, timezone


class AuditLogWriter:
    """
    Append-only JSON Lines log for machine/AI consumption.
    Each line is one JSON object; no secrets should be written here.
    """

    def __init__(self, path):
        self.path = os.path.abspath(path)

    def append(self, record):
        base = dict(record)
        base.setdefault("ts_utc", datetime.now(timezone.utc).isoformat())
        line = json.dumps(base, ensure_ascii=False, default=str)
        parent = os.path.dirname(self.path)
        if parent and not os.path.isdir(parent):
            try:
                os.makedirs(parent, exist_ok=True)
            except OSError:
                pass
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
