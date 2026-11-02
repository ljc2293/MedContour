"""Import the bundled DEMO001 mask as a draft without overwriting any work."""
import asyncio
import json
from pathlib import Path

from fastapi import UploadFile
from sqlalchemy import select

from . import main as api
from .database import SessionLocal
from .models import AnnotationVersion, CaseStatus, ImagingCase, Project, ProjectCase, User


async def seed_demo_annotation(db):
    item = db.scalar(
        select(ProjectCase).join(ImagingCase).join(Project)
        .where(ImagingCase.patient_id == "DEMO001", ImagingCase.source_key == "DEMO001",
               Project.workflow_type == "segmentation")
        .with_for_update()
    )
    if item is None or item.case.sequence_paths.get("w") != "DEMO001/W.nii.gz":
        raise RuntimeError("Bundled DEMO001 case has not been imported")
    if item.latest_version or db.scalar(select(AnnotationVersion.id).where(AnnotationVersion.project_case_id == item.id)):
        return {"demo_id": "DEMO001", "skipped": True, "reason": "existing annotation preserved"}
    if item.status not in {CaseStatus.UNASSIGNED, CaseStatus.ASSIGNED}:
        raise RuntimeError("DEMO001 is already in progress; no changes were made")
    author = db.scalar(select(User).where(User.email == api.settings.initial_admin_email.strip().lower()))
    if author is None:
        raise RuntimeError("Initial administrator does not exist; start the API first")
    mask_path = api.settings.data_root / Path("DEMO001/pancreas_mask.nii.gz")
    with mask_path.open("rb") as handle:
        upload = UploadFile(file=handle, filename="pancreas_mask.nii.gz")
        version = await api.persist_annotation_version(db, item, upload, author,
            "Bundled DEMO001 manual reference imported as a demo draft; not reviewed in this deployment.")
    item.status = CaseStatus.ANNOTATING
    api.audit(db, author, "demo.annotation_import", "project_case", item.id,
              {"demo_id": "DEMO001", "version": version.version, "sha256": version.sha256})
    db.commit()
    return {"demo_id": "DEMO001", "version": version.version, "state": "draft", "skipped": False}


if __name__ == "__main__":
    with SessionLocal() as session:
        print(json.dumps(asyncio.run(seed_demo_annotation(session))))
