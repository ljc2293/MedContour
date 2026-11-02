"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { prefetchImageBuffer, releaseImageBuffer } from "../lib/imagePrefetch";

type ViewerProps = {
  projectCaseId: string;
  csrfToken: string;
  canDraw: boolean;
  annotationVersion: number | null;
  reworkBaseVersion?: number | null;
  reviewMode?: boolean;
  reviewBaseVersion?: number | null;
  saveComment?: string;
  onDirtyChange?: (dirty: boolean) => void;
  onSaved: (version: number) => void;
};

const SEQUENCES = [
  { key: "w", label: "水相 W" },
  { key: "f", label: "脂相 F" },
  { key: "in", label: "同相 In" },
  { key: "opp", label: "反相 Opp" },
] as const;

const VIEW_LABELS = [
  { key: "axial", label: "轴位", abbreviation: "AX" },
  { key: "sagittal", label: "矢状位", abbreviation: "SAG" },
  { key: "render", label: "三维", abbreviation: "3D" },
  { key: "coronal", label: "冠状位", abbreviation: "COR" },
] as const;

type Viewer = import("@niivue/niivue").Niivue;
type DragModes = typeof import("@niivue/niivue").DRAG_MODE;
type ViewerVolume = Viewer["volumes"][number];
type SequenceKey = (typeof SEQUENCES)[number]["key"];
type SequenceLoadState = "idle" | "loading" | "loaded" | "error";
type SequenceLoader = (key: SequenceKey) => Promise<ViewerVolume>;
type ViewKey = (typeof VIEW_LABELS)[number]["key"];
type SliceViewKey = Exclude<ViewKey, "render">;
type Tool = "navigate" | "windowing" | "contour" | "erase";
type WindowLevel = { center: number; width: number };
type SlicePosition = { index: number; maximum: number };
type SlicePositions = Record<SliceViewKey, SlicePosition>;
type Point2D = [number, number];
const DEFAULT_DRAW_OPACITY = 55;
const DEFAULT_PEN_LEVEL = 1;
const DEFAULT_CURSOR_OPACITY = 70;
const PEN_SIZES = [1, 3, 5, 7] as const;
const CURSOR_OPACITY_OPTIONS = [40, 55, 70, 85, 100] as const;
const SLICE_VIEWS: SliceViewKey[] = ["axial", "sagittal", "coronal"];
const SLICE_AXES: Record<SliceViewKey, 0 | 1 | 2> = {
  sagittal: 0,
  coronal: 1,
  axial: 2,
};
const EMPTY_SLICE_POSITIONS: SlicePositions = {
  axial: { index: 0, maximum: 0 },
  sagittal: { index: 0, maximum: 0 },
  coronal: { index: 0, maximum: 0 },
};

function newSequenceLoadStates(): Record<SequenceKey, SequenceLoadState> {
  return { w: "idle", f: "idle", in: "idle", opp: "idle" };
}

type DashedCrosshairViewer = Viewer & {
  __dashedCrosshairVisible?: boolean;
  __solidCrosshairRenderer?: Viewer["drawCrosshairs3D"];
};
type DrawingLocationLockViewer = Viewer & {
  __drawingLocationLockInstalled?: boolean;
  __unlockedMouseClick?: Viewer["mouseClick"];
};
const STANDARD_POSITIONS: Record<ViewKey, [number, number, number, number]> = {
  axial: [0, 0, 0.5, 0.5],
  sagittal: [0.5, 0, 0.5, 0.5],
  render: [0, 0.5, 0.5, 0.5],
  coronal: [0.5, 0.5, 0.5, 0.5],
};

function sliceTypeForView(nv: Viewer, view: ViewKey) {
  if (view === "axial") return nv.sliceTypeAxial;
  if (view === "sagittal") return nv.sliceTypeSagittal;
  if (view === "coronal") return nv.sliceTypeCoronal;
  return nv.sliceTypeRender;
}

function applyViewLayout(nv: Viewer, focusedView: ViewKey | null) {
  const views = focusedView ? [focusedView] : VIEW_LABELS.map((view) => view.key);
  nv.setCustomLayout(views.map((view) => ({
    sliceType: sliceTypeForView(nv, view),
    position: focusedView ? [0, 0, 1, 1] : STANDARD_POSITIONS[view],
  })));
}

function configureMouse(nv: Viewer, modes: DragModes, isWindowingPrimary: boolean) {
  nv.setMouseEventConfig({
    leftButton: { primary: isWindowingPrimary ? modes.windowing : modes.crosshair },
    rightButton: isWindowingPrimary ? modes.crosshair : modes.windowing,
    centerButton: modes.pan,
  });
}

function penSizeForLevel(level: number) {
  return PEN_SIZES[Math.min(PEN_SIZES.length - 1, Math.max(0, level - 1))];
}

