import argparse
from datetime import datetime, timezone
import hashlib
import secrets
import sys
import time

from mysql.connector import Error, IntegrityError

import db


def format_time(value):
    if value is None:
        return "never"
    return datetime.fromtimestamp(int(value), timezone.utc).isoformat()


def issue_code(args):
    now = int(time.time())
    expires_at = None
    if not args.no_expiry:
        days = args.expires_in_days if args.expires_in_days is not None else 365
        if days < 1 or days > 3650:
            print("--expires-in-days must be between 1 and 3650.", file=sys.stderr)
            return 2
        expires_at = now + days * 24 * 60 * 60

    code = f"NCART-{secrets.token_urlsafe(32)}"
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    try:
        code_id = db.create_admin_access_code(code_hash, now, expires_at)
    except IntegrityError:
        print("Could not create the Admin Access Code. Retry the command.", file=sys.stderr)
        return 1
    except Error:
        print(
            "Database operation failed. Start or restart NCart once to apply the database migration, then retry.",
            file=sys.stderr,
        )
        return 1

    print(f"Access Code ID: {code_id}")
    print("Admin Access Code (shown only once; store it securely):")
    print(code)
    print(f"Expires: {format_time(expires_at)}")
    return 0


def list_codes(_args):
    try:
        codes = db.list_admin_access_codes()
    except Error:
        print(
            "Database operation failed. Start or restart NCart once to apply the database migration, then retry.",
            file=sys.stderr,
        )
        return 1

    now = int(time.time())
    print("ID\tSTATUS\tCREATED (UTC)\tEXPIRES (UTC)\tUSED AT (UTC)\tUSED BY")
    for code in codes:
        status = code["status"]
        if status == "active" and code["revoked_at"] is not None:
            status = "revoked"
        elif status == "active" and code["used_at"] is not None:
            status = "used"
        elif (
            status == "active"
            and code["expires_at"] is not None
            and int(code["expires_at"]) <= now
        ):
            status = "expired"
        print(
            f'{code["id"]}\t{status}\t{format_time(code["created_at"])}\t'
            f'{format_time(code["expires_at"])}\t{format_time(code["used_at"])}\t'
            f'{code["used_by"] if code["used_by"] is not None else "-"}'
        )
    return 0


def revoke_code(args):
    try:
        revoked = db.revoke_admin_access_code(args.code_id, int(time.time()))
    except Error:
        print(
            "Database operation failed. Start or restart NCart once to apply the database migration, then retry.",
            file=sys.stderr,
        )
        return 1
    if not revoked:
        print("No unused active Admin Access Code matched that ID.", file=sys.stderr)
        return 1
    print(f"Admin Access Code {args.code_id} revoked.")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Generate, list, or revoke one-time NCart Admin Access Codes."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue", help="Generate and display a new code once")
    expiration = issue.add_mutually_exclusive_group()
    expiration.add_argument(
        "--expires-in-days",
        type=int,
        help="Code lifetime in days (default: 365; maximum: 3650)",
    )
    expiration.add_argument(
        "--no-expiry",
        action="store_true",
        help="Create a code without an expiration date",
    )
    issue.set_defaults(handler=issue_code)
    list_command = commands.add_parser("list", help="List code status without revealing codes")
    list_command.set_defaults(handler=list_codes)
    revoke = commands.add_parser("revoke", help="Revoke an unused code by ID")
    revoke.add_argument("code_id", type=int)
    revoke.set_defaults(handler=revoke_code)

    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
