/* Production digital twin: a pillow plant in 3D.

   createPlantScene(container, facility, callbacks) builds the building, the
   receiving and bale store, eight pillow lines from one line template, CV
   packing, QA hold, palletizing, high-bay racks and the dispatch docks, plus
   department markers and animated flow arrows (the layout language of the
   food-plant canvas, rebuilt for pillows). Returns:
     update(facility)            live state (line status, machine health, racks, docks, QA)
     setCamera(preset)           'overview' | 'front' | 'side' | 'top' (smooth tween)
     focusStage(key|null)        glow + outline one stage on every line, dim the rest, fly there
     setLayers({roof, markers, flow, labels})
     setToken(stageKey|null, progress 0..1, lineIndex)   walkthrough pillow
     dispose()
   callbacks: onSelectStage(key), onOpenLine(id), onOpenMachine(id), onHover(info|null, x, y) */
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import { RoundedBoxGeometry } from 'three/examples/jsm/geometries/RoundedBoxGeometry.js';
import { mergeGeometries } from 'three/examples/jsm/utils/BufferGeometryUtils.js';
import { STAGES, SUPPORT, STAGE_BY_KEY, MACHINE_STAGE } from './stages';

export const HALL = { x0: -134, x1: 130, z0: -66, z1: 62, h: 13 };
export const lineZ = (i) => -40 + i * 11;
const X = {
  feed: -96.5, opener: -89, flapper: -84.2, bin: -79.8, chain: -73.5, card: -61, cardLen: 18, lapper: -49.5,
  fill: -38, shell: -34.6, sewBuf: -29, sew1: -24, sew2: -17.5, bagBuf: -11.5, press: -5.5, conv0: -2, conv1: 22,
  pack: 27.5, cam: 24.6, pal: 44, wrap: 51.5,
};
const RACK_X = { A: 66, B: 78, C: 90, D: 102 };
const RACK = { z0: -44, module: 6.8, levelH: 2.2, levels: 4, bays: 4, racks: 3 };
const DOCKS = { D1: -36, D2: -12, D3: 12, D4: 36 };
const RECV_DOORS = [-20, 14];
const STATUS_COLOR = { Running: 0x22c55e, Changeover: 0xf59e0b, Idle: 0x64748b, Breakdown: 0xef4444, Maintenance: 0xf97316 };
const CENTER = new THREE.Vector3(-2, 0, -6);
const PRESETS = {
  overview: { pos: [-128, 122, 142], target: [-26, 0, -4] },
  front: { pos: [-20, 74, 168], target: [-20, 0, -8] },
  side: { pos: [-236, 66, 22], target: [-14, 0, -6] },
  top: { pos: [-30, 250, -11.5], target: [-30, 0, -12] },
  // compact Production-page card: wide, short viewport, whole hall in frame
  card: { pos: [-56, 152, 110], target: [-14, 0, -4] },
};

const fmt = (n) => (n == null ? '—' : Math.round(n).toLocaleString('en-US'));
const BOX = new THREE.BoxGeometry(1, 1, 1);
const CYL = new THREE.CylinderGeometry(0.5, 0.5, 1, 20);
const CONE = new THREE.ConeGeometry(0.5, 1, 20);

function el(html, cls, onClick) {
  const d = document.createElement('div');
  d.className = cls;
  d.innerHTML = html;
  if (onClick) {
    d.style.pointerEvents = 'auto';
    d.style.cursor = 'pointer';
    d.addEventListener('pointerdown', (e) => { e.stopPropagation(); onClick(); });
  }
  return new CSS2DObject(d);
}

