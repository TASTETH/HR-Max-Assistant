"""Create a private .env after cloning. Never overwrite an existing configuration."""
from __future__ import annotations

import argparse
import secrets
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def create_env(path: Path, *, template: Path | None = None) -> Path:
    template = template or ROOT / ".env.example"
    app_password = secrets.token_urlsafe(24)
    local_password = secrets.token_urlsafe(24)
    values = {
        "MAX_BOT_TOKEN": "",
        "MAX_WEBHOOK_SECRET": secrets.token_urlsafe(32),
        "POSTGRES_PASSWORD": secrets.token_urlsafe(24),
        "DATABASE_APP_PASSWORD": app_password,
        "DATABASE_URL": f"postgresql://korsik_app:{app_password}@127.0.0.1:55432/korsik",
        "DATABASE_ADMIN_URL": f"postgresql://korsik_local_admin:{local_password}@127.0.0.1:55432/korsik",
        "SQLALCHEMY_DATABASE_URL": f"postgresql+psycopg://korsik_app:{app_password}@127.0.0.1:55432/korsik",
    }
    lines = []
    for line in template.read_text(encoding="utf-8-sig").splitlines():
        key, separator, _ = line.partition("=")
        lines.append(key + "=" + values[key] if separator and key in values else line)
    # Exclusive creation also protects an existing file or symlink during races.
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


def main():
    parser = argparse.ArgumentParser(description="Create local configuration with random credentials")
    parser.add_argument("--path", type=Path, default=ROOT / ".env")
    args = parser.parse_args()
    try:
        create_env(args.path)
    except FileExistsError:
        print("Existing configuration preserved. Edit its MAX_BOT_TOKEN if needed.")
        return
    print("Local configuration created. Set MAX_BOT_TOKEN and check MAX_BOT_URL before starting.")


if __name__ == "__main__":
    main()
