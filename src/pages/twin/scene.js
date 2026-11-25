/* Three.js scene for the facility digital twin.

   createFacilityScene(container, data, callbacks) builds the plant once from
   the facility payload and returns an API:
     update(data)          apply the latest live state (every simulation tick)
     setView('top'|'3d')   orthographic top view or orbiting 3D view
     setLayers({...})      workers, flow, labels, counts, readers, heat
     select(lineId|null)   highlight a line
     focusStage(key|null)  highlight every station of one process step
     dispose()
   callbacks: onSelectLine(id), onSelectWorker(id), onHover({kind, ...}|null, x, y) */
import * as THREE from 'three';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import { CSS2DRenderer, CSS2DObject } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import { RoundedBoxGeometry } from 'three/examples/jsm/geometries/RoundedBoxGeometry.js';
import {
  BOUNDS, DOCK_WALL_X, DOCK_Z, LINE_STATUS_COLOR, MAIN_AISLE_Z, NO_READ, OPERATOR_DZ, PACK_X, PALLET, QA, RACK,
  RECV_WALL_X, RECV_Z, ROLE_COLOR, ROOMS, STATE_COLOR, SX, ZONES, hash, lineZ, rackX, workerTarget,
} from './layout';

const fmt = (n) => (n == null ? '—' : Math.round(n).toLocaleString('en-US'));
const MAX_PILLOWS = 900;
const MAX_PALLETS = 260;

function mat(color, opts = {}) {
  return new THREE.MeshStandardMaterial({ color, roughness: 0.62, metalness: 0.18, ...opts });
}

function label(html, cls = 'tw-label', onClick) {
  const el = document.createElement('div');
  el.className = cls;
  el.innerHTML = html;
  if (onClick) {
    el.style.pointerEvents = 'auto';
    el.style.cursor = 'pointer';
    el.addEventListener('pointerdown', (e) => { e.stopPropagation(); onClick(); });
  }
  const obj = new CSS2DObject(el);
  return obj;
}

function box(w, h, d, material, x, y, z, parent, shadow = true) {
  const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), material);
  m.position.set(x, y, z);
  if (shadow) { m.castShadow = true; m.receiveShadow = true; }
  parent.add(m);
  return m;
}

function floorRect(x0, x1, z0, z1, color, parent, y = 0.01, opacity = 1) {
  const g = new THREE.PlaneGeometry(x1 - x0, z1 - z0);
  const m = new THREE.Mesh(g, new THREE.MeshStandardMaterial({ color, roughness: 0.95, metalness: 0, transparent: opacity < 1, opacity }));
  m.rotation.x = -Math.PI / 2;
  m.position.set((x0 + x1) / 2, y, (z0 + z1) / 2);
  m.receiveShadow = true;
  parent.add(m);
  return m;
}

function outline(x0, x1, z0, z1, color, parent, y = 0.03, opacity = 0.55) {
  const pts = [[x0, z0], [x1, z0], [x1, z1], [x0, z1], [x0, z0]].map(([x, z]) => new THREE.Vector3(x, y, z));
  const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts),
    new THREE.LineBasicMaterial({ color, transparent: true, opacity }));
  parent.add(line);
  return line;
}

function wall(x0, z0, x1, z1, h, material, parent) {
  const len = Math.hypot(x1 - x0, z1 - z0);
  const m = new THREE.Mesh(new THREE.BoxGeometry(len, h, 0.35), material);
  m.position.set((x0 + x1) / 2, h / 2, (z0 + z1) / 2);
  m.rotation.y = -Math.atan2(z1 - z0, x1 - x0);
  m.castShadow = true;
  m.receiveShadow = true;
  parent.add(m);
  return m;
}

