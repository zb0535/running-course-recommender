// navigate.html 의 <script>를 가짜 DOM/지도/GPS 위에서 그대로 실행해 GPS 내비를 재현한다.
// 사용: node nav_harness.js <navigate.html 경로>
const fs = require('fs');
const vm = require('vm');

const html = fs.readFileSync(process.argv[2], 'utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).join('\n');

function el() {
  return {
    hidden: false, textContent: '', disabled: false, dataset: { v: 'medium' }, style: {},
    classList: { add() {}, remove() {}, toggle() {} }, addEventListener() {}, querySelectorAll() { return []; },
  };
}

function makeContext() {
  const els = {};
  const spoken = [];
  const geo = { success: null, cleared: false };
  class Marker { constructor() { this.ll = null; } setLngLat(ll) { this.ll = ll; return this; } addTo() { return this; } getLngLat() { return this.ll; } }
  class LngLatBounds { extend() { return this; } }
  class Map {
    on(evt, cb) { if (evt === 'load') cb(); }
    addSource() {} addLayer() {} fitBounds() {} easeTo() {} remove() {}
  }
  const ctx = {
    console, Math, JSON, Date, Number, String, Array, Object, Infinity, encodeURIComponent,
    performance: { now: () => 0 },
    setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0,
    fetch: () => Promise.reject(new Error('offline test stub')),
    document: {
      getElementById: id => (els[id] ||= el()),
      querySelectorAll: () => [], querySelector: () => el(), createElement: () => el(),
    },
    navigator: {
      geolocation: {
        watchPosition: (ok) => { geo.success = ok; geo.cleared = false; return 1; },
        clearWatch: () => { geo.cleared = true; },
      },
    },
    SpeechSynthesisUtterance: class { constructor(t) { this.text = t; } },
    maplibregl: { Map, Marker, LngLatBounds },
  };
  ctx.window = ctx;
  ctx.speechSynthesis = { speak: u => spoken.push(u.text), cancel() {} };
  vm.createContext(ctx);
  vm.runInContext(script, ctx);
  return { ctx, els, spoken, geo };
}

// ---- 코스 만들기 (위경도 <-> 미터 근사) ----
const LAT0 = 34.0, LNG0 = 127.0;
const M_LAT = 111320, M_LNG = 111320 * Math.cos(LAT0 * Math.PI / 180);
const toLL = (x, y) => [LAT0 + y / M_LAT, LNG0 + x / M_LNG];

function polyline(pointsXY, stepM) {
  const out = [toLL(...pointsXY[0])];
  for (let i = 1; i < pointsXY.length; i++) {
    const [x0, y0] = pointsXY[i - 1], [x1, y1] = pointsXY[i];
    const n = Math.max(1, Math.round(Math.hypot(x1 - x0, y1 - y0) / stepM));
    for (let k = 1; k <= n; k++) out.push(toLL(x0 + (x1 - x0) * k / n, y0 + (y1 - y0) * k / n));
  }
  return out;
}

function stepsAt(ctx, path, marks) {
  const cum = ctx.cumulativeDistances(path);
  return marks.map(([frac, desc]) => {
    const target = frac * cum[cum.length - 1];
    let i = cum.findIndex(c => c >= target - 0.01);
    return { lat: path[i][0], lng: path[i][1], description: desc, turn_type: 12, cum_m: cum[i] };
  });
}

const COURSES = {
  // 실시간 생성 순환 코스: 출발점 = 도착점 (400m 정사각형 한 바퀴 1.6km)
  loop: (ctx) => {
    const path = polyline([[0, 0], [400, 0], [400, 400], [0, 400], [0, 0]], 20);
    return { path, steps: stepsAt(ctx, path, [[0.25, '좌회전'], [0.5, '좌회전'], [0.75, '좌회전'], [1, '도착']]) };
  },
  // DB 편도 코스를 왕복으로: 1km 가서 길 건너편 인도(12m 옆)로 되돌아옴
  outAndBack: (ctx) => {
    const path = polyline([[0, 0], [1000, 0], [1000, 12], [0, 12], [0, 0]], 25);
    return { path, steps: stepsAt(ctx, path, [[0.25, '직진'], [0.499, '반환점 유턴'], [0.75, '직진'], [1, '도착']]) };
  },
  // 실제 Tmap 응답처럼 끝점이 출발점과 미세하게 다른 순환 코스 (도착점이 출발점 4m 옆)
  loopTmap: (ctx) => {
    const path = polyline([[0, 0], [400, 0], [400, 400], [0, 400], [-3, 2.5]], 20);
    return { path, steps: stepsAt(ctx, path, [[0.25, '좌회전'], [0.5, '좌회전'], [0.75, '좌회전'], [1, '도착']]) };
  },
  // DB 편도 코스 + Tmap 복귀 경로: 복귀는 길 건너편 인도(12m 옆)에서 끝난다
  outAndBackTmap: (ctx) => {
    const path = polyline([[0, 0], [1000, 0], [1000, 12], [0, 12]], 25);
    return { path, steps: stepsAt(ctx, path, [[0.25, '직진'], [0.499, '반환점 유턴'], [0.75, '직진'], [1, '도착']]) };
  },
  // 경유지까지 같은 길로 들어갔다 나오는 순환(롤리팝): 처음·마지막 300m가 같은 길
  lollipop: (ctx) => {
    const path = polyline([[0, 0], [300, 0], [600, 0], [600, 300], [300, 300], [300, 0], [0, 0]], 30);
    return { path, steps: stepsAt(ctx, path, [[0.3, '좌회전'], [0.6, '좌회전'], [1, '도착']]) };
  },
};

let seed = 7;
const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647 - 0.5) * 2;

