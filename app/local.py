"""Run the bot on this computer using its own PostgreSQL cluster, bound to loopback."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from app.cli import migrate, seed
from app.settings import ROOT, Settings


def postgres_bin() -> Path:
    explicit = os.getenv("POSTGRES_BIN", "")
    if explicit:
        return Path(explicit)
    executable = shutil.which("pg_ctl")
    if executable:
        return Path(executable).parent
    parent = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "PostgreSQL"
    if parent.exists():
        for version in sorted(parent.iterdir(), key=lambda p: p.name, reverse=True):
            if (version / "bin/pg_ctl.exe").exists():
                return version / "bin"
    raise ValueError("PostgreSQL 17+ is required. Set POSTGRES_BIN, or use docker compose up --build -d")


def prepare(settings: Settings):
    if settings.database_admin_url is None:
        raise ValueError("Set DATABASE_ADMIN_URL for the local cluster")
    params = conninfo_to_dict(settings.database_admin_url.get_secret_value())
    if params.get("host") != "127.0.0.1" or params.get("user") != "korsik_local_admin":
        raise ValueError("Local launcher only manages its own loopback cluster with user korsik_local_admin")
    if not params.get("password"):
        raise ValueError("DATABASE_ADMIN_URL must contain a password for the local cluster")
    binary = postgres_bin()
    data = ROOT / "data/postgres"
    logs = ROOT / "data/logs"
    logs.mkdir(parents=True, exist_ok=True)
    suffix = ".exe" if os.name == "nt" else ""
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def command(name, *args, check=True):
        # PostgreSQL is long-lived. Its inherited stdout must be a file, not a
        # PIPE that would keep subprocess.communicate() waiting after pg_ctl exits.
        with (logs / "postgres-setup.log").open("ab") as setup_log:
            result = subprocess.run([str(binary / (name + suffix)), *map(str, args)],
                stdin=subprocess.DEVNULL, stdout=setup_log, stderr=subprocess.STDOUT,
                timeout=45, creationflags=flags)
        if check and result.returncode:
            # initdb/pg_ctl log no token, but keep user-facing failures concise.
            raise RuntimeError(f"{name} failed; check {logs / 'postgres.log'}")
        return result

    if not (data / "PG_VERSION").exists():
        data.parent.mkdir(exist_ok=True)
        handle, filename = tempfile.mkstemp(prefix="korsik-init-", text=True)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as secret_file:
                secret_file.write(params["password"] + "\n")
            command("initdb", "-D", data, "-U", params["user"], "--encoding=UTF8", "--locale=C",
                    "--auth-host=scram-sha-256", "--auth-local=scram-sha-256", "--pwfile", filename)
        finally:
            Path(filename).unlink(missing_ok=True)
    if command("pg_ctl", "status", "-D", data, check=False).returncode:
        command("pg_ctl", "start", "-D", data, "-l", logs / "postgres.log", "-w", "-t", "30",
                "-o", f"-h 127.0.0.1 -p {int(params.get('port', 55432))}")
    maintenance_params = {**params, "dbname": "postgres"}
    database = params.get("dbname", "korsik")
    with psycopg.connect(**maintenance_params, autocommit=True) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    migrate(settings)
    seed(settings)


def main():
    parser = argparse.ArgumentParser(description="Корсик на localhost")
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    settings = Settings()
    prepare(settings)
    print("Корсик: PostgreSQL готов. Локальный адрес: http://127.0.0.1:8000", flush=True)
    if not args.prepare_only:
        import uvicorn
        # This mode deliberately runs the same handlers with a local background runner.
        os.environ["LOCAL_WORKER_ENABLED"] = "true"
        uvicorn.run("app.main:app", host="127.0.0.1", port=8000, log_level="info")


if __name__ == "__main__":
    main()
