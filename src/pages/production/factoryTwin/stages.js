/* Pillow-plant process stages for the production digital twin.
   The flow follows the real plant: fibre bales are opened and blended, carded
   into a bun on Garnett lines (or blown on blow lines), blown into shells,
   sewn shut, bagged/compressed, counted into cartons by camera, palletized,
   racked and shipped. `flow` points at the matching step of
   /api/twin/facility → flow[]; `region` is the floor area in scene metres. */

export const STAGES = [
  {
    key: 'raw', flow: 'raw', name: 'Receiving & bale store', short: 'Receiving', icon: 'truck', color: '#f59e0b',
    region: [-132, -100, -62, 58],
    desc: 'Polyester fibre bales (7D conjugated, 15D hollow siliconized), shredded foam and printed pillow shells arrive at the west docks, are sampled and staged line-side.',
    actions: ['Sample each inbound fibre lot for denier and silicone finish before release.', 'Keep two hours of bales staged at every line so the openers never starve.'],
  },
  {
    key: 'opening', flow: 'fibre', name: 'Fibre opening & blending', short: 'Opening', icon: 'layers', color: '#fb923c',
    region: [-99, -77, -46, 42], machineStages: ['Mixing', 'Flapper Processing'],
    desc: 'Bale feeders with spiked lattices tease the compressed bales apart; the fibre opener and flapper unit blend the denier mix and blow it through ducts to the cards.',
    actions: ['Check lattice spikes and feed-roll gap at the start of each shift.', 'Blend ratio follows the SKU fill spec; verify the mix before a changeover.'],
  },
  {
    key: 'carding', flow: 'fibre', name: 'Garnett carding & bun formation', short: 'Carding', icon: 'settings', color: '#22d3ee',
    region: [-77, -44, -46, 42], machineStages: ['Bun Formation'],
    desc: 'Garnett cards comb the fibre into a fine web; the chain conveyor and cross-lapper build the fibre bun. Blow lines use a blow-fan unit and cyclone instead.',
    actions: ['Watch main-motor vibration and bearing temperature (S!AP flags drift days ahead).', 'Clean card wire and doffer each week to keep the web even.'],
  },
  {
    key: 'filling', flow: 'filling', name: 'Pillow filling', short: 'Filling', icon: 'download', color: '#14b8a6',
    region: [-44, -31, -46, 42], machineStages: ['Pillow Filling'],
    desc: 'The filling head blows a weighed charge of fibre into each shell through the nozzle; fill weight is checked against the SKU specification.',
    actions: ['Run a fill-weight check every 30 minutes against the SKU spec (± 3 %).', 'Inspect the blower nozzle for fibre build-up when weight drifts.'],
  },
  {
    key: 'sewing', flow: 'sewing', name: 'Sewing & closing', short: 'Sewing', icon: 'edit', color: '#ec4899',
    region: [-31, -13, -46, 42],
    desc: 'Two sewers per line close the filled shell, finish the seam and attach the law label and brand tag.',
    actions: ['Keep a relief sewer on call when a badge shows the station empty for > 10 min.', 'Check needle and thread tension at each SKU change.'],
  },
  {
    key: 'bagging', flow: 'bagging', name: 'Bagging & compression', short: 'Compression', icon: 'box', color: '#eab308',
    region: [-13, -1, -46, 42],
    desc: 'Pillows are polybagged, compressed or vacuum roll-packed and labelled with the retail UPC before the outfeed conveyor.',
    actions: ['Verify the UPC on the first bag of every lot.', 'Inspect the press platen seal when compression loss is reported.'],
  },
  {
    key: 'packing', flow: 'packing', name: 'CV carton packing', short: 'CV packing', icon: 'camera', color: '#a78bfa',
    region: [-1, 34, -46, 42],
    desc: 'Overhead cameras count pillows into each carton against the SKU spec. Approved cartons get the GS1-128 label; a wrong count blocks the carton.',
    actions: ['Recount blocked cartons within 30 minutes so the line queue does not grow.', 'Clean camera lenses at shift start; check lighting if confidence drops.'],
  },
  {
    key: 'qa', flow: 'qa', name: 'QA hold & rework', short: 'QA hold', icon: 'shield', color: '#ef4444',
    region: [36, 54, -64, -50],
    desc: 'Cartons with a wrong pillow count wait in the hold cage for recount, rework or rejection by a supervisor.',
    actions: ['Work the hold queue oldest-first.', 'Feed recurring defect types back to the packing cell.'],
  },
  {
    key: 'pallet', flow: 'pallet', name: 'Palletizing & stretch wrap', short: 'Palletizing', icon: 'grid', color: '#f97316',
    region: [36, 56, -48, 46],
    desc: 'Approved cartons are built into pallets, stretch-wrapped, tagged and sent for put-away by forklift.',
    actions: ['Put away full pallets within 20 minutes to keep the build area clear.', 'Check pallet pattern against the customer routing guide.'],
  },
  {
    key: 'fg', flow: 'fg', name: 'Finished-goods racks', short: 'FG racks', icon: 'list', color: '#3b82f6',
    region: [58, 110, -50, 42],
    desc: 'High-bay racks hold finished pallets by zone, bay, rack and level until they are allocated and picked for customer orders.',
    actions: ['Pick FIFO by pallet date.', 'Re-slot slow movers to upper levels when the racks pass 85 % full.'],
  },
  {
    key: 'dispatch', flow: 'dispatch', name: 'Dispatch docks', short: 'Dispatch', icon: 'send', color: '#22c55e',
    region: [111, 130, -56, 52],
    desc: 'Orders are picked, staged in lanes and loaded at the east docks against truck appointments.',
    actions: ['Stage the next appointment two hours before arrival.', 'Avoid detention: load the earliest appointment first.'],
  },
];

