import json

try:
    import keyring
except Exception:
    keyring = None


APP_KEYRING_SERVICE = "BackupLite"


class SecureCredentialStore:
    def __init__(self, profile="default"):
        self.profile = profile

    @staticmethod
    def is_available():
        return keyring is not None

    def _username_for_key(self, key_name):
        return f"{self.profile}:{key_name}"

    def set_secret(self, key_name, value):
        if not self.is_available():
            raise RuntimeError("Secure credential storage is unavailable (keyring not installed).")
        try:
            keyring.set_password(
                APP_KEYRING_SERVICE, self._username_for_key(key_name), value or ""
            )
        except Exception as ex:
            # Some machines have keyring installed but no backend configured (NoKeyringError).
            # Treat this as "unavailable" so callers can fall back gracefully.
            raise RuntimeError(f"Secure credential storage unavailable: {ex}")

    def get_secret(self, key_name, default=""):
        if not self.is_available():
            return default
        try:
            value = keyring.get_password(APP_KEYRING_SERVICE, self._username_for_key(key_name))
            return value if value is not None else default
        except Exception:
            # Avoid crashing SettingsDialog init when keyring has no backend configured.
            return default

    def save_ftp_password(self, password):
        self.set_secret("ftp_password", password)

    def get_ftp_password(self):
        return self.get_secret("ftp_password", "")

    def save_ftp_slot_password(self, slot_index, password):
        self.set_secret(f"ftp_password_slot_{slot_index}", password)

    def get_ftp_slot_password(self, slot_index):
        return self.get_secret(f"ftp_password_slot_{slot_index}", "")

    def save_cloud_credentials(self, provider, credentials_dict):
        payload = json.dumps(credentials_dict or {})
        self.set_secret(f"cloud:{provider}", payload)

    def get_cloud_credentials(self, provider):
        raw = self.get_secret(f"cloud:{provider}", "{}")
        try:
            return json.loads(raw)
        except Exception:
            return {}

