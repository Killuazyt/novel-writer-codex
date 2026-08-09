#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Deterministic, read-only validation for staged volume plans.

The validator intentionally does not infer chronology or semantic hand-offs
from free prose.  Those facts must be represented in the structured manifest,
bound to every staged artifact by a content hash, and echoed in the authored
Markdown.  Validation never writes project facts or runtime receipts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any, Mapping


MANIFEST_SCHEMA_VERSION = "webnovel-plan-manifest/v1"
VALIDATION_SCHEMA_VERSION = "webnovel-plan-validation/v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?$")
_WINDOWS_RESERVED_RUN_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}
_PLACEHOLDER_RE = re.compile(
    r"(?:\[\s*(?:TODO|TBD|待补[^\]]*)\s*\]|\{\s*(?:占位|待补[^}]*)\s*\}|\bBLOCKER\b)",
    re.IGNORECASE,
)
_CONTENT_MARKER_RE = re.compile(
    r"<!--\s*webnovel-plan-content-sha256:\s*([0-9a-f]{64})\s*-->",
    re.IGNORECASE,
)
_MAX_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024

REQUIRED_ARTIFACTS = ("beat", "timeline", "outline", "writeback")


class PlanValidationError(ValueError):
    """The staged plan cannot be promoted."""


def _safe_run_id(value: Any) -> bool:
    if not isinstance(value, str) or not _RUN_ID_RE.fullmatch(value):
        return False
    return value.split(".", 1)[0].upper() not in _WINDOWS_RESERVED_RUN_NAMES


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def file_sha256(path: str | Path) -> str:
    candidate = Path(path)
    return sha256_bytes(
        _stable_read_bytes(candidate, trusted_root=candidate.parent, max_bytes=64 * 1024 * 1024)
    )


def plan_content_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "volume": manifest.get("volume"),
        "chapter_range": manifest.get("chapter_range"),
        "beat": manifest.get("beat"),
        "chapters": manifest.get("chapters"),
    }


def compute_plan_content_sha256(manifest: Mapping[str, Any]) -> str:
    return sha256_bytes(_canonical_bytes(plan_content_payload(manifest)))


def expected_targets(volume: int) -> dict[str, str]:
    return {
        "beat": f"大纲/第{volume}卷-节拍表.md",
        "timeline": f"大纲/第{volume}卷-时间线.md",
        "outline": f"大纲/第{volume}卷-详细大纲.md",
        "writeback": f"大纲/第{volume}卷-总纲写回.json",
    }


