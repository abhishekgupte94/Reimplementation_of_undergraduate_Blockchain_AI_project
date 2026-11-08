"""Start/stop the project's isolated, password-protected MySQL 8.4 instance."""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import time
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = ROOT / "instance" / "private"
ADMIN_FILE = PRIVATE / "mysql-admin.json"


def server_binary():
    configured = os.getenv("MYSQLD_BINARY")
    candidates = [configured, shutil.which("mysqld"), "/opt/homebrew/opt/mysql@8.4/bin/mysqld"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise RuntimeError("MySQL 8.4 is missing. Install it with brew install mysql@8.4.")


def admin_connection(credentials=None, *, bootstrap=False):
    credentials = credentials or json.loads(ADMIN_FILE.read_text())
    return pymysql.connect(
        unix_socket=credentials["socket"],
        user="root",
        password="" if bootstrap else credentials["password"],
        autocommit=True,
        connect_timeout=2,
    )


def start():
    PRIVATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = PRIVATE / "mysql"
    data.mkdir(exist_ok=True, mode=0o700)
    binary = server_binary()
    if ADMIN_FILE.exists():
        credentials = json.loads(ADMIN_FILE.read_text())
        try:
            connection = admin_connection(credentials)
            connection.close()
            return credentials
        except pymysql.MySQLError:
            pass
    else:
        suffix = hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
        credentials = {
            "socket": f"/private/tmp/room-surveillance-{suffix}.sock",
            "port": 3307,
            "password": secrets.token_urlsafe(36),
        }
    initialized = (data / "mysql").exists()
    if not initialized:
        subprocess.run(
            [
                str(binary),
                "--no-defaults",
                "--initialize-insecure",
                f"--basedir={binary.parent.parent}",
                f"--datadir={data}",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
    log = PRIVATE / "mysql-server.log"
    with log.open("ab") as output:
        subprocess.Popen(
            [
                str(binary),
                "--no-defaults",
                f"--datadir={data}",
                f"--basedir={binary.parent.parent}",
                "--bind-address=127.0.0.1",
                f"--port={credentials['port']}",
                f"--socket={credentials['socket']}",
                "--mysqlx=OFF",
                f"--pid-file={data / 'mysql.pid'}",
                f"--log-error={log}",
            ],
            stdout=output,
            stderr=output,
            start_new_session=True,
        )
    for _ in range(60):
        try:
            connection = admin_connection(credentials, bootstrap=not ADMIN_FILE.exists())
            break
        except pymysql.MySQLError:
            time.sleep(0.5)
    else:
        raise RuntimeError(f"MySQL did not start. Inspect {log}.")
    if not ADMIN_FILE.exists():
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER USER 'root'@'localhost' IDENTIFIED BY %s", (credentials["password"],)
            )
        ADMIN_FILE.write_text(json.dumps(credentials))
        ADMIN_FILE.chmod(0o600)
    connection.close()
    return credentials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "status"])
    args = parser.parse_args()
    if args.action == "start":
        credentials = start()
        print(
            f"Project MySQL ready on 127.0.0.1:{credentials['port']} (private credentials saved)."
        )
    else:
        connection = admin_connection()
        if args.action == "stop":
            with connection.cursor() as cursor:
                cursor.execute("SHUTDOWN")
            print("Project MySQL stopped.")
        else:
            print("Project MySQL is responding.")
        connection.close()


if __name__ == "__main__":
    main()
