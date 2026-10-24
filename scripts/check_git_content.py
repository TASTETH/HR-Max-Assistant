"""Check the staged Git snapshot without printing credentials or changing files."""
from __future__ import annotations

import base64
import io
import json
import re
import subprocess
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_MARKER = re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----")
GITHUB_TOKEN = re.compile(rb"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}\b")
CLOUDFLARE_TOKEN = re.compile(rb"\beyJ[A-Za-z0-9_+/=-]{80,}")


def local_secrets():
    path = ROOT / ".env"
    if not path.exists():
        return []
    result = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        key, separator, value = line.partition("=")
        value = value.strip().strip("\"'")
        if separator and key.endswith(("_TOKEN", "_SECRET", "_PASSWORD")) and len(value) >= 12:
            result.append(value.encode())
    return result


def content_problem(body, secrets):
    if any(secret in body for secret in secrets):
        return "contains a credential from local .env"
    if PRIVATE_MARKER.search(body) or GITHUB_TOKEN.search(body):
        return "contains a private key or access token"
    for match in CLOUDFLARE_TOKEN.finditer(body):
        try:
            candidate = match[0]
            data = json.loads(base64.b64decode(candidate + b"=" * (-len(candidate) % 4)))
            if isinstance(data, dict) and {"a", "t", "s"} <= data.keys():
                return "contains a Cloudflare tunnel token"
        except (ValueError, UnicodeError):
            continue
    return None


def main():
    files = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0")
    secrets, problems, checked = local_secrets(), [], 0
    for name in filter(None, files):
        path = PurePosixPath(name)
        if (path.name == ".env" or path.name.startswith(".env.") and not path.name.endswith(".example")
                or path.parts[0] in {"data", "materials", "outputs", ".venv", "venv", "env"}
                or "__pycache__" in path.parts or path.suffix in {".pyc", ".log", ".pid", ".key", ".p12", ".pfx"}):
            problems.append((name, "local runtime file is tracked"))
            continue
        body = subprocess.check_output(["git", "show", ":" + name], cwd=ROOT)
        checked += 1
        reason = content_problem(body, secrets)
        if reason:
            problems.append((name, reason))
        if name.endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(body)) as archive:
                for entry in archive.infolist():
                    if not entry.is_dir() and entry.file_size <= 5_000_000:
                        reason = content_problem(archive.read(entry), secrets)
                        if reason:
                            problems.append((name + "::" + entry.filename, reason))
    if problems:
        for name, reason in problems:
            print(f"BLOCKED: {name}: {reason}")
        return 1
    print(f"Git snapshot checked: {checked} files; local runtime data and known credentials excluded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
