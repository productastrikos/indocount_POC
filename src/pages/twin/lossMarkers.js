/* Loss-prevention (pilferage) case markers for the facility twin.

   createLossLayer(parent, pickables, { onSelect }) draws one pulsing beacon per
   active case at the place on the floor where it was detected (lossPosition maps
   the case's `area` + `area_ref` onto the plant layout). Returns:
     set(cases, lineIndex)   add, update or remove markers in place (cheap when nothing changed)
     tick(t)                 pulse animation
     setVisible(bool)        the Security layer
     select(id|null)         highlight one marker
     positionOf(id)          {x, z} of a marker (to focus the camera on it)
     caseOf(id)              the case behind a marker
     dispose()
   Labels carry the case ref and a short kind only, never a person's name. */
import * as THREE from 'three';
import { CSS2DObject } from 'three/examples/jsm/renderers/CSS2DRenderer.js';
import { SEVERITY_COLOR } from '../../components/loss/lossShared';
import {
  DOCK_WALL_X, DOCK_Z, FLOOR_X1, GATE, PALLET, QA, RACK, RECV_WALL_X, RECV_Z, ROOMS, SX, ZONES, floorLaneZ, lineZ, rackX,
} from './layout';

const SHORT_KIND = {
  fg_cycle_count: 'Count short', exit_gate_tag: 'Gate tag read', after_hours_access: 'After-hours access',
  consumption_variance: 'Fibre variance', receiving_shortage: 'Inbound short', dispatch_mismatch: 'Load ≠ manifest',
  scrap_reconciliation: 'Scrap gap', cctv_unbadged: 'Unbadged entry',
};
export const lossShortKind = (c) => SHORT_KIND[c?.kind] || c?.kind_label || 'Loss case';
export const LOSS_SEV_RANK = { critical: 0, warning: 1, info: 2 };

const POLE_H = 6.5;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (ch) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);
const mid = (r) => ({ x: (r.x0 + r.x1) / 2, z: (r.z0 + r.z1) / 2 });

/** Floor position (metres) of a case, from its layout area and the area_ref hint. */
export function lossPosition(c, lineIndex = {}) {
  const ref = String(c.area_ref || '').trim().toUpperCase();
  switch (c.area) {
    case 'material_store': return mid(ROOMS['Material store']);
    case 'receiving': {
      const r = ref.match(/R(\d)/);
      const z = r ? RECV_Z[`R${r[1]}`] : null;
      return z != null ? { x: RECV_WALL_X + 7, z } : { x: -71, z: -2 };
    }
    case 'line': {
      const i = lineIndex[c.line_id] ?? lineIndex[c.area_ref];
      return i != null ? { x: SX.flapper, z: lineZ(i) + 0.6 } : { x: -21, z: 4 };
    }
    case 'qa': return mid(QA);
    case 'fg': {
      // rack slot "KNV-A03-R2-L1" → zone A, bay 3, rack 2: in the aisle beside that bay
      const rk = ref.match(/([A-Z])(\d{1,2})-R(\d+)-L(\d+)$/);
      if (rk && RACK.zoneZ[rk[1]] != null) return { x: rackX(Number(rk[2]), Number(rk[3])), z: RACK.zoneZ[rk[1]] + RACK.depth / 2 + 1.6 };
      // zone D floor lane "KNV-D02-LN1" → at the dock end of that lane, in front of dock D2
      const fl = ref.match(/D(\d{1,2})-LN(\d+)$/);
      if (fl && DOCK_Z[`D${Number(fl[1])}`] != null) return { x: FLOOR_X1 + 1.6, z: floorLaneZ(Number(fl[1]), Number(fl[2])) };
      return { x: (ZONES.warehouse.x0 + ZONES.warehouse.x1) / 2, z: 11 };
    }
    case 'staging': return mid(PALLET);
    case 'dock': {
      const d = ref.match(/D(?:OCK)?\s*(\d)/);
      const z = d ? DOCK_Z[`D${d[1]}`] : null;
      return z != null ? { x: DOCK_WALL_X - 3, z: z + 1 } : mid(ZONES.staging);
    }
    case 'gate': return { x: GATE.x, z: GATE.z + 3 };
    default: return mid(ZONES.hall);
  }
}

