#!/usr/bin/env node
/*
 * Behavioural tests for the dashboard's history-refresh loop and live tail.
 *
 * The client is one 745 KB HTML file with no build step, so there is nothing to
 * import. Instead this lifts the functions under test straight out of
 * static/dashboard.html and runs them in a `vm` context against a fake clock —
 * the shipped source is never modified, patched or duplicated, so the test can
 * only ever pass by the real code behaving.
 *
 * Covers the four ways the refresh loop was found to stop refreshing:
 *   R2  a throw out of a synchronous renderer killed the chain permanently
 *   R3  a flapping SSE reconnect reset the timer faster than it could fire
 *   R4  LIVE_ON tracked connection events, so a dead-but-open stream froze
 *       the cadence and silently stopped the fleet poll entirely
 *   R7  a range switch never re-armed the timer with the new period
 * plus the live-tail merge that keeps the newest chart point moving between
 * history polls.
 *
 * Run: node tests/js/test_refresh_loop.js
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

// DASHBOARD_HTML lets the suite be pointed at another build of the page — used
// to confirm these tests genuinely fail against the version that shipped the bug.
const DASH = process.env.DASHBOARD_HTML
  || path.join(__dirname, '..', '..', 'static', 'dashboard.html');
const SRC = fs.readFileSync(DASH, 'utf8');

// ── extraction ───────────────────────────────────────────────────────────────
// Top-level declarations in dashboard.html start at column 0 and a function's
// closing brace is the next `}` at column 0. That is enough structure to slice
// out exactly the definitions under test without parsing the whole file.
function takeFunction(name) {
  const start = SRC.indexOf(`\nfunction ${name}(`);
  if (start < 0) throw new Error(`could not find function ${name}() in dashboard.html`);
  const end = SRC.indexOf('\n}', start);
  if (end < 0) throw new Error(`could not find the end of ${name}()`);
  return SRC.slice(start + 1, end + 2);
}

function takeLine(re, what) {
  const m = SRC.match(re);
  if (!m) throw new Error(`could not find ${what} in dashboard.html`);
  return m[0];
}

// ── fake clock ───────────────────────────────────────────────────────────────
let NOW = 1700000000000, SEQ = 0;
const timers = new Map();
const setTimeout_ = (fn, ms) => { const id = ++SEQ; timers.set(id, { at: NOW + ms, fn }); return id; };
const clearTimeout_ = (id) => { timers.delete(id); };
function advance(ms) {
  const end = NOW + ms;
  for (;;) {
    let next = null;
    for (const [id, t] of timers) if (t.at <= end && (!next || t.at < next.t.at)) next = { id, t };
    if (!next) break;
    NOW = next.t.at;
    timers.delete(next.id);
    next.t.fn();          // deliberately unguarded: an escaping throw is a failure
  }
  NOW = end;
}
const pending = () => timers.size;

// ── context ──────────────────────────────────────────────────────────────────
function build(opts = {}) {
  const ctx = {
    // mutable state the shipped code reads and writes
    LIVE_ON: opts.liveOn || false,
    HIST_TIMER: null,
    HIST_NEXT_AT: 0,
    LIVE_LAST_FRAME: 0,
    LIVE_ES: opts.liveEs === undefined ? {} : opts.liveEs,
    D: opts.D || null,
    GPUC: opts.GPUC || null,
    LIVE_TAIL: { b: null, n: 0 },
    GPU_TAIL: { b: null, n: 0 },
    TAB: opts.tab || 'overview',
    CURRENT_HOST: opts.host || 'local',
    LOCAL_ONLY_TABS: new Set(['models', 'experiments', 'containers']),
    // counters
    loadDataCalls: 0, loadFleetCalls: 0, loadHealthCalls: 0, openStreamCalls: 0,
    // environment
    Math, JSON, Set, Array, Object,
    Date: { now: () => NOW },
    setTimeout: setTimeout_, clearTimeout: clearTimeout_,
    document: { hidden: false, getElementById: () => ({ checked: opts.auto !== false }) },
  };
  // Captured rather than printed: the loop is supposed to log what it caught,
  // and the R2 test asserts on that instead of letting a stack trace spam CI.
  ctx.errors = [];
  ctx.console = { log: () => {}, warn: () => {}, error: (...a) => ctx.errors.push(a.map(String).join(' ')) };
  ctx.autoOn = () => { const el = ctx.document.getElementById('auto'); return !el || el.checked; };
  ctx.loadData = () => { ctx.loadDataCalls++; };
  ctx.loadHealth = () => { ctx.loadHealthCalls++; };
  ctx.loadFleet = () => { ctx.loadFleetCalls++; };
  ctx.openLiveStream = () => { ctx.openStreamCalls++; ctx.LIVE_ES = {}; };
  ctx.renderHostTab = () => {};
  ctx.renderNetwork = () => {};
  ctx.renderSecurity = () => {};
  ctx.renderGpuTab = () => {};
  ctx.renderLocalOnlyNotice = () => {};
  ctx.renderDiskIo = () => {
    if (opts.diskIoThrows) throw new TypeError("Cannot read properties of undefined (reading 'items')");
  };
  vm.createContext(ctx);
  vm.runInContext([
    takeFunction('liveStale'),
    takeFunction('noteLiveFrame'),
    takeFunction('historyPeriod'),
    takeFunction('refreshHistory'),
    takeFunction('scheduleHistory'),
    takeLine(/^const _tailMean=.*$/m, 'the _tailMean helper'),
    takeFunction('_tailPush'),
    takeFunction('_tailSlot'),
    takeFunction('mergeLiveTail'),
    takeFunction('mergeGpuTail'),
  ].join('\n\n'), ctx);
  return ctx;
}

// ── assertions ───────────────────────────────────────────────────────────────
let failures = 0, checks = 0;
function check(label, cond, detail) {
  checks++;
  if (cond) { console.log(`  ok   ${label}`); return; }
  failures++;
  console.log(`  FAIL ${label}${detail ? '\n         ' + detail : ''}`);
}
function suite(name) { console.log(`\n${name}`); }

function reset() { NOW = 1700000000000; timers.clear(); }

// ── R2: a throwing renderer must not kill the chain ──────────────────────────
suite('R2  a sync throw inside refreshHistory() must not stop the loop');
{
  reset();
  const c = build({ tab: 'diskio', diskIoThrows: true, liveOn: false });
  c.scheduleHistory();
  advance(20000);
  const afterThrow = c.loadDataCalls;
  check('the tick still ran', afterThrow >= 1, `refreshes=${afterThrow}`);
  check('a timer is still pending after the throw', pending() === 1, `pending=${pending()}`);
  advance(600000);
  check('still refreshing 10 minutes later',
    c.loadDataCalls >= 40, `refreshes=${c.loadDataCalls} (expected ~41 at 15s)`);
  check('the failure was logged, not swallowed',
    c.errors.length > 0 && c.errors[0].includes('refreshHistory failed'),
    `errors=${JSON.stringify(c.errors.slice(0, 1))}`);
}

// ── R3: reconnect flapping must not starve the timer ─────────────────────────
suite('R3  a 3s SSE reconnect flap must not starve the 15s poll');
{
  reset();
  const c = build({ liveOn: false });
  c.scheduleHistory();
  for (let i = 0; i < 100; i++) { advance(3000); c.LIVE_ON = false; c.scheduleHistory(); }
  check('refreshes still happened during 300s of flapping',
    c.loadDataCalls >= 15, `refreshes=${c.loadDataCalls} (expected ~20)`);
  check('no timer stacking', pending() === 1, `pending=${pending()}`);
}

// ── R3b: a re-arm must never push the fire time further out ──────────────────
suite('R3b a re-arm must never delay an already-pending tick');
{
  reset();
  const c = build({ liveOn: false });
  c.scheduleHistory();                       // 15s
  advance(14000);
  c.LIVE_ON = true; c.D = { bucket_sec: 60 };  // would compute a 60s period
  c.scheduleHistory();
  advance(1100);
  check('the pending 15s tick still fired on time', c.loadDataCalls === 1,
    `refreshes=${c.loadDataCalls}`);
}

// ── R3c: a shorter period must be adopted immediately ────────────────────────
suite('R3c a genuinely sooner period must replace the pending tick');
{
  reset();
  const c = build({ liveOn: true, D: { bucket_sec: 60 } });
  c.scheduleHistory();                       // 60s
  advance(1000);
  c.LIVE_ON = false;                         // stream lost -> 15s
  c.scheduleHistory();
  advance(15100);
  check('re-armed at the shorter period', c.loadDataCalls === 1, `refreshes=${c.loadDataCalls}`);
}

// ── R4: watchdog on a dead-but-open stream ───────────────────────────────────
suite('R4  a stream that stops delivering must be demoted');
{
  reset();
  const c = build({ liveOn: true, D: { bucket_sec: 60, fast_interval: 2 } });
  c.noteLiveFrame();                          // one frame arrives, then silence
  check('period is the slow cadence while live', c.historyPeriod() === 60000,
    `period=${c.historyPeriod()}`);
  check('fleet is not polled while genuinely live', (() => {
    c.refreshHistory(); return c.loadFleetCalls === 0;
  })(), `loadFleet=${c.loadFleetCalls}`);
  advance(60000);                             // 60s of silence, > max(6*2s, 45s)
  check('watchdog reports the stream stale', c.liveStale() === true);
  c.refreshHistory();
  check('LIVE_ON demoted to false', c.LIVE_ON === false);
  check('period fell back to 15s', c.historyPeriod() === 15000, `period=${c.historyPeriod()}`);
  check('fleet polling resumed', c.loadFleetCalls === 1, `loadFleet=${c.loadFleetCalls}`);
}

// ── R4b: a live stream must not be demoted ───────────────────────────────────
suite('R4b a stream still delivering frames must stay live');
{
  reset();
  const c = build({ liveOn: true, D: { bucket_sec: 60, fast_interval: 2 } });
  for (let i = 0; i < 30; i++) { c.noteLiveFrame(); advance(2000); }
  check('watchdog leaves a healthy stream alone', c.liveStale() === false);
  c.refreshHistory();
  check('LIVE_ON still true', c.LIVE_ON === true);
  check('fleet still left to the stream', c.loadFleetCalls === 0, `loadFleet=${c.loadFleetCalls}`);
}

// ── R7: range switch re-arms with the new period ─────────────────────────────
suite('R7  a range switch must re-arm the timer with the new period');
{
  reset();
  const c = build({ liveOn: true, D: { bucket_sec: 240 } });   // 24h -> 60s cap
  c.scheduleHistory();
  advance(1000);
  c.D = { bucket_sec: 10 };                    // user picks 1h
  c.scheduleHistory(true);                     // what the .rb handler now does
  advance(15100);
  check('refreshed at the new 15s period, not the old 60s one',
    c.loadDataCalls === 1, `refreshes=${c.loadDataCalls}`);
}

// ── stream self-heal ─────────────────────────────────────────────────────────
suite('R8  a refresh tick must reopen a stream that failed terminally');
{
  reset();
  const c = build({ liveOn: false, liveEs: null });
  c.refreshHistory();
  check('openLiveStream() was called when LIVE_ES was null', c.openStreamCalls === 1,
    `openStreamCalls=${c.openStreamCalls}`);
}

// ── live tail ────────────────────────────────────────────────────────────────
function mkD(lastBucket, bucketSec) {
  const labels = [], mkArr = () => [];
  const n = 5;
  for (let i = n - 1; i >= 0; i--) labels.push(lastBucket - i * bucketSec);
  const arr = (v) => Array(n).fill(v);
  return {
    bucket_sec: bucketSec, interval: 10, labels,
    total: {
      cpu: arr(10), ram_used: arr(1000), ram_total: arr(4000), load1: arr(1), ctemp: arr(40),
      util: arr(5), mem: arr(500), mempk: arr(500), power: arr(20), temp: arr(30),
    },
  };
}

suite('tail  the newest bucket keeps moving between history polls');
{
  reset();
  const bk = 60, last = 1786788000;
  const D = mkD(last, bk);
  const c = build({ D });
  // a frame 20s into the open bucket
  const ok = c.mergeLiveTail({ fast_ts: last + 20, host: { cpu: 50, ram_used: 2000, ram_total: 4000, load1: 3, ctemp: 55 }, gpu_avail: false });
  check('merge reported success', ok === true);
  check('label window did not grow', D.labels.length === 5, `len=${D.labels.length}`);
  check('newest label unchanged (same bucket)', D.labels[4] === last, `last=${D.labels[4]}`);
  check('cpu moved toward the live value', D.total.cpu[4] > 10 && D.total.cpu[4] <= 50,
    `cpu=${D.total.cpu[4]}`);
  check('a GPU-less box grew no GPU value', D.total.util[4] === 5, `util=${D.total.util[4]}`);
}

suite('tail  a new bucket appends and slides the window');
{
  reset();
  const bk = 60, last = 1786788000;
  const D = mkD(last, bk);
  const oldest = D.labels[0];
  const c = build({ D });
  c.mergeLiveTail({ fast_ts: last + bk + 5, host: { cpu: 70 }, gpu_avail: false });
  check('window length is unchanged', D.labels.length === 5, `len=${D.labels.length}`);
  check('a new bucket was appended', D.labels[4] === last + bk, `last=${D.labels[4]}`);
  check('the oldest bucket was dropped', D.labels[0] !== oldest);
  check('the new point took the live value', D.total.cpu[4] === 70, `cpu=${D.total.cpu[4]}`);
  check('every series slid together', D.total.power.length === 5, `len=${D.total.power.length}`);
}

suite('tail  VRAM is a step metric and must not be averaged away');
{
  reset();
  const bk = 60, last = 1786788000;
  const D = mkD(last, bk);
  const c = build({ D });
  // a model loads: VRAM jumps from 500 MB to 20 GB inside the open bucket
  c.mergeLiveTail({ fast_ts: last + 20, host: {}, gpu_avail: true, util: 90, mem_used: 20000, power: 250, temp: 70 });
  check('VRAM shows the loaded value, not a mean', D.total.mem[4] === 20000, `mem=${D.total.mem[4]}`);
  check('the VRAM peak was captured', D.total.mempk[4] === 20000, `mempk=${D.total.mempk[4]}`);
  check('util was averaged, not slammed', D.total.util[4] > 5 && D.total.util[4] < 90,
    `util=${D.total.util[4]}`);
}

suite('tail  an old frame is ignored, and a remote host is left alone');
{
  reset();
  const bk = 60, last = 1786788000;
  const D = mkD(last, bk);
  const c = build({ D });
  const before = D.total.cpu[4];
  const ok = c.mergeLiveTail({ fast_ts: last - 5 * bk, host: { cpu: 99 }, gpu_avail: false });
  check('a frame older than the series is rejected', ok === false);
  check('nothing was written', D.total.cpu[4] === before, `cpu=${D.total.cpu[4]}`);

  const GPUC = {
    has_gpu: true, host: 'local', bucket_sec: bk, interval: 10,
    labels: D.labels.slice(),
    combined: { util: [1, 1, 1, 1, 1], vram: [10, 10, 10, 10, 10], power: [1, 1, 1, 1, 1], temp_max: [1, 1, 1, 1, 1] },
    cards: [], now_pooled: {},
  };
  const c2 = build({ GPUC, host: 'vader' });
  check('a remote host does not get the hub\'s live frame',
    c2.mergeGpuTail({ fast_ts: last + 20, gpu_avail: true, util: 99, mem_used: 9999, gpus: [] }) === false);
  check('the remote series is untouched', GPUC.combined.util[4] === 1, `util=${GPUC.combined.util[4]}`);
}

suite('tail  the GPU tab rides the same fold');
{
  reset();
  const bk = 60, last = 1786788000;
  const GPUC = {
    has_gpu: true, host: 'local', bucket_sec: bk, interval: 10,
    labels: [last - 4 * bk, last - 3 * bk, last - 2 * bk, last - bk, last],
    combined: { util: [1, 1, 1, 1, 1], vram: [10, 10, 10, 10, 10], vram_total: [5120, 5120, 5120, 5120, 5120], power: [5, 5, 5, 5, 5], temp_max: [30, 30, 30, 30, 30], fan_max: [40, 40, 40, 40, 40] },
    cards: [{ idx: 0, series: { util: [1, 1, 1, 1, 1], vram: [10, 10, 10, 10, 10], temp: [30, 30, 30, 30, 30] }, now: {} }],
    now_pooled: { util: 1, mem_used: 10, power: 5, temp_max: 30 },
  };
  const c = build({ GPUC, host: 'local' });
  const ok = c.mergeGpuTail({
    fast_ts: last + 20, gpu_avail: true, util: 95, mem_used: 4800, mem_total: 5120,
    power: 70, temp: 72, gpus: [{ idx: 0, util: 95, mem_used: 4800, mem_total: 5120, power: 70, temp: 72, fan: 80 }],
  });
  check('merge reported success', ok === true);
  check('pooled VRAM took the latest value', GPUC.combined.vram[4] === 4800,
    `vram=${GPUC.combined.vram[4]}`);
  check('per-card VRAM took the latest value', GPUC.cards[0].series.vram[4] === 4800,
    `vram=${GPUC.cards[0].series.vram[4]}`);
  check('hottest card is a max, not a mean', GPUC.combined.temp_max[4] === 72,
    `temp_max=${GPUC.combined.temp_max[4]}`);
  check('KPI tiles were refreshed from the frame', GPUC.now_pooled.util === 95,
    `util=${GPUC.now_pooled.util}`);
}

// ── result ───────────────────────────────────────────────────────────────────
console.log(`\n${checks - failures}/${checks} checks passed`);
if (failures) { console.error(`${failures} check(s) FAILED`); process.exit(1); }
