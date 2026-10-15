"""チャット画面の「現在の状態」パネル: 接続のイメージ図、Pi のピン配置、テスター表示。

- 分割（チャット｜状態）、チャットだけ、状態だけ（テスター表示つき）を切り替える。選んだものは覚えておく
- 状態は /ui/devices/{id}/state（Gateway が追っている写し）、テスターの値は /ui/devices/{id}/tester
- ピンの種類は色だけでなく必ずラベルでも示す。状態の色（good/warning…）はピンの種類には使わない
"""

STATE_CSS = """
.viewsw{display:inline-flex;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.viewsw button{border:0;border-radius:0;padding:6px 12px;font-size:13px;background:var(--card)}
.viewsw button[aria-pressed="true"]{background:var(--accent);color:#fff}
.layout{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(0,1fr);gap:20px;align-items:start}
.chatpane{min-width:0}
.statepane{position:sticky;top:12px;max-height:calc(100dvh - 24px);overflow:auto;min-width:0;
 background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px}
.layout[data-view="chat"]{grid-template-columns:minmax(0,1fr)}
.layout[data-view="chat"] .statepane{display:none}
.layout[data-view="state"]{grid-template-columns:minmax(0,1fr)}
.layout[data-view="state"] .chatpane{display:none}
.layout[data-view="state"] .statepane{position:static;max-height:none}
.layout[data-view="chat"] #sp-tester{display:none}
.layout[data-view="split"] .meters{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
.layout[data-view="split"] .meters .meter .mv{font-size:28px}
.layout[data-view="state"] .sp-grid{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(0,1fr);gap:16px}
@media (max-width:860px){.layout{grid-template-columns:minmax(0,1fr)!important}
 .layout[data-view="split"] .statepane{display:none} .viewsw [data-v="split"]{display:none}
 .layout[data-view="state"] .sp-grid{grid-template-columns:minmax(0,1fr)}}
.statepane h2{font-size:16px;margin:0}.statepane h3{font-size:13px;color:var(--muted);margin:14px 0 6px;font-weight:600}
.sp-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px;flex-wrap:wrap}
.sp-chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.pin-list{font-size:12px;border-collapse:collapse;width:100%;margin-top:6px}
.pin-list td{padding:3px 6px;border-bottom:1px solid var(--line);vertical-align:top}
.pin-legend{display:flex;gap:12px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-top:6px}
.pin-legend span{display:inline-flex;align-items:center;gap:5px}
.pin-legend i{width:11px;height:11px;border-radius:50%;display:inline-block;background:var(--k);border:1.5px solid var(--kb,var(--k))}
.pin-legend i.used{width:10px;height:10px;border-width:3px}
.sp-svg.pins{width:420px;max-width:100%;margin:0 auto}
.sp-svg{width:100%;height:auto;display:block}
.tester{background:var(--lcd-case);border-radius:16px;padding:14px;margin-top:10px;color:var(--lcd-case-fg)}
.meter{color:var(--lcd-fg)}
.tester .thead{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.tester .live{font-size:12px;opacity:.85}.tester .live b{display:inline-block;width:8px;height:8px;border-radius:50%;
 background:#3ccf6e;margin-right:6px;animation:blink 1.6s infinite}
.tester .live.hold b{background:#999;animation:none}
@keyframes blink{50%{opacity:.25}}
@media (prefers-reduced-motion:reduce){.tester .live b{animation:none}}
.tester button{background:var(--lcd-btn);color:var(--lcd-case-fg);border-color:transparent}
.tester>.mn{margin-top:8px}
.agent-panel{margin-top:12px;background:var(--card);color:var(--fg);border-radius:12px;padding:10px}
.agent-panel .ap-title{font-weight:700;font-size:13px;margin-bottom:6px}
.agent-panel .ap-head{font-size:12px;color:var(--muted);margin:6px 0 4px}
.meters{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:10px}
@media (max-width:480px){.meters{grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.meters .meter .mv{font-size:24px}.meters .meter{padding:8px 10px;min-width:0;overflow:hidden}.meters .meter .ml,.meters .meter .mn{overflow-wrap:anywhere}.meters .meter .mu{font-size:12px}}
.meter{background:var(--lcd-bg);border-radius:10px;padding:10px 12px;border:1px solid var(--lcd-edge)}
.meter .ml{font-size:12px;opacity:.8}.meter .mv{font:600 40px/1.15 ui-monospace,SFMono-Regular,Menlo,monospace;
 font-variant-numeric:tabular-nums;letter-spacing:.5px}
.meter .mu{font-size:15px;margin-left:4px;opacity:.85}.meter .mn{font-size:12px;opacity:.75}
.lamp{display:inline-block;width:14px;height:14px;border-radius:50%;border:2px solid var(--lcd-fg);vertical-align:-2px;margin-right:8px}
.lamp.on{background:#ffb02e;border-color:#ffb02e;box-shadow:0 0 10px #ffb02e}
.viz-root, .statepane{--pin-i2c:#2a78d6;--pin-out:#eb6834;--pin-in:#1baf7a;--pin-pwr:#e34948;--pin-gnd:#52514e;
 --pin-off:#c9c7c0;--pin-gpio:#a8a69e;--pin-num-off:#111;--pin-num-gnd:#fff;--pin-num-pwr:#fff;--pcb:#2f6f4f;--pcb-fg:#fff;--part:#fcfcfb;--wire:#8a8984;
 --lcd-case:#2b2e2b;--lcd-case-fg:#eef2ec;--lcd-bg:#c9d6c3;--lcd-fg:#10140f;--lcd-edge:#a9b8a2;--lcd-btn:#4a4f4a}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .statepane{--pin-i2c:#3987e5;--pin-out:#d95926;
 --pin-in:#199e70;--pin-pwr:#e66767;--pin-gnd:#c3c2b7;--pin-off:#8d8b83;--pin-gpio:#77756e;--pin-num-off:#fff;--pin-num-gnd:#111;--pin-num-pwr:#111;--pcb:#24573e;--part:#232321;--wire:#8f8e88;
 --lcd-case:#0d0f0d;--lcd-case-fg:#d8dcd6;--lcd-bg:#1b2a1d;--lcd-fg:#b9f5c4;--lcd-edge:#2c4430;--lcd-btn:#2c332d}}
:root[data-theme="dark"] .statepane{--pin-i2c:#3987e5;--pin-out:#d95926;--pin-in:#199e70;--pin-pwr:#e66767;
 --pin-gnd:#c3c2b7;--pin-off:#8d8b83;--pin-gpio:#77756e;--pin-num-off:#fff;--pin-num-gnd:#111;--pin-num-pwr:#111;--pcb:#24573e;--part:#232321;--wire:#8f8e88;--lcd-case:#0d0f0d;--lcd-bg:#1b2a1d;
 --lcd-fg:#b9f5c4;--lcd-edge:#2c4430;--lcd-btn:#2c332d;--lcd-case-fg:#d8dcd6}
"""

