import { useEffect, useMemo, useRef, useState } from 'preact/hooks';
import * as THREE from 'three';
import {
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
  forceX,
  forceY,
  forceZ,
  type Simulation,
  type SimulationLinkDatum,
  type SimulationNodeDatum,
} from 'd3-force-3d';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { MeshoptDecoder } from 'three/examples/jsm/libs/meshopt_decoder.module.js';
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/examples/jsm/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js';
import { X, Search, RotateCw, Sparkles, ChevronDown, ChevronRight, SlidersHorizontal } from 'lucide-preact';
import { formatRelativeTime } from '@/lib/format';
import { renderMarkdown } from '@/lib/markdown';
import { hasWebGL } from '@/lib/webgl';
import { BrainGraph } from './BrainGraph';
import { BrainGraph2D, type BrainGraphData, type BrainGraphEdge, type BrainGraphNode } from './BrainGraph2D';

interface HiveEntry {
  id: number;
  agent_id: string;
  chat_id: string;
  action: string;
  summary: string;
  artifacts: string | null;
  created_at: number;
}

interface Props {
  data?: BrainGraphData;
  entries: HiveEntry[];
  agentFilter: string;
  agentColors: Record<string, string>;
  blurOn: boolean;
  showActivity?: boolean;
}

// ── Lobes & agent mapping ──────────────────────────────────────────
// Same shape as the 2D version so the user gets consistent semantics:
// each agent has a "home" lobe, dots cluster in that lobe's region,
// the side panel filters apply identically.

interface Lobe {
  id: string;
  label: string;
  color: THREE.Color;
}

const FRONTAL = new THREE.Color('#5eb6ff');
const PARIETAL = new THREE.Color('#10b981');
const TEMPORAL = new THREE.Color('#f59e0b');
const OCCIPITAL = new THREE.Color('#a78bfa');

const LOBES: Lobe[] = [
  { id: 'frontal',   label: 'Frontal',   color: FRONTAL },
  { id: 'parietal',  label: 'Parietal',  color: PARIETAL },
  { id: 'temporal',  label: 'Temporal',  color: TEMPORAL },
  { id: 'occipital', label: 'Occipital', color: OCCIPITAL },
];

const LOBE_BY_ID = LOBES.reduce<Record<string, Lobe>>((acc, l) => { acc[l.id] = l; return acc; }, {});

const AGENT_LOBE: Record<string, string> = {
  default: 'frontal',
  main: 'frontal',
  research: 'parietal',
  comms: 'temporal',
  content: 'occipital',
  ops: 'parietal',
  meta: 'frontal',
};

function lobeFor(agentId: string): string {
  return AGENT_LOBE[agentId] || fallbackLobeId(agentId);
}

function fallbackLobeId(agentId: string): string {
  let hash = 0;
  for (const char of agentId) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  return LOBES[hash % LOBES.length].id;
}

const SYNAPSE_LOBES = ['frontal', 'parietal', 'temporal', 'occipital'] as const;
function pickRandomOtherLobe(exclude: string): string {
  const pool = SYNAPSE_LOBES.filter((l) => l !== exclude);
  return pool[Math.floor(Math.random() * pool.length)];
}

// ── Hash-based 3D noise ────────────────────────────────────────────
// Cheap, deterministic value noise with smoothstep interpolation.
// Good enough to give the brain mesh a lumpy organic surface.

function hash(x: number, y: number, z: number): number {
  let h = x * 374761393 + y * 668265263 + z * 2147483647;
  h = (h ^ (h >>> 13)) * 1274126177;
  h = h ^ (h >>> 16);
  return ((h >>> 0) / 0xffffffff) * 2 - 1;
}

function smooth(t: number) { return t * t * (3 - 2 * t); }

function noise3D(x: number, y: number, z: number): number {
  const xi = Math.floor(x), yi = Math.floor(y), zi = Math.floor(z);
  const xf = x - xi, yf = y - yi, zf = z - zi;
  const u = smooth(xf), v = smooth(yf), w = smooth(zf);
  // Trilinear interpolation of corner hashes
  const c000 = hash(xi,     yi,     zi    );
  const c100 = hash(xi + 1, yi,     zi    );
  const c010 = hash(xi,     yi + 1, zi    );
  const c110 = hash(xi + 1, yi + 1, zi    );
  const c001 = hash(xi,     yi,     zi + 1);
  const c101 = hash(xi + 1, yi,     zi + 1);
  const c011 = hash(xi,     yi + 1, zi + 1);
  const c111 = hash(xi + 1, yi + 1, zi + 1);
  const x00 = c000 * (1 - u) + c100 * u;
  const x10 = c010 * (1 - u) + c110 * u;
  const x01 = c001 * (1 - u) + c101 * u;
  const x11 = c011 * (1 - u) + c111 * u;
  const y0 = x00 * (1 - v) + x10 * v;
  const y1 = x01 * (1 - v) + x11 * v;
  return y0 * (1 - w) + y1 * w;
}

function fbm(x: number, y: number, z: number): number {
  return noise3D(x, y, z) * 0.55 + noise3D(x * 2.3, y * 2.3, z * 2.3) * 0.28 + noise3D(x * 5.1, y * 5.1, z * 5.1) * 0.17;
}

// Ridge noise — `1 - |fbm|` produces meandering linear ridges. Stacked
// at multiple frequencies and run through *domain warping* (sampling
// the ridge at coordinates that have themselves been jittered by
// another noise field) the result is the twisted, looping cortex
// pattern that's instantly recognizable as a brain rather than a
// generic noisy ball.
function ridgedFbm(x: number, y: number, z: number): number {
  const r1 = (1 - Math.abs(fbm(x, y, z))) * 0.55;
  const r2 = (1 - Math.abs(fbm(x * 2.7, y * 2.7, z * 2.7))) * 0.30;
  const r3 = (1 - Math.abs(fbm(x * 6.3, y * 6.3, z * 6.3))) * 0.15;
  return r1 + r2 + r3;
}

function domainWarpedRidge(x: number, y: number, z: number): number {
  // Sample warp offsets from independent noise fields, then evaluate
  // the ridge noise at the warped coordinate. Warp amplitude ~0.6
  // gives strong meandering without making the ridges chaotic.
  const wx = fbm(x * 0.7, y * 0.7, z * 0.7) * 0.6;
  const wy = fbm(x * 0.7 + 5.1, y * 0.7 + 5.1, z * 0.7 + 5.1) * 0.6;
  const wz = fbm(x * 0.7 + 9.3, y * 0.7 + 9.3, z * 0.7 + 9.3) * 0.6;
  return ridgedFbm(x + wx, y + wy, z + wz);
}

function smoothstep(edge0: number, edge1: number, x: number) {
  const t = Math.max(0, Math.min(1, (x - edge0) / (edge1 - edge0)));
  return t * t * (3 - 2 * t);
}

// ── Brain hemisphere builder ────────────────────────────────────────
// Returns a deformed ellipsoid mesh with vertex colors painted by
// soft lobe membership. The same lobe-weight function is later
// re-used to assign dots to surface positions.

function lobeWeights(x: number, y: number, z: number) {
  // Three.js camera defaults to looking in -z direction. With our
  // camera at +z, vertices facing the user have z > 0 — that's the
  // "front" of the brain (frontal lobe). Previous version inverted
  // this and painted the visible surface as occipital, which is why
  // everything looked dark/violet. Tight smoothstep bands give each
  // lobe a clearly-dominant region.
  const front = z;
  const wFrontal = smoothstep(0.15, 0.55, front);
  const wOccipital = smoothstep(-0.15, -0.55, front);
  const wParietal = smoothstep(0.05, 0.45, y) * (1 - wFrontal - wOccipital);
  const wTemporal = smoothstep(-0.05, -0.45, y);
  return { wFrontal, wParietal, wTemporal, wOccipital };
}

function buildHemisphere(side: 'left' | 'right'): { mesh: THREE.Mesh; surface: THREE.Vector3[] } {
  const detail = 6;
  const geo = new THREE.IcosahedronGeometry(1, detail);
  // Anatomical proportions: longer front-to-back than wide-or-tall,
  // matching a real brain's superior axis (~16cm L × 14cm W × 12cm H).
  geo.scale(0.50, 0.70, 1.18);

  const sign = side === 'left' ? -1 : 1;

  const positions = geo.attributes.position;
  const count = positions.count;
  const colors = new Float32Array(count * 3);
  const surface: THREE.Vector3[] = [];

  for (let i = 0; i < count; i++) {
    let x = positions.getX(i);
    let y = positions.getY(i);
    let z = positions.getZ(i);

    // Flatten the inner wall so the longitudinal fissure is crisp.
    const facingMidline = (sign === -1 && x > 0) || (sign === 1 && x < 0);
    if (facingMidline) {
      const t = Math.min(1, Math.abs(x) / 0.45);
      x *= 0.35 * (1 - t * 0.6);
    }

    // Anatomical bulges, applied before the noise displacement so the
    // ridges follow the bulge contours rather than fight them.
    //
    // Temporal pouch: lower-side area (y < 0, |x| moderate) bulges
    // outward and downward. This is the big lateral-lower bump that
    // gives a brain its iconic "kidney bean" side profile.
    const pouchT = smoothstep(0.0, -0.55, y) * smoothstep(0.0, 0.55, Math.abs(x));
    if (pouchT > 0) {
      x *= 1 + pouchT * 0.18;
      y -= pouchT * 0.10;
    }
    // Frontal pole: round and bulge the very front (high z).
    const frontT = smoothstep(0.7, 1.05, z);
    if (frontT > 0) {
      z *= 1 + frontT * 0.06;
      const radial = Math.sqrt(x * x + y * y) + 0.0001;
      const radialBoost = 1 + frontT * 0.05;
      x *= radialBoost;
      y *= radialBoost;
    }
    // Occipital pole: same treatment at the back.
    const backT = smoothstep(-0.7, -1.05, z);
    if (backT > 0) {
      z *= 1 + backT * 0.04;
    }

    const len = Math.sqrt(x * x + y * y + z * z) + 0.0001;
    const nx = x / len, ny = y / len, nz = z / len;

    // Domain-warped ridge — gives the twisting, looping fold pattern
    // that real cortex has. Higher amplitude than before since the
    // bloom pass will pick up the highlights and let valleys shadow.
    const sx = nx * 3.6;
    const sy = ny * 3.6;
    const sz = nz * 2.8;
    const ridge = domainWarpedRidge(sx, sy, sz);
    const displacement = (ridge - 0.42) * 0.26;

    const factor = 1 + displacement;
    const px = x * factor;
    const py = y * factor;
    const pz = z * factor;

    positions.setXYZ(i, px, py, pz);

    // Lobe colors: blended by weight, no desaturation — let the
    // agent-mapped hues actually show.
    const w = lobeWeights(nx, ny, nz);
    const sum = w.wFrontal + w.wParietal + w.wTemporal + w.wOccipital + 0.0001;
    const wf = w.wFrontal / sum;
    const wp = w.wParietal / sum;
    const wt = w.wTemporal / sum;
    const wo = w.wOccipital / sum;

    const cr = wf * FRONTAL.r + wp * PARIETAL.r + wt * TEMPORAL.r + wo * OCCIPITAL.r;
    const cg = wf * FRONTAL.g + wp * PARIETAL.g + wt * TEMPORAL.g + wo * OCCIPITAL.g;
    const cb = wf * FRONTAL.b + wp * PARIETAL.b + wt * TEMPORAL.b + wo * OCCIPITAL.b;

    // Pure lobe colors — desaturation here was the reason every
    // region used to read as "purple". A tiny base mix (0.08) just
    // softens the edges where two lobes meet.
    const baseR = 0.5, baseG = 0.48, baseB = 0.55;
    const mix = 0.92;
    colors[i * 3]     = cr * mix + baseR * (1 - mix);
    colors[i * 3 + 1] = cg * mix + baseG * (1 - mix);
    colors[i * 3 + 2] = cb * mix + baseB * (1 - mix);

    // Save outward-facing surface vertices for dot placement.
    if (sign === -1 && x < -0.05) surface.push(new THREE.Vector3(px, py, pz));
    if (sign === 1 && x > 0.05) surface.push(new THREE.Vector3(px, py, pz));
  }

  geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
  geo.computeVertexNormals();

  // MeshStandardMaterial gives proper PBR specular highlights; with
  // moderate roughness the gyri ridges catch light convincingly.
  const mat = new THREE.MeshStandardMaterial({
    vertexColors: true,
    roughness: 0.48,
    metalness: 0.05,
    flatShading: false,
    transparent: true,
    opacity: 0.26,
    depthWrite: false,
    side: THREE.DoubleSide,
    emissive: new THREE.Color(0x0a3d48),
    emissiveIntensity: 0.18,
  });

  const mesh = new THREE.Mesh(geo, mat);
  mesh.renderOrder = -2;
  // Bigger gap so the longitudinal fissure is visible even from
  // shallow viewing angles. The flattened inner walls meet here.
  mesh.position.x = sign * 0.04;
  return { mesh, surface };
}

type LobePools = Record<'left' | 'right', THREE.Vector3[]>;

function blendedLobeColor(nx: number, ny: number, nz: number): THREE.Color {
  const w = lobeWeights(nx, ny, nz);
  const sum = w.wFrontal + w.wParietal + w.wTemporal + w.wOccipital + 0.0001;
  const wf = w.wFrontal / sum;
  const wp = w.wParietal / sum;
  const wt = w.wTemporal / sum;
  const wo = w.wOccipital / sum;

  const cr = wf * FRONTAL.r + wp * PARIETAL.r + wt * TEMPORAL.r + wo * OCCIPITAL.r;
  const cg = wf * FRONTAL.g + wp * PARIETAL.g + wt * TEMPORAL.g + wo * OCCIPITAL.g;
  const cb = wf * FRONTAL.b + wp * PARIETAL.b + wt * TEMPORAL.b + wo * OCCIPITAL.b;

  const baseR = 0.5, baseG = 0.48, baseB = 0.55;
  const mix = 0.92;
  return new THREE.Color(
    cr * mix + baseR * (1 - mix),
    cg * mix + baseG * (1 - mix),
    cb * mix + baseB * (1 - mix),
  );
}

function cloneMaterialWithVertexColors(material: THREE.Material | THREE.Material[] | undefined) {
  const cloneOne = (m: THREE.Material | undefined) => {
    const cloned = m
      ? m.clone()
      : new THREE.MeshStandardMaterial({ roughness: 0.62, metalness: 0 });
    const std = cloned as THREE.MeshStandardMaterial;
    std.vertexColors = true;
    // Subtle emissive tint matching the vertex color, so each lobe
    // gives off a faint colored glow that the bloom pass picks up.
    // Don't tint with a single hue — set emissiveIntensity and let the
    // vertex colors drive the per-fragment emissive (Three.js
    // multiplies emissive * emissiveMap; with no map, vertexColors
    // contribute via the diffuse channel, but raising emissive on a
    // white base color makes the whole mesh glow uniformly. Trick:
    // set emissive to a soft warm color and keep intensity moderate.)
    if (std.emissive !== undefined) {
      std.emissive = new THREE.Color(0x0a3d48);
      std.emissiveIntensity = 0.24;
    }
    std.transparent = true;
    std.opacity = 0.26;
    std.depthWrite = false;
    std.side = THREE.DoubleSide;
    return cloned;
  };
  return Array.isArray(material) ? material.map(cloneOne) : cloneOne(material);
}

function isDominantLobe(lobeId: string, w: ReturnType<typeof lobeWeights>) {
  // Argmax classification — pick the lobe with the highest weight at
  // this point and check it matches. This guarantees every surface
  // vertex gets classified into exactly one lobe, even when no
  // single weight is high enough on a complex anatomical mesh
  // (where the previous fixed thresholds left many vertices with
  // no lobe and produced 0-pool results).
  let maxKey = 'frontal';
  let maxVal = w.wFrontal;
  if (w.wParietal > maxVal) { maxVal = w.wParietal; maxKey = 'parietal'; }
  if (w.wTemporal > maxVal) { maxVal = w.wTemporal; maxKey = 'temporal'; }
  if (w.wOccipital > maxVal) { maxVal = w.wOccipital; maxKey = 'occipital'; }
  return maxKey === lobeId;
}

function pointLobeId(nx: number, ny: number, nz: number): string | null {
  const w = lobeWeights(nx, ny, nz);
  for (const lobe of LOBES) {
    if (isDominantLobe(lobe.id, w)) return lobe.id;
  }
  return null;
}

function buildProceduralBrain(brainGroup: THREE.Group): LobePools {
  const left = buildHemisphere('left');
  const right = buildHemisphere('right');
  brainGroup.add(left.mesh);
  brainGroup.add(right.mesh);
  return { left: left.surface, right: right.surface };
}

