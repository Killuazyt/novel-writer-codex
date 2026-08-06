#!/usr/bin/env python3
"""Validate the frozen upstream lock and first-commit repository hygiene."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable


ALLOWED_TOP_LEVEL = {
    ".codex-plugin",
    ".coveragerc",
    ".gitattributes",
    ".gitignore",
    "AGENTS.md",
    "CHANGELOG.md",
    "LICENSE",
    "README.md",
    "UPSTREAM.md",
    "adapters",
    "dashboard",
    "docs",
    "evals",
    "hooks",
    "pytest.ini",
    "references",
    "requirements.txt",
    "scripts",
    "sitecustomize.py",
    "templates",
    "upstream-lock.json",
}
FORBIDDEN_TOP_LEVEL = {".claude-plugin", "agents", "skills"}
FORBIDDEN_PARTS = {
    ".story-system",
    ".webnovel",
    ".tmp",
    ".pytest_cache",
    "__pycache__",
    "htmlcov",
    "node_modules",
}
FORBIDDEN_EXACT_PATHS = {
    ".github/workflows/plugin-release.yml",
    ".github/workflows/plugin-version.yml",
}
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz", ".7z", ".rar")
TEXT_SUFFIXES = {
    ".cfg",
    ".css",
    ".csv",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SENSITIVE_FILENAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
}
SECRET_PATTERNS = (
    ("private_key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("aws_access_key", re.compile(rb"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(rb"gh[pousr]_[A-Za-z0-9]{36,255}")),
    ("openai_token", re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{40,}")),
)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _issue(code: str, path: str, message: str, repair: str) -> dict[str, str]:
    return {"code": code, "path": path, "message": message, "repair": repair}


def _candidate_paths(root: Path) -> list[Path]:
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode == 0:
        return [
            root / os.fsdecode(raw)
            for raw in proc.stdout.split(b"\0")
            if raw
        ]
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    ]


def _validate_candidate(
    root: Path,
    path: Path,
    errors: list[dict[str, str]],
) -> None:
    relative = path.relative_to(root)
    display = relative.as_posix()
    parts = relative.parts
    if not parts:
        return
    if parts[0] not in ALLOWED_TOP_LEVEL:
        errors.append(
            _issue(
                "archive_path_not_allowlisted",
                display,
                f"top-level path is outside the repository allowlist: {parts[0]}",
                "Remove the path or explicitly review and add its top-level owner to the allowlist.",
            )
        )
    if parts[0] in FORBIDDEN_TOP_LEVEL or any(part in FORBIDDEN_PARTS for part in parts):
        errors.append(
            _issue(
                "forbidden_runtime_or_host_path",
                display,
                "Claude-host or generated runtime data is present in the commit candidate set.",
                "Remove the path from the repository candidate set; preserve user runtime data outside the repository.",
            )
        )
    if display in FORBIDDEN_EXACT_PATHS:
        errors.append(
            _issue(
                "legacy_release_workflow_present",
                display,
                "A frozen Claude release workflow is present.",
                "Keep the old workflow excluded; add Codex release automation only in its planned milestone.",
            )
        )
    lowered = display.lower()
    if lowered.endswith(ARCHIVE_SUFFIXES):
        errors.append(
            _issue(
                "archive_binary_present",
                display,
                "Archive files are not allowed in the source baseline.",
                "Remove the archive and keep its reviewed source files instead.",
            )
        )
    if path.name.lower() in SENSITIVE_FILENAMES or (
        path.name.lower().startswith(".env.") and path.name.lower() != ".env.example"
    ):
        errors.append(
            _issue(
                "sensitive_filename_present",
                display,
                "A sensitive configuration filename is present.",
                "Remove the local secret file and provide a value-free .env.example if documentation is needed.",
            )
        )

    try:
        content = path.read_bytes()
    except OSError as exc:
        errors.append(
            _issue(
                "candidate_read_failed",
                display,
                f"Cannot read repository candidate: {exc}",
                "Repair file permissions or remove the unreadable candidate.",
            )
        )
        return

    for secret_type, pattern in SECRET_PATTERNS:
        if pattern.search(content):
            errors.append(
                _issue(
                    "high_confidence_secret_detected",
                    display,
                    f"High-confidence {secret_type} material detected; value suppressed.",
                    "Remove and rotate the credential before continuing.",
                )
            )
    if path.suffix.lower() in TEXT_SUFFIXES or path.name in {".coveragerc", ".gitattributes", ".gitignore"}:
        # Reference CSV files intentionally use UTF-8-SIG for spreadsheet
        # compatibility; source/config text must remain UTF-8 without BOM.
        if content.startswith(b"\xef\xbb\xbf") and path.suffix.lower() != ".csv":
            errors.append(
                _issue(
                    "utf8_bom_present",
                    display,
                    "Text file contains a UTF-8 BOM.",
                    "Rewrite the file as UTF-8 without BOM.",
                )
            )
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            errors.append(
                _issue(
                    "text_not_utf8",
                    display,
                    "Text file is not valid UTF-8.",
                    "Rewrite the file with explicit UTF-8 encoding.",
                )
            )


def _load_lock(lock_path: Path) -> dict[str, Any]:
    payload = json.loads(lock_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("upstream-lock.json must contain a JSON object")
    return payload


def _validate_lock(
    root: Path,
    upstream_root: Path | None,
    errors: list[dict[str, str]],
) -> None:
    lock_path = root / "upstream-lock.json"
    try:
        lock = _load_lock(lock_path)
        import_spec = lock["import"]
        hashes = lock["hashes"]
        upstream = lock["upstream"]
        records = hashes["files"]
        if not isinstance(records, dict):
            raise ValueError("hashes.files must be an object")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        errors.append(
            _issue(
                "upstream_lock_invalid",
                "upstream-lock.json",
                f"Cannot load the frozen upstream contract: {exc}",
                "Regenerate the lock from the reviewed upstream source and rerun validation.",
            )
        )
        return

    expected_count = hashes.get("file_count")
    if expected_count != len(records):
        errors.append(
            _issue(
                "upstream_lock_count_mismatch",
                "upstream-lock.json",
                f"file_count={expected_count!r}, but hashes.files contains {len(records)} entries.",
                "Regenerate the complete per-file hash map.",
            )
        )
    paths = list(records)
    sorted_paths = sorted(paths, key=lambda item: item.encode("utf-8"))
    if paths != sorted_paths:
        errors.append(
            _issue(
                "upstream_lock_order_invalid",
                "upstream-lock.json",
                "hashes.files is not sorted by UTF-8 path bytes.",
                "Regenerate the lock with deterministic UTF-8 byte ordering.",
            )
        )
    invalid_hash_paths = [path for path, digest in records.items() if not SHA256_RE.fullmatch(str(digest))]
    if invalid_hash_paths:
        errors.append(
            _issue(
                "upstream_lock_hash_invalid",
                invalid_hash_paths[0],
                f"{len(invalid_hash_paths)} file hash entries are not lowercase SHA-256.",
                "Regenerate the lock from source bytes.",
            )
        )
    aggregate_payload = "".join(
        f"{path}\0{digest}\n" for path, digest in records.items()
    ).encode("utf-8")
    aggregate = hashlib.sha256(aggregate_payload).hexdigest()
    if aggregate != hashes.get("aggregate"):
        errors.append(
            _issue(
                "upstream_lock_aggregate_mismatch",
                "upstream-lock.json",
                "The deterministic aggregate does not match hashes.aggregate.",
                "Regenerate the complete lock without truncating or reordering entries.",
            )
        )

    if upstream_root is None:
        return
    source_root = upstream_root / str(import_spec.get("source_subdirectory") or "")
    version_source = upstream_root / str(upstream.get("version_source") or "")
    for relative, expected in records.items():
        source = source_root / relative
        try:
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
        except OSError as exc:
            errors.append(
                _issue(
                    "upstream_source_read_failed",
                    relative,
                    f"Cannot read frozen upstream source: {exc}",
                    "Point --upstream-root at the frozen repository checkout.",
                )
            )
            continue
        if actual != expected:
            errors.append(
                _issue(
                    "upstream_source_hash_mismatch",
                    relative,
                    "Frozen upstream source differs from its recorded hash.",
                    "Use the locked commit or deliberately regenerate the lock after review.",
                )
            )
    try:
        version_hash = hashlib.sha256(version_source.read_bytes()).hexdigest()
    except OSError as exc:
        errors.append(
            _issue(
                "upstream_version_source_read_failed",
                str(upstream.get("version_source") or ""),
                f"Cannot read the upstream version source: {exc}",
                "Point --upstream-root at the frozen repository checkout.",
            )
        )
    else:
        if version_hash != upstream.get("version_source_sha256"):
            errors.append(
                _issue(
                    "upstream_version_source_hash_mismatch",
                    str(upstream.get("version_source") or ""),
                    "Upstream version manifest differs from the recorded hash.",
                    "Use the locked commit or deliberately regenerate the lock after review.",
                )
            )


def validate(root: Path, upstream_root: Path | None = None) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for path in _candidate_paths(root):
        _validate_candidate(root, path, errors)
    _validate_lock(root, upstream_root, errors)
    return errors


def _print_report(root: Path, errors: Iterable[dict[str, str]], output_format: str) -> None:
    issues = list(errors)
    report = {
        "schema_version": "repository-hygiene-validator/v1",
        "ok": not issues,
        "error_count": len(issues),
        "root": str(root),
        "errors": issues,
    }
    if output_format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    if not issues:
        print("OK repository hygiene")
        return
    print("ERROR repository hygiene")
    for issue in issues:
        print(f"- [{issue['code']}] {issue['path']}: {issue['message']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--upstream-root", type=Path)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()
    root = args.root.resolve()
    upstream_root = args.upstream_root.resolve() if args.upstream_root else None
    try:
        errors = validate(root, upstream_root)
        _print_report(root, errors, args.format)
        return 0 if not errors else 1
    except Exception as exc:
        issue = _issue(
            "validator_internal_error",
            str(root),
            f"Repository hygiene validator failed internally: {type(exc).__name__}: {exc}",
            "Inspect the validator implementation or filesystem, then rerun.",
        )
        _print_report(root, [issue], args.format)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
