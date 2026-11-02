"use client";

import { CSSProperties, PointerEvent as ReactPointerEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { api } from "../lib/api";
import { prefetchImageBuffer, prefetchJson, releaseImageBuffer, releaseJson } from "../lib/imagePrefetch";
import { runPdffLoadPipeline } from "../lib/pdffLoadPipeline";
import { DEFAULT_RADIUS_MM, DEFAULT_ROI_AREA_CM2, maximumRoiRadius, MIN_RADIUS_MM, MIN_ROI_AREA_CM2, normalizeRoiRadius, roiAreaCm2 } from "../lib/pdffRoiSizing";
import { AnnotationVersionDetail, NumericStats, PancreasReferenceContours, PancreasReferenceInfo, PancreasReferenceSegment, PdffCaseSummary, PdffConsistencyStats, PdffMeasurementPlane, PdffPlaneSummary, PdffPreviewResult, PdffRegion, PdffRoi, PdffRoiMetric } from "../lib/types";
import { installDashedCrosshair, setCrosshairVisible } from "./NiiVueViewer";

type Viewer = import("@niivue/niivue").Niivue;
type DragModes = typeof import("@niivue/niivue").DRAG_MODE;
type Tool = "roi" | "navigate" | "windowing";
type SequenceKey = "f" | "w" | "in" | "opp";
type SequenceLoadState = "loading" | "loaded" | "error";
type WindowLevel = { center: number; width: number };

const EMPTY_PANCREAS_SEGMENTS: PancreasReferenceSegment[] = [];

type Props = {
  projectCaseId: string;
  csrfToken: string;
  canDraw: boolean;
  annotationVersion: number | null;
  initialPlanes?: PdffMeasurementPlane[];
  initialRois: PdffRoi[];
  initialMetrics: PdffRoiMetric[];
  reviewMode?: boolean;
  reviewBaseVersion?: number | null;
  saveComment?: string;
  consoleHost?: HTMLElement | null;
  onDirtyChange?: (dirty: boolean) => void;
  onSaved: (version: number) => void;
};

const REGIONS: Array<{ key: PdffRegion; label: string; short: string; color: string }> = [
  { key: "head", label: "胰头 ROI", short: "头", color: "#2dd4bf" },
  { key: "body", label: "胰体 ROI", short: "体", color: "#60a5fa" },
  { key: "tail", label: "胰尾 ROI", short: "尾", color: "#fb7185" },
];

const SEQUENCES: Array<{ key: SequenceKey; label: string; short: string; tone: string }> = [
  { key: "f", label: "脂相 Fat", short: "F", tone: "fat" },
  { key: "w", label: "水相 Water", short: "W", tone: "water" },
  { key: "in", label: "同相位 In-phase", short: "In", tone: "in-phase" },
  { key: "opp", label: "反相位 Opposed-phase", short: "Opp", tone: "opposed-phase" },
];

const SEQUENCE_PAIRS: Array<{ key: string; label: string; hint: string; sequences: SequenceKey[] }> = [
  { key: "fat-water", label: "定量配对 · Fat / Water", hint: "PDFF 计算序列", sequences: ["f", "w"] },
  { key: "in-opposed", label: "信号配对 · In / Opposed", hint: "同位置辅助判断", sequences: ["in", "opp"] },
];

const SEQUENCE_KEYS: SequenceKey[] = ["f", "w", "in", "opp"];
const REGION_ORDER: PdffRegion[] = ["head", "body", "tail"];
type RoiPlacementSession = {
  pointerId: number;
  sequence: SequenceKey;
  region: PdffRegion;
  centerVoxel: [number, number, number];
  centerMillimeters: [number, number, number];
  radius: number;
  maxRadius: number;
  originalRois: PdffRoi[];
};

type RoiMoveSession = {
  pointerId: number;
  sequence: SequenceKey;
  region: PdffRegion;
  radius: number;
  grabOffsetMillimeters: [number, number, number];
  originalRois: PdffRoi[];
  lastValidCenterVoxel: [number, number, number];
  moved: boolean;
};

type RoiResizeSession = {
  pointerId: number;
  sequence: SequenceKey;
  region: PdffRegion;
  centerVoxel: [number, number, number];
  centerMillimeters: [number, number, number];
  radius: number;
  maxRadius: number;
  originalRois: PdffRoi[];
  resized: boolean;
};

function emptyViewerRecord(): Record<SequenceKey, Viewer | null> {
  return { f: null, w: null, in: null, opp: null };
}

function emptyCanvasRecord(): Record<SequenceKey, HTMLCanvasElement | null> {
  return { f: null, w: null, in: null, opp: null };
}

function cloneRois(rois: PdffRoi[]): PdffRoi[] {
  return rois.map((roi) => ({ ...roi, center_voxel: [...roi.center_voxel] as [number, number, number] }));
}

function planeSummary(metrics: PdffRoiMetric[]): PdffPlaneSummary | undefined {
  if (metrics.length !== 3) return undefined;
  const fatWaterValues = metrics.map(fatWaterPdff).filter((value): value is number => Number.isFinite(value));
  const inOpposedValues = metrics.map(inOpposedPdff).filter((value): value is number => Number.isFinite(value));
  if (fatWaterValues.length !== 3 || inOpposedValues.length !== 3) return undefined;
  const sampleSd = (values: number[]) => {
    const mean = values.reduce((sum, value) => sum + value, 0) / values.length;
    return Math.sqrt(values.reduce((sum, value) => sum + (value - mean) ** 2, 0) / Math.max(1, values.length - 1));
  };
  return {
    fat_water_mean: fatWaterValues.reduce((sum, value) => sum + value, 0) / fatWaterValues.length,
    fat_water_sd: sampleSd(fatWaterValues),
    in_opposed_mean: inOpposedValues.reduce((sum, value) => sum + value, 0) / inOpposedValues.length,
    in_opposed_sd: sampleSd(inOpposedValues),
    total_area_mm2: metrics.reduce((sum, metric) => sum + metric.area_mm2, 0),
    total_voxel_count: metrics.reduce((sum, metric) => sum + metric.voxel_count, 0),
  };
}

function clonePlane(plane: PdffMeasurementPlane): PdffMeasurementPlane {
  const metrics = plane.metrics.map((metric) => ({ ...metric }));
  return { ...plane, rois: cloneRois(plane.rois), metrics, summary: plane.summary ?? planeSummary(metrics) };
}

function normalizePlanes(initialPlanes: PdffMeasurementPlane[] | undefined, legacyRois: PdffRoi[], legacyMetrics: PdffRoiMetric[]): PdffMeasurementPlane[] {
  const byIndex = new Map<number, PdffMeasurementPlane>();
  for (const plane of initialPlanes ?? []) byIndex.set(plane.plane_index, clonePlane(plane));
  if (!byIndex.size && legacyRois.length) {
    const metrics = legacyMetrics.map((metric) => ({ ...metric }));
    byIndex.set(1, {
      plane_index: 1,
      slice_index: legacyRois[0] ? Math.round(legacyRois[0].center_voxel[2]) : undefined,
      rois: cloneRois(legacyRois),
      metrics,
      summary: planeSummary(metrics),
    });
  }
  return ([1, 2, 3] as const).map((planeIndex) => byIndex.get(planeIndex) ?? { plane_index: planeIndex, rois: [], metrics: [] });
}

function consistencyStats(values: number[]): PdffConsistencyStats {
  const finite = values.filter(Number.isFinite);
  if (!finite.length) return { count: 0, mean: null, sd: null, cv_percent: null, min: null, max: null, range: null };
  const mean = finite.reduce((sum, value) => sum + value, 0) / finite.length;
  const sd = finite.length > 1
    ? Math.sqrt(finite.reduce((sum, value) => sum + (value - mean) ** 2, 0) / (finite.length - 1))
    : 0;
  const minimum = Math.min(...finite);
  const maximum = Math.max(...finite);
  return { count: finite.length, mean, sd, cv_percent: Math.abs(mean) > 1e-6 ? Math.abs(sd / mean) * 100 : null, min: minimum, max: maximum, range: maximum - minimum };
}

function buildCaseSummary(planes: PdffMeasurementPlane[]): PdffCaseSummary | null {
  const completed = planes.filter((plane) => plane.rois.length === 3 && plane.metrics.length === 3 && plane.summary);
  if (!completed.length) return null;
  const regions = Object.fromEntries(REGION_ORDER.map((region) => {
    const metrics = completed.map((plane) => plane.metrics.find((metric) => metric.region === region)).filter((metric): metric is PdffRoiMetric => Boolean(metric));
    return [region, {
      fat_water: consistencyStats(metrics.map((metric) => Number(fatWaterPdff(metric)))),
      in_opposed: consistencyStats(metrics.map((metric) => Number(inOpposedPdff(metric)))),
    }];
  })) as PdffCaseSummary["regions"];
  return {
    plane_count: completed.length,
    roi_count: completed.length * 3,
    slice_indices: completed.map((plane) => plane.slice_index ?? Math.round(plane.rois[0].center_voxel[2])),
    fat_water: consistencyStats(completed.map((plane) => Number(plane.summary?.fat_water_mean))),
    in_opposed: consistencyStats(completed.map((plane) => Number(plane.summary?.in_opposed_mean))),
    regions,
    total_area_mm2: completed.reduce((sum, plane) => sum + Number(plane.summary?.total_area_mm2 ?? 0), 0),
    total_voxel_count: completed.reduce((sum, plane) => sum + Number(plane.summary?.total_voxel_count ?? 0), 0),
  };
}


function configureMouse(nv: Viewer, modes: DragModes, tool: Tool) {
  nv.setMouseEventConfig({
    leftButton: { primary: tool === "windowing" ? modes.windowing : modes.crosshair },
    rightButton: modes.windowing,
    centerButton: modes.pan,
  });
}

function sliceInfo(nv: Viewer): { index: number; maximum: number } | null {
  const dimensions = nv.volumes[0]?.dimsRAS;
  if (!dimensions || dimensions.length < 4) return null;
  const voxels = Array.from(nv.frac2vox(nv.scene.crosshairPos));
  const maximum = Math.max(0, Math.trunc(Number(dimensions[3])) - 1);
  return { index: Math.min(maximum, Math.max(0, Math.round(Number(voxels[2])))), maximum };
}

function readWindowLevel(nv: Viewer): WindowLevel | null {
  const volume = nv.volumes[0];
  if (!volume || !Number.isFinite(volume.cal_min) || !Number.isFinite(volume.cal_max)) return null;
  const minimum = Number(volume.cal_min);
  const maximum = Number(volume.cal_max);
  return { center: (minimum + maximum) / 2, width: Math.max(0, maximum - minimum) };
}

function restoreVolumeWindow(nv: Viewer) {
  const volume = nv.volumes[0];
  if (!volume) return false;
  const minimum = Number.isFinite(volume.robust_min) ? Number(volume.robust_min) : Number(volume.global_min);
  const maximum = Number.isFinite(volume.robust_max) ? Number(volume.robust_max) : Number(volume.global_max);
  if (!Number.isFinite(minimum) || !Number.isFinite(maximum)) return false;
  volume.cal_min = minimum;
  volume.cal_max = maximum;
  nv.refreshLayers(volume, 0);
  nv.drawScene();
  return true;
}

function applyVolumeWindow(nv: Viewer, center: number, width: number) {
  const volume = nv.volumes[0];
  if (!volume || !Number.isFinite(center) || !Number.isFinite(width) || width <= 0) return false;
  volume.cal_min = center - width / 2;
  volume.cal_max = center + width / 2;
  nv.refreshLayers(volume, 0);
  nv.drawScene();
  return true;
}

function formatWindowValue(value: number) {
  return Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(1);
}

type OverlayItem = { region: PdffRegion; x: number; y: number; rx: number; ry: number; color: string; label: string };
type OverlayGeometry = { width: number; height: number; items: OverlayItem[]; pancreasPath: string };

function referenceVoxelToViewerFraction(reference: Viewer, viewer: Viewer, voxel: [number, number, number]) {
  const referenceFraction = reference.vox2frac(voxel as unknown as Parameters<Viewer["vox2frac"]>[0]);
  return viewer.mm2frac(reference.frac2mm(referenceFraction));
}

function pointerToReference(
  viewer: Viewer,
  reference: Viewer,
  canvas: HTMLCanvasElement,
  clientX: number,
  clientY: number,
): { voxel: [number, number, number]; millimeters: [number, number, number] } | null {
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height || !canvas.width || !canvas.height) return null;
  const canvasX = (clientX - rect.left) * canvas.width / rect.width;
  const canvasY = (clientY - rect.top) * canvas.height / rect.height;
  const viewerFraction = viewer.canvasPos2frac([canvasX, canvasY]);
  const referenceFraction = reference.mm2frac(viewer.frac2mm(viewerFraction));
  const voxelValues = Array.from(reference.frac2vox(referenceFraction)).map(Number);
  const millimeterValues = Array.from(reference.frac2mm(referenceFraction)).map(Number);
  if (voxelValues.length < 3 || millimeterValues.length < 3 || [...voxelValues, ...millimeterValues].some((value) => !Number.isFinite(value))) return null;
  return {
    voxel: [voxelValues[0], voxelValues[1], voxelValues[2]],
    millimeters: [millimeterValues[0], millimeterValues[1], millimeterValues[2]],
  };
}

