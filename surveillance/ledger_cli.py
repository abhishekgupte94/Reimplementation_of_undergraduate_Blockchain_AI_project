import json
import time

import click
from filelock import FileLock, Timeout
from flask import current_app

from .extensions import db
from .models import AuditEntry, LedgerAnchor
from .services.ledger import AUDITED_TABLES, audit_source, process_anchor, verify_audit


def register_ledger_cli(app):
    @app.cli.command("ledger-worker")
    @click.option("--once", is_flag=True, help="Try one due outbox item and exit.")
    def worker(once):
        """Asynchronously anchor pending commitments to the local Anvil chain."""
        lock = FileLock(str(current_app.config["PRIVATE_DIR"] / "locks" / "ledger-worker.lock"))
        try:
            with lock.acquire(timeout=0):
                click.echo("Ledger worker ready. Only hash commitments leave MySQL.")
                while True:
                    processed = process_anchor()
                    db.session.remove()
                    if once:
                        break
                    if not processed:
                        time.sleep(1)
        except Timeout as error:
            raise click.ClickException("Another ledger worker is running.") from error
        except KeyboardInterrupt:
            click.echo("Ledger worker stopped; its outbox is durable.")

    @app.cli.command("ledger-verify")
    @click.option("--chain", is_flag=True, help="Also require and verify mined Ethereum anchors.")
    def verify(chain):
        """Check audit hashes, live source snapshots, and optionally chain receipts."""
        result = verify_audit(check_chain=chain)
        click.echo(json.dumps(result, indent=2))
        if not result["ok"]:
            raise click.ClickException("Audit verification failed.")

    @app.cli.command("ledger-status")
    def status():
        """Show pending/anchored counts; this command needs no blockchain connection."""
        counts = dict(
            db.session.execute(
                db.select(LedgerAnchor.status, db.func.count()).group_by(LedgerAnchor.status)
            ).all()
        )
        click.echo(
            json.dumps(
                {"entries": db.session.query(AuditEntry).count(), "anchors": counts}, indent=2
            )
        )

    @app.cli.command("ledger-backfill")
    def backfill():
        """Snapshot existing observations once when migrating a pre-ledger database."""
        from .models import PresenceInterval, RecognitionEvent

        classes = {"recognition_event": RecognitionEvent, "presence_interval": PresenceInterval}
        if "weapon_alert" in db.metadata.tables:
            from .weapon_models import WeaponAlert

            classes["weapon_alert"] = WeaponAlert
        audited = {
            (row.source_type, row.source_id)
            for row in db.session.scalars(
                db.select(AuditEntry).where(AuditEntry.source_type.is_not(None))
            )
        }
        count = 0
        for name in AUDITED_TABLES:
            if name not in classes:
                continue
            for record in db.session.scalars(db.select(classes[name])):
                if (name, str(record.id)) not in audited:
                    audit_source(record, kind=f"{name}.migration_snapshot")
                    count += 1
        db.session.commit()
        click.echo(f"Backfilled {count} operational snapshots.")
