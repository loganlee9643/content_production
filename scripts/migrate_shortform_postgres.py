"""Back up or restore the shortform PostgreSQL database."""

import argparse
from pathlib import Path
import shutil
import subprocess
import sys


def database_name(value):
    if not value or "=" in value or value.startswith(("postgres://", "postgresql://")):
        raise argparse.ArgumentTypeError("--database accepts a database name, not a connection string")
    return value


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("backup", "restore"))
    parser.add_argument("archive", type=Path, help="PostgreSQL custom-format backup file")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=5432)
    parser.add_argument("--database", required=True, type=database_name)
    parser.add_argument("--user", required=True)
    parser.add_argument("--bin-dir", type=Path, help="Folder containing pg_dump and pg_restore")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def client_path(name, bin_dir):
    path = shutil.which(name, path=str(bin_dir.resolve()) if bin_dir else None)
    if path is None:
        raise FileNotFoundError(f"{name} was not found. Install PostgreSQL client tools or set --bin-dir.")
    return path


def backup(command, archive):
    # Exclusive creation prevents replacing an existing backup.
    stream = archive.open("xb")
    try:
        with stream:
            subprocess.run([*command, "--format=custom"], stdout=stream, check=True)
    except (OSError, subprocess.CalledProcessError, KeyboardInterrupt):
        archive.unlink(missing_ok=True)
        raise


def main():
    args = parse_args()
    archive = args.archive.resolve()
    try:
        client = client_path("pg_dump" if args.operation == "backup" else "pg_restore", args.bin_dir)
        command = [
            client, "--host", args.host, "--port", str(args.port),
            "--username", args.user, "--dbname", args.database,
        ]
        if args.operation == "backup":
            backup(command, archive)
        else:
            if not archive.is_file():
                raise FileNotFoundError(f"Backup file not found: {archive}")
            subprocess.run([
                *command, "--single-transaction", "--exit-on-error", "--no-owner",
                "--no-privileges", str(archive),
            ], check=True)
    except subprocess.CalledProcessError as exc:
        print(f"{args.operation} failed (PostgreSQL exit code {exc.returncode}).", file=sys.stderr)
        return 1
    except OSError as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print(f"{args.operation} complete: {archive}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
