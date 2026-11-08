"""Back up SQLite, initialize MySQL, copy data, then switch .env without data loss."""

import json
import secrets
import sqlite3
import sys
from datetime import datetime, timezone

from dotenv import dotenv_values
from flask_migrate import upgrade
from mysql_server import PRIVATE, ROOT, admin_connection, start
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import URL

sys.path.insert(0, str(ROOT))


def main():
    credentials = start()
    app_file = PRIVATE / "mysql-app.json"
    if app_file.exists():
        app_credentials = json.loads(app_file.read_text())
    else:
        app_credentials = {"username": "surveillance_app", "password": secrets.token_urlsafe(36)}
        app_file.write_text(json.dumps(app_credentials))
        app_file.chmod(0o600)
    connection = admin_connection(credentials)
    with connection.cursor() as cursor:
        cursor.execute(
            "CREATE DATABASE IF NOT EXISTS surveillance CHARACTER SET utf8mb4 "
            "COLLATE utf8mb4_unicode_ci"
        )
        cursor.execute(
            "CREATE USER IF NOT EXISTS 'surveillance_app'@'127.0.0.1' IDENTIFIED BY %s",
            (app_credentials["password"],),
        )
        cursor.execute("GRANT ALL PRIVILEGES ON surveillance.* TO 'surveillance_app'@'127.0.0.1'")
    connection.close()
    target_url = URL.create(
        "mysql+pymysql",
        username=app_credentials["username"],
        password=app_credentials["password"],
        host="127.0.0.1",
        port=credentials["port"],
        database="surveillance",
        query={"charset": "utf8mb4"},
    )
    env_path = ROOT / ".env"
    configuration = dict(dotenv_values(env_path))
    from surveillance import create_app
    from surveillance.extensions import db

    application = create_app(
        {
            "SQLALCHEMY_DATABASE_URI": target_url,
            "APP_PROFILE": "mysql",
            "SECRET_KEY": configuration.get("SECRET_KEY") or secrets.token_hex(32),
        }
    )
    backups = PRIVATE / "backups"
    backups.mkdir(exist_ok=True, mode=0o700)
    marker = PRIVATE / "mysql-import.json"
    source = ROOT / "instance" / "surveillance.sqlite"
    counts = {}
    with application.app_context():
        upgrade(directory=str(ROOT / "migrations"))
        if source.exists() and not marker.exists():
            for table in db.metadata.sorted_tables:
                if table.name != "ledger_state" and db.session.scalar(
                    select(db.func.count()).select_from(table)
                ):
                    raise RuntimeError("Target MySQL contains data. Refusing to overwrite it.")
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = backups / f"sqlite-before-mysql-{stamp}.sqlite"
            with sqlite3.connect(source) as original, sqlite3.connect(backup) as destination:
                original.backup(destination)
            backup.chmod(0o600)
            engine = create_engine(URL.create("sqlite", database=str(backup)))
            with engine.connect() as reader:
                available = set(inspect(engine).get_table_names())
                for table in db.metadata.sorted_tables:
                    if table.name not in available or table.name == "ledger_state":
                        continue
                    rows = [dict(row) for row in reader.execute(select(table)).mappings()]
                    if rows:
                        db.session.execute(table.insert(), rows)
                    counts[table.name] = len(rows)
                db.session.commit()
            engine.dispose()
            marker.write_text(json.dumps({"backup": str(backup), "row_counts": counts}, indent=2))
            marker.chmod(0o600)
            print("SQLite snapshot backed up; imported row counts:", json.dumps(counts))
        elif marker.exists():
            print("Previous MySQL import retained; no data overwritten.")
        # Imported pre-ledger rows become the migration baseline.
        result = application.test_cli_runner().invoke(args=["ledger-backfill"])
        if result.exit_code:
            raise RuntimeError(result.output)
        print(result.output.strip())
        from surveillance.models import AuditEntry
        from surveillance.services.ledger import enqueue_audit

        if not db.session.scalar(
            db.select(AuditEntry.id).where(AuditEntry.kind == "system.mysql_setup")
        ):
            imported = (
                json.loads(marker.read_text()).get("row_counts", {}) if marker.exists() else {}
            )
            enqueue_audit(
                "system.mysql_setup",
                {
                    "backend": "mysql",
                    "imported_rows": imported,
                    "ledger": "local-anvil-development",
                },
            )
            db.session.commit()
    if env_path.exists() and configuration.get("APP_PROFILE") != "mysql":
        backup_env = backups / "env-before-mysql"
        if not backup_env.exists():
            backup_env.write_bytes(env_path.read_bytes())
            backup_env.chmod(0o600)
    configuration.update(
        APP_PROFILE="mysql",
        DATABASE_URL=target_url.render_as_string(hide_password=False),
        SECRET_KEY=application.config["SECRET_KEY"],
        LEDGER_RPC_URL="http://127.0.0.1:8545",
        LEDGER_CHAIN_ID="31337",
    )
    # JSON quoting is accepted by dotenv and avoids shell interpolation.
    env_path.write_text(
        "\n".join(
            f"{key}={json.dumps(str(value))}"
            for key, value in configuration.items()
            if value is not None
        )
        + "\n"
    )
    env_path.chmod(0o600)
    print(
        "Live .env now selects MySQL. Original SQLite and its consistent backup remain available."
    )


if __name__ == "__main__":
    main()