function prepareLoadedBrainModel(
  model: THREE.Object3D,
): { pools: LobePools; brainGeos: BrainGeoSnapshot[] } {
  const box = new THREE.Box3().setFromObject(model);
  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  const targetSize = 1.6;
  const scale = targetSize / Math.max(size.x, size.y, size.z, 0.0001);

  model.position.copy(center).multiplyScalar(-scale);
  model.scale.setScalar(scale);
  model.updateMatrixWorld(true);

  const lobePoolCounts = new Map<string, number>();
  const pools: LobePools = { left: [], right: [] };
  const brainGeos: BrainGeoSnapshot[] = [];
  const world = new THREE.Vector3();
  const normal = new THREE.Vector3();

  model.traverse((obj) => {
    if (!(obj instanceof THREE.Mesh)) return;
    const originalGeo = obj.geometry as THREE.BufferGeometry | undefined;
    const position = originalGeo?.getAttribute('position') as THREE.BufferAttribute | undefined;
    if (!originalGeo || !position) return;

    const geo = originalGeo.clone();
    obj.geometry = geo;
    obj.material = cloneMaterialWithVertexColors(obj.material);
    obj.renderOrder = -2;
    obj.updateMatrixWorld(true);

    const pos = geo.getAttribute('position') as THREE.BufferAttribute;
    const colors = new Float32Array(pos.count * 3);
    const baseColors = new Float32Array(pos.count * 3);
    const vertexLobeIds: string[] = new Array(pos.count);
    const step = Math.max(1, Math.floor(pos.count / 900));

    for (let i = 0; i < pos.count; i++) {
      world.fromBufferAttribute(pos, i).applyMatrix4(obj.matrixWorld);
      normal.copy(world).normalize();

      const color = blendedLobeColor(normal.x, normal.y, normal.z);
      colors[i * 3] = color.r;
      colors[i * 3 + 1] = color.g;
      colors[i * 3 + 2] = color.b;
      baseColors[i * 3] = color.r;
      baseColors[i * 3 + 1] = color.g;
      baseColors[i * 3 + 2] = color.b;

      // Argmax lobe assignment for every vertex so the activity-glow
      // pass knows which lobe each vertex belongs to.
      const lobeId = pointLobeId(normal.x, normal.y, normal.z) || 'frontal';
      vertexLobeIds[i] = lobeId;

      if (i % step !== 0) continue;
      const side = world.x < 0 ? 'left' : 'right';
      const poolKey = `${side}-${lobeId}`;
      if ((lobePoolCounts.get(poolKey) ?? 0) >= 120) continue;
      lobePoolCounts.set(poolKey, (lobePoolCounts.get(poolKey) ?? 0) + 1);
      const sample = world.clone();
      pools[side].push(sample);
    }

    geo.setAttribute('color', new THREE.BufferAttribute(colors, 3));
    geo.computeVertexNormals();
    brainGeos.push({ mesh: obj, baseColors, vertexLobeIds });
  });

  return { pools, brainGeos };
}

// Walk every brain mesh's vertex color attribute and brighten each
// vertex by its lobe's activity intensity. Bloom catches the bright
// spots, so heavily-active lobes glow visibly. Cheap O(verts) on
// each entry change — typically called once per refresh.
// Reference activity count where a lobe is considered "fully lit".
// Using a fixed reference (rather than max-of-current-lobes) means
// unchecking the busiest lobe doesn't cause the others to suddenly
// look brighter — each lobe's brightness reflects its actual entry
// count, independent of how active its siblings are.
const ACTIVITY_FULL_LIT = 30;

function applyActivityGlow(
  brainGeos: BrainGeoSnapshot[],
  activityByLobe: Record<string, number>,
  hoveredLobe: string | null,
  glowIntensity: number,
) {
  if (brainGeos.length === 0) return;
  for (const geo of brainGeos) {
    const colorAttr = geo.mesh.geometry.getAttribute('color') as THREE.BufferAttribute | undefined;
    if (!colorAttr) continue;
    const arr = colorAttr.array as Float32Array;
    const lobeIds = geo.vertexLobeIds;
    const base = geo.baseColors;
    for (let i = 0; i < lobeIds.length; i++) {
      const lobeId = lobeIds[i];
      const activity = activityByLobe[lobeId] || 0;
      // Absolute activity scaled to a fixed reference. Quiet lobes
      // sit at baseline (1.6×); a lobe with 30+ entries reaches the
      // full activity boost regardless of how busy its siblings are.
      const t = Math.min(1, activity / ACTIVITY_FULL_LIT);
      let boost = 1.6 + Math.pow(t, 0.5) * 0.9 * glowIntensity;
      if (hoveredLobe && lobeId === hoveredLobe) {
        boost *= 1.4;
      }
      arr[i * 3]     = Math.min(2.4, base[i * 3]     * boost);
      arr[i * 3 + 1] = Math.min(2.4, base[i * 3 + 1] * boost);
      arr[i * 3 + 2] = Math.min(2.4, base[i * 3 + 2] * boost);
    }
    colorAttr.needsUpdate = true;
  }
}

// Pick a deterministic dot position for an entry inside its lobe's
// surface points. Stable across renders so the visualization doesn't
// shuffle on every poll.
// Cache sorted lobe regions per (surfaceArrayRef, lobeId). The sort
// is the thing that makes the layout *predictable* — slot 0 lands at
// the top of the lobe and slots fill downward, so a chronological
// entry list maps to a chronological top-to-bottom band on the brain.
// WeakMap keys on the surface array reference so cache invalidates
// naturally when the brain is rebuilt (procedural fallback, etc).
const sortedRegionCache = new WeakMap<THREE.Vector3[], Map<string, THREE.Vector3[]>>();

function getSortedRegion(surface: THREE.Vector3[], lobeId: string): THREE.Vector3[] {
  let perLobe = sortedRegionCache.get(surface);
  if (!perLobe) {
    perLobe = new Map();
    sortedRegionCache.set(surface, perLobe);
  }
  let region = perLobe.get(lobeId);
  if (region) return region;

  region = surface.filter((v) => {
    const len = Math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z);
    if (len < 1e-6) return false;
    const nx = v.x / len, ny = v.y / len, nz = v.z / len;
    const w = lobeWeights(nx, ny, nz);
    return isDominantLobe(lobeId, w);
  });
  // Sort top-to-bottom (high Y first), tie-break front-to-back. This
  // gives every lobe a stable column of slots: slot 0 at the top of
  // the cortex, last slot at the bottom. Combined with a chronological
  // entry sort, the user can read recency by scanning down a lobe.
  region.sort((a, b) => (b.y - a.y) || (b.z - a.z) || (a.x - b.x));
  perLobe.set(lobeId, region);
  return region;
}

function pickSurface(surface: THREE.Vector3[], lobeId: string, slotIdx: number): THREE.Vector3 | null {
  const region = getSortedRegion(surface, lobeId);
  if (region.length === 0) return null;
  return region[slotIdx % region.length];
}

// ── Component ───────────────────────────────────────────────────────

interface BrainFilters {
  query: string;
  hiddenAgents: Set<string>;
  hiddenLobes: Set<string>;
  nodeSize: number;
  quality: BrainQualityPreset;
  lod: BrainLodMode;
  silhouette: BrainSilhouetteMode;
}

const DEFAULT_FILTERS: BrainFilters = {
  query: '',
  hiddenAgents: new Set(),
  hiddenLobes: new Set(),
  nodeSize: 1,
  quality: 'high',
  lod: 'detail',
  silhouette: 'neural',
};

const MAX_GRAPH_NODES_3D = 260;
const MAX_GRAPH_EDGES_3D = 420;
const MAX_ACTIVITY_DOTS_3D = 180;
const GRAPH_EDGE_SEGMENTS = 28;
const GRAPH_AMBIENT_PULSE_COUNT = 3;
const GRAPH_AMBIENT_PULSE_EDGE_SECONDS = 1.35;
const GRAPH_FOCUS_PULSE_EDGE_SECONDS = 0.82;
const GRAPH_VOLUME_Y_OFFSET = 0.14;
const GRAPH_VOLUME_Y_SCALE = 0.78;

export type BrainQualityPreset = 'low' | 'medium' | 'high';
export type BrainLodMode = 'clusters' | 'balanced' | 'detail';
export type BrainSilhouetteMode = 'minimal' | 'neural' | 'anatomical';

interface BrainQualityConfig {
  label: string;
  nodeCap: number;
  edgeCap: number;
  activityCap: number;
  pixelRatioCap: number;
  edgeSegments: number;
  nodeSegments: number;
  activitySegments: number;
  haloSegments: number;
  simulationTicks: number;
  bloomStrength: number;
  bloomRadius: number;
  bloomThreshold: number;
}

const BRAIN_QUALITY_PRESETS: Record<BrainQualityPreset, BrainQualityConfig> = {
  low: {
    label: 'Low',
    nodeCap: 140,
    edgeCap: 180,
    activityCap: 60,
    pixelRatioCap: 1,
    edgeSegments: 14,
    nodeSegments: 8,
    activitySegments: 8,
    haloSegments: 6,
    simulationTicks: 48,
    bloomStrength: 0.24,
    bloomRadius: 0.22,
    bloomThreshold: 0.86,
  },
  medium: {
    label: 'Medium',
    nodeCap: 220,
    edgeCap: 320,
    activityCap: 120,
    pixelRatioCap: 1.5,
    edgeSegments: 22,
    nodeSegments: 11,
    activitySegments: 10,
    haloSegments: 8,
    simulationTicks: 68,
    bloomStrength: 0.34,
    bloomRadius: 0.28,
    bloomThreshold: 0.82,
  },
  high: {
    label: 'High',
    nodeCap: MAX_GRAPH_NODES_3D,
    edgeCap: MAX_GRAPH_EDGES_3D,
    activityCap: MAX_ACTIVITY_DOTS_3D,
    pixelRatioCap: 2,
    edgeSegments: GRAPH_EDGE_SEGMENTS,
    nodeSegments: 14,
    activitySegments: 12,
    haloSegments: 10,
    simulationTicks: 90,
    bloomStrength: 0.42,
    bloomRadius: 0.34,
    bloomThreshold: 0.78,
  },
};

const BRAIN_LOD_MODES: Record<BrainLodMode, { label: string; scale: number; clusterSeed: number }> = {
  clusters: { label: 'Clusters', scale: 0.62, clusterSeed: 0.46 },
  balanced: { label: 'Balanced', scale: 0.84, clusterSeed: 0.24 },
  detail: { label: 'Detail', scale: 1, clusterSeed: 0 },
};

const BRAIN_SILHOUETTES: Record<BrainSilhouetteMode, { label: string; aura: number; opacity: number; emissive: number }> = {
  minimal: { label: 'Minimal', aura: 0, opacity: 0.18, emissive: 0.10 },
  neural: { label: 'Neural', aura: 1, opacity: 0.26, emissive: 0.24 },
  anatomical: { label: 'Anatomy', aura: 0.42, opacity: 0.34, emissive: 0.16 },
};

export interface Brain3DRenderBudget {
  nodes: number;
  edges: number;
  activity: number;
}

export function getBrain3DRenderBudget(quality: BrainQualityPreset, lod: BrainLodMode): Brain3DRenderBudget {
  const config = BRAIN_QUALITY_PRESETS[quality] ?? BRAIN_QUALITY_PRESETS.high;
  const lodConfig = BRAIN_LOD_MODES[lod] ?? BRAIN_LOD_MODES.detail;
  return {
    nodes: clampInt(Math.round(config.nodeCap * lodConfig.scale), 32, MAX_GRAPH_NODES_3D),
    edges: clampInt(Math.round(config.edgeCap * lodConfig.scale), 32, MAX_GRAPH_EDGES_3D),
    activity: clampInt(Math.round(config.activityCap * lodConfig.scale), 0, MAX_ACTIVITY_DOTS_3D),
  };
}

type BrainDotItem =
  | {
      itemKind: 'node';
      id: string;
      agent_id: string;
      lobe: string;
      label: string;
      action: string;
      summary: string;
      created_at: number;
      searchText: string;
      degree: number;
      node: BrainGraphNode;
    }
  | {
      itemKind: 'activity';
      id: string;
      agent_id: string;
      lobe: string;
      label: string;
      action: string;
      summary: string;
      artifacts: string | null;
      chat_id: string;
      created_at: number;
      searchText: string;
      entry: HiveEntry;
    };

interface DotData {
  item: BrainDotItem;
  pos: THREE.Vector3;
  mesh: THREE.Mesh;
  halo: THREE.Mesh;
  forceNode?: BrainForceNode;
}

function forEachDotData(dotMap: Map<THREE.Object3D, DotData>, visit: (dot: DotData) => void) {
  dotMap.forEach((dot, object) => {
    if (object === dot.mesh) visit(dot);
  });
}

interface BrainForceNode extends SimulationNodeDatum {
  id: string;
  item: Extract<BrainDotItem, { itemKind: 'node' }>;
  anchor: THREE.Vector3;
  mesh: THREE.Mesh;
  halo: THREE.Mesh;
  dotData: DotData;
  radius: number;
  pinned: boolean;
}

interface BrainForceLink extends SimulationLinkDatum<BrainForceNode> {
  edge: BrainGraphEdge;
  line: THREE.Line;
  baseOpacity: number;
}

interface GraphPulse {
  mesh: THREE.Mesh;
  material: THREE.MeshBasicMaterial;
}

interface GraphForceState {
  simulation: Simulation<BrainForceNode, BrainForceLink>;
  nodes: BrainForceNode[];
  links: BrainForceLink[];
  pulses: GraphPulse[];
  nodeById: Map<string, BrainForceNode>;
  adjacency: Map<string, Set<string>>;
}

interface GraphDragState {
  node: BrainForceNode;
  pointerId: number;
  plane: THREE.Plane;
  offset: THREE.Vector3;
  startX: number;
  startY: number;
  moved: boolean;
}

// Per-mesh data captured when the GLB loads, used to modulate vertex
// emissive based on per-lobe activity. baseColors holds the original
// lobe-weighted vertex colors; vertexLobeIds[i] is the dominant lobe
// for vertex i. The activity-glow effect uses these to recompute the
// `color` attribute without touching geometry topology.
interface BrainGeoSnapshot {
  mesh: THREE.Mesh;
  baseColors: Float32Array;
  vertexLobeIds: string[];
}

// Approximate centroids of each lobe in the brain's local space, used
// as endpoints when an agent activity event spawns a cross-lobe
// synapse arc. Values picked to land just inside the cortex of the
// 1.6-unit normalized brain. Two temporal centroids (left and right)
// so arcs don't always emerge from the same point.
const LOBE_CENTROIDS: Record<string, THREE.Vector3> = {
  frontal: new THREE.Vector3(0, 0.20, 0.62),
  parietal: new THREE.Vector3(0, 0.62, 0.05),
  temporal: new THREE.Vector3(0.58, -0.30, 0.10),
  temporal_l: new THREE.Vector3(-0.58, -0.30, 0.10),
  occipital: new THREE.Vector3(0, 0.20, -0.62),
};

const LOBE_VOLUME_CENTERS: Record<string, THREE.Vector3> = {
  frontal: new THREE.Vector3(0, 0.12, 0.42),
  parietal: new THREE.Vector3(0, 0.40, 0.02),
  temporal: new THREE.Vector3(0.38, -0.26, 0.02),
  occipital: new THREE.Vector3(0, 0.12, -0.42),
};

const BRAIN_VOLUME_LIMIT = new THREE.Vector3(0.58, 0.52, 0.78);

interface SynapseArc {
  mesh: THREE.Mesh;
  material: THREE.ShaderMaterial;
  // Rendered to the synapsesGroup (parented to brainGroup so the arc
  // rotates and breathes with the brain). createdAt drives uProgress.
  createdAt: number;
  lifeSec: number;
}

function nodeLobe(node: BrainGraphNode): string {
  const scope = normalizeScopeId(node.scope_id);
  if ((node.scope_type === 'persona' || node.scope_type === 'agent') && scope) {
    return lobeFor(scope);
  }
  if (node.scope_type === 'team') return 'parietal';
  if (node.scope_type === 'room') return 'temporal';
  if (node.kind === 'decision' || node.kind === 'session') return 'frontal';
  if (node.kind === 'entity') return 'parietal';
  if (node.kind === 'note') return 'occipital';
  return 'temporal';
}

function nodeAgentId(node: BrainGraphNode): string {
  const scope = normalizeScopeId(node.scope_id);
  if (node.scope_type === 'persona' || node.scope_type === 'agent' || node.scope_type === 'room') {
    return scope || 'main';
  }
  return 'main';
}

function normalizeScopeId(value: unknown): string {
  const text = String(value || 'main').trim();
  return text === 'default' ? 'main' : text || 'main';
}

function graphNodeSearchText(node: BrainGraphNode): string {
  return [
    node.id,
    node.label,
    node.kind,
    node.scope_type,
    node.scope_id,
    node.source_path,
    node.section_title,
    node.text,
    ...(Array.isArray(node.tags) ? node.tags : []),
  ].map((value) => String(value ?? '').toLowerCase()).join(' ');
}

function graphNodeSummary(node: BrainGraphNode): string {
  const text = String(node.text || '').trim();
  if (text) return text;
  if (node.section_title) return String(node.section_title);
  return `${node.scope_type}/${normalizeScopeId(node.scope_id)} ${node.kind}`;
}