export const SUPPORT = [
  { key: 'utilities', name: 'Utilities · compressors & dust collection', color: '#e879f9', region: [-132, -88, -88, -68],
    desc: 'Air compressors and receivers feed the pneumatic fibre transport; the cyclone and bag-house collect fibre dust from the opening and carding lines.' },
  { key: 'maint', name: 'Maintenance crib', color: '#fdba74', region: [-6, 14, -64, -53],
    desc: 'Spare bearings, belts and seals; technicians are dispatched from here to S!AP work orders.' },
  { key: 'amenities', name: 'Break room · canteen · lockers', color: '#94a3b8', region: [-98, -10, -64, -53],
    desc: 'Scheduled breaks are read by RFID at the door and never counted as time away from the station.' },
];

export const STAGE_BY_KEY = Object.fromEntries(STAGES.map((s) => [s.key, s]));
export const LINE_STAGES = ['opening', 'carding', 'filling', 'sewing', 'bagging', 'packing'];
export const MACHINE_STAGE = { Mixing: 'opening', 'Flapper Processing': 'opening', 'Bun Formation': 'carding', 'Pillow Filling': 'filling' };
const ROLE_STAGE = {
  Filler: 'filling', Sewer: 'sewing', Bagger: 'bagging', 'Packaging operator': 'packing',
  'Forklift operator': 'pallet', 'Warehouse associate': 'fg',
};
const ALERT_STAGE = {
  material: 'raw', chain_material: 'raw', inbound_delay: 'raw', line_perf: 'filling', changeover: 'filling',
  idle_capacity: 'filling', workforce_low: 'sewing', cartons_blocked: 'qa', putaway: 'pallet', capacity: 'fg',
  ship_risk: 'dispatch', detention: 'dispatch',
};

/** machine code → stage key, from the live facility payload */
export function machineStageIndex(fac) {
  const out = {};
  (fac?.lines || []).forEach((ln) => ln.machines.forEach((m) => { out[m.code] = MACHINE_STAGE[m.stage]; }));
  return out;
}

