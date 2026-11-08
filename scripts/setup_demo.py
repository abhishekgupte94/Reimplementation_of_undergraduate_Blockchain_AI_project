"""Select the SQLite demo profile without overwriting existing configuration."""

import secrets
from pathlib import Path

destination = Path(__file__).resolve().parents[1] / ".env"
if destination.exists():
    print("Existing .env kept. Inspect its APP_PROFILE before starting.")
else:
    destination.write_text(
        f"APP_PROFILE=demo\nSECRET_KEY={secrets.token_hex(32)}\nDISPLAY_TIMEZONE=Europe/Dublin\n"
    )
    destination.chmod(0o600)
    print("Created .env with the explicit SQLite demo profile and a unique secret.")
