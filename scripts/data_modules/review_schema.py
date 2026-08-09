#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Review payload validation and normalized review artifacts.

``parse_review_output(..., strict=True)`` is the production trust boundary for
the Codex reviewer.  The default non-strict mode is retained only for upstream
Python callers that construct legacy v6 metrics in-process; the Review Skill
never uses that compatibility path.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

try:
    from security_utils import atomic_write_json
except ImportError:
    from scripts.security_utils import atomic_write_json


STRICT_DIMENSIONS = ("setting", "timeline", "continuity", "character", "logic")
STRICT_CATEGORIES = frozenset(STRICT_DIMENSIONS)
VALID_SEVERITIES = {"critical", "high", "medium", "low"}
# Legacy categories remain available to in-process write-chain helpers.  They
# are never accepted from the managed Codex reviewer.
VALID_CATEGORIES = STRICT_CATEGORIES | {"ai_flavor", "pacing", "other"}
SCORE_CATEGORIES = (*STRICT_DIMENSIONS, "ai_flavor", "pacing", "other")
SEVERITY_PENALTIES = {
    "critical": 35.0,
    "high": 15.0,
    "medium": 6.0,
    "low": 2.0,
}
REVIEW_MODES = {"full", "fast"}
MAX_ISSUES = 200
MAX_REVIEW_JSON_BYTES = 512 * 1024
MAX_TEXT = {
    "location": 512,
    "description": 4096,
    "evidence": 4096,
    "fix_hint": 4096,
    "conclusion": 2048,
    "summary": 4096,
}


class ReviewSchemaError(ValueError):
    """Raised when reviewer JSON violates the exact production contract."""


def _clamp_score(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 2)


def _issue_penalty(issue: "ReviewIssue") -> float:
    return float(SEVERITY_PENALTIES.get(issue.severity, SEVERITY_PENALTIES["medium"]))


@dataclass
class ReviewIssue:
    severity: str
    category: str = "other"
    location: str = ""
    description: str = ""
    evidence: str = ""
    fix_hint: str = ""
    blocking: Optional[bool] = None

    def __post_init__(self) -> None:
        # Compatibility for trusted, in-process legacy callers.  Production
        # reviewer input is validated before construction and never normalized.
        if self.severity not in VALID_SEVERITIES:
            self.severity = "medium"
        if self.category not in VALID_CATEGORIES:
            self.category = "other"
        if self.blocking is None:
            self.blocking = self.severity == "critical"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DimensionResult:
    dimension: str
    conclusion: str

    def to_dict(self) -> Dict[str, str]:
        return {"dimension": self.dimension, "conclusion": self.conclusion}


