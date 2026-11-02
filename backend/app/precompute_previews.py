from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path, PurePosixPath

from sqlalchemy import select

from .config import get_settings
from .database import SessionLocal
from .models import ImagingCase, ProjectCase
from .previews import ensure_preview


def source_path(relative: str, data_root: Path) -> Path | None:
    relative_path = PurePosixPath(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return None
    path = (data_root / relative_path).resolve()
    root = data_root.resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path


def build_one(arguments: tuple[str, str, str]) -> tuple[bool, str]:
    source, data_root, preview_root = (Path(value) for value in arguments)
    try:
        ensure_preview(source, data_root, preview_root)
        return True, source.name
    except Exception as reason:  # pragma: no cover - operational reporting
        return False, f"{source}: {reason}"


def main():
    parser = argparse.ArgumentParser(description="预生成 MedContour 水相快速预览")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()
    settings = get_settings()
    settings.preview_root.mkdir(parents=True, exist_ok=True)

    with SessionLocal() as db:
        cases = db.scalars(select(ImagingCase).join(ProjectCase, ProjectCase.case_id == ImagingCase.id)).all()

    sources: set[Path] = set()
    for case in cases:
        relative = case.sequence_paths.get("w")
        if relative and (path := source_path(relative, settings.data_root)):
            sources.add(path)

    tasks = [(str(path), str(settings.data_root), str(settings.preview_root)) for path in sorted(sources)]
    completed = 0
    failed: list[str] = []
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as executor:
        for success, detail in executor.map(build_one, tasks, chunksize=4):
            completed += 1
            if not success:
                failed.append(detail)
            if completed % 25 == 0 or completed == len(tasks):
                print(f"PREVIEW_PROGRESS {completed}/{len(tasks)} failed={len(failed)}", flush=True)

    if failed:
        print("PREVIEW_FAILURES", flush=True)
        for detail in failed[:20]:
            print(detail, flush=True)
        raise SystemExit(1)
    print(f"PREVIEW_COMPLETE total={completed}", flush=True)


if __name__ == "__main__":
    main()
