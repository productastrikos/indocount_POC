/* Plant floor layout used by the facility twin (metres, top view).
   x runs with the material flow (receiving → lines → packing → racks → docks),
   z runs across the building (negative z is the amenities side). */

export const BOUNDS = { x0: -106, x1: 104, z0: -52, z1: 42 };

export const LINE_Z0 = -27.4;
export const LINE_DZ = 8.6;
export const lineZ = (i) => LINE_Z0 + i * LINE_DZ;
export const OPERATOR_DZ = 2.35;   // operators stand on the aisle side of their line
export const TECH_DZ = -2.35;      // technicians work from the other side

// station x positions along every line
export const SX = {
  feed: -50.5, opener: -46, flapper: -40.5, card: -28, cardLen: 10.5, filler: -18.5,
  sewBuf: -15, sew1: -11.2, sew2: -7.3, bagBuf: -3.9, bagger: -0.4, convA: 1.8, convB: 16.4,
};
export const STAGE_X = { Mixing: SX.opener, 'Flapper Processing': SX.flapper, 'Bun Formation': SX.card, 'Pillow Filling': SX.filler };

export const ZONES = {
  receiving: { x0: -86, x1: -56, z0: -32, z1: 37, label: 'Receiving & raw materials', color: 0x1a2436 },
  hall: { x0: -56, x1: 14, z0: -32, z1: 37, label: 'Production hall · filling & sewing lines', color: 0x152033 },
  packing: { x0: 14, x1: 37, z0: -32, z1: 37, label: 'Packing & QA', color: 0x1a1f36 },
  warehouse: { x0: 37, x1: 76, z0: -32, z1: 37, label: 'Finished goods', color: 0x13233a },
  staging: { x0: 76, x1: 82, z0: -32, z1: 37, label: 'Staging', color: 0x1b2536 },
};

// rooms on the amenities side; RFID readers sit at each doorway
export const ROOMS = {
  'Break room': { x0: -86, x1: -66, z0: -47, z1: -36, color: 0x10304a },
  Canteen: { x0: -64, x1: -42, z0: -47, z1: -36, color: 0x0f3a3a },
  Restroom: { x0: -40, x1: -31, z0: -47, z1: -36, color: 0x2a2440 },
  'Locker room': { x0: -29, x1: -19, z0: -47, z1: -36, color: 0x262a3c },
  'Supervisor desk': { x0: -17, x1: -6, z0: -47, z1: -36, color: 0x23303f },
  'Maintenance crib': { x0: -4, x1: 9, z0: -47, z1: -36, color: 0x3a2a1a },
  'Material store': { x0: -84, x1: -66, z0: 23, z1: 35, color: 0x2c2a18 },
};
export const NO_READ = { x: -97, z: -43, label: 'No read · out of coverage' };
export const MAIN_AISLE_Z = -34;

export const PACK_X = 21;
export const QA = { x0: 26.5, x1: 35.5, z0: -31, z1: -19 };
export const PALLET = { x0: 26.5, x1: 35.5, z0: -16, z1: 35 };

export const RACK = { x0: 40, module: 2.6, levelH: 1.6, depth: 2.4, zoneZ: { A: -22, B: 0, C: 22 } };
export const rackX = (bay, rack) => RACK.x0 + ((bay - 1) * 3 + (rack - 1)) * RACK.module + RACK.module / 2;

export const DOCK_Z = { D1: -22, D2: -6, D3: 10, D4: 26 };
export const DOCK_WALL_X = 82;

/* Zone D: bulk floor lanes (Gaylords) between the rack ends and the dock pads. Bay n sits in front of dock Dn
   ("KNV-D02-LN1" = bay 2, lane 1); each lane runs toward the dock with PER_ROW floor positions, stacked up
   to the lane's capacity (12 pallets = 4 positions × 3 high). */
export const FLOOR = { x0: 72.6, pitch: 1.3, lanePitch: 1.4, firstDz: -3.5, perRow: 4, tierH: 1.15, size: 1.15 };
export const floorLaneZ = (bay, lane) => (DOCK_Z[`D${bay}`] ?? 0) + FLOOR.firstDz - (lane - 1) * FLOOR.lanePitch;
export const FLOOR_X1 = FLOOR.x0 + (FLOOR.perRow - 1) * FLOOR.pitch;
/** Floor position of the i-th pallet (0-based) in a lane. */
export function floorSlot(bay, lane, i) {
  return {
    x: FLOOR.x0 + (i % FLOOR.perRow) * FLOOR.pitch,
    y: Math.floor(i / FLOOR.perRow) * FLOOR.tierH,
    z: floorLaneZ(bay, lane),
  };
}
export const RECV_Z = { R1: -12, R2: 8 };
export const RECV_WALL_X = -86;
// staff exit gate (RFID portal) in the north wall, east of the maintenance crib
export const GATE = { x: 14, z: -49, half: 2.2, label: 'Staff exit gate' };