STATE_JS = r"""
// ---- 表示の切り替え（分割 / チャット / 状態）。選んだものを覚えておく -------------------------------
const layout = document.getElementById('layout');
function setView(v) {
  layout.dataset.view = v;
  document.querySelectorAll('.viewsw button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.v === v)));
  try { localStorage.setItem('mugemuge.view', v); } catch (_) {}
  if (typeof panelState !== 'undefined') panelState.version = -1;     // 幅が変わるので描き直す
  refreshState(); if (v !== 'chat') { refreshTester(); refreshPanel(); }
}
document.querySelectorAll('.viewsw button').forEach(b => b.addEventListener('click', () => setView(b.dataset.v)));

// ---- Pi の 40 ピン（物理ピン番号 → 種類と BCM 番号） ------------------------------------------------
const PIN = {1:'3V3',2:'5V',3:2,4:'5V',5:3,6:'GND',7:4,8:14,9:'GND',10:15,11:17,12:18,13:27,14:'GND',15:22,16:23,
  17:'3V3',18:24,19:10,20:'GND',21:9,22:25,23:11,24:8,25:'GND',26:7,27:0,28:1,29:5,30:'GND',31:6,32:12,33:13,34:'GND',
  35:19,36:16,37:26,38:20,39:'GND',40:21};
const NS = 'http://www.w3.org/2000/svg';
function svgEl(svg, tag, attrs, text) { const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]); if (text !== undefined) e.textContent = text; svg.append(e); return e; }
function pinInfo(n, st) {
  const v = PIN[n];
  if (v === '3V3' || v === '5V') return {cls: 'pwr', label: v, desc: v + ' 電源'};
  if (v === 'GND') return {cls: 'gnd', label: 'GND', desc: 'GND'};
  const ln = st.gpio_lines.find(l => l.line === v);
  if ((v === 2 || v === 3) && st.i2c_buses.some(b => b.bus === 1))
    return {cls: 'i2c', label: v === 2 ? 'SDA' : 'SCL', desc: `GPIO${v}（I2C バス1 の ${v === 2 ? 'SDA' : 'SCL'}）`};
  if (ln) {
    const out = ln.direction === 'output';
    const how = out ? `出力・${ln.write === 'allow' ? '自動' : '承認制'}${ln.max_on_ms ? '・最大' + ln.max_on_ms + 'ms' : ''}`
                    : `入力${ln.bias === 'pull_up' ? '（プルアップ）' : ''}`;
    return {cls: out ? 'out' : 'in', label: String(v), desc: `GPIO${v} ${ln.label || ''}（${how}）`, line: ln};
  }
  return {cls: 'off', label: String(v), desc: `GPIO${v}（マニフェストにない＝使えない）`};
}
// 使用中（I2C・出力・入力）は白い丸に種類の色の太い縁。未設定の GPIO・電源・GND は塗りつぶし
const PIN_FILL = {pwr: 'var(--pin-pwr)', gnd: 'var(--pin-gnd)', off: 'var(--pin-gpio)', i2c: '#fff', out: '#fff', in: '#fff'};
const PIN_EDGE = {i2c: 'var(--pin-i2c)', out: 'var(--pin-out)', in: 'var(--pin-in)'};
const USED = c => c === 'i2c' || c === 'out' || c === 'in';
function pinLabel(n, p) {
  if (p.cls === 'pwr' || p.cls === 'gnd') return p.label;
  if (p.cls === 'i2c') return `GPIO${PIN[n]} ${p.label}`;
  if (p.cls === 'out' || p.cls === 'in') {
    const name = ((p.line && p.line.label) || (p.cls === 'out' ? '出力' : '入力')).replace(/（.*?）/g, '');
    return `GPIO${PIN[n]} ${name.length > 9 ? name.slice(0, 9) + '…' : name}`;
  }
  return `GPIO${PIN[n]}`;
}
function pinsView(st) {
  // 実物と同じ向き（2列×20段、ピン1が左上）。左に奇数ピン、右に偶数ピンのラベル
  const box = el('div'), W = 364, H = 20 * 23 + 24, lx = 162, rx = 202;
  const svg = document.createElementNS(NS, 'svg'); svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('class', 'sp-svg pins');
  svg.setAttribute('width', W); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', 'Raspberry Pi の 40 ピンの割り当て');
  svgEl(svg, 'rect', {x: lx - 14, y: 6, width: rx - lx + 28, height: H - 12, rx: 6, fill: 'var(--pcb)'});
  for (let n = 1; n <= 40; n++) {
    const row = Math.floor((n - 1) / 2), left = n % 2 === 1, cx = left ? lx : rx, cy = 19 + row * 23;
    const p = pinInfo(n, st), used = USED(p.cls);
    const c = svgEl(svg, 'circle', {cx, cy, r: used ? 8.5 : 8.5, fill: PIN_FILL[p.cls],
      stroke: used ? PIN_EDGE[p.cls] : 'var(--pcb-fg)', 'stroke-width': used ? 3.5 : 1});
    svgEl(c, 'title', {}, `物理ピン ${n}: ${p.desc}`);
    svgEl(svg, 'text', {x: left ? lx - 16 : rx + 16, y: cy + 4, 'text-anchor': left ? 'end' : 'start',
      'font-size': used ? 11 : 10, fill: used ? 'var(--fg)' : 'var(--muted)', 'font-weight': used ? 700 : 400},
      pinLabel(n, p));
    // 物理ピン番号を全ピンに（丸の色ごとに読める文字色）
    svgEl(svg, 'text', {x: cx, y: cy + 3, 'text-anchor': 'middle', 'font-size': n < 10 ? 8.5 : 7.5, 'font-weight': 700,
                        fill: used ? '#111' : `var(--pin-num-${p.cls})`, 'pointer-events': 'none'}, String(n));
  }
  box.append(svg);
  return pinsLegend(st, box);
}
function pinsLegend(st, box) {
  const lg = el('div', 'pin-legend');
  for (const [k, t] of [['i2c', '使用中: I2C'], ['out', '使用中: 出力'], ['in', '使用中: 入力'], ['off', '未設定の GPIO（使えない）'],
                        ['pwr', '電源'], ['gnd', 'GND']]) {
    const s = el('span'); const i = el('i'); i.style.setProperty('--k', PIN_FILL[k]);
    if (USED(k)) { i.style.setProperty('--kb', PIN_EDGE[k]); i.classList.add('used'); }
    s.append(i, document.createTextNode(t)); lg.append(s);
  }
  box.append(lg);
  const tb = el('table', 'pin-list');
  for (let n = 1; n <= 40; n++) { const p = pinInfo(n, st); if (!['out', 'in', 'i2c'].includes(p.cls)) continue;
    const tr = el('tr'); tr.append(el('td', '', '物理 ' + n), el('td', '', p.desc)); tb.append(tr); }
  box.append(tb);
  return box;
}

// ---- 接続のイメージ図（Pi → I2C の部品、GPIO の部品） -------------------------------------------
function diagramView(st) {
  const bound = new Map((st.state.bindings || []).map(b => [b.addr, b]));
  const seen = new Set(((st.i2c_seen['1'] || {}).found) || []); for (const a of bound.keys()) seen.add(a);
  const items = [...seen].sort((a, b) => a - b).map(a => ({type: 'i2c', addr: a, b: bound.get(a)}));
  for (const ln of st.gpio_lines) items.push({type: ln.direction === 'output' ? 'out' : 'in', ln});
  const box = el('div');
  if (!items.length) { box.append(el('p', 'meta', 'まだ何も分かっていません。「何がつながっているか調べて」と頼むと出ます。')); return box; }
  // 実際の表示幅で描く（縮小すると文字が読めなくなる）
  const W = Math.max(300, Math.min(640, sp.diagram.clientWidth || 520)), narrow = W < 440;
  const rowH = 58, H = Math.max(150, items.length * rowH + 24), bx = 6, bw = narrow ? 74 : 120, px = narrow ? bx + bw + 64 : 270;
  const svg = document.createElementNS(NS, 'svg'); svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('class', 'sp-svg');
  svg.setAttribute('width', W);
  svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', 'デバイスにつながっている部品');
  svgEl(svg, 'rect', {x: bx, y: 12, width: bw, height: H - 24, rx: 10, fill: 'var(--pcb)'});
  for (let i = 0; i < 20; i++) svgEl(svg, 'circle', {cx: bx + bw - 12, cy: 26 + i * ((H - 52) / 19), r: 2.6, fill: '#d9b648'});
  svgEl(svg, 'text', {x: bx + 10, y: 36, 'font-size': narrow ? 11 : 13, 'font-weight': 700, fill: 'var(--pcb-fg)'}, st.virtual ? '仮想' : narrow ? 'Pi' : 'Raspberry');
  svgEl(svg, 'text', {x: bx + 10, y: 52, 'font-size': narrow ? 11 : 13, 'font-weight': 700, fill: 'var(--pcb-fg)'}, st.virtual ? 'デバイス' : narrow ? '' : 'Pi');
  svgEl(svg, 'text', {x: bx + 12, y: 70, 'font-size': 10, fill: 'var(--pcb-fg)', opacity: .8}, st.online ? '● オンライン' : '○ オフライン');
  const i2cRows = items.map((it, i) => it.type === 'i2c' ? 24 + i * rowH + rowH / 2 - 6 : null).filter(y => y !== null);
  if (i2cRows.length) {      // I2C は1本のバスに部品がぶら下がる
    const tx = bx + bw + (narrow ? 24 : 46);
    svgEl(svg, 'line', {x1: bx + bw, x2: tx, y1: i2cRows[0], y2: i2cRows[0], stroke: 'var(--pin-i2c)', 'stroke-width': 3});
    svgEl(svg, 'line', {x1: tx, x2: tx, y1: i2cRows[0], y2: i2cRows[i2cRows.length - 1], stroke: 'var(--pin-i2c)', 'stroke-width': 3});
    svgEl(svg, 'text', {x: bx + bw + 4, y: i2cRows[0] - 8, 'font-size': 10, fill: 'var(--muted)'}, narrow ? 'I2C' : 'I2C バス1');
  }
  items.forEach((it, i) => {
    const y = 24 + i * rowH, cy = y + rowH / 2 - 6;
    const color = it.type === 'i2c' ? 'var(--pin-i2c)' : it.type === 'out' ? 'var(--pin-out)' : 'var(--pin-in)';
    svgEl(svg, 'line', {x1: it.type === 'i2c' ? bx + bw + (narrow ? 24 : 46) : bx + bw, x2: px, y1: cy, y2: cy, stroke: color, 'stroke-width': 3});
    // 部品のイメージ（小さな基板）
    svgEl(svg, 'rect', {x: px, y: y + 2, width: W - px - 8, height: rowH - 16, rx: 8, fill: 'var(--part)', stroke: color, 'stroke-width': 1.5});
    if (it.type === 'i2c') {
      svgEl(svg, 'rect', {x: px + 10, y: y + 12, width: 20, height: 20, rx: 3, fill: 'var(--fg)', opacity: .85});
      svgEl(svg, 'text', {x: px - 6, y: cy - 6, 'text-anchor': 'end', 'font-size': 10, fill: 'var(--muted)'}, '0x' + it.addr.toString(16).padStart(2, '0'));
      const name = it.b ? it.b.part : '未同定';
      const kind = it.b && st.parts[it.b.part] ? st.parts[it.b.part].kind : (it.b ? '' : narrow ? '' : '「調べて」で同定できる');
      svgEl(svg, 'text', {x: px + 40, y: y + 22, 'font-size': 13, 'font-weight': 700, fill: 'var(--fg)'}, name);
      svgEl(svg, 'text', {x: px + 40, y: y + 37, 'font-size': 10.5, fill: 'var(--muted)'}, narrow ? kind : (it.b ? '登録済み ・ ' : '') + kind);
    } else {
      const ln = it.ln, o = (st.state.outputs || []).find(x => x.line === ln.line);
      const inp = (st.state.inputs || []).find(x => x.line === ln.line);
      const val = it.type === 'out' ? (o ? o.value : ln.safe_value) : (inp ? inp.value : null);
      svgEl(svg, 'text', {x: px - 6, y: cy - 6, 'text-anchor': 'end', 'font-size': 10, fill: 'var(--muted)'}, 'GPIO' + ln.line);
      if (it.type === 'out') {
        const on = val !== ln.safe_value;
        svgEl(svg, 'circle', {cx: px + 20, cy: y + 21, r: 9, fill: on ? '#ffb02e' : 'none', stroke: on ? '#ffb02e' : 'var(--muted)', 'stroke-width': 2});
        svgEl(svg, 'text', {x: px + 40, y: y + 22, 'font-size': 13, 'font-weight': 700, fill: 'var(--fg)'},
              narrow ? `${on ? 'ON' : 'OFF'} ${ln.label || ''}` : `${ln.label || 'output'}  ${on ? 'ON' : 'OFF'}`);
        svgEl(svg, 'text', {x: px + 40, y: y + 37, 'font-size': 10.5, fill: 'var(--muted)'},
              narrow ? `出力・${ln.write === 'allow' ? '自動' : '承認制'}` :
              `出力 ・ ${ln.write === 'allow' ? '自動' : '承認制'}${ln.max_on_ms ? ' ・ 最大' + ln.max_on_ms / 1000 + '秒で自動OFF' : ''}`);
      } else {
        svgEl(svg, 'circle', {cx: px + 20, cy: y + 21, r: 9, fill: 'var(--pin-in)', opacity: .35});
        svgEl(svg, 'circle', {cx: px + 20, cy: y + 21, r: 5, fill: 'var(--pin-in)'});
        const t = val === null ? '値はテスターで読みます' : (ln.bias === 'pull_up' ? (val ? '離れている' : '押されている') : '値 ' + val);
        svgEl(svg, 'text', {x: px + 40, y: y + 22, 'font-size': 13, 'font-weight': 700, fill: 'var(--fg)'}, ln.label || 'input');
        svgEl(svg, 'text', {x: px + 40, y: y + 37, 'font-size': 10.5, fill: 'var(--muted)'}, narrow ? '入力' : '入力 ・ ' + t);
      }
    }
  });
  box.append(svg);
  return box;
}

// ---- 状態の取得と描画 -----------------------------------------------------------------------
const sp = {summary: document.getElementById('sp-summary'), diagram: document.getElementById('sp-diagram'),
            pins: document.getElementById('sp-pins'), updated: document.getElementById('sp-updated'),
            tester: document.getElementById('sp-tester')};
let lastState = null, stateBusy = false;
async function refreshState() {
  if (layout.dataset.view === 'chat' || stateBusy) return;
  stateBusy = true;
  try {
    const {ok, body} = await api(`/ui/devices/${S.device}/state`); if (!ok) return;
    const st = body; lastState = st;
    sp.summary.replaceChildren(
      el('span', 'chip', st.online ? '● オンライン' : '○ オフライン'),
      el('span', 'chip', st.state.estop ? '非常停止中' : '非常停止なし'),
      el('span', 'chip', (() => { const j = (st.state.jobs || []).filter(x => !x.startsWith('log_'));
        return j.length ? '計測中: ' + j.join(', ') : '計測なし'; })()),
      el('span', 'chip', (() => { const l = (st.state.jobs || []).filter(x => x.startsWith('log_'));
        return l.length ? '記録中: ' + l.length + '件' : '記録なし'; })()),
      el('span', 'chip', '登録済みの部品 ' + (st.state.bindings || []).length));
    sp.diagram.replaceChildren(diagramView(st));
    sp.pins.replaceChildren(pinsView(st));
    sp.updated.textContent = '更新 ' + new Date().toLocaleTimeString();
  } finally { stateBusy = false; }
}
setInterval(refreshState, 3000);

// ---- テスター表示（状態だけを最大化したとき。2秒ごとに読む。HOLD で止める） ---------------------------
let hold = false, testerBusy = false;
const UNIT = {temperature_c: ['気温', '℃', 2], pressure_hpa: ['気圧', 'hPa', 1], humidity_pct: ['湿度', '%RH', 1],
              bus_v: ['電圧', 'V', 3], current_ma: ['電流', 'mA', 1], shunt_mv: ['シャント電圧', 'mV', 3]};
function testerShell() {
  if (sp.tester.firstChild) return;
  const t = el('div', 'tester'), head = el('div', 'thead'), live = el('span', 'live');
  live.append(el('b'), document.createTextNode('測定中 ・ 2秒ごと'));
  const hb = el('button', '', 'HOLD'); hb.onclick = () => { hold = !hold; hb.textContent = hold ? '再開' : 'HOLD';
    live.classList.toggle('hold', hold); live.lastChild.textContent = hold ? '停止中（HOLD）' : '測定中 ・ 2秒ごと'; };
  const h = el('strong', '', 'テスター'); head.append(h, live, hb);
  const meters = el('div', 'meters'); meters.id = 'meters';
  const note = el('div', 'mn', '登録済みの部品を、自動で許可される読み出しだけで読みます（書き込みはしません）');
  const ap = el('div', 'agent-panel'); ap.id = 'agent-panel'; ap.hidden = true;
  t.append(head, meters, note, ap); sp.tester.append(t);
}
function meter(label, value, unit, note) {
  const m = el('div', 'meter'); m.append(el('div', 'ml', label));
  const v = el('div', 'mv', value); if (unit) v.append(el('span', 'mu', unit)); m.append(v);
  if (note) m.append(el('div', 'mn', note)); return m;
}
// INA226 のシャント抵抗（基板で違う）。人が決めてクラウドに残す。電流の換算にだけ使う
function shuntCard(name, r) {
  const m = r.shunt_ohm ? +(r.shunt_ohm * 1000).toPrecision(6) : null;
  const c = meter(`${name} ・ シャント抵抗`, m === null ? '未設定' : String(m), m === null ? '' : 'mΩ', r.note || '');
  const btn = el('button', '', m === null ? '設定する' : '変更');
  btn.onclick = async () => {
    const v = prompt('シャント抵抗を mΩ で入力してください（例: M5Stack INA226-10A は 5、INA226-1A は 80、多くの基板は 100）',
                     m === null ? '' : String(m));
    if (v === null) return;
    const mohm = Number(v.trim());
    if (!(mohm > 0)) { alert('数値で入力してください'); return; }
    const res = await api(`/ui/devices/${S.device}/part-params`, {method: 'POST',
      headers: {'content-type': 'application/json', 'x-csrf': S.csrf},
      body: JSON.stringify({bus: r.bus, addr: r.addr, shunt_ohm: mohm / 1000})});
    if (!res.ok) alert('保存できませんでした: ' + ((res.body && res.body.detail) || res.status));
    refreshTester();
  };
  c.append(btn); return c;
}
async function refreshTester() {
  if (layout.dataset.view === 'chat' || hold || testerBusy) return;
  testerShell(); testerBusy = true;
  try {
    const {ok, body} = await api(`/ui/devices/${S.device}/tester`);
    const meters = document.getElementById('meters');
    if (!ok) { meters.replaceChildren(meter('デバイス', '—', '', 'オフラインか、読めませんでした')); return; }
    const cards = [];
    for (const r of body.readings) {
      const name = `${r.part} 0x${r.addr.toString(16).padStart(2, '0')}`;
      const keys = Object.keys(r.values || {});
      if (!keys.length) { cards.push(meter(name, '—', '', r.note || '')); continue; }
      for (const k of keys) { const u = UNIT[k] || [k, '', 2];
        cards.push(meter(`${name} ・ ${u[0]}`, Number(r.values[k]).toFixed(u[2]), u[1])); }
      if ('shunt_ohm' in r) cards.push(shuntCard(name, r));
    }
    for (const g of body.gpio) {
      const m = el('div', 'meter'); m.append(el('div', 'ml', `${g.label || 'GPIO'}（GPIO${g.line}）`));
      const v = el('div', 'mv');
      if (g.direction === 'output') { const on = g.value !== null && g.value !== g.safe_value;
        const lamp = el('span', 'lamp' + (on ? ' on' : '')); v.append(lamp, document.createTextNode(g.value === null ? '—' : on ? 'ON' : 'OFF'));
        m.append(v, el('div', 'mn', g.max_on_ms ? `ON は最大 ${g.max_on_ms / 1000} 秒で自動 OFF` : ''));
      } else { v.textContent = g.value === null ? '—' : String(g.value);
        m.append(v, el('div', 'mn', g.value === null ? '' : (g.value ? '離れている（プルアップ）' : '押されている'))); }
      cards.push(m);
    }
    if (!cards.length) cards.push(meter('まだ何もありません', '—', '', '部品を同定して登録すると、ここに値が出ます'));
    meters.replaceChildren(...cards);
  } finally { testerBusy = false; }
}
setInterval(refreshTester, 2000);

// ---- エージェントがテスター画面に出したグラフ（show_on_tester） ------------------------------------
const panelState = {version: -1, widgets: [], done: new Map(), busy: false};
async function refreshPanel() {
  if (layout.dataset.view === 'chat' || panelState.busy) return;
  testerShell(); panelState.busy = true;
  try {
    const {ok, body} = await api(`/ui/chat/${S.chat}/panel`); if (!ok) return;
    const box = document.getElementById('agent-panel');
    if (body.version !== panelState.version) {
      panelState.version = body.version; panelState.widgets = body.widgets; panelState.done.clear();
      box.replaceChildren(el('div', 'ap-title', 'エージェントが出したグラフ'));
      for (const w of body.widgets) { const c = el('div', 'ap-item'); c.dataset.key = w.job_id + '|' + w.columns.join(',');
        c.append(el('div', 'ap-head', w.title), el('div', 'charts viz-root')); box.append(c); }
      box.hidden = !body.widgets.length;
    }
    for (const w of panelState.widgets) {
      const key = w.job_id + '|' + w.columns.join(',');
      if (panelState.done.get(key)) continue;          // 終わった計測は描き直さない
      const item = box.querySelector(`[data-key="${CSS.escape(key)}"]`); if (!item) continue;
      const r = await api(`/ui/chat/${S.chat}/jobs/${w.job_id}`); if (!r.ok) continue;
      const cols = {}; for (const c of w.columns) if (r.body.columns[c]) cols[c] = r.body.columns[c];
      item.querySelector('.ap-head').textContent = `${w.title} ・ ${r.body.status === 'completed' ? '計測完了' : '計測中 ' + mmss(r.body.elapsed_ms) + ' / ' + mmss(r.body.duration_ms)}`;
      renderSeries(item.querySelector('.charts'), cols);
      if (r.body.status === 'completed') panelState.done.set(key, true);
    }
  } finally { panelState.busy = false; }
}
setInterval(refreshPanel, 3000);
let resizeTimer = null;
window.addEventListener('resize', () => { clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => { panelState.version = -1; refreshPanel(); }, 300); });

let initial = 'split'; try { initial = localStorage.getItem('mugemuge.view') || 'split'; } catch (_) {}
if (window.matchMedia('(max-width: 860px)').matches && initial === 'split') initial = 'chat';
setView(initial);
"""