export function createFacilityScene(container, initial, callbacks = {}) {
  const W = () => container.clientWidth || 800;
  const H = () => container.clientHeight || 500;

  // ── renderer, scene, cameras ───────────────────────────────────────
  const renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.setSize(W(), H());
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  container.appendChild(renderer.domElement);
  renderer.domElement.style.display = 'block';

  const labelRenderer = new CSS2DRenderer();
  labelRenderer.setSize(W(), H());
  Object.assign(labelRenderer.domElement.style, { position: 'absolute', inset: '0', pointerEvents: 'none' });
  container.appendChild(labelRenderer.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x050a14);

  const cx = (BOUNDS.x0 + BOUNDS.x1) / 2;
  const cz = (BOUNDS.z0 + BOUNDS.z1) / 2;
  const ortho = new THREE.OrthographicCamera(-100, 100, 50, -50, 1, 1000);
  const persp = new THREE.PerspectiveCamera(42, W() / H(), 1, 2000);
  let camera = ortho;
  let controls = null;
  let view = 'top';

  function fitOrtho() {
    const a = W() / H();
    // extra room at the top for the floating overlays
    const hw = (BOUNDS.x1 - BOUNDS.x0 + 10) / 2;
    const hd = (BOUNDS.z1 - BOUNDS.z0 + 16) / 2;
    const hh = Math.max(hd, hw / a);
    ortho.left = -hh * a; ortho.right = hh * a; ortho.top = hh; ortho.bottom = -hh;
    ortho.zoom = 1;
    ortho.updateProjectionMatrix();
  }

  function makeControls() {
    if (controls) controls.dispose();
    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.12;
    if (view === 'top') {
      controls.enableRotate = false;
      controls.screenSpacePanning = true;
      controls.mouseButtons = { LEFT: THREE.MOUSE.PAN, MIDDLE: THREE.MOUSE.DOLLY, RIGHT: THREE.MOUSE.PAN };
      controls.touches = { ONE: THREE.TOUCH.PAN, TWO: THREE.TOUCH.DOLLY_PAN };
      controls.minZoom = 0.8;
      controls.maxZoom = 7;
    } else {
      controls.maxPolarAngle = 1.32;
      controls.minDistance = 25;
      controls.maxDistance = 420;
    }
    controls.addEventListener('change', onZoomChange);
  }

  function setView(v, keep = false) {
    view = v;
    if (v === 'top') {
      camera = ortho;
      if (!keep) fitOrtho();
      ortho.position.set(cx, 300, cz - 7 + 0.01);
      ortho.lookAt(cx, 0, cz - 7);
      makeControls();
      controls.target.set(cx, 0, cz - 7);
    } else {
      camera = persp;
      persp.aspect = W() / H();
      persp.updateProjectionMatrix();
      persp.position.set(cx - 34, 92, cz + 104);
      makeControls();
      controls.target.set(cx + 4, 0, cz - 2);
    }
    controls.update();
    onZoomChange();
  }

  // ── lights ──────────────────────────────────────────────────────────
  scene.add(new THREE.HemisphereLight(0xbfd8ff, 0x0b1220, 0.75));
  const sun = new THREE.DirectionalLight(0xffffff, 1.55);
  sun.position.set(-60, 160, 70);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  Object.assign(sun.shadow.camera, { left: -120, right: 120, top: 70, bottom: -70, near: 10, far: 400 });
  sun.shadow.bias = -0.0006;
  scene.add(sun);
  const fill = new THREE.DirectionalLight(0x7aa2ff, 0.35);
  fill.position.set(80, 60, -60);
  scene.add(fill);

  // ── static building ────────────────────────────────────────────────
  const root = new THREE.Group();
  scene.add(root);
  floorRect(BOUNDS.x0 - 30, BOUNDS.x1 + 30, BOUNDS.z0 - 30, BOUNDS.z1 + 30, 0x060c18, root, -0.02);
  floorRect(-88, 84, -49, 39, 0x0f1828, root, 0);
  const grid = new THREE.GridHelper(180, 90, 0x1d2c45, 0x142036);
  grid.position.set(-2, 0.015, -5);
  grid.material.transparent = true;
  grid.material.opacity = 0.35;
  root.add(grid);
  Object.values(ZONES).forEach((z) => {
    floorRect(z.x0 + 0.3, z.x1 - 0.3, z.z0 + 0.3, z.z1 - 0.3, z.color, root, 0.02);
    outline(z.x0 + 0.3, z.x1 - 0.3, z.z0 + 0.3, z.z1 - 0.3, 0x3b5b8a, root, 0.04, 0.5);
  });
  // safety aisle markings
  const yellow = new THREE.MeshBasicMaterial({ color: 0xd4a017, transparent: true, opacity: 0.55 });
  [MAIN_AISLE_Z - 1.1, MAIN_AISLE_Z + 1.1].forEach((z) => {
    const s = new THREE.Mesh(new THREE.PlaneGeometry(166, 0.18), yellow);
    s.rotation.x = -Math.PI / 2;
    s.position.set(-2, 0.05, z);
    root.add(s);
  });
  // exterior walls (low, so the top view stays readable)
  const wallMat = mat(0x2a3b55, { transparent: true, opacity: 0.9 });
  wall(-88, -49, 84, -49, 3.2, wallMat, root);
  wall(-88, 39, 84, 39, 3.2, wallMat, root);
  wall(-88, -49, -88, -15, 3.2, wallMat, root);
  wall(-88, -9, -88, 5, 3.2, wallMat, root);
  wall(-88, 11, -88, 39, 3.2, wallMat, root);
  wall(84, -49, 84, -27, 3.2, wallMat, root);
  [[-17, -11], [-1, 5], [15, 21]].forEach(([a, b]) => wall(84, a, 84, b, 3.2, wallMat, root));
  wall(84, 31, 84, 39, 3.2, wallMat, root);
  const roomWall = mat(0x30476b, { transparent: true, opacity: 0.85 });
  Object.entries(ROOMS).forEach(([name, r]) => {
    floorRect(r.x0, r.x1, r.z0, r.z1, r.color, root, 0.03);
    wall(r.x0, r.z0, r.x1, r.z0, 1.4, roomWall, root);
    wall(r.x0, r.z0, r.x0, r.z1, 1.4, roomWall, root);
    wall(r.x1, r.z0, r.x1, r.z1, 1.4, roomWall, root);
    const door = name === 'Material store' ? r.z0 : r.z1;
    const mid = (r.x0 + r.x1) / 2;
    wall(r.x0, door, mid - 1.4, door, 1.4, roomWall, root);
    wall(mid + 1.4, door, r.x1, door, 1.4, roomWall, root);
  });
  // break room / canteen furniture
  const tableMat = mat(0x4a5d7a);
  [ROOMS['Break room'], ROOMS.Canteen].forEach((r) => {
    for (let x = r.x0 + 3; x < r.x1 - 2; x += 4.5) {
      for (let z = r.z0 + 2.5; z < r.z1 - 2; z += 3.6) box(2.6, 0.75, 1.2, tableMat, x, 0.38, z, root);
    }
  });
  box(5, 0.9, 1.4, mat(0x6b5a3a), -11.5, 0.45, -44.5, root);               // supervisor desk
  box(7, 1.8, 1.2, mat(0x7a4b1e), 2.5, 0.9, -45.6, root);                   // maintenance crib shelving
  for (let i = 0; i < 4; i += 1) box(0.9, 1.9, 0.6, mat(0x5b6b86), -27.6 + i * 1.1, 0.95, -46.3, root);  // lockers

  // ── zone labels ─────────────────────────────────────────────────────
  const staticLabels = [];
  Object.values(ZONES).forEach((z) => {
    const l = label(z.label, 'tw-zone');
    l.position.set((z.x0 + z.x1) / 2, 0.2, z.z1 + 1.6);
    if (z === ZONES.hall) l.position.set(-21, 0.2, z.z1 + 1.6);
    if (z === ZONES.receiving) l.position.set(-71, 0.2, -33.6);
    root.add(l);
    staticLabels.push(l);
  });
  const ROOM_SHORT = { 'Locker room': 'Lockers', 'Supervisor desk': 'Supervisor', 'Maintenance crib': 'Maint. crib' };
  Object.entries(ROOMS).forEach(([name, r]) => {
    const l = label(ROOM_SHORT[name] || name, 'tw-room');
    l.position.set((r.x0 + r.x1) / 2, 1.6, name === 'Material store' ? r.z1 - 1.5 : r.z0 + 1.6);
    root.add(l);
    staticLabels.push(l);
  });
  const noReadLabel = label(NO_READ.label, 'tw-room tw-room-muted');
  noReadLabel.position.set(NO_READ.x, 0.3, NO_READ.z + 4.2);
  root.add(noReadLabel);
  staticLabels.push(noReadLabel);
  const noReadRing = new THREE.Mesh(new THREE.RingGeometry(4.5, 4.8, 48),
    new THREE.MeshBasicMaterial({ color: 0x94a3b8, transparent: true, opacity: 0.45, side: THREE.DoubleSide }));
  noReadRing.rotation.x = -Math.PI / 2;
  noReadRing.position.set(NO_READ.x, 0.05, NO_READ.z);
  root.add(noReadRing);

  // ── RFID readers ────────────────────────────────────────────────────
  const readers = new THREE.Group();
  root.add(readers);
  const readerGeo = new THREE.CylinderGeometry(0.42, 0.42, 0.35, 6);
  const readerMat = new THREE.MeshStandardMaterial({ color: 0x22d3ee, emissive: 0x22d3ee, emissiveIntensity: 0.9 });
  const readerMeshes = [];
  const addReader = (x, z) => {
    const r = new THREE.Mesh(readerGeo, readerMat);
    r.position.set(x, 2.2, z);
    readers.add(r);
    const ring = new THREE.Mesh(new THREE.RingGeometry(1.2, 1.35, 32),
      new THREE.MeshBasicMaterial({ color: 0x22d3ee, transparent: true, opacity: 0.35, side: THREE.DoubleSide }));
    ring.rotation.x = -Math.PI / 2;
    ring.position.set(x, 0.06, z);
    readers.add(ring);
    readerMeshes.push(ring);
  };
  Object.entries(ROOMS).forEach(([name, r]) => addReader((r.x0 + r.x1) / 2, name === 'Material store' ? r.z0 : r.z1));

  // ── shared geometry/materials ──────────────────────────────────────
  const M = {
    opener: mat(0x4b6a93), flapper: mat(0x5b7aa6), card: mat(0x667892), roller: mat(0xa9b6c9, { metalness: 0.6, roughness: 0.3 }),
    blow: mat(0x7766a3), filler: mat(0x2a9d8f), hopper: mat(0x7fd1c7, { transparent: true, opacity: 0.75 }),
    table: mat(0x8a7c64), sewm: mat(0x2b3242), press: mat(0xc28b2c), conv: mat(0x1e2735), convSide: mat(0x3a4a63),
    bun: new THREE.MeshStandardMaterial({ color: 0xf8fafc, roughness: 1, transparent: true, opacity: 0.85, emissive: 0x334155, emissiveIntensity: 0.2 }),
    kraft: mat(0xc49a5c), kraftBlocked: mat(0xb4473b, { emissive: 0x5a1010, emissiveIntensity: 0.4 }), pallet: mat(0x8b6a43),
    upright: mat(0x2f6fd6, { metalness: 0.5 }), beam: mat(0xe07b24, { metalness: 0.4 }),
    bale: mat(0xe9e4d8, { roughness: 0.95 }), shell: mat(0x4f78b8), cage: new THREE.MeshBasicMaterial({ color: 0xef4444, transparent: true, opacity: 0.6 }),
    camera: mat(0x111827), fov: new THREE.MeshBasicMaterial({ color: 0x38bdf8, transparent: true, opacity: 0.12, side: THREE.DoubleSide, depthWrite: false }),
    beaconRed: new THREE.MeshStandardMaterial({ color: 0xef4444, emissive: 0xef4444, emissiveIntensity: 1.4 }),
    beaconAmber: new THREE.MeshStandardMaterial({ color: 0xf59e0b, emissive: 0xf59e0b, emissiveIntensity: 1.2 }),
  };

  // ── production lines ───────────────────────────────────────────────
  const lineObjs = [];      // per line: {id, group, strip, stations, machines:{code:mesh}, labels...}
  const pickables = [];
  const lines0 = initial.lines;
  const lineIndex = {};
  lines0.forEach((ln, i) => { lineIndex[ln.id] = i; });

  lines0.forEach((ln, i) => {
    const z = lineZ(i);
    const g = new THREE.Group();
    g.userData = { kind: 'line', id: ln.id };
    root.add(g);
    const pick = (mesh, extra = {}) => { mesh.userData = { kind: 'line', id: ln.id, ...extra }; pickables.push(mesh); return mesh; };
    const base = floorRect(-53, 15, z - 2.6, z + 3.6, 0x1a2a44, g, 0.035, 0.0);
    const sel = floorRect(-53.5, 17, z - 3.1, z + 3.9, 0x38bdf8, g, 0.036, 0.0);
    // status strip
    const stripMat = new THREE.MeshBasicMaterial({ color: LINE_STATUS_COLOR[ln.status] || '#64748b', transparent: true, opacity: 0.9 });
    const strip = new THREE.Mesh(new THREE.PlaneGeometry(51, 0.34), stripMat);
    strip.rotation.x = -Math.PI / 2;
    strip.position.set(-26, 0.06, z - 2.25);
    g.add(strip);
    const mcByStage = {};
    ln.machines.forEach((mc) => { mcByStage[mc.stage] = mc; });
    const machineMeshes = {};
    // bale feeder + fibre opener
    pick(box(2.2, 1.1, 2.0, M.bale, SX.feed, 0.55, z, g), { station: 'fibre' });
    const opener = pick(box(4, 2.5, 2.7, M.opener.clone(), SX.opener, 1.25, z, g), { station: 'fibre', machine: mcByStage.Mixing?.code });
    box(1.2, 0.9, 1.2, M.roller, SX.opener + 1.2, 2.9, z, g);
    machineMeshes[mcByStage.Mixing?.code] = opener;
    const flap = pick(box(3, 2.2, 2.4, M.flapper.clone(), SX.flapper, 1.1, z, g), { station: 'fibre', machine: mcByStage['Flapper Processing']?.code });
    machineMeshes[mcByStage['Flapper Processing']?.code] = flap;
    // duct from flapper to card
    const duct = new THREE.Mesh(new THREE.CylinderGeometry(0.35, 0.35, 4.6, 12), M.convSide);
    duct.rotation.z = Math.PI / 2;
    duct.position.set(SX.flapper + 3.3, 2.3, z);
    g.add(duct);
    let cardMesh;
    if (ln.type === 'Garnett') {
      cardMesh = pick(box(SX.cardLen, 1.9, 2.8, M.card.clone(), SX.card, 0.95, z, g), { station: 'fibre', machine: mcByStage['Bun Formation']?.code });
      for (let k = 0; k < 6; k += 1) {
        const r = new THREE.Mesh(new THREE.CylinderGeometry(0.42, 0.42, 2.6, 16), M.roller);
        r.rotation.x = Math.PI / 2;
        r.position.set(SX.card - SX.cardLen / 2 + 1 + k * 1.7, 2.15, z);
        r.castShadow = true;
        g.add(r);
      }
    } else {
      cardMesh = pick(new THREE.Mesh(new THREE.CylinderGeometry(1.45, 1.45, 2.2, 28), M.blow.clone()), { station: 'fibre', machine: mcByStage['Bun Formation']?.code });
      cardMesh.position.set(SX.card - 2, 1.1, z);
      cardMesh.castShadow = true;
      g.add(cardMesh);
      const blowDuct = new THREE.Mesh(new THREE.CylinderGeometry(0.45, 0.45, 7.5, 12), M.convSide);
      blowDuct.rotation.z = Math.PI / 2;
      blowDuct.position.set(SX.card + 3.2, 1.6, z);
      g.add(blowDuct);
    }
    machineMeshes[mcByStage['Bun Formation']?.code] = cardMesh;
    // fibre bun travelling into the filler
    const bun = new THREE.Mesh(new THREE.BoxGeometry(6, 0.18, 1.6), M.bun);
    bun.position.set(SX.card + 6.3, 1.05, z);
    g.add(bun);
    // filling head with hopper
    const filler = pick(box(2.4, 2.8, 2.4, M.filler.clone(), SX.filler, 1.4, z, g), { station: 'filling', machine: mcByStage['Pillow Filling']?.code });
    const hop = new THREE.Mesh(new THREE.ConeGeometry(1.3, 1.6, 20, 1, true), M.hopper);
    hop.rotation.x = Math.PI;
    hop.position.set(SX.filler, 3.6, z);
    g.add(hop);
    machineMeshes[mcByStage['Pillow Filling']?.code] = filler;
    // buffers, sewing tables, bagging press
    pick(box(2.0, 0.85, 1.7, M.table, SX.sewBuf, 0.43, z, g), { station: 'sewing' });
    [SX.sew1, SX.sew2].forEach((x) => {
      pick(box(2.8, 0.85, 1.6, M.table, x, 0.43, z, g), { station: 'sewing' });
      box(0.9, 0.55, 0.45, M.sewm, x - 0.4, 1.13, z + 0.2, g);
    });
    pick(box(1.7, 0.85, 1.6, M.table, SX.bagBuf, 0.43, z, g), { station: 'bagging' });
    pick(box(2.4, 2.1, 2.1, M.press, SX.bagger, 1.05, z, g), { station: 'bagging' });
    // outgoing conveyor to packing
    const convLen = SX.convB - SX.convA;
    pick(box(convLen, 0.25, 1.1, M.conv, (SX.convA + SX.convB) / 2, 0.85, z, g, false), { station: 'packing' });
    [-0.62, 0.62].forEach((dz) => box(convLen, 0.18, 0.1, M.convSide, (SX.convA + SX.convB) / 2, 1.0, z + dz, g, false));
    // beacon for breakdowns / maintenance
    const beacon = new THREE.Mesh(new THREE.CylinderGeometry(0.35, 0.35, 0.7, 14), M.beaconRed);
    beacon.visible = false;
    g.add(beacon);
    // station RFID readers
    [SX.filler, SX.sew1, SX.sew2, SX.bagger].forEach((x) => addReader(x + 1.2, z + OPERATOR_DZ + 0.9));

    // labels
    const lineLabel = label('', 'tw-line', () => callbacks.onSelectLine?.(ln.id));
    lineLabel.center.set(0, 0.5);   // anchor the label's left edge at the start of the line
    lineLabel.position.set(-53.2, 0.5, z - 3.45);
    g.add(lineLabel);
    const chips = {};
    [['filling', SX.filler], ['sewing', (SX.sew1 + SX.sew2) / 2], ['bagging', SX.bagger], ['packing', SX.convB - 2]].forEach(([k, x]) => {
      const c = label('', 'tw-chip');
      c.position.set(x, 3.4, z - 1.2);
      g.add(c);
      chips[k] = c;
    });
    lineObjs.push({ id: ln.id, z, g, base, sel, strip, stripMat, machineMeshes, beacon, bun, lineLabel, chips, type: ln.type });
  });

  // ── packing cells, QA hold, palletizing ────────────────────────────
  const cellZ = [0, 1, 2, 3].map((p) => (lineZ(p * 2) + lineZ(p * 2 + 1)) / 2);
  const packChips = [];
  cellZ.forEach((z, p) => {
    const t = box(4.4, 0.9, 6.2, M.table, PACK_X, 0.45, z, root);
    t.userData = { kind: 'packing', cell: p };
    pickables.push(t);
    box(1.4, 1.1, 1.2, M.kraft, PACK_X - 0.8, 1.45, z - 1.5, root);
    // CV camera on a pole with its field of view
    box(0.18, 4.6, 0.18, M.camera, PACK_X - 2.6, 2.3, z - 2.8, root);
    box(0.9, 0.5, 0.6, M.camera, PACK_X - 1.9, 4.5, z - 2.4, root);
    const fov = new THREE.Mesh(new THREE.ConeGeometry(2.1, 3.6, 4, 1, true), M.fov);
    fov.position.set(PACK_X - 0.9, 2.7, z - 1.2);
    fov.rotation.y = Math.PI / 4;
    root.add(fov);
    addReader(PACK_X + 3, z + 2.8);
    const c = label('', 'tw-chip');
    c.position.set(PACK_X, 3.6, z + 2.6);
    root.add(c);
    packChips.push(c);
  });
  floorRect(QA.x0, QA.x1, QA.z0, QA.z1, 0x3a1518, root, 0.04);
  [[QA.x0, QA.z0, QA.x1, QA.z0], [QA.x1, QA.z0, QA.x1, QA.z1], [QA.x0, QA.z0, QA.x0, QA.z1], [QA.x0, QA.z1, QA.x1, QA.z1]]
    .forEach(([a, b, c, d]) => wall(a, b, c, d, 1.8, M.cage, root));
  const qaLabel = label('QA hold', 'tw-room tw-room-danger');
  qaLabel.position.set((QA.x0 + QA.x1) / 2, 2.4, QA.z0 + 1.2);
  root.add(qaLabel);
  const qaPick = floorRect(QA.x0, QA.x1, QA.z0, QA.z1, 0x000000, root, 0.045, 0.0);
  qaPick.userData = { kind: 'qa' };
  pickables.push(qaPick);
  const palletLabel = label('Palletizing', 'tw-room');
  palletLabel.position.set((PALLET.x0 + PALLET.x1) / 2, 0.3, PALLET.z0 + 0.2);
  root.add(palletLabel);

  // ── racks ───────────────────────────────────────────────────────────
  const rackLen = 12 * RACK.module;
  const rackChips = {};
  Object.entries(RACK.zoneZ).forEach(([zone, z]) => {
    for (let k = 0; k <= 12; k += 1) {
      const x = RACK.x0 + k * RACK.module;
      [-RACK.depth / 2, RACK.depth / 2].forEach((dz) => box(0.14, RACK.levelH * 4 + 0.4, 0.14, M.upright, x, (RACK.levelH * 4 + 0.4) / 2, z + dz, root, false));
    }
    for (let lv = 0; lv < 4; lv += 1) {
      [-RACK.depth / 2, RACK.depth / 2].forEach((dz) => box(rackLen, 0.12, 0.12, M.beam, RACK.x0 + rackLen / 2, lv * RACK.levelH + 0.02, z + dz, root, false));
    }
    const pick = floorRect(RACK.x0, RACK.x0 + rackLen, z - RACK.depth / 2, z + RACK.depth / 2, 0x000000, root, 0.05, 0.0);
    pick.userData = { kind: 'rack', zone };
    pickables.push(pick);
    const c = label('', 'tw-chip tw-chip-lg');
    c.position.set(RACK.x0 - 2.2, 0.4, z);
    root.add(c);
    rackChips[zone] = c;
  });
  addReader(44, -11); addReader(62, 11);
  const palletMesh = new THREE.InstancedMesh(new THREE.BoxGeometry(RACK.module * 0.86, 1.2, RACK.depth * 0.86), M.kraft, MAX_PALLETS);
  palletMesh.castShadow = true;
  palletMesh.receiveShadow = true;
  palletMesh.count = 0;
  root.add(palletMesh);
  const palletBase = new THREE.InstancedMesh(new THREE.BoxGeometry(RACK.module * 0.9, 0.16, RACK.depth * 0.9), M.pallet, MAX_PALLETS);
  palletBase.count = 0;
  root.add(palletBase);

  // ── docks & receiving ──────────────────────────────────────────────
  const dockObjs = {};
  Object.entries(DOCK_Z).forEach(([d, z]) => {
    floorRect(DOCK_WALL_X - 5.5, DOCK_WALL_X - 0.2, z - 2.4, z + 2.4, 0x23334d, root, 0.04);
    const truck = new THREE.Group();
    const trailer = box(15, 3.4, 2.7, mat(0xe5e7eb), 8.2, 2.1, 0, truck);
    const stripe = box(15.02, 0.5, 2.72, mat(0x3b7de8), 8.2, 2.9, 0, truck);
    box(3.4, 2.8, 2.6, mat(0x1f4e8c), 17.6, 1.6, 0, truck);
    truck.position.set(DOCK_WALL_X + 0.4, 0, z);
    truck.visible = false;
    trailer.userData = { kind: 'dock', dock: d };
    pickables.push(trailer);
    root.add(truck);
    const l = label('', 'tw-dock');
    l.position.set(DOCK_WALL_X - 3, 0.3, z - 3.4);
    root.add(l);
    dockObjs[d] = { truck, stripe, label: l, z };
  });
  const stagedMesh = new THREE.InstancedMesh(new THREE.BoxGeometry(1.4, 1.1, 1.2), M.kraft, 64);
  stagedMesh.count = 0;
  stagedMesh.castShadow = true;
  root.add(stagedMesh);
  const recvTrucks = {};
  Object.entries(RECV_Z).forEach(([d, z]) => {
    const truck = new THREE.Group();
    box(14, 3.4, 2.7, mat(0xd1d5db), -8, 2.1, 0, truck);
    box(3.2, 2.8, 2.6, mat(0x4b5563), -17, 1.6, 0, truck);
    truck.position.set(RECV_WALL_X - 2.2, 0, z);
    truck.visible = false;
    root.add(truck);
    const l = label(d, 'tw-dock');
    l.position.set(RECV_WALL_X + 3.2, 0.3, z - 3.2);
    root.add(l);
    recvTrucks[d] = { truck, label: l };
  });
  // raw material stacks (bales & shell pallets)
  const baleMesh = new THREE.InstancedMesh(new THREE.BoxGeometry(1.5, 1.2, 1.2), M.bale, 120);
  baleMesh.castShadow = true;
  baleMesh.count = 0;
  root.add(baleMesh);
  const shellMesh = new THREE.InstancedMesh(new THREE.BoxGeometry(1.4, 1.0, 1.2), M.shell, 60);
  shellMesh.castShadow = true;
  shellMesh.count = 0;
  root.add(shellMesh);
  const rawChip = label('', 'tw-chip tw-chip-lg');
  rawChip.position.set(-71, 3, -26);
  root.add(rawChip);
  addReader(-80, -12); addReader(-80, 8);

  // ── pillows (instanced) ────────────────────────────────────────────
  const pillowGeo = new RoundedBoxGeometry(0.95, 0.3, 0.62, 2, 0.13);
  const pillowMat = new THREE.MeshStandardMaterial({ color: 0xf8fafc, roughness: 0.85, metalness: 0 });
  const pillows = new THREE.InstancedMesh(pillowGeo, pillowMat, MAX_PILLOWS);
  pillows.castShadow = true;
  pillows.count = 0;
  root.add(pillows);
  const blockedMesh = new THREE.InstancedMesh(new THREE.BoxGeometry(1.3, 1.0, 1.1), M.kraftBlocked, 40);
  blockedMesh.count = 0;
  root.add(blockedMesh);
  const openPalletMesh = new THREE.InstancedMesh(new THREE.BoxGeometry(2.1, 1, 1.9), M.kraft, 60);
  openPalletMesh.count = 0;
  openPalletMesh.castShadow = true;
  root.add(openPalletMesh);

  // ── heat (away dwell) ──────────────────────────────────────────────
  const heat = new THREE.Group();
  root.add(heat);
  const heatDiscs = {};
  Object.keys(ROOMS).concat([NO_READ.label]).forEach((name) => {
    const disc = new THREE.Mesh(new THREE.CircleGeometry(1, 40),
      new THREE.MeshBasicMaterial({ color: 0xf97316, transparent: true, opacity: 0.32, depthWrite: false }));
    disc.rotation.x = -Math.PI / 2;
    const r = ROOMS[name];
    disc.position.set(r ? (r.x0 + r.x1) / 2 : NO_READ.x, 0.08, r ? (r.z0 + r.z1) / 2 : NO_READ.z);
    disc.visible = false;
    heat.add(disc);
    heatDiscs[name] = disc;
  });

  // ── workers ─────────────────────────────────────────────────────────
  const workers = new Map(); // id -> {group, ring, pulse, body, pos, path, w}
  const bodyGeo = new THREE.CylinderGeometry(0.36, 0.42, 1.3, 16);
  const headGeo = new THREE.SphereGeometry(0.32, 16, 12);
  const ringGeo = new THREE.RingGeometry(0.62, 0.86, 28);
  const pulseGeo = new THREE.RingGeometry(0.9, 1.08, 28);
  const skin = mat(0xd9a77f, { roughness: 0.8 });
  const roleMats = {};
  const roleMat = (role) => {
    if (!roleMats[role]) roleMats[role] = mat(ROLE_COLOR[role] || '#94a3b8');
    return roleMats[role];
  };
  function ensureWorker(w) {
    let o = workers.get(w.id);
    if (o) return o;
    const group = new THREE.Group();
    const body = new THREE.Mesh(bodyGeo, roleMat(w.role));
    body.position.y = 0.75;
    body.castShadow = true;
    const head = new THREE.Mesh(headGeo, skin);
    head.position.y = 1.65;
    head.castShadow = true;
    const ring = new THREE.Mesh(ringGeo, new THREE.MeshBasicMaterial({ color: STATE_COLOR.Station, side: THREE.DoubleSide, transparent: true, opacity: 0.95 }));
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = 0.06;
    const pulse = new THREE.Mesh(pulseGeo, new THREE.MeshBasicMaterial({ color: STATE_COLOR.Long, side: THREE.DoubleSide, transparent: true, opacity: 0.8 }));
    pulse.rotation.x = -Math.PI / 2;
    pulse.position.y = 0.07;
    pulse.visible = false;
    group.add(body, head, ring, pulse);
    [body, head, ring].forEach((m) => { m.userData = { kind: 'worker', id: w.id }; pickables.push(m); });
    group.visible = false;
    root.add(group);
    o = { group, ring, pulse, body, pos: null, path: [], w, visibleTarget: false };
    workers.set(w.id, o);
    return o;
  }
  function planPath(from, to) {
    const crossing = (from.z < -33 && to.z > -33) || (from.z > -33 && to.z < -33)
      || from.x < -86 || to.x < -86;
    if (!crossing && Math.abs(from.z - to.z) < 5) return [to];
    const aisle = (from.z < -33 || to.z < -33 || from.x < -86 || to.x < -86) ? MAIN_AISLE_Z : (from.z + to.z) / 2;
    const exitX = (p) => (p.x < -86 ? -86 : p.x);
    return [{ x: exitX(from), z: aisle }, { x: exitX(to), z: aisle }, to];
  }

  // ── state ──────────────────────────────────────────────────────────
  let data = initial;
  let layers = { workers: true, flow: true, labels: true, counts: true, readers: true, heat: false };
  let selected = null;
  let stageFocus = null;
  let flowSpeed = {};
  const clock = new THREE.Clock();
  let elapsed = 0;
  let retarget = 0;
  const tmpM = new THREE.Matrix4();
  const tmpQ = new THREE.Quaternion();
  const tmpS = new THREE.Vector3(1, 1, 1);
  const tmpP = new THREE.Vector3();

  function update(d) {
    data = d;
    const lines = d.lines;
    // lines
    lines.forEach((ln, i) => {
      const o = lineObjs[i];
      if (!o) return;
      o.stripMat.color.set(LINE_STATUS_COLOR[ln.status] || '#64748b');
      const running = ln.status === 'Running';
      flowSpeed[ln.id] = running ? Math.max(0.15, Math.min(1.6, (ln.last_hour || ln.rate_target * 0.8) / ln.rate_target)) : 0;
      ln.machines.forEach((mc) => {
        const mesh = o.machineMeshes[mc.code];
        if (!mesh) return;
        const tint = mc.status === 'Breakdown' ? 0xef4444 : mc.status === 'Maintenance' ? 0xf97316
          : mc.health_status === 'Critical' ? 0xf87171 : mc.health_status === 'Warning' ? 0xfbbf24 : 0x000000;
        mesh.material.emissive.setHex(tint);
        mesh.material.emissiveIntensity = tint ? 0.45 : 0;
      });
      const bad = ln.machines.find((mc) => mc.status === 'Breakdown' || mc.status === 'Maintenance');
      o.beacon.visible = !!bad || ln.status === 'Breakdown';
      if (o.beacon.visible) {
        const mesh = bad ? o.machineMeshes[bad.code] : o.machineMeshes[ln.machines[1]?.code];
        o.beacon.material = (bad?.status === 'Maintenance' || ln.status === 'Maintenance') ? M.beaconAmber : M.beaconRed;
        if (mesh) o.beacon.position.set(mesh.position.x, 3.6, o.z);
      }
      o.bun.visible = running;
      const eff = ln.efficiency_pct;
      const tone = ln.status !== 'Running' ? (ln.status === 'Breakdown' ? 'danger' : ln.status === 'Idle' ? 'muted' : 'warning')
        : eff >= 95 ? 'success' : eff >= 85 ? 'warning' : 'danger';
      o.lineLabel.element.innerHTML = `<span class="tw-dot tw-${tone}"></span><b>${ln.code.split('-')[1]}</b>`
        + `<span class="tw-line-sub">${fmt(ln.output_today)} · ${eff == null ? '—' : `${Math.round(eff)}%`}</span>`
        + (ln.status !== 'Running' ? `<span class="tw-line-status tw-${tone}">${ln.status}</span>` : '');
      o.lineLabel.element.classList.toggle('is-selected', selected === ln.id);
      const s = ln.stages;
      o.chips.filling.element.innerHTML = `<i>Filled</i>${fmt(s.filling.today)}`;
      o.chips.sewing.element.innerHTML = `<i>Sewn</i>${fmt(s.sewing.today)}<em>WIP ${fmt(s.sewing.wip)}</em>`;
      o.chips.bagging.element.innerHTML = `<i>Bagged</i>${fmt(s.bagging.today)}<em>WIP ${fmt(s.bagging.wip)}</em>`;
      o.chips.packing.element.innerHTML = `<i>To pack</i>${fmt(s.packing.queue)}`;
    });
    // packing cells
    packChips.forEach((c, p) => {
      const q = [lines[p * 2], lines[p * 2 + 1]].filter(Boolean).reduce((a, ln) => a + (ln.stages.packing.queue || 0), 0);
      c.element.innerHTML = `<i>Cell ${p + 1}</i>${fmt(q)} queued`;
    });
    // racks
    let n = 0;
    Object.entries(RACK.zoneZ).forEach(([zone, z]) => {
      const slots = d.warehouse.racks?.[zone] || [];
      slots.forEach(([bay, rack, level]) => {
        if (n >= MAX_PALLETS) return;
        const x = rackX(bay, rack);
        const y = (level - 1) * RACK.levelH;
        tmpM.compose(tmpP.set(x, y + 0.82, z), tmpQ.identity(), tmpS.set(1, 1, 1));
        palletMesh.setMatrixAt(n, tmpM);
        tmpM.compose(tmpP.set(x, y + 0.16, z), tmpQ.identity(), tmpS.set(1, 1, 1));
        palletBase.setMatrixAt(n, tmpM);
        n += 1;
      });
      const cap = 48;
      rackChips[zone].element.innerHTML = `<i>Zone ${zone}</i>${slots.length}/${cap}`;
    });
    palletMesh.count = n;
    palletBase.count = n;
    palletMesh.instanceMatrix.needsUpdate = true;
    palletBase.instanceMatrix.needsUpdate = true;
    // docks
    let staged = 0;
    d.docks.forEach((dk) => {
      const o = dockObjs[dk.dock];
      if (!o) return;
      o.truck.visible = !!dk.truck;
      const pctLoaded = dk.loaded_pct == null ? '' : ` · ${Math.round(dk.loaded_pct)}%`;
      o.label.element.innerHTML = dk.truck
        ? `<b>${dk.dock}</b> ${dk.status}${pctLoaded}<span>${dk.customer || ''}</span>`
        : `<b>${dk.dock}</b> free${dk.next ? `<span>next ${dk.next.at?.slice(11, 16)} · ${dk.next.customer || ''}</span>` : ''}`;
      o.label.element.classList.toggle('is-active', !!dk.truck);
      if (dk.truck && dk.status !== 'Loaded') {
        const k = Math.min(6, Math.ceil(((dk.order_qty || 0) - (dk.loaded_qty || 0)) / 600));
        for (let i = 0; i < k && staged < 64; i += 1) {
          tmpM.compose(tmpP.set(DOCK_WALL_X - 4.6 + (i % 2) * 1.6, 0.55, o.z - 1.6 + Math.floor(i / 2) * 1.35), tmpQ.identity(), tmpS.set(1, 1, 1));
          stagedMesh.setMatrixAt(staged, tmpM);
          staged += 1;
        }
      }
    });
    stagedMesh.count = staged;
    stagedMesh.instanceMatrix.needsUpdate = true;
    Object.values(recvTrucks).forEach((t) => { t.truck.visible = false; });
    (d.receiving || []).forEach((s) => { if (recvTrucks[s.dock]) recvTrucks[s.dock].truck.visible = true; });
    // raw materials
    const bales = Math.min(120, Math.round((d.raw?.fibre_kg || 0) / 900));
    for (let i = 0; i < bales; i += 1) {
      const col = i % 10;
      const row = Math.floor(i / 10) % 6;
      const lv = Math.floor(i / 60);
      tmpM.compose(tmpP.set(-83 + col * 1.7, 0.6 + lv * 1.22, -28 + row * 1.5), tmpQ.identity(), tmpS.set(1, 1, 1));
      baleMesh.setMatrixAt(i, tmpM);
    }
    baleMesh.count = bales;
    baleMesh.instanceMatrix.needsUpdate = true;
    const shellsN = Math.min(60, Math.round((d.raw?.shells || 0) / 9000));
    for (let i = 0; i < shellsN; i += 1) {
      tmpM.compose(tmpP.set(-83 + (i % 10) * 1.6, 0.5 + Math.floor(i / 30) * 1.02, -14 + (Math.floor(i / 10) % 3) * 1.4 + 22), tmpQ.identity(), tmpS.set(1, 1, 1));
      shellMesh.setMatrixAt(i, tmpM);
    }
    shellMesh.count = shellsN;
    shellMesh.instanceMatrix.needsUpdate = true;
    rawChip.element.innerHTML = `<i>Fibre on hand</i>${fmt(d.raw?.fibre_kg)} kg<em>${fmt(d.raw?.shells)} shells</em>`;
    // QA hold & palletizing
    const nb = Math.min(40, d.packing.blocked_cartons || 0);
    for (let i = 0; i < nb; i += 1) {
      tmpM.compose(tmpP.set(QA.x0 + 1.4 + (i % 5) * 1.6, 0.5 + Math.floor(i / 20) * 1.02, QA.z0 + 3 + (Math.floor(i / 5) % 4) * 1.8), tmpQ.identity(), tmpS.set(1, 1, 1));
      blockedMesh.setMatrixAt(i, tmpM);
    }
    blockedMesh.count = nb;
    blockedMesh.instanceMatrix.needsUpdate = true;
    const np = Math.min(60, (d.palletizing.open_pallets || 0) + (d.palletizing.pending_putaway || 0));
    for (let i = 0; i < np; i += 1) {
      const h = 0.6 + hash(i + 3) * 1.4;
      tmpM.compose(tmpP.set(PALLET.x0 + 1.6 + (i % 3) * 2.9, h / 2 + 0.1, PALLET.z0 + 2 + Math.floor(i / 3) * 2.6), tmpQ.identity(), tmpS.set(1, h, 1));
      openPalletMesh.setMatrixAt(i, tmpM);
    }
    openPalletMesh.count = np;
    openPalletMesh.instanceMatrix.needsUpdate = true;
    // heat
    const zoneMin = Object.fromEntries((d.rfid.zones || []).map((z) => [z.zone, z.minutes]));
    const maxZ = Math.max(1, ...Object.values(zoneMin));
    Object.entries(heatDiscs).forEach(([name, disc]) => {
      const v = (zoneMin[name] || 0) + (name === 'Break room' ? zoneMin['Break overrun'] || 0 : 0);
      disc.visible = layers.heat && v > 0;
      const r = 1.5 + 7 * Math.sqrt(v / maxZ);
      disc.scale.set(r, r, r);
      disc.material.color.setHex(v / maxZ > 0.6 ? 0xef4444 : v / maxZ > 0.3 ? 0xf97316 : 0xf59e0b);
    });
    // workers
    const seen = new Set();
    d.workers.forEach((w) => {
      const o = ensureWorker(w);
      o.w = w;
      seen.add(w.id);
    });
    workers.forEach((o, id) => { if (!seen.has(id)) { o.group.visible = false; o.pos = null; } });
    applyWorkerTargets();
    applyFocus();
  }

  function applyWorkerTargets() {
    const lineIdx = {};
    data.lines.forEach((ln, i) => { lineIdx[ln.id] = i; });
    workers.forEach((o) => {
      const w = o.w;
      const tgt = workerTarget(w, { lineIndex: lineIdx, lines: data.lines, t: elapsed });
      if (!tgt) { o.group.visible = false; o.pos = null; return; }
      const long = w.state === 'Away' && (w.minutes_in_state || 0) >= (data.rfid.alert_threshold_min || 10);
      const color = tgt.noRead ? STATE_COLOR.NoRead : long ? STATE_COLOR.Long : (STATE_COLOR[w.state] || STATE_COLOR.Station);
      o.ring.material.color.set(color);
      o.pulse.visible = long;
      o.group.visible = layers.workers;
      o.body.material = roleMat(w.role);
      if (!o.pos) {
        o.pos = { x: tgt.x, z: tgt.z };
        o.path = [];
        o.group.position.set(tgt.x, 0, tgt.z);
      } else {
        const last = o.path.length ? o.path[o.path.length - 1] : o.pos;
        if (Math.hypot(last.x - tgt.x, last.z - tgt.z) > 0.3) o.path = planPath(o.pos, tgt);
      }
    });
  }

  function applyFocus() {
    const focusRoles = stageFocus ? STAGE_ROLES[stageFocus] || [] : null;
    lineObjs.forEach((o) => {
      const isSel = selected === o.id;
      o.sel.material.opacity = isSel ? 0.16 : 0;
      o.sel.material.transparent = true;
      const showCounts = layers.counts && (isSel || zoomLevel() > 1.7 || (selected == null && stageFocus));
      Object.entries(o.chips).forEach(([k, c]) => {
        c.visible = layers.labels && showCounts && (!stageFocus || STAGE_CHIP[stageFocus] === k || isSel);
      });
      o.lineLabel.visible = layers.labels;
    });
    workers.forEach((o) => {
      if (!o.pos) return;
      const dim = focusRoles && !focusRoles.includes(o.w.role);
      o.group.visible = layers.workers;
      o.group.scale.setScalar(dim ? 0.6 : focusRoles ? 1.25 : 1);
    });
    packChips.forEach((c) => { c.visible = layers.labels && layers.counts; });
    Object.values(rackChips).forEach((c) => { c.visible = layers.labels; });
    staticLabels.forEach((l) => { l.visible = layers.labels; });
    readers.visible = layers.readers;
    rawChip.visible = layers.labels && layers.counts;
  }

  function zoomLevel() {
    if (view === 'top') return ortho.zoom;
    return controls ? 260 / Math.max(40, camera.position.distanceTo(controls.target)) : 1;
  }
  let lastZoomBucket = null;
  function onZoomChange() {
    const b = zoomLevel() > 1.7;
    if (b !== lastZoomBucket) { lastZoomBucket = b; applyFocus(); }
  }

  // ── animation ──────────────────────────────────────────────────────
  let raf = null;
  function animate() {
    raf = requestAnimationFrame(animate);
    const dt = Math.min(0.05, clock.getDelta());
    elapsed += dt;
    // pillows: outgoing conveyor flow + WIP buffers + packing queues
    let n = 0;
    const put = (x, y, z, ry = 0) => {
      if (n >= MAX_PILLOWS) return;
      tmpQ.setFromAxisAngle(UP, ry);
      tmpM.compose(tmpP.set(x, y, z), tmpQ, tmpS.set(1, 1, 1));
      pillows.setMatrixAt(n, tmpM);
      n += 1;
    };
    if (layers.flow && data) {
      data.lines.forEach((ln, i) => {
        const z = lineZ(i);
        const sp = flowSpeed[ln.id] || 0;
        const len = SX.convB - SX.convA;
        const k = 7;
        for (let j = 0; j < k; j += 1) {
          const ph = ((elapsed * sp * 0.12) + j / k) % 1;
          put(SX.convA + ph * len, 1.13, z);
        }
        // pillow leaving the filler / going through sewing (one in each station when running)
        if (sp > 0) {
          const cyc = (elapsed * sp * 0.5) % 1;
          put(SX.filler + 1.3 + cyc * 1.6, 1.0, z);
        }
        const sw = Math.min(10, Math.round(ln.stages.sewing.wip || 0));
        for (let j = 0; j < sw; j += 1) put(SX.sewBuf - 0.45 + (j % 2) * 0.9, 0.98 + Math.floor(j / 2) * 0.3, z + (j % 4 < 2 ? -0.35 : 0.35), 0.05 * (j % 3));
        [SX.sew1, SX.sew2].forEach((x) => { if (sp > 0 || ln.status === 'Running') put(x + 0.6, 1.0, z - 0.25, 0.3); });
        const bw = Math.min(10, Math.round(ln.stages.bagging.wip || 0));
        for (let j = 0; j < bw; j += 1) put(SX.bagBuf - 0.4 + (j % 2) * 0.8, 0.98 + Math.floor(j / 2) * 0.3, z, 0.05 * (j % 3));
        const q = Math.min(14, Math.round((ln.stages.packing.queue || 0) / 2));
        for (let j = 0; j < q; j += 1) put(SX.convB + 0.2 - (j % 7) * 1.0, 1.13 + Math.floor(j / 7) * 0.3, z);
        // fibre bun shimmer
        const lo = lineObjs[i];
        if (lo && lo.bun.visible) lo.bun.material.opacity = 0.65 + 0.2 * Math.sin(elapsed * 3 + i);
      });
    }
    pillows.count = n;
    pillows.instanceMatrix.needsUpdate = true;
    // workers walk their planned path
    workers.forEach((o) => {
      if (!o.pos || !o.group.visible) return;
      if (o.path.length) {
        const tgt = o.path[0];
        const dx = tgt.x - o.pos.x;
        const dz = tgt.z - o.pos.z;
        const dist = Math.hypot(dx, dz);
        const step = 9 * dt;
        if (dist <= step) { o.pos = { x: tgt.x, z: tgt.z }; o.path.shift(); } else {
          o.pos = { x: o.pos.x + dx / dist * step, z: o.pos.z + dz / dist * step };
          o.group.rotation.y = Math.atan2(dx, dz);
        }
        o.group.position.set(o.pos.x, Math.abs(Math.sin(elapsed * 12)) * 0.08, o.pos.z);
      }
      if (o.pulse.visible) {
        const s = 1 + 0.35 * ((elapsed * 1.4) % 1);
        o.pulse.scale.set(s, s, s);
        o.pulse.material.opacity = 0.85 * (1 - ((elapsed * 1.4) % 1));
      }
    });
    // readers pulse
    if (layers.readers) {
      const s = 1 + 0.25 * Math.sin(elapsed * 2.4);
      readerMeshes.forEach((r) => r.scale.set(s, s, s));
    }
    // beacons blink
    lineObjs.forEach((o) => { if (o.beacon.visible) o.beacon.material.emissiveIntensity = 0.6 + Math.abs(Math.sin(elapsed * 5)) * 1.4; });
    // roaming roles re-target periodically
    retarget += dt;
    if (retarget > 3.5) { retarget = 0; applyWorkerTargets(); }
    controls?.update();
    renderer.render(scene, camera);
    labelRenderer.render(scene, camera);
  }

  // ── picking & hover ────────────────────────────────────────────────
  const ray = new THREE.Raycaster();
  const ndc = new THREE.Vector2();
  let downAt = null;
  function hit(ev) {
    const r = renderer.domElement.getBoundingClientRect();
    ndc.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
    ray.setFromCamera(ndc, camera);
    const hits = ray.intersectObjects(pickables, false).filter((h) => {
      let o = h.object;
      while (o) { if (o.visible === false) return false; o = o.parent; }
      return true;
    });
    const w = hits.find((h) => h.object.userData.kind === 'worker');
    return (w || hits[0])?.object.userData || null;
  }
  function onMove(ev) {
    const u = hit(ev);
    renderer.domElement.style.cursor = u ? 'pointer' : (view === 'top' ? 'grab' : 'default');
    const r = renderer.domElement.getBoundingClientRect();
    if (!u) { callbacks.onHover?.(null); return; }
    let info = { ...u };
    if (u.kind === 'worker') info.worker = data.workers.find((w) => w.id === u.id);
    if (u.kind === 'line') info.line = data.lines.find((ln) => ln.id === u.id);
    if (u.kind === 'dock') info.dock = data.docks.find((d) => d.dock === u.dock);
    callbacks.onHover?.(info, ev.clientX - r.left, ev.clientY - r.top);
  }
  function onDown(ev) { downAt = { x: ev.clientX, y: ev.clientY }; }
  function onUp(ev) {
    if (!downAt || Math.hypot(ev.clientX - downAt.x, ev.clientY - downAt.y) > 5) return;
    const u = hit(ev);
    if (!u) { callbacks.onSelectLine?.(null); return; }
    if (u.kind === 'worker') callbacks.onSelectWorker?.(u.id);
    else if (u.kind === 'line') callbacks.onSelectLine?.(u.id, u.station);
    else callbacks.onSelectArea?.(u);
  }
  function onLeave() { callbacks.onHover?.(null); }
  renderer.domElement.addEventListener('pointermove', onMove);
  renderer.domElement.addEventListener('pointerdown', onDown);
  renderer.domElement.addEventListener('pointerup', onUp);
  renderer.domElement.addEventListener('pointerleave', onLeave);

  const ro = new ResizeObserver(() => {
    renderer.setSize(W(), H());
    labelRenderer.setSize(W(), H());
    if (view === 'top') {
      const z = ortho.zoom;
      fitOrtho();
      ortho.zoom = z;
      ortho.updateProjectionMatrix();
    } else {
      persp.aspect = W() / H();
      persp.updateProjectionMatrix();
    }
  });
  ro.observe(container);

  setView('top');
  update(initial);
  animate();

  return {
    update,
    setView: (v) => setView(v),
    resetView: () => setView(view),
    zoomBy: (f) => {
      if (view === 'top') { ortho.zoom = Math.max(0.8, Math.min(7, ortho.zoom * f)); ortho.updateProjectionMatrix(); onZoomChange(); } else {
        const dir = camera.position.clone().sub(controls.target).multiplyScalar(1 / f);
        camera.position.copy(controls.target).add(dir);
      }
    },
    setLayers: (l) => { layers = { ...layers, ...l }; applyFocus(); applyWorkerTargets(); update(data); },
    select: (id) => {
      selected = id;
      applyFocus();
      update(data);
      if (id != null && view === 'top') {
        const i = lineObjs.findIndex((o) => o.id === id);
        if (i >= 0 && ortho.zoom < 1.6) {
          const z = lineObjs[i].z;
          controls.target.set(-18, 0, z);
          ortho.position.set(-18, 300, z + 0.01);
          ortho.zoom = 2.1;
          ortho.updateProjectionMatrix();
          onZoomChange();
        }
      }
    },
    focusStage: (k) => { stageFocus = k; applyFocus(); },
    dispose: () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      renderer.domElement.removeEventListener('pointermove', onMove);
      renderer.domElement.removeEventListener('pointerdown', onDown);
      renderer.domElement.removeEventListener('pointerup', onUp);
      renderer.domElement.removeEventListener('pointerleave', onLeave);
      controls?.dispose();
      scene.traverse((o) => {
        if (o.geometry) o.geometry.dispose();
        if (o.material) (Array.isArray(o.material) ? o.material : [o.material]).forEach((m) => m.dispose());
      });
      renderer.dispose();
      renderer.forceContextLoss?.();
      container.removeChild(renderer.domElement);
      container.removeChild(labelRenderer.domElement);
    },
  };
}

const UP = new THREE.Vector3(0, 1, 0);
const STAGE_ROLES = {
  raw: ['Forklift operator'], fibre: ['Filler'], filling: ['Filler'], sewing: ['Sewer'], bagging: ['Bagger'],
  packing: ['Packaging operator'], qa: ['Supervisor'], pallet: ['Forklift operator'], fg: ['Warehouse associate'],
  dispatch: ['Warehouse associate', 'Forklift operator'],
};
const STAGE_CHIP = { fibre: 'filling', filling: 'filling', sewing: 'sewing', bagging: 'bagging', packing: 'packing' };
