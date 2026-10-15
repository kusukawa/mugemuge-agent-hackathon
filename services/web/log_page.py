"""「記録」ページ（長期の記録: 時刻・値・写真の組み合わせ）。外部ライブラリなし、インラインの JS と SVG だけ。

- 一覧: 記録ごとのカード（状態・期間・行数・写真・最新の値・最新の写真）
- 詳細: 範囲の切り替え、最新の値、物理量ごとの時系列グラフ（間引いた区間は最小〜最大の帯）、写真の列、
  時刻を合わせると「その時刻の写真と値」を並べて見せる（スライダー・グラフのクリック・サムネイル・コマ送り再生）、CSV、停止
- 文字列は textContent で入れる。配色はチャットのグラフと同じ検証済みのパレット
"""
from __future__ import annotations

import html
import json
from datetime import datetime, timedelta, timezone

from mugemuge_agentd.policy import fmt_ms

JST = timezone(timedelta(hours=9))
STATUS = {"running": "記録中", "completed": "完了", "stopped": "停止", "estop": "非常停止で終了", "error": "エラーで終了",
          "deadman": "終了", "interrupted": "中断（デーモンの再起動・電源断）",
          "unbound": "部品の登録が外れたため終了"}


def esc(x) -> str:
    return html.escape(str(x))


def jst(ms: int | None) -> str:
    return datetime.fromtimestamp(ms / 1000, JST).strftime("%m/%d %H:%M") if ms else "—"


def dur(ms: int) -> str:
    m = ms // 60_000
    return f"{m // 1440}日{m % 1440 // 60}時間" if m >= 1440 else f"{m // 60}時間{m % 60}分" if m >= 60 else f"{max(1, m)}分"


LOG_CSS = """
[hidden]{display:none!important}
.viz-root{--surface-1:#fcfcfb;--grid:#e6e4df;--axis:#8a8984;--series-1:#2a78d6;--series-2:#eb6834}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{--surface-1:#1a1a19;--grid:#33332f;
 --axis:#8f8e88;--series-1:#3987e5;--series-2:#d95926}}
:root[data-theme="dark"] .viz-root{--surface-1:#1a1a19;--grid:#33332f;--axis:#8f8e88;--series-1:#3987e5;--series-2:#d95926}
main{max-width:1200px}
.logs{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.logcard{display:flex;flex-direction:column;gap:6px;text-decoration:none;color:inherit}
.logcard img{width:100%;aspect-ratio:4/3;object-fit:cover;border-radius:8px;background:var(--chip)}
.logcard .nm{font-weight:700;font-size:16px}.vals{display:flex;gap:10px;flex-wrap:wrap;font-size:13px}
.vals b{font-size:18px;font-variant-numeric:tabular-nums}
.ranges{display:inline-flex;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.ranges button{border:0;border-radius:0;padding:6px 12px;font-size:13px}
.ranges button[aria-pressed="true"]{background:var(--accent);color:#fff}
.warn{border-left:4px solid #fab219;padding:4px 10px;margin:6px 0}.warn::before{content:"⚠ "}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;margin:10px 0}
.tile{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px}
.tile .tl{font-size:12px;color:var(--muted)}.tile .tv{font:600 28px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace;
 font-variant-numeric:tabular-nums}.tile .tu{font-size:13px;color:var(--muted);margin-left:4px}
.tchart{background:var(--surface-1);border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin:10px 0;position:relative}
.tchart h4{margin:0 0 2px;font-size:14px}.tchart svg{display:block;max-width:100%;height:auto;touch-action:none}
.legend{display:flex;gap:14px;font-size:12px;color:var(--muted);margin:2px 0}
.legend span::before{content:"";display:inline-block;width:14px;height:2px;margin-right:6px;vertical-align:middle;background:var(--k)}
.tip{position:absolute;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:8px;
 padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);display:none;min-width:140px}
.tip .row{display:flex;gap:8px;align-items:center}.tip i{display:inline-block;width:12px;height:2px;background:var(--k)}
.strip{display:flex;gap:8px;overflow-x:auto;padding:6px 2px 10px}
.strip button{flex:none;padding:0;border:2px solid transparent;border-radius:8px;background:none;width:120px}
.strip button[aria-current="true"]{border-color:var(--accent)}
.strip img{width:100%;aspect-ratio:4/3;object-fit:cover;border-radius:6px;display:block;background:var(--chip)}
.strip span{font-size:11px;color:var(--muted)}
.scrub{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:14px;align-items:start}
@media (max-width:760px){.scrub{grid-template-columns:minmax(0,1fr)}}
.scrub .big{width:100%;aspect-ratio:4/3;object-fit:contain;background:#111;border-radius:10px}
.scrub .when{font:600 20px/1.3 ui-monospace,SFMono-Regular,Menlo,monospace}
.scrub input[type=range]{width:100%}
.scrub .at{font-size:14px}.scrub .at b{font-size:22px;font-variant-numeric:tabular-nums}
.nophoto{display:flex;align-items:center;justify-content:center;color:#aaa;font-size:13px}
"""