export function getBrainNodeDetailBody(node: BrainGraphNode, relatedNodes: BrainGraphNode[] = []): string {
  const text = String(node.text || '').trim();
  if (text) return text;
  if (node.kind === 'note' || relatedNodes.length > 0) {
    const textBearingRelated = relatedNodes
      .filter((related) => String(related.text || '').trim())
      .sort((a, b) => Number(b.created_at ?? 0) - Number(a.created_at ?? 0));
    const derived = textBearingRelated
      .slice(0, 3)
      .map((related) => {
        const body = String(related.text || '').trim();
        const heading = related.section_title || related.label;
        return heading ? `## ${heading}\n\n${body}` : body;
      })
      .filter(Boolean)
      .join('\n\n---\n\n');
    if (derived) return derived;
  }
  const section = String(node.section_title || '').trim();
  if (section) return section;
  const source = String(node.source_path || '').trim();
  if (source) {
    return `Loaded body is not available in the current graph page.\n\nSource: ${source}`;
  }
  return "";
}

function graphNodeColor(node: BrainGraphNode, degree = 0): string {
  const tags = Array.isArray(node.tags) ? node.tags.map((tag) => String(tag).toLowerCase()) : [];
  const label = `${node.id} ${node.label} ${node.source_path ?? ''}`.toLowerCase();
  const isHub = tags.includes('index') || tags.includes('moc') || /\b(index|moc)\b/.test(label);
  const isCompiled = tags.includes('auto-compiled') || tags.includes('connection');
  switch (node.kind) {
    case 'note':
      if (degree >= 25) return '#fff7dc';
      if (isHub || isCompiled || degree >= 10) return '#ffc45f';
      if (degree >= 4) return '#74f6ff';
      return '#f6a6d7';
    case 'entity': return '#74f6ff';
    case 'decision': return '#ffc45f';
    case 'session': return '#f6a6d7';
    default:
      switch (node.scope_type) {
        case 'persona': return '#f6a6d7';
        case 'agent': return '#ffc45f';
        case 'team': return '#74f6ff';
        case 'room': return '#74f6ff';
        default: return '#74f6ff';
      }
  }
}

function graphNodeRadius(node: BrainGraphNode, degree = 0): number {
  const degreeBoost = Math.min(0.011, Math.log1p(Math.max(0, degree)) * 0.0025);
  if (node.kind === 'note') return 0.011 + degreeBoost;
  if (node.kind === 'session' || node.kind === 'decision') return 0.015 + degreeBoost * 0.78;
  if (node.kind === 'entity') return 0.014 + degreeBoost * 0.72;
  return 0.010 + degreeBoost * 0.68;
}

function makeNodeItem(node: BrainGraphNode, degree = 0): BrainDotItem {
  const lobe = nodeLobe(node);
  const agent = nodeAgentId(node);
  return {
    itemKind: 'node',
    id: `node:${node.id}`,
    agent_id: agent,
    lobe,
    label: node.label || node.id,
    action: node.kind || 'memory_node',
    summary: graphNodeSummary(node),
    created_at: normalizeNodeTimestamp(node.created_at),
    searchText: graphNodeSearchText(node),
    degree,
    node,
  };
}

function makeActivityItem(entry: HiveEntry): BrainDotItem {
  const lobe = lobeFor(entry.agent_id);
  return {
    itemKind: 'activity',
    id: `activity:${entry.id}`,
    agent_id: entry.agent_id,
    lobe,
    label: entry.summary,
    action: entry.action,
    summary: entry.summary,
    artifacts: entry.artifacts,
    chat_id: entry.chat_id,
    created_at: entry.created_at,
    searchText: [entry.agent_id, entry.action, entry.summary, entry.chat_id, entry.artifacts].map((value) => String(value ?? '').toLowerCase()).join(' '),
    entry,
  };
}

function selectGraphNodes(
  nodes: BrainGraphNode[],
  edges: BrainGraphEdge[],
  options: { nodeCap: number; lod: BrainLodMode },
): BrainGraphNode[] {
  const nodeCap = clampInt(options.nodeCap, 1, MAX_GRAPH_NODES_3D);
  if (nodes.length <= nodeCap) return nodes;
  const degree = new Map<string, number>();
  const byId = new Map(nodes.map((node) => [node.id, node]));
  for (const edge of edges) {
    degree.set(edge.source, (degree.get(edge.source) ?? 0) + 1);
    degree.set(edge.target, (degree.get(edge.target) ?? 0) + 1);
  }
  const rankNodes = (a: BrainGraphNode, b: BrainGraphNode) => {
    const da = degree.get(a.id) ?? 0;
    const db = degree.get(b.id) ?? 0;
    if (db !== da) return db - da;
    const ka = a.kind === 'note' ? 0 : 1;
    const kb = b.kind === 'note' ? 0 : 1;
    if (ka !== kb) return ka - kb;
    return a.id.localeCompare(b.id);
  };
  const rankedNodes = [...nodes].sort(rankNodes);
  const selected: BrainGraphNode[] = [];
  const selectedIds = new Set<string>();
  const add = (node: BrainGraphNode | undefined) => {
    if (!node || selectedIds.has(node.id) || selected.length >= nodeCap) return false;
    selected.push(node);
    selectedIds.add(node.id);
    return true;
  };
  const rankedEdges = edges
    .filter((edge) => byId.has(edge.source) && byId.has(edge.target))
    .sort((a, b) => {
      const ak = a.kind === 'source' ? 1 : 0;
      const bk = b.kind === 'source' ? 1 : 0;
      if (ak !== bk) return ak - bk;
      const ad = (degree.get(a.source) ?? 0) + (degree.get(a.target) ?? 0);
      const bd = (degree.get(b.source) ?? 0) + (degree.get(b.target) ?? 0);
      if (bd !== ad) return bd - ad;
      return a.id.localeCompare(b.id);
    });

  const clusterSeedTarget = Math.floor(nodeCap * (BRAIN_LOD_MODES[options.lod]?.clusterSeed ?? 0));
  if (clusterSeedTarget > 0) {
    const groups = new Map<string, BrainGraphNode[]>();
    for (const node of rankedNodes) {
      const key = `${nodeLobe(node)}:${node.scope_type}:${node.kind}`;
      const group = groups.get(key) ?? [];
      group.push(node);
      groups.set(key, group);
    }
    const orderedGroups = Array.from(groups.values())
      .map((group) => group.filter((node) => (degree.get(node.id) ?? 0) > 0))
      .filter((group) => group.length > 0)
      .sort((a, b) => {
        const ad = degree.get(a[0].id) ?? 0;
        const bd = degree.get(b[0].id) ?? 0;
        if (bd !== ad) return bd - ad;
        return a[0].id.localeCompare(b[0].id);
      });

    for (let round = 0; selected.length < clusterSeedTarget && round < 5; round++) {
      let addedThisRound = false;
      for (const group of orderedGroups) {
        if (selected.length >= clusterSeedTarget) break;
        if (add(group[round])) addedThisRound = true;
      }
      if (!addedThisRound) break;
    }
  }

  for (const edge of rankedEdges) {
    if (selected.length >= nodeCap) break;
    const sourceSelected = selectedIds.has(edge.source);
    const targetSelected = selectedIds.has(edge.target);
    if (!sourceSelected && !targetSelected && selected.length <= nodeCap - 2) {
      add(byId.get(edge.source));
      add(byId.get(edge.target));
    } else if (sourceSelected && !targetSelected) {
      add(byId.get(edge.target));
    } else if (!sourceSelected && targetSelected) {
      add(byId.get(edge.source));
    }
  }

  for (const node of rankedNodes) {
    if (selected.length >= nodeCap) break;
    add(node);
  }

  return selected;
}

function selectGraphEdges(
  edges: BrainGraphEdge[],
  visibleNodeIds: Set<string>,
  degreeByNodeId: Map<string, number>,
  edgeCap = MAX_GRAPH_EDGES_3D,
): BrainGraphEdge[] {
  const cappedEdges = clampInt(edgeCap, 1, MAX_GRAPH_EDGES_3D);
  const candidates = edges
    .filter((edge) => visibleNodeIds.has(edge.source) && visibleNodeIds.has(edge.target))
    .sort((a, b) => {
      const ak = a.kind === 'source' ? 1 : 0;
      const bk = b.kind === 'source' ? 1 : 0;
      if (ak !== bk) return ak - bk;
      const ad = (degreeByNodeId.get(a.source) ?? 0) + (degreeByNodeId.get(a.target) ?? 0);
      const bd = (degreeByNodeId.get(b.source) ?? 0) + (degreeByNodeId.get(b.target) ?? 0);
      if (bd !== ad) return bd - ad;
      return a.id.localeCompare(b.id);
    });

  const selected: BrainGraphEdge[] = [];
  const selectedIds = new Set<string>();
  const coveredNodeIds = new Set<string>();

  for (const edge of candidates) {
    if (selected.length >= cappedEdges) break;
    if (coveredNodeIds.has(edge.source) && coveredNodeIds.has(edge.target)) continue;
    selected.push(edge);
    selectedIds.add(edge.id);
    coveredNodeIds.add(edge.source);
    coveredNodeIds.add(edge.target);
  }

  for (const edge of candidates) {
    if (selected.length >= cappedEdges) break;
    if (selectedIds.has(edge.id)) continue;
    selected.push(edge);
  }

  return selected;
}

function itemVisible(item: BrainDotItem, filters: BrainFilters, agentFilter: string, showActivity: boolean): boolean {
  if (item.itemKind === 'activity' && !showActivity) return false;
  if (filters.hiddenAgents.has(item.agent_id)) return false;
  if (filters.hiddenLobes.has(item.lobe)) return false;
  if (agentFilter !== 'all' && item.itemKind === 'activity' && item.agent_id !== agentFilter) return false;
  if (filters.query && !item.searchText.includes(filters.query.toLowerCase())) return false;
  return true;
}

function normalizeNodeTimestamp(value: unknown): number {
  if (typeof value === 'number' && Number.isFinite(value)) {
    const seconds = value > 10_000_000_000 ? value / 1000 : value;
    if (seconds > 0) return seconds;
  }
  return Date.now() / 1000;
}

function stringHash(input: string): number {
  let value = 2166136261;
  for (const char of input) {
    value ^= char.charCodeAt(0);
    value = Math.imul(value, 16777619);
  }
  return value >>> 0;
}

function seededUnit(input: string): number {
  return stringHash(input) / 0xffffffff;
}

function signedSeed(input: string): number {
  return seededUnit(input) * 2 - 1;
}

function clampInt(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, Math.floor(value)));
}

function clampToBrainVolume(point: THREE.Vector3): THREE.Vector3 {
  const nx = point.x / BRAIN_VOLUME_LIMIT.x;
  const ny = point.y / BRAIN_VOLUME_LIMIT.y;
  const nz = point.z / BRAIN_VOLUME_LIMIT.z;
  const length = Math.sqrt(nx * nx + ny * ny + nz * nz);
  if (length <= 1) return point;
  point.x /= length;
  point.y /= length;
  point.z /= length;
  return point;
}

