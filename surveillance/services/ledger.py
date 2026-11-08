"""Transactional audit snapshots and asynchronous hash-only Ethereum anchoring.

MySQL remains the query store. The local Anvil development chain gives a separate
commitment history; it is single-node and resettable, not distributed immutability.
"""

import hashlib
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from flask import current_app
from sqlalchemy import inspect

from ..extensions import db
from ..models import AuditEntry, LedgerAnchor, LedgerState, utcnow

GENESIS = "0" * 64
PREFIX = b"ROOMAUDITv1:"
AUDITED_TABLES = ("recognition_event", "presence_interval", "weapon_alert")


class LedgerError(RuntimeError):
    pass


def canonical_value(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc).isoformat(timespec="microseconds")
    if isinstance(value, dict):
        return {str(key): canonical_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [canonical_value(item) for item in value]
    return value


def canonical(value):
    return json.dumps(
        canonical_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest_entry(sequence, kind, source_type, source_id, payload, previous_hash, created_at):
    return hashlib.sha256(
        canonical(
            {
                "sequence": sequence,
                "kind": kind,
                "source_type": source_type,
                "source_id": source_id,
                "payload": payload,
                "previous_hash": previous_hash,
                "created_at": created_at,
            }
        )
    ).hexdigest()


def enqueue_audit(kind, payload, *, source_type=None, source_id=None):
    """Add an audit entry/outbox in the caller's transaction; never contacts a node.

    Source payloads use {"record": {column: value, ...}} for reconciliation.
    Names, images and face embeddings must never be passed to public anchoring;
    this service sends only the final hash to its loopback-only development RPC.
    """
    state = db.session.scalar(db.select(LedgerState).where(LedgerState.id == 1).with_for_update())
    if state is None:
        state = LedgerState(id=1, sequence=0, head_hash=GENESIS)
        db.session.add(state)
        db.session.flush()
    payload = canonical_value(payload)
    timestamp = utcnow()
    sequence = state.sequence + 1
    source_id = str(source_id) if source_id is not None else None
    digest = digest_entry(
        sequence, kind, source_type, source_id, payload, state.head_hash, timestamp
    )
    entry = AuditEntry(
        id=sequence,
        kind=kind,
        source_type=source_type,
        source_id=source_id,
        payload=payload,
        previous_hash=state.head_hash,
        entry_hash=digest,
        created_at=timestamp,
    )
    db.session.add(entry)
    db.session.flush()
    db.session.add(
        LedgerAnchor(entry_id=sequence, status="pending", attempts=0, next_attempt_at=timestamp)
    )
    state.sequence = sequence
    state.head_hash = digest
    return entry


def audit_source(record, kind=None):
    """Snapshot a supported operational row after its identity has been assigned."""
    db.session.flush()
    mapper = inspect(type(record))
    table = mapper.local_table.name
    if table not in AUDITED_TABLES:
        raise ValueError(f"Unsupported audit source: {table}")
    # Hash the actual persisted representation (not a pre-flush Python float
    # that MySQL FLOAT may round), so later reconciliation is deterministic.
    db.session.refresh(record)
    payload = {"record": {column.name: getattr(record, column.name) for column in mapper.columns}}
    return enqueue_audit(
        kind or f"{table}.snapshot", payload, source_type=table, source_id=record.id
    )


def rpc(method, params=None):
    endpoint = current_app.config["LEDGER_RPC_URL"]
    parsed = urlparse(endpoint)
    # Unlocked funded development accounts must never be used on remote networks.
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise LedgerError("Ledger RPC must be an HTTP loopback Anvil development node.")
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        ).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            result = json.load(response)
    except (OSError, urllib.error.URLError, ValueError) as error:
        raise LedgerError(f"Local ledger RPC unavailable: {error}") from error
    if result.get("error"):
        raise LedgerError(str(result["error"]))
    return result.get("result")


def chain_identity():
    chain_id = str(int(rpc("eth_chainId"), 16))
    expected = str(current_app.config["LEDGER_CHAIN_ID"])
    if chain_id != expected:
        raise LedgerError(f"Wrong development chain: expected {expected}, received {chain_id}.")
    block = rpc("eth_getBlockByNumber", ["0x0", False])
    if not block or not block.get("hash"):
        raise LedgerError("The local chain has no genesis block.")
    return chain_id, block["hash"]


def process_anchor():
    """Try one due outbox item. A failed RPC leaves operational writes untouched."""
    anchor = db.session.scalar(
        db.select(LedgerAnchor)
        .where(LedgerAnchor.status == "pending", LedgerAnchor.next_attempt_at <= utcnow())
        .order_by(LedgerAnchor.entry_id)
    )
    if not anchor:
        return False
    try:
        chain_id, genesis_hash = chain_identity()
        state = db.session.get(LedgerState, 1)
        if state.chain_id and (state.chain_id != chain_id or state.genesis_hash != genesis_hash):
            raise LedgerError("Development chain identity changed; restore its persisted state.")
        if not state.chain_id:
            state.chain_id, state.genesis_hash = chain_id, genesis_hash
            db.session.commit()
        entry = db.session.get(AuditEntry, anchor.entry_id)
        if not anchor.transaction_hash:
            accounts = rpc("eth_accounts")
            if not accounts:
                raise LedgerError("Anvil has no unlocked development account.")
            anchor.transaction_hash = rpc(
                "eth_sendTransaction",
                [
                    {
                        "from": accounts[0],
                        "to": accounts[0],
                        "value": "0x0",
                        "gas": "0x186a0",
                        "data": "0x" + (PREFIX + bytes.fromhex(entry.entry_hash)).hex(),
                    }
                ],
            )
            db.session.commit()  # remember broadcast before polling its receipt
        receipt = rpc("eth_getTransactionReceipt", [anchor.transaction_hash])
        if not receipt:
            raise LedgerError("Anchor transaction is waiting to be mined.")
        if int(receipt.get("status", "0x0"), 16) != 1:
            raise LedgerError("Anchor transaction reverted.")
        anchor.status = "anchored"
        anchor.block_number = int(receipt["blockNumber"], 16)
        anchor.anchored_at = utcnow()
        anchor.last_error = None
        anchor.attempts += 1
        db.session.commit()
    except Exception as error:
        db.session.rollback()
        anchor = db.session.get(LedgerAnchor, anchor.entry_id)
        anchor.attempts += 1
        anchor.last_error = str(error)[:500]
        anchor.next_attempt_at = utcnow() + timedelta(
            seconds=min(300, 2 ** min(anchor.attempts, 8))
        )
        db.session.commit()
    return True


def verify_audit(*, check_chain=False):
    errors, latest, expected_previous = [], {}, GENESIS
    entries = list(db.session.scalars(db.select(AuditEntry).order_by(AuditEntry.id)))
    for expected_sequence, entry in enumerate(entries, 1):
        expected_hash = digest_entry(
            entry.id,
            entry.kind,
            entry.source_type,
            entry.source_id,
            entry.payload,
            entry.previous_hash,
            entry.created_at,
        )
        if entry.id != expected_sequence or entry.previous_hash != expected_previous:
            errors.append(f"Audit sequence/link mismatch at {entry.id}.")
        if expected_hash != entry.entry_hash:
            errors.append(f"Audit payload/hash mismatch at {entry.id}.")
        expected_previous = entry.entry_hash
        if entry.source_type and entry.source_id:
            latest[(entry.source_type, entry.source_id)] = entry.payload.get("record")
    state = db.session.get(LedgerState, 1)
    if state and (state.sequence != len(entries) or state.head_hash != expected_previous):
        errors.append("Stored audit head does not match the entry history.")
    for table_name in AUDITED_TABLES:
        table = db.metadata.tables.get(table_name)
        if table is None:
            continue
        seen = set()
        for row in db.session.execute(db.select(table)).mappings():
            key = (table_name, str(row["id"]))
            seen.add(key)
            if canonical_value(dict(row)) != latest.get(key):
                errors.append(
                    f"Operational row missing or changed since audit: {table_name}:{row['id']}."
                )
        for key in latest:
            if key[0] == table_name and key not in seen:
                errors.append(f"Audited operational row deleted: {table_name}:{key[1]}.")
    anchored = 0
    if check_chain:
        try:
            identity = chain_identity()
            if state and state.chain_id and identity != (state.chain_id, state.genesis_hash):
                errors.append("Blockchain identity differs from the pinned history.")
            for entry in entries:
                anchor = db.session.get(LedgerAnchor, entry.id)
                if not anchor or anchor.status != "anchored":
                    errors.append(f"Audit entry {entry.id} is not yet anchored.")
                    continue
                tx = rpc("eth_getTransactionByHash", [anchor.transaction_hash])
                receipt = rpc("eth_getTransactionReceipt", [anchor.transaction_hash])
                expected_input = "0x" + (PREFIX + bytes.fromhex(entry.entry_hash)).hex()
                if (
                    not tx
                    or tx.get("input", "").lower() != expected_input
                    or not receipt
                    or int(receipt.get("status", "0x0"), 16) != 1
                    or int(receipt["blockNumber"], 16) != anchor.block_number
                ):
                    errors.append(f"Blockchain anchor mismatch at audit entry {entry.id}.")
                else:
                    anchored += 1
            # This dedicated demo chain is also the independent witness for tail
            # truncation. Comparing only receipts named by SQL would miss a DBA
            # deleting the final entries and rewriting the SQL head.
            known = {entry.entry_hash for entry in entries}
            height = int(rpc("eth_blockNumber"), 16)
            for number in range(1, height + 1):
                block = rpc("eth_getBlockByNumber", [hex(number), True])
                for transaction in block.get("transactions", []) if block else []:
                    raw = transaction.get("input", "")
                    try:
                        data = bytes.fromhex(raw.removeprefix("0x"))
                    except ValueError:
                        continue
                    if data.startswith(PREFIX) and len(data) == len(PREFIX) + 32:
                        commitment = data[len(PREFIX) :].hex()
                        if commitment not in known:
                            errors.append(
                                "Blockchain commitment absent from SQL audit history: "
                                + commitment
                                + "."
                            )
        except LedgerError as error:
            errors.append(str(error))
    return {
        "ok": not errors,
        "entries": len(entries),
        "anchored": anchored,
        "chain_checked": check_chain,
        "errors": errors,
    }