export function createPlantScene(container, initial, cb = {}, opts = {}) {
  const home = PRESETS[opts.preset] ? opts.preset : 'overview';
  const W = () => container.clientWidth || 900;
  const H = () => container.clientHeight || 560;

  // ── renderer / camera ──────────────────────────────────────────────
  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(W(), H());
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.1;
  renderer.domElement.style.display = 'block';
  container.appendChild(renderer.domElement);
  const labels = new CSS2DRenderer();
  labels.setSize(W(), H());
  Object.assign(labels.domElement.style, { position: 'absolute', inset: '0', pointerEvents: 'none' });
  container.appendChild(labels.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x070d1a);
  scene.fog = new THREE.Fog(0x070d1a, 420, 900);
  const camera = new THREE.PerspectiveCamera(40, W() / H(), 1, 2400);
  camera.position.set(...PRESETS[home].pos);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.1;
  controls.maxPolarAngle = 1.47;
  controls.minDistance = 25;
  controls.maxDistance = 620;
  controls.target.set(...PRESETS[home].target);
  controls.update();

  // ── lights (canvas: ambient + key + rim + hemisphere) ───────────────
  scene.add(new THREE.HemisphereLight(0xc8dcff, 0x1a2333, 0.85));
  scene.add(new THREE.AmbientLight(0xffffff, 0.18));
  const sun = new THREE.DirectionalLight(0xffffff, 1.6);
  sun.position.set(-90, 210, 120);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  Object.assign(sun.shadow.camera, { left: -170, right: 170, top: 110, bottom: -110, near: 20, far: 520 });
  sun.shadow.bias = -0.0005;
  scene.add(sun);
  const rim = new THREE.DirectionalLight(0x7aa2ff, 0.45);
  rim.position.set(160, 120, -140);
  scene.add(rim);

  // ── materials: one set per stage so a stage can glow or dim as a unit ─
  const stageMats = {};
  const matCache = new Map();
  function smat(stage, color, opts = {}) {
    const key = `${stage}|${color}|${JSON.stringify(opts)}`;
    if (matCache.has(key)) return matCache.get(key);
    const m = new THREE.MeshStandardMaterial({ color, roughness: 0.6, metalness: 0.22, ...opts });
    m.userData.base = m.color.clone();
    m.userData.baseEm = m.emissive.clone();
    m.userData.baseEI = m.emissiveIntensity;
    (stageMats[stage] ||= []).push(m);
    matCache.set(key, m);
    return m;
  }
  function uniqueMat(stage, color, opts = {}) {
    const m = new THREE.MeshStandardMaterial({ color, roughness: 0.55, metalness: 0.25, ...opts });
    m.userData.base = m.color.clone();
    m.userData.baseEm = m.emissive.clone();
    m.userData.baseEI = m.emissiveIntensity;
    (stageMats[stage] ||= []).push(m);
    return m;
  }

  // static geometry is merged per material (few draw calls)
  const buckets = new Map();
  const tmpM = new THREE.Matrix4();
  const tmpQ = new THREE.Quaternion();
  const tmpE = new THREE.Euler();
  const tmpV = new THREE.Vector3();
  const tmpS = new THREE.Vector3();
  function put(geo, mat, x, y, z, sx = 1, sy = 1, sz = 1, rx = 0, ry = 0, rz = 0) {
    tmpM.compose(tmpV.set(x, y, z), tmpQ.setFromEuler(tmpE.set(rx, ry, rz)), tmpS.set(sx, sy, sz));
    const g = geo.clone();
    g.applyMatrix4(tmpM);
    if (!buckets.has(mat)) buckets.set(mat, []);
    buckets.get(mat).push(g);
  }
  const bx = (mat, w, h, d, x, y, z, ry = 0) => put(BOX, mat, x, y, z, w, h, d, 0, ry, 0);
  const root = new THREE.Group();
  scene.add(root);
  const stageEdges = {};
  function flushStatic(parent, stageOfMat) {
    buckets.forEach((geos, mat) => {
      const merged = mergeGeometries(geos, false);
      geos.forEach((g) => g.dispose());
      if (!merged) return;
      const mesh = new THREE.Mesh(merged, mat);
      mesh.castShadow = true;
      mesh.receiveShadow = true;
      parent.add(mesh);
      const st = stageOfMat.get(mat);
      if (st && !mat.transparent) {
        const edges = new THREE.LineSegments(new THREE.EdgesGeometry(merged, 35), edgeMat(st));
        edges.visible = false;
        parent.add(edges);
        (stageEdges[st] ||= []).push(edges);
      }
    });
    buckets.clear();
  }
  const edgeMats = {};
  function edgeMat(stage) {
    if (!edgeMats[stage]) {
      const c = (STAGE_BY_KEY[stage] || SUPPORT.find((s) => s.key === stage) || { color: '#38bdf8' }).color;
      edgeMats[stage] = new THREE.LineBasicMaterial({ color: c, transparent: true, opacity: 0.95 });
    }
    return edgeMats[stage];
  }
  const stageOfMat = new Map();
  const S = (stage, color, opts) => { const m = smat(stage, color, opts); stageOfMat.set(m, stage); return m; };

  // ── ground, slab, aisles ───────────────────────────────────────────
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(900, 900), new THREE.MeshStandardMaterial({ color: 0x0b1424, roughness: 1 }));
  ground.rotation.x = -Math.PI / 2;
  ground.position.y = -0.05;
  ground.receiveShadow = true;
  scene.add(ground);
  const grid = new THREE.GridHelper(800, 100, 0x2a5d8f, 0x1a2c44);
  grid.material.transparent = true;
  grid.material.opacity = 0.32;
  grid.position.y = -0.03;
  scene.add(grid);
  const slab = new THREE.Mesh(new THREE.PlaneGeometry(HALL.x1 - HALL.x0, HALL.z1 - HALL.z0),
    new THREE.MeshStandardMaterial({ color: 0x232f42, roughness: 0.92, metalness: 0.05 }));
  slab.rotation.x = -Math.PI / 2;
  slab.position.set((HALL.x0 + HALL.x1) / 2, 0, (HALL.z0 + HALL.z1) / 2);
  slab.receiveShadow = true;
  scene.add(slab);
  const floorPlane = (x0, x1, z0, z1, color, opacity, y = 0.03, parent = scene) => {
    const m = new THREE.Mesh(new THREE.PlaneGeometry(x1 - x0, z1 - z0),
      new THREE.MeshBasicMaterial({ color, transparent: true, opacity, depthWrite: false }));
    m.rotation.x = -Math.PI / 2;
    m.position.set((x0 + x1) / 2, y, (z0 + z1) / 2);
    parent.add(m);
    return m;
  };
  // department floor tints + selection glow
  const glow = {};
  [...STAGES, ...SUPPORT].forEach((s) => {
    const [x0, x1, z0, z1] = s.region;
    floorPlane(x0, x1, z0, z1, s.color, 0.045, 0.02);
    const g = floorPlane(x0 - 0.5, x1 + 0.5, z0 - 0.5, z1 + 0.5, s.color, 0, 0.04);
    g.material.blending = THREE.AdditiveBlending;
    const pts = [[x0, z0], [x1, z0], [x1, z1], [x0, z1], [x0, z0]].map(([x, z]) => new THREE.Vector3(x, 0.08, z));
    const border = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), new THREE.LineBasicMaterial({ color: s.color, transparent: true, opacity: 0 }));
    scene.add(border);
    glow[s.key] = { plane: g, border };
  });
  const yellow = new THREE.MeshBasicMaterial({ color: 0xd4a017, transparent: true, opacity: 0.6 });
  [[-48.5], [-47], [45.5], [47]].forEach(([z]) => {
    const s = new THREE.Mesh(new THREE.PlaneGeometry(HALL.x1 - HALL.x0 - 6, 0.22), yellow);
    s.rotation.x = -Math.PI / 2;
    s.position.set((HALL.x0 + HALL.x1) / 2, 0.05, z);
    scene.add(s);
  });
  [35, 57].forEach((x) => {
    const s = new THREE.Mesh(new THREE.PlaneGeometry(0.22, 92), yellow);
    s.rotation.x = -Math.PI / 2;
    s.position.set(x, 0.05, -2);
    scene.add(s);
  });

  // ── building shell: walls, columns, truss roof, LED high-bays ──────
  const shell = new THREE.Group();
  scene.add(shell);
  const roof = new THREE.Group();
  scene.add(roof);
  const wallMat = new THREE.MeshStandardMaterial({ color: 0x3b5278, transparent: true, opacity: 0.3, side: THREE.DoubleSide, depthWrite: false });
  const wallSolid = new THREE.MeshStandardMaterial({ color: 0x2b3d5c, roughness: 0.8 });
  const steel = new THREE.MeshStandardMaterial({ color: 0x8195b3, metalness: 0.65, roughness: 0.35 });
  const wallSeg = (x0, z0, x1, z1, h, mat, parent = shell) => {
    const len = Math.hypot(x1 - x0, z1 - z0);
    const m = new THREE.Mesh(new THREE.BoxGeometry(len, h, 0.4), mat);
    m.position.set((x0 + x1) / 2, h / 2, (z0 + z1) / 2);
    m.rotation.y = -Math.atan2(z1 - z0, x1 - x0);
    parent.add(m);
    return m;
  };
  const WH = 11;
  wallSeg(HALL.x0, HALL.z0, HALL.x1, HALL.z0, WH, wallMat);                    // north
  wallSeg(HALL.x0, HALL.z1, HALL.x1, HALL.z1, 1.2, wallSolid);                // south (cut away)
  // west wall with the two receiving doors
  let zc = HALL.z0;
  RECV_DOORS.forEach((dz) => { wallSeg(HALL.x0, zc, HALL.x0, dz - 5, WH, wallMat); zc = dz + 5; });
  wallSeg(HALL.x0, zc, HALL.x0, HALL.z1, WH, wallMat);
  // east wall with four dock doors
  zc = HALL.z0;
  Object.values(DOCKS).forEach((dz) => { wallSeg(HALL.x1, zc, HALL.x1, dz - 3.5, WH, wallMat); zc = dz + 3.5; });
  wallSeg(HALL.x1, zc, HALL.x1, HALL.z1, WH, wallMat);
  // door frames + roller doors half open
  const doorMat = new THREE.MeshStandardMaterial({ color: 0x9fb3cf, metalness: 0.5, roughness: 0.4 });
  const doorFrame = (x, z, w, h) => {
    [[-w / 2], [w / 2]].forEach(([dz]) => { const p = new THREE.Mesh(BOX, steel); p.scale.set(0.5, h, 0.5); p.position.set(x, h / 2, z + dz); shell.add(p); });
    const top = new THREE.Mesh(BOX, doorMat); top.scale.set(0.5, h * 0.35, w); top.position.set(x, h * 0.82, z); shell.add(top);
  };
  RECV_DOORS.forEach((z) => doorFrame(HALL.x0, z, 10, 7));
  Object.values(DOCKS).forEach((z) => doorFrame(HALL.x1, z, 7, 5.5));

  const colGeo = new THREE.BoxGeometry(0.6, HALL.h + 1.5, 0.6);
  const colPos = [];
  for (let x = HALL.x0 + 6; x < HALL.x1; x += 12) {
    colPos.push([x, HALL.z0 + 0.6], [x, HALL.z1 - 0.6]);
    if (((x - HALL.x0 - 6) / 12) % 2 === 0) colPos.push([x, -48], [x, 46]);
  }
  const cols = new THREE.InstancedMesh(colGeo, steel, colPos.length);
  colPos.forEach(([x, z], i) => { tmpM.makeTranslation(x, (HALL.h + 1.5) / 2, z); cols.setMatrixAt(i, tmpM); });
  cols.castShadow = true;
  shell.add(cols);

  // gable trusses spanning the hall width, purlins, translucent roof, LED high-bays
  const zMid = (HALL.z0 + HALL.z1) / 2;
  const eave = HALL.h;
  const apex = HALL.h + 4;
  const roofY = (z) => eave + (apex - eave) * (1 - Math.abs(z - zMid) / ((HALL.z1 - HALL.z0) / 2));
  const members = [];
  const member = (x, y0, z0, y1, z1, t = 0.32) => {
    const len = Math.hypot(y1 - y0, z1 - z0);
    members.push({ x, y: (y0 + y1) / 2, z: (z0 + z1) / 2, len, rx: -Math.atan2(y1 - y0, z1 - z0), t });
  };
  for (let x = HALL.x0 + 6; x < HALL.x1; x += 12) {
    member(x, eave - 0.6, HALL.z0, eave - 0.6, HALL.z1, 0.3);
    member(x, eave, HALL.z0, apex, zMid, 0.4);
    member(x, apex, zMid, eave, HALL.z1, 0.4);
    for (let z = HALL.z0 + 8; z < HALL.z1 - 4; z += 8) {
      member(x, eave - 0.6, z, roofY(z), z, 0.18);
      const zn = Math.min(HALL.z1, z + 8);
      member(x, eave - 0.6, z, roofY(zn), zn, 0.14);
    }
  }
  const truss = new THREE.InstancedMesh(BOX, steel, members.length);
  members.forEach((m, i) => {
    tmpM.compose(tmpV.set(m.x, m.y, m.z), tmpQ.setFromEuler(tmpE.set(m.rx, 0, 0)), tmpS.set(m.t, m.t, m.len));
    truss.setMatrixAt(i, tmpM);
  });
  roof.add(truss);
  const purlins = [];
  for (let z = HALL.z0 + 4; z <= HALL.z1 - 4; z += 10) purlins.push(z);
  const purl = new THREE.InstancedMesh(BOX, steel, purlins.length);
  purlins.forEach((z, i) => {
    tmpM.compose(tmpV.set((HALL.x0 + HALL.x1) / 2, roofY(z) + 0.3, z), tmpQ.identity(), tmpS.set(HALL.x1 - HALL.x0, 0.22, 0.22));
    purl.setMatrixAt(i, tmpM);
  });
  roof.add(purl);
  const roofMat = new THREE.MeshStandardMaterial({ color: 0x6b8cc2, transparent: true, opacity: 0.07, side: THREE.DoubleSide, depthWrite: false });
  [[HALL.z0, zMid], [zMid, HALL.z1]].forEach(([za, zb]) => {
    const len = Math.hypot(zb - za, apex - eave);
    const p = new THREE.Mesh(new THREE.PlaneGeometry(HALL.x1 - HALL.x0, len), roofMat);
    p.position.set((HALL.x0 + HALL.x1) / 2, (eave + apex) / 2 + 0.4, (za + zb) / 2);
    p.rotation.x = -Math.PI / 2 + (za < zMid ? -1 : 1) * Math.atan2(apex - eave, zb - za);
    roof.add(p);
  });
  const ledPos = [];
  for (let x = HALL.x0 + 12; x < HALL.x1 - 4; x += 12) for (let z = HALL.z0 + 6; z < HALL.z1 - 3; z += 9) ledPos.push([x, z]);
  // dark housing seen from above; the glowing lens faces the floor only (single-sided disc)
  const led = new THREE.InstancedMesh(new THREE.CylinderGeometry(0.55, 0.7, 0.28, 12),
    new THREE.MeshStandardMaterial({ color: 0x3b4352, metalness: 0.5, roughness: 0.5 }), ledPos.length);
  ledPos.forEach(([x, z], i) => { tmpM.makeTranslation(x, eave - 1.2, z); led.setMatrixAt(i, tmpM); });
  roof.add(led);
  const lensGeo = new THREE.CircleGeometry(0.62, 16);
  lensGeo.rotateX(Math.PI / 2); // normal points down
  const lens = new THREE.InstancedMesh(lensGeo,
    new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: 0xeaf4ff, emissiveIntensity: 2.2, side: THREE.FrontSide }), ledPos.length);
  ledPos.forEach(([x, z], i) => { tmpM.makeTranslation(x, eave - 1.36, z); lens.setMatrixAt(i, tmpM); });
  led.add(lens);

  // ── receiving & bale store (west) ──────────────────────────────────
  const baleMat = S('raw', 0xe9e4d8, { roughness: 0.95 });
  const balePos = [];
  for (const [za, zb] of [[-60, -27], [-6, 6], [22, 56]]) {
    for (let x = -129; x <= -105; x += 1.6) for (let z = za; z <= zb; z += 1.3) balePos.push([x, z]);
  }
  const bales = new THREE.InstancedMesh(new RoundedBoxGeometry(1.35, 1.05, 1.1, 2, 0.12), baleMat, balePos.length * 2);
  let bi = 0;
  balePos.forEach(([x, z]) => {
    [0.55, 1.65].forEach((y) => { tmpM.makeTranslation(x, y, z); bales.setMatrixAt(bi, tmpM); bi += 1; });
  });
  bales.castShadow = true;
  bales.receiveShadow = true;
  root.add(bales);
  const baleCap = bi;
  const shellBox = S('raw', 0x3f6fb5);
  for (let x = -128; x <= -116; x += 2.2) for (let z = 40; z <= 54; z += 2.2) bx(shellBox, 1.8, 1.4 + ((x * 7 + z) % 3) * 0.5, 1.8, x, 0.9, z);
  const forkMat = S('raw', 0xeab308);
  const forklift = (stage, mat, x, z, ry) => {
    bx(mat, 2.2, 1.4, 1.3, x, 0.9, z, ry);
    bx(S(stage, 0x1f2937), 0.2, 3.6, 1.1, x + Math.cos(ry) * 1.2, 1.8, z - Math.sin(ry) * 1.2, ry);
    bx(S(stage, 0x111827), 1.2, 1.2, 1.1, x - Math.cos(ry) * 0.3, 2.2, z + Math.sin(ry) * 0.3, ry);
  };
  forklift('raw', forkMat, -110, -15, 0);
  forklift('raw', forkMat, -112, 18, Math.PI);
  bx(S('raw', 0x64748b), 3, 0.9, 1.6, -103, 0.45, -52);   // sampling bench
  // receiving trucks (shown while unloading)
  const trailerMat = S('raw', 0xdbe4f0, { metalness: 0.4, roughness: 0.4 });
  const cabMat = S('raw', 0x1d4ed8);
  const recvTrucks = RECV_DOORS.map((z) => {
    const g = new THREE.Group();
    const t = new THREE.Mesh(BOX, trailerMat); t.scale.set(15, 4, 3); t.position.set(-143, 2.6, z); t.castShadow = true; g.add(t);
    const c = new THREE.Mesh(BOX, cabMat); c.scale.set(3.6, 3.2, 3); c.position.set(-152.6, 2.1, z); c.castShadow = true; g.add(c);
    scene.add(g);
    g.visible = false;
    return g;
  });

  // ── the pillow lines (one template × 8) ─────────────────────────────
  const lines0 = initial.lines || [];
  const pickMachines = [];
  const pickAreas = [];
  const lineObjs = [];
  const hitMat = new THREE.MeshBasicMaterial({ visible: false });
  const rollers = [];
  const bunMat = new THREE.MeshStandardMaterial({ color: 0xf8fafc, roughness: 1, transparent: true, opacity: 0.88, emissive: 0x64748b, emissiveIntensity: 0.25 });
  (stageMats.carding ||= []).push(bunMat);
  bunMat.userData = { base: bunMat.color.clone(), baseEm: bunMat.emissive.clone(), baseEI: bunMat.emissiveIntensity };

  const M = {
    feed: S('opening', 0x5c7a99), lattice: S('opening', 0x3b4b63), spike: S('opening', 0xcbd5e1, { metalness: 0.8, roughness: 0.3 }),
    binWall: S('opening', 0x7c94b8, { metalness: 0.5 }), fibre: S('opening', 0xf1f5f9, { roughness: 1, emissive: 0x334155, emissiveIntensity: 0.25 }),
    duct: S('opening', 0x9fb1c9, { metalness: 0.7, roughness: 0.3 }), motor: S('opening', 0x1e293b),
    cardFrame: S('carding', 0x55708f), roller: S('carding', 0xb8c4d6, { metalness: 0.75, roughness: 0.25 }), guard: S('carding', 0xf59e0b),
    chain: S('carding', 0x2c3a52), slat: S('carding', 0x9aa7bb, { metalness: 0.6 }), lapper: S('carding', 0x41608a), blowBody: S('carding', 0x7766a3),
    fillBody: S('filling', 0x2a9d8f), hopper: S('filling', 0x8be0d4, { transparent: true, opacity: 0.55 }), blower: S('filling', 0x1f6f66),
    nozzle: S('filling', 0xd1d5db, { metalness: 0.8, roughness: 0.25 }), shellTable: S('filling', 0x8a7c64), shellCloth: S('filling', 0x6d9be0),
    table: S('sewing', 0x9a8b70), sewHead: S('sewing', 0xe5e7eb, { metalness: 0.35 }), sewArm: S('sewing', 0x1f2937), thread: S('sewing', 0xec4899),
    pillowS: S('sewing', 0xf8fafc, { roughness: 0.9 }),
    bagTable: S('bagging', 0x9a8b70), roll: S('bagging', 0xbfdbfe, { transparent: true, opacity: 0.8 }), press: S('bagging', 0xd97706, { metalness: 0.45 }),
    platen: S('bagging', 0x94a3b8, { metalness: 0.7 }),
    conv: S('packing', 0x1e2735), rail: S('packing', 0x64748b, { metalness: 0.6 }), leg: S('packing', 0x475569),
    packTable: S('packing', 0x9a8b70), kraft: S('packing', 0xc49a5c), camPole: S('packing', 0x111827), cam: S('packing', 0x0f172a),
  };
  const fov = new THREE.MeshBasicMaterial({ color: 0x38bdf8, transparent: true, opacity: 0.14, side: THREE.DoubleSide, depthWrite: false });
  const spikesPerLine = [];

  lines0.forEach((ln, i) => {
    const z = lineZ(i);
    const mcBy = {};
    ln.machines.forEach((mc) => { mcBy[mc.stage] = mc; });
    const machineMesh = (stage, mcStage, geo, mat, x, y, zz, sx, sy, sz, rx = 0, rz = 0) => {
      const mc = mcBy[mcStage];
      const m = new THREE.Mesh(geo, uniqueMat(stage, mat.color.getHex(), { metalness: mat.metalness, roughness: mat.roughness }));
      m.scale.set(sx, sy, sz);
      m.rotation.set(rx, 0, rz);
      m.position.set(x, y, zz);
      m.castShadow = true;
      m.receiveShadow = true;
      m.userData = { kind: 'machine', id: mc?.id, code: mc?.code, name: mc?.name, lineId: ln.id, line: ln.name, stage };
      root.add(m);
      const edges = new THREE.LineSegments(new THREE.EdgesGeometry(geo, 30), edgeMat(stage));
      edges.visible = false;
      m.add(edges);
      (stageEdges[stage] ||= []).push(edges);
      pickMachines.push(m);
      return m;
    };
    // opening: bale feeder with spiked lattice, opener, flapper, fibre bin
    bx(M.feed, 3.2, 2.3, 2.9, X.feed, 1.15, z);
    put(BOX, M.lattice, X.feed + 1.9, 2.2, z, 0.25, 3.6, 2.6, 0, 0, -0.55);
    for (let r = 0; r < 5; r += 1) for (let c = 0; c < 4; c += 1) {
      spikesPerLine.push([X.feed + 1.55 + r * 0.38, 0.95 + r * 0.55, z - 0.95 + c * 0.63]);
    }
    bx(baleMat, 1.3, 1.05, 1.1, X.feed - 2.3, 0.55, z);
    const opener = machineMesh('opening', 'Mixing', BOX, M.feed, X.opener, 1.35, z, 3.8, 2.7, 3);
    put(CYL, M.roller, X.opener, 3.0, z, 1.1, 2.6, 1.1, Math.PI / 2, 0, 0);
    bx(M.motor, 1, 0.9, 0.9, X.opener - 1.4, 0.45, z + 1.9);
    const flapper = machineMesh('opening', 'Flapper Processing', BOX, M.feed, X.flapper, 1.15, z, 2.8, 2.3, 2.5);
    flapper.material.color.setHex(0x6b88b3);
    flapper.material.userData.base = flapper.material.color.clone();
    // open fibre bin (like the white bins in the plant footage)
    [[0, -1.45, 3.4, 0.12], [0, 1.45, 3.4, 0.12]].forEach(([dx, dz, w, t]) => bx(M.binWall, w, 2.1, t, X.bin + dx, 1.05, z + dz));
    [[-1.7], [1.7]].forEach(([dx]) => bx(M.binWall, 0.12, 2.1, 3, X.bin + dx, 1.05, z));
    bx(M.fibre, 3.2, 1.75, 2.8, X.bin, 1.0, z);
    put(new THREE.SphereGeometry(1, 10, 8), M.fibre, X.bin, 1.95, z, 1.4, 0.45, 1.2);
    // fibre transport duct: up from the flapper, overhead along the line, down into the filler
    put(CYL, M.duct, X.flapper, 4.6, z - 0.9, 0.7, 5, 0.7);
    put(CYL, M.duct, (X.flapper + X.fill) / 2, 7.1, z - 0.9, 0.7, X.fill - X.flapper, 0.7, 0, 0, Math.PI / 2);
    put(CYL, M.duct, X.fill, 5.6, z - 0.9, 0.6, 3, 0.6);

    // carding: chain conveyor, Garnett card with rollers (or blow unit + cyclone), cross-lapper
    put(BOX, M.chain, X.chain, 1.7, z, 0.4, 4.2, 2.6, 0, 0, -0.7);
    for (let k = 0; k < 6; k += 1) put(BOX, M.slat, X.chain - 1.3 + k * 0.5, 0.45 + k * 0.48, z, 0.1, 0.1, 2.7, 0, 0, 0);
    let card;
    if (ln.type === 'Blow') {
      card = machineMesh('carding', 'Bun Formation', CYL, M.blowBody, X.card - 2, 1.6, z, 3.2, 3.2, 3.2);
      put(CYL, M.duct, X.card + 4, 4.6, z, 1.6, 2.4, 1.6);
      put(CONE, M.duct, X.card + 4, 2.6, z, 3.2, 1.8, 3.2, Math.PI, 0, 0);
      put(CYL, M.duct, X.card + 1, 4.2, z, 0.6, 6, 0.6, 0, 0, Math.PI / 2);
      bx(M.cardFrame, 8, 1, 2.6, X.card + 6, 0.5, z);
    } else {
      card = machineMesh('carding', 'Bun Formation', BOX, M.cardFrame, X.card, 0.85, z, X.cardLen, 1.7, 3);
      for (let k = 0; k < 5; k += 1) {
        const xx = X.card - X.cardLen / 2 + 2 + k * 3.5;
        rollers.push({ x: xx, y: 2.45, z, r: 0.85, line: i });
        if (k < 4) rollers.push({ x: xx + 1.75, y: 2.05, z, r: 0.38, line: i });
      }
      put(BOX, M.guard, X.card, 3.5, z + 1.55, X.cardLen - 1, 0.08, 0.08);
      put(BOX, M.guard, X.card, 3.5, z - 1.55, X.cardLen - 1, 0.08, 0.08);
      bx(M.motor, 1.6, 1.4, 1.4, X.card - X.cardLen / 2 + 1, 0.7, z + 2.4);
    }
    // cross-lapper carriage
    bx(M.lapper, 3.2, 2.6, 3.6, X.lapper, 1.3, z);
    bx(M.slat, 3.4, 0.2, 3.8, X.lapper, 2.7, z);
    const bun = new THREE.Mesh(BOX, bunMat);
    bun.scale.set(7.5, 0.35, 2.2);
    bun.position.set((X.lapper + X.fill) / 2 - 1, 0.95, z);
    root.add(bun);

    // filling: machine body, hopper, blower, nozzle, shell table
    const filler = machineMesh('filling', 'Pillow Filling', BOX, M.fillBody, X.fill, 1.45, z, 2.8, 2.9, 2.8);
    put(CONE, M.hopper, X.fill, 3.9, z, 2.8, 2, 2.8, Math.PI, 0, 0);
    put(CYL, M.blower, X.fill - 0.6, 1.4, z + 1.9, 1.6, 1.0, 1.6, Math.PI / 2, 0, 0);
    put(CYL, M.nozzle, X.fill + 2.1, 1.25, z, 0.32, 1.6, 0.32, 0, 0, Math.PI / 2);
    bx(M.shellTable, 2.3, 0.9, 2, X.shell, 0.45, z);
    put(new RoundedBoxGeometry(1.6, 0.35, 1.0, 2, 0.15), M.shellCloth, X.shell, 1.1, z);

    // sewing: buffer + two sewing tables with sewing heads
    bx(M.table, 2.2, 0.9, 1.8, X.sewBuf, 0.45, z);
    put(new RoundedBoxGeometry(1.4, 0.45, 0.9, 2, 0.18), M.pillowS, X.sewBuf, 1.15, z);
    put(new RoundedBoxGeometry(1.4, 0.45, 0.9, 2, 0.18), M.pillowS, X.sewBuf + 0.1, 1.6, z + 0.1);
    [X.sew1, X.sew2].forEach((x) => {
      bx(M.table, 3.2, 0.9, 1.8, x, 0.45, z);
      bx(M.sewHead, 1.1, 0.35, 0.5, x - 0.4, 1.12, z + 0.35);
      bx(M.sewArm, 0.3, 0.6, 0.45, x - 0.85, 1.25, z + 0.35);
      bx(M.sewHead, 0.9, 0.28, 0.42, x - 0.5, 1.55, z + 0.35);
      put(CYL, M.thread, x - 0.1, 1.95, z + 0.45, 0.16, 0.34, 0.16);
      put(new RoundedBoxGeometry(1.4, 0.45, 0.9, 2, 0.18), M.pillowS, x + 0.6, 1.15, z - 0.3);
    });

    // bagging & compression: bag roll + compression press frame
    bx(M.bagTable, 2.2, 0.9, 1.8, X.bagBuf, 0.45, z);
    put(CYL, M.roll, X.bagBuf, 1.3, z, 0.7, 1.6, 0.7, Math.PI / 2, 0, 0);
    [[-1.1, -1.0], [1.1, -1.0], [-1.1, 1.0], [1.1, 1.0]].forEach(([dx, dz]) => bx(M.press, 0.3, 3.6, 0.3, X.press + dx, 1.8, z + dz));
    bx(M.press, 2.6, 0.5, 2.4, X.press, 3.7, z);
    bx(M.press, 2.6, 0.6, 2.4, X.press, 0.3, z);
    const platen = new THREE.Mesh(BOX, M.platen);
    platen.scale.set(2.1, 0.3, 2.0);
    platen.position.set(X.press, 2.6, z);
    root.add(platen);

    // outfeed conveyor + CV packing station
    const cl = X.conv1 - X.conv0;
    bx(M.conv, cl, 0.25, 1.3, (X.conv0 + X.conv1) / 2, 0.9, z);
    [-0.72, 0.72].forEach((dz) => bx(M.rail, cl, 0.2, 0.1, (X.conv0 + X.conv1) / 2, 1.08, z + dz));
    for (let x = X.conv0 + 1; x < X.conv1; x += 3.5) [-0.55, 0.55].forEach((dz) => bx(M.leg, 0.12, 0.8, 0.12, x, 0.4, z + dz));
    bx(M.packTable, 4.4, 0.9, 3.2, X.pack, 0.45, z);
    bx(M.kraft, 1.4, 1.2, 1.2, X.pack + 0.6, 1.5, z - 0.6);
    [0, 1, 2].forEach((k) => bx(M.kraft, 1.2, 0.9, 1.1, X.pack + 2.9, 0.45 + k * 0.92, z + 1.0));
    bx(M.camPole, 0.18, 4.8, 0.18, X.cam, 2.4, z + 1.9);
    bx(M.camPole, 2.2, 0.12, 0.12, X.cam + 1.1, 4.75, z + 1.9);
    bx(M.cam, 0.6, 0.4, 0.4, X.cam + 2.1, 4.55, z + 1.9);
    const cone = new THREE.Mesh(new THREE.ConeGeometry(1.6, 3.7, 4, 1, true), fov);
    cone.position.set(X.cam + 2.3, 2.75, z + 0.6);
    cone.rotation.y = Math.PI / 4;
    root.add(cone);

    // status strip, beacon, hit boxes, label
    const stripMat = new THREE.MeshBasicMaterial({ color: STATUS_COLOR[ln.status] || 0x64748b, transparent: true, opacity: 0.9 });
    const strip = new THREE.Mesh(new THREE.PlaneGeometry(X.conv1 - X.feed + 4, 0.4), stripMat);
    strip.rotation.x = -Math.PI / 2;
    strip.position.set((X.feed + X.conv1) / 2 - 2, 0.07, z + 3.3);
    root.add(strip);
    const beacon = new THREE.Mesh(new THREE.CylinderGeometry(0.45, 0.45, 0.9, 16),
      new THREE.MeshStandardMaterial({ color: 0xef4444, emissive: 0xef4444, emissiveIntensity: 1.6 }));
    beacon.position.set(X.card, 4.6, z);
    beacon.visible = false;
    root.add(beacon);
    STAGES.filter((s) => ['opening', 'carding', 'filling', 'sewing', 'bagging', 'packing'].includes(s.key)).forEach((s) => {
      const [x0, x1] = s.region;
      const hb = new THREE.Mesh(BOX, hitMat);
      hb.scale.set(x1 - x0, 5, 6.5);
      hb.position.set((x0 + x1) / 2, 2.5, z);
      hb.userData = { kind: 'line', lineId: ln.id, line: ln.name, stage: s.key };
      root.add(hb);
      pickAreas.push(hb);
    });
    const lab = el('', 'ftw-line', () => cb.onOpenLine?.(ln.id));
    lab.center.set(1, 0.5);
    lab.position.set(X.feed - 3.6, 0.6, z);
    root.add(lab);
    lineObjs.push({ id: ln.id, z, stripMat, beacon, lab, opener, flapper, card, filler, platen, type: ln.type, running: false, rate: 0 });
  });
  const spikes = new THREE.InstancedMesh(new THREE.ConeGeometry(0.08, 0.32, 6), M.spike, spikesPerLine.length);
  spikesPerLine.forEach(([x, y, z], i) => {
    tmpM.compose(tmpV.set(x, y, z), tmpQ.setFromEuler(tmpE.set(0, 0, -0.55 - Math.PI / 2)), tmpS.set(1, 1, 1));
    spikes.setMatrixAt(i, tmpM);
  });
  root.add(spikes);
  const rollerMesh = new THREE.InstancedMesh(CYL, M.roller, Math.max(1, rollers.length));
  rollerMesh.castShadow = true;
  root.add(rollerMesh);
  const setRollers = (t) => {
    rollers.forEach((r, k) => {
      const lo = lineObjs[r.line];
      const ang = lo?.running ? t * (r.r > 0.5 ? 2.2 : 4.4) : 0;
      tmpM.compose(tmpV.set(r.x, r.y, r.z), tmpQ.setFromEuler(tmpE.set(Math.PI / 2, ang, 0)), tmpS.set(r.r * 2, 2.8, r.r * 2));
      rollerMesh.setMatrixAt(k, tmpM);
    });
    rollerMesh.instanceMatrix.needsUpdate = true;
  };
  setRollers(0);

  // pillows riding the outfeed conveyors
  const PILLOWS_PER_LINE = 6;
  const pillowMat = S('packing', 0xf8fafc, { roughness: 0.85 });
  stageOfMat.delete(pillowMat);
  const pillows = new THREE.InstancedMesh(new RoundedBoxGeometry(1.25, 0.42, 0.85, 2, 0.16), pillowMat, Math.max(1, lineObjs.length * PILLOWS_PER_LINE));
  pillows.castShadow = true;
  root.add(pillows);

  // ── QA hold cage ───────────────────────────────────────────────────
  const qa = STAGE_BY_KEY.qa.region;
  const cageMat = new THREE.MeshBasicMaterial({ color: 0xef4444, transparent: true, opacity: 0.22, side: THREE.DoubleSide, depthWrite: false });
  (stageMats.qa ||= []).push(cageMat);
  cageMat.userData = { base: cageMat.color.clone(), baseEm: new THREE.Color(0), baseEI: 0 };
  const qaPost = S('qa', 0xb91c1c, { metalness: 0.5 });
  const [qx0, qx1, qz0, qz1] = [qa[0] + 1, qa[1] - 1, qa[2] + 2, qa[3] - 1];
  [[qx0, qz0], [qx1, qz0], [qx0, qz1], [qx1, qz1]].forEach(([x, z]) => bx(qaPost, 0.25, 3, 0.25, x, 1.5, z));
  [[qx0, qz0, qx1, qz0], [qx0, qz1, qx1, qz1], [qx0, qz0, qx0, qz1], [qx1, qz0, qx1, qz1]].forEach(([a, b, c, d]) => {
    const len = Math.hypot(c - a, d - b);
    const p = new THREE.Mesh(new THREE.PlaneGeometry(len, 3), cageMat);
    p.position.set((a + c) / 2, 1.5, (b + d) / 2);
    p.rotation.y = -Math.atan2(d - b, c - a);
    root.add(p);
  });
  const blockedMat = S('qa', 0xb4473b, { emissive: 0x5a1010, emissiveIntensity: 0.4 });
  const blocked = new THREE.InstancedMesh(BOX, blockedMat, 24);
  for (let k = 0; k < 24; k += 1) {
    tmpM.compose(tmpV.set(qx0 + 1.5 + (k % 6) * 1.6, 0.5 + Math.floor(k / 12) * 1.0, qz0 + 1.5 + (Math.floor(k / 6) % 2) * 2.2), tmpQ.identity(), tmpS.set(1.3, 0.9, 1.1));
    blocked.setMatrixAt(k, tmpM);
  }
  blocked.count = 0;
  root.add(blocked);
  bx(S('qa', 0x64748b), 3, 0.9, 1.5, qx1 - 2.2, 0.45, qz1 - 1.2);   // recount bench

  // ── palletizing & stretch wrap ─────────────────────────────────────
  const palletMat = S('pallet', 0x8b6a43);
  const loadMat = S('pallet', 0xc49a5c);
  const wrapMat = S('pallet', 0xdbeafe, { transparent: true, opacity: 0.55 });
  const cellZ = [0, 1, 2, 3].map((p) => (lineZ(p * 2) + lineZ(p * 2 + 1)) / 2);
  const palletStations = cellZ.map((z) => {
    bx(palletMat, 2.4, 0.3, 2.0, X.pal, 0.15, z);
    const load = new THREE.Mesh(BOX, loadMat);
    load.position.set(X.pal, 0.3, z);
    load.castShadow = true;
    root.add(load);
    return load;
  });
  cellZ.forEach((z) => {
    put(CYL, S('pallet', 0x334155), X.wrap, 0.15, z, 3.2, 0.3, 3.2);
    bx(S('pallet', 0x475569), 0.4, 4.2, 0.4, X.wrap + 1.9, 2.1, z);
    put(CYL, wrapMat, X.wrap + 1.6, 2.1, z, 0.4, 1.3, 0.4);
    bx(palletMat, 2.2, 0.3, 1.9, X.wrap, 0.45, z);
    bx(loadMat, 2.0, 1.6, 1.7, X.wrap, 1.4, z);
  });
  forklift('pallet', S('pallet', 0xeab308), 54, -2, Math.PI / 2);

  // ── high-bay finished-goods racks ──────────────────────────────────
  const upright = S('fg', 0x2f6fd6, { metalness: 0.5 });
  const beam = S('fg', 0xe07b24, { metalness: 0.45 });
  const slotPos = {};
  Object.entries(RACK_X).forEach(([zone, x]) => {
    const n = RACK.bays * RACK.racks;
    for (let k = 0; k <= n; k += 1) {
      const z = RACK.z0 + k * RACK.module;
      [-1.4, 1.4].forEach((dx) => bx(upright, 0.22, RACK.levels * RACK.levelH + 0.8, 0.22, x + dx, (RACK.levels * RACK.levelH + 0.8) / 2, z));
    }
    for (let k = 0; k < n; k += 1) {
      const z = RACK.z0 + k * RACK.module + RACK.module / 2;
      for (let lv = 1; lv <= RACK.levels; lv += 1) {
        [-1.4, 1.4].forEach((dx) => bx(beam, 0.14, 0.18, RACK.module - 0.2, x + dx, (lv - 1) * RACK.levelH + 0.1, z));
        const bay = Math.floor(k / RACK.racks) + 1;
        const rk = (k % RACK.racks) + 1;
        slotPos[`${zone}-${bay}-${rk}-${lv}`] = [x, (lv - 1) * RACK.levelH + 0.2, z];
      }
    }
  });
  const slotKeys = Object.keys(slotPos);
  const rackPallets = new THREE.InstancedMesh(BOX, loadMat, slotKeys.length * 2);
  const rackBases = new THREE.InstancedMesh(BOX, palletMat, slotKeys.length * 2);
  rackPallets.castShadow = true;
  root.add(rackPallets, rackBases);
  forklift('fg', S('fg', 0xeab308), 72, 14, Math.PI / 2);
  forklift('fg', S('fg', 0xeab308), 96, -24, -Math.PI / 2);

  // ── dispatch docks ──────────────────────────────────────────────────
  const staged = S('dispatch', 0xc49a5c);
  const dockObjs = {};
  Object.entries(DOCKS).forEach(([dock, z]) => {
    floorPlane(112, 128.5, z - 3.2, z + 3.2, 0x22c55e, 0.08, 0.05);
    bx(S('dispatch', 0x475569), 2.2, 1.2, 6.2, HALL.x1 - 1.2, 0.6, z);    // dock leveler
    const pallets = new THREE.InstancedMesh(BOX, staged, 8);
    for (let k = 0; k < 8; k += 1) {
      tmpM.compose(tmpV.set(114.5 + (k % 4) * 3.2, 0.95, z + (k < 4 ? -1.4 : 1.4)), tmpQ.identity(), tmpS.set(2.2, 1.6, 1.9));
      pallets.setMatrixAt(k, tmpM);
    }
    pallets.count = 0;
    pallets.castShadow = true;
    root.add(pallets);
    const truck = new THREE.Group();
    const tr = new THREE.Mesh(BOX, S('dispatch', 0xe2e8f0, { metalness: 0.4, roughness: 0.4 }));
    tr.scale.set(15, 4, 3); tr.position.set(HALL.x1 + 9, 2.6, z); tr.castShadow = true; truck.add(tr);
    const cab = new THREE.Mesh(BOX, S('dispatch', 0x15803d));
    cab.scale.set(3.6, 3.2, 3); cab.position.set(HALL.x1 + 18.6, 2.1, z); cab.castShadow = true; truck.add(cab);
    truck.visible = false;
    scene.add(truck);
    const lab = el('', 'ftw-dock');
    lab.position.set(HALL.x1 + 2.5, 6.2, z);
    scene.add(lab);
    dockObjs[dock] = { pallets, truck, lab };
  });

  // ── utilities annex, maintenance crib, amenities ───────────────────
  const U = SUPPORT.find((s) => s.key === 'utilities').region;
  const annexFloor = new THREE.Mesh(new THREE.PlaneGeometry(U[1] - U[0], U[3] - U[2]), new THREE.MeshStandardMaterial({ color: 0x1f2a3d, roughness: 0.9 }));
  annexFloor.rotation.x = -Math.PI / 2;
  annexFloor.position.set((U[0] + U[1]) / 2, 0.01, (U[2] + U[3]) / 2);
  scene.add(annexFloor);
  wallSeg(U[0], U[2], U[1], U[2], 6, wallMat);
  wallSeg(U[0], U[2], U[0], U[3], 6, wallMat);
  wallSeg(U[1], U[2], U[1], U[3], 6, wallMat);
  const comp = S('utilities', 0x3b82f6);
  [0, 1, 2].forEach((k) => {
    bx(comp, 4, 2.4, 2.6, U[0] + 5 + k * 5.2, 1.2, U[2] + 4);
    put(CYL, S('utilities', 0x1e293b), U[0] + 5 + k * 5.2, 2.5, U[2] + 4, 1.4, 0.2, 1.4);
  });
  [0, 1].forEach((k) => put(CYL, S('utilities', 0x94a3b8, { metalness: 0.7 }), U[0] + 6 + k * 3.4, 3, U[2] + 11, 2.2, 6, 2.2));
  bx(S('utilities', 0x64748b), 6, 7, 5, U[1] - 10, 6.5, U[2] + 8);        // bag-house
  put(CONE, S('utilities', 0x64748b), U[1] - 10, 2, U[2] + 8, 5.5, 3, 5.5, Math.PI, 0, 0);
  [[-2.4, -2], [2.4, -2], [-2.4, 2], [2.4, 2]].forEach(([dx, dz]) => bx(steel, 0.3, 3.4, 0.3, U[1] - 10 + dx, 1.7, U[2] + 8 + dz));
  put(CYL, S('utilities', 0xa5b4cb, { metalness: 0.6 }), U[1] - 3.5, 7, U[2] + 13, 2.4, 4, 2.4);
  put(CONE, S('utilities', 0xa5b4cb, { metalness: 0.6 }), U[1] - 3.5, 4, U[2] + 13, 2.4, 2.2, 2.4, Math.PI, 0, 0);
  // main dust / fibre header from the annex into the hall, feeding every line
  put(CYL, M.duct, -92, 9.2, (U[3] + 40) / 2 - 6, 1.1, 40 - U[3] + 12, 1.1, Math.PI / 2, 0, 0);
  put(CYL, M.duct, U[1] - 3.5, 9.2, (U[2] + 13 + U[3]) / 2 + 2, 0.9, 8, 0.9, Math.PI / 2, 0, 0);
  const mc = SUPPORT.find((s) => s.key === 'maint').region;
  [0, 1, 2].forEach((k) => bx(S('maint', 0x7a4b1e), 5, 2.4, 1.1, mc[0] + 3.5 + k * 5.6, 1.2, mc[2] + 1.4));
  bx(S('maint', 0x52525b), 4.5, 0.95, 1.8, mc[0] + 9, 0.48, mc[3] - 3);
  const am = SUPPORT.find((s) => s.key === 'amenities').region;
  const roomWall = S('amenities', 0x30476b);
  for (let x = am[0]; x <= am[1]; x += 22) bx(roomWall, 0.25, 2.4, am[3] - am[2], x, 1.2, (am[2] + am[3]) / 2);
  bx(roomWall, am[1] - am[0], 2.4, 0.25, (am[0] + am[1]) / 2, 1.2, am[3]);
  const tbl = S('amenities', 0x4a5d7a);
  for (let x = am[0] + 4; x < am[0] + 40; x += 5) for (let z = am[2] + 3; z < am[3] - 2; z += 3.6) bx(tbl, 2.8, 0.75, 1.3, x, 0.38, z);
  for (let k = 0; k < 10; k += 1) bx(S('amenities', 0x5b6b86), 0.9, 2, 0.6, am[0] + 48 + k * 1.1, 1, am[2] + 1);

  flushStatic(root, stageOfMat);

  // ── department markers (beam + bobbing sphere + ring + label) ──────
  const markers = new THREE.Group();
  scene.add(markers);
  const markerObjs = {};
  const HEIGHTS = [17, 24, 31];
  const markerPos = {};
  [...STAGES, ...SUPPORT].forEach((s, idx) => {
    const [x0, x1, z0, z1] = s.region;
    const isLine = ['opening', 'carding', 'filling', 'sewing', 'bagging', 'packing'].includes(s.key);
    const x = (x0 + x1) / 2;
    const z = isLine ? -1.5 : (z0 + z1) / 2;
    const support = !STAGE_BY_KEY[s.key];
    const y = s.key === 'utilities' ? 16 : (support ? 9 : HEIGHTS[idx % 3]);
    markerPos[s.key] = new THREE.Vector3(x, y, z);
    const c = new THREE.Color(s.color);
    const g = new THREE.Group();
    const beamM = new THREE.Mesh(new THREE.CylinderGeometry(0.16, 0.16, y, 8),
      new THREE.MeshStandardMaterial({ color: c, emissive: c, emissiveIntensity: 0.6, transparent: true, opacity: 0.5 }));
    beamM.position.set(x, y / 2, z);
    const sphere = new THREE.Mesh(new THREE.SphereGeometry(0.95, 18, 14),
      new THREE.MeshStandardMaterial({ color: c, emissive: c, emissiveIntensity: 1.6 }));
    sphere.position.set(x, y, z);
    const ring = new THREE.Mesh(new THREE.RingGeometry(1.5, 1.85, 40),
      new THREE.MeshBasicMaterial({ color: c, transparent: true, opacity: 0.45, side: THREE.DoubleSide }));
    ring.position.set(x, y, z);
    ring.rotation.x = Math.PI / 2;
    const foot = new THREE.Mesh(new THREE.RingGeometry(2, 2.4, 40),
      new THREE.MeshBasicMaterial({ color: c, transparent: true, opacity: 0.5, side: THREE.DoubleSide }));
    foot.position.set(x, 0.09, z);
    foot.rotation.x = -Math.PI / 2;
    g.add(beamM, sphere, ring, foot);
    const n = STAGES.findIndex((t) => t.key === s.key);
    const lab = el(`<span class="ftw-marker-num">${n >= 0 ? n + 1 : '•'}</span><span class="ftw-marker-name">${s.name}</span>`
      + `<span class="ftw-marker-desc">${s.desc}</span>`, `ftw-marker ${n < 0 ? 'is-support' : ''}`, n >= 0 ? () => cb.onSelectStage?.(s.key) : null);
    lab.element.style.setProperty('--c', s.color);
    lab.position.set(x, y + 2.1, z);
    g.add(lab);
    markers.add(g);
    markerObjs[s.key] = { g, sphere, ring, beam: beamM, lab, y, phase: idx * 0.7 };
  });

  // ── process-flow arrows between consecutive stages ─────────────────
  const flows = new THREE.Group();
  scene.add(flows);
  const flowArrows = [];
  for (let k = 0; k < STAGES.length - 1; k += 1) {
    const a = markerPos[STAGES[k].key].clone();
    const b = markerPos[STAGES[k + 1].key].clone();
    a.y -= 0.2; b.y -= 0.2;
    const dir = b.clone().sub(a);
    const len = dir.length();
    dir.normalize();
    const from = a.clone().addScaledVector(dir, 2.2);
    const to = b.clone().addScaledVector(dir, -2.4);
    const c = new THREE.Color(STAGES[k + 1].color);
    const mm = new THREE.MeshStandardMaterial({ color: c, emissive: c, emissiveIntensity: 0.7, transparent: true, opacity: 0.6 });
    const q = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), dir);
    const shaft = new THREE.Mesh(new THREE.CylinderGeometry(0.13, 0.13, Math.max(0.1, len - 4.6), 8), mm);
    shaft.position.copy(from.clone().add(to).multiplyScalar(0.5));
    shaft.quaternion.copy(q);
    const head = new THREE.Mesh(new THREE.ConeGeometry(0.5, 1.4, 12), mm);
    head.position.copy(to);
    head.quaternion.copy(q);
    const dots = [0, 1, 2].map(() => {
      const d = new THREE.Mesh(new THREE.SphereGeometry(0.3, 10, 8), new THREE.MeshBasicMaterial({ color: c }));
      flows.add(d);
      return d;
    });
    flows.add(shaft, head);
    flowArrows.push({ mm, from, to, dots, k });
  }

  // ── walkthrough token ──────────────────────────────────────────────
  const token = new THREE.Group();
  const tokFibre = new THREE.Mesh(new THREE.IcosahedronGeometry(0.9, 1), new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: 0x7dd3fc, emissiveIntensity: 0.5, roughness: 1, flatShading: true }));
  const tokPillow = new THREE.Mesh(new RoundedBoxGeometry(1.7, 0.55, 1.1, 3, 0.22), new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: 0x38bdf8, emissiveIntensity: 0.35, roughness: 0.8 }));
  const tokBag = new THREE.Mesh(new RoundedBoxGeometry(1.5, 0.38, 1.0, 3, 0.15), new THREE.MeshStandardMaterial({ color: 0xbfdbfe, emissive: 0x38bdf8, emissiveIntensity: 0.4, transparent: true, opacity: 0.9 }));
  const tokCarton = new THREE.Mesh(new THREE.BoxGeometry(1.3, 1.0, 1.0), new THREE.MeshStandardMaterial({ color: 0xc49a5c, emissive: 0xf59e0b, emissiveIntensity: 0.3 }));
  const tokPallet = new THREE.Group();
  const tp1 = new THREE.Mesh(new THREE.BoxGeometry(2.2, 0.3, 1.9), new THREE.MeshStandardMaterial({ color: 0x8b6a43 }));
  const tp2 = new THREE.Mesh(new THREE.BoxGeometry(2.0, 1.5, 1.7), new THREE.MeshStandardMaterial({ color: 0xc49a5c, emissive: 0xf59e0b, emissiveIntensity: 0.3 }));
  tp2.position.y = 0.9;
  tokPallet.add(tp1, tp2);
  const tokBeam = new THREE.Mesh(new THREE.CylinderGeometry(0.12, 0.12, 14, 8), new THREE.MeshBasicMaterial({ color: 0x7dd3fc, transparent: true, opacity: 0.55 }));
  tokBeam.position.y = 7.5;
  const tokRing = new THREE.Mesh(new THREE.RingGeometry(1.6, 2.1, 40), new THREE.MeshBasicMaterial({ color: 0x7dd3fc, transparent: true, opacity: 0.8, side: THREE.DoubleSide }));
  tokRing.rotation.x = -Math.PI / 2;
  token.add(tokFibre, tokPillow, tokBag, tokCarton, tokPallet, tokBeam, tokRing);
  token.visible = false;
  scene.add(token);
  const tokenLabel = el('', 'ftw-token');
  tokenLabel.position.set(0, 3.6, 0);
  tokenLabel.visible = false;
  token.add(tokenLabel);
  function waypoint(key, zl, li) {
    const cz = cellZ[Math.min(3, Math.floor(li / 2))];
    switch (key) {
      case 'raw': return new THREE.Vector3(-116, 2.6, zl);
      case 'opening': return new THREE.Vector3(X.bin, 3.0, zl);
      case 'carding': return new THREE.Vector3(X.card + 2, 3.6, zl);
      case 'filling': return new THREE.Vector3(X.shell, 1.9, zl);
      case 'sewing': return new THREE.Vector3(X.sew2 + 0.6, 1.7, zl);
      case 'bagging': return new THREE.Vector3(X.press, 1.3, zl);
      case 'packing': return new THREE.Vector3(X.pack, 1.4, zl);
      case 'qa': return new THREE.Vector3(X.pack + 5, 1.4, zl);
      case 'pallet': return new THREE.Vector3(X.pal, 0.5, cz);
      case 'fg': return new THREE.Vector3(RACK_X.B, 2.4, -6);
      case 'dispatch': return new THREE.Vector3(HALL.x1 + 4, 1.5, DOCKS.D2);
      default: return new THREE.Vector3();
    }
  }
  const TOKEN_FORM = { raw: tokFibre, opening: tokFibre, carding: tokFibre, filling: tokPillow, sewing: tokPillow, bagging: tokBag, packing: tokCarton, qa: tokCarton, pallet: tokPallet, fg: tokPallet, dispatch: tokPallet };
  const TOKEN_TEXT = { raw: 'Fibre bale', opening: 'Opened fibre', carding: 'Fibre bun', filling: 'Filled shell', sewing: 'Sewn pillow', bagging: 'Bagged & compressed', packing: 'In carton · CV count ✓', qa: 'Count verified', pallet: 'On pallet', fg: 'Racked', dispatch: 'Loading on truck' };
  let tokenState = null;

  // ── state ──────────────────────────────────────────────────────────
  let focus = null;
  let flyTween = null;
  const layers = { roof: true, markers: true, flow: true, labels: true };
  const lineIdx = Object.fromEntries(lineObjs.map((lo, i) => [lo.id, i]));

  function update(d) {
    (d.lines || []).forEach((ln) => {
      const lo = lineObjs[lineIdx[ln.id]];
      if (!lo) return;
      lo.running = ln.status === 'Running';
      lo.rate = ln.rate_target || 150;
      lo.stripMat.color.setHex(STATUS_COLOR[ln.status] || 0x64748b);
      lo.beacon.visible = ln.status === 'Breakdown' || ln.status === 'Maintenance';
      lo.beacon.material.color.setHex(ln.status === 'Breakdown' ? 0xef4444 : 0xf97316);
      lo.beacon.material.emissive.setHex(ln.status === 'Breakdown' ? 0xef4444 : 0xf97316);
      const col = `#${(STATUS_COLOR[ln.status] || 0x64748b).toString(16).padStart(6, '0')}`;
      lo.lab.element.innerHTML = `<i style="background:${col}"></i><b>${ln.name}</b>`
        + `<span class="ftw-line-sub">${ln.status.toUpperCase()} · ${fmt(ln.output_today)} pillows · ${ln.efficiency_pct ?? '—'}% eff</span>`;
      const byCode = Object.fromEntries(ln.machines.map((m) => [m.code, m]));
      [lo.opener, lo.flapper, lo.card, lo.filler].forEach((mesh) => {
        const m = byCode[mesh.userData.code];
        if (!m) return;
        mesh.userData.health = m.health;
        mesh.userData.healthStatus = m.health_status;
        mesh.userData.status = m.status;
        mesh.material.userData.alert = m.status === 'Breakdown' || m.health_status === 'Critical' ? 'critical'
          : (m.health_status === 'Warning' ? 'warning' : null);
      });
    });
    // raw stock → bales on the floor
    const want = Math.min(baleCap, Math.round((d.raw?.fibre_kg || 0) / 220));
    bales.count = Math.max(40, want);
    recvTrucks.forEach((t, k) => { t.visible = !!(d.receiving || [])[k]; });
    // QA cage
    blocked.count = Math.min(24, d.packing?.blocked_cartons || 0);
    // palletizing loads
    const open = d.palletizing?.open_pallets || 0;
    palletStations.forEach((load, k) => {
      const h = open > k ? 0.6 + ((open * 7 + k * 3) % 5) * 0.3 : 0.01;
      load.scale.set(2.1, h, 1.8);
      load.position.y = 0.3 + h / 2;
    });
    // rack occupancy
    let n = 0;
    Object.entries(d.warehouse?.racks || {}).forEach(([zone, slots]) => {
      slots.forEach(([bay, rk, lv]) => {
        const p = slotPos[`${zone}-${bay}-${rk}-${lv}`];
        if (!p) return;
        [-1.65, 1.65].forEach((dz) => {
          tmpM.compose(tmpV.set(p[0], p[1] + 1.0, p[2] + dz), tmpQ.identity(), tmpS.set(2.2, 1.5, 2.9));
          rackPallets.setMatrixAt(n, tmpM);
          tmpM.compose(tmpV.set(p[0], p[1] + 0.12, p[2] + dz), tmpQ.identity(), tmpS.set(2.3, 0.22, 3.0));
          rackBases.setMatrixAt(n, tmpM);
          n += 1;
        });
      });
    });
    rackPallets.count = n;
    rackBases.count = n;
    rackPallets.instanceMatrix.needsUpdate = true;
    rackBases.instanceMatrix.needsUpdate = true;
    // docks
    (d.docks || []).forEach((dk) => {
      const o = dockObjs[dk.dock];
      if (!o) return;
      o.truck.visible = !!dk.truck;
      o.pallets.count = dk.truck ? Math.min(8, Math.max(1, Math.round(((dk.order_qty || 0) - (dk.loaded_qty || 0)) / 400))) : 0;
      o.lab.element.innerHTML = dk.truck
        ? `<b>${dk.dock}</b> ${dk.customer || ''}<span>${dk.status}${dk.loaded_pct != null ? ` · ${Math.round(dk.loaded_pct)}% loaded` : ''}</span>`
        : `<b>${dk.dock}</b> free<span>${dk.next ? `next ${dk.next.customer} ${String(dk.next.at || '').slice(11, 16)}` : 'no appointment'}</span>`;
      o.lab.element.classList.toggle('is-busy', !!dk.truck);
    });
  }
  update(initial);

  function applyFocus() {
    Object.entries(stageMats).forEach(([key, mats]) => {
      const st = STAGE_BY_KEY[key];
      const on = focus === key;
      const dim = focus && !on;
      mats.forEach((m) => {
        if (!m.userData.base) return;
        m.color.copy(m.userData.base);
        if (dim) m.color.lerp(new THREE.Color(0x0b1220), 0.72);
        if (m.emissive) {
          m.emissive.copy(on && st ? new THREE.Color(st.color) : m.userData.baseEm);
          m.emissiveIntensity = on ? 0.32 : m.userData.baseEI;
        }
      });
    });
    Object.entries(stageEdges).forEach(([key, list]) => list.forEach((e) => { e.visible = focus === key; }));
    Object.entries(glow).forEach(([key, g]) => {
      g.border.material.opacity = focus === key ? 0.95 : 0;
      if (focus !== key) g.plane.material.opacity = 0;
    });
    Object.entries(markerObjs).forEach(([key, o]) => {
      o.lab.element.classList.toggle('is-active', focus === key);
      o.lab.element.classList.toggle('is-dim', !!focus && focus !== key);
      const s = focus === key ? 1.6 : 1;
      o.sphere.scale.setScalar(s);
      o.ring.scale.setScalar(s);
    });
    lineObjs.forEach((lo) => lo.lab.element.classList.toggle('is-dim', !!focus && !['opening', 'carding', 'filling', 'sewing', 'bagging', 'packing'].includes(focus)));
  }

  function fly(pos, target, dur = 1300) {
    flyTween = { p0: camera.position.clone(), t0: controls.target.clone(), p1: pos.clone(), t1: target.clone(), start: performance.now(), dur };
  }
  function setCamera(preset) {
    const p = PRESETS[preset] || PRESETS[home];
    fly(new THREE.Vector3(...p.pos), new THREE.Vector3(...p.target));
  }
  function stageView(key) {
    const s = STAGE_BY_KEY[key] || SUPPORT.find((t) => t.key === key);
    if (!s) return null;
    const [x0, x1, z0, z1] = s.region;
    const t = new THREE.Vector3((x0 + x1) / 2, 1, (z0 + z1) / 2);
    const size = Math.max(x1 - x0, (z1 - z0) * 0.6);
    const dist = Math.min(240, size * 1.25 + 46);
    return { target: t, pos: t.clone().add(new THREE.Vector3(dist * 0.32, dist * 0.62, dist * 0.78)) };
  }
  function focusStage(key, doFly = true) {
    focus = key || null;
    applyFocus();
    if (key && doFly) {
      const v = stageView(key);
      if (v) fly(v.pos, v.target);
    }
  }
  function setLayers(next) {
    Object.assign(layers, next);
    roof.visible = layers.roof;
    markers.visible = layers.markers;
    flows.visible = layers.flow;
    lineObjs.forEach((lo) => { lo.lab.visible = layers.labels; });
    Object.values(dockObjs).forEach((o) => { o.lab.visible = layers.labels; });
    Object.values(markerObjs).forEach((o) => { o.lab.visible = layers.markers; });
  }
  function setToken(stageKey, progress = 0, lineIndex = 3) {
    if (!stageKey) { token.visible = false; tokenLabel.visible = false; tokenState = null; return; }
    const k = STAGES.findIndex((s) => s.key === stageKey);
    const zl = lineZ(Math.max(0, Math.min(lineObjs.length - 1, lineIndex)));
    const a = waypoint(STAGES[k].key, zl, lineIndex);
    const b = k < STAGES.length - 1 ? waypoint(STAGES[k + 1].key, zl, lineIndex) : a.clone().add(new THREE.Vector3(14, 0, 0));
    tokenState = { a, b, progress, key: stageKey };
    token.visible = true;
    tokenLabel.visible = true;
    Object.values(TOKEN_FORM).forEach((m) => { m.visible = false; });
    TOKEN_FORM[stageKey].visible = true;
    const c = new THREE.Color(STAGES[k].color);
    tokRing.material.color.copy(c);
    tokBeam.material.color.copy(c);
    tokenLabel.element.textContent = TOKEN_TEXT[stageKey];
    tokenLabel.element.style.setProperty('--c', STAGES[k].color);
  }

  // ── animation loop ─────────────────────────────────────────────────
  let raf = 0;
  let visible = true;
  const clock = new THREE.Clock();
  const io = new IntersectionObserver(([e]) => { visible = e.isIntersecting; });
  io.observe(container);
  const pillowDummy = new THREE.Object3D();
  function animate() {
    raf = requestAnimationFrame(animate);
    if (!visible || document.hidden) return;
    const t = clock.getElapsedTime();
    if (flyTween) {
      const k = Math.min(1, (performance.now() - flyTween.start) / flyTween.dur);
      const e = k < 0.5 ? 2 * k * k : 1 - (-2 * k + 2) ** 2 / 2;
      camera.position.lerpVectors(flyTween.p0, flyTween.p1, e);
      controls.target.lerpVectors(flyTween.t0, flyTween.t1, e);
      if (k >= 1) flyTween = null;
    }
    controls.update();
    // roof fades when looking straight down
    const down = (camera.position.y - controls.target.y) / camera.position.distanceTo(controls.target);
    roofMat.opacity = down > 0.93 ? 0 : 0.07;
    truss.visible = layers.roof && down < 0.93;
    purl.visible = truss.visible;
    led.visible = layers.roof && down < 0.93;
    setRollers(t);
    // conveyor pillows and press stroke
    let p = 0;
    lineObjs.forEach((lo) => {
      for (let k = 0; k < PILLOWS_PER_LINE; k += 1) {
        if (!lo.running) { pillowDummy.scale.setScalar(0.0001); } else {
          const ph = ((t * lo.rate / 900) + k / PILLOWS_PER_LINE) % 1;
          pillowDummy.position.set(X.conv0 + 0.8 + ph * (X.conv1 - X.conv0 - 1.6), 1.26, lo.z);
          pillowDummy.scale.setScalar(1);
        }
        pillowDummy.updateMatrix();
        pillows.setMatrixAt(p, pillowDummy.matrix);
        p += 1;
      }
      lo.platen.position.y = lo.running ? 2.0 + Math.abs(Math.sin(t * 1.6 + lo.z)) * 1.1 : 2.6;
    });
    pillows.instanceMatrix.needsUpdate = true;
    // machine health pulse
    pickMachines.forEach((m) => {
      const a = m.material.userData.alert;
      if (!a || focus === m.userData.stage) return;
      const c = a === 'critical' ? 0xef4444 : 0xf59e0b;
      m.material.emissive.setHex(c);
      m.material.emissiveIntensity = 0.35 + 0.35 * Math.sin(t * 4);
    });
    // selected stage glow pulse
    if (focus && glow[focus]) glow[focus].plane.material.opacity = 0.16 + 0.1 * Math.sin(t * 3);
    if (focus && stageMats[focus]) {
      const st = STAGE_BY_KEY[focus];
      if (st) stageMats[focus].forEach((m) => { if (m.emissive && !m.userData.alert) m.emissiveIntensity = 0.24 + 0.14 * Math.sin(t * 3); });
    }
    // markers bob + flow pulse
    Object.values(markerObjs).forEach((o) => {
      o.sphere.position.y = o.y + Math.sin(t * 2 + o.phase) * 0.45;
      o.ring.rotation.z = t * 0.8;
    });
    flowArrows.forEach((f) => {
      f.mm.opacity = 0.45 + 0.3 * Math.sin(t * 2 + f.k);
      f.dots.forEach((d, j) => {
        const ph = ((t * 0.35) + j / 3 + f.k * 0.13) % 1;
        d.position.lerpVectors(f.from, f.to, ph);
      });
    });
    // walkthrough token
    if (tokenState) {
      const e = tokenState.progress < 0.5 ? 2 * tokenState.progress ** 2 : 1 - (-2 * tokenState.progress + 2) ** 2 / 2;
      token.position.lerpVectors(tokenState.a, tokenState.b, Math.max(0, (e - 0.35) / 0.65));
      token.position.y += Math.abs(Math.sin(t * 3)) * 0.25;
      tokFibre.rotation.y = t;
      tokRing.scale.setScalar(1 + 0.15 * Math.sin(t * 4));
    }
    renderer.render(scene, camera);
    labels.render(scene, camera);
    if (t - lastDeclutter > 0.2) { lastDeclutter = t; declutter(); }
  }
  // labels never stack: a lower-priority label that collides with a higher one is hidden
  let lastDeclutter = -1;
  const labelPrio = (c) => {
    if (c.contains('ftw-token')) return 0;
    if (c.contains('ftw-marker')) {
      if (c.contains('is-active')) return 1;
      if (c.contains('is-support')) return 6;
      return c.contains('is-dim') ? 4 : 2;
    }
    if (c.contains('ftw-line')) return 3;
    if (c.contains('ftw-dock')) return 5;
    return 7;
  };
  function declutter() {
    const items = [...labels.domElement.children]
      .filter((e) => e.style.display !== 'none')
      .map((e) => ({ e, p: labelPrio(e.classList), r: e.getBoundingClientRect() }))
      .sort((a, b) => a.p - b.p);
    const placed = [];
    items.forEach(({ e, r }) => {
      if (!r.width) return;
      const hit = placed.some((q) => r.left < q.right + 3 && r.right > q.left - 3 && r.top < q.bottom + 2 && r.bottom > q.top - 2);
      e.classList.toggle('ftw-occluded', hit);
      if (!hit) placed.push(r);
    });
  }
  animate();

  // ── picking & hover ────────────────────────────────────────────────
  const ray = new THREE.Raycaster();
  const mouse = new THREE.Vector2();
  function hit(ev) {
    const r = renderer.domElement.getBoundingClientRect();
    mouse.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(mouse, camera);
    const m = ray.intersectObjects(pickMachines, false)[0];
    if (m) return m.object.userData;
    const a = ray.intersectObjects(pickAreas, false)[0];
    if (a) return a.object.userData;
    // area stages: test the floor point against stage regions
    const g = new THREE.Vector3();
    if (ray.ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0, 1, 0), 0), g)) {
      const s = STAGES.find((st) => g.x >= st.region[0] && g.x <= st.region[1] && g.z >= st.region[2] && g.z <= st.region[3]
        && !['opening', 'carding', 'filling', 'sewing', 'bagging', 'packing'].includes(st.key));
      if (s) return { kind: 'stage', stage: s.key };
    }
    return null;
  }
  let down = null;
  const onMove = (ev) => {
    if (ev.buttons) return;
    const u = hit(ev);
    renderer.domElement.style.cursor = u ? 'pointer' : 'grab';
    cb.onHover?.(u, ev.clientX, ev.clientY);
  };
  const onDown = (ev) => { down = { x: ev.clientX, y: ev.clientY }; };
  const onUp = (ev) => {
    if (!down || Math.hypot(ev.clientX - down.x, ev.clientY - down.y) > 5) return;
    const u = hit(ev);
    if (!u) return;
    cb.onSelectStage?.(u.stage);
    if (u.kind === 'machine' && u.id) cb.onOpenMachine?.(u.id);
    else if (u.kind === 'line') cb.onOpenLine?.(u.lineId);
  };
  const onLeave = () => cb.onHover?.(null);
  renderer.domElement.addEventListener('pointermove', onMove);
  renderer.domElement.addEventListener('pointerdown', onDown);
  renderer.domElement.addEventListener('pointerup', onUp);
  renderer.domElement.addEventListener('pointerleave', onLeave);
  const ro = new ResizeObserver(() => {
    camera.aspect = W() / H();
    camera.updateProjectionMatrix();
    renderer.setSize(W(), H());
    labels.setSize(W(), H());
  });
  ro.observe(container);

  function dispose() {
    cancelAnimationFrame(raf);
    ro.disconnect();
    io.disconnect();
    renderer.domElement.removeEventListener('pointermove', onMove);
    renderer.domElement.removeEventListener('pointerdown', onDown);
    renderer.domElement.removeEventListener('pointerup', onUp);
    renderer.domElement.removeEventListener('pointerleave', onLeave);
    controls.dispose();
    scene.traverse((o) => {
      if (o.geometry && ![BOX, CYL, CONE].includes(o.geometry)) o.geometry.dispose();
      const mats = Array.isArray(o.material) ? o.material : [o.material];
      mats.forEach((m) => m && m.dispose && m.dispose());
    });
    renderer.dispose();
    renderer.domElement.remove();
    labels.domElement.remove();
  }

  return {
    update, setCamera, focusStage, setLayers, setToken, dispose,
    center: CENTER,
    stageView,
  };
}

export { MACHINE_STAGE };