function buildNeuralAuraShell(): THREE.Group {
  const group = new THREE.Group();
  group.renderOrder = 0;

  const addCurve = (
    coords: Array<[number, number, number]>,
    color: number,
    opacity: number,
    closed = false,
    samples = 96,
  ) => {
    const curve = new THREE.CatmullRomCurve3(
      coords.map(([x, y, z]) => new THREE.Vector3(x, y, z)),
      closed,
      'catmullrom',
      0.38,
    );
    const geometry = new THREE.BufferGeometry().setFromPoints(curve.getPoints(samples));
    const material = new THREE.LineBasicMaterial({
      color,
      transparent: true,
      opacity,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    material.userData.baseOpacity = opacity;
    const line = new THREE.Line(geometry, material);
    line.frustumCulled = false;
    group.add(line);
  };

  const outline: Array<[number, number]> = [
    [0.06, -0.82],
    [0.33, -0.68],
    [0.48, -0.38],
    [0.52, 0.04],
    [0.44, 0.46],
    [0.22, 0.72],
    [-0.06, 0.76],
    [-0.36, 0.62],
    [-0.56, 0.30],
    [-0.54, -0.08],
    [-0.36, -0.30],
    [-0.12, -0.34],
  ];
  for (const [x, opacity] of [[-0.20, 0.06], [0, 0.18], [0.20, 0.08]] as const) {
    addCurve(outline.map(([y, z]) => [x, y, z]), 0x5ee7ff, opacity, true, 128);
  }

  const folds: Array<Array<[number, number, number]>> = [
    [[-0.12, 0.34, -0.56], [-0.04, 0.43, -0.20], [0.06, 0.40, 0.20], [0.14, 0.26, 0.56]],
    [[0.08, 0.18, -0.62], [0.02, 0.30, -0.30], [-0.08, 0.28, 0.06], [-0.12, 0.12, 0.46]],
    [[-0.16, 0.02, -0.52], [-0.03, 0.12, -0.22], [0.10, 0.06, 0.12], [0.16, -0.08, 0.50]],
    [[0.12, -0.18, -0.40], [0.00, -0.08, -0.10], [-0.12, -0.10, 0.24], [-0.18, -0.24, 0.48]],
    [[-0.02, -0.30, -0.36], [0.12, -0.24, -0.06], [0.16, -0.28, 0.24], [0.04, -0.34, 0.54]],
    [[0.18, 0.46, -0.18], [0.04, 0.54, 0.06], [-0.08, 0.50, 0.32], [-0.16, 0.34, 0.60]],
  ];
  folds.forEach((coords, idx) => {
    addCurve(coords, idx % 3 === 1 ? 0xffb86b : 0x5ee7ff, idx % 3 === 1 ? 0.24 : 0.20, false, 72);
  });

  const lobeKeys = ['frontal', 'parietal', 'temporal', 'occipital'];
  for (let i = 0; i < 18; i++) {
    const a = LOBE_VOLUME_CENTERS[lobeKeys[i % lobeKeys.length]].clone();
    const b = LOBE_VOLUME_CENTERS[lobeKeys[(i * 2 + 1) % lobeKeys.length]].clone();
    a.x += signedSeed(`aura-a-x-${i}`) * 0.12;
    a.y += signedSeed(`aura-a-y-${i}`) * 0.10;
    a.z += signedSeed(`aura-a-z-${i}`) * 0.12;
    b.x += signedSeed(`aura-b-x-${i}`) * 0.12;
    b.y += signedSeed(`aura-b-y-${i}`) * 0.10;
    b.z += signedSeed(`aura-b-z-${i}`) * 0.12;
    const mid = a.clone().lerp(b, 0.5);
    mid.x += signedSeed(`aura-m-x-${i}`) * 0.14;
    mid.y += 0.08 + signedSeed(`aura-m-y-${i}`) * 0.12;
    mid.z += signedSeed(`aura-m-z-${i}`) * 0.14;
    clampToBrainVolume(a);
    clampToBrainVolume(b);
    clampToBrainVolume(mid);
    addCurve(
      [[a.x, a.y, a.z], [mid.x, mid.y, mid.z], [b.x, b.y, b.z]],
      i % 4 === 0 ? 0xffb86b : 0x62f4ff,
      i % 4 === 0 ? 0.22 : 0.14,
      false,
      44,
    );
  }

  const makeSparkField = (count: number, color: number, opacity: number, prefix: string) => {
    const positions = new Float32Array(count * 3);
    for (let i = 0; i < count; i++) {
      const center = LOBE_VOLUME_CENTERS[lobeKeys[i % lobeKeys.length]].clone();
      center.x += signedSeed(`${prefix}-x-${i}`) * 0.42;
      center.y += signedSeed(`${prefix}-y-${i}`) * 0.34;
      center.z += signedSeed(`${prefix}-z-${i}`) * 0.46;
      clampToBrainVolume(center);
      positions[i * 3] = center.x;
      positions[i * 3 + 1] = center.y;
      positions[i * 3 + 2] = center.z;
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    const material = new THREE.PointsMaterial({
      color,
      size: 0.010,
      sizeAttenuation: true,
      transparent: true,
      opacity,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    });
    material.userData.baseOpacity = opacity;
    const points = new THREE.Points(geometry, material);
    points.frustumCulled = false;
    group.add(points);
  };

  makeSparkField(120, 0x5ee7ff, 0.28, 'cyan-spark');
  makeSparkField(72, 0xffb86b, 0.48, 'gold-spark');

  return group;
}

function volumePointForItem(item: BrainDotItem, slotIdx: number): THREE.Vector3 {
  const center = (LOBE_VOLUME_CENTERS[item.lobe] || LOBE_VOLUME_CENTERS.frontal).clone();
  const seed = `${item.id}:${item.agent_id}:${slotIdx}`;
  if (item.lobe === 'temporal') {
    center.x *= signedSeed(`${seed}:side`) < 0 ? -1 : 1;
  }

  const theta = seededUnit(`${seed}:theta`) * Math.PI * 2;
  const phi = Math.acos(2 * seededUnit(`${seed}:phi`) - 1);
  const radius = Math.cbrt(seededUnit(`${seed}:radius`)) * (item.itemKind === 'node' ? 0.24 : 0.18);
  const spiral = slotIdx * 0.36;
  const offset = new THREE.Vector3(
    Math.sin(phi) * Math.cos(theta + spiral) * radius * 0.92,
    Math.cos(phi) * radius * 0.72 + signedSeed(`${seed}:y`) * 0.035,
    Math.sin(phi) * Math.sin(theta + spiral) * radius,
  );
  const point = center.add(offset);
  point.y = point.y * GRAPH_VOLUME_Y_SCALE + GRAPH_VOLUME_Y_OFFSET;
  if (item.itemKind === 'activity') {
    point.multiplyScalar(0.92);
  }
  return clampToBrainVolume(point);
}

function resolveColor(raw: string | undefined, fallback: string): string {
  let color = raw || fallback;
  if (typeof color === 'string' && color.startsWith('var(') && typeof document !== 'undefined') {
    const match = color.match(/var\((--[^)]+)\)/);
    if (match) {
      const resolved = getComputedStyle(document.documentElement).getPropertyValue(match[1]).trim();
      if (resolved) color = resolved;
    }
  }
  return color;
}

function itemColor(item: BrainDotItem, agentColors: Record<string, string>): string {
  if (item.itemKind === 'node') return graphNodeColor(item.node, item.degree);
  return resolveColor(agentColors[item.agent_id], '#888');
}

function quadraticPoint(from: THREE.Vector3, mid: THREE.Vector3, to: THREE.Vector3, t: number): THREE.Vector3 {
  const one = 1 - t;
  return new THREE.Vector3(
    one * one * from.x + 2 * one * t * mid.x + t * t * to.x,
    one * one * from.y + 2 * one * t * mid.y + t * t * to.y,
    one * one * from.z + 2 * one * t * mid.z + t * t * to.z,
  );
}

function graphEdgeMidpoint(from: THREE.Vector3, to: THREE.Vector3): THREE.Vector3 {
  const mid = from.clone().add(to).multiplyScalar(0.5);
  const lift = mid.clone();
  if (lift.lengthSq() > 0) {
    lift.normalize().multiplyScalar(0.10 + from.distanceTo(to) * 0.10);
    mid.add(lift);
  }
  return clampToBrainVolume(mid);
}

function graphEdgePoint(from: THREE.Vector3, to: THREE.Vector3, t: number, mid = graphEdgeMidpoint(from, to)): THREE.Vector3 {
  return quadraticPoint(from, mid, to, t);
}

function setGraphEdgeMeshPoints(line: THREE.Line, from: THREE.Vector3, to: THREE.Vector3) {
  const position = line.geometry.getAttribute('position') as THREE.BufferAttribute;
  const mid = graphEdgeMidpoint(from, to);
  const segments = Number(line.userData.edgeSegments || GRAPH_EDGE_SEGMENTS);
  for (let i = 0; i <= segments; i++) {
    const t = i / segments;
    const point = graphEdgePoint(from, to, t, mid);
    position.setXYZ(i, point.x, point.y, point.z);
  }
  position.needsUpdate = true;
  line.geometry.computeBoundingSphere();
}

function makeGraphEdgeMesh(from: THREE.Vector3, to: THREE.Vector3, edge: BrainGraphEdge, edgeSegments = GRAPH_EDGE_SEGMENTS): THREE.Line {
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(new Float32Array((edgeSegments + 1) * 3), 3));
  const mat = new THREE.LineBasicMaterial({
    color: edge.kind === 'source' ? '#ffb86b' : '#5ee7ff',
    transparent: true,
    opacity: edge.kind === 'source' ? 0.26 : 0.34,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  });
  const line = new THREE.Line(geo, mat);
  line.userData.edgeSegments = edgeSegments;
  line.renderOrder = 3;
  setGraphEdgeMeshPoints(line, from, to);
  return line;
}

function graphPulseColor(edge: BrainGraphEdge): THREE.Color {
  return new THREE.Color(edge.kind === 'source' ? '#ffc45f' : '#74f6ff').multiplyScalar(1.22);
}

function makeGraphPulseMesh(): GraphPulse {
  const material = new THREE.MeshBasicMaterial({
    color: '#74f6ff',
    transparent: true,
    opacity: 0,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
    toneMapped: false,
  });
  const mesh = new THREE.Mesh(new THREE.SphereGeometry(0.005, 8, 8), material);
  mesh.renderOrder = 4;
  mesh.visible = false;
  return { mesh, material };
}

function forceNodePosition(node: BrainForceNode): THREE.Vector3 {
  return new THREE.Vector3(node.x ?? 0, node.y ?? 0, node.z ?? 0);
}

function forceLinkEndpoint(
  endpoint: string | number | BrainForceNode,
  graphForce: GraphForceState,
): BrainForceNode | null {
  if (typeof endpoint === 'object') return endpoint;
  return graphForce.nodeById.get(String(endpoint)) ?? null;
}

function buildGraphAdjacency(links: BrainForceLink[]): Map<string, Set<string>> {
  const adjacency = new Map<string, Set<string>>();
  const add = (from: string, to: string) => {
    const set = adjacency.get(from) ?? new Set<string>();
    set.add(to);
    adjacency.set(from, set);
  };
  for (const link of links) {
    const source = typeof link.source === 'object' ? link.source.id : String(link.source);
    const target = typeof link.target === 'object' ? link.target.id : String(link.target);
    add(source, target);
    add(target, source);
  }
  return adjacency;
}

function relatedToDraggedNode(item: BrainDotItem, graphForce: GraphForceState, draggedId: string): boolean {
  if (item.itemKind !== 'node') return false;
  const id = item.node.id;
  return id === draggedId || !!graphForce.adjacency.get(draggedId)?.has(id);
}

function graphNodeIdFromItemId(itemId: string | null): string | null {
  if (!itemId?.startsWith('node:')) return null;
  return itemId.slice('node:'.length) || null;
}

function graphNodeRelated(item: BrainDotItem, graphForce: GraphForceState, nodeId: string): boolean {
  if (item.itemKind !== 'node') return false;
  const id = item.node.id;
  return id === nodeId || !!graphForce.adjacency.get(nodeId)?.has(id);
}

export function BrainGraph3D({ data, entries, agentFilter, agentColors, blurOn, showActivity = true }: Props) {
  const webglAvailable = useMemo(() => hasWebGL(), []);
  const wrapRef = useRef<HTMLDivElement>(null);
  const sceneStateRef = useRef<{
    scene: THREE.Scene;
    camera: THREE.PerspectiveCamera;
    renderer: THREE.WebGLRenderer;
    composer: EffectComposer;
    controls: OrbitControls;
    leftSurface: THREE.Vector3[];
    rightSurface: THREE.Vector3[];
    brainGeos: BrainGeoSnapshot[];
    dotsGroup: THREE.Group;
    graphEdgesGroup: THREE.Group;
    synapsesGroup: THREE.Group;
    auraShell: THREE.Group;
    synapses: SynapseArc[];
    spawnSynapse: (fromLobe: string, toLobe: string, color: THREE.Color) => void;
    bloom: UnrealBloomPass;
    raycaster: THREE.Raycaster;
    pointer: THREE.Vector2;
    dotMap: Map<THREE.Object3D, DotData>;
    graphForce: GraphForceState | null;
    drag: GraphDragState | null;
    suppressClickUntil: number;
    rafId: number;
    lastInteract: number;
    brainGroup: THREE.Group;
    markInteract: () => void;
    cleanup: () => void;
  } | null>(null);

  // Track which entry ids we've already converted into synapse arcs,
  // so a re-render of the same `entries` list doesn't spawn duplicate
  // arcs. Keep a tail-cap to bound memory across long sessions.
  const seenEntryIdsRef = useRef<Set<string>>(new Set());
  // Track the previous lobe so each new entry traces FROM the lobe
  // that just fired TO the lobe of the new entry — gives the arcs a
  // narrative ("comms hands off to research").
  const previousLobeRef = useRef<string | null>(null);

  const [hovered, setHovered] = useState<string | null>(null);
  const [mousePos, setMousePos] = useState<{ x: number; y: number } | null>(null);
  const [selected, setSelected] = useState<BrainDotItem | null>(null);
  const [filters, setFilters] = useState<BrainFilters>(DEFAULT_FILTERS);
  const [panelOpen, setPanelOpen] = useState(false);
  const [ready, setReady] = useState(false);
  const [scenePointCount, setScenePointCount] = useState(0);
  const [pinnedCount, setPinnedCount] = useState(0);
  // Which lobe the cursor is currently over (drives the per-lobe
  // highlight and the hover-time stats card with its pie chart).
  const [hoveredLobe, setHoveredLobe] = useState<string | null>(null);

  // Refs so the rAF animate loop can read the latest hovered/selected
  // without re-binding the loop on every state change.
  const hoveredEntryRef = useRef<string | null>(null);
  const selectedEntryRef = useRef<string | null>(null);
  useEffect(() => { hoveredEntryRef.current = hovered; }, [hovered]);
  useEffect(() => { selectedEntryRef.current = selected?.id ?? null; }, [selected]);
  const qualityConfig = BRAIN_QUALITY_PRESETS[filters.quality] ?? BRAIN_QUALITY_PRESETS.high;
  const graphBudget = useMemo(
    () => getBrain3DRenderBudget(filters.quality, filters.lod),
    [filters.quality, filters.lod],
  );

  // Init scene once
  useEffect(() => {
    if (!webglAvailable || !wrapRef.current) return;
    setReady(false);
    const wrap = wrapRef.current;
    const w = wrap.clientWidth;
    const h = wrap.clientHeight;

    const scene = new THREE.Scene();
    scene.background = null;

    const camera = new THREE.PerspectiveCamera(38, w / h, 0.1, 100);
    // Three-quarter side view — the iconic angle for a brain.
    // Front lobe forward-right, temporal pouch visible below.
    const cameraDirection = new THREE.Vector3(3.4, 0.6, 2.4).normalize();
    camera.position.copy(cameraDirection).multiplyScalar(4.24);

    const renderer = new THREE.WebGLRenderer({
      antialias: qualityConfig.nodeSegments > 8,
      alpha: true,
      powerPreference: 'high-performance',
    });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, qualityConfig.pixelRatioCap));
    renderer.setSize(w, h, false);
    renderer.domElement.style.width = '100%';
    renderer.domElement.style.height = '100%';
    renderer.setClearColor(0x000000, 0);
    wrap.appendChild(renderer.domElement);
    renderer.domElement.style.outline = 'none';
    renderer.domElement.style.display = 'block';

    // Lighting — calibrated for PBR. Total intensity ~1.0 so vertex
    // colors aren't washed out. Stronger directional contrast picks
    // out the cortex ridges; weaker ambient keeps the lobe hues
    // recognizable.
    scene.add(new THREE.AmbientLight(0xffffff, 0.35));
    const key = new THREE.DirectionalLight(0xffffff, 0.65);
    key.position.set(2, 3, 4);
    scene.add(key);
    const fill = new THREE.DirectionalLight(0xffffff, 0.18);
    fill.position.set(-3, -1, 2);
    scene.add(fill);
    const rim = new THREE.DirectionalLight(0xffffff, 0.25);
    rim.position.set(0, 1, -3);
    scene.add(rim);

    // Brain. The GLB path is preferred; the procedural mesh remains as
    // a runtime fallback if the asset is absent, corrupt, or blocked.
    const brainGroup = new THREE.Group();
    scene.add(brainGroup);

    // Brain-shaped neural lace: owns the outer contour without
    // falling back to a generic spherical glow.
    const auraShell = buildNeuralAuraShell();
    brainGroup.add(auraShell);

    // Dots group — parented to the brain so the dots rotate, breathe,
    // and tilt with it. Previously they sat in scene root, which left
    // them floating in space while the brain spun around them.
    const dotsGroup = new THREE.Group();
    brainGroup.add(dotsGroup);

    const graphEdgesGroup = new THREE.Group();
    graphEdgesGroup.renderOrder = 1;
    brainGroup.add(graphEdgesGroup);

    // Synapse arcs group — same parent so arcs follow the brain's
    // rotation and breathing. Each arc is a TubeGeometry along a
    // quadratic Bezier whose control point is pushed outward from
    // origin to give the line a satisfying arc above the cortex.
    const synapsesGroup = new THREE.Group();
    synapsesGroup.renderOrder = 2; // drawn after the brain meshes
    brainGroup.add(synapsesGroup);
    const synapses: SynapseArc[] = [];

    function spawnSynapse(fromLobe: string, toLobe: string, color: THREE.Color) {
      // Resolve endpoints. Temporal lobe randomizes between left/right
      // hemispheres so repeated arcs to/from temporal don't hammer the
      // same spot.
      const pickEndpoint = (id: string): THREE.Vector3 => {
        if (id === 'temporal') {
          return (Math.random() < 0.5 ? LOBE_CENTROIDS.temporal : LOBE_CENTROIDS.temporal_l).clone();
        }
        return (LOBE_CENTROIDS[id] || LOBE_CENTROIDS.frontal).clone();
      };
      const from = pickEndpoint(fromLobe);
      const to = pickEndpoint(toLobe);
      // Don't draw a same-point arc.
      if (from.distanceTo(to) < 0.05) return;

      // Control point pushed outward from origin along the midpoint
      // direction so the arc bows over the cortex instead of cutting
      // through it. 1.4× radius is enough lift to read clearly.
      const mid = from.clone().add(to).multiplyScalar(0.5);
      const lift = mid.clone().normalize().multiplyScalar(1.18);
      mid.lerp(lift, 0.7);

      const curve = new THREE.QuadraticBezierCurve3(from, mid, to);
      const tubeGeo = new THREE.TubeGeometry(curve, 48, 0.008, 8, false);

      const mat = new THREE.ShaderMaterial({
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
        uniforms: {
          uColor: { value: color.clone() },
          uProgress: { value: 0 },
          uIntensity: { value: 1.4 },
        },
        vertexShader: /* glsl */ `
          varying float vU;
          void main() {
            // TubeGeometry's UV.x runs 0..1 along the spine.
            vU = uv.x;
            gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
          }
        `,
        fragmentShader: /* glsl */ `
          uniform vec3 uColor;
          uniform float uProgress;
          uniform float uIntensity;
          varying float vU;
          void main() {
            // Moving Gaussian bullet riding the curve.
            float head = exp(-pow((vU - uProgress) * 8.0, 2.0));
            // Trailing tail behind the head fades smoothly.
            float trail = clamp(1.0 - (uProgress - vU) * 2.5, 0.0, 1.0);
            trail *= step(vU, uProgress);
            float fade = smoothstep(0.0, 0.08, uProgress) * (1.0 - smoothstep(0.85, 1.0, uProgress));
            float a = (head * 1.6 + trail * 0.45) * fade;
            gl_FragColor = vec4(uColor * uIntensity, clamp(a, 0.0, 1.0));
          }
        `,
      });
      const tube = new THREE.Mesh(tubeGeo, mat);
      tube.frustumCulled = false;
      synapsesGroup.add(tube);

      synapses.push({
        mesh: tube,
        material: mat,
        createdAt: performance.now() / 1000,
        lifeSec: 1.2,
      });

      // Hard cap — never let runaway entry feeds spawn unbounded arcs.
      while (synapses.length > 24) {
        const oldest = synapses.shift()!;
        synapsesGroup.remove(oldest.mesh);
        oldest.mesh.geometry.dispose();
        oldest.material.dispose();
      }
    }

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.rotateSpeed = 0.65;
    controls.minDistance = 2.2;
    controls.maxDistance = 5.5;
    controls.enablePan = false;
    // Shift the lookAt point slightly down so the brain renders in
    // the upper half of the canvas. With target at origin (the default)
    // the brain landed visually low and users had to scroll the page
    // to see the bottom of the cortex.
    controls.target.set(0, -0.18, 0);

    function frameCamera(nw: number, nh: number) {
      const aspect = nw / Math.max(nh, 1);
      const distance = aspect < 0.55 ? 7.1 : (aspect < 0.85 ? 5.6 : 4.24);
      camera.position.copy(cameraDirection).multiplyScalar(distance);
      camera.fov = aspect < 0.55 ? 40 : 38;
      camera.aspect = aspect;
      camera.updateProjectionMatrix();
      controls.minDistance = aspect < 0.55 ? 4.2 : 2.2;
      controls.maxDistance = aspect < 0.55 ? 9.0 : 5.5;
      controls.update();
    }
    frameCamera(w, h);

    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    const dotMap = new Map<THREE.Object3D, DotData>();
    let graphForce: GraphForceState | null = null;
    let drag: GraphDragState | null = null;
    let suppressClickUntil = 0;
    let lastInteract = Date.now();
    const markInteract = () => { lastInteract = Date.now(); };
    controls.addEventListener('start', markInteract);
    controls.addEventListener('change', markInteract);

    // Post-processing: bloom pass picks up the emissive dots and the
    // bright ridge highlights and gives them a soft HDR-style glow.
    // Tuned conservatively so the brain doesn't look radioactive — the
    // glow should suggest activity, not blow out the colors.
    const composer = new EffectComposer(renderer);
    composer.addPass(new RenderPass(scene, camera));
    const bloom = new UnrealBloomPass(
      new THREE.Vector2(w, h),
      qualityConfig.bloomStrength, // strength — pushed up for demo wow. The cortex now reads
            // as luminous instead of merely lit; active lobes blow out
            // into a real HDR-feeling halo. Tier-2 slider can drive
            // this dynamically; baseline lives here.
      qualityConfig.bloomRadius, // radius — slightly wider so the halo wraps the silhouette
      qualityConfig.bloomThreshold, // threshold — lower so quieter lobes still contribute
    );
    composer.addPass(bloom);
    composer.addPass(new OutputPass());

    let disposed = false;
    let rafId = 0;
    const start = performance.now();
    // Idle blend tracks a smooth 0..1 weight so the cinematic drift
    // fades in/out instead of cutting on/off when the user grabs the
    // brain or lets it go.
    let idleWeight = 0;
    function animate() {
      rafId = requestAnimationFrame(animate);
      const t = (performance.now() - start) / 1000;

      // Cinematic idle drift. Two non-resonant rotation rates give the
      // brain a Lissajous-style path that never quite repeats. Subtle
      // X tilt + Y spin reads as if the camera is gently orbiting.
      // Fades in over ~1s of inactivity, fades out the moment the user
      // touches the OrbitControls. Replaces the previous straight
      // rotation.y += 0.0035 which felt mechanical for a hero shot.
      const idleTarget = (Date.now() - lastInteract > 1200) ? 1 : 0;
      idleWeight += (idleTarget - idleWeight) * 0.05;
      const driftY = Math.cos(t * 0.18) * 0.0042;
      const driftX = Math.sin(t * 0.11) * 0.0009;
      brainGroup.rotation.y += driftY * idleWeight;
      brainGroup.rotation.x += driftX * idleWeight;

      // Breathing pulse — bumped from 2.4% to 5% amplitude. At the old
      // setting the pulse was so subtle most viewers missed it. 5%
      // reads as alive without becoming distracting.
      const breathe = 1 + Math.sin(t * 0.7) * 0.025;
      brainGroup.scale.setScalar(breathe);

      // Neural firing — every dot has its own deterministic pulse
      // schedule based on its entry id so a few flash brightly at any
      // given time, like neurons firing across the cortex.
      // Synapse arcs — advance each pulse, dispose expired. The
      // animation reads as if information is flowing between lobes
      // when an agent kicks off work.
      const nowSec = performance.now() / 1000;
      for (let i = synapses.length - 1; i >= 0; i--) {
        const s = synapses[i];
        const age = nowSec - s.createdAt;
        const progress = Math.min(1, age / s.lifeSec);
        s.material.uniforms.uProgress.value = progress;
        if (progress >= 1) {
          synapsesGroup.remove(s.mesh);
          s.mesh.geometry.dispose();
          s.material.dispose();
          synapses.splice(i, 1);
        }
      }

      const activeForce = sceneStateRef.current?.graphForce ?? graphForce;
      const activeDrag = sceneStateRef.current?.drag ?? drag;
      if (activeForce) {
        const sim = activeForce.simulation;
        if (activeDrag || sim.alpha() > sim.alphaMin()) {
          sim.tick(activeDrag ? 2 : 1);
        }
        for (const node of activeForce.nodes) {
          const pos = forceNodePosition(node);
          clampToBrainVolume(pos);
          node.x = pos.x;
          node.y = pos.y;
          node.z = pos.z;
          node.mesh.position.copy(pos);
          node.halo.position.copy(pos);
          node.dotData.pos.copy(pos);
        }
        const draggedId = activeDrag?.node.id ?? null;
        const selectedNodeId = graphNodeIdFromItemId(selectedEntryRef.current);
        const hoveredNodeId = graphNodeIdFromItemId(hoveredEntryRef.current);
        const focusNodeId = draggedId ?? selectedNodeId ?? hoveredNodeId;
        const pulseStates: Array<{ link: BrainForceLink; progress: number; opacity: number; scale: number }> = [];
        if (activeForce.links.length > 0) {
          if (focusNodeId) {
            const focusedLinks = activeForce.links.filter((link) => {
              const source = forceLinkEndpoint(link.source, activeForce);
              const target = forceLinkEndpoint(link.target, activeForce);
              return Boolean(source && target && (source.id === focusNodeId || target.id === focusNodeId));
            });
            const pulseCount = Math.min(activeDrag ? GRAPH_AMBIENT_PULSE_COUNT : 2, focusedLinks.length);
            for (let pulseSlot = 0; pulseSlot < pulseCount; pulseSlot++) {
              const networkTravel = (t / (GRAPH_FOCUS_PULSE_EDGE_SECONDS * focusedLinks.length) + pulseSlot / pulseCount) % 1;
              const scaledTravel = networkTravel * focusedLinks.length;
              const linkIndex = Math.floor(scaledTravel) % focusedLinks.length;
              pulseStates.push({
                link: focusedLinks[linkIndex],
                progress: scaledTravel - Math.floor(scaledTravel),
                opacity: activeDrag ? 0.42 : 0.34,
                scale: activeDrag ? 1.06 : 1,
              });
            }
          } else {
            const pulseCount = Math.min(GRAPH_AMBIENT_PULSE_COUNT, activeForce.links.length);
            for (let pulseSlot = 0; pulseSlot < pulseCount; pulseSlot++) {
              const networkTravel = (t / (GRAPH_AMBIENT_PULSE_EDGE_SECONDS * activeForce.links.length) + pulseSlot / pulseCount) % 1;
              const scaledTravel = networkTravel * activeForce.links.length;
              const linkIndex = Math.floor(scaledTravel) % activeForce.links.length;
              const link = activeForce.links[linkIndex];
              pulseStates.push({
                link,
                progress: scaledTravel - Math.floor(scaledTravel),
                opacity: link.edge.kind === 'source' ? 0.24 : 0.18,
                scale: 0.94,
              });
            }
          }
        }
        const pulseByLink = new Map(pulseStates.map((pulse) => [pulse.link, pulse]));

        for (const link of activeForce.links) {
          const source = forceLinkEndpoint(link.source, activeForce);
          const target = forceLinkEndpoint(link.target, activeForce);
          if (!source || !target) continue;
          const sourcePos = forceNodePosition(source);
          const targetPos = forceNodePosition(target);
          setGraphEdgeMeshPoints(link.line, sourcePos, targetPos);
          const mat = link.line.material as THREE.LineBasicMaterial;
          const pulse = pulseByLink.get(link);
          const pulseGlow = pulse ? Math.sin(pulse.progress * Math.PI) : 0;
          if (focusNodeId) {
            const touchesFocus = source.id === focusNodeId || target.id === focusNodeId;
            mat.opacity = touchesFocus ? 0.86 : 0.04;
          } else {
            mat.opacity = pulse ? Math.min(link.baseOpacity + pulseGlow * 0.10, 0.48) : link.baseOpacity;
          }
        }

        for (let i = 0; i < activeForce.pulses.length; i++) {
          const pulseMesh = activeForce.pulses[i];
          const pulse = pulseStates[i];
          if (pulse) {
            const source = forceLinkEndpoint(pulse.link.source, activeForce);
            const target = forceLinkEndpoint(pulse.link.target, activeForce);
            const pulseGlow = Math.sin(pulse.progress * Math.PI);
            if (!source || !target || pulseGlow <= 0.04) {
              pulseMesh.mesh.visible = false;
              pulseMesh.material.opacity = 0;
              continue;
            }
            const sourcePos = forceNodePosition(source);
            const targetPos = forceNodePosition(target);
            const edgeT = 0.08 + pulse.progress * 0.84;
            pulseMesh.mesh.position.copy(graphEdgePoint(sourcePos, targetPos, edgeT));
            pulseMesh.mesh.scale.setScalar(pulse.scale);
            pulseMesh.mesh.visible = true;
            pulseMesh.material.color.copy(graphPulseColor(pulse.link.edge));
            pulseMesh.material.opacity = pulse.opacity * pulseGlow;
          } else {
            pulseMesh.mesh.visible = false;
            pulseMesh.material.opacity = 0;
          }
        }
      }

      const draggedId = activeDrag?.node.id ?? null;
      const selectedNodeId = graphNodeIdFromItemId(selectedEntryRef.current);
      const hoveredNodeId = graphNodeIdFromItemId(hoveredEntryRef.current);
      const focusNodeId = draggedId ?? selectedNodeId ?? hoveredNodeId;
      const hoveredDimming = hoveredEntryRef.current !== null || selectedEntryRef.current !== null || draggedId !== null;
      forEachDotData(dotMap, (d) => {
        const isFocused = d.item.id === hoveredEntryRef.current
                       || selectedEntryRef.current === d.item.id;
        const isDragRelated = draggedId && activeForce
          ? relatedToDraggedNode(d.item, activeForce, draggedId)
          : false;
        const isFocusRelated = focusNodeId && activeForce
          ? graphNodeRelated(d.item, activeForce, focusNodeId)
          : false;
        const dotMat = d.mesh.material as THREE.MeshBasicMaterial;

        if (isFocused) {
          d.mesh.scale.setScalar(1.32);
          d.halo.scale.setScalar(1.55);
          dotMat.opacity = 1;
          return;
        }

        if (isDragRelated || isFocusRelated) {
          d.mesh.scale.setScalar(isDragRelated ? 1.28 : 1.16);
          d.halo.scale.setScalar(isDragRelated ? 1.52 : 1.30);
          dotMat.opacity = isDragRelated ? 0.98 : 0.92;
          return;
        }

        // Slow per-dot pulse keeps the layout feeling alive without
        // making any one dot scream for attention. When the user is
        // hovering something, dim the others so the focused one stands
        // out clearly.
        const seed = (stringHash(d.item.id) % 100) / 100;
        const phase = (t * 0.45 + seed * 8) % 5;
        let scale = 1;
        let opacityBoost = 0;
        if (phase < 0.55) {
          const pulse = Math.sin((phase / 0.55) * Math.PI);
          scale = 1 + pulse * 0.07;
          opacityBoost = pulse * 0.04;
        }
        d.mesh.scale.setScalar(scale);
        d.halo.scale.setScalar(scale);
        const baseOpacity = hoveredDimming ? 0.34 : 0.88;
        dotMat.opacity = Math.min(1, baseOpacity + opacityBoost);
      });

      controls.update();
      composer.render();
    }
    animate();

    function resize() {
      const nw = wrap.clientWidth;
      const nh = wrap.clientHeight;
      if (nw === 0 || nh === 0) return;
      renderer.setSize(nw, nh, false);
      composer.setSize(nw, nh);
      bloom.setSize(nw, nh);
      frameCamera(nw, nh);
    }
    const ro = new ResizeObserver(resize);
    ro.observe(wrap);
    requestAnimationFrame(() => requestAnimationFrame(resize));

    sceneStateRef.current = {
      scene, camera, renderer, composer, controls,
      leftSurface: [], rightSurface: [], brainGeos: [],
      dotsGroup, graphEdgesGroup, synapsesGroup, auraShell, synapses, spawnSynapse, bloom,
      raycaster, pointer, dotMap,
      graphForce, drag, suppressClickUntil,
      rafId, lastInteract, brainGroup, markInteract,
      cleanup: () => {
        disposed = true;
        cancelAnimationFrame(rafId);
        ro.disconnect();
        sceneStateRef.current?.graphForce?.simulation.stop();
        controls.dispose();
        composer.dispose();
        scene.traverse((obj) => {
          if ((obj as any).geometry) (obj as any).geometry.dispose();
          if ((obj as any).material) {
            const m = (obj as any).material;
            if (Array.isArray(m)) m.forEach((mm) => mm.dispose());
            else m.dispose();
          }
        });
        renderer.dispose();
        if (renderer.domElement.parentNode) renderer.domElement.parentNode.removeChild(renderer.domElement);
      },
    };

    const activateBrain = (pools: LobePools, brainGeos: BrainGeoSnapshot[] = []) => {
      if (disposed || !sceneStateRef.current) return;
      sceneStateRef.current.leftSurface = pools.left;
      sceneStateRef.current.rightSurface = pools.right;
      sceneStateRef.current.brainGeos = brainGeos;
      setReady(true);
    };

    const fallbackToProcedural = (err: unknown) => {
      if (import.meta.env.DEV) console.warn('Falling back to procedural brain mesh; /brain.glb failed to load.', err);
      if (disposed) return;
      // Remove only the loaded GLB (if any) — keep dotsGroup, the
      // backside halo, and synapsesGroup parented. Earlier code naively
      // removed every child except dotsGroup, which silently detached
      // the halo shell and synapse arcs whenever brain.glb failed.
      // Codex T3-1 finding: the new visual effects vanished on the
      // procedural fallback path. Whitelist the things we built at init
      // and drop the rest (the loaded gltf scene).
      const keep = new Set<THREE.Object3D>([dotsGroup, graphEdgesGroup, synapsesGroup, auraShell]);
      const toRemove: THREE.Object3D[] = [];
      brainGroup.children.forEach((c) => { if (!keep.has(c)) toRemove.push(c); });
      toRemove.forEach((c) => brainGroup.remove(c));
      activateBrain(buildProceduralBrain(brainGroup));
    };

    const loader = new GLTFLoader();
    // Brain GLB ships with meshopt geometry compression (~8x smaller).
    // Without this decoder the load fails and we fall back to procedural.
    loader.setMeshoptDecoder(MeshoptDecoder as any);
    loader.load(
      '/brain.glb',
      (gltf) => {
        if (disposed) return;
        try {
          const { pools, brainGeos } = prepareLoadedBrainModel(gltf.scene);
          if (pools.left.length + pools.right.length === 0) {
            throw new Error('Loaded brain GLB did not expose usable surface vertices.');
          }
          // Keep dotsGroup parented; just add the loaded gltf scene
          // alongside it.
          brainGroup.add(gltf.scene);
          activateBrain(pools, brainGeos);
        } catch (err) {
          fallbackToProcedural(err);
        }
      },
      undefined,
      fallbackToProcedural,
    );

    return () => { sceneStateRef.current?.cleanup(); sceneStateRef.current = null; };
  }, [webglAvailable]);

  // Sync graph topology and activity dots whenever the shared brain contract changes.
  useEffect(() => {
    const state = sceneStateRef.current;
    if (!state || !ready) return;
    const sceneState = state;

    // Detect newly arrived entries since last sync. Each one fires a
    // synapse arc from the previously-active lobe to the new entry's
    // lobe — visually narrating "this agent just handed off to that
    // one". Skips the very first sync (initial bulk load) so a fresh
    // page open doesn't fire 100 arcs simultaneously.
    const seen = seenEntryIdsRef.current;
    const isInitialLoad = seen.size === 0;
    const newEntries: typeof entries = [];
    for (const e of entries) {
      const key = `activity:${e.id}`;
      if (!seen.has(key)) {
        seen.add(key);
        if (!isInitialLoad) newEntries.push(e);
      }
    }
    if (showActivity && !isInitialLoad && newEntries.length > 0) {
      // Fire arcs for up to a small batch — bursts shouldn't drown
      // the visualization with overlapping pulses.
      for (const e of newEntries.slice(0, 6)) {
        const toLobe = lobeFor(e.agent_id);
        const fromLobe = previousLobeRef.current && previousLobeRef.current !== toLobe
          ? previousLobeRef.current
          : pickRandomOtherLobe(toLobe);
        const colorHex = resolveColor(agentColors[e.agent_id], '#a074ff');
        state.spawnSynapse(fromLobe, toLobe, new THREE.Color(colorHex));
        previousLobeRef.current = toLobe;
      }
    }
    // Memory cap: keep the seen set bounded so a long session doesn't
    // grow it unbounded. We re-add every render, so trimming is safe.
    if (seen.size > 4000) {
      const arr = Array.from(seen);
      seenEntryIdsRef.current = new Set(arr.slice(arr.length - 2000));
    }

    // Clear old dots, graph edges, and the previous graph simulation.
    state.graphForce?.simulation.stop();
    state.graphForce = null;
    state.drag = null;
    state.controls.enabled = true;
    setPinnedCount(0);
    while (state.dotsGroup.children.length > 0) {
      const child = state.dotsGroup.children[0];
      state.dotsGroup.remove(child);
      if ((child as any).geometry) (child as any).geometry.dispose();
      if ((child as any).material) (child as any).material.dispose();
    }
    state.dotMap.clear();
    while (state.graphEdgesGroup.children.length > 0) {
      const child = state.graphEdgesGroup.children[0];
      state.graphEdgesGroup.remove(child);
      if ((child as any).geometry) (child as any).geometry.dispose();
      if ((child as any).material) (child as any).material.dispose();
    }

    // Track slot index per (lobe, side) so dots spread out evenly.
    const slotIdx: Record<string, number> = {};
    const nodePositions = new Map<string, THREE.Vector3>();
    let placed = 0;

    function placeItem(item: BrainDotItem, radius: number, colorHex: string): DotData | null {
      const lobe = item.lobe;
      const lobeSlot = (slotIdx[lobe] ?? -1) + 1;
      slotIdx[lobe] = lobeSlot;
      const pos = volumePointForItem(item, lobeSlot);
      placed++;

      const outward = pos.clone();
      const color = new THREE.Color(colorHex);
      const displayColor = color.clone().multiplyScalar(item.itemKind === 'node' ? 1.18 : 1.08);

      const dotSegments = item.itemKind === 'node' ? qualityConfig.nodeSegments : qualityConfig.activitySegments;
      const dotGeo = new THREE.SphereGeometry(radius, dotSegments, dotSegments);
      const dotMat = new THREE.MeshBasicMaterial({
        color: displayColor,
        transparent: true,
        opacity: item.itemKind === 'node' ? 0.92 : 0.86,
        depthWrite: false,
        toneMapped: false,
      });
      const dot = new THREE.Mesh(dotGeo, dotMat);
      dot.position.copy(outward);
      sceneState.dotsGroup.add(dot);

      const haloGeo = new THREE.SphereGeometry(radius * (item.itemKind === 'node' ? 2.2 : 2.6), qualityConfig.haloSegments, qualityConfig.haloSegments);
      const haloMat = new THREE.MeshBasicMaterial({
        color,
        transparent: true,
        opacity: 0,
        depthWrite: false,
      });
      // Invisible pick volume only. The rendered scene should read as clean dots,
      // not as a stack of translucent target rings.
      haloMat.colorWrite = false;
      const halo = new THREE.Mesh(haloGeo, haloMat);
      halo.position.copy(outward);
      sceneState.dotsGroup.add(halo);
      const dotData: DotData = { item, pos: outward, mesh: dot, halo };
      sceneState.dotMap.set(dot, dotData);
      sceneState.dotMap.set(halo, dotData);
      return dotData;
    }

    const degreeByNodeId = new Map<string, number>();
    for (const edge of data?.edges ?? []) {
      degreeByNodeId.set(edge.source, (degreeByNodeId.get(edge.source) ?? 0) + 1);
      degreeByNodeId.set(edge.target, (degreeByNodeId.get(edge.target) ?? 0) + 1);
    }

    const graphNodes = selectGraphNodes(data?.nodes ?? [], data?.edges ?? [], {
      nodeCap: graphBudget.nodes,
      lod: filters.lod,
    })
      .map((node) => makeNodeItem(node, degreeByNodeId.get(node.id) ?? 0))
      .filter((item) => itemVisible(item, filters, agentFilter, showActivity));

    const forceNodes: BrainForceNode[] = [];
    const nodeById = new Map<string, BrainForceNode>();
    for (const item of graphNodes) {
      if (item.itemKind !== 'node') continue;
      const node = item.node;
      const radius = graphNodeRadius(node, item.degree);
      const dotData = placeItem(item, radius, graphNodeColor(node, item.degree));
      if (!dotData) continue;
      const anchor = dotData.pos.clone();
      const forceNode: BrainForceNode = {
        id: node.id,
        item,
        anchor,
        mesh: dotData.mesh,
        halo: dotData.halo,
        dotData,
        radius,
        pinned: false,
        x: anchor.x,
        y: anchor.y,
        z: anchor.z,
        vx: 0,
        vy: 0,
        vz: 0,
      };
      dotData.forceNode = forceNode;
      forceNodes.push(forceNode);
      nodeById.set(node.id, forceNode);
      nodePositions.set(node.id, anchor);
    }

    const forceLinks: BrainForceLink[] = [];
    const graphEdges = selectGraphEdges(data?.edges ?? [], new Set(nodePositions.keys()), degreeByNodeId, graphBudget.edges);
    for (const edge of graphEdges) {
      const source = nodePositions.get(edge.source);
      const target = nodePositions.get(edge.target);
      if (!source || !target) continue;
      const line = makeGraphEdgeMesh(source, target, edge, qualityConfig.edgeSegments);
      const baseOpacity = edge.kind === 'source' ? 0.26 : 0.34;
      state.graphEdgesGroup.add(line);
      forceLinks.push({ source: edge.source, target: edge.target, edge, line, baseOpacity });
    }
    const graphPulses = Array.from({ length: GRAPH_AMBIENT_PULSE_COUNT }, () => makeGraphPulseMesh());
    for (const pulse of graphPulses) {
      state.graphEdgesGroup.add(pulse.mesh);
    }

    if (forceNodes.length > 0) {
      const graphForce: GraphForceState = {
        simulation: forceSimulation<BrainForceNode>(forceNodes, 3)
          .alpha(0.65)
          .alphaMin(0.002)
          .alphaDecay(0.055)
          .velocityDecay(0.46)
          .force('link', forceLink<BrainForceNode, BrainForceLink>(forceLinks)
            .id((node) => node.id)
            .distance((link) => link.edge.kind === 'wikilink' ? 0.20 : 0.14)
            .strength((link) => link.edge.kind === 'wikilink' ? 0.22 : 0.16))
          .force('charge', forceManyBody<BrainForceNode>()
            .strength((node) => node.item.node.kind === 'note' ? -0.0042 : -0.0028)
            .distanceMin(0.035)
            .distanceMax(0.82))
          .force('collide', forceCollide<BrainForceNode>((node) => node.radius * 2.7).strength(0.72).iterations(2))
          .force('x', forceX<BrainForceNode>((node) => node.anchor.x).strength(0.045))
          .force('y', forceY<BrainForceNode>((node) => node.anchor.y).strength(0.075))
          .force('z', forceZ<BrainForceNode>((node) => node.anchor.z).strength(0.045))
          .stop(),
        nodes: forceNodes,
        links: forceLinks,
        pulses: graphPulses,
        nodeById,
        adjacency: buildGraphAdjacency(forceLinks),
      };
      graphForce.simulation.tick(qualityConfig.simulationTicks);
      for (const node of graphForce.nodes) {
        const pos = forceNodePosition(node);
        clampToBrainVolume(pos);
        node.x = pos.x;
        node.y = pos.y;
        node.z = pos.z;
        node.mesh.position.copy(pos);
        node.halo.position.copy(pos);
        node.dotData.pos.copy(pos);
      }
      for (const link of graphForce.links) {
        const sourceNode = forceLinkEndpoint(link.source, graphForce);
        const targetNode = forceLinkEndpoint(link.target, graphForce);
        if (sourceNode && targetNode) {
          setGraphEdgeMeshPoints(link.line, forceNodePosition(sourceNode), forceNodePosition(targetNode));
        }
      }
      state.graphForce = graphForce;
    }

    // Place activity in chronological order so the overlay remains scannable:
    // newest activity starts high in each lobe and older activity fills down.
    if (showActivity) {
      const placementOrder = [...entries]
        .sort((a, b) => (b.created_at - a.created_at) || (b.id - a.id))
        .slice(0, graphBudget.activity);

      for (const entry of placementOrder) {
        const item = makeActivityItem(entry);
        if (!itemVisible(item, filters, agentFilter, showActivity)) continue;
        const colorHex = resolveColor(agentColors[entry.agent_id], '#888');
        placeItem(item, 0.018, colorHex);
      }
    }
    if (placed === 0 && ((data?.nodes?.length ?? 0) > 0 || entries.length > 0) && import.meta.env.DEV) {
      console.warn('[brain3d] no graph or activity dots placed despite available data — surface pools may be empty');
    }
    setScenePointCount(placed);
  }, [data?.nodes, data?.edges, entries, agentColors, filters, agentFilter, showActivity, ready]);

  function applySilhouetteMode(mode: BrainSilhouetteMode) {
    const state = sceneStateRef.current;
    if (!state) return;
    const cfg = BRAIN_SILHOUETTES[mode] ?? BRAIN_SILHOUETTES.neural;
    state.auraShell.visible = cfg.aura > 0;
    state.auraShell.traverse((obj) => {
      const mat = (obj as any).material as THREE.Material | undefined;
      if (!mat || !('opacity' in mat)) return;
      const material = mat as THREE.Material & { opacity: number };
      const base = typeof material.userData.baseOpacity === 'number'
        ? material.userData.baseOpacity
        : material.opacity;
      material.opacity = base * cfg.aura;
      material.needsUpdate = true;
    });

    for (const geo of state.brainGeos) {
      const materials = Array.isArray(geo.mesh.material) ? geo.mesh.material : [geo.mesh.material];
      for (const material of materials) {
        const std = material as THREE.MeshStandardMaterial;
        std.transparent = true;
        std.opacity = cfg.opacity;
        if (std.emissive !== undefined) {
          std.emissiveIntensity = cfg.emissive;
        }
        std.needsUpdate = true;
      }
    }
  }

  // Activity glow — recompute brain vertex colors so each lobe brightens
  // in proportion to how much agent activity has landed there. Bloom
  // catches the hot regions and the cortex grooves naturally appear lit.
  useEffect(() => {
    const state = sceneStateRef.current;
    if (!state || !ready || state.brainGeos.length === 0) return;

    // Activity per lobe: sum entries whose agent maps there, but
    // respect the agent / lobe / search filters so the brain
    // actually responds to filter toggles.
    const activity: Record<string, number> = {
      frontal: 0, parietal: 0, temporal: 0, occipital: 0,
    };
    if (showActivity) {
      for (const e of entries) {
        if (filters.hiddenAgents.has(e.agent_id)) continue;
        if (agentFilter !== 'all' && e.agent_id !== agentFilter) continue;
        if (filters.query) {
          const q = filters.query.toLowerCase();
          if (!e.summary.toLowerCase().includes(q) && !e.action.toLowerCase().includes(q)) continue;
        }
        const lobe = lobeFor(e.agent_id);
        if (filters.hiddenLobes.has(lobe)) continue;
        activity[lobe] = (activity[lobe] || 0) + 1;
      }
    }
    applyActivityGlow(state.brainGeos, activity, hoveredLobe, filters.nodeSize);
  }, [entries, ready, filters.hiddenAgents, filters.hiddenLobes, filters.query, agentFilter, hoveredLobe, filters.nodeSize, showActivity]);

  useEffect(() => {
    if (!ready) return;
    applySilhouetteMode(filters.silhouette);
  }, [ready, filters.silhouette]);

  // The "Glow intensity" slider also drives the bloom pass strength so
  // moving it has a visible HDR effect on the silhouette, not only the
  // per-vertex brightness boost. Quality presets also bound pixel ratio
  // so 3D stays an exploration layer instead of trying to render the full
  // audit graph.
  useEffect(() => {
    const state = sceneStateRef.current;
    if (!state || !ready) return;
    const slider = filters.nodeSize;
    const dpr = Math.min(window.devicePixelRatio || 1, qualityConfig.pixelRatioCap);
    const w = wrapRef.current?.clientWidth ?? 0;
    const h = wrapRef.current?.clientHeight ?? 0;
    state.renderer.setPixelRatio(dpr);
    state.composer.setPixelRatio?.(dpr);
    if (w > 0 && h > 0) {
      state.renderer.setSize(w, h, false);
      state.composer.setSize(w, h);
      state.bloom.setSize(w, h);
    }
    state.bloom.strength = qualityConfig.bloomStrength * (0.72 + slider * 0.28);
    state.bloom.radius = qualityConfig.bloomRadius;
    state.bloom.threshold = qualityConfig.bloomThreshold;
  }, [filters.nodeSize, filters.quality, ready]);

  // Apply visibility (agent / lobe / search filter) without rebuilding meshes.
  useEffect(() => {
    const state = sceneStateRef.current;
    if (!state) return;
    forEachDotData(state.dotMap, (d) => {
      const visible = itemVisible(d.item, filters, agentFilter, showActivity);
      // Hidden dots stop participating in raycasting too — Three.js
      // skips invisible objects in intersectObjects by default.
      d.mesh.visible = visible;
      d.halo.visible = visible;
    });
  }, [filters.hiddenAgents, filters.hiddenLobes, filters.query, agentFilter, showActivity]);

  function updatePointerRay(state: NonNullable<typeof sceneStateRef.current>, clientX: number, clientY: number) {
    if (!wrapRef.current) return null;
    const rect = wrapRef.current.getBoundingClientRect();
    const cx = clientX - rect.left;
    const cy = clientY - rect.top;
    state.pointer.x = (cx / rect.width) * 2 - 1;
    state.pointer.y = -(cy / rect.height) * 2 + 1;
    state.raycaster.setFromCamera(state.pointer, state.camera);
    return { rect, cx, cy };
  }

  function pickDotAt(clientX: number, clientY: number): DotData | null {
    const state = sceneStateRef.current;
    const pointer = state ? updatePointerRay(state, clientX, clientY) : null;
    if (!state || !pointer) return null;
    const dotMeshes = Array.from(state.dotMap.keys());
    const dotHits = state.raycaster.intersectObjects(dotMeshes, false);
    return dotHits.length > 0 ? (state.dotMap.get(dotHits[0].object) ?? null) : null;
  }

  function dragLocalPoint(state: NonNullable<typeof sceneStateRef.current>, clientX: number, clientY: number): THREE.Vector3 | null {
    if (!state.drag) return null;
    updatePointerRay(state, clientX, clientY);
    const worldPoint = new THREE.Vector3();
    if (!state.raycaster.ray.intersectPlane(state.drag.plane, worldPoint)) return null;
    return state.brainGroup.worldToLocal(worldPoint.clone()).sub(state.drag.offset);
  }

  function handlePointerDown(e: PointerEvent | MouseEvent) {
    const state = sceneStateRef.current;
    if (!state || !wrapRef.current) return;
    if (state.drag) return;
    const dot = pickDotAt(e.clientX, e.clientY);
    if (!dot?.forceNode) return;
    const pointerId = 'pointerId' in e ? e.pointerId : -1;

    state.markInteract();
    state.controls.enabled = false;
    const worldPos = new THREE.Vector3();
    dot.mesh.getWorldPosition(worldPos);
    const planeNormal = new THREE.Vector3();
    state.camera.getWorldDirection(planeNormal);
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(planeNormal, worldPos);
    const localHit = state.brainGroup.worldToLocal(worldPos.clone());
    const nodePos = forceNodePosition(dot.forceNode);
    state.drag = {
      node: dot.forceNode,
      pointerId,
      plane,
      offset: localHit.sub(nodePos),
      startX: e.clientX,
      startY: e.clientY,
      moved: false,
    };
    dot.forceNode.fx = dot.forceNode.x ?? dot.forceNode.anchor.x;
    dot.forceNode.fy = dot.forceNode.y ?? dot.forceNode.anchor.y;
    dot.forceNode.fz = dot.forceNode.z ?? dot.forceNode.anchor.z;
    state.graphForce?.simulation.alphaTarget(0.28).restart();
    setHovered(dot.item.id);
    setHoveredLobe(dot.item.lobe);
    if (pointerId >= 0) {
      (e.currentTarget as HTMLElement).setPointerCapture?.(pointerId);
    }
    (e.currentTarget as HTMLElement).style.cursor = 'grabbing';
    e.preventDefault();
    e.stopPropagation();
  }

  // Pointer move → drag a graph node when grabbed; otherwise raycast
  // against dots first (specific entry), then against the brain mesh
  // (which lobe is under the cursor).
  function handlePointerMove(e: PointerEvent | MouseEvent) {
    const state = sceneStateRef.current;
    if (!state || !wrapRef.current) return;
    const pointer = updatePointerRay(state, e.clientX, e.clientY);
    if (!pointer) return;
    setMousePos({ x: pointer.cx, y: pointer.cy });

    if (state.drag) {
      const distance = Math.hypot(e.clientX - state.drag.startX, e.clientY - state.drag.startY);
      if (distance > 4) state.drag.moved = true;
      const next = dragLocalPoint(state, e.clientX, e.clientY);
      if (next) {
        clampToBrainVolume(next);
        state.drag.node.fx = next.x;
        state.drag.node.fy = next.y;
        state.drag.node.fz = next.z;
        state.drag.node.x = next.x;
        state.drag.node.y = next.y;
        state.drag.node.z = next.z;
        state.drag.node.pinned = true;
        state.graphForce?.simulation.alpha(0.32).alphaTarget(0.16).restart();
        state.markInteract();
      }
      setHovered(state.drag.node.item.id);
      setHoveredLobe(state.drag.node.item.lobe);
      e.preventDefault();
      e.stopPropagation();
      return;
    }

    // 1) Specific entry hit (invisible dot meshes)
    const dotMeshes = Array.from(state.dotMap.keys());
    const dotHits = state.raycaster.intersectObjects(dotMeshes, false);
    if (dotHits.length > 0) {
      const data = state.dotMap.get(dotHits[0].object);
      if (data) {
        setHovered(data.item.id);
        // Also lift the lobe glow so the hovered entry's region brightens.
        setHoveredLobe(data.item.lobe);
        return;
      }
    }
    setHovered(null);

    // 2) Lobe hit (the brain mesh)
    if (state.brainGeos.length > 0) {
      const meshes = state.brainGeos.map((g) => g.mesh);
      const meshHits = state.raycaster.intersectObjects(meshes, false);
      if (meshHits.length > 0 && meshHits[0].face) {
        const hit = meshHits[0];
        const geo = state.brainGeos.find((g) => g.mesh === hit.object);
        if (geo) {
          const lobeId = geo.vertexLobeIds[hit.face!.a];
          if (lobeId) {
            setHoveredLobe(lobeId);
            return;
          }
        }
      }
    }
    setHoveredLobe(null);
  }

  function handlePointerUp(e: PointerEvent | MouseEvent) {
    const state = sceneStateRef.current;
    if (!state?.drag) return;
    const dragState = state.drag;
    if (dragState.moved) {
      dragState.node.pinned = true;
      dragState.node.fx = dragState.node.x ?? dragState.node.anchor.x;
      dragState.node.fy = dragState.node.y ?? dragState.node.anchor.y;
      dragState.node.fz = dragState.node.z ?? dragState.node.anchor.z;
      state.suppressClickUntil = Date.now() + 250;
    } else {
      dragState.node.pinned = false;
      dragState.node.fx = null;
      dragState.node.fy = null;
      dragState.node.fz = null;
    }
    state.drag = null;
    state.controls.enabled = true;
    state.graphForce?.simulation.alphaTarget(0.02).restart();
    state.markInteract();
    setPinnedCount(state.graphForce?.nodes.filter((node) => node.pinned).length ?? 0);
    const pointerId = 'pointerId' in e ? e.pointerId : -1;
    if (pointerId >= 0) {
      (e.currentTarget as HTMLElement).releasePointerCapture?.(pointerId);
    }
    (e.currentTarget as HTMLElement).style.cursor = 'grab';
    if (dragState.moved) {
      e.preventDefault();
      e.stopPropagation();
    }
  }

  function handlePointerCancel(e: PointerEvent) {
    const state = sceneStateRef.current;
    if (!state?.drag) return;
    state.drag.node.pinned = false;
    state.drag.node.fx = null;
    state.drag.node.fy = null;
    state.drag.node.fz = null;
    state.drag = null;
    state.controls.enabled = true;
    state.graphForce?.simulation.alphaTarget(0.02).restart();
    setPinnedCount(state.graphForce?.nodes.filter((node) => node.pinned).length ?? 0);
    (e.currentTarget as HTMLElement).style.cursor = 'grab';
  }

  function handleClick(e: MouseEvent) {
    const state = sceneStateRef.current;
    if (!state || !wrapRef.current) return;
    if (Date.now() < state.suppressClickUntil) return;
    updatePointerRay(state, e.clientX, e.clientY);
    const dotMeshes = Array.from(state.dotMap.keys());
    const hits = state.raycaster.intersectObjects(dotMeshes, false);
    if (hits.length > 0) {
      const data = state.dotMap.get(hits[0].object);
      if (data) {
        setSelected(data.item);
        setPanelOpen(true);
        focusCameraOnDot(state, data);
      }
      return;
    }
    const nearest = nearestVisibleDot(state, e.clientX, e.clientY);
    if (nearest) {
      setSelected(nearest.item);
      setPanelOpen(true);
      focusCameraOnDot(state, nearest);
    }
  }

  function nearestVisibleDot(state: NonNullable<typeof sceneStateRef.current>, clientX: number, clientY: number): DotData | null {
    const rect = state.renderer.domElement.getBoundingClientRect();
    const projected = new THREE.Vector3();
    let best: { dot: DotData; distance: number } | null = null;
    for (const dot of state.dotMap.values()) {
      if (!dot.mesh.visible) continue;
      dot.mesh.getWorldPosition(projected);
      projected.project(state.camera);
      if (projected.z < -1 || projected.z > 1) continue;
      const x = rect.left + ((projected.x + 1) / 2) * rect.width;
      const y = rect.top + ((1 - projected.y) / 2) * rect.height;
      const distance = Math.hypot(clientX - x, clientY - y);
      if (!best || distance < best.distance) {
        best = { dot, distance };
      }
    }
    return best && best.distance <= 34 ? best.dot : null;
  }

  function focusCameraOnDot(state: NonNullable<typeof sceneStateRef.current>, dot: DotData) {
    const target = new THREE.Vector3();
    dot.mesh.getWorldPosition(target);
    const viewDirection = state.camera.position.clone().sub(state.controls.target);
    if (viewDirection.lengthSq() < 0.001) viewDirection.set(0, 0.4, 1);
    viewDirection.normalize();
    const distance = dot.item.itemKind === 'node' ? 3.45 : 3.8;
    state.controls.target.copy(target);
    state.camera.position.copy(target.clone().add(viewDirection.multiplyScalar(distance)));
    state.controls.update();
    state.markInteract();
  }

  function resetGraphPins() {
    const state = sceneStateRef.current;
    if (!state?.graphForce) return;
    for (const node of state.graphForce.nodes) {
      node.pinned = false;
      node.fx = null;
      node.fy = null;
      node.fz = null;
    }
    state.graphForce.simulation.alpha(0.42).alphaTarget(0.04).restart();
    setPinnedCount(0);
    state.markInteract();
  }

  // Pulse hovered dot
  useEffect(() => {
    const state = sceneStateRef.current;
    if (!state) return;
    forEachDotData(state.dotMap, (d) => {
      const target = d.item.id === hovered ? 1.6 : 1;
      d.mesh.scale.setScalar(target);
      d.halo.scale.setScalar(target);
    });
  }, [hovered]);

  const hoveredItem = useMemo(() => {
    if (!hovered) return null;
    const state = sceneStateRef.current;
    if (!state) return null;
    for (const [object, d] of state.dotMap) {
      if (object === d.mesh && d.item.id === hovered) return d.item;
    }
    return null;
  }, [hovered]);

  const visibleAgents = useMemo(() => {
    const counts: Record<string, number> = {};
    for (const node of data?.nodes ?? []) {
      const id = nodeAgentId(node);
      counts[id] = (counts[id] || 0) + 1;
    }
    if (showActivity) {
      for (const e of entries) counts[e.agent_id] = (counts[e.agent_id] || 0) + 1;
    }
    return counts;
  }, [data?.nodes, entries, showActivity]);

  const selectedRelatedNodes = useMemo(() => {
    if (!selected || selected.itemKind !== 'node' || !data) return [];
    const nodeById = new Map(data.nodes.map((node) => [node.id, node]));
    const relatedIds = new Set<string>();
    for (const edge of data.edges ?? []) {
      if (edge.source === selected.node.id) relatedIds.add(edge.target);
      if (edge.target === selected.node.id) relatedIds.add(edge.source);
    }
    return Array.from(relatedIds)
      .map((id) => nodeById.get(id))
      .filter((node): node is BrainGraphNode => Boolean(node));
  }, [selected, data]);

  function update<K extends keyof BrainFilters>(key: K, value: BrainFilters[K]) {
    setFilters((f) => ({ ...f, [key]: value }));
  }
  function toggleHidden(set: 'hiddenAgents' | 'hiddenLobes', id: string) {
    setFilters((f) => {
      const next = new Set(f[set]);
      if (next.has(id)) next.delete(id); else next.add(id);
      return { ...f, [set]: next };
    });
  }

  if (!webglAvailable) {
    if (data) {
      return (
        <BrainGraph2D
          data={data}
          mode="hive"
          agentFilter={agentFilter}
          agentColors={agentColors}
          showActivity={showActivity}
          allowActivityToggle={false}
          blurOn={blurOn}
        />
      );
    }
    return <BrainGraph entries={entries} agentFilter={agentFilter} agentColors={agentColors} blurOn={blurOn} />;
  }

  return (
    <div class="flex-1 flex min-h-0 relative">
      <div
        ref={wrapRef}
        class="flex-1 relative overflow-hidden"
        style={{
          background:
            'radial-gradient(ellipse 70% 60% at 50% 50%, color-mix(in srgb, var(--color-accent) 7%, transparent), transparent 70%), var(--color-bg)',
          cursor: 'grab',
          touchAction: 'none',
        }}
        onPointerDownCapture={handlePointerDown as any}
        onPointerMove={handlePointerMove as any}
        onPointerUp={handlePointerUp as any}
        onPointerCancel={handlePointerCancel as any}
        onMouseDownCapture={handlePointerDown as any}
        onMouseMove={handlePointerMove as any}
        onMouseUp={handlePointerUp as any}
        onMouseLeave={() => setHovered(null)}
        onClick={handleClick as any}
      >
        {!panelOpen && (
          <div class="absolute top-4 right-4 flex items-center gap-2 z-30">
            {pinnedCount > 0 && (
              <button
                type="button"
                onClick={(e) => { e.stopPropagation(); resetGraphPins(); }}
                class="inline-flex items-center justify-center w-8 h-8 rounded-full bg-[var(--color-card)] border border-[var(--color-border)] hover:border-[var(--color-accent)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] shadow-lg transition-colors"
                style={{ backdropFilter: 'blur(8px)' }}
                title={`Release ${pinnedCount} pinned node${pinnedCount === 1 ? '' : 's'}`}
                aria-label="Release pinned graph nodes"
              >
                <RotateCw size={13} />
              </button>
            )}
            <button
              type="button"
              onClick={(e) => { e.stopPropagation(); setPanelOpen(true); }}
              class="inline-flex items-center gap-2 px-3 py-1.5 rounded-full bg-[var(--color-card)] border border-[var(--color-border)] hover:border-[var(--color-accent)] text-[11.5px] text-[var(--color-text)] shadow-lg transition-colors"
              style={{ backdropFilter: 'blur(8px)' }}
            >
              <SlidersHorizontal size={12} />
              Filters
              <span class="text-[10.5px] text-[var(--color-text-faint)] tabular-nums">
                {scenePointCount}
              </span>
            </button>
          </div>
        )}

        {/* Drag hint */}
        <div class="absolute bottom-3 left-1/2 -translate-x-1/2 text-[10.5px] text-[var(--color-text-faint)] pointer-events-none select-none z-30 px-2 py-0.5 rounded bg-[var(--color-bg)]/60" style={{ backdropFilter: 'blur(4px)' }}>
          click node to focus · drag node to pin
        </div>

        {hoveredItem && mousePos && !selected && (
          <div
            class="absolute pointer-events-none bg-[var(--color-card)] border border-[var(--color-border)] rounded-lg shadow-xl px-3 py-2 text-[11.5px] text-[var(--color-text)] max-w-[320px] z-10"
            style={{
              left: Math.min(mousePos.x + 14, (wrapRef.current?.clientWidth || 800) - 340),
              top: Math.min(mousePos.y + 14, (wrapRef.current?.clientHeight || 500) - 110),
              backdropFilter: 'blur(8px)',
            }}
          >
            <div class="flex items-center gap-2 mb-1">
              <span
                class="inline-block w-1.5 h-1.5 rounded-full"
                style={{ backgroundColor: itemColor(hoveredItem, agentColors) }}
              />
              <span class="font-mono text-[10.5px] text-[var(--color-text-muted)]">
                {hoveredItem.itemKind === 'node' ? hoveredItem.label : `@${hoveredItem.agent_id}`} · {hoveredItem.action}
              </span>
              <span class="text-[10px] text-[var(--color-text-faint)] ml-auto tabular-nums">
                {formatRelativeTime(hoveredItem.created_at)}
              </span>
            </div>
            <div class={'leading-snug ' + (blurOn ? 'privacy-blur revealed' : '')}>
              {hoveredItem.summary}
            </div>
          </div>
        )}

        {/* Lobe hover stats — only when not hovering a specific entry. */}
        {!hoveredItem && hoveredLobe && mousePos && !selected && (
          <LobeStatsTooltip
            lobeId={hoveredLobe}
            entries={entries}
            nodes={data?.nodes ?? []}
            agentColors={agentColors}
            mousePos={mousePos}
            wrapWidth={wrapRef.current?.clientWidth || 800}
            wrapHeight={wrapRef.current?.clientHeight || 500}
          />
        )}
      </div>

      <aside
        class={[
          'absolute top-0 right-0 bottom-0 w-[320px] bg-[var(--color-card)] border-l border-[var(--color-border)] flex flex-col min-h-0 shadow-2xl z-20',
          'transition-transform duration-300 ease-out',
          panelOpen ? 'translate-x-0' : 'translate-x-full',
        ].join(' ')}
        style={{ backdropFilter: 'blur(8px)' }}
      >
        {selected ? (
          <DetailPanel
            item={selected}
            color={itemColor(selected, agentColors)}
            blurOn={blurOn}
            lobeLabel={LOBE_BY_ID[selected.lobe]?.label}
            relatedNodes={selectedRelatedNodes}
            onClose={() => { setSelected(null); setPanelOpen(false); }}
          />
        ) : (
          <FilterPanel
            filters={filters}
            update={update}
            toggleHidden={toggleHidden}
            visibleAgents={visibleAgents}
            agentColors={agentColors}
            onReset={() => setFilters(DEFAULT_FILTERS)}
            totalEntries={(data?.nodes?.length ?? 0) + (showActivity ? entries.length : 0)}
            visibleEntries={scenePointCount}
            onClose={() => setPanelOpen(false)}
          />
        )}
      </aside>
    </div>
  );
}

