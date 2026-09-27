"""Final rendered-candidate review and signoff evidence binding."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .artifacts import BTPError, atomic_write_json, load_json, sha256_file, validate_schema
from .exceptions import approved_exception_ids
from .state import now_utc


REQUIRED_REVIEW_CHECKS = (
    "cover",
    "toc",
    "chapter_openings",
    "running_heads_feet",
    "figures",
    "dense_tables",
    "narrow_mobile",
)


def create_review_template(project: Path) -> dict[str, Any]:
    candidate_path = project / "qa" / "typeset-preview.json"
    if not candidate_path.is_file():
        raise BTPError("Create a typeset candidate before publication review")
    candidate = load_json(candidate_path)
    value = {
        "schema_version": 1,
        "candidate_sha256": sha256_file(candidate_path),
        "clean_master_sha256": candidate["clean_master_sha256"],
        "reviewer": "",
        "reviewed_at": now_utc(),
        "checks": [
            {"id": check_id, "status": "approved", "notes": "", "evidence": []}
            for check_id in REQUIRED_REVIEW_CHECKS
        ],
    }
    atomic_write_json(project / "qa" / "publication-review-template.json", value)
    return value


def record_publication_review(project: Path, source: Path) -> dict[str, Any]:
    value = load_json(source)
    validate_schema(value, "publication-review.schema.json")
    candidate_path = project / "qa" / "typeset-preview.json"
    if value["candidate_sha256"] != sha256_file(candidate_path):
        raise BTPError("Publication review does not match the current typeset candidate")
    candidate = load_json(candidate_path)
    if value["clean_master_sha256"] != candidate.get("clean_master_sha256"):
        raise BTPError("Publication review clean-master hash does not match the candidate")
    check_ids = [item["id"] for item in value["checks"]]
    if set(check_ids) != set(REQUIRED_REVIEW_CHECKS) or len(check_ids) != len(REQUIRED_REVIEW_CHECKS):
        raise BTPError("Publication review must contain each required check exactly once")
    for item in value["checks"]:
        if item["status"] == "exception":
            exception_id = item.get("exception_id", "")
            if exception_id not in approved_exception_ids(project):
                raise BTPError(f"Publication review references an unapproved exception: {exception_id}")
    destination = project / "qa" / "publication-review.json"
    atomic_write_json(destination, value)
    return {"recorded": "publication-review", "path": "qa/publication-review.json"}


def signoff_evidence(project: Path) -> tuple[dict[str, str], list[str]]:
    candidate_path = project / "qa" / "typeset-preview.json"
    review_path = project / "qa" / "publication-review.json"
    exceptions_path = project / "qa" / "exceptions.json"
    if not candidate_path.is_file() or not review_path.is_file():
        raise BTPError("Publication signoff requires the current candidate and final rendered review")
    review = load_json(review_path)
    validate_schema(review, "publication-review.schema.json")
    if review["candidate_sha256"] != sha256_file(candidate_path):
        raise BTPError("Publication review is stale for the current candidate")
    used = sorted({
        item["exception_id"] for item in review["checks"] if item["status"] == "exception"
    })
    approved = set(approved_exception_ids(project))
    if not set(used).issubset(approved):
        raise BTPError("Publication review contains expired or unapproved exceptions")
    evidence = {
        "typeset_candidate": sha256_file(candidate_path),
        "publication_review": sha256_file(review_path),
    }
    if exceptions_path.is_file():
        evidence["qa_exceptions"] = sha256_file(exceptions_path)
    candidate = load_json(candidate_path)
    evidence["clean_master"] = candidate["clean_master_sha256"]
    for name, item in candidate.get("generated", {}).items():
        evidence[f"candidate_{name}"] = item["sha256"]
    return evidence, used


def verify_signoff_evidence(project: Path, review: dict[str, Any]) -> list[str]:
    try:
        current, used = signoff_evidence(project)
    except BTPError as exc:
        return [str(exc)]
    errors = []
    if review.get("evidence_hashes", {}) != current:
        errors.append("Publication signoff evidence hashes do not match the current candidate")
    if sorted(review.get("exception_ids", [])) != used:
        errors.append("Publication signoff exception IDs do not match the current review")
    return errors