function overlayGeometry(
  viewer: Viewer | null,
  roiReference: Viewer | null,
  rois: PdffRoi[],
  currentSlice: number,
  pancreasReference: Viewer | null,
  pancreasSegments: PancreasReferenceSegment[],
): OverlayGeometry | null {
  const canvas = viewer?.canvas;
  const referenceVolume = roiReference?.volumes[0];
  if (!viewer || !roiReference || !canvas || !referenceVolume) return null;
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height || !canvas.width || !canvas.height) return null;
  const spacing = referenceVolume.pixDimsRAS;
  if (!spacing || spacing.length < 4) return null;
  const scaleX = rect.width / canvas.width;
  const scaleY = rect.height / canvas.height;
  const items: OverlayItem[] = [];
  for (const roi of rois) {
    if (Math.round(roi.center_voxel[2]) !== currentSlice) continue;
    const centerVoxel = roi.center_voxel;
    const xEdgeVoxel: [number, number, number] = [centerVoxel[0] + roi.radius_mm / Number(spacing[1]), centerVoxel[1], centerVoxel[2]];
    const yEdgeVoxel: [number, number, number] = [centerVoxel[0], centerVoxel[1] + roi.radius_mm / Number(spacing[2]), centerVoxel[2]];
    const center = viewer.frac2canvasPosWithTile(referenceVoxelToViewerFraction(roiReference, viewer, centerVoxel), viewer.sliceTypeAxial);
    const xEdge = viewer.frac2canvasPosWithTile(referenceVoxelToViewerFraction(roiReference, viewer, xEdgeVoxel), viewer.sliceTypeAxial);
    const yEdge = viewer.frac2canvasPosWithTile(referenceVoxelToViewerFraction(roiReference, viewer, yEdgeVoxel), viewer.sliceTypeAxial);
    if (!center || !xEdge || !yEdge || center.tileIndex !== xEdge.tileIndex || center.tileIndex !== yEdge.tileIndex) continue;
    const definition = REGIONS.find((item) => item.key === roi.region)!;
    items.push({
      region: roi.region,
      x: center.pos[0] * scaleX,
      y: center.pos[1] * scaleY,
      rx: Math.max(4, Math.abs(xEdge.pos[0] - center.pos[0]) * scaleX),
      ry: Math.max(4, Math.abs(yEdge.pos[1] - center.pos[1]) * scaleY),
      color: definition.color,
      label: definition.short,
    });
  }
  const pancreasPath = pancreasReference ? pancreasSegments.flatMap(([x1, y1, x2, y2]) => {
    const start = viewer.frac2canvasPosWithTile(
      referenceVoxelToViewerFraction(pancreasReference, viewer, [x1, y1, currentSlice]),
      viewer.sliceTypeAxial,
    );
    const end = viewer.frac2canvasPosWithTile(
      referenceVoxelToViewerFraction(pancreasReference, viewer, [x2, y2, currentSlice]),
      viewer.sliceTypeAxial,
    );
    if (!start || !end || start.tileIndex !== end.tileIndex) return [];
    return [`M ${start.pos[0] * scaleX} ${start.pos[1] * scaleY} L ${end.pos[0] * scaleX} ${end.pos[1] * scaleY}`];
  }).join(" ") : "";
  return { width: rect.width, height: rect.height, items, pancreasPath };
}

type RoiOverlayProps = {
  geometry: OverlayGeometry | null;
  activeRegion: PdffRegion;
  sequence: SequenceKey;
  canMove: boolean;
  onMoveStart: (sequence: SequenceKey, region: PdffRegion, event: ReactPointerEvent<SVGEllipseElement>) => void;
  onMove: (sequence: SequenceKey, event: ReactPointerEvent<SVGEllipseElement>) => void;
  onMoveEnd: (sequence: SequenceKey, event: ReactPointerEvent<SVGEllipseElement>) => void;
  onMoveCancel: (sequence: SequenceKey, event: ReactPointerEvent<SVGEllipseElement>) => void;
  onResizeStart: (sequence: SequenceKey, region: PdffRegion, event: ReactPointerEvent<SVGCircleElement>) => void;
  onResize: (sequence: SequenceKey, event: ReactPointerEvent<SVGCircleElement>) => void;
  onResizeEnd: (sequence: SequenceKey, event: ReactPointerEvent<SVGCircleElement>) => void;
  onResizeCancel: (sequence: SequenceKey, event: ReactPointerEvent<SVGCircleElement>) => void;
};

function RoiOverlay({ geometry, activeRegion, sequence, canMove, onMoveStart, onMove, onMoveEnd, onMoveCancel, onResizeStart, onResize, onResizeEnd, onResizeCancel }: RoiOverlayProps) {
  if (!geometry) return null;
  return <svg aria-hidden={!canMove} className="pdff-roi-overlay" preserveAspectRatio="none" viewBox={`0 0 ${geometry.width} ${geometry.height}`}>
    {geometry.pancreasPath && <>
      <path className="pancreas-reference-halo" d={geometry.pancreasPath} />
      <path className="pancreas-reference-line" d={geometry.pancreasPath} />
    </>}
    {geometry.items.map((item) => <g className={item.region === activeRegion ? "active" : ""} key={item.region}>
      <ellipse
        aria-label={canMove ? `拖动${REGIONS.find((region) => region.key === item.region)?.label}移动位置` : undefined}
        className={canMove ? "move-handle" : undefined}
        cx={item.x}
        cy={item.y}
        fill="transparent"
        onPointerCancel={(event) => onMoveCancel(sequence, event)}
        onPointerDown={(event) => onMoveStart(sequence, item.region, event)}
        onPointerMove={(event) => onMove(sequence, event)}
        onPointerUp={(event) => onMoveEnd(sequence, event)}
        role={canMove ? "button" : undefined}
        rx={item.rx + 5}
        ry={item.ry + 5}
      />
      <ellipse className="roi-shape" cx={item.x} cy={item.y} fill={`${item.color}22`} rx={item.rx} ry={item.ry} stroke={item.color} strokeWidth={item.region === activeRegion ? 2.5 : 1.8} />
      <circle cx={item.x} cy={item.y} fill={item.color} r="2.5" />
      {canMove && item.region === activeRegion && <circle
        aria-label={`拖动${REGIONS.find((region) => region.key === item.region)?.label}边缘手柄调整大小`}
        className="resize-handle"
        cx={item.x + item.rx}
        cy={item.y}
        fill={item.color}
        onPointerCancel={(event) => onResizeCancel(sequence, event)}
        onPointerDown={(event) => onResizeStart(sequence, item.region, event)}
        onPointerMove={(event) => onResize(sequence, event)}
        onPointerUp={(event) => onResizeEnd(sequence, event)}
        r="5"
        role="button"
        stroke="#f4fffd"
        strokeWidth="1.8"
      />}
      <g transform={`translate(${item.x + item.rx + 7} ${item.y - item.ry - 4})`}><rect fill="rgba(5,18,24,.82)" height="22" rx="6" width="42" /><text fill={item.color} x="21" y="15">{item.label} ROI</text></g>
    </g>)}
  </svg>;
}

function formatValue(value: number | undefined, digits = 1) {
  return Number.isFinite(value) ? Number(value).toFixed(digits) : "—";
}

function varianceOf(stats: NumericStats | undefined) {
  return stats ? stats.sd * stats.sd : undefined;
}

function fatWaterPdff(metric: PdffRoiMetric | undefined) {
  return metric?.pdff_ratio_of_means ?? metric?.pdff.mean;
}

function inOpposedPdff(metric: PdffRoiMetric | undefined) {
  return metric?.pdff_in_opposed_ratio_of_means ?? metric?.pdff_in_opposed?.mean;
}