def list_page(device_id: str, device_name: str, logs: list[dict], latest: dict[str, dict], csrf: str) -> str:
    cards = []
    for lg in logs:
        st = lg.get("status", "running")
        args = lg.get("args") or {}
        vals = "".join(f"<span>{esc(k)} <b>{esc(v)}</b></span>" for k, v in latest.get(lg["log_id"], {}).items())
        img = (f'<img loading="lazy" alt="最新の写真" src="/ui/devices/{esc(device_id)}/blobs/{esc(lg["last_photo"]["blob_id"])}">'
               if lg.get("last_photo") else "")
        cards.append(f"""<a class="card logcard" href="/logs/{esc(device_id)}/{esc(lg['log_id'])}">
{img}<div class="row" style="justify-content:space-between"><span class="nm">{esc(lg.get('name', lg['log_id']))}</span>
<span class="chip">{'● ' if st == 'running' else ''}{esc(STATUS.get(st, st))}</span></div>
<div class="meta">{esc(jst(lg.get('started_ts')))} から ・ {esc(args.get('interval_ms', 0) // 1000)}秒ごと ・
予定 {esc(dur(args.get('duration_ms', 0)))} ・ {esc(lg.get('rows', 0))}行 ・ 写真 {esc(lg.get('photos', 0))}枚</div>
<div class="vals">{vals}</div></a>""")
    body = f"""<header><div><a href="/">← むげむげ</a><h1>記録 ・ {esc(device_name)}</h1></div></header>
<p class="meta">長期の記録は、エージェントとのチャットで「一晩、5分ごとの写真つきで室温を記録して」のように頼むと始まります。
通信が切れても Pi に貯めておき、つながったら届きます。写真は90日で自動的に消えます。</p>
<div class="logs">{''.join(cards) or '<p class="meta">まだ記録はありません</p>'}</div>"""
    return f"<style>{LOG_CSS}</style>{body}"