function run(name, { noiseM = 6, fixEveryM = 4, startOffsetM = 0, accuracy = 8 } = {}) {
  const { ctx, els, spoken, geo } = makeContext();
  const course = COURSES[name](ctx);
  course.name = name; course.region = 'test'; course.distance_km = 1;
  const cum = ctx.cumulativeDistances(course.path);
  const total = cum[cum.length - 1];

  const progressLog = [];
  const orig = ctx.updateProgressUI;
  ctx.updateProgressUI = (lat, lng, d, ...rest) => { progressLog.push(d); return orig(lat, lng, d, ...rest); };

  ctx.drawCourse(course);
  ctx.startNavigationGPS(course);

  let arrivedAtTrueM = null, maxAhead = 0, maxBehind = 0, firstFixProgress = null;
  for (let s = 0; s <= total + 0.01 && !geo.cleared; s += fixEveryM) {
    const p = ctx.stateAtFraction(course.path, cum, Math.min(1, s / total));
    const lat = p.lat + (rand() * noiseM + startOffsetM) / M_LAT;
    const lng = p.lng + rand() * noiseM / M_LNG;
    geo.success({ coords: { latitude: lat, longitude: lng, speed: 3, accuracy } });
    const d = progressLog[progressLog.length - 1];
    if (firstFixProgress === null) firstFixProgress = d;
    maxAhead = Math.max(maxAhead, d - s);
    maxBehind = Math.max(maxBehind, s - d);
    if (!els.arrivedBadge.hidden && arrivedAtTrueM === null) arrivedAtTrueM = s;
  }
  const spokenSteps = spoken.filter(t => !t.includes('앞 ') && !t.includes('도착했습니다'));
  return {
    course: name, totalM: Math.round(total),
    firstFixProgressM: Math.round(firstFixProgress),
    arrivedAtTrueM: arrivedAtTrueM === null ? '도착 안 함' : Math.round(arrivedAtTrueM),
    maxAheadM: Math.round(maxAhead), maxBehindM: Math.round(maxBehind),
    spokenTurns: spokenSteps.join(' > '),
    status: els.status.textContent,
  };
}

function runOffStart() {
  // 출발점에서 150m 떨어진 곳에서 안내 시작 후 가만히 서 있기
  const { ctx, els, geo } = makeContext();
  const course = COURSES.loop(ctx);
  course.name = 'loop'; course.region = 'test'; course.distance_km = 1.6;
  ctx.drawCourse(course);
  ctx.startNavigationGPS(course);
  const [lat, lng] = toLL(-150, 200);
  for (let i = 0; i < 20; i++) geo.success({ coords: { latitude: lat, longitude: lng, speed: 0, accuracy: 8 } });
  return { case: '출발점 150m 밖에서 20회 대기', arrived: !els.arrivedBadge.hidden, status: els.status.textContent };
}