export function PdffRoiViewer({
  projectCaseId,
  csrfToken,
  canDraw,
  annotationVersion,
  initialPlanes = [],
  initialRois,
  initialMetrics,
  reviewMode = false,
  reviewBaseVersion = null,
  saveComment = "",
  consoleHost = null,
  onDirtyChange,
  onSaved,
}: Props) {
  const canvasRefs = useRef<Record<SequenceKey, HTMLCanvasElement | null>>(emptyCanvasRecord());
  const viewerRefs = useRef<Record<SequenceKey, Viewer | null>>(emptyViewerRecord());
  const dragModesRef = useRef<DragModes | null>(null);
  const roiPlacementRef = useRef<RoiPlacementSession | null>(null);
  const roiMoveRef = useRef<RoiMoveSession | null>(null);
  const roiResizeRef = useRef<RoiResizeSession | null>(null);
  const initialPlanesRef = useRef(normalizePlanes(initialPlanes, initialRois, initialMetrics));
  const onDirtyChangeRef = useRef(onDirtyChange);
  const activeSequenceRef = useRef<SequenceKey>("f");
  const [planes, setPlanes] = useState<PdffMeasurementPlane[]>(() => normalizePlanes(initialPlanes, initialRois, initialMetrics));
  const [activePlaneIndex, setActivePlaneIndex] = useState<1 | 2 | 3>(1);
  const activePlane = planes.find((plane) => plane.plane_index === activePlaneIndex) ?? planes[0];
  const rois = activePlane.rois;
  const metrics = activePlane.metrics;
  const activeRoisRef = useRef<PdffRoi[]>(cloneRois(rois));
  const [activeRegion, setActiveRegionState] = useState<PdffRegion>(rois.length < 3 ? REGION_ORDER.find((region) => !rois.some((roi) => roi.region === region)) ?? "head" : "head");
  const activeRegionRef = useRef<PdffRegion>(activeRegion);
  const [activeSequence, setActiveSequence] = useState<SequenceKey>("f");
  const [tool, setTool] = useState<Tool>(canDraw ? "roi" : "navigate");
  const [slice, setSlice] = useState({ index: 0, maximum: 0 });
  const [overlayRevision, setOverlayRevision] = useState(0);
  const [overlays, setOverlays] = useState<Record<SequenceKey, OverlayGeometry | null>>({ f: null, w: null, in: null, opp: null });
  const [loadStates, setLoadStates] = useState<Record<SequenceKey, SequenceLoadState>>({ f: "loading", w: "loading", in: "loading", opp: "loading" });
  const primaryVolumesReady = loadStates.f === "loaded" && loadStates.w === "loaded";
  const [windowInputs, setWindowInputs] = useState({ center: "", width: "" });
  const [history, setHistory] = useState<PdffRoi[][]>([]);
  const [showRois, setShowRois] = useState(true);
  const [showPancreasReference, setShowPancreasReference] = useState(true);
  const [pancreasReferenceInfo, setPancreasReferenceInfo] = useState<PancreasReferenceInfo | null>(null);
  const [pancreasContours, setPancreasContours] = useState<Record<number, PancreasReferenceSegment[]>>({});
  const [pancreasReferenceLoading, setPancreasReferenceLoading] = useState(true);
  const pancreasReferenceSegments = showPancreasReference && pancreasReferenceInfo?.available
    ? pancreasContours[slice.index] ?? EMPTY_PANCREAS_SEGMENTS
    : EMPTY_PANCREAS_SEGMENTS;
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [placingRoi, setPlacingRoi] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [message, setMessage] = useState("正在读取 Dixon 四相位影像…");
  const [error, setError] = useState("");

  const activateRegion = useCallback((region: PdffRegion) => {
    activeRegionRef.current = region;
    setActiveRegionState(region);
  }, []);

  const setRois = useCallback((action: PdffRoi[] | ((current: PdffRoi[]) => PdffRoi[])) => {
    const nextRois = cloneRois(typeof action === "function" ? action(cloneRois(activeRoisRef.current)) : action);
    activeRoisRef.current = nextRois;
    setPlanes((current) => current.map((plane) => {
      if (plane.plane_index !== activePlaneIndex) return plane;
      return {
        ...plane,
        slice_index: nextRois[0] ? Math.round(nextRois[0].center_voxel[2]) : undefined,
        rois: cloneRois(nextRois),
        summary: undefined,
      };
    }));
  }, [activePlaneIndex]);

  const setMetrics = useCallback((action: PdffRoiMetric[] | ((current: PdffRoiMetric[]) => PdffRoiMetric[])) => {
    setPlanes((current) => current.map((plane) => {
      if (plane.plane_index !== activePlaneIndex) return plane;
      const nextMetrics = typeof action === "function" ? action(plane.metrics) : action;
      return { ...plane, metrics: nextMetrics.map((metric) => ({ ...metric })), summary: planeSummary(nextMetrics) };
    }));
  }, [activePlaneIndex]);

  useEffect(() => {
    initialPlanesRef.current = normalizePlanes(initialPlanes, initialRois, initialMetrics);
    onDirtyChangeRef.current = onDirtyChange;
  }, [initialMetrics, initialPlanes, initialRois, onDirtyChange]);

  useEffect(() => {
    activeRoisRef.current = cloneRois(rois);
  }, [activePlaneIndex, rois]);

  useEffect(() => {
    activeSequenceRef.current = activeSequence;
  }, [activeSequence]);

  useEffect(() => {
    let active = true;
    const referenceUrl = `/api/cases/${projectCaseId}/pancreas-reference/contours`;
    const loadReferenceContours = async () => {
      setPancreasReferenceInfo(null);
      setPancreasContours({});
      setPancreasReferenceLoading(true);
      try {
        const result = await prefetchJson<PancreasReferenceContours>(referenceUrl);
        if (!active) return;
        setPancreasReferenceInfo(result);
        setPancreasContours(Object.fromEntries(
          result.contours.map((contour) => [contour.slice_index, contour.segments]),
        ));
      } catch (reason) {
        if (!active) return;
        setPancreasReferenceInfo({
          available: false,
          status: "invalid",
          model: null,
          reference_sequence: "w",
          slice_count: null,
          reason: reason instanceof Error ? reason.message : "胰腺参考分割读取失败",
          warning: "AI 胰腺参考轮廓仅用于辅助定位，不限制 PDFF ROI 的放置范围",
        });
        setPancreasContours({});
      } finally {
        if (active) setPancreasReferenceLoading(false);
      }
    };
    void loadReferenceContours();
    return () => {
      active = false;
      releaseJson(referenceUrl);
    };
  }, [projectCaseId]);

  const bumpOverlay = useCallback(() => setOverlayRevision((current) => current + 1), []);
  const syncSlice = useCallback((nv: Viewer) => {
    const next = sliceInfo(nv);
    if (next) setSlice(next);
    window.requestAnimationFrame(bumpOverlay);
  }, [bumpOverlay]);

  const syncWindowInputs = useCallback((viewer: Viewer | null) => {
    const next = viewer ? readWindowLevel(viewer) : null;
    if (next) setWindowInputs({ center: formatWindowValue(next.center), width: formatWindowValue(next.width) });
  }, []);

  const focusSequence = useCallback((key: SequenceKey) => {
    setActiveSequence(key);
    activeSequenceRef.current = key;
    syncWindowInputs(viewerRefs.current[key]);
  }, [syncWindowInputs]);

  const syncViewersToVoxel = useCallback((voxel: [number, number, number]) => {
    const reference = viewerRefs.current.f;
    if (!reference) return;
    const referenceFraction = reference.vox2frac(voxel as unknown as Parameters<Viewer["vox2frac"]>[0]);
    const millimeters = reference.frac2mm(referenceFraction);
    for (const key of SEQUENCE_KEYS) {
      const viewer = viewerRefs.current[key];
      if (!viewer?.volumes.length) continue;
      viewer.scene.crosshairPos = viewer.mm2frac(millimeters);
      viewer.drawScene();
    }
    syncSlice(reference);
  }, [syncSlice]);

  useEffect(() => {
    let disposed = false;
    const managedViewers = emptyViewerRecord();
    const viewerStore = viewerRefs.current;
    const initialize = async () => {
      if (SEQUENCE_KEYS.some((key) => !canvasRefs.current[key])) return;
      setLoading(true);
      setError("");
      setMessage("正在读取 Dixon 四相位影像…");
      setLoadStates({ f: "loading", w: "loading", in: "loading", opp: "loading" });
      const referenceUrl = `/api/cases/${projectCaseId}/pancreas-reference/contours`;
      const pancreasReferenceReady = prefetchJson<PancreasReferenceContours>(referenceUrl).catch(() => null);
      try {
        const { Niivue, DRAG_MODE } = await import("@niivue/niivue");
        if (disposed) return;
        const createViewer = () => new Niivue({
          backColor: [0.01, 0.02, 0.035, 1],
          crosshairColor: [0.18, 0.83, 0.75, 0.82],
          crosshairWidth: 1,
          isColorbar: false,
          isRuler: true,
          fontMinPx: 12,
          isRadiologicalConvention: true,
          isNearestInterpolation: false,
          showAllOrientationMarkers: true,
        });
        dragModesRef.current = DRAG_MODE;
        const viewers = managedViewers;
        for (const key of SEQUENCE_KEYS) {
          viewers[key] = createViewer();
          viewerStore[key] = viewers[key];
        }
        await Promise.all(SEQUENCE_KEYS.map(async (key) => {
          await viewers[key]!.attachToCanvas(canvasRefs.current[key]!);
          installDashedCrosshair(viewers[key]!, !canDraw);
        }));
        const configureViewer = (key: SequenceKey) => {
          const viewer = viewers[key]!;
          viewer.setSliceType(viewer.sliceTypeAxial);
          viewer.setRadiologicalConvention(true);
          viewer.setDrawingEnabled(false);
          configureMouse(viewer, DRAG_MODE, canDraw ? "roi" : "navigate");
          viewer.onLocationChange = () => { if (!disposed) syncSlice(viewer); };
          viewer.onIntensityChange = () => {
            if (!disposed && activeSequenceRef.current === key) syncWindowInputs(viewer);
          };
          viewer.onMouseUp = () => {
            if (disposed) return;
            bumpOverlay();
            if (activeSequenceRef.current === key) syncWindowInputs(viewer);
          };
        };
        const connectLoadedViewers = () => {
          const loadedKeys = SEQUENCE_KEYS.filter((key) => viewers[key]!.volumes.length > 0);
          for (const key of loadedKeys) {
            viewers[key]!.broadcastTo(loadedKeys.filter((other) => other !== key).map((other) => viewers[other]!), { "2d": true, "3d": false });
          }
        };
        const loadOriginal = async (key: SequenceKey) => {
          const volumeUrl = `/api/cases/${projectCaseId}/volume/${key}.nii.gz`;
          try {
            const buffer = await prefetchImageBuffer(volumeUrl);
            if (disposed) throw new Error("影像加载已取消");
            await viewers[key]!.loadVolumes([{ url: volumeUrl, buffer, name: `${key}.nii.gz`, colormap: "gray", opacity: 1 }]);
            configureViewer(key);
            if ((key === "in" || key === "opp") && viewers.f!.volumes.length) {
              const referenceMillimeters = viewers.f!.frac2mm(viewers.f!.scene.crosshairPos);
              viewers[key]!.scene.crosshairPos = viewers[key]!.mm2frac(referenceMillimeters);
              viewers[key]!.drawScene();
            }
            if (!disposed) setLoadStates((current) => ({ ...current, [key]: "loaded" }));
            window.requestAnimationFrame(bumpOverlay);
            return viewers[key]!;
          } catch (reason) {
            if (!disposed) setLoadStates((current) => ({ ...current, [key]: "error" }));
            throw reason;
          } finally {
            releaseImageBuffer(volumeUrl);
          }
        };
        const { secondaryResults } = await runPdffLoadPipeline({
          loadVolume: loadOriginal,
          loadReference: () => pancreasReferenceReady,
          onPrimaryReady: (pancreasResult) => {
            if (disposed) throw new Error("影像加载已取消");
            if (pancreasResult) {
              setPancreasReferenceInfo(pancreasResult);
              setPancreasContours(Object.fromEntries(
                pancreasResult.contours.map((contour) => [contour.slice_index, contour.segments]),
              ));
            }
            setPancreasReferenceLoading(false);
            connectLoadedViewers();
            const initial = initialPlanesRef.current.find((plane) => plane.rois.length)?.rois[0];
            if (initial) syncViewersToVoxel(initial.center_voxel);
            for (const key of (["f", "w"] as SequenceKey[])) viewers[key]!.drawScene();
            syncSlice(viewers.f!);
            syncWindowInputs(viewers.f);
            setTool(canDraw ? "roi" : "navigate");
            setLoading(false);
            setMessage(canDraw
              ? `Fat / Water 原图与${pancreasResult?.available ? "胰腺参考分割" : "参考分割检查"}已就绪，可立即标注；正在加载 In / Opposed 原图。`
              : `Fat / Water 原图与${pancreasResult?.available ? "胰腺参考分割" : "参考分割检查"}已就绪；正在加载 In / Opposed 原图。`
            );
          },
        });
        if (disposed) return;
        const referenceMillimeters = viewers.f!.frac2mm(viewers.f!.scene.crosshairPos);
        for (const key of (["in", "opp"] as SequenceKey[])) {
          const viewer = viewers[key]!;
          if (!viewer.volumes.length) continue;
          viewer.scene.crosshairPos = viewer.mm2frac(referenceMillimeters);
          viewer.drawScene();
        }
        connectLoadedViewers();
        window.requestAnimationFrame(bumpOverlay);
        const secondaryFailed = secondaryResults.some((result) => result.status === "rejected");
        setMessage(secondaryFailed
          ? "Fat / Water 原始影像可正常标注；辅助相位加载失败，可刷新后重试。"
          : canDraw
            ? `四相位原始影像均已联动；单击放置默认 ${DEFAULT_ROI_AREA_CM2.toFixed(1)} cm² 圆，拖圆内移动，拖边缘手柄缩放。`
            : "四相位原始影像均已加载并联动。"
        );
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "Dixon 四相位影像加载失败");
      } finally {
        if (!disposed) setLoading(false);
      }
    };
    void initialize();
    const onResize = () => bumpOverlay();
    window.addEventListener("resize", onResize);
    return () => {
      disposed = true;
      for (const key of SEQUENCE_KEYS) releaseImageBuffer(`/api/cases/${projectCaseId}/volume/${key}.nii.gz`);
      releaseJson(`/api/cases/${projectCaseId}/pancreas-reference/contours`);
      window.removeEventListener("resize", onResize);
      for (const key of SEQUENCE_KEYS) {
        managedViewers[key]?.cleanup();
        if (viewerStore[key] === managedViewers[key]) viewerStore[key] = null;
      }
    };
  }, [bumpOverlay, canDraw, projectCaseId, syncSlice, syncViewersToVoxel, syncWindowInputs]);

  useEffect(() => {
    const modes = dragModesRef.current;
    if (!modes) return;
    for (const key of SEQUENCE_KEYS) {
      const viewer = viewerRefs.current[key];
      if (viewer) {
        configureMouse(viewer, modes, tool);
        setCrosshairVisible(viewer, tool !== "roi");
      }
    }
  }, [tool]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      const roiReference = viewerRefs.current.f;
      const pancreasReference = viewerRefs.current.w;
      const visibleRois = showRois && primaryVolumesReady ? rois : [];
      const visiblePancreasSegments = showPancreasReference && primaryVolumesReady ? pancreasReferenceSegments : [];
      setOverlays({
        f: overlayGeometry(viewerRefs.current.f, roiReference, visibleRois, slice.index, pancreasReference, visiblePancreasSegments),
        w: overlayGeometry(viewerRefs.current.w, roiReference, visibleRois, slice.index, pancreasReference, visiblePancreasSegments),
        in: overlayGeometry(viewerRefs.current.in, roiReference, visibleRois, slice.index, pancreasReference, visiblePancreasSegments),
        opp: overlayGeometry(viewerRefs.current.opp, roiReference, visibleRois, slice.index, pancreasReference, visiblePancreasSegments),
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [primaryVolumesReady, overlayRevision, pancreasReferenceSegments, rois, showPancreasReference, showRois, slice.index]);

  useEffect(() => {
    if (placingRoi || !rois.length) return;
    const controller = new AbortController();
    let active = true;
    const timer = window.setTimeout(async () => {
      setPreviewing(true);
      setPreviewError("");
      try {
        const result = await api<PdffPreviewResult>(`/api/cases/${projectCaseId}/pdff-preview`, {
          method: "POST",
          body: JSON.stringify({ rois: [...rois].sort((left, right) => REGION_ORDER.indexOf(left.region) - REGION_ORDER.indexOf(right.region)) }),
          signal: controller.signal,
        }, csrfToken);
        if (active) setMetrics(result.metrics ?? []);
      } catch (reason) {
        if (active && !(reason instanceof DOMException && reason.name === "AbortError")) {
          setPreviewError(reason instanceof Error ? reason.message : "ROI 实时统计失败");
        }
      } finally {
        if (active) setPreviewing(false);
      }
    }, 220);
    return () => {
      active = false;
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [csrfToken, placingRoi, projectCaseId, rois, setMetrics]);

  const markDirty = useCallback(() => {
    setDirty(true);
    setPreviewing(true);
    setPreviewError("");
    onDirtyChangeRef.current?.(true);
  }, []);

  const commitRois = useCallback((next: PdffRoi[]) => {
    setHistory((current) => [...current.slice(-19), cloneRois(rois)]);
    setRois(cloneRois(next));
    markDirty();
    if (!next.length) {
      setMetrics([]);
      setPreviewing(false);
      setPreviewError("");
    }
    window.requestAnimationFrame(bumpOverlay);
  }, [bumpOverlay, markDirty, rois, setMetrics, setRois]);

  const beginRoiPlacement = useCallback((sequence: SequenceKey, event: ReactPointerEvent<HTMLCanvasElement>) => {
    focusSequence(sequence);
    if (!canDraw || tool !== "roi" || event.button !== 0) return;
    const placementRegion = activeRegionRef.current;
    const viewer = viewerRefs.current[sequence];
    const reference = viewerRefs.current.f;
    const canvas = canvasRefs.current[sequence];
    if (!viewer || !reference || !canvas || loading) return;
    event.preventDefault();
    event.stopPropagation();
    event.nativeEvent.stopImmediatePropagation();
    const point = pointerToReference(viewer, reference, canvas, event.clientX, event.clientY);
    const dimensions = reference.volumes[0]?.dimsRAS;
    const spacing = reference.volumes[0]?.pixDimsRAS;
    if (!point || !dimensions || !spacing || point.voxel[0] < 0 || point.voxel[1] < 0 || point.voxel[0] >= Number(dimensions[1]) || point.voxel[1] >= Number(dimensions[2])) {
      setError("请在轴位影像范围内放置 ROI");
      return;
    }
    const centerVoxel: [number, number, number] = [Number(point.voxel[0].toFixed(3)), Number(point.voxel[1].toFixed(3)), Math.round(point.voxel[2])];
    const occupiedByAnotherPlane = planes.some((plane) =>
      plane.plane_index !== activePlaneIndex
      && plane.rois.some((roi) => Math.round(roi.center_voxel[2]) === centerVoxel[2])
    );
    if (occupiedByAnotherPlane) {
      setError(`第 ${centerVoxel[2] + 1} 层已用于其他测量平面，请为平面 ${activePlaneIndex} 选择不同轴位层面。`);
      return;
    }
    const originalRois = cloneRois(activeRoisRef.current);
    const maxRadius = maximumRoiRadius(centerVoxel, placementRegion, originalRois, dimensions, spacing);
    if (!Number.isFinite(maxRadius) || maxRadius < MIN_RADIUS_MM) {
      setError(`该圆心距影像边缘或其他 ROI 太近，无法容纳面积至少 ${MIN_ROI_AREA_CM2.toFixed(1)} cm² 的圆。`);
      return;
    }
    const initialRadius = Math.min(DEFAULT_RADIUS_MM, maxRadius);
    const previewRoi: PdffRoi = {
      region: placementRegion,
      center_voxel: centerVoxel,
      radius_mm: initialRadius,
    };
    roiPlacementRef.current = {
      pointerId: event.pointerId,
      sequence,
      region: placementRegion,
      centerVoxel,
      centerMillimeters: point.millimeters,
      radius: initialRadius,
      maxRadius,
      originalRois,
    };
    setPlacingRoi(true);
    const preview = [...originalRois.filter((roi) => roi.region !== placementRegion), previewRoi]
      .sort((left, right) => REGION_ORDER.indexOf(left.region) - REGION_ORDER.indexOf(right.region));
    setRois(preview);
    setShowRois(true);
    syncViewersToVoxel(centerVoxel);
    setError("");
    setMessage(`${REGIONS.find((item) => item.key === placementRegion)?.label}：已放置默认 ${roiAreaCm2(initialRadius).toFixed(2)} cm²；松开保留默认大小，拖动时由光标到圆心的距离缩小或放大（最小 ${MIN_ROI_AREA_CM2.toFixed(1)} cm²）。`);
    try { canvas.setPointerCapture(event.pointerId); } catch { /* Pointer capture is best-effort. */ }
    window.requestAnimationFrame(bumpOverlay);
  }, [activePlaneIndex, bumpOverlay, canDraw, focusSequence, loading, planes, setRois, syncViewersToVoxel, tool]);

  const updateRoiPlacement = useCallback((sequence: SequenceKey, event: ReactPointerEvent<HTMLCanvasElement>) => {
    const session = roiPlacementRef.current;
    if (!session || session.sequence !== sequence || session.pointerId !== event.pointerId) return;
    const viewer = viewerRefs.current[sequence];
    const reference = viewerRefs.current.f;
    const canvas = canvasRefs.current[sequence];
    if (!viewer || !reference || !canvas) return;
    event.preventDefault();
    event.stopPropagation();
    event.nativeEvent.stopImmediatePropagation();
    const point = pointerToReference(viewer, reference, canvas, event.clientX, event.clientY);
    if (!point) return;
    const radius = normalizeRoiRadius(Math.hypot(
      point.millimeters[0] - session.centerMillimeters[0],
      point.millimeters[1] - session.centerMillimeters[1],
      point.millimeters[2] - session.centerMillimeters[2],
    ), session.maxRadius);
    if (radius === session.radius) return;
    session.radius = radius;
    setRois((current) => current.map((roi) => roi.region === session.region ? { ...roi, radius_mm: radius } : roi));
    setMessage(`${REGIONS.find((item) => item.key === session.region)?.label}：面积 ${roiAreaCm2(radius).toFixed(2)} cm²（半径 ${radius.toFixed(1)} mm）；松开完成。`);
    window.requestAnimationFrame(bumpOverlay);
  }, [bumpOverlay, setRois]);

  const finishRoiPlacement = useCallback((sequence: SequenceKey, event: ReactPointerEvent<HTMLCanvasElement>) => {
    const session = roiPlacementRef.current;
    if (!session || session.sequence !== sequence || session.pointerId !== event.pointerId) return;
    event.preventDefault();
    event.stopPropagation();
    event.nativeEvent.stopImmediatePropagation();
    roiPlacementRef.current = null;
    setPlacingRoi(false);
    try { event.currentTarget.releasePointerCapture(event.pointerId); } catch { /* Capture may already be released. */ }
    const finalRoi: PdffRoi = { region: session.region, center_voxel: session.centerVoxel, radius_mm: session.radius };
    const finalRois = [...session.originalRois.filter((roi) => roi.region !== session.region), finalRoi]
      .sort((left, right) => REGION_ORDER.indexOf(left.region) - REGION_ORDER.indexOf(right.region));
    setRois(finalRois);
    setHistory((current) => [...current.slice(-19), session.originalRois]);
    markDirty();
    const missingAfterPlacement = REGION_ORDER.find((region) => !finalRois.some((roi) => roi.region === region));
    const completedLabel = REGIONS.find((item) => item.key === session.region)?.label;
    if (missingAfterPlacement) {
      activateRegion(missingAfterPlacement);
      const nextLabel = REGIONS.find((item) => item.key === missingAfterPlacement)?.label;
      setMessage(`${completedLabel}已完成，已立即切换到${nextLabel}；可继续标注，当前区域的统计正在后台计算。`);
    } else {
      setMessage(`${completedLabel}已完成：三个 ROI 均已放置；拖圆内可移动位置，拖选中圆右侧手柄可调整大小。`);
    }
    window.requestAnimationFrame(bumpOverlay);
  }, [activateRegion, bumpOverlay, markDirty, setRois]);

  const cancelRoiPlacement = useCallback((sequence: SequenceKey, event: ReactPointerEvent<HTMLCanvasElement>) => {
    const session = roiPlacementRef.current;
    if (!session || session.sequence !== sequence || session.pointerId !== event.pointerId) return;
    roiPlacementRef.current = null;
    setPlacingRoi(false);
    setRois(session.originalRois);
    setMessage("本次 ROI 放置已取消。");
    window.requestAnimationFrame(bumpOverlay);
  }, [bumpOverlay, setRois]);

  const beginRoiMove = useCallback((sequence: SequenceKey, region: PdffRegion, event: ReactPointerEvent<SVGEllipseElement>) => {
    focusSequence(sequence);
    if (!canDraw || tool !== "roi" || event.button !== 0 || loading) return;
    const viewer = viewerRefs.current[sequence];
    const reference = viewerRefs.current.f;
    const canvas = canvasRefs.current[sequence];
    const roi = rois.find((item) => item.region === region);
    if (!viewer || !reference || !canvas || !roi) return;
    event.preventDefault();
    event.stopPropagation();
    event.nativeEvent.stopImmediatePropagation();
    const pointer = pointerToReference(viewer, reference, canvas, event.clientX, event.clientY);
    const centerFraction = reference.vox2frac(roi.center_voxel as unknown as Parameters<Viewer["vox2frac"]>[0]);
    const centerMillimeters = Array.from(reference.frac2mm(centerFraction)).map(Number);
    if (!pointer || centerMillimeters.length < 3 || centerMillimeters.some((value) => !Number.isFinite(value))) return;
    roiMoveRef.current = {
      pointerId: event.pointerId,
      sequence,
      region,
      radius: roi.radius_mm,
      grabOffsetMillimeters: [
        pointer.millimeters[0] - centerMillimeters[0],
        pointer.millimeters[1] - centerMillimeters[1],
        pointer.millimeters[2] - centerMillimeters[2],
      ],
      originalRois: cloneRois(rois),
      lastValidCenterVoxel: [...roi.center_voxel] as [number, number, number],
      moved: false,
    };
    activateRegion(region);
    setPlacingRoi(true);
    setError("");
    setMessage(`${REGIONS.find((item) => item.key === region)?.label}：拖动圆内色块移动圆心，半径保持 ${roi.radius_mm.toFixed(1)} mm。`);
    try { event.currentTarget.setPointerCapture(event.pointerId); } catch { /* Pointer capture is best-effort. */ }
  }, [activateRegion, canDraw, focusSequence, loading, rois, tool]);

  const updateRoiMove = useCallback((sequence: SequenceKey, event: ReactPointerEvent<SVGEllipseElement>) => {
    const session = roiMoveRef.current;
    if (!session || session.sequence !== sequence || session.pointerId !== event.pointerId) return;
    const viewer = viewerRefs.current[sequence];
    const reference = viewerRefs.current.f;
    const canvas = canvasRefs.current[sequence];
    const dimensions = reference?.volumes[0]?.dimsRAS;
    const spacing = reference?.volumes[0]?.pixDimsRAS;
    if (!viewer || !reference || !canvas || !dimensions || !spacing) return;
    event.preventDefault();
    event.stopPropagation();
    event.nativeEvent.stopImmediatePropagation();
    const pointer = pointerToReference(viewer, reference, canvas, event.clientX, event.clientY);
    if (!pointer) return;
    const targetMillimeters: [number, number, number] = [
      pointer.millimeters[0] - session.grabOffsetMillimeters[0],
      pointer.millimeters[1] - session.grabOffsetMillimeters[1],
      pointer.millimeters[2] - session.grabOffsetMillimeters[2],
    ];
    const targetFraction = reference.mm2frac(targetMillimeters as unknown as Parameters<Viewer["mm2frac"]>[0]);
    const targetVoxel = Array.from(reference.frac2vox(targetFraction)).map(Number);
    if (targetVoxel.length < 3 || targetVoxel.some((value) => !Number.isFinite(value))) return;
    const spacingX = Math.abs(Number(spacing[1]));
    const spacingY = Math.abs(Number(spacing[2]));
    if (!spacingX || !spacingY) return;
    const minimumX = session.radius / spacingX;
    const minimumY = session.radius / spacingY;
    const maximumX = Number(dimensions[1]) - 1 - minimumX;
    const maximumY = Number(dimensions[2]) - 1 - minimumY;
    const candidate: [number, number, number] = [
      Number(Math.min(maximumX, Math.max(minimumX, targetVoxel[0])).toFixed(3)),
      Number(Math.min(maximumY, Math.max(minimumY, targetVoxel[1])).toFixed(3)),
      session.lastValidCenterVoxel[2],
    ];
    const conflictingRoi = session.originalRois.find((other) => {
      if (other.region === session.region || Math.round(other.center_voxel[2]) !== Math.round(candidate[2])) return false;
      const centerDistance = Math.hypot(
        (candidate[0] - other.center_voxel[0]) * spacingX,
        (candidate[1] - other.center_voxel[1]) * spacingY,
      );
      return centerDistance < session.radius + other.radius_mm;
    });
    if (conflictingRoi) {
      setMessage(`不能与${REGIONS.find((item) => item.key === conflictingRoi.region)?.label}重叠；继续拖过该区域或移回空白位置。`);
      return;
    }
    if (candidate[0] === session.lastValidCenterVoxel[0] && candidate[1] === session.lastValidCenterVoxel[1]) return;
    session.lastValidCenterVoxel = candidate;
    session.moved = true;
    setRois((current) => current.map((roi) => roi.region === session.region ? { ...roi, center_voxel: candidate } : roi));
    setMessage(`${REGIONS.find((item) => item.key === session.region)?.label}：正在移动，半径 ${session.radius.toFixed(1)} mm 不变；松开完成。`);
    window.requestAnimationFrame(bumpOverlay);
  }, [bumpOverlay, setRois]);

  const finishRoiMove = useCallback((sequence: SequenceKey, event: ReactPointerEvent<SVGEllipseElement>) => {
    const session = roiMoveRef.current;
    if (!session || session.sequence !== sequence || session.pointerId !== event.pointerId) return;
    event.preventDefault();
    event.stopPropagation();
    event.nativeEvent.stopImmediatePropagation();
    roiMoveRef.current = null;
    setPlacingRoi(false);
    try { event.currentTarget.releasePointerCapture(event.pointerId); } catch { /* Capture may already be released. */ }
    if (!session.moved) {
      setMessage(`${REGIONS.find((item) => item.key === session.region)?.label}已选中；按住圆内色块拖动即可移动位置。`);
      return;
    }
    const finalRois = session.originalRois
      .map((roi) => roi.region === session.region ? { ...roi, center_voxel: session.lastValidCenterVoxel } : roi)
      .sort((left, right) => REGION_ORDER.indexOf(left.region) - REGION_ORDER.indexOf(right.region));
    setRois(cloneRois(finalRois));
    setHistory((current) => [...current.slice(-19), session.originalRois]);
    markDirty();
    syncViewersToVoxel(session.lastValidCenterVoxel);
    setMessage(`${REGIONS.find((item) => item.key === session.region)?.label}位置已调整；半径保持 ${session.radius.toFixed(1)} mm，统计正在重新计算。`);
    window.requestAnimationFrame(bumpOverlay);
  }, [bumpOverlay, markDirty, setRois, syncViewersToVoxel]);

  const cancelRoiMove = useCallback((sequence: SequenceKey, event: ReactPointerEvent<SVGEllipseElement>) => {
    const session = roiMoveRef.current;
    if (!session || session.sequence !== sequence || session.pointerId !== event.pointerId) return;
    event.preventDefault();
    event.stopPropagation();
    roiMoveRef.current = null;
    setPlacingRoi(false);
    setRois(session.originalRois);
    setMessage("本次 ROI 移动已取消，已恢复原位置。");
    window.requestAnimationFrame(bumpOverlay);
  }, [bumpOverlay, setRois]);

  const beginRoiResize = useCallback((sequence: SequenceKey, region: PdffRegion, event: ReactPointerEvent<SVGCircleElement>) => {
    focusSequence(sequence);
    if (!canDraw || tool !== "roi" || event.button !== 0 || loading) return;
    const viewer = viewerRefs.current[sequence];
    const reference = viewerRefs.current.f;
    const canvas = canvasRefs.current[sequence];
    const dimensions = reference?.volumes[0]?.dimsRAS;
    const spacing = reference?.volumes[0]?.pixDimsRAS;
    const roi = rois.find((item) => item.region === region);
    if (!viewer || !reference || !canvas || !dimensions || !spacing || !roi) return;
    event.preventDefault();
    event.stopPropagation();
    event.nativeEvent.stopImmediatePropagation();
    const centerFraction = reference.vox2frac(roi.center_voxel as unknown as Parameters<Viewer["vox2frac"]>[0]);
    const centerValues = Array.from(reference.frac2mm(centerFraction)).map(Number);
    if (centerValues.length < 3 || centerValues.some((value) => !Number.isFinite(value))) return;
    const originalRois = cloneRois(rois);
    const maxRadius = maximumRoiRadius(roi.center_voxel, region, originalRois, dimensions, spacing);
    if (!Number.isFinite(maxRadius) || maxRadius < MIN_RADIUS_MM) return;
    roiResizeRef.current = {
      pointerId: event.pointerId,
      sequence,
      region,
      centerVoxel: [...roi.center_voxel] as [number, number, number],
      centerMillimeters: [centerValues[0], centerValues[1], centerValues[2]],
      radius: roi.radius_mm,
      maxRadius,
      originalRois,
      resized: false,
    };
    activateRegion(region);
    setPlacingRoi(true);
    setError("");
    setMessage(`${REGIONS.find((item) => item.key === region)?.label}：拖动边缘手柄缩小或放大，最小面积 ${MIN_ROI_AREA_CM2.toFixed(1)} cm²。`);
    try { event.currentTarget.setPointerCapture(event.pointerId); } catch { /* Pointer capture is best-effort. */ }
  }, [activateRegion, canDraw, focusSequence, loading, rois, tool]);

  const updateRoiResize = useCallback((sequence: SequenceKey, event: ReactPointerEvent<SVGCircleElement>) => {
    const session = roiResizeRef.current;
    if (!session || session.sequence !== sequence || session.pointerId !== event.pointerId) return;
    const viewer = viewerRefs.current[sequence];
    const reference = viewerRefs.current.f;
    const canvas = canvasRefs.current[sequence];
    if (!viewer || !reference || !canvas) return;
    event.preventDefault();
    event.stopPropagation();
    event.nativeEvent.stopImmediatePropagation();
    const point = pointerToReference(viewer, reference, canvas, event.clientX, event.clientY);
    if (!point) return;
    const radius = normalizeRoiRadius(Math.hypot(
      point.millimeters[0] - session.centerMillimeters[0],
      point.millimeters[1] - session.centerMillimeters[1],
      point.millimeters[2] - session.centerMillimeters[2],
    ), session.maxRadius);
    if (radius === session.radius) return;
    session.radius = radius;
    session.resized = true;
    setRois((current) => current.map((roi) => roi.region === session.region ? { ...roi, radius_mm: radius } : roi));
    setMessage(`${REGIONS.find((item) => item.key === session.region)?.label}：面积 ${roiAreaCm2(radius).toFixed(2)} cm²（最小 ${MIN_ROI_AREA_CM2.toFixed(1)} cm²）；松开完成。`);
    window.requestAnimationFrame(bumpOverlay);
  }, [bumpOverlay, setRois]);

  const finishRoiResize = useCallback((sequence: SequenceKey, event: ReactPointerEvent<SVGCircleElement>) => {
    const session = roiResizeRef.current;
    if (!session || session.sequence !== sequence || session.pointerId !== event.pointerId) return;
    event.preventDefault();
    event.stopPropagation();
    event.nativeEvent.stopImmediatePropagation();
    roiResizeRef.current = null;
    setPlacingRoi(false);
    try { event.currentTarget.releasePointerCapture(event.pointerId); } catch { /* Pointer capture may already be released. */ }
    if (!session.resized) {
      setMessage(`${REGIONS.find((item) => item.key === session.region)?.label}大小未改变；拖动边缘圆点可调整。`);
      return;
    }
    const finalRois = session.originalRois
      .map((roi) => roi.region === session.region ? { ...roi, radius_mm: session.radius } : roi)
      .sort((left, right) => REGION_ORDER.indexOf(left.region) - REGION_ORDER.indexOf(right.region));
    setRois(cloneRois(finalRois));
    setHistory((current) => [...current.slice(-19), session.originalRois]);
    markDirty();
    setMessage(`${REGIONS.find((item) => item.key === session.region)?.label}大小已调整为 ${roiAreaCm2(session.radius).toFixed(2)} cm²，统计正在重新计算。`);
    window.requestAnimationFrame(bumpOverlay);
  }, [bumpOverlay, markDirty, setRois]);

  const cancelRoiResize = useCallback((sequence: SequenceKey, event: ReactPointerEvent<SVGCircleElement>) => {
    const session = roiResizeRef.current;
    if (!session || session.sequence !== sequence || session.pointerId !== event.pointerId) return;
    event.preventDefault();
    event.stopPropagation();
    roiResizeRef.current = null;
    setPlacingRoi(false);
    setRois(session.originalRois);
    setMessage("本次 ROI 大小调整已取消，已恢复原大小。");
    window.requestAnimationFrame(bumpOverlay);
  }, [bumpOverlay, setRois]);

  const chooseTool = useCallback((next: Tool) => {
    if (next === "roi" && !canDraw) return;
    setTool(next);
    setMessage(next === "roi"
      ? `ROI 编辑：单击空白处放置默认 ${DEFAULT_ROI_AREA_CM2.toFixed(1)} cm²，拖动可缩放；拖圆内移动，拖边缘圆点调整大小。最小 ${MIN_ROI_AREA_CM2.toFixed(1)} cm²。`
      : next === "windowing"
        ? "调窗：左键拖动调整当前相位窗宽/窗位；右侧可输入精确 WL / WW。"
        : "十字定位：左键定位，滚轮换层，中键平移/缩放，右键调窗；四相位同步。"
    );
  }, [canDraw]);

  const changeSlice = useCallback((requested: number) => {
    const reference = viewerRefs.current.f;
    if (!reference) return;
    const target = Math.min(slice.maximum, Math.max(0, Math.round(requested)));
    const delta = target - slice.index;
    if (delta) reference.moveCrosshairInVox(0, 0, delta);
    const millimeters = reference.frac2mm(reference.scene.crosshairPos);
    for (const key of SEQUENCE_KEYS) {
      const viewer = viewerRefs.current[key];
      if (!viewer || viewer === reference) continue;
      viewer.scene.crosshairPos = viewer.mm2frac(millimeters);
      viewer.drawScene();
    }
    setSlice({ ...slice, index: target });
    window.requestAnimationFrame(bumpOverlay);
  }, [bumpOverlay, slice]);

  const selectPlane = useCallback((planeIndex: 1 | 2 | 3) => {
    if (planeIndex === activePlaneIndex || placingRoi || roiMoveRef.current || roiResizeRef.current) return;
    const target = planes.find((plane) => plane.plane_index === planeIndex);
    setActivePlaneIndex(planeIndex);
    setHistory([]);
    const targetRois = target?.rois ?? [];
    activeRoisRef.current = cloneRois(targetRois);
    activateRegion(targetRois.length < 3
      ? REGION_ORDER.find((region) => !targetRois.some((roi) => roi.region === region)) ?? "head"
      : "head");
    if (targetRois[0]) syncViewersToVoxel(targetRois[0].center_voxel);
    setMessage(targetRois.length
      ? `已切换到测量平面 ${planeIndex}（第 ${Math.round(targetRois[0].center_voxel[2]) + 1} 层），该平面的 ROI 与统计已恢复。`
      : `测量平面 ${planeIndex} 尚未标注，请先选择一个不同于其他平面的轴位层面。`);
    setError("");
    window.requestAnimationFrame(bumpOverlay);
  }, [activateRegion, activePlaneIndex, bumpOverlay, placingRoi, planes, syncViewersToVoxel]);

  const locateRegion = useCallback((region: PdffRegion) => {
    activateRegion(region);
    const roi = rois.find((item) => item.region === region);
    if (roi) {
      syncViewersToVoxel(roi.center_voxel);
      setMessage(`已定位到${REGIONS.find((item) => item.key === region)?.label}；切换到 ROI 工具后可在任一相位移动。`);
    }
  }, [activateRegion, rois, syncViewersToVoxel]);

  const removeRegion = useCallback((region: PdffRegion) => {
    if (!rois.some((roi) => roi.region === region)) return;
    commitRois(rois.filter((roi) => roi.region !== region));
    activateRegion(region);
    setMessage(`${REGIONS.find((item) => item.key === region)?.label} 已删除，可重新放置。`);
  }, [activateRegion, commitRois, rois]);

  const clearRois = useCallback(() => {
    if (!rois.length) return;
    commitRois([]);
    activateRegion("head");
    setMessage("三个 ROI 已清空，可从胰头重新放置。");
  }, [activateRegion, commitRois, rois.length]);

  const undo = useCallback(() => {
    const previous = history.at(-1);
    if (!previous) return;
    setHistory((current) => current.slice(0, -1));
    setRois(cloneRois(previous));
    markDirty();
    if (!previous.length) {
      setMetrics([]);
      setPreviewing(false);
      setPreviewError("");
    }
    setMessage("已撤销上一次 ROI 编辑。");
    window.requestAnimationFrame(bumpOverlay);
  }, [bumpOverlay, history, markDirty, setMetrics, setRois]);

  const adjustZoom = useCallback((factor: number) => {
    for (const key of SEQUENCE_KEYS) {
      const viewer = viewerRefs.current[key];
      if (!viewer) continue;
      const pan = Array.from(viewer.scene.pan2Dxyzmm).map(Number);
      const zoom = Math.min(8, Math.max(0.35, (pan[3] || 1) * factor));
      viewer.setPan2Dxyzmm([pan[0] || 0, pan[1] || 0, pan[2] || 0, zoom] as unknown as Parameters<Viewer["setPan2Dxyzmm"]>[0]);
    }
    setMessage(factor > 1 ? "四相位视图已同步放大。" : "四相位视图已同步缩小。");
    window.requestAnimationFrame(bumpOverlay);
  }, [bumpOverlay]);

  const resetView = useCallback(() => {
    for (const key of SEQUENCE_KEYS) {
      viewerRefs.current[key]?.setPan2Dxyzmm([0, 0, 0, 1] as unknown as Parameters<Viewer["setPan2Dxyzmm"]>[0]);
    }
    setMessage("四相位缩放与平移已复位。");
    window.requestAnimationFrame(bumpOverlay);
  }, [bumpOverlay]);

  const resetWindow = useCallback(() => {
    const viewer = viewerRefs.current[activeSequence];
    if (!viewer || !restoreVolumeWindow(viewer)) return;
    syncWindowInputs(viewer);
    setMessage(`${SEQUENCES.find((item) => item.key === activeSequence)?.label} 的窗宽/窗位已复位。`);
  }, [activeSequence, syncWindowInputs]);

  const applyNumericWindow = useCallback(() => {
    const viewer = viewerRefs.current[activeSequence];
    const center = Number(windowInputs.center.trim());
    const width = Number(windowInputs.width.trim());
    if (!viewer || !Number.isFinite(center) || !Number.isFinite(width) || width <= 0) {
      setMessage("WL 需要是数字，WW 必须大于 0。");
      return;
    }
    applyVolumeWindow(viewer, center, width);
    syncWindowInputs(viewer);
    setMessage(`${SEQUENCES.find((item) => item.key === activeSequence)?.label} 已应用 WL ${formatWindowValue(center)} / WW ${formatWindowValue(width)}。`);
  }, [activeSequence, syncWindowInputs, windowInputs]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.matches("input, textarea, select")) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") {
        event.preventDefault();
        undo();
      } else if (event.key.toLowerCase() === "r" && canDraw) chooseTool("roi");
      else if (event.key.toLowerCase() === "c") chooseTool("navigate");
      else if (event.key.toLowerCase() === "w") chooseTool("windowing");
      else if ((event.key === "Delete" || event.key === "Backspace") && canDraw) removeRegion(activeRegion);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [activeRegion, canDraw, chooseTool, removeRegion, undo]);

  const completedPlanes = planes.filter((plane) => plane.rois.length === 3);
  const hasPartialPlane = planes.some((plane) => plane.rois.length > 0 && plane.rois.length < 3);
  const savedPlaneCount = initialPlanes.length || (initialRois.length === 3 ? 1 : 0);

  const save = async () => {
    if (hasPartialPlane) {
      setError("每个开始测量的平面都需要完成胰头、胰体、胰尾三个 ROI 后才能保存");
      return;
    }
    if (!completedPlanes.length) {
      setError("请先完成至少一个测量平面的三个 ROI");
      return;
    }
    const planeSlices = completedPlanes.map((plane) => {
      const slices = new Set(plane.rois.map((roi) => Math.round(roi.center_voxel[2])));
      return slices.size === 1 ? [...slices][0] : null;
    });
    if (planeSlices.some((value) => value === null)) {
      setError("每个测量平面的三个 ROI 必须位于同一轴位层面");
      return;
    }
    if (new Set(planeSlices).size !== planeSlices.length) {
      setError("三个测量平面必须选择三个不同的轴位层面");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const endpoint = reviewMode ? "pdff-review-revision" : "pdff-annotation";
      const comment = reviewMode
        ? saveComment.trim() || `审核微调 · 基于提交版本 v${reviewBaseVersion ?? annotationVersion ?? 0}`
        : `Dixon 四相位三平面测量 · 已完成 ${completedPlanes.length}/3 · 层面 ${planeSlices.map((value) => Number(value) + 1).join("、")}`;
      const result = await api<AnnotationVersionDetail>(`/api/cases/${projectCaseId}/${endpoint}`, {
        method: "POST",
        body: JSON.stringify({
          planes: completedPlanes.map((plane) => ({
            plane_index: plane.plane_index,
            rois: [...plane.rois].sort((left, right) => REGION_ORDER.indexOf(left.region) - REGION_ORDER.indexOf(right.region)),
          })),
          comment,
        }),
      }, csrfToken);
      const normalizedResult = normalizePlanes(result.planes, result.rois ?? [], result.metrics ?? []);
      setPlanes(normalizedResult);
      activeRoisRef.current = cloneRois(normalizedResult.find((plane) => plane.plane_index === activePlaneIndex)?.rois ?? []);
      setHistory([]);
      setDirty(false);
      onDirtyChangeRef.current?.(false);
      const nextCount = result.case_summary?.plane_count ?? completedPlanes.length;
      setMessage(reviewMode
        ? `审核修订版 v${result.version} 已保存；${nextCount} 个平面的 ROI 与 PDFF 已重新计算。`
        : `PDFF 草稿 v${result.version} 已保存；当前完成 ${nextCount}/3 个独立测量平面。`);
      onSaved(result.version);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "PDFF ROI 保存失败");
    } finally {
      setSaving(false);
    }
  };

  const metricByRegion = useMemo(() => Object.fromEntries(metrics.map((metric) => [metric.region, metric])) as Partial<Record<PdffRegion, PdffRoiMetric>>, [metrics]);
  const roiSlices = new Set(rois.map((roi) => Math.round(roi.center_voxel[2])));
  const placedMetrics = REGION_ORDER
    .map((region) => metricByRegion[region])
    .filter((metric): metric is PdffRoiMetric => Boolean(metric && rois.some((roi) => roi.region === metric.region)));
  const fatWaterValues = placedMetrics
    .map(fatWaterPdff)
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  const inOpposedValues = placedMetrics
    .map(inOpposedPdff)
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value));
  const fatWaterSummary = fatWaterValues.length ? fatWaterValues.reduce((sum, value) => sum + value, 0) / fatWaterValues.length : undefined;
  const inOpposedSummary = inOpposedValues.length ? inOpposedValues.reduce((sum, value) => sum + value, 0) / inOpposedValues.length : undefined;
  const caseSummary = buildCaseSummary(planes);
  const canSaveProgress = !hasPartialPlane && completedPlanes.length > 0 && (dirty || completedPlanes.length !== savedPlaneCount);
  const savedPlaneIndices = new Set<number>(initialPlanes.map((plane) => plane.plane_index));
  if (!savedPlaneIndices.size && initialRois.length === 3) savedPlaneIndices.add(1);
  const pancreasReferenceStatus = pancreasReferenceLoading
    ? "正在预载整例胰腺参考线…"
    : pancreasReferenceInfo?.available
      ? !showPancreasReference
        ? "AI 胰腺参考线已隐藏"
        : pancreasReferenceSegments.length
          ? "AI 胰腺参考线 · 已整例预载，仅辅助定位，不限制 ROI 范围"
          : "当前层没有胰腺参考轮廓 · 不限制 ROI 范围"
      : pancreasReferenceInfo?.reason || "本病例没有可用的胰腺参考分割";

  return <section className="pdff-viewer-shell" aria-label="PDFF 四相位三平面三点 ROI 标注工作台">
    <div className="pdff-toolbar">
      <div className="tool-group" aria-label="ROI 与影像工具">
        <button className={tool === "roi" ? "tool-button active accent" : "tool-button"} disabled={!canDraw || loading} onClick={() => chooseTool("roi")} title="快捷键 R" type="button">圆形 ROI</button>
        <button className={tool === "navigate" ? "tool-button active" : "tool-button"} disabled={loading} onClick={() => chooseTool("navigate")} title="快捷键 C" type="button">十字定位</button>
        <button className={tool === "windowing" ? "tool-button active" : "tool-button"} disabled={loading} onClick={() => chooseTool("windowing")} title="快捷键 W" type="button">窗宽/窗位</button>
        <button className="tool-button icon-tool" disabled={loading} onClick={() => adjustZoom(1.2)} title="同步放大四相位" type="button">＋ 放大</button>
        <button className="tool-button icon-tool" disabled={loading} onClick={() => adjustZoom(1 / 1.2)} title="同步缩小四相位" type="button">− 缩小</button>
        <button className="tool-button" disabled={loading} onClick={resetView} type="button">适配视图</button>
        <button className="tool-button" disabled={loading} onClick={resetWindow} type="button">复位调窗</button>
        <button className="tool-button" disabled={!canDraw || !history.length} onClick={undo} title="Ctrl+Z" type="button">撤销</button>
        <button className={showRois ? "tool-button active" : "tool-button"} disabled={loading} onClick={() => setShowRois((current) => !current)} type="button">{showRois ? "隐藏 ROI" : "显示 ROI"}</button>
        <button
          aria-pressed={pancreasReferenceInfo?.available ? showPancreasReference : undefined}
          className={pancreasReferenceInfo?.available && showPancreasReference ? "tool-button active pancreas-reference-toggle" : "tool-button pancreas-reference-toggle"}
          disabled={pancreasReferenceLoading || !pancreasReferenceInfo?.available}
          onClick={() => setShowPancreasReference((current) => !current)}
          title={pancreasReferenceInfo?.available ? "显示或隐藏 AI 胰腺参考轮廓；该轮廓不会限制 ROI" : pancreasReferenceInfo?.reason}
          type="button"
        >{pancreasReferenceLoading ? "预载胰腺参考…" : pancreasReferenceInfo?.available ? showPancreasReference ? "隐藏胰腺参考" : "显示胰腺参考" : "无胰腺参考"}</button>
      </div>
      <div className="pdff-formula"><span>PAIR × 2</span><strong>F/W PDFF + In/Opp 估算</strong></div>
      {canDraw && <button className="primary compact" disabled={saving || loading || !canSaveProgress} onClick={() => void save()} type="button">{saving ? "计算并保存中…" : dirty ? `保存测量进度 ${completedPlanes.length}/3` : "测量进度已保存"}</button>}
    </div>
    <div className="pdff-guidance">
      <span>i</span><p>{message}</p>
      <div className="pdff-window-editor">
        <strong>{SEQUENCES.find((item) => item.key === activeSequence)?.short}</strong>
        <label>WL<input aria-label="PDFF 窗位 WL" inputMode="decimal" onChange={(event) => setWindowInputs((current) => ({ ...current, center: event.target.value }))} onKeyDown={(event) => { if (event.key === "Enter") applyNumericWindow(); }} type="number" value={windowInputs.center} /></label>
        <label>WW<input aria-label="PDFF 窗宽 WW" inputMode="decimal" min="0.1" onChange={(event) => setWindowInputs((current) => ({ ...current, width: event.target.value }))} onKeyDown={(event) => { if (event.key === "Enter") applyNumericWindow(); }} type="number" value={windowInputs.width} /></label>
        <button onClick={applyNumericWindow} type="button">应用</button>
      </div>
      <em>平面 {activePlaneIndex}/3 · {slice.index + 1}/{slice.maximum + 1} · {rois.length}/3 ROI</em>
    </div>
    {consoleHost && createPortal(<aside className="pdff-roi-console" aria-label="三平面 ROI 操作台">
        <header>
          <div><span>ROI CONSOLE</span><strong>三平面 ROI 操作台</strong></div>
          <b>{completedPlanes.length}/3</b>
        </header>
        <p>每个平面分别放置胰头、胰体、胰尾三个 ROI；三个平面必须选择不同轴位层。每完成一个平面即可保存，最终按三个平面均值汇总。</p>
        <nav className="pdff-plane-tabs" aria-label="PDFF 测量平面">{planes.map((plane) => {
          const completed = plane.rois.length === 3;
          const planeSlice = plane.rois[0] ? Math.round(plane.rois[0].center_voxel[2]) + 1 : null;
          const saved = savedPlaneIndices.has(plane.plane_index);
          return <button aria-current={plane.plane_index === activePlaneIndex ? "step" : undefined} className={`${plane.plane_index === activePlaneIndex ? "active" : ""} ${completed ? "completed" : ""}`} key={plane.plane_index} onClick={() => selectPlane(plane.plane_index)} type="button">
            <span>平面 {plane.plane_index}</span><strong>{planeSlice ? `第 ${planeSlice} 层` : "待选层"}</strong><small>{saved ? "已保存" : completed ? "待保存" : `${plane.rois.length}/3 ROI`}</small>
          </button>;
        })}</nav>
        <div className="pdff-roi-strip">{REGIONS.map((region) => {
          const roi = rois.find((item) => item.region === region.key);
          const metric = metricByRegion[region.key];
          return <article className={`${activeRegion === region.key ? "active" : ""} ${roi ? "placed" : ""}`} key={region.key} style={{ "--roi-color": region.color } as CSSProperties}>
            <button aria-current={activeRegion === region.key ? "true" : undefined} onClick={() => locateRegion(region.key)} title={roi ? "选中并定位到该 ROI" : "选中后在影像上拖动放置"} type="button"><i>{roi ? "✓" : "+"}</i><span><strong>{region.label}</strong><small>{roi ? metric ? `面积 ${(metric.area_mm2 / 100).toFixed(2)} cm² · ${metric.voxel_count} 像素 · 第 ${Math.round(roi.center_voxel[2]) + 1} 层` : `几何面积 ${roiAreaCm2(roi.radius_mm).toFixed(2)} cm² · 统计计算中` : "在任一相位按住拖动放置"}</small></span>{roi && <em>{metric ? "已测量" : previewing ? "计算中" : "待计算"}</em>}</button>
            {canDraw && roi && <div className="pdff-roi-actions"><span>拖圆内移动 · 拖边缘圆点缩放</span><button aria-label={`删除${region.label}`} className="delete-roi" onClick={() => removeRegion(region.key)} title="删除该 ROI" type="button">删除</button></div>}
            {roi && metric && <section className="pdff-roi-metrics" aria-label={`${region.label} 实时统计`}>
              <div className="pdff-pair-values">
                <div><span>Fat / Water</span><strong>{formatValue(fatWaterPdff(metric))}%</strong><small>逐像素 {formatValue(metric.pdff.mean)}% · SD {formatValue(metric.pdff.sd)}%</small></div>
                <div><span>In / Opposed 估算</span><strong>{formatValue(inOpposedPdff(metric))}%</strong><small>逐像素 {formatValue(metric.pdff_in_opposed?.mean)}% · SD {formatValue(metric.pdff_in_opposed?.sd)}%</small></div>
              </div>
              <div className="pdff-pixel-count"><span>ROI 内有效像素</span><strong>{metric.voxel_count}</strong><small>{metric.area_mm2.toFixed(1)} mm²</small></div>
              <table className="pdff-stat-table">
                <thead><tr><th>相位</th><th>均值</th><th>SD</th><th>方差</th></tr></thead>
                <tbody>{([
                  ["Fat", metric.fat],
                  ["Water", metric.water],
                  ["In", metric.in_phase],
                  ["Opp", metric.opposed_phase],
                ] as Array<[string, NumericStats | undefined]>).map(([label, stats]) => <tr key={label}><th>{label}</th><td>{formatValue(stats?.mean, 2)}</td><td>{formatValue(stats?.sd, 2)}</td><td>{formatValue(varianceOf(stats), 2)}</td></tr>)}</tbody>
              </table>
            </section>}
          </article>;
        })}</div>
        <section className="pdff-live-summary" aria-label="PDFF 预计算结果">
          <header><div><span>LIVE CALCULATION</span><strong>PDFF 预计算</strong></div><em>{previewing ? "计算中…" : dirty ? "未保存" : annotationVersion ? `v${annotationVersion}` : "实时"}</em></header>
          <div>
            <article><span>Fat / Water</span><strong>{formatValue(fatWaterSummary)}%</strong><small>{fatWaterValues.length === 3 ? "三点等权平均" : `${fatWaterValues.length}/3 ROI 平均`}</small></article>
            <article><span>In / Opposed 估算</span><strong>{formatValue(inOpposedSummary)}%</strong><small>{inOpposedValues.length === 3 ? "三点等权平均" : `${inOpposedValues.length}/3 ROI 平均`}</small></article>
          </div>
          {previewError && <p>{previewError}</p>}
        </section>
        {caseSummary && <section className="pdff-case-summary" aria-label="病例级 PDFF 与层间一致性">
          <header><div><span>CASE AGGREGATION</span><strong>病例级结果与一致性</strong></div><em>{caseSummary.plane_count}/3 平面</em></header>
          <div className="pdff-case-values">
            <article><span>Fat / Water 主结果</span><strong>{formatValue(caseSummary.fat_water.mean ?? undefined)}%</strong><small>各平面三点均值再平均</small></article>
            <article><span>In / Opposed 辅助</span><strong>{formatValue(caseSummary.in_opposed.mean ?? undefined)}%</strong><small>信号法估算，单独报告</small></article>
          </div>
          <dl className="pdff-consistency-grid">
            <div><dt>层间 SD</dt><dd>{caseSummary.plane_count > 1 ? `${formatValue(caseSummary.fat_water.sd ?? undefined)}%` : "待更多平面"}</dd></div>
            <div><dt>层间 CV</dt><dd>{caseSummary.plane_count > 1 ? `${formatValue(caseSummary.fat_water.cv_percent ?? undefined)}%` : "待更多平面"}</dd></div>
            <div><dt>层间极差</dt><dd>{caseSummary.plane_count > 1 ? `${formatValue(caseSummary.fat_water.range ?? undefined)}%` : "待更多平面"}</dd></div>
            <div><dt>累计采样</dt><dd>{caseSummary.total_voxel_count} 像素</dd></div>
          </dl>
          <table className="pdff-plane-result-table">
            <thead><tr><th>平面</th><th>层面</th><th>F/W</th><th>In/Opp</th><th>面积</th><th>像素</th></tr></thead>
            <tbody>{planes.filter((plane) => plane.summary).map((plane) => <tr key={plane.plane_index}><th>P{plane.plane_index}</th><td>{(plane.slice_index ?? Math.round(plane.rois[0].center_voxel[2])) + 1}</td><td>{formatValue(plane.summary?.fat_water_mean)}%</td><td>{formatValue(plane.summary?.in_opposed_mean)}%</td><td>{formatValue(Number(plane.summary?.total_area_mm2) / 100, 2)} cm²</td><td>{plane.summary?.total_voxel_count}</td></tr>)}</tbody>
          </table>
          <p>一致性为同一病例三个平面均值之间的样本 SD、变异系数（CV）与极差；不是跨医生 ICC。</p>
        </section>}
        <section className="pdff-formula-panel" aria-label="PDFF 计算公式">
          <header><span>FORMULA</span><strong>计算公式与口径</strong></header>
          <div><b>① Fat / Water</b><code>μFat ÷ (μFat + μWater) × 100%</code></div>
          <div><b>② In / Opposed 信号法估算</b><code>|μIn − μOpp| ÷ (2 × |μIn|) × 100%</code></div>
          <p>主结果使用 ROI 信号均值代入公式；表内同时显示逐像素结果的均值与 SD。第二式基于双点 Dixon、水信号占优假设，仅为 signal-FF 估算，不等同于多回波校正 PDFF；两式均不做范围截断。</p>
        </section>
        <footer>
          <span>当前选择</span>
          <strong>平面 {activePlaneIndex} · {REGIONS.find((region) => region.key === activeRegion)?.label}</strong>
        </footer>
        <button className="pdff-clear" disabled={!canDraw || !rois.length} onClick={clearRois} type="button">清空当前平面三点</button>
      </aside>, consoleHost)}
    <div className="pdff-workspace-body">
      <div className="pdff-image-stage">
        <div className="pdff-pair-stack">
          {SEQUENCE_PAIRS.map((pair) => <section className="pdff-sequence-pair" key={pair.key}>
        <header><strong>{pair.label}</strong><span>{pair.hint}</span></header>
        <div className="pdff-canvas-grid">
          {pair.sequences.map((key) => {
            const sequence = SEQUENCES.find((item) => item.key === key)!;
            return <div className={`pdff-canvas-panel${activeSequence === key ? " active" : ""}`} key={key} onPointerEnter={() => focusSequence(key)} onPointerMove={() => { if (tool !== "roi") bumpOverlay(); }} onWheel={() => window.requestAnimationFrame(bumpOverlay)}>
              <header><div><i className={sequence.tone} />{sequence.label}</div><span>{key === "f" || key === "w" ? "PDFF 配对" : "信号配对"}</span></header>
              <canvas onPointerCancelCapture={(event) => cancelRoiPlacement(key, event)} onPointerDownCapture={(event) => beginRoiPlacement(key, event)} onPointerMoveCapture={(event) => updateRoiPlacement(key, event)} onPointerUpCapture={(event) => finishRoiPlacement(key, event)} ref={(canvas) => { canvasRefs.current[key] = canvas; }} tabIndex={0} />
              <RoiOverlay
                activeRegion={activeRegion}
                canMove={canDraw && tool === "roi" && !loading}
                geometry={overlays[key]}
                onMove={updateRoiMove}
                onMoveCancel={cancelRoiMove}
                onMoveEnd={finishRoiMove}
                onMoveStart={beginRoiMove}
                onResize={updateRoiResize}
                onResizeCancel={cancelRoiResize}
                onResizeEnd={finishRoiResize}
                onResizeStart={beginRoiResize}
                sequence={key}
              />
              {slice.maximum > 0 && <label className="pdff-panel-slider" title={`拖动切换${sequence.label}轴位切片；四相位同步`}>
                <span>{slice.index + 1}/{slice.maximum + 1}</span>
                <input aria-label={`${sequence.label}轴位切片`} aria-valuetext={`第 ${slice.index + 1} 层，共 ${slice.maximum + 1} 层`} max={slice.maximum} min="0" onInput={(event) => changeSlice(event.currentTarget.valueAsNumber)} step="1" type="range" value={slice.index} />
              </label>}
              {loadStates[key] === "loading" && <div className="viewer-overlay"><span className="spinner" />载入 {sequence.label} 原始影像…</div>}
              {loadStates[key] === "error" && <div className="viewer-overlay">{sequence.label} 加载失败</div>}
            </div>;
          })}
        </div>
          </section>)}
        </div>
        <div className="pdff-layer-status"><span>{roiSlices.size > 1 ? `平面 ${activePlaneIndex} 的 ROI 分布在不同层，请统一` : rois.length ? `测量平面 ${activePlaneIndex} · 四相位 ROI 位于第 ${[...roiSlices][0] + 1} 层` : `测量平面 ${activePlaneIndex} · 请选择不同于其他平面的轴位层面`}</span><div><small className={pancreasReferenceInfo?.available ? "available" : ""}>{pancreasReferenceStatus}</small><small>已完成 {completedPlanes.length}/3 平面 · 四相位同步</small></div></div>
      </div>
    </div>
    {error && <div className="viewer-error pdff-error">{error}</div>}
  </section>;
}
