"""Run the project's persisted local Anvil node without exposing development keys."""

import os
import platform
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def anvil_command():
    operating_system = {"Darwin": "darwin", "Linux": "linux", "Windows": "win32"}.get(
        platform.system()
    )
    architecture = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64", "AMD64": "amd64"}.get(
        platform.machine()
    )
    suffix = ".exe" if operating_system == "win32" else ""
    binary = (
        ROOT
        / "node_modules"
        / "@foundry-rs"
        / (f"anvil-{operating_system}-{architecture}")
        / "bin"
        / f"anvil{suffix}"
    )
    if not binary.is_file():
        raise RuntimeError("Pinned Anvil binary missing. Run npm ci --ignore-scripts first.")
    private = ROOT / "instance" / "private" / "chain"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    return [
        str(binary),
        "--host",
        "127.0.0.1",
        "--port",
        "8545",
        "--chain-id",
        "31337",
        "--accounts",
        "1",
        "--state",
        str(private / "anvil-state.json"),
        "--state-interval",
        "1",
        "--preserve-historical-states",
        "--no-cors",
        "--quiet",
    ]


def main():
    command = anvil_command()
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
