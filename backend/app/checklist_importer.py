from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ChecklistFeedbackCase, ChecklistFeedbackStatus


REQUIRED_FILES = ("case.json", "checklist.json", "review-form.json")


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取有效 JSON：{path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点必须是对象：{path}")
    return value


def _source_hash(case_payload: dict, checklist_payload: dict, review_template: dict) -> str:
    canonical = json.dumps(
        [case_payload, checklist_payload, review_template],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def import_checklist_feedback_packages(db: Session, source_root: Path) -> dict[str, int]:
    """Import source packages as independent, unassigned two-person review tasks.

    Existing assignments and all clinician-created versions are never overwritten.
    Source payloads are refreshed only while a task still has no answer or review.
    """

    root = source_root.resolve()
    index_path = root / "assignment-index.csv"
    if not root.is_dir() or not index_path.is_file():
        raise FileNotFoundError(f"Checklist 任务源目录无效：{root}")

    with index_path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError("assignment-index.csv 中没有任务")

    seen: set[str] = set()
    counts = {"scanned": 0, "created": 0, "updated": 0, "unchanged": 0}
    for row in rows:
        case_id = (row.get("case_id") or "").strip()
        package_name = (row.get("package_path") or case_id).strip()
        if not case_id or case_id in seen:
            raise ValueError(f"case_id 缺失或重复：{case_id or '<empty>'}")
        seen.add(case_id)
        package_dir = (root / package_name).resolve()
        if root not in package_dir.parents or not package_dir.is_dir():
            raise ValueError(f"任务包路径越界或不存在：{package_name}")
        missing = [name for name in REQUIRED_FILES if not (package_dir / name).is_file()]
        if missing:
            raise ValueError(f"{case_id} 缺少文件：{', '.join(missing)}")

        case_payload = _read_json(package_dir / "case.json")
        checklist_payload = _read_json(package_dir / "checklist.json")
        review_template = _read_json(package_dir / "review-form.json")
        if any(payload.get("case_id") != case_id for payload in (case_payload, checklist_payload, review_template)):
            raise ValueError(f"{case_id} 的三个 JSON 文件 case_id 不一致")
        criteria = checklist_payload.get("criteria")
        if not isinstance(criteria, list) or not criteria:
            raise ValueError(f"{case_id} 的 checklist 没有有效 criteria")
        criterion_ids = [str(item.get("criterion_id") or "") for item in criteria if isinstance(item, dict)]
        if len(criterion_ids) != len(criteria) or not all(criterion_ids) or len(set(criterion_ids)) != len(criterion_ids):
            raise ValueError(f"{case_id} 的 criterion_id 缺失或重复")

        digest = _source_hash(case_payload, checklist_payload, review_template)
        existing = db.scalar(select(ChecklistFeedbackCase).where(ChecklistFeedbackCase.id == case_id))
        values = {
            "primary_task": (row.get("primary_task") or case_payload.get("primary_task") or "").strip(),
            "target_subtype": (row.get("target_subtype") or review_template.get("target_subtype") or "").strip(),
            "screening_complexity": (
                row.get("complexity")
                or review_template.get("screening_complexity")
                or case_payload.get("metadata", {}).get("complexity")
                or "medium"
            ).strip(),
            "case_payload": case_payload,
            "checklist_payload": checklist_payload,
            "review_template": review_template,
            "source_hash": digest,
            "source_path": package_name,
        }
        counts["scanned"] += 1
        if existing is None:
            db.add(
                ChecklistFeedbackCase(
                    id=case_id,
                    status=ChecklistFeedbackStatus.UNASSIGNED,
                    answerer_id=None,
                    reviewer_id=None,
                    **values,
                )
            )
            counts["created"] += 1
            continue
        if existing.latest_answer_version or existing.latest_review_version:
            counts["unchanged"] += 1
            continue
        if existing.source_hash == digest and all(getattr(existing, key) == value for key, value in values.items()):
            counts["unchanged"] += 1
            continue
        for key, value in values.items():
            setattr(existing, key, value)
        counts["updated"] += 1

    db.flush()
    return counts
