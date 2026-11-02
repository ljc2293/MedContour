from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

import nibabel as nib
import numpy as np


ReferenceStatus = Literal["available", "missing", "ambiguous", "geometry_mismatch", "invalid"]


@dataclass(frozen=True)
class PredictionRecord:
    patient_id: str
    prediction_path: Path
    native_shape: tuple[int, int, int]
    native_affine: tuple[tuple[float, ...], ...]
    model_method_id: str


@dataclass(frozen=True)
class PancreasReferenceResolution:
    status: ReferenceStatus
    reason: str
    prediction: PredictionRecord | None = None
    slice_count: int | None = None

    @property
    def available(self) -> bool:
        return self.status == "available" and self.prediction is not None


def _normalized_patient_id(value: str) -> str:
    return value.strip().casefold()


def _prediction_root_stamp(root: Path) -> int:
    try:
        return root.stat().st_mtime_ns
    except OSError:
        return -1


@lru_cache(maxsize=4)
def _prediction_index(
    prediction_root_text: str,
    root_stamp: int,
) -> dict[str, tuple[PredictionRecord, ...]]:
    del root_stamp  # It is part of the cache key so newly added case folders refresh the index.
    root = Path(prediction_root_text)
    records: defaultdict[str, list[PredictionRecord]] = defaultdict(list)
    if not root.is_dir():
        return {}

    for metadata_path in sorted(root.glob("*/inference_metadata.json")):
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            patient_id = str(payload.get("patient_id") or "").strip()
            shape_values = tuple(int(value) for value in payload.get("native_shape", []))
            affine_values = tuple(
                tuple(float(value) for value in row)
                for row in payload.get("native_affine", [])
            )
            prediction_name = str(payload.get("prediction_filename") or "pancreas_pred.nii.gz")
            prediction_path = (metadata_path.parent / prediction_name).resolve()
            if (
                not patient_id
                or payload.get("status") != "completed"
                or len(shape_values) != 3
                or len(affine_values) != 4
                or any(len(row) != 4 for row in affine_values)
                or metadata_path.parent.resolve() not in prediction_path.parents
                or not prediction_path.is_file()
            ):
                continue
            records[_normalized_patient_id(patient_id)].append(
                PredictionRecord(
                    patient_id=patient_id,
                    prediction_path=prediction_path,
                    native_shape=shape_values,
                    native_affine=affine_values,
                    model_method_id=str(payload.get("model_method_id") or ""),
                )
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
    return {patient_id: tuple(items) for patient_id, items in records.items()}


def _same_geometry(
    shape: tuple[int, ...],
    affine: np.ndarray,
    expected_shape: tuple[int, ...],
    expected_affine: np.ndarray,
) -> bool:
    return shape == expected_shape and np.allclose(affine, expected_affine, rtol=0, atol=1e-3)


def resolve_pancreas_reference(
    prediction_root: Path,
    patient_id: str,
    reference_path: Path,
) -> PancreasReferenceResolution:
    try:
        reference_stamp = reference_path.stat().st_mtime_ns
    except OSError:
        return PancreasReferenceResolution("invalid", "水相参考影像不可读取")
    return _resolve_pancreas_reference_cached(
        str(prediction_root.resolve()),
        _prediction_root_stamp(prediction_root),
        _normalized_patient_id(patient_id),
        str(reference_path.resolve()),
        reference_stamp,
    )


@lru_cache(maxsize=4096)
def _resolve_pancreas_reference_cached(
    prediction_root_text: str,
    root_stamp: int,
    normalized_patient_id: str,
    reference_path_text: str,
    reference_stamp: int,
) -> PancreasReferenceResolution:
    del reference_stamp  # Invalidates this result when the source image is replaced.
    if not normalized_patient_id:
        return PancreasReferenceResolution("missing", "病例缺少患者 ID，无法匹配参考分割")

    candidates = _prediction_index(prediction_root_text, root_stamp).get(normalized_patient_id, ())
    if not candidates:
        return PancreasReferenceResolution("missing", "本病例没有可用的胰腺参考分割")

    try:
        reference_image = nib.load(reference_path_text)
        reference_shape = tuple(int(value) for value in reference_image.shape[:3])
        reference_affine = np.asarray(reference_image.affine, dtype=np.float64)
    except Exception:
        return PancreasReferenceResolution("invalid", "水相参考影像不是有效的 NIfTI")

    metadata_matches = [
        candidate
        for candidate in candidates
        if _same_geometry(
            candidate.native_shape,
            np.asarray(candidate.native_affine, dtype=np.float64),
            reference_shape,
            reference_affine,
        )
    ]
    if not metadata_matches:
        return PancreasReferenceResolution(
            "geometry_mismatch",
            "找到了同一患者的分割，但其检查空间与当前病例不一致",
        )

    verified: list[PredictionRecord] = []
    for candidate in metadata_matches:
        try:
            prediction_image = nib.load(candidate.prediction_path)
            prediction_shape = tuple(int(value) for value in prediction_image.shape[:3])
            prediction_affine = np.asarray(prediction_image.affine, dtype=np.float64)
        except Exception:
            continue
        if len(prediction_image.shape) == 3 and _same_geometry(
            prediction_shape,
            prediction_affine,
            reference_shape,
            reference_affine,
        ):
            verified.append(candidate)

    if not verified:
        return PancreasReferenceResolution("invalid", "匹配到的胰腺分割文件无效或几何信息不一致")
    if len(verified) > 1:
        return PancreasReferenceResolution(
            "ambiguous",
            "同一患者存在多个几何相同的分割结果，无法安全确定当前检查",
        )
    return PancreasReferenceResolution(
        "available",
        "",
        prediction=verified[0],
        slice_count=reference_shape[2],
    )


@lru_cache(maxsize=2)
def _load_binary_prediction(prediction_path_text: str, file_stamp: int) -> np.ndarray:
    del file_stamp  # Invalidates the cached array when the NIfTI file changes.
    image = nib.load(prediction_path_text)
    if len(image.shape) != 3:
        raise ValueError("胰腺参考分割必须是三维 NIfTI")
    mask = np.asanyarray(image.dataobj) > 0
    mask.setflags(write=False)
    return mask


def _edge_segments(mask_slice: np.ndarray) -> tuple[tuple[float, float, float, float], ...]:
    """Return the exposed half-voxel edges of a 2-D binary mask."""
    if mask_slice.ndim != 2 or not np.any(mask_slice):
        return ()

    left = mask_slice.copy()
    left[1:, :] &= ~mask_slice[:-1, :]
    right = mask_slice.copy()
    right[:-1, :] &= ~mask_slice[1:, :]
    lower = mask_slice.copy()
    lower[:, 1:] &= ~mask_slice[:, :-1]
    upper = mask_slice.copy()
    upper[:, :-1] &= ~mask_slice[:, 1:]

    segments: list[tuple[float, float, float, float]] = []
    for boundary, offset, vertical in (
        (left, -0.5, True),
        (right, 0.5, True),
        (lower, -0.5, False),
        (upper, 0.5, False),
    ):
        x_values, y_values = np.nonzero(boundary)
        if vertical:
            segments.extend(
                (float(x + offset), float(y - 0.5), float(x + offset), float(y + 0.5))
                for x, y in zip(x_values, y_values, strict=True)
            )
        else:
            segments.extend(
                (float(x - 0.5), float(y + offset), float(x + 0.5), float(y + offset))
                for x, y in zip(x_values, y_values, strict=True)
            )
    return tuple(segments)


@lru_cache(maxsize=512)
def _pancreas_slice_segments_cached(
    prediction_path_text: str,
    file_stamp: int,
    slice_index: int,
) -> tuple[tuple[float, float, float, float], ...]:
    mask = _load_binary_prediction(prediction_path_text, file_stamp)
    if slice_index < 0 or slice_index >= mask.shape[2]:
        raise IndexError("切片索引超出胰腺参考分割范围")
    return _edge_segments(mask[:, :, slice_index])


def pancreas_slice_segments(
    prediction_path: Path,
    slice_index: int,
) -> tuple[tuple[float, float, float, float], ...]:
    return _pancreas_slice_segments_cached(
        str(prediction_path.resolve()),
        prediction_path.stat().st_mtime_ns,
        slice_index,
    )


@lru_cache(maxsize=8)
def _pancreas_volume_segments_cached(
    prediction_path_text: str,
    file_stamp: int,
) -> tuple[tuple[int, tuple[tuple[float, float, float, float], ...]], ...]:
    """Build all non-empty axial contours once so the browser can switch locally."""
    mask = _load_binary_prediction(prediction_path_text, file_stamp)
    contours: list[tuple[int, tuple[tuple[float, float, float, float], ...]]] = []
    for slice_index in range(mask.shape[2]):
        segments = _edge_segments(mask[:, :, slice_index])
        if segments:
            contours.append((slice_index, segments))
    return tuple(contours)


def pancreas_volume_segments(
    prediction_path: Path,
) -> tuple[tuple[int, tuple[tuple[float, float, float, float], ...]], ...]:
    return _pancreas_volume_segments_cached(
        str(prediction_path.resolve()),
        prediction_path.stat().st_mtime_ns,
    )