def _is_reparse_point(path: Path) -> bool:
    try:
        attrs = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return False
    return path.is_symlink() or bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _lexical_parts_are_safe(root: Path, relative: Path) -> bool:
    if relative.is_absolute() or ".." in relative.parts:
        return False
    current = root
    for part in relative.parts:
        current = current / part
        if (current.exists() or current.is_symlink()) and _is_reparse_point(current):
            return False
    return True


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _absolute_lexical(path: str | Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _stable_read_bytes(path: Path, *, trusted_root: Path, max_bytes: int) -> bytes:
    root = _absolute_lexical(trusted_root)
    lexical = _absolute_lexical(path)
    try:
        relative = lexical.relative_to(root)
    except ValueError as exc:
        raise PlanValidationError(f"artifact escapes trusted root: {lexical}") from exc
    if not _lexical_parts_are_safe(root, relative):
        raise PlanValidationError(f"reparse-point artifact path is forbidden: {lexical}")
    if not lexical.is_file():
        raise PlanValidationError(f"artifact is missing: {lexical}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lexical, flags)
    except OSError as exc:
        raise PlanValidationError(f"artifact cannot be safely opened: {lexical}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
            raise PlanValidationError(f"artifact is not regular or is too large: {lexical}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
        after = os.fstat(fd)
        path_after = lexical.stat()
        before_id = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_id = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        path_id = (path_after.st_dev, path_after.st_ino, path_after.st_size, path_after.st_mtime_ns)
        if not _lexical_parts_are_safe(root, relative):
            raise PlanValidationError(f"reparse-point artifact path is forbidden: {lexical}")
        if len(raw) > max_bytes or len(raw) != before.st_size or before_id != after_id or before_id != path_id:
            raise PlanValidationError(f"artifact changed during bounded read: {lexical}")
        return raw
    except OSError as exc:
        raise PlanValidationError(f"artifact cannot be safely read: {lexical}: {exc}") from exc
    finally:
        os.close(fd)


def _read_utf8(
    path: Path,
    *,
    max_bytes: int,
    trusted_root: Path | None = None,
) -> tuple[bytes, str]:
    raw = _stable_read_bytes(
        path,
        trusted_root=trusted_root or path.parent,
        max_bytes=max_bytes,
    )
    if raw.startswith(b"\xef\xbb\xbf"):
        raise PlanValidationError(f"UTF-8 BOM is forbidden: {path}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PlanValidationError(f"artifact is not valid UTF-8: {path}") from exc
    if not text.strip():
        raise PlanValidationError(f"artifact is empty: {path}")
    return raw, text


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _render_node(node: Mapping[str, Any]) -> str:
    return " | ".join(str(node.get(key) or "").strip() for key in ("subject", "action", "result"))


def _validate_node(
    node: Any,
    *,
    field: str,
    chapter: int,
    problems: list[dict[str, Any]],
) -> None:
    if not isinstance(node, Mapping):
        problems.append({"code": "invalid_node", "chapter": chapter, "field": field})
        return
    for key in ("subject", "action", "result"):
        if not _nonempty(node.get(key)):
            problems.append(
                {"code": "invalid_node", "chapter": chapter, "field": f"{field}.{key}"}
            )
    handoff = node.get("handoff_id", "")
    if handoff is not None and not isinstance(handoff, str):
        problems.append(
            {"code": "invalid_handoff", "chapter": chapter, "field": f"{field}.handoff_id"}
        )


def _validate_beat(beat: Any, problems: list[dict[str, Any]]) -> None:
    if not isinstance(beat, Mapping):
        problems.append({"code": "invalid_beat", "detail": "beat must be an object"})
        return
    crises = beat.get("crises")
    if not isinstance(crises, list) or len(crises) < 3:
        problems.append({"code": "insufficient_crises", "detail": "at least three crises are required"})
    else:
        for index, crisis in enumerate(crises):
            if isinstance(crisis, Mapping):
                valid = all(_nonempty(crisis.get(key)) for key in ("conflict", "cost", "result"))
            else:
                valid = _nonempty(crisis)
            if not valid:
                problems.append({"code": "invalid_crisis", "index": index})
    midpoint = beat.get("midpoint")
    if not isinstance(midpoint, Mapping) or not (
        _nonempty(midpoint.get("event")) or _nonempty(midpoint.get("reason_if_none"))
    ):
        problems.append(
            {"code": "midpoint_missing", "detail": "provide a midpoint event or an explicit no-midpoint reason"}
        )
    if not _nonempty(beat.get("final_open_question")):
        problems.append({"code": "final_hook_missing"})


def _validate_chapters(
    manifest: Mapping[str, Any],
    *,
    outline_text: str,
    timeline_text: str,
    problems: list[dict[str, Any]],
) -> None:
    chapter_range = manifest.get("chapter_range")
    if (
        not isinstance(chapter_range, list)
        or len(chapter_range) != 2
        or not all(_positive_int(item) for item in chapter_range)
        or chapter_range[0] > chapter_range[1]
    ):
        problems.append({"code": "invalid_chapter_range"})
        return
    start, end = chapter_range
    chapters = manifest.get("chapters")
    if not isinstance(chapters, list):
        problems.append({"code": "chapters_missing"})
        return
    expected = list(range(start, end + 1))
    actual = [item.get("chapter") if isinstance(item, Mapping) else None for item in chapters]
    if actual != expected:
        problems.append(
            {"code": "chapter_coverage_mismatch", "expected": expected, "actual": actual}
        )
        return

    previous: Mapping[str, Any] | None = None
    countdown_previous: dict[str, tuple[int, int]] = {}
    for item in chapters:
        chapter = int(item["chapter"])
        if not _nonempty(item.get("goal")):
            problems.append({"code": "chapter_goal_missing", "chapter": chapter})
        offset = item.get("time_offset_minutes")
        span = item.get("span_minutes")
        if not _nonnegative_int(offset):
            problems.append({"code": "invalid_time_offset", "chapter": chapter})
        if not _positive_int(span):
            problems.append({"code": "invalid_time_span", "chapter": chapter})
        if not _nonempty(item.get("transition")):
            problems.append({"code": "transition_missing", "chapter": chapter})
        mode = item.get("time_mode", "linear")
        if mode not in {"linear", "flashback"}:
            problems.append({"code": "invalid_time_mode", "chapter": chapter})
        if mode == "flashback" and not _nonempty(item.get("flashback_note")):
            problems.append({"code": "flashback_unmarked", "chapter": chapter})

        _validate_node(item.get("cbn"), field="cbn", chapter=chapter, problems=problems)
        cpns = item.get("cpns")
        if not isinstance(cpns, list) or not 2 <= len(cpns) <= 4:
            problems.append({"code": "invalid_cpn_count", "chapter": chapter})
            cpns = []
        for index, node in enumerate(cpns):
            _validate_node(node, field=f"cpns[{index}]", chapter=chapter, problems=problems)
        _validate_node(item.get("cen"), field="cen", chapter=chapter, problems=problems)

        must_cover = item.get("must_cover_nodes")
        forbidden = item.get("forbidden_zones")
        if (
            not isinstance(must_cover, list)
            or len(must_cover) > 4
            or not all(_nonempty(value) for value in must_cover)
        ):
            problems.append({"code": "invalid_must_cover", "chapter": chapter})
        if (
            not isinstance(forbidden, list)
            or len(forbidden) > 5
            or not all(_nonempty(value) for value in forbidden)
        ):
            problems.append({"code": "invalid_forbidden_zones", "chapter": chapter})
        if not _nonempty(item.get("chapter_end_open_question")):
            problems.append({"code": "chapter_hook_missing", "chapter": chapter})

        countdowns = item.get("countdowns", {})
        if not isinstance(countdowns, Mapping):
            problems.append({"code": "invalid_countdowns", "chapter": chapter})
            countdowns = {}
        for event, remaining in countdowns.items():
            if not _nonempty(event) or not _nonnegative_int(remaining):
                problems.append({"code": "invalid_countdown", "chapter": chapter, "event": event})
                continue
            if event in countdown_previous and _nonnegative_int(offset):
                prior_offset, prior_remaining = countdown_previous[event]
                expected_remaining = max(0, prior_remaining - (offset - prior_offset))
                if remaining != expected_remaining:
                    problems.append(
                        {
                            "code": "countdown_mismatch",
                            "chapter": chapter,
                            "event": event,
                            "expected": expected_remaining,
                            "actual": remaining,
                        }
                    )
            if _nonnegative_int(offset):
                countdown_previous[str(event)] = (offset, remaining)

        if previous is not None:
            previous_offset = previous.get("time_offset_minutes")
            if _nonnegative_int(previous_offset) and _nonnegative_int(offset) and offset < previous_offset:
                problems.append({"code": "timeline_not_monotonic", "chapter": chapter})
            previous_cen = previous.get("cen") if isinstance(previous.get("cen"), Mapping) else {}
            current_cbn = item.get("cbn") if isinstance(item.get("cbn"), Mapping) else {}
            previous_handoff = str(previous_cen.get("handoff_id") or "").strip()
            current_handoff = str(current_cbn.get("handoff_id") or "").strip()
            if not previous_handoff or previous_handoff != current_handoff:
                problems.append(
                    {
                        "code": "cen_cbn_handoff_mismatch",
                        "chapter": chapter,
                        "expected": previous_handoff,
                        "actual": current_handoff,
                    }
                )
        previous = item

        # Cross-check the human-authored artifacts without pretending to infer
        # semantics from prose.
        required_outline_tokens = [
            f"第{chapter}章",
            str(item.get("goal") or "").strip(),
            _render_node(item.get("cbn") or {}),
            *[_render_node(node) for node in cpns if isinstance(node, Mapping)],
            _render_node(item.get("cen") or {}),
            str(item.get("chapter_end_open_question") or "").strip(),
        ]
        for token in [value for value in required_outline_tokens if value]:
            if token not in outline_text:
                problems.append(
                    {"code": "outline_manifest_mismatch", "chapter": chapter, "token": token}
                )
        if f"第{chapter}章" not in timeline_text:
            problems.append({"code": "timeline_chapter_missing", "chapter": chapter})
        if _nonnegative_int(offset) and f"T+{offset}m" not in timeline_text:
            problems.append({"code": "timeline_offset_missing", "chapter": chapter})
        for event, remaining in countdowns.items():
            if f"CD:{event}={remaining}m" not in timeline_text:
                problems.append(
                    {"code": "timeline_countdown_missing", "chapter": chapter, "event": event}
                )

    beat = manifest.get("beat")
    if chapters and isinstance(beat, Mapping):
        final_question = chapters[-1].get("chapter_end_open_question")
        if beat.get("final_open_question") != final_question:
            problems.append({"code": "final_hook_mismatch"})


def _artifact_paths(
    root: Path,
    manifest: Mapping[str, Any],
    *,
    problems: list[dict[str, Any]],
) -> tuple[dict[str, Path], dict[str, str], dict[str, str]]:
    run_id = manifest.get("run_id")
    volume = manifest.get("volume")
    artifacts = manifest.get("artifacts")
    paths: dict[str, Path] = {}
    texts: dict[str, str] = {}
    hashes: dict[str, str] = {}
    if not isinstance(artifacts, Mapping) or set(artifacts) != set(REQUIRED_ARTIFACTS):
        problems.append({"code": "artifact_set_mismatch"})
        return paths, texts, hashes
    if not _safe_run_id(run_id):
        problems.append({"code": "invalid_run_id"})
        return paths, texts, hashes
    if not _positive_int(volume):
        problems.append({"code": "invalid_volume"})
        return paths, texts, hashes

    staging_root = _absolute_lexical(root / ".webnovel" / "tmp" / "plan-runs" / run_id)
    targets = expected_targets(volume)
    for name in REQUIRED_ARTIFACTS:
        spec = artifacts.get(name)
        if not isinstance(spec, Mapping):
            problems.append({"code": "invalid_artifact", "artifact": name})
            continue
        if set(spec) != {"path", "target", "sha256"}:
            problems.append({"code": "invalid_artifact_shape", "artifact": name})
            continue
        raw_path = Path(str(spec.get("path") or ""))
        raw_target = Path(str(spec.get("target") or ""))
        digest = str(spec.get("sha256") or "")
        if not _lexical_parts_are_safe(root, raw_path):
            problems.append({"code": "artifact_path_out_of_bounds", "artifact": name})
            continue
        if not _lexical_parts_are_safe(root, raw_target):
            problems.append({"code": "artifact_target_out_of_bounds", "artifact": name})
            continue
        path = _absolute_lexical(root / raw_path)
        target = _absolute_lexical(root / raw_target)
        expected_target = _absolute_lexical(root / targets[name])
        if not _inside(path, staging_root):
            problems.append({"code": "artifact_path_out_of_bounds", "artifact": name})
            continue
        if target != expected_target:
            problems.append(
                {
                    "code": "artifact_target_mismatch",
                    "artifact": name,
                    "expected": targets[name],
                }
            )
            continue
        if not path.is_file():
            problems.append({"code": "artifact_missing", "artifact": name, "path": str(path)})
            continue
        if not _SHA256_RE.fullmatch(digest):
            problems.append({"code": "artifact_hash_invalid", "artifact": name})
            continue
        try:
            raw, text = _read_utf8(path, max_bytes=_MAX_ARTIFACT_BYTES, trusted_root=root)
        except (OSError, PlanValidationError) as exc:
            problems.append({"code": "artifact_read_failed", "artifact": name, "detail": str(exc)})
            continue
        actual = sha256_bytes(raw)
        if actual != digest:
            problems.append({"code": "artifact_hash_mismatch", "artifact": name})
            continue
        if _PLACEHOLDER_RE.search(text):
            problems.append({"code": "artifact_placeholder", "artifact": name})
        paths[name] = path
        texts[name] = text
        hashes[name] = actual
    return paths, texts, hashes


def validate_plan_manifest(
    project_root: str | Path,
    manifest_path: str | Path,
) -> dict[str, Any]:
    """Validate one staged manifest without writing any file."""

    root = Path(project_root).resolve()
    problems: list[dict[str, Any]] = []
    if not root.is_dir():
        return {
            "schema_version": VALIDATION_SCHEMA_VERSION,
            "ok": False,
            "status": "blocked",
            "problems": [{"code": "project_root_missing", "path": str(root)}],
        }
    supplied_manifest = Path(manifest_path)
    manifest_file = _absolute_lexical(
        supplied_manifest if supplied_manifest.is_absolute() else root / supplied_manifest
    )
    expected_plan_runs = _absolute_lexical(root / ".webnovel" / "tmp" / "plan-runs")
    try:
        manifest_relative = manifest_file.relative_to(root)
    except ValueError:
        manifest_relative = Path("..")
    if (
        not _inside(manifest_file, expected_plan_runs)
        or not _lexical_parts_are_safe(root, manifest_relative)
        or not manifest_file.is_file()
    ):
        return {
            "schema_version": VALIDATION_SCHEMA_VERSION,
            "ok": False,
            "status": "blocked",
            "project_root": str(root),
            "manifest_path": str(manifest_file),
            "problems": [{"code": "manifest_path_invalid"}],
        }
    try:
        raw, _ = _read_utf8(
            manifest_file,
            max_bytes=_MAX_MANIFEST_BYTES,
            trusted_root=root,
        )
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, PlanValidationError, json.JSONDecodeError) as exc:
        return {
            "schema_version": VALIDATION_SCHEMA_VERSION,
            "ok": False,
            "status": "blocked",
            "project_root": str(root),
            "manifest_path": str(manifest_file),
            "problems": [{"code": "manifest_invalid", "detail": str(exc)}],
        }
    if not isinstance(manifest, Mapping):
        problems.append({"code": "manifest_invalid", "detail": "manifest must be an object"})
        manifest = {}
    run_id = manifest.get("run_id")
    if _safe_run_id(run_id):
        expected_manifest = _absolute_lexical(
            root / ".webnovel" / "tmp" / "plan-runs" / run_id / "plan-manifest.json"
        )
        if manifest_file != expected_manifest:
            problems.append(
                {
                    "code": "manifest_path_mismatch",
                    "expected": str(expected_manifest),
                }
            )
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        problems.append({"code": "manifest_schema_invalid"})
    if manifest.get("executor") != "parent" or manifest.get("invoked_agents") != []:
        problems.append({"code": "parent_only_violation"})
    if not _nonempty(manifest.get("parent_model")):
        problems.append({"code": "parent_model_missing"})
    blockers = manifest.get("blockers")
    if blockers != []:
        problems.append({"code": "unresolved_blockers", "count": len(blockers) if isinstance(blockers, list) else -1})

    content_sha = compute_plan_content_sha256(manifest)
    if manifest.get("content_sha256") != content_sha:
        problems.append({"code": "content_hash_mismatch"})
    _validate_beat(manifest.get("beat"), problems)
    _, texts, hashes = _artifact_paths(root, manifest, problems=problems)

    for name in ("beat", "timeline", "outline"):
        text = texts.get(name, "")
        marker = _CONTENT_MARKER_RE.search(text)
        if text and (marker is None or marker.group(1).lower() != content_sha):
            problems.append({"code": "content_marker_mismatch", "artifact": name})
    writeback_text = texts.get("writeback")
    if writeback_text:
        try:
            writeback = json.loads(writeback_text)
        except json.JSONDecodeError:
            problems.append({"code": "writeback_json_invalid"})
        else:
            if not isinstance(writeback, Mapping) or writeback.get("plan_content_sha256") != content_sha:
                problems.append({"code": "writeback_content_hash_mismatch"})

    beat_text = texts.get("beat", "")
    beat = manifest.get("beat") if isinstance(manifest.get("beat"), Mapping) else {}
    if beat_text:
        for crisis in beat.get("crises") or []:
            values = crisis.values() if isinstance(crisis, Mapping) else [crisis]
            for value in values:
                if _nonempty(value) and str(value).strip() not in beat_text:
                    problems.append({"code": "beat_manifest_mismatch", "token": str(value).strip()})
        midpoint = beat.get("midpoint") if isinstance(beat.get("midpoint"), Mapping) else {}
        midpoint_token = midpoint.get("event") or midpoint.get("reason_if_none")
        if _nonempty(midpoint_token) and str(midpoint_token).strip() not in beat_text:
            problems.append({"code": "beat_manifest_mismatch", "token": str(midpoint_token).strip()})
        final_question = beat.get("final_open_question")
        if _nonempty(final_question) and str(final_question).strip() not in beat_text:
            problems.append({"code": "beat_manifest_mismatch", "token": str(final_question).strip()})

    _validate_chapters(
        manifest,
        outline_text=texts.get("outline", ""),
        timeline_text=texts.get("timeline", ""),
        problems=problems,
    )
    return {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "ok": not problems,
        "status": "validated" if not problems else "blocked",
        "project_root": str(root),
        "manifest_path": str(manifest_file),
        "manifest_sha256": sha256_bytes(raw),
        "run_id": manifest.get("run_id"),
        "volume": manifest.get("volume"),
        "content_sha256": content_sha,
        "artifact_hashes": hashes,
        "problems": problems,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a staged volume plan without writing facts")
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args()
    report = validate_plan_manifest(args.project_root, args.manifest)
    if args.format == "json":
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"status: {report['status']}")
        for item in report.get("problems") or []:
            print(f"- {item.get('code')}: {item.get('detail', '')}")
    raise SystemExit(0 if report["ok"] else 2)


if __name__ == "__main__":
    main()
