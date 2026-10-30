# Token Ledger Operations and Reversible Cutover Staging Guide

This document defines the operational deployment procedures, service supervision patterns, and reversible cutover staging protocol for the local-first Token Ledger engine.

---

## 1. Operational Overview & Local-First Principles

Token Ledger is architected as an offline, local-first analytics and observability system. Operational deployments must adhere to these fundamental principles:
- **Loopback-Only Isolation:** All web services bind exclusively to loopback interfaces (`127.0.0.1`, `::1`, or `localhost`). No external network interfaces or public sockets are opened.
- **Zero External Dependencies:** Dashboards, static assets, reports, and exports are served or rendered offline without CDNs or outbound telemetry.
- **Explicit Ingestion:** Log files are never discovered via automated directory scanning; all ingestion requires explicit, validated file paths.
- **Non-Destructive Coexistence:** The staging deployment runs side-by-side with existing legacy tools (`tokentrack.py` and artifact workflows) without modifying legacy data stores or log files.

---

## 2. Local-First Service Deployment

### 2.1 Local Loopback Web Service & API

The local web service provides the interactive dashboard and hardened REST API.

#### Execution Command
```bash
PYTHONPATH=src python3 -m token_ledger serve --db PATH --host 127.0.0.1 --port 8765
```

#### Command Options
- `--db PATH`: Path to the SQLite database file (required; schema migrations run automatically on open).
- `--host HOST`: Loopback interface to bind (default: `127.0.0.1`; binding non-loopback addresses is strictly forbidden).
- `--port PORT`: Loopback TCP port (default: `8765`).
- `--web-dist PATH`: Path to static bundled UI directory (default: `web/dist`).

#### Security Baseline
- **Host Validation:** Inbound requests must present a `Host` header exactly matching the bound host and port (`127.0.0.1:8765`). Spoofed or external host headers receive HTTP 400/403.
- **Zero CORS:** Cross-origin resource sharing headers are omitted; preflight `OPTIONS` requests return HTTP 405.
- **Same-Origin & CSRF:** State-modifying requests (`POST`, `PUT`, `DELETE`) require identical origin headers (`Origin` or `Referer`) and a cryptographic `X-CSRF-Token` matching the session cookie (`tl_session`).
- **Bootstrap Verification:** Verify server readiness with loopback curl:
  ```bash
  curl -s http://127.0.0.1:8765/api/v1/bootstrap
  ```

---

### 2.2 Background / Unattended Reports Daemon

Token Ledger includes an automated report execution engine that periodically runs due scheduled exports (JSON, CSV, HTML, PDF, PNG) to managed local storage.

#### 2.2.1 Foreground / Continuous Polling Daemon
For unattended interactive sessions or simple containerized execution:
```bash
PYTHONPATH=src python3 -m token_ledger reports daemon --db PATH --interval 3600
```
- `--db PATH`: SQLite database file (required).
- `--interval SECONDS`: Polling interval in seconds (default: `300`; set to `3600` for hourly checks).
- `--managed-root PATH`: Directory where generated reports are written (default: `data/exports/`, which is git-ignored).

#### 2.2.2 Systemd User Unit Configuration
For persistent unattended scheduling across desktop sessions, generate configuration using the `--systemd` flag:
```bash
PYTHONPATH=src python3 -m token_ledger reports daemon --systemd --interval 3600 --db /path/to/token_ledger.db
```

##### 1. Create User Service (`~/.config/systemd/user/token-ledger-reports.service`):
```ini
[Unit]
Description=Token Ledger Scheduled Reports Runner
After=network.target

[Service]
Type=oneshot
WorkingDirectory=/home/tankaizokuo/Code/projects/token-tracker
ExecStart=/usr/bin/python3 -m token_ledger reports run-due --db /home/tankaizokuo/Code/projects/token-tracker/data/token_ledger.db --managed-root /home/tankaizokuo/Code/projects/token-tracker/data/exports
Environment=PYTHONPATH=src
```

##### 2. Create User Timer (`~/.config/systemd/user/token-ledger-reports.timer`):
```ini
[Unit]
Description=Run Token Ledger Scheduled Reports every 3600s

[Timer]
OnBootSec=2min
OnUnitActiveSec=3600s
Unit=token-ledger-reports.service

[Install]
WantedBy=timers.target
```

##### 3. Activate and Verify:
```bash
systemctl --user daemon-reload
systemctl --user enable --now token-ledger-reports.timer
systemctl --user list-timers --all | grep token-ledger
```

#### 2.2.3 Crontab Configuration
For environments preferring standard cron scheduling, print crontab syntax using the `--cron` flag:
```bash
PYTHONPATH=src python3 -m token_ledger reports daemon --cron --interval 3600 --db /path/to/token_ledger.db
```

Add the entry to the user's crontab (`crontab -e`):
```cron
# Token Ledger unattended reports runner (every 3600s / hourly)
0 * * * * cd /home/tankaizokuo/Code/projects/token-tracker && PYTHONPATH=src python3 -m token_ledger reports run-due --db /path/to/token_ledger.db --managed-root /home/tankaizokuo/Code/projects/token-tracker/data/exports >> /var/log/token-ledger-reports.log 2>&1
```

---

### 2.3 Incremental Ingestion Pipeline

The ingestion subsystem processes log streams into canonical database models with complete checkpoint durability.

#### Execution Command
```bash
PYTHONPATH=src python3 -m token_ledger ingest --db PATH --adapter <name> --file <path>
```