// Detail + Filter panels: identical to the 2D version visually.

function DetailPanel({
  item, color, blurOn, lobeLabel, relatedNodes = [], onClose,
}: {
  item: BrainDotItem;
  color: string;
  blurOn: boolean;
  lobeLabel?: string;
  relatedNodes?: BrainGraphNode[];
  onClose: () => void;
}) {
  const [revealed, setRevealed] = useState(false);
  const isNode = item.itemKind === 'node';
  const nodeBody = isNode ? getBrainNodeDetailBody(item.node, relatedNodes) : item.summary;
  const nodeBodyHtml = isNode ? renderMarkdown(nodeBody) : '';
  return (
    <>
      <header class="flex items-center px-4 py-3 border-b border-[var(--color-border)] gap-2">
        <span class="inline-block w-2 h-2 rounded-full" style={{ backgroundColor: color }} />
        <span class="font-mono text-[12px] text-[var(--color-text)]">{isNode ? item.label : `@${item.agent_id}`}</span>
        {lobeLabel && (
          <span class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] ml-1">{lobeLabel}</span>
        )}
        <span class="text-[10.5px] text-[var(--color-text-faint)] ml-auto tabular-nums">
          {formatRelativeTime(item.created_at)}
        </span>
        <button type="button" onClick={onClose} class="p-1 rounded hover:bg-[var(--color-elevated)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors">
          <X size={13} />
        </button>
      </header>
      <div class="flex-1 overflow-y-auto px-4 py-3 space-y-3">
        {isNode && (
          <Field label="Memory Body">
            {nodeBody ? (
              <div
                class={'max-h-[260px] overflow-y-auto rounded-md border border-[var(--color-accent)] bg-[var(--color-accent-soft)]/35 px-3 py-2 text-[12.5px] text-[var(--color-text)] leading-relaxed prose-sm ' + (blurOn && !revealed ? 'privacy-blur' : (blurOn && revealed ? 'privacy-blur revealed' : ''))}
                onClick={() => blurOn && setRevealed((v) => !v)}
                dangerouslySetInnerHTML={{ __html: nodeBodyHtml }}
              />
            ) : (
              <div class="rounded-md border border-[var(--color-border)] bg-[var(--color-elevated)] px-3 py-2 text-[12px] text-[var(--color-text-muted)]">
                No loaded memory body for this node.
              </div>
            )}
          </Field>
        )}
        {!isNode && (
          <Field label="Action">
            <span class="font-mono text-[11.5px] text-[var(--color-text)]">{item.action}</span>
          </Field>
        )}
        {!isNode && (
          <Field label="Summary">
            <div
              class={'text-[12.5px] text-[var(--color-text)] leading-relaxed ' + (blurOn && !revealed ? 'privacy-blur' : (blurOn && revealed ? 'privacy-blur revealed' : ''))}
              onClick={() => blurOn && setRevealed((v) => !v)}
            >
              {item.summary}
            </div>
          </Field>
        )}
        {isNode && (
          <NodeProperties node={item.node} degree={item.degree} relatedNodes={relatedNodes} />
        )}
        {!isNode && item.artifacts && (
          <Field label="Artifacts">
            <div class="font-mono text-[11px] text-[var(--color-text-muted)] whitespace-pre-wrap break-words">{item.artifacts}</div>
          </Field>
        )}
        {!isNode && (
          <Field label="Chat">
            <div class="font-mono text-[11px] text-[var(--color-text-muted)] truncate">{item.chat_id}</div>
          </Field>
        )}
      </div>
    </>
  );
}