/** Group S!AP advisories and plant alerts by stage. */
export function risksByStage(fac, advisories = [], alerts = []) {
  const mIdx = machineStageIndex(fac);
  const out = Object.fromEntries(STAGES.map((s) => [s.key, []]));
  const push = (k, item) => { if (out[k]) out[k].push(item); };
  advisories.forEach((a) => {
    let k = null;
    if (a.machine?.code) k = mIdx[a.machine.code] || 'carding';
    else if ((a.category || '').startsWith('Workforce')) {
      const role = Object.keys(ROLE_STAGE).find((r) => (a.title || '').includes(`(${r})`));
      k = role ? ROLE_STAGE[role] : null;
    } else if (/fill/i.test(a.category || '') || /fill/i.test(a.title || '')) k = 'filling';
    else if (/spare|maintenance capacity/i.test(a.category || '')) k = 'carding';
    else if (/energy/i.test(a.category || '')) k = 'carding';
    if (k) push(k, { id: a.id, source: 'S!AP', severity: a.severity, title: a.title, text: a.recommendation || a.summary, category: a.category });
  });
  alerts.forEach((al) => {
    const prefix = (al.key || '').split(':')[0];
    let k = ALERT_STAGE[prefix];
    if (prefix === 'machine_health' || prefix === 'machine_down') {
      const code = ((al.title || '').match(/M-\d+/) || [])[0];
      k = mIdx[code] || 'carding';
    }
    if (!k && al.stage === 'quality') k = 'packing';
    if (!k && al.stage === 'dispatch') k = 'dispatch';
    if (!k && al.stage === 'warehouse') k = 'fg';
    if (!k && al.stage === 'procurement') k = 'raw';
    if (k) push(k, { id: `al-${al.id}`, source: 'Alert', severity: al.severity, title: al.title, text: al.recommended_action || al.description });
  });
  const rank = { critical: 0, high: 1, warning: 2, medium: 3, info: 4, low: 5 };
  Object.values(out).forEach((l) => l.sort((a, b) => (rank[a.severity] ?? 9) - (rank[b.severity] ?? 9)));
  return out;
}

const num = (n) => (n == null ? '—' : Math.round(n).toLocaleString('en-US'));

/** Live status + headline stat for each stage (the stage strip). */
export function stageStatus(fac, risks) {
  const flow = Object.fromEntries((fac?.flow || []).map((f) => [f.key, f]));
  const lines = fac?.lines || [];
  const running = lines.filter((l) => l.status === 'Running').length;
  const crit = (k) => (risks[k] || []).some((r) => r.severity === 'critical');
  const warn = (k) => (risks[k] || []).some((r) => ['warning', 'high'].includes(r.severity));
  const sumStage = (key, f) => lines.reduce((s, l) => s + (f(l.stages?.[key]) || 0), 0);
  const machinesIn = (stage) => lines.flatMap((l) => l.machines.filter((m) => MACHINE_STAGE[m.stage] === stage));
  const out = {};
  STAGES.forEach((s) => {
    const f = flow[s.flow] || {};
    let stat = '';
    let status = running ? 'Active' : 'Idle';
    switch (s.key) {
      case 'raw': stat = `${num(fac?.raw?.fibre_kg)} kg fibre`; status = 'Normal'; break;
      case 'opening': stat = `${num(f.today)} kg opened`; break;
      case 'carding': stat = `${num(sumStage('fibre', (x) => x?.kg_per_hour))} kg/h bun`; break;
      case 'filling': stat = `${num(f.today)} pillows`; break;
      case 'sewing': stat = `${num(f.today)} · WIP ${num(f.now)}`; break;
      case 'bagging': stat = `${num(f.today)} bagged`; break;
      case 'packing': stat = `${num(fac?.packing?.cartons_today)} cartons`; break;
      case 'qa': stat = `${num(fac?.packing?.blocked_cartons)} blocked`; status = fac?.packing?.blocked_cartons ? 'Watch' : 'Normal'; break;
      case 'pallet': stat = `${num(fac?.palletizing?.open_pallets)} open pallets`; status = 'Active'; break;
      case 'fg': stat = `${fac?.warehouse?.utilization_pct ?? '—'}% full`; status = (fac?.warehouse?.utilization_pct || 0) > 90 ? 'Watch' : 'Normal'; break;
      case 'dispatch': {
        const at = (fac?.docks || []).filter((d) => d.truck).length;
        stat = `${at}/${(fac?.docks || []).length} docks busy`;
        status = at ? 'Active' : 'Normal';
        break;
      }
      default: break;
    }
    const mc = machinesIn(s.key);
    if (mc.some((m) => m.health_status === 'Critical' || m.status === 'Breakdown')) status = 'Alert';
    else if (crit(s.key)) status = 'Alert';
    else if (mc.some((m) => m.health_status === 'Warning') || warn(s.key)) status = status === 'Idle' ? 'Idle' : 'Watch';
    out[s.key] = { status, stat, flow: f, risks: risks[s.key] || [] };
  });
  return out;
}

export const STATUS_TONE = { Active: 'info', Normal: 'success', Watch: 'warning', Alert: 'danger', Idle: 'neutral' };
