from __future__ import annotations

import csv
import hashlib
import re
from collections import defaultdict
from pathlib import Path, PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .models import CaseStatus, ImagingCase, Project, ProjectCase
from .pancreas_reference import resolve_pancreas_reference


REQUIRED_ROLES = {"opp", "in", "f", "w"}
REPEAT_STUDY_REASON = re.compile(r"^原始病例目录数=\d+$")
SEGMENTATION_PROJECT_NAME = "Dixon 四序列器官分割"
PDFF_PROJECT_NAME = "Dixon PDFF 三点 ROI 勾画"


def _read_csv(path: Path):
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def _safe_relative_path(raw: str) -> str:
    path = PurePosixPath(raw.replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"不安全的数据路径: {raw}")
    return path.as_posix()


def identity_review_required(identity: dict) -> bool:
    flagged = (identity.get("需人工复核") or "").strip().lower() in {"1", "true", "yes", "是"}
    if not flagged:
        return False
    reason = (identity.get("复核原因") or "").strip()
    mapping_status = (identity.get("映射状态") or "").strip()
    source_status = (identity.get("源身份状态") or "").strip().lower()
    repeat_study_only = bool(REPEAT_STUDY_REASON.fullmatch(reason))
    unique_mapping = mapping_status in {"唯一", "唯一匹配"}
    if repeat_study_only and unique_mapping and source_status == "ok":
        return False
    return True


def _has_unique_pdff_reference(item: ImagingCase, settings: Settings) -> bool:
    raw_path = item.sequence_paths.get("w", "")
    if not raw_path:
        return False
    try:
        relative_path = PurePosixPath(_safe_relative_path(raw_path))
        reference_path = (settings.data_root / relative_path).resolve()
        data_root = settings.data_root.resolve()
    except (OSError, ValueError):
        return False
    if data_root not in reference_path.parents or not reference_path.is_file():
        return False
    return resolve_pancreas_reference(
        settings.pancreas_prediction_root,
        item.patient_id,
        reference_path,
    ).available


def sync_pdff_project_cases(db: Session, settings: Settings) -> int:
    """Publish only source cases with one geometry-verified pancreas reference."""
    source_project = db.scalar(select(Project).where(Project.name == SEGMENTATION_PROJECT_NAME))
    pdff_project = db.scalar(select(Project).where(Project.name == PDFF_PROJECT_NAME))
    if source_project is None or pdff_project is None:
        return 0

    created = 0
    source_tasks = db.scalars(select(ProjectCase).where(ProjectCase.project_id == source_project.id)).all()
    existing_case_ids = set(
        db.scalars(select(ProjectCase.case_id).where(ProjectCase.project_id == pdff_project.id)).all()
    )
    for source_task in source_tasks:
        if source_task.case_id in existing_case_ids:
            continue
        if not _has_unique_pdff_reference(source_task.case, settings):
            continue
        db.add(
            ProjectCase(
                project_id=pdff_project.id,
                case_id=source_task.case_id,
                status=CaseStatus.UNASSIGNED,
                annotator_id=None,
                reviewer_id=None,
            )
        )
        existing_case_ids.add(source_task.case_id)
        created += 1
    return created


def import_dataset(db: Session, settings: Settings, only_available: bool = True) -> dict[str, int]:
    mapping_path = settings.manifests_root / "patient_name_zh_patient_id_mapping.csv"
    conversion_path = settings.manifests_root / "conversion_manifest.csv"
    quartet_path = settings.manifests_root / "source_quartet_manifest.csv"
    for required in (mapping_path, conversion_path):
        if not required.exists():
            raise FileNotFoundError(f"缺少清单文件: {required}")

    identity_map: dict[str, dict] = {}
    for row in _read_csv(mapping_path):
        identity_map[row.get("PatientID", "").strip()] = row

    quartet_meta: dict[str, dict] = {}
    if quartet_path.exists():
        for row in _read_csv(quartet_path):
            key = _safe_relative_path(row.get("output_acquisition_directory", "").strip())
            quartet_meta[key] = row

    grouped: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in _read_csv(conversion_path):
        if row.get("status") != "converted":
            continue
        role = row.get("target_role", "").lower().strip()
        relative_path = _safe_relative_path(row.get("nifti_relative_path", "").strip())
        parent = PurePosixPath(relative_path).parent.as_posix()
        grouped[parent][role] = {**row, "nifti_relative_path": relative_path}

    project = db.scalar(select(Project).where(Project.name == SEGMENTATION_PROJECT_NAME))
    if project is None:
        raise RuntimeError("默认项目尚未创建")

    result = {"groups": len(grouped), "created": 0, "updated": 0, "unavailable": 0, "skipped": 0, "identity_review": 0, "repeat_study_resolved": 0, "pdff_tasks_created": 0}
    for source_key, role_rows in grouped.items():
        if set(role_rows) != REQUIRED_ROLES:
            result["skipped"] += 1
            continue

        sequence_paths = {role: role_rows[role]["nifti_relative_path"] for role in sorted(REQUIRED_ROLES)}
        available = all((settings.data_root / PurePosixPath(relative)).is_file() for relative in sequence_paths.values())
        if only_available and not available:
            result["unavailable"] += 1
            continue

        first = role_rows["w"]
        patient_id = first.get("patient_id", "").strip()
        identity = identity_map.get(patient_id, {})
        display_name = (identity.get("显示姓名") or identity.get("中文姓名") or "待人工核对").strip()
        manifest_review_flag = (identity.get("需人工复核") or "").strip().lower() in {"1", "true", "yes", "是"}
        needs_review = identity_review_required(identity)
        review_reason = (identity.get("复核原因") or "").strip()
        mapping_status = (identity.get("映射状态") or "").strip()
        if needs_review:
            result["identity_review"] += 1
        elif manifest_review_flag:
            result["repeat_study_resolved"] += 1
        meta = quartet_meta.get(source_key, {})
        case_code = "DX-" + hashlib.sha1(source_key.encode("utf-8")).hexdigest()[:10].upper()

        item = db.scalar(select(ImagingCase).where(ImagingCase.source_key == source_key))
        if item is None:
            item = ImagingCase(source_key=source_key, case_code=case_code, patient_id=patient_id, patient_name_zh=display_name, sequence_paths=sequence_paths)
            db.add(item)
            db.flush()
            result["created"] += 1
        else:
            result["updated"] += 1

        item.patient_id = patient_id
        item.patient_name_zh = display_name
        item.mapping_status = (review_reason if needs_review and review_reason else mapping_status)[:80]
        item.needs_identity_review = needs_review
        item.study_date = (meta.get("study_dates") or "").strip()
        item.protocol_family = (meta.get("normalized_family") or first.get("normalized_family") or "").strip()
        item.sequence_paths = sequence_paths
        item.is_available = available

        project_case = db.scalar(
            select(ProjectCase).where(ProjectCase.project_id == project.id, ProjectCase.case_id == item.id)
        )
        if project_case is None:
            db.add(ProjectCase(project_id=project.id, case_id=item.id))

    db.flush()
    result["pdff_tasks_created"] = sync_pdff_project_cases(db, settings)
    db.commit()
    return result
