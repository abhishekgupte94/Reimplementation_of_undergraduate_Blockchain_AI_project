# MySQL plus a local Ethereum audit ledger

The demo uses **MySQL 8.4 LTS** for accounts, enrollment metadata, room arrivals, facial expression observations, model pointers and weapon alerts. The rule-based chatbot and room charts read through SQLAlchemy.

The second store is **Anvil 1.7.1**, Foundry's local Ethereum development node, on `127.0.0.1:8545`, chain ID `31337`. It is a real EVM development blockchain with blocks and mined transactions. It is a single-node demonstration, resettable by its operator, and does **not** provide independent-validator or production immutability guarantees. A local SQL hash chain by itself is an audit ledger, not a distributed blockchain; this implementation additionally anchors its commitments to Anvil.

## Why this pairing

- MySQL provides indexed queries for participant and date filters and chatbot responses.
- Arrival, expression and weapon alert writes enqueue a canonical snapshot, hash-linked audit entry and outbox row in the **same SQL transaction**. A rollback discards them together.
- A separate ledger worker sends only `ROOMAUDITv1:` plus the 32-byte SHA-256 commitment in an Ethereum transaction to its local development account.
- RPC failure leaves a pending retry in MySQL. Capture, face training, reporting and the chatbot continue operating while Anvil is unavailable. Exponential retries cap at 300 seconds; pending items survive restarts.
- This avoids forcing a slow or unavailable secondary store into the camera request transaction. It also avoids placing identity records into public immutable storage.

The chain provides a way to verify audit evidence independently of the SQL query path.

## Start and stop

MySQL was installed through Homebrew, but this project uses its **own private server/data directory**, not the global Homebrew data directory. It binds only to loopback on port **3307**; MySQL X Protocol is disabled. Root and application credentials are generated randomly and stored with owner-only permissions under `instance/private/`. They are never printed by the setup command.

```sh
source .venv/bin/activate
python scripts/mysql_server.py start
flask --app surveillance db upgrade
npm run ledger:node
```

In separate terminals, from the same project directory/configuration:

```sh
flask --app surveillance ledger-worker
flask --app surveillance trainer
flask --app surveillance run --host 127.0.0.1 --port 5001 --no-reload
```

Anvil's command enables state dumps every second and preserves historical states. Stop it with Ctrl+C to flush the last state before shutting down. Keep `instance/private/chain/` together with database backups. The local development accounts must not receive real funds or be used on public networks. The RPC helper rejects non-loopback endpoints and unexpected chain IDs; CORS is disabled on the node.

```sh
python scripts/mysql_server.py status
python scripts/mysql_server.py stop
```

For a new checkout, install dependencies as described in the main README, install `mysql@8.4`, run `npm ci --ignore-scripts`, and run `python scripts/setup_mysql.py`. The npm script invokes the official platform binary through its package wrapper, so no global Foundry installation is necessary.

## SQLite cutover and recovery

`scripts/setup_mysql.py` initializes the project-specific MySQL server and database, upgrades the schema, creates a consistent SQLite backup through SQLite's backup API, imports rows in foreign-key order, and only then switches `.env` to MySQL. It refuses to overwrite an already populated target when no prior import marker exists. It keeps the original SQLite file, private backup, row-count import manifest and previous `.env`. Rerunning the command preserves the existing import.

The initial SQLite store contained no participant accounts, photographs, models or observations; its existing runtime state was preserved. SQLite support remains available for isolated automated tests and deliberate recovery, but the running application uses the explicit MySQL URL. There is no automatic SQLite fallback on database failure.

Workbench connection: Standard TCP/IP, `127.0.0.1`, port `3307`, schema `surveillance`, user `surveillance_app`. Obtain the local generated password from your private configuration; do not commit it. This is the application account, not root.

## Verification and limits

```sh
flask --app surveillance ledger-status
flask --app surveillance ledger-verify
flask --app surveillance ledger-verify --chain
```

Local verification checks contiguous audit sequence, each payload/header hash, previous-hash links and the stored head. It compares the **latest audited full-column snapshot** against each `room_arrival`, `emotion_observation` and `weapon_alert` row and the historical `recognition_event` and `presence_interval` rows. Missing, changed, deleted or unaudited operational rows fail verification. The verifier hashes the persisted values, including MySQL's floating-point representation and UTC microseconds.

Chain verification additionally checks chain ID/genesis, mined receipts, transaction payloads and block numbers. It scans this dedicated development chain for prefixed commitments absent from SQL, so deleting a suffix of SQL audit/outbox/source rows and rewriting the SQL head does not silently pass while the chain still contains those commitments. Duplicate identical anchor transactions are harmless; a worker crash immediately after broadcast can lead to a duplicate on retry.

The ORM rejects updating or deleting `AuditEntry`. A privileged database administrator can bypass ORM checks with direct SQL; verification is designed to detect the resulting mismatch while the independent chain history is intact. A person controlling both MySQL and the single Anvil node can rewrite/reset both histories. This demonstration therefore provides testable tamper evidence, not a security guarantee against the machine's administrator. Backups and independently operated validators would be required for stronger guarantees.

Every eligible source row must be written through its audited service. `ledger-backfill` establishes a baseline for records imported from the pre-ledger application; it does not prove those records' earlier history. Users, passwords, uploaded photos and embedding artifacts are outside the audited table scope.

## Research sources

Forum discussions were used as design leads, with implementation behavior checked against primary documentation:

- [Ethereum Stack Exchange: storing hashes for off-chain row integrity](https://ethereum.stackexchange.com/questions/145722/best-way-to-store-and-verify-data-onchain-for-offchain-data-integrity) discusses row commitments and batching. Advice about prices or gas costs is not treated as a current cost estimate.
- [Ethereum Stack Exchange: data certification](https://ethereum.stackexchange.com/questions/44301/how-does-data-certification-work) explains the distinction between storing a document and committing its hash.
- [DBA Stack Exchange: relational data integrity](https://dba.stackexchange.com/questions/275660/ensure-data-integrity-in-relational-db) illustrates why an audit record does not itself replace database integrity and access control.
- [MySQL 8.4 InnoDB transaction documentation](https://dev.mysql.com/doc/refman/8.4/en/innodb-transaction-model.html) supports keeping operational writes and their outbox in one transaction.
- [Transactional outbox pattern, by its author](https://microservices.io/patterns/data/transactional-outbox.html) describes committing a message with the database change and relaying it separately.
- [Foundry Anvil documentation](https://getfoundry.sh/anvil/overview/) and the installed `anvil --help` document local development accounts, state persistence and mining behavior.
- [Ethereum JSON-RPC documentation](https://ethereum.org/developers/docs/apis/json-rpc/) defines transaction submission, block lookup and receipt verification used by the worker.
- [Homebrew's MySQL 8.4 formula](https://formulae.brew.sh/formula/mysql@8.4) provides the installed LTS server distribution.
