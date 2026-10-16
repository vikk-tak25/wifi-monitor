"""Command line: `fleet serve | migrate | hash-password | create-unit`."""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path

from .config import ConfigError, load_settings

MIGRATIONS = Path(__file__).resolve().parent / "migrations"


def migrate(database_url: str | None = None) -> None:
    from alembic import command
    from alembic.config import Config

    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    if database_url:
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    command.upgrade(config, "head")


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    settings = load_settings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if args.migrate:
        migrate(settings.database_url)
    uvicorn.run(create_app(settings), host=args.host, port=args.port, proxy_headers=False,
                log_level="info", access_log=not settings.is_production)
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    migrate(load_settings().database_url)
    print("Database is up to date.")
    return 0


def cmd_hash_password(args: argparse.Namespace) -> int:
    from .security import hash_password

    password = getpass.getpass("Admin password: ")
    if len(password) < 12:
        print("Use at least 12 characters.", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat: ") != password:
        print("Passwords do not match.", file=sys.stderr)
        return 1
    print(f"FLEET_ADMIN_PASSWORD_HASH='{hash_password(password)}'")
    return 0


def cmd_create_unit(args: argparse.Namespace) -> int:
    from .db import make_engine, utcnow
    from .schemas import UnitCreate
    from .units import create_unit

    settings = load_settings()
    engine = make_engine(settings.database_url)
    with engine.begin() as conn:
        unit, key = create_unit(conn, settings, UnitCreate(name=args.name, notes=args.notes), utcnow())
    engine.dispose()
    print(f"Unit {unit.id} ({unit.name})")
    print(f"Key (shown once): {key}")
    print(f"Server URL: {settings.public_url}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fleet", description="Wi-Fi fleet server")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8800)
    p.add_argument("--migrate", action="store_true", help="apply database migrations first")
    p.set_defaults(func=cmd_serve)

    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)
    sub.add_parser("hash-password", help="create FLEET_ADMIN_PASSWORD_HASH").set_defaults(func=cmd_hash_password)

    p = sub.add_parser("create-unit", help="register a unit and print its key")
    p.add_argument("--name", required=True, help="room name, e.g. 'B-204'")
    p.add_argument("--notes")
    p.set_defaults(func=cmd_create_unit)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