function NodeProperties({ node, degree, relatedNodes }: { node: BrainGraphNode; degree: number; relatedNodes: BrainGraphNode[] }) {
  const tags = Array.isArray(node.tags) ? node.tags : [];
  const created = formatNodeDate(node.created_at);
  const visibleRelated = relatedNodes.slice(0, 5);
  return (
    <section class="rounded-md border border-[var(--color-border)] bg-[var(--color-bg)]/45 px-3 py-3">
      <div class="text-[11px] font-semibold text-[var(--color-text)] mb-2">Properties</div>
      <div class="space-y-2">
        <PropertyRow label="title">
          <span class="text-[12px] text-[var(--color-text)] break-words">{node.label}</span>
        </PropertyRow>
        <PropertyRow label="tags">
          {tags.length > 0 ? (
            <div class="flex flex-wrap gap-1">
              {tags.map((tag) => (
                <span key={tag} class="px-1.5 py-0.5 rounded-full bg-[var(--color-elevated)] text-[10.5px] text-[var(--color-accent)]">
                  {tag}
                </span>
              ))}
            </div>
          ) : (
            <span class="text-[11px] text-[var(--color-text-faint)]">No tags</span>
          )}
        </PropertyRow>
        <PropertyRow label="type">
          <span class="font-mono text-[11px] text-[var(--color-text-muted)]">{node.kind}</span>
        </PropertyRow>
        <PropertyRow label="scope">
          <span class="font-mono text-[11px] text-[var(--color-text-muted)]">
            {node.scope_type}/{normalizeScopeId(node.scope_id)}
          </span>
        </PropertyRow>
        <PropertyRow label="connections">
          <span class="font-mono text-[11px] text-[var(--color-text-muted)]">{degree}</span>
        </PropertyRow>
        {created && (
          <PropertyRow label="created">
            <span class="font-mono text-[11px] text-[var(--color-text-muted)]">{created}</span>
          </PropertyRow>
        )}
        {node.source_path && (
          <PropertyRow label="source">
            <span class="font-mono text-[11px] text-[var(--color-text-muted)] break-all">{node.source_path}</span>
          </PropertyRow>
        )}
        {node.section_title && (
          <PropertyRow label="section">
            <span class="text-[11px] text-[var(--color-text-muted)] break-words">{node.section_title}</span>
          </PropertyRow>
        )}
        {relatedNodes.length > 0 && (
          <PropertyRow label="connects">
            <div class="flex flex-wrap gap-1">
              {visibleRelated.map((related) => (
                <span key={related.id} class="px-1.5 py-0.5 rounded bg-[var(--color-elevated)] text-[10.5px] text-[var(--color-text-muted)] max-w-[190px] truncate">
                  {related.label}
                </span>
              ))}
              {relatedNodes.length > visibleRelated.length && (
                <span class="px-1.5 py-0.5 rounded bg-[var(--color-elevated)] text-[10.5px] text-[var(--color-text-faint)]">
                  +{relatedNodes.length - visibleRelated.length}
                </span>
              )}
            </div>
          </PropertyRow>
        )}
      </div>
    </section>
  );
}