export const ROLE_COLOR = {
  Filler: '#14b8a6', Sewer: '#ec4899', Bagger: '#f59e0b', 'Packaging operator': '#a78bfa',
  Supervisor: '#e2e8f0', 'Maintenance technician': '#f97316', 'Warehouse associate': '#3b82f6', 'Forklift operator': '#eab308',
};
export const STATE_COLOR = { Station: '#22c55e', Away: '#f59e0b', Long: '#ef4444', Break: '#38bdf8', NoRead: '#94a3b8' };
export const LINE_STATUS_COLOR = { Running: '#22c55e', Changeover: '#f59e0b', Idle: '#64748b', Breakdown: '#ef4444', Maintenance: '#f97316' };

/** Small deterministic hash for seat/jitter placement. */
export function hash(n) {
  let x = (n * 2654435761) % 4294967296;
  x = (x ^ (x >>> 15)) * 2246822519 % 4294967296;
  return (x >>> 0) / 4294967296;
}

/** A seat inside a room rectangle for the k-th occupant (grid, deterministic). */
export function seat(room, id) {
  const cols = Math.max(2, Math.floor((room.x1 - room.x0 - 2) / 1.6));
  const rows = Math.max(2, Math.floor((room.z1 - room.z0 - 2) / 1.6));
  const k = Math.floor(hash(id) * cols * rows);
  return { x: room.x0 + 1.4 + (k % cols) * 1.6, z: room.z0 + 1.4 + (Math.floor(k / cols) % rows) * 1.6 };
}

/** Where a badge is on the floor, from its RFID state and zone. */
export function workerTarget(w, ctx) {
  const { lineIndex, lines, t } = ctx;
  if (w.state === 'Break') return seat(ROOMS[w.zone] || ROOMS['Break room'], w.id);
  if (w.state === 'Away') {
    if ((w.zone || '').startsWith('No read')) {
      return { x: NO_READ.x + (hash(w.id) - 0.5) * 6, z: NO_READ.z + (hash(w.id + 7) - 0.5) * 4, noRead: true };
    }
    const base = (w.zone || '').replace(' (overrun)', '');
    return seat(ROOMS[base] || ROOMS['Locker room'], w.id + 3);
  }
  if (w.state !== 'Station') return null;
  const li = w.line_id != null ? lineIndex[w.line_id] : undefined;
  if (li !== undefined && ['Filler', 'Sewer', 'Bagger'].includes(w.role)) {
    const z = lineZ(li) + OPERATOR_DZ;
    if (w.role === 'Filler') return { x: SX.filler, z };
    if (w.role === 'Bagger') return { x: SX.bagger, z };
    const sewers = lines[li]?.stages?.sewing?.workers || [];
    return { x: sewers.indexOf(w.id) === 1 ? SX.sew2 : SX.sew1, z };
  }
  if (w.role === 'Packaging operator' && li !== undefined) {
    const pair = Math.floor(li / 2);
    return { x: PACK_X + 1.8, z: (lineZ(pair * 2) + lineZ(pair * 2 + 1)) / 2 + 1.4 };
  }
  if (w.role === 'Maintenance technician') {
    const code = (w.zone || '').split(' ')[0];
    if (code.startsWith('M-')) {
      for (let i = 0; i < lines.length; i += 1) {
        const mc = lines[i].machines.find((x) => x.code === code);
        if (mc) return { x: (STAGE_X[mc.stage] ?? SX.card) + (hash(w.id) - 0.5) * 2, z: lineZ(i) + TECH_DZ };
      }
    }
    return seat(ROOMS['Maintenance crib'], w.id);
  }
  // roaming roles patrol inside their RFID zone (position within the zone is cosmetic)
  const phase = Math.floor((t / 14 + hash(w.id) * 10)) % 4;
  if (w.role === 'Supervisor') {
    const pts = [[-48, -31], [-20, 21], [6, -12], [-34, 5]];
    const p = pts[(phase + Math.floor(hash(w.id) * 4)) % 4];
    return { x: p[0], z: p[1] + OPERATOR_DZ + 1.6 };
  }
  if (w.role === 'Warehouse associate') {
    const pts = [[46, -11], [62, 11], [54, -11], [68, 11]];
    const p = pts[(phase + Math.floor(hash(w.id) * 4)) % 4];
    return { x: p[0], z: p[1] };
  }
  if (w.role === 'Forklift operator') {
    const pts = [[31, 0], [58, -11], [79, 10], [44, 11]];
    const p = pts[(phase + Math.floor(hash(w.id) * 4)) % 4];
    return { x: p[0], z: p[1] };
  }
  return { x: -20, z: MAIN_AISLE_Z };
}