LOG_JS = r"""
const C = JSON.parse(document.getElementById('cfg').textContent);
const el = (t, c, x) => { const e = document.createElement(t); if (c) e.className = c; if (x !== undefined) e.textContent = x; return e; };
const NS = 'http://www.w3.org/2000/svg';
const sv = (p, t, a, x) => { const e = document.createElementNS(NS, t); for (const k in a) e.setAttribute(k, a[k]);
  if (x !== undefined) e.textContent = x; p.append(e); return e; };
const pad = n => String(n).padStart(2, '0');
const fmt = (ts, sec) => { const d = new Date(ts); const s = `${pad(d.getHours())}:${pad(d.getMinutes())}` + (sec ? ':' + pad(d.getSeconds()) : '');
  return d.toDateString() === new Date().toDateString() ? s : `${d.getMonth() + 1}/${d.getDate()} ${s}`; };
const blobUrl = id => (window.__BLOB && window.__BLOB[id]) || `/ui/devices/${C.device}/blobs/${id}`;   // __BLOB は画面の下見用
let span = 0, data = null, cursor = null, playTimer = null;

document.querySelectorAll('.ranges button').forEach(b => b.addEventListener('click', () => {
  span = Number(b.dataset.span); document.querySelectorAll('.ranges button').forEach(x => x.setAttribute('aria-pressed', String(x === b)));
  cursor = null; load(); }));

async function load() {
  const r = await fetch(`/ui/devices/${C.device}/logs/${C.log}/data?span=${span}`, {credentials: 'same-origin'});
  if (!r.ok) return;
  data = await r.json();
  render();
}
function niceTicks(lo, hi, n = 4) {
  if (lo === hi) { lo -= 0.5; hi += 0.5; }
  const s0 = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(s0));
  const step = [1, 2, 2.5, 5, 10].map(k => k * mag).find(s => s >= s0);
  const dec = Math.max(0, -Math.floor(Math.log10(step) + 1e-9) + (String(step).includes('.5') ? 1 : 0));
  const t = []; for (let i = Math.floor(lo / step); i <= Math.ceil(hi / step); i++) t.push(+(i * step).toFixed(dec));
  t.dec = dec; return t;
}
function timeTicks(lo, hi, n) {
  const steps = [1, 5, 10, 30, 60, 300, 600, 1800, 3600, 7200, 21600, 43200, 86400].map(s => s * 1000);
  const step = steps.find(s => (hi - lo) / s <= n) || 86400000;
  const off = new Date().getTimezoneOffset() * 60000, t = [];
  for (let v = Math.ceil((lo - off) / step) * step + off; v <= hi; v += step) t.push(v);
  return {t, sec: step < 60000};
}

function chart(group, W) {
  const narrow = W < 520, H = narrow ? 190 : 230, L = 58, R = group.length > 1 ? (narrow ? 50 : 72) : 16, T = 10, B = 28;
  const box = el('div', 'tchart'); box.append(el('h4', '', `${group[0].label}（${group[0].unit}）`));
  if (group.length > 1) { const lg = el('div', 'legend');
    group.forEach((s, i) => { const x = el('span', '', `${s.column}${s.part ? '（' + s.part + '）' : ''}`);
      x.style.setProperty('--k', `var(--series-${i + 1})`); lg.append(x); }); box.append(lg); }
  const [lo, hi] = data.range, vals = group.flatMap(s => s.points.flatMap(p => [p[2], p[3]]));
  const yt = niceTicks(Math.min(...vals), Math.max(...vals)), y0 = yt[0], y1 = yt[yt.length - 1];
  const x = t => L + (W - L - R) * (t - lo) / ((hi - lo) || 1), y = v => T + (H - T - B) * (1 - (v - y0) / ((y1 - y0) || 1));
  const svg = document.createElementNS(NS, 'svg'); svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('width', W);
  svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', `${group[0].label}の推移`);
  for (const v of yt) { sv(svg, 'line', {x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: 'var(--grid)'});
    sv(svg, 'text', {x: L - 6, y: y(v) + 4, 'text-anchor': 'end', 'font-size': 11, fill: 'var(--axis)'}, v.toFixed(yt.dec)); }
  const tt = timeTicks(lo, hi, narrow ? 3 : 6);
  for (const t of tt.t) sv(svg, 'text', {x: x(t), y: H - 8, 'text-anchor': 'middle', 'font-size': 11, fill: 'var(--axis)'}, fmt(t, tt.sec));
  for (const [t] of data.photos) sv(svg, 'line', {x1: x(t), x2: x(t), y1: H - B, y2: H - B + 5, stroke: 'var(--axis)', 'stroke-width': 1.5});
  group.forEach((s, i) => {
    const c = `var(--series-${i + 1})`;
    if (s.points.some(p => p[2] !== p[3]))              // 間引いた区間は最小〜最大の帯
      sv(svg, 'path', {d: 'M' + s.points.map(p => `${x(p[0]).toFixed(1)} ${y(p[3]).toFixed(1)}`).join(' L') + ' L' +
        s.points.slice().reverse().map(p => `${x(p[0]).toFixed(1)} ${y(p[2]).toFixed(1)}`).join(' L') + ' Z', fill: c, opacity: 0.16});
    sv(svg, 'path', {d: s.points.map((p, j) => (j ? 'L' : 'M') + x(p[0]).toFixed(1) + ' ' + y(p[1]).toFixed(1)).join(' '),
      fill: 'none', stroke: c, 'stroke-width': 2, 'stroke-linejoin': 'round'});
    if (group.length > 1 && s.points.length) { const p = s.points[s.points.length - 1];
      sv(svg, 'text', {x: x(p[0]) + 6, y: y(p[1]) + 4, 'font-size': 11, fill: 'var(--fg)'}, s.column); }
  });
  const hair = sv(svg, 'line', {y1: T, y2: H - B, stroke: 'var(--axis)', visibility: cursor ? 'visible' : 'hidden'});
  if (cursor) { hair.setAttribute('x1', x(cursor)); hair.setAttribute('x2', x(cursor)); }
  const hit = sv(svg, 'rect', {x: L, y: T, width: W - L - R, height: H - T - B, fill: 'transparent'});
  const tip = el('div', 'tip'); box.append(svg, tip);
  const at = ev => { const r = svg.getBoundingClientRect(); return lo + ((ev.clientX - r.left) * W / r.width - L) / (W - L - R) * (hi - lo); };
  hit.addEventListener('pointermove', ev => {
    const t = at(ev); hair.setAttribute('x1', x(t)); hair.setAttribute('x2', x(t)); hair.setAttribute('visibility', 'visible');
    tip.replaceChildren(el('div', 'meta', fmt(t, true)));
    group.forEach((s, i) => { if (!s.points.length) return;
      const p = s.points.reduce((b, q) => Math.abs(q[0] - t) < Math.abs(b[0] - t) ? q : b);
      const row = el('div', 'row'), k = el('i'); k.style.setProperty('--k', `var(--series-${i + 1})`);
      row.append(k, el('b', '', `${p[1]} ${s.unit}`), el('span', 'meta', group.length > 1 ? s.column : '')); tip.append(row); });
    tip.style.display = 'block'; const r = svg.getBoundingClientRect();
    tip.style.left = Math.min((x(t) / W) * r.width + 14, r.width - 160) + 'px'; tip.style.top = '36px';
  });
  hit.addEventListener('pointerleave', () => { tip.style.display = 'none'; if (!cursor) hair.setAttribute('visibility', 'hidden'); });
  hit.addEventListener('click', ev => setCursor(at(ev)));
  return box;
}

function nearest(points, t) { let best = null; for (const p of points) if (!best || Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p; return best; }
function photoAt(t) { let best = null; for (const p of data.photos) if (p[0] <= t + 1000) best = p; return best || data.photos[0] || null; }

function setCursor(t) {
  cursor = Math.max(data.range[0], Math.min(data.range[1], t));
  const s = document.getElementById('scrub'); s.value = cursor; renderScrub(); renderCharts();
}
function renderScrub() {
  const t = cursor ?? data.range[1];
  document.getElementById('when').textContent = fmt(t, true);
  const p = photoAt(t), img = document.getElementById('big'), none = document.getElementById('nophoto');
  if (p) { img.hidden = false; none.hidden = true; if (img.dataset.id !== p[1]) { img.src = blobUrl(p[1]); img.dataset.id = p[1]; }
    img.alt = '写真 ' + fmt(p[0], true); document.getElementById('photowhen').textContent = '写真: ' + fmt(p[0], true); }
  else { img.hidden = true; none.hidden = false; document.getElementById('photowhen').textContent = ''; }
  const at = document.getElementById('at'); at.replaceChildren();
  for (const s of data.series) { const q = nearest(s.points, t); if (!q) continue;
    const d = el('div'); d.append(el('span', 'meta', `${s.column} ・ ${s.label} `), el('b', '', String(q[1])), el('span', 'meta', ' ' + s.unit)); at.append(d); }
  document.querySelectorAll('.strip button').forEach(b => b.setAttribute('aria-current', String(p && b.dataset.id === p[1])));
}
function renderCharts() {
  const box = document.getElementById('charts'); box.replaceChildren();
  const W = Math.max(300, Math.min(1100, box.clientWidth));
  const by = {}; for (const s of data.series) (by[s.channel] = by[s.channel] || []).push(s);
  for (const g of Object.values(by)) if (g.some(s => s.points.length)) box.append(chart(g.slice(0, 2), W));
  if (!data.series.length) box.append(el('p', 'meta', data.not_decodable.length
    ? `換算できる列がありません（${data.not_decodable.join(', ')}）。CSV には生の値が入っています。部品を登録して校正値を読んでから記録を始めると、ここにグラフが出ます。`
    : 'まだデータがありません'));
}
function render() {
  const tiles = document.getElementById('tiles'); tiles.replaceChildren();
  const UNIT = {temperature_c: ['気温', '℃', 2], pressure_hpa: ['気圧', 'hPa', 1], humidity_pct: ['湿度', '%RH', 1],
                bus_v: ['電圧', 'V', 3], current_ma: ['電流', 'mA', 1]};
  for (const [col, v] of Object.entries(data.latest)) for (const [k, val] of Object.entries(v)) { if (k === 'ts' || !UNIT[k]) continue;
    const t = el('div', 'tile'); t.append(el('div', 'tl', `${col} ・ ${UNIT[k][0]} ・ ${fmt(v.ts)}`));
    const tv = el('div', 'tv', Number(val).toFixed(UNIT[k][2])); tv.append(el('span', 'tu', UNIT[k][1])); t.append(tv); tiles.append(t); }
  document.getElementById('count').textContent = `${data.rows} 行 ・ 写真 ${data.photos.length} 枚（表示範囲）`;
  const s = document.getElementById('scrub'); s.min = data.range[0]; s.max = data.range[1]; s.step = 1000; s.value = cursor ?? data.range[1];
  const strip = document.getElementById('strip'); strip.replaceChildren();
  const step = Math.max(1, Math.ceil(data.photos.length / 60));      // 写真が多いときは間引いて並べる
  data.photos.filter((_, i) => i % step === 0).forEach(([t, id]) => {
    const b = el('button'); b.type = 'button'; b.dataset.id = id; b.title = fmt(t, true);
    const im = el('img'); im.loading = 'lazy'; im.alt = '写真 ' + fmt(t, true); im.src = blobUrl(id);
    b.append(im, el('span', '', fmt(t, true))); b.onclick = () => setCursor(t); strip.append(b); });
  document.getElementById('photos').hidden = !data.photos.length;
  renderCharts(); renderScrub();
}
document.getElementById('scrub').addEventListener('input', e => setCursor(Number(e.target.value)));
function stepPhoto(dir) { if (!data.photos.length) return; const t = cursor ?? data.range[1];
  const i = data.photos.findIndex(p => p[0] > t + 1); const cur = i < 0 ? data.photos.length - 1 : Math.max(0, i - 1);
  const n = Math.max(0, Math.min(data.photos.length - 1, cur + dir)); setCursor(data.photos[n][0]); }
document.getElementById('prev').onclick = () => stepPhoto(-1);
document.getElementById('next').onclick = () => stepPhoto(1);
document.getElementById('play').onclick = e => {
  if (playTimer) { clearInterval(playTimer); playTimer = null; e.target.textContent = '▶ コマ送り'; return; }
  e.target.textContent = '■ 止める'; if (data.photos.length) setCursor(data.photos[0][0]);
  playTimer = setInterval(() => { const t = cursor ?? 0; const n = data.photos.find(p => p[0] > t + 1);
    if (!n) { clearInterval(playTimer); playTimer = null; e.target.textContent = '▶ コマ送り'; return; } setCursor(n[0]); }, 700); };
let rt = null; window.addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(() => data && renderCharts(), 300); });
if (C.running) setInterval(() => { if (!playTimer) load(); }, 30000);
load();
"""


