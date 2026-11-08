# AGENTS.md

Blocka.i is a local Flask rebuild of the 2019 surveillance demo. Participants enroll photos and use one webcam to simulate visits to two rooms. Administrators review observed activity, ask the rule-based chatbot about it, and acknowledge SSD pen simulation alerts. MySQL stores the operational data and a local Anvil chain anchors audit hashes.

## Working in this checkout

- Read the current implementation before editing. Other chats may share this workspace and its running services. Preserve their work and keep edits scoped to the request.
- Follow the existing Flask blueprint and service boundaries. Use Jinja, plain JavaScript and CSS for web changes. Keep endpoint payloads compatible with their existing callers unless the task calls for a contract change.
- Use Python 3.13 for local verification. Dependencies are declared in `pyproject.toml` and pinned in `requirements.lock`. Keep both in sync when changing Python dependencies.
- Treat `.env`, `instance/`, enrollment photos, model weights, database files, chain state, TLS keys and private logs as local data. Keep them out of commits, fixtures and command output. Use synthetic enrollment data in tests.

## Where to work

| Area | Entry points |
| --- | --- |
| App factory, configuration and extensions | `surveillance/__init__.py`, `extensions.py`, `security.py` |
| Enrollment and recognition | `routes/enrollment.py`, `services/training.py`, `services/vision.py`, `services/recognition.py` |
| Camera and room timing | `routes/camera.py`, `services/presence.py`, `templates/cameras.html`, `static/camera.js` |
| Admin reporting and chatbot | `routes/admin.py`, `services/reporting.py`, `services/chatbot.py`, `static/analytics.js` |
| SSD alerts | `services/weapons.py`, `routes/weapons.py`, `weapon_models.py`, `weapon_cli.py`, `static/weapons.js` |
| Schema and audit anchoring | `models.py`, `migrations/versions/`, `services/ledger.py`, `ledger_cli.py` |

Paths in the table are relative to `surveillance/` unless they already name that directory or `migrations/`.

## Invariants to preserve

- Apply role checks and CSRF protection at the HTTP boundary. Enrollment files belong to the uploader. Recognised identity comes from inference, while the logged-in account owns the camera lease.
- Enrollment requires three distinct valid photos with one face each. Use the same Haar and OpenFace preprocessing for enrollment and live inference. Keep nearest-sample matching for one identity and the linear SVC plus distance gate for multiple identities. Publish a trained model only after its artifact is complete so failed retraining preserves the active model.
- Keep one active capture and one browser frame request in flight. Validate ownership, token, room generation and increasing sequence under the shared capture lock. A room switch closes the previous visit and requires fresh recognition. Reuse a cached match only when the frame, capture generation, model version and matching threshold agree. Record a fresh observation for each recognised identity on every accepted frame, including cache hits.
- Two consecutive matches confirm presence. Bridge gaps of up to three seconds and close a visit at its last observation. A capture lease expires after five seconds without frames. Keep bounding boxes in sampled-frame coordinates so the browser overlay aligns with the preview.
- Commit face observations before submitting optional SSD work. The live simulation accepts the Pen class. Check capture generation and freshness again before recording background results. Keep object observations separate from recognised identities.
- Store timestamps in UTC with subsecond precision. Use `Europe/Dublin` by default for display and report-day boundaries. Keep `room_report` as the source for both charts and chatbot answers. The ChatterBot adapter uses fixed rules and reads the selected participant and date without storing conversation history.
- Use the audited services for changes to recognition events, presence intervals and SSD alerts. Commit the operational record, audit snapshot and outbox entry together. The worker sends only hash commitments to Anvil. Keep RPC calls out of capture and reporting requests. Add a new Alembic revision for schema changes and verify it against both supported database dialects.

## Run and verify

Run commands from the project root with `.venv` active.

```sh
source .venv/bin/activate
python scripts/run_demo.py --check
python scripts/run_demo.py
```

The launcher defaults to `http://127.0.0.1:5001`. MySQL and Anvil listen locally on ports `3307` and `8545`. `--lan` exposes the web listener only. A browser on another device needs trusted HTTPS for webcam capture. The launcher runs without Flask auto-reload, so coordinate restarts with anyone using the demo.

```sh
python -m pytest -q
ruff check surveillance tests scripts migrations
node --check surveillance/static/camera.js
```

- Use focused tests for the code you change. `tests/test_combined_pipeline.py` covers the interaction between camera, reporting, SSD alerts and audit snapshots.
- The normal test suite uses isolated SQLite databases. For the MySQL test, point `TEST_MYSQL_URL` at a dedicated empty database ending in `_test`. The test creates and removes its schema.
- For relevant changes, use `flask --app surveillance db check`, `model-smoke`, `weapon-smoke`, or `ledger-verify --chain`. Database and chain checks require the corresponding local services. Report skipped checks and distinguish model smoke tests from live webcam verification.

Use [README.md](README.md) for setup and the browser walkthrough. See [MySQL and ledger](docs/mysql-ledger.md) for migrations and recovery, and [SSD pen simulation](docs/weapon-detection.md) for model setup and smoke checks.
