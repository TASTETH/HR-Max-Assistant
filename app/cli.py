"""Operational commands. No admin endpoints or bot tokens in command arguments."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
from datetime import date
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.max.client import MaxClient, MaxRejected, MaxUncertain
from app.router.repository import PostgresRepository
from app.router.schemas import Choice, QuestionSettings
from app.settings import ROOT, Settings, get_settings


def _script(conn, path: Path):
    # psql's \set is a client directive, not SQL. All SQL transactions stay intact.
    source = "\n".join(line for line in path.read_text(encoding="utf-8-sig").splitlines() if not line.startswith("\\"))
    conn.execute(source, prepare=False)


def migrate(settings: Settings):
    if settings.database_admin_url is None:
        raise ValueError("Set DATABASE_ADMIN_URL for the explicit migration command")
    with psycopg.connect(settings.database_admin_url.get_secret_value(), autocommit=True, row_factory=dict_row) as conn:
        conn.execute("SELECT pg_advisory_lock(hashtext('korsik.bootstrap'))")
        for role in ("korsik_owner", "korsik_app"):
            if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone():
                if role == "korsik_app":
                    conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE").format(
                        sql.Identifier(role), sql.Literal(settings.database_app_password.get_secret_value())))
                else:
                    conn.execute("CREATE ROLE korsik_owner NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
        database = conn.execute("SELECT current_database() AS name").fetchone()["name"]
        conn.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO korsik_owner").format(sql.Identifier(database)))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO korsik_app").format(sql.Identifier(database)))
        conn.execute("SET ROLE korsik_owner")
        if not conn.execute("SELECT to_regclass('korsik.employees') AS name").fetchone()["name"]:
            _script(conn, ROOT / "database/schema.sql")
        if not conn.execute("SELECT to_regprocedure('korsik.finish_send(bigint,bigint,text,text,boolean)') AS name").fetchone()["name"]:
            _script(conn, ROOT / "database/operations.sql")
        _script(conn, ROOT / "database/grants.sql")
        _script(conn, ROOT / "app/router/schema.sql")
        _script(conn, ROOT / "database/002_transport.sql")
        conn.execute("RESET ROLE")
        conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")


def admin_repository(settings):
    if settings.database_admin_url is None:
        raise ValueError("This administrative command requires DATABASE_ADMIN_URL")
    return PostgresRepository(settings.database_admin_url.get_secret_value())


def seed(settings):
    repository = admin_repository(settings)
    with repository.connection() as conn:
        hr = conn.execute("""INSERT INTO korsik.hr_users(auth_subject,display_name,access_role)
            VALUES('local:default','HR Полипластик','admin') ON CONFLICT(auth_subject) DO UPDATE
            SET auth_subject=excluded.auth_subject RETURNING id""").fetchone()["id"]
        for code, name in [("GRPP", "GrPP"), ("GTP", "GTP"), ("LUZHNIKI", "Лужники")]:
            conn.execute("INSERT INTO korsik.org_units(code,name) VALUES(%s,%s) ON CONFLICT DO NOTHING", (code, name))
        if settings.hr_max_chat_id:
            conn.execute("INSERT INTO korsik_router.hr_routes(hr_id,max_chat_id) VALUES(%s,%s) ON CONFLICT(hr_id) DO UPDATE SET max_chat_id=excluded.max_chat_id", (hr, settings.hr_max_chat_id))
        if settings.hr_contact_url:
            conn.execute("INSERT INTO korsik_router.hr_contacts(hr_id,url) VALUES(%s,%s) ON CONFLICT(hr_id) DO UPDATE SET url=excluded.url", (hr, settings.hr_contact_url))
        template = conn.execute("""INSERT INTO korsik.plan_templates(code,name) VALUES('mvp_demo','Демонстрационный опрос адаптации')
            ON CONFLICT(code) DO UPDATE SET code=excluded.code RETURNING id""").fetchone()["id"]
        version = conn.execute("SELECT id FROM korsik.plan_versions WHERE template_id=%s AND version_no=1", (template,)).fetchone()
        if not version:
            vid = conn.execute("INSERT INTO korsik.plan_versions(template_id,version_no,created_by) VALUES(%s,1,%s) RETURNING id", (template, hr)).fetchone()["id"]
            for position, code, kind, body in [
                (1, "welcome", "info", "Добро пожаловать! Я Корсик. Помогу с адаптацией, документами и связью с HR. Команды: /help, /HR, /Food, /plan, /resume."),
                (2, "satisfaction", "rating", "Как вы оцениваете первые дни работы по шкале от 1 до 10?"),
                (3, "retention", "feedback", "Планируете ли вы продолжить работу в компании?"),
            ]:
                conn.execute("""INSERT INTO korsik.template_events(version_id,code,position,kind,body,local_time,response_hours)
                    VALUES(%s,%s,%s,%s,%s,'00:00',168)""", (vid, code, position, kind, body))
            conn.execute("UPDATE korsik.plan_versions SET published_at=clock_timestamp() WHERE id=%s", (vid,))
    return hr


def invite(settings, hr_id=None, site=None):
    token = secrets.token_urlsafe(24)
    repository = admin_repository(settings)
    with repository.connection() as conn:
        if hr_id is None:
            row = conn.execute("SELECT id FROM korsik.hr_users WHERE auth_subject='local:default' AND enabled").fetchone()
            if not row:
                raise ValueError("Run seed, or specify --hr-id")
            hr_id = row["id"]
        employee = conn.execute("INSERT INTO korsik.employees(hr_id) VALUES(%s) RETURNING id", (hr_id,)).fetchone()["id"]
        conn.execute("INSERT INTO korsik.invitations(employee_id,token_hash,expires_at) VALUES(%s,%s,clock_timestamp()+interval '7 days')", (employee, hashlib.sha256(token.encode()).digest()))
        if site:
            conn.execute("INSERT INTO korsik_router.profiles(employee_id,site_code) VALUES(%s,%s)", (employee, site))
    return {"employee_id": employee, "invitation_url": settings.max_bot_url.rstrip("/") + "?start=" + quote(token)}


def approve(settings, employee_id, site, job_title, start_date):
    repository = admin_repository(settings)
    with repository.connection() as conn:
        employee = conn.execute("SELECT * FROM korsik.employees WHERE id=%s FOR UPDATE", (employee_id,)).fetchone()
        if not employee:
            raise ValueError("Employee not found")
        unit = conn.execute("SELECT id FROM korsik.org_units WHERE code=%s", (site.upper(),)).fetchone()
        if not unit:
            raise ValueError("Unknown site; run seed")
        conn.execute("SELECT korsik.verify_registration(%s,%s,%s,%s,%s,'Europe/Moscow')",
                     (employee["hr_id"], employee_id, unit["id"], job_title, start_date))
        version = conn.execute("""SELECT v.id FROM korsik.plan_versions v JOIN korsik.plan_templates t ON t.id=v.template_id
            WHERE t.code='mvp_demo' AND v.published_at IS NOT NULL ORDER BY v.version_no DESC LIMIT 1""").fetchone()["id"]
        plan_id = conn.execute("SELECT korsik.assign_plan(%s,%s,%s) AS id", (employee["hr_id"], employee_id, version)).fetchone()["id"]
        conn.execute("""INSERT INTO korsik_router.profiles(employee_id,site_code) VALUES(%s,%s)
            ON CONFLICT(employee_id) DO UPDATE SET site_code=excluded.site_code,revision=korsik_router.profiles.revision+1""", (employee_id, site))
        rows = conn.execute("SELECT id,kind,body,code FROM korsik.plan_events WHERE plan_id=%s AND kind<>'info'", (plan_id,)).fetchall()
        for row in rows:
            config = QuestionSettings(kind=row["kind"], survey_id=f"demo:{plan_id}", prompt=row["body"])
            if row["code"] == "retention":
                config = QuestionSettings(kind="choice", survey_id=f"demo:{plan_id}", prompt=row["body"], options=[
                    Choice(code="stay", label="Да, планирую"),
                    Choice(code="leave", label="Нет, задумываюсь об уходе", comment_required=True),
                ])
            conn.execute("INSERT INTO korsik_router.question_configs(event_id,config) VALUES(%s,%s)", (row["id"], Jsonb(config.model_dump(mode="json"))))
    return plan_id


async def max_command(settings, command):
    async with MaxClient(settings) as client:
        if command == "check-bot":
            result = await client.me()
            # Whitelist fields: do not print an arbitrary provider response.
            return {k: result.get(k) for k in ("user_id", "username", "first_name", "is_bot")}
        if command == "subscribe":
            from app.webhook_manager import register
            return await register(settings)
        result = await client.subscriptions()
        return {"subscriptions": [{k: s.get(k) for k in ("url", "update_types", "time")} for s in result.get("subscriptions", [])]}


def main():
    parser = argparse.ArgumentParser(description="Корсик: установка и управление ботом MAX")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("migrate", "seed", "check-bot", "subscribe", "subscriptions", "status"):
        commands.add_parser(name)
    invitation = commands.add_parser("invite")
    invitation.add_argument("--hr-id", type=int)
    invitation.add_argument("--site", choices=["GrPP", "GTP", "Luzhniki"])
    approval = commands.add_parser("approve")
    approval.add_argument("employee_id", type=int)
    approval.add_argument("--site", choices=["GrPP", "GTP", "Luzhniki"], required=True)
    approval.add_argument("--job-title", required=True)
    approval.add_argument("--start-date", type=date.fromisoformat, required=True)
    document = commands.add_parser("import-document")
    document.add_argument("path", type=Path)
    document.add_argument("--title")
    document.add_argument("--code")
    document.add_argument("--org-unit-id", type=int)
    bundle = commands.add_parser("import-materials")
    bundle.add_argument("path", type=Path)
    reply = commands.add_parser("hr-reply")
    reply.add_argument("case_id", type=int)
    reply.add_argument("--hr-id", type=int, required=True)
    reply.add_argument("--text-file", type=Path, required=True)
    args, settings = parser.parse_args(), get_settings()
    try:
        if args.command in {"check-bot", "subscribe", "subscriptions"}:
            result = asyncio.run(max_command(settings, args.command))
        elif args.command == "migrate":
            migrate(settings)
            result = {"status": "migrated"}
        elif args.command == "seed":
            result = {"hr_id": seed(settings), "note": "Демонстрационный план. Корпоративные материалы не добавляются автоматически."}
        elif args.command == "invite":
            result = invite(settings, args.hr_id, args.site)
        elif args.command == "approve":
            result = {"plan_id": approve(settings, args.employee_id, args.site, args.job_title, args.start_date)}
        elif args.command == "import-document":
            from app.rag import import_text
            result = {"version_id": import_text(admin_repository(settings), args.path, title=args.title, code=args.code, org_unit_id=args.org_unit_id)}
        elif args.command == "import-materials":
            from app.materials import import_bundle
            result = {"documents": import_bundle(admin_repository(settings), args.path)}
        elif args.command == "hr-reply":
            with admin_repository(settings).connection() as conn:
                row = conn.execute("SELECT korsik.prepare_hr_reply(%s,%s,%s) AS message_id",
                    (args.hr_id, args.case_id, args.text_file.read_text(encoding="utf-8-sig"))).fetchone()
            result = dict(row)
        else:
            repository = PostgresRepository(settings.database_url)
            with repository.connection() as conn:
                result = {table: conn.execute(sql.SQL("SELECT status,count(*) AS count FROM {} GROUP BY status ORDER BY status").format(sql.Identifier(*table.split(".")))).fetchall()
                          for table in ("korsik.max_inbox", "korsik.outbox", "korsik_router.transport_outbox", "korsik_router.hr_jobs")}
        print(json.dumps(result, ensure_ascii=False, default=str, indent=2))
    except (MaxRejected, MaxUncertain) as exc:
        parser.exit(1, f"MAX API: {exc}\n")
    except (ValueError, OSError, psycopg.Error) as exc:
        # Database errors can include DSNs, employee text or secrets: print a safe code.
        parser.exit(1, f"Command failed: {type(exc).__name__}. Check settings, service availability and command prerequisites.\n")


if __name__ == "__main__":
    main()
