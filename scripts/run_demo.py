"""Run the local MySQL/ledger/camera demonstration and its background workers."""

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from ledger_node import anvil_command
from mysql_server import start as start_mysql
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def port_in_use(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--lan", action="store_true", help="Allow access on the local network.")
    parser.add_argument("--cert", type=Path, help="TLS certificate in PEM format.")
    parser.add_argument("--key", type=Path, help="Private key for the TLS certificate.")
    parser.add_argument("--check", action="store_true", help="Check existing services and exit.")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if bool(args.cert) != bool(args.key):
        parser.error("--cert and --key must be supplied together")
    if args.cert and not (args.cert.is_file() and args.key.is_file()):
        parser.error("--cert and --key must point to existing files")

    host = "0.0.0.0" if args.lan else "127.0.0.1"
    web_options = ["--host", host, "--port", str(args.port), "--no-reload"]
    scheme = "https" if args.cert else "http"
    if args.cert:
        web_options.extend(["--cert", str(args.cert.resolve()), "--key", str(args.key.resolve())])

    def stop_requested(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_requested)
    load_dotenv(ROOT / ".env")
    os.environ["SURVEILLANCE_HTTPS"] = "1" if args.cert else "0"
    if os.getenv("APP_PROFILE") != "mysql":
        raise RuntimeError("Select MySQL first: python scripts/setup_mysql.py")

    from surveillance import create_app
    from surveillance.extensions import db
    from surveillance.services.ledger import LedgerError, chain_identity

    application = create_app()
    if args.check:
        with application.app_context():
            version = db.session.execute(text("SELECT VERSION()")).scalar_one()
            chain_id, _ = chain_identity()
            print(f"MySQL {version} responding; local ledger chain {chain_id} responding.")
        return
    if port_in_use(args.port):
        raise RuntimeError(f"Port {args.port} is in use. Stop that server or select --port NUMBER.")

    children = []
    handles = []
    logs = application.config["PRIVATE_DIR"] / "logs"
    logs.mkdir(exist_ok=True, mode=0o700)

    def spawn(name, command):
        path = logs / f"{name}.log"
        output = path.open("ab")
        path.chmod(0o600)
        handles.append(output)
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=output,
            stderr=output,
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        children.append((name, process))
        return process

    try:
        start_mysql()
        with application.app_context():
            db.session.execute(text("SELECT 1"))
            try:
                chain_identity()
            except LedgerError:
                if port_in_use(8545):
                    raise RuntimeError(
                        "Port 8545 is occupied by an unexpected ledger node."
                    ) from None
                node = spawn("anvil", anvil_command())
                for _ in range(100):
                    if node.poll() is not None:
                        raise RuntimeError(f"Anvil stopped. Inspect {logs / 'anvil.log'}.")
                    try:
                        chain_identity()
                        break
                    except LedgerError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Anvil did not become ready in ten seconds.")

        base = [sys.executable, "-m", "flask", "--app", "surveillance"]
        spawn("trainer", [*base, "trainer"])
        spawn("ledger-worker", [*base, "ledger-worker"])
        web = spawn("web", [*base, "run", *web_options])
        for _ in range(200):
            if web.poll() is not None:
                raise RuntimeError(f"Flask stopped. Inspect {logs / 'web.log'}.")
            if port_in_use(args.port):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Flask did not become ready in twenty seconds.")
        if args.lan:
            print(f"LAN access enabled on port {args.port} using {scheme.upper()}.", flush=True)
            print(f"On the same network, open {scheme}://<this Mac's LAN address>:{args.port}")
        else:
            print(f"Demo ready: {scheme}://127.0.0.1:{args.port}", flush=True)
        print(f"Private process logs: {logs}", flush=True)
        print("Ctrl+C stops the web app and workers started by this launcher.", flush=True)
        reported = set()
        while web.poll() is None:
            for name, child in children:
                if child is not web and child.poll() is not None and name not in reported:
                    print(
                        f"{name} stopped; inspect {logs / (name + '.log')}. "
                        "The web app continues running.",
                        flush=True,
                    )
                    reported.add(name)
            time.sleep(0.5)
        if web.returncode:
            raise RuntimeError(f"Flask exited with code {web.returncode}. Inspect its private log.")
    except KeyboardInterrupt:
        print("Stopping local application processes…", flush=True)
    finally:
        for _, child in reversed(children):
            if child.poll() is None:
                child.send_signal(signal.SIGINT)
        for _, child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=5)
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Cannot run demo: {error}", file=sys.stderr)
        sys.exit(1)
