# Easy Backup by PromptITEasy.com

Easy Backup is a secure, local-first backup and versioning application designed for creators, developers, and small teams who want powerful protection without complexity.

## Core Value

- Protect critical files and folders with automated, versioned ZIP backups.
- Send backups to multiple destinations in one run.
- Keep full control of data with local, AirGap, cloud, and Git repository options.
- Use a clean dashboard with secure-status visibility and quick actions.

## Key Features

### Source Protection

- Add up to 5 source items (files and/or folders).
- Mix source types in the same protection set.
- File-change detection with added/modified/deleted tracking.
- Snapshot-style versioning with per-source daily version reset.

### Multi-Destination Backup

Run backups to multiple destinations at the same time:

- **Local Vault (Path)**  
  - Up to 3 local/UNC/NAS path destinations.
  - Optional date-based folder organization.

- **AirGap Backup (FTP)**  
  - Up to 2 FTP destination endpoints.
  - Per-endpoint username/password support.
  - TLS (FTPS) and passive mode options.

- **Cloud Vault**  
  - AWS S3, Azure Blob, and Google Cloud Storage.
  - Provider-specific secure credential storage.

- **Versioned Repo Backup (Git)**  
  - Write backups into a local Git repository path.
  - Optional auto-commit and auto-push workflow.
  - Branch/remote/subfolder controls.

### Automation & Scheduling

- Manual one-click backup.
- Auto-backup via file watcher (debounced change trigger).
- Interval scheduler (1-1440 minutes).
- Mini-calendar scheduler for date/time slot-based backups.

### Security & Reliability

- Secure credential handling via system keyring (not plain text in config).
- Local-first architecture; no forced cloud dependency.
- Graceful error handling with activity logging.
- Retention control to limit stored backup count.

### Dashboard & UX

- Modern card-based interface with a secure visual hierarchy.
- Expandable sidebar navigation.
- Active destination summary and destination selection controls.
- Vault usage overview with progress indicator.
- Activity log with date/time stamped events.
- Light/Dark mode toggle.

## Best For

- Solo developers and makers protecting work-in-progress files.
- Teams needing simple, resilient backup coverage without heavy infrastructure.
- Users wanting “safe by default” backup workflows with professional destination flexibility.

