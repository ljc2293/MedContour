from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path
from threading import Lock

import nibabel as nib
import numpy as np


PREVIEW_VERSION = "xy2-u8-v1"
DISPLAY_VERSION = "full-u8-v1"
_preview_lock = Lock()


def preview_cache_path(source: Path, data_root: Path, preview_root: Path) -> Path:
    return _cache_path(PREVIEW_VERSION, source, data_root, preview_root)


def display_cache_path(source: Path, data_root: Path, preview_root: Path) -> Path:
    return _cache_path(DISPLAY_VERSION, source, data_root, preview_root)


def _cache_path(version: str, source: Path, data_root: Path, preview_root: Path) -> Path:
    source = source.resolve()
    relative = source.relative_to(data_root.resolve())
    stat = source.stat()
    fingerprint = f"{version}\0{relative.as_posix()}\0{stat.st_size}\0{stat.st_mtime_ns}"
    digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
    return preview_root.resolve() / digest[:2] / f"{digest}.nii.gz"


def _save_nifti_atomic(image: nib.Nifti1Image, destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.tmp.nii.gz")
    try:
        nib.save(image, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def generate_preview(source: Path, destination: Path) -> Path:
    image = nib.load(source)
    if len(image.shape) != 3:
        raise ValueError("仅支持三维 NIfTI 预览")

    sampled = np.asarray(image.dataobj[::2, ::2, :], dtype=np.float32)
    finite = np.isfinite(sampled)
    if not finite.any():
        preview = np.zeros(sampled.shape, dtype=np.uint8)
    else:
        values = sampled[finite]
        low, high = np.percentile(values, [0.5, 99.5])
        span = float(high - low)
        if not np.isfinite(span) or span <= 0:
            preview = np.zeros(sampled.shape, dtype=np.uint8)
        else:
            np.nan_to_num(sampled, copy=False, nan=float(low), posinf=float(high), neginf=float(low))
            sampled -= float(low)
            sampled /= span
            np.clip(sampled, 0, 1, out=sampled)
            sampled *= 255
            preview = np.rint(sampled).astype(np.uint8)

    affine = image.affine @ np.diag([2, 2, 1, 1])
    header = image.header.copy()
    header.set_data_dtype(np.uint8)
    header.set_slope_inter(1.0, 0.0)
    header["cal_min"] = 0
    header["cal_max"] = 255

    return _save_nifti_atomic(nib.Nifti1Image(preview, affine, header), destination)


def ensure_preview(source: Path, data_root: Path, preview_root: Path) -> Path:
    destination = preview_cache_path(source, data_root, preview_root)
    if destination.is_file():
        return destination
    with _preview_lock:
        if destination.is_file():
            return destination
        return generate_preview(source, destination)


def _encode_display_data(image: nib.spatialimages.SpatialImage) -> tuple[np.ndarray, float, float, float, float]:
    data = np.asarray(image.dataobj, dtype=np.float32)
    if not data.flags.writeable:
        data = data.copy()
    z_stride = max(1, data.shape[2] // 64)
    sample = data[::4, ::4, ::z_stride]
    finite_sample = sample[np.isfinite(sample)]
    if finite_sample.size:
        low, high = (float(value) for value in np.percentile(finite_sample, [0.2, 99.8]))
    else:
        low, high = 0.0, 0.0
    span = high - low
    if not np.isfinite(span) or span <= 0:
        encoded = np.zeros(data.shape, dtype=np.uint8)
        slope = 1.0
        intercept = low if np.isfinite(low) else 0.0
        high = intercept
    else:
        np.nan_to_num(data, copy=False, nan=low, posinf=high, neginf=low)
        data -= low
        data /= span
        np.clip(data, 0, 1, out=data)
        data *= 255
        encoded = np.rint(data).astype(np.uint8)
        slope = span / 255
        intercept = low
    return encoded, low, high, slope, intercept


def _display_image(
    encoded: np.ndarray,
    affine: np.ndarray,
    source_header: nib.nifti1.Nifti1Header,
    low: float,
    high: float,
    slope: float,
    intercept: float,
) -> nib.Nifti1Image:
    header = source_header.copy()
    header.set_data_dtype(np.uint8)
    image = nib.Nifti1Image(encoded, affine, header)
    image.header.set_slope_inter(slope, intercept)
    image.header["cal_min"] = low
    image.header["cal_max"] = high
    return image


def generate_display_volume(source: Path, destination: Path) -> Path:
    """Create a full-geometry 8-bit display proxy while preserving intensity units."""
    image = nib.load(source)
    if len(image.shape) != 3:
        raise ValueError("仅支持三维 NIfTI 显示缓存")
    encoded, low, high, slope, intercept = _encode_display_data(image)
    display_image = _display_image(encoded, image.affine, image.header, low, high, slope, intercept)
    return _save_nifti_atomic(display_image, destination)


def generate_display_and_preview(
    source: Path,
    display_destination: Path,
    preview_destination: Path,
) -> tuple[Path, Path]:
    """Generate both cache variants from one source decompression."""
    image = nib.load(source)
    if len(image.shape) != 3:
        raise ValueError("仅支持三维 NIfTI 显示缓存")
    encoded, low, high, slope, intercept = _encode_display_data(image)
    display_image = _display_image(encoded, image.affine, image.header, low, high, slope, intercept)
    preview_image = _display_image(
        encoded[::2, ::2, :],
        image.affine @ np.diag([2, 2, 1, 1]),
        image.header,
        low,
        high,
        slope,
        intercept,
    )
    _save_nifti_atomic(display_image, display_destination)
    _save_nifti_atomic(preview_image, preview_destination)
    return display_destination, preview_destination


def ensure_display_volume(source: Path, data_root: Path, preview_root: Path) -> Path:
    destination = display_cache_path(source, data_root, preview_root)
    if destination.is_file():
        return destination
    with _preview_lock:
        if destination.is_file():
            return destination
        return generate_display_volume(source, destination)