#### Examples
- Ingesting Oh-My-Pi session logs:
  ```bash
  PYTHONPATH=src python3 -m token_ledger ingest \
    --db /path/to/token_ledger.db \
    --adapter omp \
    --file /path/to/omp-session.jsonl
  ```
- Ingesting Claude Code session logs:
  ```bash
  PYTHONPATH=src python3 -m token_ledger ingest \
    --db /path/to/token_ledger.db \
    --adapter claude-code \
    --file /path/to/claude-session.jsonl
  ```

#### Ingestion Constraints & Guarantees
- **No Automatic Collection:** Source log paths must be specified explicitly via `--file`. Unsolicited scanning of user directories is prohibited.
- **Scoping Controls:**
  - `--account ALIAS`: User/account scope (defaults to `{adapter}_local`).
  - `--harness HARNESS`: Explicit origin harness tag override (`omp` or `claude-code`).
  - `--provider PROVIDER`: Explicit provider override (e.g. `anthropic`, `google`, `openai`).
  - `--route ROUTE`: Access route override (e.g. `direct`, `bedrock`, `openrouter`).
- **Resilient Checkpoints:** Tracks byte offsets and file generation keys in `source_checkpoints`. Incomplete trailing lines are deferred until completed; rotated or truncated files are handled cleanly without duplicate or lost events.
- **Pre-Persistence Privacy:** Sanitizes absolute directory paths and hashes email/account credentials prior to persistence.

---

## 3. Reversible Cutover Staging Protocol

Staging permits complete validation of the rebuilt SQLite engine and web dashboard while strictly preventing modification or corruption of legacy data.

### 3.1 Parallel Staging Isolation
1. **Isolated Database Path:** All staging commands must target a designated staging database file (e.g., `data/staging.db` or a dedicated test path), never the production database path.
2. **Preservation of Existing Stores:**
   - The existing SQLite database and legacy cache files are maintained intact.
   - The private historical artifact (`token-ledger.live.html`) is treated as read-only reference data and remains completely unmodified.
   - Legacy scripts (`tokentrack.py`, `harness.py`, `pricing.json`) remain fully executable in parallel.
3. **No Cross-Interference:** Staging service processes bind to alternate or standard loopback ports without modifying environment variables or existing user configurations.

### 3.2 Verification Checkpoints During Staging
- Verify schema migration: `PYTHONPATH=src python3 -m token_ledger migrate --db data/staging.db`
- Ingest test fixtures: Ingest synthetic fixture files from `tests/fixtures/` to verify adapter parsing.
- Run loopback API verification: Verify query, outcome, and report endpoints.
- Check export generation: Execute `token_ledger reports run-due` and verify generated artifacts in `data/exports/`.

---

## 4. Separately Gated Actions (Strict Exclusions)

To protect system integrity and privacy, the following actions are **explicitly unperformed** during staging and require separate, gated approval before execution:

| Gated Action | Current Staging Status | Condition for Future Execution |
|---|---|---|
| **Private Data Migration** | **NOT PERFORMED** | Only synthetic test fixtures are used in staging. Real private session logs, personal API keys, and private historical prompt files must NEVER be migrated without explicit authorization. |
| **External Archive-Restorer Retirement** | **NOT PERFORMED** | The external splice-and-restore utility (`~/.claude/skills/token-ledger-update/merge_data.py`) and legacy Claude Code artifact update pathways remain fully preserved and operational. |
| **Production Release or Public Push** | **NOT PERFORMED** | Branch `token-ledger-native-events` remains an isolated local staging branch. No code has been pushed to remote tracking repositories, and no production artifact publication has been triggered. |

---

## 5. Reversibility and Rollback Procedure

If rollback is required at any point during staging, follow this clean four-step procedure to terminate all services and return the workstation to its initial state with zero data loss.

### Step 1: Terminate the Local Web Service
If running as a background process or terminal task:
```bash
# Locate and terminate the serve process
pkill -f "token_ledger serve" || true

# Verify loopback port 8765 is released
lsof -ti :8765
```

### Step 2: Stop and Disable Reports Daemon / Schedulers
- **If running continuous daemon:**
  ```bash
  pkill -f "token_ledger reports daemon" || true
  ```
- **If using systemd user timer:**
  ```bash
  systemctl --user stop token-ledger-reports.timer token-ledger-reports.service
  systemctl --user disable token-ledger-reports.timer
  rm -f ~/.config/systemd/user/token-ledger-reports.service ~/.config/systemd/user/token-ledger-reports.timer
  systemctl --user daemon-reload
  ```
- **If using crontab:**
  Remove the `token_ledger reports run-due` line from `crontab -e`.

### Step 3: Remove Staging Database and Generated Exports
Clean up the isolated staging files without touching production or legacy files:
```bash
# Remove staging SQLite database and WAL/SHM files
rm -f data/staging.db data/staging.db-shm data/staging.db-wal

# Clean up staging exports if no longer needed
rm -rf data/exports/*
```

### Step 4: Verify Environment Integrity
1. Confirm no rogue Python processes remain:
   ```bash
   pgrep -a -f token_ledger
   ```
2. Verify legacy CLI operation:
   ```bash
   ./tokentrack.py
   ```
   Confirm that existing legacy reports run identically without errors.
3. Confirm that all original log files, legacy caches, and `token-ledger.live.html` remain untouched.