@dataclass
class ReviewResult:
    chapter: int
    issues: List[ReviewIssue] = field(default_factory=list)
    dimension_results: List[DimensionResult] = field(default_factory=list)
    summary: str = ""

    @property
    def issues_count(self) -> int:
        return len(self.issues)

    @property
    def blocking_count(self) -> int:
        return sum(1 for issue in self.issues if issue.blocking is True)

    @property
    def has_blocking(self) -> bool:
        return self.blocking_count > 0

    @property
    def severity_counts(self) -> Dict[str, int]:
        counts = {level: 0 for level in ("critical", "high", "medium", "low")}
        for issue in self.issues:
            severity = issue.severity if issue.severity in counts else "medium"
            counts[severity] += 1
        return counts

    @property
    def categories(self) -> List[str]:
        return sorted(set(issue.category for issue in self.issues))

    @property
    def critical_issues(self) -> List[str]:
        return [
            issue.description
            for issue in self.issues
            if issue.severity == "critical" and issue.description
        ]

    def _build_dimension_scores(self) -> Dict[str, float]:
        scores = {category: 100.0 for category in SCORE_CATEGORIES}
        for issue in self.issues:
            category = issue.category if issue.category in scores else "other"
            scores[category] = _clamp_score(scores[category] - _issue_penalty(issue))
        return scores

    def _build_notes(self, categories: List[str], provenance: Mapping[str, Any] | None) -> str:
        parts: List[str] = []
        if self.summary:
            parts.append(self.summary)
        parts.append(f"issues={self.issues_count}")
        parts.append(f"blocking={self.blocking_count}")
        if categories:
            parts.append("categories=" + ",".join(categories))
        if provenance:
            for name in ("run_id", "review_sha256", "chapter_sha256"):
                value = str(provenance.get(name) or "").strip()
                if value:
                    parts.append(f"{name}={value}")
        return " | ".join(parts)

    def _calculate_overall_score(self) -> float:
        score = 100.0
        for issue in self.issues:
            score -= _issue_penalty(issue)
        return _clamp_score(score)

    def to_dict(self) -> Dict[str, Any]:
        dimensions = self.dimension_results or [
            DimensionResult(dimension=name, conclusion="pass")
            for name in STRICT_DIMENSIONS
        ]
        return {
            "chapter": self.chapter,
            "issues": [issue.to_dict() for issue in self.issues],
            "issues_count": self.issues_count,
            "blocking_count": self.blocking_count,
            "has_blocking": self.has_blocking,
            "dimension_results": [item.to_dict() for item in dimensions],
            "summary": self.summary,
        }

    def to_metrics_dict(
        self,
        report_file: str = "",
        *,
        provenance: Mapping[str, Any] | None = None,
    ) -> Dict[str, Any]:
        categories = self.categories
        severity_counts = self.severity_counts
        payload: Dict[str, Any] = {
            "chapter": self.chapter,
            "start_chapter": self.chapter,
            "end_chapter": self.chapter,
            "overall_score": self._calculate_overall_score(),
            "dimension_scores": self._build_dimension_scores(),
            "severity_counts": severity_counts,
            "critical_issues": self.critical_issues,
            "report_file": report_file,
            "notes": self._build_notes(categories, provenance),
            "issues_count": self.issues_count,
            "blocking_count": self.blocking_count,
            "categories": categories,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if provenance:
            payload["provenance"] = dict(provenance)
        return payload


def _strict_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ReviewSchemaError(f"{field_name} must be a string")
    if not value.strip():
        raise ReviewSchemaError(f"{field_name} must not be empty")
    if "\x00" in value or len(value) > MAX_TEXT[field_name.rsplit(".", 1)[-1]]:
        raise ReviewSchemaError(f"{field_name} is too long or contains NUL")
    return value.strip()


def _parse_strict_review_output(
    chapter: int,
    raw: Mapping[str, Any],
    *,
    review_mode: str,
) -> ReviewResult:
    if type(chapter) is not int or chapter <= 0:
        raise ReviewSchemaError("chapter must be a positive integer")
    if review_mode not in REVIEW_MODES:
        raise ReviewSchemaError("review_mode must be full or fast")
    try:
        encoded = json.dumps(raw, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReviewSchemaError("review output must be JSON serializable") from exc
    if len(encoded) > MAX_REVIEW_JSON_BYTES:
        raise ReviewSchemaError("review output exceeds the bounded JSON size")

    required_top = {
        "chapter",
        "issues",
        "issues_count",
        "blocking_count",
        "has_blocking",
        "dimension_results",
        "summary",
    }
    if set(raw) != required_top:
        raise ReviewSchemaError("review output must contain exactly the seven contract fields")
    if type(raw.get("chapter")) is not int or raw.get("chapter") != chapter:
        raise ReviewSchemaError("review chapter does not match the prepared request")

    raw_issues = raw.get("issues")
    if not isinstance(raw_issues, list) or len(raw_issues) > MAX_ISSUES:
        raise ReviewSchemaError(f"issues must be a list with at most {MAX_ISSUES} entries")
    issue_fields = {
        "severity",
        "category",
        "location",
        "description",
        "evidence",
        "fix_hint",
        "blocking",
    }
    issues: List[ReviewIssue] = []
    for index, item in enumerate(raw_issues):
        if not isinstance(item, Mapping) or set(item) != issue_fields:
            raise ReviewSchemaError(f"issues[{index}] has an invalid field set")
        severity = item.get("severity")
        category = item.get("category")
        blocking = item.get("blocking")
        if severity not in VALID_SEVERITIES:
            raise ReviewSchemaError(f"issues[{index}].severity is invalid")
        if category not in STRICT_CATEGORIES:
            raise ReviewSchemaError(f"issues[{index}].category is invalid")
        if type(blocking) is not bool:
            raise ReviewSchemaError(f"issues[{index}].blocking must be boolean")
        if severity == "critical" and blocking is not True:
            raise ReviewSchemaError(f"issues[{index}] critical issues must block")
        if review_mode == "fast" and category in {"character", "logic"}:
            raise ReviewSchemaError("fast mode cannot report issues in skipped dimensions")
        issues.append(
            ReviewIssue(
                severity=str(severity),
                category=str(category),
                location=_strict_text(item.get("location"), "location"),
                description=_strict_text(item.get("description"), "description"),
                evidence=_strict_text(item.get("evidence"), "evidence"),
                fix_hint=_strict_text(item.get("fix_hint"), "fix_hint"),
                blocking=blocking,
            )
        )

    raw_dimensions = raw.get("dimension_results")
    if not isinstance(raw_dimensions, list) or len(raw_dimensions) != len(STRICT_DIMENSIONS):
        raise ReviewSchemaError("dimension_results must contain exactly five entries")
    dimensions: List[DimensionResult] = []
    issue_counts = {name: 0 for name in STRICT_DIMENSIONS}
    for issue in issues:
        issue_counts[issue.category] += 1
    for index, (expected, item) in enumerate(zip(STRICT_DIMENSIONS, raw_dimensions, strict=True)):
        if not isinstance(item, Mapping) or set(item) != {"dimension", "conclusion"}:
            raise ReviewSchemaError(f"dimension_results[{index}] has an invalid field set")
        if item.get("dimension") != expected:
            raise ReviewSchemaError("dimension_results are missing or out of order")
        conclusion = _strict_text(item.get("conclusion"), "conclusion")
        if review_mode == "fast" and expected in {"character", "logic"}:
            if conclusion != "skipped: fast mode":
                raise ReviewSchemaError("fast mode skipped conclusions must be exact")
        else:
            if conclusion.startswith("skipped"):
                raise ReviewSchemaError("required review dimensions cannot be skipped")
            if issue_counts[expected] == 0 and conclusion != "pass":
                raise ReviewSchemaError(f"{expected} must conclude pass when it has no issues")
            if issue_counts[expected] > 0 and conclusion == "pass":
                raise ReviewSchemaError(f"{expected} cannot pass while issues are reported")
        dimensions.append(DimensionResult(dimension=expected, conclusion=conclusion))

    blocking_count = sum(1 for issue in issues if issue.blocking is True)
    if type(raw.get("issues_count")) is not int or raw.get("issues_count") != len(issues):
        raise ReviewSchemaError("issues_count does not match issues")
    if type(raw.get("blocking_count")) is not int or raw.get("blocking_count") != blocking_count:
        raise ReviewSchemaError("blocking_count does not match issues")
    if type(raw.get("has_blocking")) is not bool or raw.get("has_blocking") is not bool(blocking_count):
        raise ReviewSchemaError("has_blocking does not match blocking_count")
    summary = _strict_text(raw.get("summary"), "summary")
    return ReviewResult(
        chapter=chapter,
        issues=issues,
        dimension_results=dimensions,
        summary=summary,
    )


def parse_review_output(
    chapter: int,
    raw: Dict[str, Any],
    *,
    review_mode: str = "full",
    strict: bool = False,
) -> ReviewResult:
    """Parse reviewer output.

    Production callers must pass ``strict=True``.  The compatibility path is
    intentionally tolerant only for older trusted Python integrations.
    """

    if strict:
        if not isinstance(raw, Mapping):
            raise ReviewSchemaError("review output must be a JSON object")
        return _parse_strict_review_output(chapter, raw, review_mode=review_mode)

    issues: List[ReviewIssue] = []
    for item in raw.get("issues", []):
        if not isinstance(item, dict):
            continue
        issues.append(
            ReviewIssue(
                severity=str(item.get("severity", "medium")),
                category=str(item.get("category", "other")),
                location=str(item.get("location", "")),
                description=str(item.get("description", "")),
                evidence=str(item.get("evidence", "")),
                fix_hint=str(item.get("fix_hint", "")),
                blocking=item.get("blocking"),
            )
        )
    dimensions: List[DimensionResult] = []
    for item in raw.get("dimension_results", []):
        if isinstance(item, Mapping):
            dimensions.append(
                DimensionResult(
                    dimension=str(item.get("dimension") or ""),
                    conclusion=str(item.get("conclusion") or ""),
                )
            )
    return ReviewResult(
        chapter=chapter,
        issues=issues,
        dimension_results=dimensions,
        summary=str(raw.get("summary", "")),
    )


def _read_json_if_exists(path: Path) -> Any | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Bad JSON in {path}") from exc


def _write_json(path: Path, payload: Any) -> None:
    atomic_write_json(path, payload, backup=True)


def append_ai_flavor_anti_patterns(project_root: str | Path, result: ReviewResult) -> int:
    """Legacy write-chain helper; standalone strict review never calls it."""

    root = Path(project_root).expanduser().resolve()
    path = root / ".story-system" / "anti_patterns.json"
    existing = _read_json_if_exists(path) or []
    if not isinstance(existing, list):
        existing = []

    seen_texts = {
        str(item.get("text") or "").strip()
        for item in existing
        if isinstance(item, dict)
    }
    additions: List[Dict[str, Any]] = []
    for index, issue in enumerate(result.issues, start=1):
        if issue.category != "ai_flavor" or issue.severity not in {"medium", "high", "critical"}:
            continue
        text = (issue.evidence or issue.description or "").strip()[:200]
        if not text or text in seen_texts:
            continue
        seen_texts.add(text)
        additions.append(
            {
                "text": text,
                "source_table": "review_extracted",
                "source_id": f"ch{int(result.chapter):04d}_issue_{index}",
                "category": issue.category,
                "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )

    if additions:
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_json(path, [*existing, *additions])
    return len(additions)
