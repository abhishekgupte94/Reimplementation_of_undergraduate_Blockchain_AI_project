import pytest
from conftest import send_frame

from surveillance.extensions import db
from surveillance.models import AuditEntry, LedgerAnchor, LedgerState, RoomArrival
from surveillance.services import ledger


class FakeRPC:
    def __init__(self):
        self.transactions = {}

    def __call__(self, method, params=None):
        params = params or []
        if method == "eth_chainId":
            return hex(31337)
        if method == "eth_blockNumber":
            return hex(len(self.transactions))
        if method == "eth_accounts":
            return ["0x" + "1" * 40]
        if method == "eth_sendTransaction":
            tx_hash = "0x" + f"{len(self.transactions) + 1:064x}"
            self.transactions[tx_hash] = {"input": params[0]["data"], "hash": tx_hash}
            return tx_hash
        if method == "eth_getTransactionReceipt":
            return {
                "status": "0x1",
                "blockNumber": hex(list(self.transactions).index(params[0]) + 1),
            }
        if method == "eth_getTransactionByHash":
            return self.transactions.get(params[0])
        if method == "eth_getBlockByNumber":
            number = int(params[0], 16)
            return {
                "hash": "0x" + "a" * 64,
                "transactions": []
                if number == 0
                else [list(self.transactions.values())[number - 1]],
            }
        raise AssertionError(method)


def test_arrival_and_audit_are_atomic(trained, app):
    capture = trained.post("/api/camera/start", json={"room": 1}).json
    assert send_frame(trained, capture).status_code == 200
    with app.app_context():
        assert db.session.query(RoomArrival).count() == 0
        assert db.session.query(AuditEntry).count() == 0
    assert send_frame(trained, capture, sequence=1).status_code == 200
    assert send_frame(trained, capture, sequence=2).status_code == 200
    with app.app_context():
        assert db.session.query(RoomArrival).count() == 1
        assert db.session.query(AuditEntry).count() == 1
        assert db.session.query(LedgerAnchor).count() == 1
        assert ledger.verify_audit()["ok"]
        before = db.session.query(AuditEntry).count()
        ledger.enqueue_audit("transaction.rollback", {"value": "discard"})
        db.session.rollback()
        assert db.session.query(AuditEntry).count() == before


def test_same_room_reacquisition_adds_no_audit_entry(trained, app):
    capture = trained.post("/api/camera/start", json={"room": 1}).json
    assert send_frame(trained, capture).status_code == 200
    assert send_frame(trained, capture, sequence=1).status_code == 200
    assert trained.post("/api/camera/stop", json=capture).status_code == 200
    capture = trained.post("/api/camera/start", json={"room": 1}).json
    assert send_frame(trained, capture).status_code == 200
    assert send_frame(trained, capture, sequence=1).status_code == 200
    with app.app_context():
        assert db.session.query(AuditEntry).count() == 1
        assert ledger.verify_audit()["ok"]


def test_chain_outage_does_not_block_capture_or_reports(trained, app, monkeypatch):
    capture = trained.post("/api/camera/start", json={"room": 1}).json
    assert send_frame(trained, capture).status_code == 200
    assert send_frame(trained, capture, sequence=1).status_code == 200

    def unavailable(*args, **kwargs):
        raise ledger.LedgerError("offline")

    monkeypatch.setattr(ledger, "rpc", unavailable)
    with app.app_context():
        assert ledger.process_anchor()
        anchor = db.session.get(LedgerAnchor, 1)
        assert anchor.status == "pending" and anchor.attempts == 1
        assert ledger.verify_audit()["ok"]
        assert not ledger.verify_audit(check_chain=True)["ok"]
    assert trained.get("/api/enrollment/status").status_code == 200


def test_operational_tamper_and_deletion_are_detected(trained, app):
    capture = trained.post("/api/camera/start", json={"room": 1}).json
    assert send_frame(trained, capture).status_code == 200
    assert send_frame(trained, capture, sequence=1).status_code == 200
    with app.app_context():
        db.session.execute(db.update(RoomArrival).values(room=2))
        db.session.commit()
        result = ledger.verify_audit()
        assert not result["ok"] and any("Operational row" in error for error in result["errors"])
        db.session.execute(db.delete(RoomArrival))
        db.session.commit()
        assert any("deleted" in error for error in ledger.verify_audit()["errors"])


def test_audit_payload_tamper_and_append_only_orm(app):
    with app.app_context():
        entry = ledger.enqueue_audit("test.checkpoint", {"value": 1})
        db.session.commit()
        entry.kind = "changed"
        with pytest.raises(ValueError, match="append-only"):
            db.session.commit()
        db.session.rollback()
        db.session.execute(db.update(AuditEntry).values(payload={"value": 2}))
        db.session.commit()
        assert not ledger.verify_audit()["ok"]


def test_hash_only_anchor_and_tail_truncation_detection(app, monkeypatch):
    chain = FakeRPC()
    monkeypatch.setattr(ledger, "rpc", chain)
    with app.app_context():
        first = ledger.enqueue_audit("test.checkpoint", {"private_name": "Never sent to chain"})
        db.session.commit()
        first_hash = first.entry_hash
        second = ledger.enqueue_audit("test.checkpoint", {"value": 2})
        db.session.commit()
        second_id = second.id
        assert ledger.process_anchor() and ledger.process_anchor()
        assert ledger.verify_audit(check_chain=True)["ok"]
        for transaction in chain.transactions.values():
            payload = bytes.fromhex(transaction["input"][2:])
            assert len(payload) == len(ledger.PREFIX) + 32
            assert b"Never sent" not in payload
        # Simulate a DB administrator bypassing append-only application controls.
        db.session.execute(db.delete(LedgerAnchor).where(LedgerAnchor.entry_id == second_id))
        db.session.execute(db.delete(AuditEntry).where(AuditEntry.id == second_id))
        db.session.execute(db.update(LedgerState).values(sequence=1, head_hash=first_hash))
        db.session.commit()
        assert ledger.verify_audit()["ok"]
        result = ledger.verify_audit(check_chain=True)
        assert not result["ok"] and any("absent from SQL" in error for error in result["errors"])