function PropertyRow({ label, children }: { label: string; children: any }) {
  return (
    <div class="grid grid-cols-[72px_minmax(0,1fr)] gap-2 items-start">
      <div class="text-[10.5px] text-[var(--color-text-faint)]">{label}</div>
      <div class="min-w-0">{children}</div>
    </div>
  );
}

function formatNodeDate(value: unknown): string | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  const seconds = value > 10_000_000_000 ? value / 1000 : value;
  if (seconds <= 0) return null;
  return new Date(seconds * 1000).toLocaleDateString(undefined, {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
}

function Field({ label, children }: { label: string; children: any }) {
  return (
    <div>
      <div class="text-[10px] uppercase tracking-wider text-[var(--color-text-faint)] mb-1">{label}</div>
      {children}
    </div>
  );
}

function FilterPanel({
  filters, update, toggleHidden, visibleAgents, agentColors, onReset, totalEntries, visibleEntries, onClose,
}: {
  filters: BrainFilters;
  update: <K extends keyof BrainFilters>(key: K, value: BrainFilters[K]) => void;
  toggleHidden: (set: 'hiddenAgents' | 'hiddenLobes', id: string) => void;
  visibleAgents: Record<string, number>;
  agentColors: Record<string, string>;
  onReset: () => void;
  totalEntries: number;
  visibleEntries: number;
  onClose: () => void;
}) {
  const [openSection, setOpenSection] = useState({ agents: true, lobes: false, display: false });
  return (
    <>
      <header class="flex items-center px-4 py-3 border-b border-[var(--color-border)] gap-2">
        <Sparkles size={13} class="text-[var(--color-accent)]" />
        <span class="text-[12.5px] font-semibold text-[var(--color-text)]">Filters</span>
        <span class="text-[10.5px] text-[var(--color-text-faint)] ml-auto tabular-nums">
          {visibleEntries} / {totalEntries}
        </span>
        <button type="button" onClick={onReset} class="p-1 rounded hover:bg-[var(--color-elevated)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors" title="Reset">
          <RotateCw size={11} />
        </button>
        <button type="button" onClick={onClose} class="p-1 rounded hover:bg-[var(--color-elevated)] text-[var(--color-text-muted)] hover:text-[var(--color-text)] transition-colors" title="Close">
          <X size={13} />
        </button>
      </header>
      <div class="flex-1 overflow-y-auto px-4 py-3 space-y-4">
        <div class="relative">
          <Search size={12} class="absolute left-2.5 top-1/2 -translate-y-1/2 text-[var(--color-text-faint)]" />
          <input
            value={filters.query}
            onInput={(e) => update('query', (e.target as HTMLInputElement).value)}
            placeholder="Search summaries…"
            class="w-full pl-7 pr-2.5 py-1.5 rounded bg-[var(--color-bg)] border border-[var(--color-border)] focus:border-[var(--color-accent)] focus:outline-none text-[12px] text-[var(--color-text)]"
          />
        </div>
        <Section label="Agents" open={openSection.agents} onToggle={() => setOpenSection((s) => ({ ...s, agents: !s.agents }))}>
          <div class="space-y-1">
            {Object.entries(visibleAgents).sort((a, b) => b[1] - a[1]).map(([id, count]) => {
              const on = !filters.hiddenAgents.has(id);
              const color = agentColors[id] || 'var(--color-text-muted)';
              const lobe = LOBE_BY_ID[lobeFor(id)];
              return (
                <button
                  key={id}
                  type="button"
                  onClick={() => toggleHidden('hiddenAgents', id)}
                  class="w-full flex items-center gap-2 px-2 py-1.5 rounded hover:bg-[var(--color-elevated)] transition-colors text-left"
                >
                  <span class="inline-block w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: color, boxShadow: on ? `0 0 6px ${color}` : 'none' }} />
                  <span class={'font-mono text-[11.5px] ' + (on ? 'text-[var(--color-text)]' : 'text-[var(--color-text-faint)]')}>@{id}</span>
                  {lobe && <span class="text-[10px]" style={{ color: on ? `#${lobe.color.getHexString()}` : 'var(--color-text-faint)', opacity: on ? 0.75 : 0.4 }}>{lobe.label.toLowerCase()}</span>}
                  <span class="ml-auto text-[10.5px] tabular-nums text-[var(--color-text-faint)]">{count}</span>
                  <span class={'brain-switch ' + (on ? 'is-on' : '')} />
                </button>
              );
            })}
          </div>
        </Section>
        <Section label="Regions" open={openSection.lobes} onToggle={() => setOpenSection((s) => ({ ...s, lobes: !s.lobes }))}>
          <div class="space-y-1">
            {LOBES.map((l) => {
              const on = !filters.hiddenLobes.has(l.id);
              const colorHex = `#${l.color.getHexString()}`;
              return (
                <button key={l.id} type="button" onClick={() => toggleHidden('hiddenLobes', l.id)} class="w-full flex items-center gap-2 px-2 py-1.5 rounded hover:bg-[var(--color-elevated)] transition-colors text-left">
                  <span class="inline-block w-2.5 h-2.5 rounded-sm shrink-0" style={{ backgroundColor: colorHex, opacity: on ? 1 : 0.3, boxShadow: on ? `0 0 6px ${colorHex}` : 'none' }} />
                  <span class={'text-[12px] ' + (on ? 'text-[var(--color-text)]' : 'text-[var(--color-text-faint)]')}>{l.label}</span>
                  <span class={'brain-switch ml-auto ' + (on ? 'is-on' : '')} />
                </button>
              );
            })}
          </div>
        </Section>
        <Section label="Display" open={openSection.display} onToggle={() => setOpenSection((s) => ({ ...s, display: !s.display }))}>
          <SegmentedRow label="Quality">
            {(['low', 'medium', 'high'] as BrainQualityPreset[]).map((preset) => (
              <SegmentButton
                key={preset}
                active={filters.quality === preset}
                onClick={() => update('quality', preset)}
                title={`${BRAIN_QUALITY_PRESETS[preset].label} quality`}
              >
                {BRAIN_QUALITY_PRESETS[preset].label}
              </SegmentButton>
            ))}
          </SegmentedRow>
          <SegmentedRow label="LOD">
            {(['clusters', 'balanced', 'detail'] as BrainLodMode[]).map((mode) => (
              <SegmentButton
                key={mode}
                active={filters.lod === mode}
                onClick={() => update('lod', mode)}
                title={`${BRAIN_LOD_MODES[mode].label} LOD`}
              >
                {BRAIN_LOD_MODES[mode].label}
              </SegmentButton>
            ))}
          </SegmentedRow>
          <SegmentedRow label="Silhouette">
            {(['minimal', 'neural', 'anatomical'] as BrainSilhouetteMode[]).map((mode) => (
              <SegmentButton
                key={mode}
                active={filters.silhouette === mode}
                onClick={() => update('silhouette', mode)}
                title={`${BRAIN_SILHOUETTES[mode].label} silhouette`}
              >
                {BRAIN_SILHOUETTES[mode].label}
              </SegmentButton>
            ))}
          </SegmentedRow>
          <SliderRow
            label="Glow intensity"
            value={filters.nodeSize}
            min={0}
            max={2}
            step={0.05}
            onInput={(v) => update('nodeSize', v)}
          />
        </Section>
      </div>
    </>
  );
}

function Section({ label, open, onToggle, children }: { label: string; open: boolean; onToggle: () => void; children: any }) {
  return (
    <div>
      <button type="button" onClick={onToggle} class="w-full flex items-center gap-1 text-[10.5px] uppercase tracking-wider text-[var(--color-text-faint)] hover:text-[var(--color-text-muted)] mb-1.5">
        {open ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
        {label}
      </button>
      {open && children}
    </div>
  );
}

function SegmentedRow({ label, children }: { label: string; children: any }) {
  return (
    <div class="space-y-1.5">
      <div class="text-[11px] text-[var(--color-text-muted)]">{label}</div>
      <div class="grid grid-cols-3 gap-1">
        {children}
      </div>
    </div>
  );
}

function SegmentButton({ active, onClick, title, children }: { active: boolean; onClick: () => void; title: string; children: any }) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      class={[
        'h-7 px-1.5 rounded border text-[10.5px] transition-colors truncate',
        active
          ? 'border-[var(--color-accent)] bg-[var(--color-accent-soft)] text-[var(--color-accent)]'
          : 'border-[var(--color-border)] bg-[var(--color-bg)] text-[var(--color-text-muted)] hover:text-[var(--color-text)]',
      ].join(' ')}
    >
      {children}
    </button>
  );
}