export function createLossLayer(parent, pickables, { onSelect } = {}) {
  const group = new THREE.Group();
  parent.add(group);
  const geo = {
    pole: new THREE.CylinderGeometry(0.09, 0.09, POLE_H, 8),
    lamp: new THREE.OctahedronGeometry(0.8, 0),
    ring: new THREE.RingGeometry(1.05, 1.35, 36),
    disc: new THREE.CircleGeometry(1.05, 36),
    hit: new THREE.CylinderGeometry(1.7, 1.7, POLE_H + 2, 8),
  };
  const hitMat = new THREE.MeshBasicMaterial({ visible: false });
  const markers = new Map();   // case id → marker
  let visible = true;
  let selected = null;

  function create(c) {
    const mats = {
      pole: new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.7 }),
      lamp: new THREE.MeshStandardMaterial({ color: 0xffffff, emissive: 0xffffff, emissiveIntensity: 1.2 }),
      ring: new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.85, side: THREE.DoubleSide, depthWrite: false }),
      disc: new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0.18, side: THREE.DoubleSide, depthWrite: false }),
    };
    const g = new THREE.Group();
    const pole = new THREE.Mesh(geo.pole, mats.pole);
    pole.position.y = POLE_H / 2;
    const lamp = new THREE.Mesh(geo.lamp, mats.lamp);
    lamp.position.y = POLE_H + 0.6;
    const ring = new THREE.Mesh(geo.ring, mats.ring);
    ring.rotation.x = -Math.PI / 2;
    ring.position.y = 0.09;
    const disc = new THREE.Mesh(geo.disc, mats.disc);
    disc.rotation.x = -Math.PI / 2;
    disc.position.y = 0.08;
    const hit = new THREE.Mesh(geo.hit, hitMat);
    hit.position.y = (POLE_H + 2) / 2;
    [hit, lamp].forEach((m) => { m.userData = { kind: 'loss', id: c.id }; pickables.push(m); });
    g.add(pole, lamp, ring, disc, hit);
    const el = document.createElement('div');
    el.className = 'tw-loss';
    el.style.pointerEvents = 'auto';
    el.style.cursor = 'pointer';
    el.addEventListener('pointerdown', (e) => { e.stopPropagation(); onSelect?.(c.id); });
    const lab = new CSS2DObject(el);
    lab.center.set(0.5, 1);   // label sits just above the lamp
    lab.position.y = POLE_H + 1.8;
    g.add(lab);
    group.add(g);
    return { id: c.id, c, g, ring, lamp, lab, mats, pick: [hit, lamp], sig: '', phase: (c.id * 0.618) % 1 };
  }

  function remove(mk) {
    mk.lab.removeFromParent();   // CSS2DObject drops its element on 'removed'
    mk.lab.element.remove();
    group.remove(mk.g);
    mk.pick.forEach((p) => { const i = pickables.indexOf(p); if (i >= 0) pickables.splice(i, 1); });
    Object.values(mk.mats).forEach((m) => m.dispose());
    markers.delete(mk.id);
    if (selected === mk.id) selected = null;
  }

  function apply() {
    group.visible = visible;
    markers.forEach((mk) => {
      mk.lab.visible = visible;   // CSS2D labels ignore their parent's visibility
      mk.lab.element.classList.toggle('is-selected', selected === mk.id);
      mk.g.scale.setScalar(selected === mk.id ? 1.35 : 1);
    });
  }

  function set(cases = [], lineIndex = {}) {
    const seen = new Set();
    const used = {};
    [...cases]
      .sort((a, b) => (LOSS_SEV_RANK[a.severity] ?? 3) - (LOSS_SEV_RANK[b.severity] ?? 3) || a.id - b.id)
      .forEach((c) => {
        const p = lossPosition(c, lineIndex);
        // several cases on one spot fan out around it (most severe stays in the middle)
        const key = `${Math.round(p.x)}:${Math.round(p.z)}`;
        const k = used[key] || 0;
        used[key] = k + 1;
        const x = p.x + (k ? Math.cos(k * 1.26) * 2.6 : 0);
        const z = p.z + (k ? Math.sin(k * 1.26) * 2.6 : 0);
        let mk = markers.get(c.id);
        if (!mk) { mk = create(c); markers.set(c.id, mk); }
        mk.c = c;
        mk.g.position.set(x, 0, z);
        const sig = `${c.ref}|${c.severity}|${c.kind}|${c.status}`;
        if (mk.sig !== sig) {
          mk.sig = sig;
          const col = SEVERITY_COLOR[c.severity] || SEVERITY_COLOR.info;
          Object.values(mk.mats).forEach((m) => m.color.set(col));
          mk.mats.lamp.emissive.set(col);
          mk.lab.element.style.setProperty('--c', col);
          mk.lab.element.innerHTML = `<i></i><b>${esc(c.ref)}</b><span>${esc(lossShortKind(c))}</span>`;
          mk.lab.element.title = `${c.ref} · ${c.kind_label || lossShortKind(c)} (${c.status}) — click to open the case`;
        }
        seen.add(c.id);
      });
    markers.forEach((mk) => { if (!seen.has(mk.id)) remove(mk); });
    apply();
  }

  function tick(t) {
    if (!visible) return;
    markers.forEach((mk) => {
      const ph = (t * 0.9 + mk.phase) % 1;
      mk.ring.scale.setScalar(1 + ph * 1.6);
      mk.mats.ring.opacity = 0.9 * (1 - ph);
      mk.lamp.rotation.y = t * 1.6;
      mk.mats.lamp.emissiveIntensity = 0.7 + Math.abs(Math.sin(t * 4 + mk.phase * 6)) * 1.3;
    });
  }

  return {
    set,
    tick,
    setVisible: (v) => { visible = !!v; apply(); },
    select: (id) => { selected = id ?? null; apply(); },
    positionOf: (id) => { const mk = markers.get(id); return mk ? { x: mk.g.position.x, z: mk.g.position.z } : null; },
    caseOf: (id) => markers.get(id)?.c || null,
    dispose: () => {
      [...markers.values()].forEach(remove);
      group.removeFromParent();
      Object.values(geo).forEach((g) => g.dispose());
      hitMat.dispose();
    },
  };
}