function contourCursorForLevel(level: number, opacityPercentage: number) {
  const radius = 4 + (Math.min(PEN_SIZES.length, Math.max(1, level)) - 1) * 1.5;
  const opacity = Math.min(100, Math.max(0, opacityPercentage)) / 100;
  const fillOpacity = (opacity * 0.2).toFixed(3);
  const strokeOpacity = opacity.toFixed(3);
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32"><circle cx="16" cy="16" r="${radius}" fill="#2dd4bf" fill-opacity="${fillOpacity}" stroke="#9ff4e8" stroke-opacity="${strokeOpacity}" stroke-width=".8"/><circle cx="16" cy="16" r=".75" fill="#d8fffa" fill-opacity="${strokeOpacity}"/></svg>`;
  return `url("data:image/svg+xml,${encodeURIComponent(svg)}") 16 16, crosshair`;
}

function projectWorldPoint(point: number[], matrix: ArrayLike<number>, viewport: number[], canvasHeight: number): Point2D | null {
  const [x, y, z] = point;
  const clipX = matrix[0] * x + matrix[4] * y + matrix[8] * z + matrix[12];
  const clipY = matrix[1] * x + matrix[5] * y + matrix[9] * z + matrix[13];
  const clipW = matrix[3] * x + matrix[7] * y + matrix[11] * z + matrix[15];
  if (!Number.isFinite(clipX) || !Number.isFinite(clipY) || !Number.isFinite(clipW) || Math.abs(clipW) < 1e-6) return null;
  return [
    viewport[0] + ((clipX / clipW + 1) * viewport[2]) / 2,
    canvasHeight - (viewport[1] + ((clipY / clipW + 1) * viewport[3]) / 2),
  ];
}

function clipLineToRect(start: Point2D, end: Point2D, rect: [number, number, number, number]): [Point2D, Point2D] | null {
  const [left, top, right, bottom] = rect;
  const dx = end[0] - start[0];
  const dy = end[1] - start[1];
  let low = 0;
  let high = 1;
  const tests: Array<[number, number]> = [
    [-dx, start[0] - left],
    [dx, right - start[0]],
    [-dy, start[1] - top],
    [dy, bottom - start[1]],
  ];
  for (const [p, q] of tests) {
    if (Math.abs(p) < 1e-6) {
      if (q < 0) return null;
      continue;
    }
    const ratio = q / p;
    if (p < 0) low = Math.max(low, ratio);
    else high = Math.min(high, ratio);
    if (low > high) return null;
  }
  return [
    [start[0] + low * dx, start[1] + low * dy],
    [start[0] + high * dx, start[1] + high * dy],
  ];
}

function drawDashedScreenLine(nv: Viewer, start: Point2D, end: Point2D, color: number[]) {
  const dx = end[0] - start[0];
  const dy = end[1] - start[1];
  const length = Math.hypot(dx, dy);
  if (length < 2) return;
  const dpr = Math.max(1, Number(nv.uiData.dpr) || 1);
  const dashLength = 8 * dpr;
  const gapLength = 5 * dpr;
  const step = dashLength + gapLength;
  const unitX = dx / length;
  const unitY = dy / length;
  for (let offset = 0; offset < length; offset += step) {
    const dashEnd = Math.min(offset + dashLength, length);
    nv.drawLine([
      start[0] + unitX * offset,
      start[1] + unitY * offset,
      start[0] + unitX * dashEnd,
      start[1] + unitY * dashEnd,
    ], Math.max(1, dpr), color);
  }
}

export function installDashedCrosshair(nv: Viewer, initiallyVisible: boolean) {
  const viewer = nv as DashedCrosshairViewer;
  if (viewer.__solidCrosshairRenderer) {
    viewer.__dashedCrosshairVisible = initiallyVisible;
    return;
  }
  viewer.__dashedCrosshairVisible = initiallyVisible;
  viewer.__solidCrosshairRenderer = viewer.drawCrosshairs3D.bind(viewer);
  viewer.drawCrosshairs3D = (isDepthTest = true, alpha = 1, mvpMatrix = null, is2DView = false, isSliceMM = true) => {
    if (!viewer.__dashedCrosshairVisible || isDepthTest) return;
    if (!mvpMatrix) {
      viewer.__solidCrosshairRenderer?.(isDepthTest, alpha, mvpMatrix, is2DView, isSliceMM);
      return;
    }

    const gl = viewer.gl;
    const viewport = Array.from(gl.getParameter(gl.VIEWPORT) as Int32Array);
    const top = gl.canvas.height - viewport[1] - viewport[3];
    const clipRect: [number, number, number, number] = [viewport[0], top, viewport[0] + viewport[2], top + viewport[3]];
    const millimeters = Array.from(viewer.frac2mm(viewer.scene.crosshairPos, 0, isSliceMM));
    const [minimum, maximum] = viewer.sceneExtentsMinMax(isSliceMM);
    const center = projectWorldPoint(millimeters, mvpMatrix, viewport, gl.canvas.height);
    if (!center) return;

    const axisEndpoints = [
      [[minimum[0], millimeters[1], millimeters[2]], [maximum[0], millimeters[1], millimeters[2]]],
      [[millimeters[0], minimum[1], millimeters[2]], [millimeters[0], maximum[1], millimeters[2]]],
      [[millimeters[0], millimeters[1], minimum[2]], [millimeters[0], millimeters[1], maximum[2]]],
    ];
    const color = Array.from(viewer.opts.crosshairColor);
    color[3] = 0.82;
    const wasDepthTest = gl.isEnabled(gl.DEPTH_TEST);
    const wasBlend = gl.isEnabled(gl.BLEND);
    const blendState = [
      gl.getParameter(gl.BLEND_SRC_RGB),
      gl.getParameter(gl.BLEND_DST_RGB),
      gl.getParameter(gl.BLEND_SRC_ALPHA),
      gl.getParameter(gl.BLEND_DST_ALPHA),
    ];
    gl.viewport(0, 0, gl.canvas.width, gl.canvas.height);
    gl.disable(gl.DEPTH_TEST);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);
    for (const [worldStart, worldEnd] of axisEndpoints) {
      const projectedStart = projectWorldPoint(worldStart, mvpMatrix, viewport, gl.canvas.height);
      const projectedEnd = projectWorldPoint(worldEnd, mvpMatrix, viewport, gl.canvas.height);
      if (!projectedStart || !projectedEnd || Math.hypot(projectedEnd[0] - projectedStart[0], projectedEnd[1] - projectedStart[1]) < 2) continue;
      const firstHalf = clipLineToRect(center, projectedStart, clipRect);
      const secondHalf = clipLineToRect(center, projectedEnd, clipRect);
      if (firstHalf) drawDashedScreenLine(viewer, firstHalf[0], firstHalf[1], color);
      if (secondHalf) drawDashedScreenLine(viewer, secondHalf[0], secondHalf[1], color);
    }
    gl.viewport(viewport[0], viewport[1], viewport[2], viewport[3]);
    gl.blendFuncSeparate(blendState[0], blendState[1], blendState[2], blendState[3]);
    if (!wasBlend) gl.disable(gl.BLEND);
    if (wasDepthTest) gl.enable(gl.DEPTH_TEST);
  };
}

