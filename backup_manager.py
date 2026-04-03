import fnmatch
import ftplib
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import traceback
import uuid
import zipfile
from datetime import datetime

from audit_log import AuditLogWriter
from secure_credentials import SecureCredentialStore

try:
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError
except Exception:
    boto3 = None
    BotoCoreError = Exception
    ClientError = Exception

try:
    from azure.storage.blob import BlobServiceClient
except Exception:
    BlobServiceClient = None

try:
    from google.cloud import storage as gcs_storage
except Exception:
    gcs_storage = None


class BackupManager:
    def __init__(self, config_manager, status_callback=None):
        self.cfg = config_manager
        self.status_callback = status_callback or (lambda msg: None)
        self.secure_store = SecureCredentialStore()
        _cfg_dir = os.path.dirname(os.path.abspath(self.cfg.config_path)) or "."
        _audit_name = self.cfg.get("audit_log_file", "easy_backup_audit.jsonl")
        self._audit = AuditLogWriter(os.path.join(_cfg_dir, _audit_name))

    def _log(self, message):
        self.status_callback(message)

    @staticmethod
    def _safe_dir_name(path):
        return os.path.basename(os.path.normpath(path)) or "Root"

    @staticmethod
    def _date_folder(now):
        return now.strftime("%Y%b%d")

    @staticmethod
    def _timestamp(now):
        return now.strftime("%Y-%m-%d_%H-%M-%S")

    @staticmethod
    def _day_key(now):
        return now.strftime("%Y-%m-%d")

    def _should_include(self, rel_path):
        include_patterns = self.cfg.get("include_patterns", ["*"])
        exclude_patterns = self.cfg.get("exclude_patterns", [])

        normalized = rel_path.replace("\\", "/")
        included = any(fnmatch.fnmatch(normalized, p) for p in include_patterns)
        excluded = any(fnmatch.fnmatch(normalized, p) for p in exclude_patterns)
        return included and not excluded

    def _iter_files(self, source_path):
        if os.path.isfile(source_path):
            rel_path = os.path.basename(source_path).replace("\\", "/")
            if self._should_include(rel_path):
                yield source_path, rel_path
            return

        for root, _, files in os.walk(source_path):
            for file_name in files:
                abs_path = os.path.join(root, file_name)
                rel_path = os.path.relpath(abs_path, source_path).replace("\\", "/")
                if self._should_include(rel_path):
                    yield abs_path, rel_path

    @staticmethod
    def _hash_file(path):
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    def _build_snapshot(self, source_path):
        snapshot = {}
        for abs_path, rel_path in self._iter_files(source_path):
            try:
                snapshot[rel_path] = self._hash_file(abs_path)
            except Exception:
                continue
        return snapshot

    @staticmethod
    def _compare_snapshots(old, new):
        old_keys = set(old.keys())
        new_keys = set(new.keys())
        added = sorted(new_keys - old_keys)
        deleted = sorted(old_keys - new_keys)
        modified = sorted([k for k in (old_keys & new_keys) if old[k] != new[k]])
        return added, modified, deleted

    def _next_version(self, source_key, now):
        counters = self.cfg.get("version_counters", {})
        day = self._day_key(now)

        if source_key not in counters:
            counters[source_key] = {"day": day, "version": 0}
        if counters[source_key]["day"] != day:
            counters[source_key] = {"day": day, "version": 0}

        counters[source_key]["version"] += 1
        self.cfg.set("version_counters", counters)
        return counters[source_key]["version"]

    def _apply_retention(self, source_key, destination_type="path", destination_label=None):
        """
        Retention currently applies to local `path` destination backups only.
        For FTP/cloud we keep remote history records but do not attempt remote deletion.
        """
        retention_limit = int(self.cfg.get("retention_limit", 20))
        history = self.cfg.get("backup_history", [])

        dir_items = [
            h
            for h in history
            if h.get("source_key") == source_key
            and h.get("destination_type") == destination_type
            and (destination_label is None or h.get("destination_label") == destination_label)
        ]
        if len(dir_items) <= retention_limit:
            return

        # Keep newest N entries.
        dir_items_sorted = sorted(dir_items, key=lambda x: x.get("created_at", ""), reverse=True)
        to_keep = dir_items_sorted[:retention_limit]
        keep_paths = {item.get("backup_path", "") for item in to_keep if item.get("backup_path")}

        new_history = []
        for item in history:
            if (
                item.get("source_key") != source_key
                or item.get("destination_type") != destination_type
                or (destination_label is not None and item.get("destination_label") != destination_label)
            ):
                new_history.append(item)
                continue

            backup_path = item.get("backup_path", "")
            if backup_path and backup_path in keep_paths:
                new_history.append(item)
                continue

            old_path = item.get("backup_path") or item.get("local_backup_path")
            if old_path and os.path.exists(old_path):
                try:
                    os.remove(old_path)
                except Exception:
                    pass

        self.cfg.set("backup_history", new_history)

    def _get_sources(self):
        items = self.cfg.get("source_items", [])
        if items:
            return items
        return self.cfg.get("source_directories", [])

    def _resolve_path_destinations(self, now):
        destinations = self.cfg.get("path_destinations", [])
        if not destinations:
            single = self.cfg.get("destination_path", "") or self.cfg.get("backup_location", "")
            destinations = [single] if single else []

        resolved = []
        for destination_path in destinations:
            if not destination_path:
                continue
            if self.cfg.get("organize_by_date", True):
                resolved.append(os.path.join(destination_path, self._date_folder(now)))
            else:
                resolved.append(destination_path)
        return resolved

    def _build_object_key(self, zip_name):
        prefix = self.cfg.get("cloud_prefix", "").strip().strip("/")
        return f"{prefix}/{zip_name}" if prefix else zip_name

    def _get_ftp_password(self, override_password="", slot_index=None):
        if override_password:
            return override_password
        if slot_index is not None:
            slot_pw = self.secure_store.get_ftp_slot_password(slot_index)
            if slot_pw:
                return slot_pw
        configured = self.cfg.get("ftp_password", "")
        if configured:
            return configured
        return self.secure_store.get_ftp_password()

    def _get_cloud_secure_credentials(self, provider):
        return self.secure_store.get_cloud_credentials(provider)

    def _upload_to_ftp(self, local_zip_path, zip_name, ftp_destination=None):
        destination = ftp_destination or {}
        host = destination.get("host", self.cfg.get("ftp_host", ""))
        port = int(destination.get("port", self.cfg.get("ftp_port", 21)))
        username = destination.get("username", self.cfg.get("ftp_username", ""))
        slot_index = destination.get("slot")
        password = self._get_ftp_password(self.cfg.get("ftp_password", ""), slot_index=slot_index)
        remote_dir = destination.get("remote_dir", self.cfg.get("ftp_remote_dir", "")).strip().replace("\\", "/")
        use_tls = bool(self.cfg.get("ftp_use_tls", False))
        passive = bool(self.cfg.get("ftp_passive_mode", True))

        if not host:
            raise ValueError("FTP host is required.")

        ftp_client = ftplib.FTP_TLS() if use_tls else ftplib.FTP()
        ftp_client.connect(host=host, port=port, timeout=30)
        ftp_client.login(user=username, passwd=password)
        ftp_client.set_pasv(passive)
        if use_tls:
            ftp_client.prot_p()

        if remote_dir:
            parts = [p for p in remote_dir.split("/") if p]
            for part in parts:
                try:
                    ftp_client.mkd(part)
                except Exception:
                    pass
                ftp_client.cwd(part)

        with open(local_zip_path, "rb") as f:
            ftp_client.storbinary(f"STOR {zip_name}", f)
        ftp_client.quit()

        return f"ftp://{host}/{remote_dir}/{zip_name}".replace("//", "/").replace("ftp:/", "ftp://")

    def test_ftp_connection(self, ftp_settings=None):
        settings = ftp_settings or {}
        host = settings.get("ftp_host", self.cfg.get("ftp_host", ""))
        port = int(settings.get("ftp_port", self.cfg.get("ftp_port", 21)))
        username = settings.get("ftp_username", self.cfg.get("ftp_username", ""))
        slot_index = settings.get("ftp_slot")
        password = self._get_ftp_password(settings.get("ftp_password", ""), slot_index=slot_index)
        remote_dir = settings.get("ftp_remote_dir", self.cfg.get("ftp_remote_dir", "")).strip().replace("\\", "/")
        use_tls = bool(settings.get("ftp_use_tls", self.cfg.get("ftp_use_tls", False)))
        passive = bool(settings.get("ftp_passive_mode", self.cfg.get("ftp_passive_mode", True)))

        if not host:
            raise ValueError("FTP host is required.")

        ftp_client = ftplib.FTP_TLS() if use_tls else ftplib.FTP()
        try:
            ftp_client.connect(host=host, port=port, timeout=15)
            ftp_client.login(user=username, passwd=password)
            ftp_client.set_pasv(passive)
            if use_tls:
                ftp_client.prot_p()
            if remote_dir:
                parts = [p for p in remote_dir.split("/") if p]
                for part in parts:
                    ftp_client.cwd(part)
        finally:
            try:
                ftp_client.quit()
            except Exception:
                pass
        return True

    def _upload_to_aws_s3(self, local_zip_path, zip_name):
        if boto3 is None:
            raise RuntimeError("AWS S3 support requires boto3.")

        bucket = self.cfg.get("cloud_bucket", "").strip()
        region = self.cfg.get("cloud_region", "").strip() or None
        creds = self._get_cloud_secure_credentials("aws_s3")
        access_key = creds.get("access_key", "")
        secret_key = creds.get("secret_key", "")

        if not bucket:
            raise ValueError("Cloud bucket is required.")

        session_kwargs = {}
        if access_key and secret_key:
            session_kwargs["aws_access_key_id"] = access_key
            session_kwargs["aws_secret_access_key"] = secret_key
        if region:
            session_kwargs["region_name"] = region

        s3 = boto3.client("s3", **session_kwargs)
        object_key = self._build_object_key(zip_name)
        s3.upload_file(local_zip_path, bucket, object_key)
        return f"s3://{bucket}/{object_key}"

    def _upload_to_azure_blob(self, local_zip_path, zip_name):
        if BlobServiceClient is None:
            raise RuntimeError("Azure Blob support requires azure-storage-blob.")

        container = self.cfg.get("cloud_bucket", "").strip()
        creds = self._get_cloud_secure_credentials("azure_blob")
        connection_string = creds.get("connection_string", "")
        if not container:
            raise ValueError("Cloud bucket/container is required.")
        if not connection_string:
            raise ValueError("Azure connection string is required in secure credentials.")

        service_client = BlobServiceClient.from_connection_string(connection_string)
        container_client = service_client.get_container_client(container)
        try:
            container_client.create_container()
        except Exception:
            pass

        blob_name = self._build_object_key(zip_name)
        blob_client = container_client.get_blob_client(blob=blob_name)
        with open(local_zip_path, "rb") as data:
            blob_client.upload_blob(data, overwrite=True)
        return f"azure://{container}/{blob_name}"

    def _upload_to_gcs(self, local_zip_path, zip_name):
        if gcs_storage is None:
            raise RuntimeError("Google Cloud support requires google-cloud-storage.")

        bucket_name = self.cfg.get("cloud_bucket", "").strip()
        if not bucket_name:
            raise ValueError("Cloud bucket is required.")

        creds = self._get_cloud_secure_credentials("google_cloud_storage")
        service_account_json = creds.get("service_account_json", "")

        if service_account_json:
            info = json.loads(service_account_json)
            client = gcs_storage.Client.from_service_account_info(info)
        else:
            client = gcs_storage.Client()

        object_key = self._build_object_key(zip_name)
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(object_key)
        blob.upload_from_filename(local_zip_path)
        return f"gs://{bucket_name}/{object_key}"

    def _upload_to_cloud(self, local_zip_path, zip_name):
        provider = self.cfg.get("cloud_provider", "aws_s3")
        if provider == "aws_s3":
            return self._upload_to_aws_s3(local_zip_path, zip_name)
        if provider == "azure_blob":
            return self._upload_to_azure_blob(local_zip_path, zip_name)
        if provider == "google_cloud_storage":
            return self._upload_to_gcs(local_zip_path, zip_name)
        raise ValueError(f"Unsupported cloud provider: {provider}")

    @staticmethod
    def _run_git(args, repo_path):
        return subprocess.run(
            ["git"] + args,
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_git_connection(self, git_settings=None):
        settings = git_settings or {}
        repo_path = settings.get("git_repo_path", self.cfg.get("git_repo_path", "")).strip()
        branch = settings.get("git_branch", self.cfg.get("git_branch", "main")).strip() or "main"
        remote_name = settings.get("git_remote_name", self.cfg.get("git_remote_name", "origin")).strip() or "origin"

        if not repo_path:
            raise ValueError("Git repository path is required.")
        if not os.path.isdir(repo_path):
            raise ValueError("Git repository path does not exist.")
        if not os.path.isdir(os.path.join(repo_path, ".git")):
            raise ValueError("Path is not a Git repository (.git folder not found).")

        self._run_git(["status", "--porcelain"], repo_path)
        try:
            self._run_git(["rev-parse", "--verify", branch], repo_path)
        except Exception:
            try:
                self._run_git(["rev-parse", "--verify", f"{remote_name}/{branch}"], repo_path)
            except Exception as ex:
                raise RuntimeError(f"Could not verify branch '{branch}'.") from ex
        return True

    def _upload_to_git_repo(self, local_zip_path, zip_name, source_name):
        repo_path = self.cfg.get("git_repo_path", "").strip()
        branch = self.cfg.get("git_branch", "main").strip() or "main"
        auto_commit = bool(self.cfg.get("git_auto_commit", True))
        auto_push = bool(self.cfg.get("git_auto_push", False))
        remote_name = self.cfg.get("git_remote_name", "origin").strip() or "origin"
        backup_subdir = self.cfg.get("git_backup_subdir", "backups").strip().strip("/\\") or "backups"

        if not repo_path:
            raise ValueError("Git repository path is required.")
        if not os.path.isdir(repo_path):
            raise ValueError("Git repository path does not exist.")
        if not os.path.isdir(os.path.join(repo_path, ".git")):
            raise ValueError("Git repository path is not a Git repository.")

        try:
            self._run_git(["checkout", branch], repo_path)
        except Exception:
            pass

        rel_dir = os.path.join(backup_subdir, datetime.now().strftime("%Y-%m-%d"), source_name)
        abs_dir = os.path.join(repo_path, rel_dir)
        os.makedirs(abs_dir, exist_ok=True)

        target_path = os.path.join(abs_dir, zip_name)
        shutil.copy2(local_zip_path, target_path)
        rel_file = os.path.relpath(target_path, repo_path).replace("\\", "/")

        if auto_commit:
            self._run_git(["add", rel_file], repo_path)
            commit_msg = f"Backup {source_name} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            try:
                self._run_git(["commit", "-m", commit_msg], repo_path)
            except Exception:
                pass

            if auto_push:
                self._run_git(["push", remote_name, branch], repo_path)

        return f"git://{repo_path}/{rel_file}"

    def test_cloud_connection(self, cloud_settings=None):
        settings = cloud_settings or {}
        provider = settings.get("cloud_provider", self.cfg.get("cloud_provider", "aws_s3"))
        bucket = settings.get("cloud_bucket", self.cfg.get("cloud_bucket", "")).strip()
        region = settings.get("cloud_region", self.cfg.get("cloud_region", "")).strip() or None

        if not bucket:
            raise ValueError("Cloud bucket/container is required.")

        if provider == "aws_s3":
            if boto3 is None:
                raise RuntimeError("AWS S3 support requires boto3.")
            creds = settings.get("secure_credentials") or self._get_cloud_secure_credentials("aws_s3")
            access_key = creds.get("access_key", "")
            secret_key = creds.get("secret_key", "")

            kwargs = {}
            if access_key and secret_key:
                kwargs["aws_access_key_id"] = access_key
                kwargs["aws_secret_access_key"] = secret_key
            if region:
                kwargs["region_name"] = region
            client = boto3.client("s3", **kwargs)
            client.head_bucket(Bucket=bucket)
            return True

        if provider == "azure_blob":
            if BlobServiceClient is None:
                raise RuntimeError("Azure Blob support requires azure-storage-blob.")
            creds = settings.get("secure_credentials") or self._get_cloud_secure_credentials("azure_blob")
            connection_string = creds.get("connection_string", "")
            if not connection_string:
                raise ValueError("Azure connection string is required.")
            service_client = BlobServiceClient.from_connection_string(connection_string)
            service_client.get_container_client(bucket).get_container_properties()
            return True

        if provider == "google_cloud_storage":
            if gcs_storage is None:
                raise RuntimeError("Google Cloud support requires google-cloud-storage.")
            creds = settings.get("secure_credentials") or self._get_cloud_secure_credentials("google_cloud_storage")
            service_account_json = creds.get("service_account_json", "")
            if service_account_json:
                info = json.loads(service_account_json)
                client = gcs_storage.Client.from_service_account_info(info)
            else:
                client = gcs_storage.Client()
            client.get_bucket(bucket)
            return True

        raise ValueError(f"Unsupported cloud provider: {provider}")

    def backup_item(self, source_path, selected_destination_types):
        if not (os.path.isdir(source_path) or os.path.isfile(source_path)):
            self._log(f"Skip (missing source): {source_path}")
            return None

        run_id = str(uuid.uuid4())
        t0 = time.perf_counter()
        now = datetime.now()
        self._audit.append(
            {
                "event": "backup_item_started",
                "schema_version": 1,
                "run_id": run_id,
                "source_path": source_path,
                "selected_destination_types": list(selected_destination_types),
                "timestamp_local": now.isoformat(),
            }
        )

        version = self._next_version(source_path, now)
        source_name = self._safe_dir_name(source_path)
        zip_name = f"backup_{source_name}_{self._timestamp(now)}_v{version}.zip"

        # Always build the ZIP once, then upload/copy to each destination.
        temp_dir = tempfile.mkdtemp(prefix="backuplite_")
        local_zip_path = os.path.join(temp_dir, zip_name)
        path_destinations = self._resolve_path_destinations(now)  # used for path and optional local copy
        primary_destination_dir = path_destinations[0] if path_destinations else ""

        files_in_archive = 0
        try:
            self._log(f"Backing up: {source_path} (run_id={run_id})")
            with zipfile.ZipFile(local_zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for abs_path, rel_path in self._iter_files(source_path):
                    try:
                        zf.write(abs_path, rel_path)
                        files_in_archive += 1
                    except Exception:
                        self._log(f"Skipped file: {rel_path}")

            zip_size = os.path.getsize(local_zip_path) if os.path.isfile(local_zip_path) else 0

            snapshots = self.cfg.get("last_snapshots", {})
            old_snapshot = snapshots.get(source_path, {})
            new_snapshot = self._build_snapshot(source_path)
            added, modified, deleted = self._compare_snapshots(old_snapshot, new_snapshot)
            snapshots[source_path] = new_snapshot
            self.cfg.set("last_snapshots", snapshots)

            history = self.cfg.get("backup_history", [])
            created_dest_summaries = []

            path_enabled = bool(self.cfg.get("path_enabled", False))
            ftp_enabled = bool(self.cfg.get("ftp_enabled", False))
            cloud_enabled = bool(self.cfg.get("cloud_enabled", False))
            git_enabled = bool(self.cfg.get("git_enabled", False))

            try:
                for dest_type in selected_destination_types:
                    if dest_type == "path":
                        if not path_enabled:
                            continue
                        if not path_destinations:
                            self._log("Destination path is not configured; skipping PATH destination.")
                            continue
                        for destination_dir in path_destinations[:3]:
                            os.makedirs(destination_dir, exist_ok=True)
                            persisted_copy = os.path.join(destination_dir, zip_name)
                            with open(local_zip_path, "rb") as src, open(persisted_copy, "wb") as dst:
                                dst.write(src.read())

                            history.append(
                                {
                                    "run_id": run_id,
                                    "source_dir": source_path,
                                    "source_key": source_path,
                                    "backup_path": persisted_copy,
                                    "destination_type": "path",
                                    "destination_label": destination_dir,
                                    "local_backup_path": persisted_copy,
                                    "created_at": now.isoformat(),
                                    "date": now.strftime("%Y-%m-%d"),
                                    "time": now.strftime("%H:%M:%S"),
                                    "version": version,
                                    "added_count": len(added),
                                    "modified_count": len(modified),
                                    "deleted_count": len(deleted),
                                    "added_files": added[:20],
                                    "modified_files": modified[:20],
                                    "deleted_files": deleted[:20],
                                }
                            )
                            created_dest_summaries.append(f"PATH -> {persisted_copy}")

                    elif dest_type == "ftp":
                        if not ftp_enabled:
                            continue
                        ftp_destinations = self.cfg.get("ftp_destinations", [])
                        if not ftp_destinations and self.cfg.get("ftp_host", ""):
                            ftp_destinations = [
                                {
                                    "host": self.cfg.get("ftp_host", ""),
                                    "port": int(self.cfg.get("ftp_port", 21)),
                                    "remote_dir": self.cfg.get("ftp_remote_dir", ""),
                                }
                            ]
                        for ftp_destination in ftp_destinations[:2]:
                            backup_path = self._upload_to_ftp(local_zip_path, zip_name, ftp_destination=ftp_destination)
                            final_local_copy = ""
                            keep_local = bool(self.cfg.get("ftp_keep_local_copy", True))
                            if keep_local and primary_destination_dir:
                                os.makedirs(primary_destination_dir, exist_ok=True)
                                persisted_copy = os.path.join(primary_destination_dir, zip_name)
                                with open(local_zip_path, "rb") as src, open(persisted_copy, "wb") as dst:
                                    dst.write(src.read())
                                final_local_copy = persisted_copy

                            destination_label = (
                                f"{ftp_destination.get('host', '')}:{ftp_destination.get('port', 21)}"
                                f"{ftp_destination.get('remote_dir', '')}"
                            )
                            history.append(
                                {
                                    "run_id": run_id,
                                    "source_dir": source_path,
                                    "source_key": source_path,
                                    "backup_path": backup_path,
                                    "destination_type": "ftp",
                                    "destination_label": destination_label,
                                    "local_backup_path": final_local_copy,
                                    "created_at": now.isoformat(),
                                    "date": now.strftime("%Y-%m-%d"),
                                    "time": now.strftime("%H:%M:%S"),
                                    "version": version,
                                    "added_count": len(added),
                                    "modified_count": len(modified),
                                    "deleted_count": len(deleted),
                                    "added_files": added[:20],
                                    "modified_files": modified[:20],
                                    "deleted_files": deleted[:20],
                                }
                            )
                            created_dest_summaries.append(f"FTP -> {backup_path}")

                    elif dest_type == "cloud":
                        if not cloud_enabled:
                            continue
                        backup_path = self._upload_to_cloud(local_zip_path, zip_name)
                        final_local_copy = ""
                        keep_local = bool(self.cfg.get("cloud_keep_local_copy", True))
                        if keep_local and primary_destination_dir:
                            os.makedirs(primary_destination_dir, exist_ok=True)
                            persisted_copy = os.path.join(primary_destination_dir, zip_name)
                            with open(local_zip_path, "rb") as src, open(persisted_copy, "wb") as dst:
                                dst.write(src.read())
                            final_local_copy = persisted_copy

                        history.append(
                            {
                                "run_id": run_id,
                                "source_dir": source_path,
                                "source_key": source_path,
                                "backup_path": backup_path,
                                "destination_type": "cloud",
                                "destination_label": self.cfg.get("cloud_provider", "aws_s3"),
                                "local_backup_path": final_local_copy,
                                "created_at": now.isoformat(),
                                "date": now.strftime("%Y-%m-%d"),
                                "time": now.strftime("%H:%M:%S"),
                                "version": version,
                                "added_count": len(added),
                                "modified_count": len(modified),
                                "deleted_count": len(deleted),
                                "added_files": added[:20],
                                "modified_files": modified[:20],
                                "deleted_files": deleted[:20],
                            }
                        )
                        created_dest_summaries.append(f"CLOUD -> {backup_path}")

                    elif dest_type == "git":
                        if not git_enabled:
                            continue
                        backup_path = self._upload_to_git_repo(local_zip_path, zip_name, source_name)
                        history.append(
                            {
                                "run_id": run_id,
                                "source_dir": source_path,
                                "source_key": source_path,
                                "backup_path": backup_path,
                                "destination_type": "git",
                                "destination_label": self.cfg.get("git_repo_path", ""),
                                "local_backup_path": "",
                                "created_at": now.isoformat(),
                                "date": now.strftime("%Y-%m-%d"),
                                "time": now.strftime("%H:%M:%S"),
                                "version": version,
                                "added_count": len(added),
                                "modified_count": len(modified),
                                "deleted_count": len(deleted),
                                "added_files": added[:20],
                                "modified_files": modified[:20],
                                "deleted_files": deleted[:20],
                            }
                        )
                        created_dest_summaries.append(f"GIT -> {backup_path}")

            finally:
                # Keep local copy behavior separate from the temp ZIP.
                try:
                    if os.path.exists(temp_dir):
                        for root, dirs, files in os.walk(temp_dir, topdown=False):
                            for name in files:
                                try:
                                    os.remove(os.path.join(root, name))
                                except Exception:
                                    pass
                            for name in dirs:
                                try:
                                    os.rmdir(os.path.join(root, name))
                                except Exception:
                                    pass
                        try:
                            os.rmdir(temp_dir)
                        except Exception:
                            pass
                except Exception:
                    pass

            duration = time.perf_counter() - t0
            patch = {
                "duration_seconds": round(duration, 3),
                "files_in_archive": files_in_archive,
                "zip_size_bytes": zip_size,
                "snapshot_total_files": len(new_snapshot),
            }
            for entry in history:
                if entry.get("run_id") == run_id:
                    entry.update(patch)

            history = sorted(history, key=lambda x: x.get("created_at", ""), reverse=True)
            self.cfg.set("backup_history", history)
            if "path" in selected_destination_types:
                for destination_dir in path_destinations[:3]:
                    self._apply_retention(source_path, destination_type="path", destination_label=destination_dir)

            self._log(
                "Backup complete:\n"
                f"  run_id: {run_id}\n"
                f"  duration_s: {patch['duration_seconds']}\n"
                f"  files_in_archive: {files_in_archive} | snapshot_files: {len(new_snapshot)} | zip_bytes: {zip_size}\n"
                f"  Source: {source_path}\n"
                f"  Destinations: {', '.join(created_dest_summaries) if created_dest_summaries else 'none'}\n"
                f"  Added ({len(added)}): {added[:20]}\n"
                f"  Modified ({len(modified)}): {modified[:20]}\n"
                f"  Deleted ({len(deleted)}): {deleted[:20]}"
            )

            self._audit.append(
                {
                    "event": "backup_item_completed",
                    "schema_version": 1,
                    "run_id": run_id,
                    "source_path": source_path,
                    "source_name": source_name,
                    "selected_destination_types": list(selected_destination_types),
                    "duration_seconds": patch["duration_seconds"],
                    "files_in_archive": files_in_archive,
                    "snapshot_total_files": len(new_snapshot),
                    "zip_size_bytes": zip_size,
                    "version": version,
                    "change_summary": {
                        "added_count": len(added),
                        "modified_count": len(modified),
                        "deleted_count": len(deleted),
                        "added_files": added,
                        "modified_files": modified,
                        "deleted_files": deleted,
                    },
                    "destination_summaries": created_dest_summaries,
                }
            )

            return created_dest_summaries
        except Exception as ex:
            self._audit.append(
                {
                    "event": "backup_item_failed",
                    "schema_version": 1,
                    "run_id": run_id,
                    "source_path": source_path,
                    "duration_seconds": round(time.perf_counter() - t0, 3),
                    "error_class": type(ex).__name__,
                    "error_message": str(ex),
                    "traceback": traceback.format_exc(),
                }
            )
            raise

    def backup_all(self, selected_destination_types=None):
        sources = self._get_sources()
        if selected_destination_types is None:
            selected_destination_types = self.cfg.get("selected_destination_types", ["path"])

        self._audit.append(
            {
                "event": "backup_batch_started",
                "schema_version": 1,
                "sources": list(sources),
                "selected_destination_types": list(selected_destination_types),
            }
        )

        for src in sources:
            try:
                self.backup_item(src, selected_destination_types)
            except Exception as ex:
                self._log(f"Backup failed for {src}: {ex}")