function runInaccurateFirstFix() {
  // 첫 위치가 기지국 기반이라 오차 ±300m, 하필 경로 끝(=출발점) 근처로 찍힘
  const { ctx, els, geo } = makeContext();
  const course = COURSES.loop(ctx);
  course.name = 'loop'; course.region = 'test'; course.distance_km = 1.6;
  ctx.drawCourse(course);
  ctx.startNavigationGPS(course);
  const [lat, lng] = toLL(0, 1);
  geo.success({ coords: { latitude: lat, longitude: lng, speed: 0, accuracy: 300 } });
  return { case: '첫 위치 오차 ±300m', arrived: !els.arrivedBadge.hidden, status: els.status.textContent };
}

function runShortcut() {
  // 순환 코스에서 첫 코너를 대각선으로 가로질러 두 번째 변 중간(약 600m 지점)에 합류
  const { ctx, els, geo } = makeContext();
  const course = COURSES.loop(ctx);
  course.name = 'loop'; course.region = 'test'; course.distance_km = 1.6;
  const log = [];
  const orig = ctx.updateProgressUI;
  ctx.updateProgressUI = (lat, lng, d, ...r) => { log.push(d); return orig(lat, lng, d, ...r); };
  ctx.drawCourse(course);
  ctx.startNavigationGPS(course);
  const fix = (x, y) => { const [lat, lng] = toLL(x, y); geo.success({ coords: { latitude: lat, longitude: lng, speed: 3, accuracy: 8 } }); };
  for (let x = 0; x <= 100; x += 5) fix(x, 0);                  // 경로 따라 100m
  for (let t = 0; t <= 1; t += 0.05) fix(100 + 300 * t, 200 * t); // 대각선 샛길 (경로 밖)
  for (let y = 200; y <= 260; y += 5) fix(400, y);              // 합류 후 60m
  return { case: '코너 가로질러 약 600m 지점 합류', finalProgressM: Math.round(log[log.length - 1]), expectedM: 660, arrived: !els.arrivedBadge.hidden };
}

function immediateArrivalRate(name, trials = 50) {
  // 출발점에 서서(GPS 오차 ±6m) '실제 위치로 안내 시작'을 누른 처음 3번의 위치 수신
  let hit = 0;
  for (let t = 0; t < trials; t++) {
    const { ctx, els, geo } = makeContext();
    const course = COURSES[name](ctx);
    course.name = name; course.region = 'test'; course.distance_km = 1;
    ctx.drawCourse(course);
    ctx.startNavigationGPS(course);
    for (let k = 0; k < 3 && !geo.cleared; k++) {
      const [lat, lng] = toLL(rand() * 6, rand() * 6 + (name.startsWith('outAndBack') ? 6 : 0));
      geo.success({ coords: { latitude: lat, longitude: lng, speed: 0, accuracy: 8 } });
    }
    if (!els.arrivedBadge.hidden) hit++;
  }
  return `${hit}/${trials}`;
}

console.log('출발하자마자 도착 판정 비율:', {
  loop: immediateArrivalRate('loop'), loopTmap: immediateArrivalRate('loopTmap'),
  outAndBack: immediateArrivalRate('outAndBack'), outAndBackTmap: immediateArrivalRate('outAndBackTmap'),
  lollipop: immediateArrivalRate('lollipop'),
});
const results = ['loop', 'loopTmap', 'outAndBack', 'outAndBackTmap', 'lollipop'].map(n => run(n))
  .concat([run('loopTmap', { fixEveryM: 15, noiseM: 10 })]);
console.table(results.map(r => ({ ...r, status: undefined, spokenTurns: undefined })));
for (const r of results) console.log(`- ${r.course} 음성: ${r.spokenTurns || '(없음)'}`);
for (const f of [runOffStart, runInaccurateFirstFix, runShortcut]) {
  try { console.log(f()); } catch (e) { console.log(f.name, 'ERROR', e.message); }
}
