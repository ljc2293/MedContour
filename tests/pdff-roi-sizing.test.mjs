import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_RADIUS_MM,
  DEFAULT_ROI_AREA_CM2,
  maximumRoiRadius,
  MIN_RADIUS_MM,
  MIN_ROI_AREA_CM2,
  normalizeRoiRadius,
  roiAreaCm2,
} from "../app/lib/pdffRoiSizing.ts";

test("the default ROI is 1 cm² and is larger than the independent minimum", () => {
  assert.ok(Math.abs(roiAreaCm2(DEFAULT_RADIUS_MM) - DEFAULT_ROI_AREA_CM2) < 1e-12);
  assert.ok(MIN_RADIUS_MM < DEFAULT_RADIUS_MM);
  assert.ok(roiAreaCm2(MIN_RADIUS_MM) >= MIN_ROI_AREA_CM2);
  assert.ok(roiAreaCm2(MIN_RADIUS_MM) < 0.51);
});

test("drag sizing can shrink below the default but clamps at 0.5 cm²", () => {
  const smallerRadius = normalizeRoiRadius(4.5, 50);
  assert.ok(smallerRadius < DEFAULT_RADIUS_MM);
  assert.ok(roiAreaCm2(smallerRadius) > MIN_ROI_AREA_CM2);
  assert.equal(normalizeRoiRadius(0, 50), MIN_RADIUS_MM);
  assert.equal(normalizeRoiRadius(3.2, 50), MIN_RADIUS_MM);
});

test("resizing respects image edges and neighbouring ROIs", () => {
  const dimensions = [3, 100, 80, 30];
  const spacing = [1, 1, 1, 3];
  assert.equal(maximumRoiRadius([20, 20, 5], "head", [], dimensions, spacing), 20);
  assert.equal(maximumRoiRadius(
    [20, 20, 5],
    "head",
    [{ region: "body", center_voxel: [30, 20, 5], radius_mm: 4 }],
    dimensions,
    spacing,
  ), 6);
  assert.equal(normalizeRoiRadius(20, 6), 6);
});
