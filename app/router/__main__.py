"""python -m app.router: install, handle JSON, run workers, or tick the scheduler."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from .repository import PostgresRepository
from .schemas import IncomingEvent
from .core import Router


def main() -> int:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    parser = argparse.ArgumentParser(description="Persistent router for the supplied korsik PostgreSQL database")
    sub = parser.add_subparsers(dest="command", required=True)
    install = sub.add_parser("install", help="Apply router schema using an owner/migrator DATABASE_URL")
    install.add_argument("--role", help="Optional owner role, e.g. korsik_owner")
    handle = sub.add_parser("handle", help="Accept an authenticated normalized event JSON")
    handle.add_argument("file", help="JSON file, or - for stdin")
    work = sub.add_parser("work", help="Process inbox and analysis; optional ROUTER_RAG_URL enables RAG")
    work.add_argument("--once", action="store_true")
    work.add_argument("--interval", type=float, default=2)
    sub.add_parser("tick", help="Queue due plan messages and reminders")
    sub.add_parser("recover", help="Recover expired worker leases")
    args = parser.parse_args()

    async def run():
        repo = PostgresRepository(os.getenv("DATABASE_ADMIN_URL") or os.getenv("DATABASE_URL")) if args.command == "install" else PostgresRepository()
        if args.command == "install":
            await asyncio.to_thread(repo.install, args.role)
            return {"installed": True}
        if args.command == "recover":
            return await asyncio.to_thread(repo.recover)
        router = Router(repo)
        if args.command == "handle":
            raw = sys.stdin.read(65537) if args.file == "-" else Path(args.file).read_text(encoding="utf-8-sig")
            if len(raw.encode("utf-8")) > 65536:
                raise ValueError("Event JSON exceeds 64 KiB")
            result = await router.handle(IncomingEvent.model_validate_json(raw))
            return result.model_dump(mode="json")
        if args.command == "tick":
            return {"queued": await router.tick()}
        if not 0.1 <= args.interval <= 60:
            raise ValueError("Worker interval must be between 0.1 and 60 seconds")
        while True:
            result = await router.work_once()
            if args.once:
                return result
            print(json.dumps(result, ensure_ascii=False), flush=True)
            await asyncio.sleep(args.interval)

    try:
        result = asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Do not print connection strings, webhook bodies, or invitation secrets.
        print(json.dumps({"error": type(exc).__name__, "message": "Router operation failed; verify configuration and worker state."}), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if isinstance(result, dict) and result.get("status") == "dead" else 0


if __name__ == "__main__":
    raise SystemExit(main())
