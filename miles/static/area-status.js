// Plain-words status for an area row from /api/areas: shared by the Areas page
// and the area picker. Include after nav.js.

// Import phase -> [step of AREA_STEPS, label], in the order the job runs them.
const AREA_STEPS = 4;
const AREA_PHASES = {
  'waiting-for-download': [1, 'Waiting for another download'],
  downloading: [1, 'Downloading map data'],
  queued: [2, 'Waiting for another import'],
  tiles: [2, 'Cutting the area'],
  elevation: [3, 'Fetching elevation'],
  network: [4, 'Building the trail network'],
  familiarity: [4, 'Matching your activities'],
};

// area_id -> when the current phase was first seen and its progress then, the
// basis for a rate. Estimates only start after a few seconds of observation.
const areaPhaseSeen = new Map();
const AREA_ETA_MIN_SECONDS = 4;

const areaIsBusy = (a) => a.status === 'importing' || a.status === 'new';
const megabytes = (bytes) => Math.round(bytes / 1048576);

function areaProgress(a) {
  if (a.status === 'ready') return { text: 'Ready' };
  if (a.status === 'error') return { text: `Failed: ${a.error ?? 'unknown error'}` };
  const [step, label] = AREA_PHASES[a.phase] ?? [null, a.status === 'new' ? 'Queued' : 'Starting'];
  const out = { text: label, step: step && `step ${step} of ${AREA_STEPS}` };
  if (!(a.total > 1)) return out;

  out.fraction = Math.min(1, a.done / a.total);
  out.detail = a.phase === 'downloading'
    ? `${megabytes(a.done)} of ${megabytes(a.total)} MB`
    : `${a.done} of ${a.total}`;
  const now = Date.now() / 1000;
  let seen = areaPhaseSeen.get(a.area_id);
  if (seen?.phase !== a.phase) {
    seen = { phase: a.phase, at: now, done: a.done };
    areaPhaseSeen.set(a.area_id, seen);
  }
  if (now - seen.at >= AREA_ETA_MIN_SECONDS && a.done > seen.done) {
    const seconds = (a.total - a.done) * (now - seen.at) / (a.done - seen.done);
    out.eta = seconds < 90 ? 'under a minute left' : `about ${Math.round(seconds / 60)} min left`;
  }
  return out;
}

function areaStatusText(a) {
  const p = areaProgress(a);
  return p.fraction == null ? p.text : `${p.text}, ${Math.round(p.fraction * 100)}%`;
}