function SliderRow({ label, value, min, max, step, onInput, fmt }: { label: string; value: number; min: number; max: number; step: number; onInput: (v: number) => void; fmt?: (v: number) => string }) {
  return (
    <div>
      <div class="flex items-center justify-between mb-1">
        <span class="text-[11px] text-[var(--color-text-muted)]">{label}</span>
        <span class="text-[10.5px] text-[var(--color-text-faint)] tabular-nums">{fmt ? fmt(value) : value.toFixed(2)}</span>
      </div>
      <input type="range" class="brain-slider" min={min} max={max} step={step} value={value} onInput={(e) => onInput(parseFloat((e.target as HTMLInputElement).value))} />
    </div>
  );
}

// ── Lobe hover stats ─────────────────────────────────────────────────
// Each lobe stands in for a "function" of the brain. Hovering shows
// what that lobe is currently full of: total entry count + a small
// agent-distribution pie chart.

const LOBE_FUNCTION: Record<string, string> = {
  frontal:   'Decisions & planning',
  parietal:  'Sensing & integration',
  temporal:  'Language & memory',
  occipital: 'Output & creation',
};

function LobeStatsTooltip({
  lobeId, entries, nodes, agentColors, mousePos, wrapWidth, wrapHeight,
}: {
  lobeId: string;
  entries: HiveEntry[];
  nodes: BrainGraphNode[];
  agentColors: Record<string, string>;
  mousePos: { x: number; y: number };
  wrapWidth: number;
  wrapHeight: number;
}) {
  const lobe = LOBE_BY_ID[lobeId];
  if (!lobe) return null;

  // Tally graph nodes and activity mapped to this lobe, grouped by scope/agent.
  const byAgent: Record<string, number> = {};
  let total = 0;
  let nodeTotal = 0;
  for (const node of nodes) {
    if (nodeLobe(node) !== lobeId) continue;
    const id = nodeAgentId(node);
    byAgent[id] = (byAgent[id] || 0) + 1;
    total++;
    nodeTotal++;
  }
  for (const e of entries) {
    if (lobeFor(e.agent_id) !== lobeId) continue;
    byAgent[e.agent_id] = (byAgent[e.agent_id] || 0) + 1;
    total++;
  }
  const slices = Object.entries(byAgent).sort((a, b) => b[1] - a[1]);

  return (
    <div
      class="absolute pointer-events-none bg-[var(--color-card)]/95 border border-[var(--color-border)] rounded-lg shadow-xl p-3 text-[12px] text-[var(--color-text)] z-10 brain-tooltip-enter"
      style={{
        left: Math.min(mousePos.x + 14, wrapWidth - 230),
        top: Math.min(mousePos.y + 14, wrapHeight - 200),
        backdropFilter: 'blur(8px)',
        width: 220,
      }}
    >
      <div class="flex items-center gap-2 mb-1">
        <span
          class="inline-block w-2 h-2 rounded-full"
          style={{ backgroundColor: `#${lobe.color.getHexString()}` }}
        />
        <span class="font-semibold">{lobe.label}</span>
        <span class="ml-auto text-[10.5px] text-[var(--color-text-faint)] tabular-nums">
          {nodeTotal} nodes / {total - nodeTotal} events
        </span>
      </div>
      <div class="text-[10.5px] uppercase tracking-wider text-[var(--color-text-faint)] mb-2">
        {LOBE_FUNCTION[lobeId] || lobe.label}
      </div>
      {total === 0 ? (
        <div class="text-[11px] text-[var(--color-text-faint)]">No graph nodes or activity yet in this region.</div>
      ) : (
        <div class="flex items-center gap-3">
          <LobePie slices={slices} agentColors={agentColors} />
          <div class="flex-1 space-y-1 min-w-0">
            {slices.slice(0, 4).map(([agentId, count]) => (
              <div key={agentId} class="flex items-center gap-1.5 text-[10.5px]">
                <span
                  class="inline-block w-1.5 h-1.5 rounded-full shrink-0"
                  style={{ backgroundColor: agentColors[agentId] || 'var(--color-text-muted)' }}
                />
                <span class="font-mono truncate text-[var(--color-text-muted)]">@{agentId}</span>
                <span class="ml-auto tabular-nums text-[var(--color-text-faint)]">{count}</span>
              </div>
            ))}
            {slices.length > 4 && (
              <div class="text-[10px] text-[var(--color-text-faint)]">
                +{slices.length - 4} more
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function LobePie({
  slices, agentColors,
}: {
  slices: [string, number][];
  agentColors: Record<string, string>;
}) {
  const total = slices.reduce((sum, [, c]) => sum + c, 0);
  const cx = 32, cy = 32, r = 28;
  let acc = 0;
  // Resolve CSS-var colors once for SVG fill (Three.js path resolves
  // them too; SVG can use them directly but only if they're real CSS
  // refs, which is fine for our case).
  function color(agent: string) {
    const raw = agentColors[agent] || '#888';
    if (raw.startsWith('var(')) {
      const m = raw.match(/var\((--[^)]+)\)/);
      if (m) {
        const v = getComputedStyle(document.documentElement).getPropertyValue(m[1]).trim();
        if (v) return v;
      }
    }
    return raw;
  }
  return (
    <svg width="64" height="64" viewBox="0 0 64 64" class="shrink-0">
      <circle cx={cx} cy={cy} r={r} fill="var(--color-bg)" stroke="var(--color-border)" stroke-width="0.5" />
      {slices.map(([agentId, count], i) => {
        const startAngle = (acc / total) * 2 * Math.PI - Math.PI / 2;
        const endAngle = ((acc + count) / total) * 2 * Math.PI - Math.PI / 2;
        acc += count;
        const x1 = cx + r * Math.cos(startAngle);
        const y1 = cy + r * Math.sin(startAngle);
        const x2 = cx + r * Math.cos(endAngle);
        const y2 = cy + r * Math.sin(endAngle);
        const largeArc = endAngle - startAngle > Math.PI ? 1 : 0;
        // Single-slice case: draw a filled circle instead of an arc
        // (an arc with start === end would render nothing).
        if (slices.length === 1) {
          return <circle key={agentId} cx={cx} cy={cy} r={r} fill={color(agentId)} />;
        }
        const d = `M ${cx},${cy} L ${x1},${y1} A ${r},${r} 0 ${largeArc} 1 ${x2},${y2} Z`;
        return <path key={agentId} d={d} fill={color(agentId)} stroke="var(--color-bg)" stroke-width="0.5" />;
      })}
    </svg>
  );
}
