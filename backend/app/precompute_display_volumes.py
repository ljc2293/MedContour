from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path, PurePosixPath

from sqlalchemy import select

from .config import get_settings
from .database import SessionLocal
from .models import ImagingCase, ProjectCase
from .previews import (
    display_cache_path,
    ensure_display_volume,
    ensure_preview,
    generate_display_and_preview,
    preview_cache_path,
)


SEQUENCES = ("w", "f", "in", "opp")


def source_path(relative: str, data_root: Path) -> Path | None:
    relative_path = PurePosixPath(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        return None
    path = (data_root / relative_path).resolve()
    root = data_root.resolve()
    if root not in path.parents or not path.is_file():
        return None
    return path


def build_one(arguments: tuple[str, str, str, bool]) -> tuple[bool, str, int]:
    source_text, data_root_text, preview_root_text, include_preview = arguments
    source = Path(source_text)
    data_root = Path(data_root_text)
    preview_root = Path(preview_root_text)
    try:
        display = display_cache_path(source, data_root, preview_root)
        preview = preview_cache_path(source, data_root, preview_root)
        if include_preview and not display.is_file() and not preview.is_file():
            generate_display_and_preview(source, display, preview)
        else:
            if include_preview:
                ensure_preview(source, data_root, preview_root)
            display = ensure_display_volume(source, data_root, preview_root)
        return True, source.name, display.stat().st_size
    except Exception as reason:  # pragma: no cover - operational reporting
        return False, f"{source}: {reason}", 0


def main():
    parser = argparse.ArgumentParser(description="预生成 MedContour 全分辨率优化显示影像")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--include-previews", action="store_true", help="同时生成四相位快速预览")
    parser.add_argument("--reverse", action="store_true", help="从反向文件顺序生成，便于双队列从两端汇合")
    args = parser.parse_args()
    settings = get_settings()
    settings.preview_root.mkdir(parents=True, exist_ok=True)

    with SessionLocal() as db:
        cases = db.scalars(select(ImagingCase).join(ProjectCase, ProjectCase.case_id == ImagingCase.id)).all()

    sources: set[Path] = set()
    for case in cases:
        for sequence in SEQUENCES:
            relative = case.sequence_paths.get(sequence)
            if relative and (path := source_path(relative, settings.data_root)):
                sources.add(path)

    tasks = [
        (str(path), str(settings.data_root), str(settings.preview_root), args.include_previews)
        for path in sorted(sources, reverse=args.reverse)
    ]
    completed = 0
    generated_bytes = 0
    failed: list[str] = []
    with ProcessPoolExecutor(max_workers=max(1, args.workers)) as executor:
        for success, detail, size in executor.map(build_one, tasks, chunksize=2):
            completed += 1
            generated_bytes += size
            if not success:
                failed.append(detail)
            if completed % 25 == 0 or completed == len(tasks):
                print(
                    f"DISPLAY_PROGRESS {completed}/{len(tasks)} failed={len(failed)} cache_gb={generated_bytes / 1024**3:.2f}",
                    flush=True,
                )

    if failed:
        print("DISPLAY_FAILURES", flush=True)
        for detail in failed[:20]:
            print(detail, flush=True)
        raise SystemExit(1)
    print(f"DISPLAY_COMPLETE total={completed} cache_gb={generated_bytes / 1024**3:.2f}", flush=True)


if __name__ == "__main__":
    main()
