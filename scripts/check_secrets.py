from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_TRACKED_PATHS = [
    re.compile(r"(^|/)secrets/(?!\.gitignore$).+"),
    re.compile(r"(^|/)data/.+"),
    re.compile(r"(^|/)\.env(\..+)?$"),
    re.compile(r".*\.(sqlite3|sqlite|db|pem|key|crt|p12|pfx|mobileconfig)$", re.IGNORECASE),
]

SECRET_PATTERNS = {
    "telegram_bot_token": re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{30,}\b"),
    "openai_token": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "private_key_block": re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    "xui_panel_path": re.compile(r"/[A-Za-z0-9]{16,}/"),
}

BINARY_SUFFIXES = {
    ".db",
    ".ico",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".pyc",
    ".sqlite",
    ".sqlite3",
    ".tar",
    ".tgz",
    ".webp",
    ".zip",
}

IGNORED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "data",
    "secrets",
    "venv",
}

NON_SECRET_VALUES = {
    "/",
    "chrome",
    "data/bot.sqlite3",
    "false",
    "true",
    "xtls-rprx-vision",
}

SENSITIVE_CONFIG_KEYS = {
    "admin_ids",
    "base_url",
    "bot_token",
    "card_number",
    "password",
    "phone",
    "receiver_name",
    "username",
}


def _git_files(*args: str) -> list[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    return [item.decode() for item in result.stdout.split(b"\0") if item]


def _candidate_files() -> list[Path]:
    try:
        paths = _git_files("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    except (subprocess.CalledProcessError, FileNotFoundError):
        paths = [
            str(path.relative_to(ROOT))
            for path in ROOT.rglob("*")
            if path.is_file() and not any(part in IGNORED_DIRS for part in path.relative_to(ROOT).parts)
        ]

    candidates: list[Path] = []
    for raw_path in paths:
        path = ROOT / raw_path
        if not path.is_file():
            continue
        relative_parts = path.relative_to(ROOT).parts
        if any(part in IGNORED_DIRS for part in relative_parts):
            continue
        if path.suffix.lower() in BINARY_SUFFIXES:
            continue
        candidates.append(path)
    return candidates


def _looks_like_payment_card(match: str) -> bool:
    digits = "".join(ch for ch in match if ch.isdigit())
    if len(digits) < 13 or len(digits) > 19:
        return False
    if len(set(digits)) == 1:
        return False
    return True


def _local_secret_values() -> set[str]:
    secret_file = ROOT / "secrets" / "runtime.toml"
    if not secret_file.exists():
        return set()

    text = secret_file.read_text(errors="ignore")
    values: set[str] = set()
    for match in re.finditer(r"^\s*([A-Za-z0-9_]+)\s*=\s*['\"]([^'\"]{6,})['\"]", text, re.MULTILINE):
        key = match.group(1)
        value = match.group(2).strip()
        if key not in SENSITIVE_CONFIG_KEYS:
            continue
        if value.lower() not in NON_SECRET_VALUES:
            values.add(value)
    for match in re.finditer(r"^\s*([A-Za-z0-9_]+)\s*=\s*(\[[^\]]+\]|\d{6,})", text, re.MULTILINE):
        key = match.group(1)
        if key not in SENSITIVE_CONFIG_KEYS:
            continue
        values.update(re.findall(r"\d{6,}", match.group(2)))
    return values


def main() -> int:
    findings: list[str] = []
    tracked = _git_files("ls-files", "-z")
    local_secret_values = _local_secret_values()

    for path in tracked:
        if any(pattern.fullmatch(path) or pattern.search(path) for pattern in FORBIDDEN_TRACKED_PATHS):
            findings.append(f"forbidden tracked file: {path}")

    for path in _candidate_files():
        relative = path.relative_to(ROOT).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(errors="ignore")

        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{name}: {relative}")

        for match in re.finditer(r"\b(?:\d[ -]*?){13,19}\b", text):
            if _looks_like_payment_card(match.group(0)):
                findings.append(f"payment_card_like_number: {relative}")
                break

        for value in local_secret_values:
            if value in text:
                findings.append(f"local_runtime_secret_value: {relative}")

    if findings:
        print("Potential secrets were found:", file=sys.stderr)
        for finding in sorted(set(findings)):
            print(f"- {finding}", file=sys.stderr)
        return 1

    print("No secrets found in tracked or publishable files.")
    return 0


if __name__ == "__main__":
    os.chdir(ROOT)
    raise SystemExit(main())