export function setCrosshairVisible(nv: Viewer, visible: boolean) {
  (nv as DashedCrosshairViewer).__dashedCrosshairVisible = visible;
  nv.drawScene();
}

function installDrawingLocationLock(nv: Viewer) {
  const viewer = nv as DrawingLocationLockViewer;
  if (viewer.__drawingLocationLockInstalled) return;
  const unlockedMouseClick = viewer.mouseClick.bind(viewer);
  viewer.__drawingLocationLockInstalled = true;
  viewer.__unlockedMouseClick = unlockedMouseClick;
  viewer.mouseClick = (x, y, posChange = 0, isDelta = true) => {
    const shouldLockLocation = viewer.opts.drawingEnabled && posChange === 0 && isDelta;
    if (!shouldLockLocation) return unlockedMouseClick(x, y, posChange, isDelta);

    const lockedPosition = Array.from(viewer.scene.crosshairPos);
    const onLocationChange = viewer.onLocationChange;
    viewer.onLocationChange = () => {};
    try {
      return unlockedMouseClick(x, y, posChange, isDelta);
    } finally {
      viewer.scene.crosshairPos[0] = lockedPosition[0];
      viewer.scene.crosshairPos[1] = lockedPosition[1];
      viewer.scene.crosshairPos[2] = lockedPosition[2];
      viewer.onLocationChange = onLocationChange;
      viewer.drawScene();
    }
  };
}

function readWindowLevel(nv: Viewer): WindowLevel | null {
  const volume = nv.volumes[0];
  if (!volume || !Number.isFinite(volume.cal_min) || !Number.isFinite(volume.cal_max)) return null;
  const minimum = Number(volume.cal_min);
  const maximum = Number(volume.cal_max);
  return { center: (minimum + maximum) / 2, width: Math.max(0, maximum - minimum) };
}

