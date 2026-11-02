import type { PdffRegion, PdffRoi } from "./types";

export const DEFAULT_ROI_AREA_CM2 = 1;
export const MIN_ROI_AREA_CM2 = 0.5;
export const DEFAULT_RADIUS_MM = Math.sqrt(DEFAULT_ROI_AREA_CM2 * 100 / Math.PI);
// Round upward to 0.1 mm so a snapped frontend radius never falls below 0.5 cm².
export const MIN_RADIUS_MM = Math.ceil(Math.sqrt(MIN_ROI_AREA_CM2 * 100 / Math.PI) * 10) / 10;

export function roiAreaCm2(radiusMm: number) {
  return Math.PI * radiusMm * radiusMm / 100;
}

export function normalizeRoiRadius(radius: number, maxRadius: number) {
  const roundedMaximum = Math.floor(maxRadius * 10) / 10;
  return Math.min(roundedMaximum, Math.max(MIN_RADIUS_MM, Math.round(radius * 10) / 10));
}

export function maximumRoiRadius(
  centerVoxel: [number, number, number],
  region: PdffRegion,
  rois: PdffRoi[],
  dimensions: ArrayLike<number>,
  spacing: ArrayLike<number>,
) {
  const spacingX = Math.abs(Number(spacing[1]));
  const spacingY = Math.abs(Number(spacing[2]));
  let maximum = Math.min(
    centerVoxel[0] * spacingX,
    (Number(dimensions[1]) - 1 - centerVoxel[0]) * spacingX,
    centerVoxel[1] * spacingY,
    (Number(dimensions[2]) - 1 - centerVoxel[1]) * spacingY,
  );
  for (const other of rois) {
    if (other.region === region || Math.round(other.center_voxel[2]) !== Math.round(centerVoxel[2])) continue;
    const centerDistance = Math.hypot(
      (centerVoxel[0] - other.center_voxel[0]) * spacingX,
      (centerVoxel[1] - other.center_voxel[1]) * spacingY,
    );
    maximum = Math.min(maximum, centerDistance - other.radius_mm);
  }
  return Math.floor(maximum * 10) / 10;
}
