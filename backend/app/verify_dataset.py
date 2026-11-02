"""Verify the copied Dixon dataset against its conversion manifest."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path, PurePosixPath

from .config import get_settings


def safe_path(root: Path, raw: str) -> Path:
    relative = PurePosixPath(raw.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe path in manifest: {raw}")
    return root / relative


def verify() -> dict:
    settings = get_settings()
    manifest = settings.manifests_root / "conversion_manifest.csv"
    if not manifest.is_file():
        raise FileNotFoundError(manifest)

    result = {
        "converted_rows": 0,
        "nifti_ok": 0,
        "json_ok": 0,
        "missing": 0,
        "size_mismatch": 0,
        "roles": Counter(),
        "examples": [],
    }

    def check(path: Path, expected_size: str, kind: str):
        if not path.is_file():
            result["missing"] += 1
            if len(result["examples"]) < 10:
                result["examples"].append({"kind": kind, "issue": "missing", "path": str(path.relative_to(settings.data_root))})
            return
        if expected_size and path.stat().st_size != int(expected_size):
            result["size_mismatch"] += 1
            if len(result["examples"]) < 10:
                result["examples"].append({"kind": kind, "issue": "size", "path": str(path.relative_to(settings.data_root))})
            return
        result[f"{kind}_ok"] += 1

    with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("status") != "converted":
                continue
            result["converted_rows"] += 1
            result["roles"][row.get("target_role", "unknown")] += 1
            check(safe_path(settings.data_root, row["nifti_relative_path"]), row.get("nifti_bytes", ""), "nifti")
            check(safe_path(settings.data_root, row["json_relative_path"]), row.get("json_bytes", ""), "json")

    result["roles"] = dict(sorted(result["roles"].items()))
    result["valid"] = result["missing"] == 0 and result["size_mismatch"] == 0
    return result


if __name__ == "__main__":
    report = verify()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["valid"] else 1)