function formatIntensity(value: number) {
  return Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(1);
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

function readSlicePositions(nv: Viewer): SlicePositions | null {
  const dimensions = nv.volumes[0]?.dimsRAS;
  if (!dimensions || dimensions.length < 4) return null;
  const voxels = Array.from(nv.frac2vox(nv.scene.crosshairPos));
  const positionFor = (view: SliceViewKey): SlicePosition => {
    const axis = SLICE_AXES[view];
    const maximum = Math.max(0, Math.trunc(Number(dimensions[axis + 1])) - 1);
    const index = Math.min(maximum, Math.max(0, Math.round(Number(voxels[axis]))));
    return { index, maximum };
  };
  return {
    axial: positionFor("axial"),
    sagittal: positionFor("sagittal"),
    coronal: positionFor("coronal"),
  };
}

function slicePositionsEqual(left: SlicePositions, right: SlicePositions) {
  return SLICE_VIEWS.every((view) => left[view].index === right[view].index && left[view].maximum === right[view].maximum);
}

export function NiiVueViewer({
  projectCaseId,
  csrfToken,
  canDraw,
  annotationVersion,
  reworkBaseVersion = null,
  reviewMode = false,
  reviewBaseVersion = null,
  saveComment = "",
  onDirtyChange,
  onSaved,
}: ViewerProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const nvRef = useRef<import("@niivue/niivue").Niivue | null>(null);
  const dragModesRef = useRef<DragModes | null>(null);
  const annotationVersionRef = useRef(annotationVersion);
  const reworkBaseVersionRef = useRef(reworkBaseVersion);
  const onDirtyChangeRef = useRef(onDirtyChange);
  const drawOpacityRef = useRef(DEFAULT_DRAW_OPACITY);
  const penLevelRef = useRef(DEFAULT_PEN_LEVEL);
  const sequenceLoaderRef = useRef<SequenceLoader | null>(null);
  const sequenceRequestRef = useRef(0);
  const [activeSequence, setActiveSequence] = useState<SequenceKey>("w");
  const [sequenceLoadStates, setSequenceLoadStates] = useState<Record<SequenceKey, SequenceLoadState>>(newSequenceLoadStates);
  const [switchingSequence, setSwitchingSequence] = useState<SequenceKey | null>(null);
  const [tool, setTool] = useState<Tool>(canDraw ? "contour" : "navigate");
  const [focusedView, setFocusedView] = useState<ViewKey | null>(null);
  const [windowLevel, setWindowLevel] = useState<WindowLevel | null>(null);
  const [windowInputs, setWindowInputs] = useState({ center: "", width: "" });
  const [drawOpacity, setDrawOpacity] = useState(DEFAULT_DRAW_OPACITY);
  const [penLevel, setPenLevel] = useState(DEFAULT_PEN_LEVEL);
  const [cursorOpacity, setCursorOpacity] = useState(DEFAULT_CURSOR_OPACITY);
  const [slicePositions, setSlicePositions] = useState<SlicePositions>(EMPTY_SLICE_POSITIONS);
  const [loading, setLoading] = useState(true);
  const [previewReady, setPreviewReady] = useState(false);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [message, setMessage] = useState("正在读取水相影像…");
  const [error, setError] = useState("");

  const syncWindowControls = useCallback((nv: Viewer) => {
    const next = readWindowLevel(nv);
    setWindowLevel(next);
    if (next) setWindowInputs({ center: formatIntensity(next.center), width: formatIntensity(next.width) });
  }, []);

  const syncSliceControls = useCallback((nv: Viewer) => {
    const next = readSlicePositions(nv);
    if (next) setSlicePositions((current) => slicePositionsEqual(current, next) ? current : next);
  }, []);

  useEffect(() => {
    annotationVersionRef.current = annotationVersion;
    reworkBaseVersionRef.current = reworkBaseVersion;
    onDirtyChangeRef.current = onDirtyChange;
  }, [annotationVersion, onDirtyChange, reworkBaseVersion]);

  useEffect(() => {
    let disposed = false;
    let waterStartTimer: number | undefined;
    let loadSequence: SequenceLoader | null = null;
    const initialize = async () => {
      if (!canvasRef.current) return;
      try {
        setLoading(true);
        setPreviewReady(false);
        setError("");
        setActiveSequence("w");
        setSwitchingSequence(null);
        setSequenceLoadStates(newSequenceLoadStates());
        setTool("navigate");
        setDirty(false);
        onDirtyChangeRef.current?.(false);
        setMessage("正在生成快速三维预览…");
        const { Niivue, DRAG_MODE, NVImage } = await import("@niivue/niivue");
        if (disposed || !canvasRef.current) return;
        const createViewer = () => new Niivue({
          backColor: [0.015, 0.025, 0.04, 1],
          crosshairColor: [0.18, 0.83, 0.75, 1],
          crosshairWidth: 1,
          show3Dcrosshair: true,
          isColorbar: false,
          isRuler: true,
          fontMinPx: 12,
          isRadiologicalConvention: true,
          isNearestInterpolation: false,
          showAllOrientationMarkers: true,
          penSize: penSizeForLevel(penLevelRef.current),
          smoothDrawing: 0,
        });
        const attachViewer = async (viewer: Viewer) => {
          nvRef.current = viewer;
          await viewer.attachToCanvas(canvasRef.current!);
          installDashedCrosshair(viewer, true);
          installDrawingLocationLock(viewer);
          viewer.setDrawingEnabled(false);
          configureMouse(viewer, DRAG_MODE, false);
        };
        let nv = createViewer();
        dragModesRef.current = DRAG_MODE;
        await attachViewer(nv);
        const previewUrl = `/api/cases/${projectCaseId}/preview/w.nii.gz`;
        const previewRequest = prefetchImageBuffer(previewUrl).then((buffer) => nv.addVolumeFromUrl({
          url: previewUrl,
          buffer,
          name: "w-preview.nii.gz",
          colormap: "gray",
          opacity: 1,
        })).finally(() => releaseImageBuffer(previewUrl));
        const previewOutcome = previewRequest
          .then((volume) => ({ kind: "preview" as const, volume }))
          .catch(() => ({ kind: "preview-error" as const }));
        if (!disposed) setSequenceLoadStates((current) => ({ ...current, w: "loading" }));
        const waterUrl = `/api/cases/${projectCaseId}/volume/w.nii.gz`;
        const waterPromise = prefetchImageBuffer(waterUrl).then((buffer) => NVImage.loadFromUrl({
          url: waterUrl,
          buffer,
          name: "w.nii.gz",
          colormap: "gray",
          opacity: 1,
        })).then((volume) => {
          if (!disposed) setSequenceLoadStates((current) => ({ ...current, w: "loaded" }));
          return volume;
        }).catch((reason) => {
          if (!disposed) setSequenceLoadStates((current) => ({ ...current, w: "error" }));
          throw reason;
        }).finally(() => releaseImageBuffer(waterUrl));
        const headStartOutcome = await Promise.race([
          previewOutcome,
          new Promise<{ kind: "head-start-expired" }>((resolve) => {
            waterStartTimer = window.setTimeout(() => resolve({ kind: "head-start-expired" }), 1800);
          }),
        ]);
        if (waterStartTimer !== undefined) {
          window.clearTimeout(waterStartTimer);
          waterStartTimer = undefined;
        }
        if (disposed) return;
        const firstVolume = headStartOutcome.kind === "head-start-expired"
          ? await Promise.race([
            previewOutcome,
            waterPromise.then((volume) => ({ kind: "water" as const, volume })),
          ])
          : headStartOutcome;
        let waterVolume: ViewerVolume;
        if (firstVolume.kind === "water") {
          waterVolume = firstVolume.volume;
        } else if (firstVolume.kind === "preview") {
          if (disposed) return;
          nv.setSliceType(nv.sliceTypeMultiplanar);
          applyViewLayout(nv, null);
          setFocusedView(null);
          nv.setRadiologicalConvention(true);
          syncSliceControls(nv);
          setPreviewReady(true);
          setMessage("快速预览已显示 · 正在读取水相原始影像，完成后开放勾画");
          waterVolume = await waterPromise;
        } else {
          setMessage("正在直接读取高清水相…");
          waterVolume = await waterPromise;
        }
        const previewCrosshair = new Float32Array([
          Number(nv.scene.crosshairPos[0]),
          Number(nv.scene.crosshairPos[1]),
          Number(nv.scene.crosshairPos[2]),
        ]) as unknown as typeof nv.scene.crosshairPos;
        nv.cleanup();
        if (disposed || !canvasRef.current) return;
        nv = createViewer();
        await attachViewer(nv);
        if (disposed) {
          nv.cleanup();
          return;
        }
        nv.addVolume(waterVolume);
        nv.scene.crosshairPos = previewCrosshair;
        const inFlight = new Map<SequenceKey, Promise<ViewerVolume>>();
        loadSequence = async (key: SequenceKey) => {
          const existing = nv.volumes.find((volume) => volume.name === `${key}.nii.gz`);
          if (existing) return existing;
          const pending = inFlight.get(key);
          if (pending) return pending;
          if (!disposed) setSequenceLoadStates((current) => ({ ...current, [key]: "loading" }));
          const volumeUrl = `/api/cases/${projectCaseId}/volume/${key}.nii.gz`;
          const task = prefetchImageBuffer(volumeUrl).then((buffer) => nv.addVolumeFromUrl({
            url: volumeUrl,
            buffer,
            name: `${key}.nii.gz`,
            colormap: "gray",
            opacity: key === "w" ? 1 : 0,
          })).finally(() => releaseImageBuffer(volumeUrl));
          inFlight.set(key, task);
          try {
            const loaded = await task;
            if (!disposed) setSequenceLoadStates((current) => ({ ...current, [key]: "loaded" }));
            return loaded;
          } catch (reason) {
            if (!disposed) setSequenceLoadStates((current) => ({ ...current, [key]: "error" }));
            throw reason;
          } finally {
            inFlight.delete(key);
          }
        };
        sequenceLoaderRef.current = loadSequence;
        nv.setSliceType(nv.sliceTypeMultiplanar);
        applyViewLayout(nv, null);
        setFocusedView(null);
        nv.setRadiologicalConvention(true);
        nv.setDrawOpacity(drawOpacityRef.current / 100);
        nv.setDrawingEnabled(canDraw);
        if (canDraw) nv.setPenValue(1, true);
        configureMouse(nv, DRAG_MODE, false);
        setCrosshairVisible(nv, !canDraw);
        if (annotationVersionRef.current) {
          await nv.loadDrawingFromUrl(`/api/cases/${projectCaseId}/annotation.nii.gz?version=${annotationVersionRef.current}`);
        }
        nv.onDrawingChanged = () => {
          if (!disposed) {
            setDirty(true);
            onDirtyChangeRef.current?.(true);
          }
        };
        nv.onIntensityChange = () => { if (!disposed) syncWindowControls(nv); };
        nv.onMouseUp = () => { if (!disposed) syncWindowControls(nv); };
        nv.onLocationChange = () => { if (!disposed) syncSliceControls(nv); };
        syncWindowControls(nv);
        syncSliceControls(nv);
        setTool(canDraw ? "contour" : "navigate");
        if (canDraw && reviewMode) {
          setMessage(`审核微调模式：已载入提交版本 v${reviewBaseVersion ?? annotationVersionRef.current ?? 0} · 原提交不会被覆盖 · 保存后生成审核修订版`);
        } else if (canDraw && reworkBaseVersionRef.current) {
          setMessage(`返工模式：已载入退回版本 v${reworkBaseVersionRef.current} · 可直接在原标注上修改 · 保存后生成新版本`);
        } else {
          setMessage(canDraw ? "轮廓笔：十字定位已隐藏 · 左键闭合勾画 · 右键仍可调窗" : "虚线十字定位：左键定位，右键调窗，中键平移，滚轮换层");
        }
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "影像加载失败");
      } finally {
        if (!disposed) setLoading(false);
      }
    };
    void initialize();
    return () => {
      disposed = true;
      if (waterStartTimer !== undefined) window.clearTimeout(waterStartTimer);
      sequenceRequestRef.current += 1;
      if (sequenceLoaderRef.current === loadSequence) sequenceLoaderRef.current = null;
      nvRef.current?.cleanup();
      nvRef.current = null;
    };
  }, [projectCaseId, canDraw, reviewBaseVersion, reviewMode, syncSliceControls, syncWindowControls]);

  const chooseSequence = useCallback(async (key: SequenceKey) => {
    const nv = nvRef.current;
    const loader = sequenceLoaderRef.current;
    if (!nv || !loader || loading) return;
    const requestId = sequenceRequestRef.current + 1;
    sequenceRequestRef.current = requestId;
    const sequence = SEQUENCES.find((item) => item.key === key);
    const wasLoaded = nv.volumes.some((volume) => volume.name === `${key}.nii.gz`);
    setSwitchingSequence(key);
    setError("");
    if (!wasLoaded) setMessage(`正在载入${sequence?.label || key.toUpperCase()}序列…`);
    try {
      const selected = await loader(key);
      if (requestId !== sequenceRequestRef.current || nv !== nvRef.current) return;
      if (nv.volumes[0] !== selected) nv.moveVolumeToBottom(selected);
      nv.volumes.forEach((volume, index) => nv.setOpacity(index, volume === selected ? 1 : 0));
      nv.drawScene();
      setActiveSequence(key);
      setMessage(`${sequence?.label || key.toUpperCase()}序列已就绪`);
      syncWindowControls(nv);
      syncSliceControls(nv);
    } catch (reason) {
      if (requestId === sequenceRequestRef.current) setError(reason instanceof Error ? reason.message : "序列加载失败，请重试");
    } finally {
      if (requestId === sequenceRequestRef.current) setSwitchingSequence(null);
    }
  }, [loading, syncSliceControls, syncWindowControls]);

  const changeSlice = useCallback((view: SliceViewKey, requestedIndex: number) => {
    const nv = nvRef.current;
    if (!nv) return;
    const current = readSlicePositions(nv)?.[view];
    if (!current) return;
    const target = Math.min(current.maximum, Math.max(0, Math.round(requestedIndex)));
    const delta = target - current.index;
    if (delta !== 0) {
      if (view === "axial") nv.moveCrosshairInVox(0, 0, delta);
      else if (view === "sagittal") nv.moveCrosshairInVox(delta, 0, 0);
      else nv.moveCrosshairInVox(0, delta, 0);
    }
    syncSliceControls(nv);
  }, [syncSliceControls]);

  const chooseTool = useCallback((nextTool: Tool) => {
    const nv = nvRef.current;
    const modes = dragModesRef.current;
    if (!nv || !modes) return;
    setTool(nextTool);
    if (nextTool === "navigate") {
      nv.setDrawingEnabled(false);
      configureMouse(nv, modes, false);
      setCrosshairVisible(nv, true);
      setMessage("虚线十字定位：左键定位，右键调窗，中键平移，滚轮换层");
    } else if (nextTool === "windowing") {
      nv.setDrawingEnabled(false);
      configureMouse(nv, modes, true);
      setCrosshairVisible(nv, true);
      setMessage("调窗模式：按住左键拖动 · 左右调整 WW · 上下调整 WL · 也可直接输入数值");
    } else if (nextTool === "contour") {
      nv.setDrawingEnabled(true);
      configureMouse(nv, modes, false);
      nv.setPenValue(1, true);
      setCrosshairVisible(nv, false);
      setMessage("轮廓笔：十字定位已隐藏 · 左键闭合勾画 · 右键仍可调窗");
    } else {
      nv.setDrawingEnabled(true);
      configureMouse(nv, modes, false);
      nv.setPenValue(0, true);
      setCrosshairVisible(nv, false);
      setMessage("橡皮擦：十字定位已隐藏 · 左键圈出移除区域 · 右键仍可调窗");
    }
  }, []);

  const resetWindow = useCallback(() => {
    const nv = nvRef.current;
    if (!nv || !restoreVolumeWindow(nv)) return;
    syncWindowControls(nv);
    setMessage(activeSequence.toUpperCase() + " 序列窗宽/窗位已复位");
  }, [activeSequence, syncWindowControls]);

  const changeDrawOpacity = useCallback((percentage: number) => {
    const normalized = Math.min(100, Math.max(0, percentage));
    drawOpacityRef.current = normalized;
    setDrawOpacity(normalized);
    nvRef.current?.setDrawOpacity(normalized / 100);
  }, []);

  const changePenLevel = useCallback((nextLevel: number) => {
    const normalized = Math.min(PEN_SIZES.length, Math.max(1, Math.round(nextLevel)));
    penLevelRef.current = normalized;
    setPenLevel(normalized);
    if (nvRef.current) nvRef.current.opts.penSize = penSizeForLevel(normalized);
  }, []);

  const applyNumericWindow = useCallback(() => {
    const nv = nvRef.current;
    const center = Number(windowInputs.center.trim());
    const width = Number(windowInputs.width.trim());
    if (!nv || !windowInputs.center.trim() || !windowInputs.width.trim() || !Number.isFinite(center) || !Number.isFinite(width) || width <= 0) {
      setMessage("数值无效：WL 需要是数字，WW 必须大于 0");
      return;
    }
    if (!applyVolumeWindow(nv, center, width)) return;
    syncWindowControls(nv);
    setMessage(`${activeSequence.toUpperCase()} 序列已应用 WL ${formatIntensity(center)} · WW ${formatIntensity(width)}`);
  }, [activeSequence, syncWindowControls, windowInputs]);

  const toggleView = useCallback((view: ViewKey) => {
    const nv = nvRef.current;
    if (!nv) return;
    const next = focusedView === view ? null : view;
    applyViewLayout(nv, next);
    setFocusedView(next);
    const label = VIEW_LABELS.find((item) => item.key === view)?.label || "视图";
    setMessage(next ? label + "已放大；点击标签或按 Esc 恢复四分屏" : "已恢复标准四分屏");
  }, [focusedView]);

  useEffect(() => {
    if (!focusedView) return;
    const restoreOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || !nvRef.current) return;
      applyViewLayout(nvRef.current, null);
      setFocusedView(null);
      setMessage("已恢复标准四分屏");
    };
    window.addEventListener("keydown", restoreOnEscape);
    return () => window.removeEventListener("keydown", restoreOnEscape);
  }, [focusedView]);

  const save = useCallback(async () => {
    const nv = nvRef.current;
    if (!nv?.drawBitmap || !nv.volumes[0]) {
      setError("当前没有可保存的标注");
      return;
    }
    setSaving(true);
    setError("");
    try {
      const bytes = await nv.volumes[0].saveToUint8Array("pancreas-mask.nii.gz", nv.drawBitmap);
      const form = new FormData();
      const maskBuffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
      form.append("mask", new Blob([maskBuffer], { type: "application/gzip" }), "pancreas-mask.nii.gz");
      const normalizedComment = saveComment.trim();
      form.append("comment", reviewMode
        ? normalizedComment || `审核微调 · 基于提交版本 v${reviewBaseVersion ?? annotationVersionRef.current ?? 0} · ${activeSequence.toUpperCase()} 序列`
        : `基于 ${activeSequence.toUpperCase()} 序列保存`);
      const endpoint = reviewMode ? "review-revision" : "annotation";
      const result = await api<{ version: number }>(`/api/cases/${projectCaseId}/${endpoint}`, { method: "POST", body: form }, csrfToken);
      setDirty(false);
      onDirtyChangeRef.current?.(false);
      setMessage(reviewMode ? `审核修订版 v${result.version} 已保存 · 请在右侧确认通过或退回` : `版本 v${result.version} 已安全保存`);
      onSaved(result.version);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }, [activeSequence, csrfToken, onSaved, projectCaseId, reviewBaseVersion, reviewMode, saveComment]);

  return (
    <section className="viewer-shell" aria-label="医学影像标注视图">
      <div className="viewer-toolbar">
        <div className="tool-group" aria-label="影像序列">
          {SEQUENCES.map((sequence) => {
            const state = sequenceLoadStates[sequence.key];
            const isBusy = switchingSequence === sequence.key || state === "loading";
            return <button aria-busy={isBusy} className={activeSequence === sequence.key ? "tool-button active" : "tool-button"} disabled={loading || switchingSequence !== null} key={sequence.key} onClick={() => void chooseSequence(sequence.key)} type="button">{sequence.label}{isBusy && <i aria-hidden="true" className="sequence-loading-dot" />}{state === "error" && <i aria-hidden="true" className="sequence-error-mark">!</i>}</button>;
          })}
        </div>
        <div className="toolbar-divider" />
        <div className="tool-group" aria-label="标注工具">
          <button className={tool === "navigate" ? "tool-button active" : "tool-button"} disabled={loading} onClick={() => chooseTool("navigate")} type="button">十字定位</button>
          <button className={tool === "windowing" ? "tool-button active" : "tool-button"} disabled={loading} onClick={() => chooseTool("windowing")} title="左右拖动调窗宽，上下拖动调窗位" type="button">窗宽/窗位</button>
          <button className="tool-button" disabled={loading} onClick={resetWindow} title="复位当前序列的显示范围" type="button">复位调窗</button>
          {canDraw && <>
            <button className={tool === "contour" ? "tool-button active accent" : "tool-button"} disabled={loading} onClick={() => chooseTool("contour")} type="button">闭合轮廓</button>
            <button className={tool === "erase" ? "tool-button active" : "tool-button"} disabled={loading} onClick={() => chooseTool("erase")} type="button">擦除</button>
            <button className="tool-button" disabled={loading} onClick={() => { nvRef.current?.drawUndo(); setDirty(true); }} type="button">撤销</button>
            <button className="primary compact" disabled={saving || loading || !dirty} onClick={() => void save()} type="button">{saving ? "保存中…" : dirty ? reviewMode ? "保存审核修改" : "保存草稿" : reviewMode ? "审核修改已保存" : "已保存"}</button>
          </>}
        </div>
      </div>
      <div className={tool === "windowing" ? "viewer-guidance emphasis" : "viewer-guidance"}>
        <div className="guidance-copy">
          <span className="guidance-icon" aria-hidden="true">i</span>
          <div><strong>操作提示</strong><span aria-live="polite" role="status">{message}</span></div>
        </div>
        <div className="viewer-guidance-controls">
          {canDraw && <div className="pen-size-editor" role="group" aria-label="实际勾画线宽调节" title="调节实际勾画和擦除宽度；最细为 1 体素">
            <span>线宽</span>
            <button aria-label="减小笔宽" disabled={penLevel <= 1} onClick={() => changePenLevel(penLevel - 1)} type="button">−</button>
            <strong>{penSizeForLevel(penLevel)}体素</strong>
            <button aria-label="增大笔宽" disabled={penLevel >= PEN_SIZES.length} onClick={() => changePenLevel(penLevel + 1)} type="button">＋</button>
          </div>}
          {canDraw && <label className="cursor-opacity-editor" title="只改变圆形鼠标笔尖，不改变标注覆盖透明度">
            <span>笔尖</span>
            <select aria-label="笔尖透明度" onChange={(event) => setCursorOpacity(Number(event.currentTarget.value))} value={cursorOpacity}>
              {CURSOR_OPACITY_OPTIONS.map((value) => <option key={value} value={value}>{value}%</option>)}
            </select>
          </label>}
          {(canDraw || annotationVersion !== null) && <label className="annotation-opacity-editor" title="0% 隐藏标注覆盖，100% 完全不透明">
            <span>标注透明度</span>
            <input aria-label="标注覆盖透明度" className="opacity-range" max="100" min="0" onInput={(event) => changeDrawOpacity(Number(event.currentTarget.value))} step="5" type="range" value={drawOpacity} />
            <input aria-label="标注透明度百分比" className="opacity-number" max="100" min="0" onChange={(event) => { if (Number.isFinite(event.currentTarget.valueAsNumber)) changeDrawOpacity(event.currentTarget.valueAsNumber); }} step="5" type="number" value={drawOpacity} />
            <span className="opacity-unit">%</span>
          </label>}
          {windowLevel && <div className="window-editor" role="group" aria-label="窗宽窗位数值调整">
            <label><span>WL</span><input aria-label="窗位 WL" inputMode="decimal" onChange={(event) => setWindowInputs((current) => ({ ...current, center: event.target.value }))} onFocus={(event) => event.currentTarget.select()} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); applyNumericWindow(); } }} step="any" type="number" value={windowInputs.center} /></label>
            <label><span>WW</span><input aria-label="窗宽 WW" inputMode="decimal" min="0.1" onChange={(event) => setWindowInputs((current) => ({ ...current, width: event.target.value }))} onFocus={(event) => event.currentTarget.select()} onKeyDown={(event) => { if (event.key === "Enter") { event.preventDefault(); applyNumericWindow(); } }} step="any" type="number" value={windowInputs.width} /></label>
            <button className="window-apply" onClick={applyNumericWindow} type="button">应用</button>
          </div>}
        </div>
      </div>
      <div className={tool === "contour" ? "canvas-stage contour-cursor" : "canvas-stage"}>
        <canvas ref={canvasRef} style={tool === "contour" ? { cursor: contourCursorForLevel(penLevel, cursorOpacity) } : undefined} tabIndex={0} />
        <div className={focusedView ? "viewer-grid-guide focused" : "viewer-grid-guide"}>
          {VIEW_LABELS.filter((view) => !focusedView || view.key === focusedView).map((view) => <button aria-label={focusedView ? "恢复四分屏" : "放大" + view.label + "视图"} className={"view-label " + view.key + (focusedView ? " maximized" : "")} key={view.key} onClick={() => toggleView(view.key)} title={focusedView ? "恢复四分屏（Esc）" : "放大" + view.label} type="button">{view.label}<small>{view.abbreviation}</small><i>{focusedView ? "↙" : "⛶"}</i></button>)}
        </div>
        <div className={focusedView ? "slice-slider-layer focused" : "slice-slider-layer"} aria-label="二维视图切片控制">
          {SLICE_VIEWS.filter((view) => !focusedView || view === focusedView).map((view) => {
            const position = slicePositions[view];
            const label = VIEW_LABELS.find((item) => item.key === view)?.label || view;
            if (position.maximum < 1) return null;
            return <label className={"slice-slider " + view} key={view} title={`拖动切换${label}切片；与鼠标滚轮同步`}>
              <span>{position.index + 1}/{position.maximum + 1}</span>
              <input aria-label={`${label}切片`} aria-valuetext={`第 ${position.index + 1} 层，共 ${position.maximum + 1} 层`} max={position.maximum} min="0" onInput={(event) => changeSlice(view, event.currentTarget.valueAsNumber)} step="1" type="range" value={position.index} />
            </label>;
          })}
        </div>
        {loading && !previewReady && <div className="viewer-overlay"><span className="spinner" />{message}</div>}
        {loading && previewReady && <div className="viewer-upgrade-status"><span className="spinner" />原始影像加载中 · 预览已可查看</div>}
        {error && <div className="viewer-error">{error}</div>}
      </div>
    </section>
  );
}