def spool_notes(log: dict) -> str:
    """Pi の spool が満杯になったときの欠落・写真の停止（F5）。"""
    out = []
    if log.get("dropped"):
        ranges = "、".join(f"{a}" if a == b else f"{a}〜{b}" for a, b in log["dropped"])
        out.append(f'<p class="meta warn">欠落: Pi の保存領域（spool）が満杯になり、未送信の記録の一部を捨てました'
                   f'（バッチ {esc(ranges)}）。その時間帯はグラフと CSV に出ません。</p>')
    if log.get("photos_paused_ts"):
        out.append(f'<p class="meta warn">Pi の保存領域が残り少なくなったため、{esc(jst(log["photos_paused_ts"]))} から'
                   f'写真の撮影を止めた時間があります（数値の記録は続けています）。</p>')
    if log.get("clock_corrected"):
        out.append('<p class="meta">開始時刻は、Pi の時計が合っていなかったため、クラウドの受信時刻で補正しています。</p>')
    return "".join(out)


def detail_page(*, device_id: str, log: dict, csrf: str) -> str:
    args = log.get("args") or {}
    st = log.get("status", "running")
    running = st == "running"
    photo = args.get("photo")
    cfg = json.dumps({"device": device_id, "log": log["log_id"], "running": running}).replace("<", "\\u003c")
    stop = (f"""<form method="post" action="/ui/devices/{esc(device_id)}/logs/{esc(log['log_id'])}/stop">
<input type="hidden" name="csrf" value="{esc(csrf)}"><button class="stop" type="submit">記録を止める</button></form>"""
            if running else "")
    planned_end = (log.get("started_ts") or 0) + args.get("duration_ms", 0)
    return f"""<style>{LOG_CSS}</style><script type="application/json" id="cfg">{cfg}</script>
<header><div><a href="/logs/{esc(device_id)}">← 記録の一覧</a>
<h1>{esc(log.get('name', log['log_id']))} <span class="chip">{'● ' if running else ''}{esc(STATUS.get(st, st))}</span></h1>
<div class="meta">{esc(jst(log.get('started_ts')))} 〜 {esc(jst(log.get('ended_ts') or planned_end))}{'（予定）' if running else ''} ・
{esc(fmt_ms(args.get('interval_ms', 0)))}ごと{f" ・ 写真 {esc(fmt_ms(photo['interval_ms']))}ごと（{esc(photo['camera'])}）" if photo else ''}
 ・ <span id="count"></span></div>{spool_notes(log)}</div>
<div class="row"><div class="ranges" role="group" aria-label="表示範囲">
<button type="button" data-span="3600000" aria-pressed="false">1時間</button><button type="button" data-span="21600000" aria-pressed="false">6時間</button><button type="button" data-span="86400000" aria-pressed="false">24時間</button><button type="button" data-span="0" aria-pressed="true">全部</button></div>
<a class="chip" href="/ui/devices/{esc(device_id)}/logs/{esc(log['log_id'])}.csv">CSV</a>{stop}</div></header>
<div class="tiles" id="tiles"></div>
<section class="card viz-root"><h2 style="margin-top:0">その時刻の写真と値</h2>
<div class="scrub"><div><img id="big" class="big" alt="" hidden><div id="nophoto" class="big nophoto" hidden>写真なし</div>
<div class="meta" id="photowhen"></div></div>
<div><div class="when" id="when"></div><div class="at" id="at"></div>
<input type="range" id="scrub" aria-label="時刻"><div class="row">
<button type="button" id="prev">◀ 前の写真</button><button type="button" id="play">▶ コマ送り</button><button type="button" id="next">次の写真 ▶</button></div>
<p class="meta">グラフ・写真をクリックしても、その時刻に合わせられます。</p></div></div></section>
<section id="photos" hidden><h2>写真</h2><div class="strip" id="strip"></div></section>
<section class="viz-root"><div id="charts"></div></section>
<script>{LOG_JS}</script>"""
