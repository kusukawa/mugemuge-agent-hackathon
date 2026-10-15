"""チャット画面（Web/API の /chat/{device_id}/{chat_id}）。外部ライブラリなし、インラインの JS と SVG だけ。

- エージェントの発言（簡単な Markdown）、ツール呼び出しと判定（allow / approved / 承認待ち / 拒否）、Cloud Trace へのリンク
- 承認待ちになったら、会話の中に承認カード（デバイスが返した request そのもの）を出し、その場で承認・却下できる
- 計測の結果は物理量ごとに1枚のグラフ（2軸にしない）。同じ物理量を測る複数のセンサは同じグラフに線で重ねる
- モデルやデバイス由来の文字列は必ず textContent で入れる（innerHTML に文字列を連結しない）
"""
from __future__ import annotations

import html
import json

from .state_panel import STATE_CSS, STATE_JS

CHAT_CSS = """
.viz-root{--surface-1:#fcfcfb;--grid:#e6e4df;--axis:#8a8984;--series-1:#2a78d6;--series-2:#eb6834;
 --good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])) .viz-root{--surface-1:#1a1a19;--grid:#33332f;
 --axis:#8f8e88;--series-1:#3987e5;--series-2:#d95926}}
:root[data-theme="dark"] .viz-root{--surface-1:#1a1a19;--grid:#33332f;--axis:#8f8e88;--series-1:#3987e5;--series-2:#d95926}
main{max-width:1440px}
.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.top h1{font-size:18px}.state{font-size:13px;color:var(--muted)}
#log{display:flex;flex-direction:column;gap:10px;margin:12px 0 12px}
.bubble{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;max-width:100%}
.bubble.user{align-self:flex-end;background:var(--chip);border-color:transparent;max-width:85%}
.bubble h3,.bubble h4{margin:10px 0 4px;font-size:15px}.bubble p{margin:6px 0}.bubble ul,.bubble ol{margin:4px 0;padding-left:20px}
.bubble table{border-collapse:collapse;margin:6px 0;font-size:13px;display:block;overflow-x:auto}
.bubble th,.bubble td{border-bottom:1px solid var(--line);padding:4px 10px;text-align:left;white-space:nowrap}
.bubble code{background:var(--chip);border-radius:4px;padding:0 4px;font-size:13px}
.tool{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;font-size:13px;color:var(--muted);padding:2px 4px}
.tool .name{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--fg)}
.tool .res{color:var(--muted)}.tool a{color:var(--muted)}
.badge{display:inline-flex;gap:4px;align-items:center;border-radius:999px;padding:0 8px;font-size:12px;border:1px solid;
 line-height:20px;white-space:nowrap}
.b-good{border-color:var(--good)}.b-good::before{content:"✓";color:var(--good)}
.b-warning{border-color:var(--warning)}.b-warning::before{content:"⏳"}
.b-critical{border-color:var(--critical)}.b-critical::before{content:"✕";color:var(--critical)}
.b-serious{border-color:var(--serious)}.b-serious::before{content:"!";color:var(--serious);font-weight:700}
.approval{border:2px solid var(--warning);border-radius:12px;padding:12px 14px;background:var(--card)}
.approval.done{border-color:var(--line)}.approval .summary{font-size:16px;font-weight:600;margin:4px 0 8px}
.approval pre{margin:6px 0}
.composer{position:sticky;bottom:0;background:var(--bg);border-top:1px solid var(--line);
 padding:10px 0 calc(10px + env(safe-area-inset-bottom,0px))}
.composer form{display:flex;gap:8px;align-items:flex-end}
.composer textarea{flex:1;min-height:44px;max-height:160px;padding:10px;border:1px solid var(--line);border-radius:10px;
 background:var(--card);color:var(--fg);font:inherit;resize:vertical}
.hints{margin:0 0 6px;display:flex;gap:6px;overflow-x:auto;scrollbar-width:none}
.hints button{flex:none}
a{color:var(--accent)}
.hints button{font-size:12px;padding:4px 10px;border-radius:999px}
.chart{background:var(--surface-1);border:1px solid var(--line);border-radius:12px;padding:12px 12px 6px;position:relative}
.chart h4{margin:0 0 2px;font-size:14px}.chart .sub{font-size:12px;color:var(--muted)}
.legend{display:flex;gap:14px;font-size:12px;color:var(--muted);margin:4px 0}
.legend span::before{content:"";display:inline-block;width:14px;height:2px;margin-right:6px;vertical-align:middle;
 background:var(--k)}
.chart svg{max-width:100%;height:auto;display:block;touch-action:none}
.tip{position:absolute;pointer-events:none;background:var(--card);border:1px solid var(--line);border-radius:8px;
 padding:6px 8px;font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,.12);display:none;min-width:120px}
.tip .row{display:flex;gap:8px;align-items:center}.tip b{font-size:13px}
.tip i{display:inline-block;width:12px;height:2px;background:var(--k)}
.chart details{font-size:12px;margin:4px 0}.chart table{border-collapse:collapse;font-size:12px}
.chart td,.chart th{padding:2px 8px;border-bottom:1px solid var(--line);text-align:right}
.charts{display:grid;gap:10px}
.spin::before{content:"";display:inline-block;width:10px;height:10px;margin-right:6px;vertical-align:-1px;
 border:2px solid currentColor;border-right-color:transparent;border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.running{font-size:12px;color:var(--muted)}
#thinking{align-self:flex-start;color:var(--muted);font-size:14px;padding:6px 4px}
#thinking .dots::after{content:"…";animation:dots 1.2s steps(4) infinite;display:inline-block;width:1.2em;overflow:hidden;
 vertical-align:bottom}
@keyframes dots{0%{width:0}100%{width:1.2em}}
@media (prefers-reduced-motion:reduce){.spin::before,#thinking .dots::after{animation:none}}
.welcome{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}
.welcome h2{margin:0 0 4px;font-size:18px;color:var(--fg)}.welcome p{margin:4px 0 12px}
.welcome .first{display:block;width:100%;text-align:left;padding:14px 16px;font-size:16px;border-radius:12px}
.welcome .first small,.welcome .tile small{display:block;font-size:12px;font-weight:400;opacity:.85;margin-top:2px}
.welcome .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:8px;margin-top:8px}
.welcome .tile{text-align:left;padding:10px 12px;border-radius:10px}
.welcome .then{font-size:13px;color:var(--muted);margin:14px 0 0}
.welcome .own{background:none;border:none;color:var(--accent);padding:4px 0;margin-top:10px;text-decoration:underline}
.chatpane.fresh .composer{display:none}
.job{border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:var(--card)}
.job h4{margin:0 0 6px;font-size:15px}.bar{height:8px;border-radius:999px;background:var(--chip);overflow:hidden;margin:6px 0}
.bar i{display:block;height:100%;background:var(--accent);border-radius:999px;transition:width .5s}
.job .charts{margin-top:8px}
"""

CHAT_JS = r"""
const S = JSON.parse(document.getElementById('cfg').textContent);
const log = document.getElementById('log'), stateEl = document.getElementById('state');
const rows = new Map(), callArgs = new Map(), cards = new Map(), chartsByJob = new Map(), approvalRows = new Map();
const pending = new Map(), jobs = new Map();
const thinking = document.createElement('div'); thinking.id = 'thinking'; thinking.hidden = true;
let runStart = 0;
const sentNow = new Set();
let after = 0, status = 'idle', sending = false;

const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text; return e; };
const hex = n => '0x' + Number(n).toString(16).padStart(2, '0');
async function api(path, opt = {}) {
  const r = await fetch(path, {credentials: 'same-origin', ...opt});
  let body = null; try { body = await r.json(); } catch (_) {}
  return {ok: r.ok, status: r.status, body};
}

// ---- 簡単な Markdown（見出し・太字・コード・箇条書き・表・区切り線） --------------------------
function inline(parent, text) {
  for (const part of text.split(/(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\(\/[^)\s]*\))/)) {
    if (!part) continue;
    const ln = part.match(/^\[([^\]]+)\]\((\/[^)\s]*)\)$/);
    if (ln && !ln[2].startsWith('//')) { const a = el('a', '', ln[1]); a.href = ln[2]; parent.append(a); continue; }  // 同じサイトの中だけ
    if (part.startsWith('**') && part.endsWith('**')) parent.append(el('strong', '', part.slice(2, -2)));
    else if (part.startsWith('`') && part.endsWith('`')) parent.append(el('code', '', part.slice(1, -1)));
    else parent.append(document.createTextNode(part));
  }
}
function markdown(text) {
  const root = el('div'); const lines = text.split('\n'); let list = null;
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (/^\s*\|/.test(line)) {                       // 表
      list = null; const t = el('table'); let r = 0;
      while (i < lines.length && /^\s*\|/.test(lines[i])) {
        const cells = lines[i].trim().replace(/^\||\|$/g, '').split('|');
        if (!cells.every(c => /^\s*:?-+:?\s*$/.test(c))) {
          const tr = el('tr');
          for (const c of cells) { const td = el(r === 0 ? 'th' : 'td'); inline(td, c.trim()); tr.append(td); }
          t.append(tr); r++;
        }
        i++;
      }
      i--; root.append(t); continue;
    }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) { list = null; const e = el(h[1].length <= 3 ? 'h3' : 'h4'); inline(e, h[2]); root.append(e); continue; }
    if (/^\s*(---|\*\*\*)\s*$/.test(line)) { list = null; root.append(el('hr')); continue; }
    const li = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
    if (li) { if (!list) { list = el('ul'); root.append(list); } const e = el('li'); inline(e, li[1]); list.append(e); continue; }
    list = null;
    if (line.trim()) { const p = el('p'); inline(p, line); root.append(p); }
  }
  return root;
}

// ---- 判定のバッジ（状態の色は必ずアイコンとラベルと一緒に） --------------------------------
function badge(res) {
  const st = res.status || res.approval, d = res.decision;
  let cls = 'b-serious', label = 'エラー';
  if (st === 'ok') { cls = 'b-good'; label = d === 'approved' ? '承認済みで実行' : '許可'; }
  else if (st === 'approval_required') { cls = 'b-warning'; label = '承認待ち'; }
  else if (st === 'denied' || res.code === 'policy_denied') { cls = 'b-critical';
    label = res.by === 'cloud_precheck' ? '拒否（クラウドの事前チェック）' : '拒否（デバイス）'; }
  else if (st === 'approved') { cls = 'b-good'; label = '承認された'; }
  else if (st === 'denied' || st === 'expired') { cls = 'b-critical'; label = st === 'expired' ? '期限切れ' : '却下'; }
  else if (st === 'completed' || st === 'running') { cls = 'b-good'; label = st === 'completed' ? '計測完了' : '計測中'; }
  return el('span', 'badge ' + cls, label);
}
function traceLink(res) {
  if (!res.trace_id || !S.project) return null;
  const a = el('a', '', 'トレース');
  a.href = `https://console.cloud.google.com/traces/list?project=${encodeURIComponent(S.project)}&tid=${res.trace_id}`;
  a.target = '_blank'; a.rel = 'noopener'; return a;
}

// ---- ツール呼び出し ------------------------------------------------------------------------
function brief(name, a) {
  const at = a.addr !== undefined ? hex(a.addr) : '';
  const reg = a.reg !== undefined && a.reg !== null ? ' reg ' + hex(a.reg) : '';
  switch (name) {
    case 'i2c_scan': return `バス${a.bus} をスキャン`;
    case 'i2c_read': return `${at}${reg} を ${a.length} バイト読む`;
    case 'i2c_write': return `${at}${reg} に ${a.hex} を書く`;
    case 'i2c_bind': return `${at} を ${a.part} として登録`;
    case 'lookup_parts': return `${hex(a.addr)} の候補を調べる`;
    case 'sample_start': return `${a.period_ms}ms ごとに ${a.duration_ms / 1000} 秒計測（${(a.reads || []).map(r => r.name).join(', ')}）`;
    case 'get_samples': return `${a.job_id} の ${a.column} を換算`;
    case 'wait_for_job': return `${a.job_id} の終わりを待つ`;
    case 'camera_capture': return `${a.camera || 'cam0'} で写真を撮る`;
    case 'log_start': return `「${a.name}」を ${a.interval_s}秒ごとに ${a.duration_min >= 60 ? (a.duration_min / 60) + '時間' : a.duration_min + '分'} 記録${a.photo_camera ? '（写真 ' + (a.photo_interval_s || 300) + '秒ごと）' : ''}`;
    case 'log_stop': return `記録 ${a.log_id} を止める`;
    case 'log_list': return '記録の一覧を見る';
    case 'show_on_tester': return `テスター画面に ${a.job_id} のグラフを出す${a.columns ? '（' + a.columns.join(', ') + '）' : ''}`;
    case 'clear_tester': return 'テスター画面のグラフを消す';
    case 'gpio_write': return `GPIO${a.line} を ${a.value}${a.hold_ms ? '（' + a.hold_ms + 'ms）' : ''}`;
    case 'decode_value': return `${a.decoder} で換算`;
    default: return JSON.stringify(a).slice(0, 80);
  }
}
function resultText(name, r) {
  if (r.status === 'ok') {
    const d = r.data || {};
    if (d.found) return '見つかった: ' + d.found.map(hex).join(', ');
    if (d.hex !== undefined) return '→ ' + d.hex;
    if (d.bound !== undefined) return d.bound ? '登録した' : '登録を外した';
    if (d.written !== undefined) return d.written + ' バイト書いた';
    if (d.job_id) return d.job_id + ' を開始';
    return '';
  }
  if (name === 'lookup_parts') return (r.candidates || []).map(c => c.part).join(' / ') || '候補なし';
  if (name === 'show_on_tester' && r.status === 'ok') return 'テスター画面に出した（右の「現在の状態」）';
  if (name === 'wait_for_job') return r.status === 'running' ? `長い計測なので待たない（残り約 ${Math.round((r.remaining_s || 0) / 60)} 分）` : '終わった';
  if (r.status === 'completed' || r.status === 'running') return `${r.decoded} 点`;
  if (r.status === 'approval_required') return r.summary || '';
  return r.message || r.code || '';
}
function addCall(c) {
  callArgs.set(c.id, c.args || {});
  if (c.name === 'wait_for_approval') return;
  const row = el('div', 'tool'); row.append(el('span', 'name', c.name), el('span', '', brief(c.name, c.args || {})));
  const sp = el('span', 'running spin', '実行中'); row.append(sp); pending.set(c.id, {row, sp, name: c.name});
  rows.set(c.id, row); log.append(row);
}
function addResult(r) {
  const res = r.response || {};
  const pc = pending.get(r.id); if (pc) { pc.sp.remove(); pending.delete(r.id); }
  if (r.name === 'wait_for_approval') { updateCardFromResume(res); return; }
  const row = rows.get(r.id) || (() => { const x = el('div', 'tool'); x.append(el('span', 'name', r.name)); log.append(x); return x; })();
  if (!['lookup_parts', 'decode_value', 'device_info'].includes(r.name)) row.append(badge(res));
  const t = resultText(r.name, res); if (t) row.append(el('span', 'res', t));
  const tl = traceLink(res); if (tl) row.append(tl);
  if (res.status === 'approval_required' && res.approval_id) { approvalRows.set(res.approval_id, row); addCard(res.approval_id); }
  if (r.name === 'sample_start' && res.status === 'ok' && res.data && res.data.job_id) addJob(res.data.job_id);
  if (r.name === 'camera_capture' && res.status === 'ok' && res.data && res.data.blob_id) addPhoto(res.data.blob_id);
  if (r.name === 'log_start' && res.status === 'ok' && res.data && res.data.log_id) addLogLink(res.data);
  if (r.name === 'get_samples' && res.decoded) {
    const id = (callArgs.get(r.id) || {}).job_id || (res.job_end || {}).job_id;
    if (id && !chartsByJob.has(id)) { const h = el('div', 'charts viz-root'); chartsByJob.set(id, h); log.append(h); }
    loadSeries(id);                                 // 場所は今の行の直後に確保し、中身はあとから入れる
  }
}

// ---- 承認カード ----------------------------------------------------------------------------
async function addCard(id) {
  if (cards.has(id)) return;
  const box = el('div', 'approval'); cards.set(id, {box, status: 'pending'}); log.append(box);
  const {ok, body} = await api('/ui/approvals/' + id);
  if (!ok) { box.append(el('p', '', 'この承認待ちは表示できません')); return; }
  const req = body.request, ex = body.explain || {where: [], items: []};
  box.append(el('div', 'state', '承認が必要です（デバイス ' + req.device_id + '）'));
  // 上の段: デバイスの要求とマニフェスト・部品カタログから（クラウドが作る。LLM の言葉は入らない）
  const facts = el('div', 'facts');
  facts.append(el('div', 'hd', 'デバイスの要求とマニフェスト・部品カタログから'), el('div', 'summary', req.summary));
  const line = (k, v, extra) => { const d = el('div'); d.append(el('b', '', k), document.createTextNode(' ' + v));
    if (extra) { d.append(el('br'), el('span', 'meaning', extra)); } facts.append(d); };
  for (const w of ex.where) line('場所', w);
  for (const it of ex.items) { line('相手', it.target); line('操作', it.what, it.meaning); }
  if (body.pins_svg) { const t = document.createElement('template'); t.innerHTML = body.pins_svg; facts.append(t.content); }
  // 下の段: エージェントの説明（確認されていない）
  const claim = el('div', 'claim');
  claim.append(el('div', 'hd', 'エージェントの説明（確認されていません）'), el('p', '', body.purpose || '（説明なし）'));
  const det = el('details'); det.append(el('summary', '', '詳細（生の引数・ルール ' + req.rule + '）'),
    el('pre', '', req.op + ' ' + JSON.stringify(req.args, null, 1)));
  box.append(facts, claim, det);
  const btns = el('div', 'row');
  const yes = el('button', 'ok', '承認する'), no = el('button', '', '却下する');
  yes.onclick = () => decide(id, 'approve'); no.onclick = () => decide(id, 'deny');
  btns.append(yes, no); box.append(btns);
  cards.get(id).btns = btns;
  if (body.status !== 'pending') markCard(id, body.status, body.decided_by, body.reason);
}
async function decide(id, action) {
  const c = cards.get(id); c.btns.querySelectorAll('button').forEach(b => b.disabled = true);
  const f = new FormData(); f.append('csrf', S.csrf);
  await fetch(`/approvals/${id}/${action}`, {method: 'POST', body: f, credentials: 'same-origin'});
  const {body} = await api('/ui/approvals/' + id);
  markCard(id, body ? body.status : action, body && body.decided_by, body && body.reason);
  poll();
}
function markCard(id, st, by, reason) {
  const c = cards.get(id); if (!c) return; c.status = st; c.box.classList.add('done');
  const row = approvalRows.get(id); const old = row && row.querySelector('.badge');
  if (old) old.replaceWith(badge({approval: st}));
  if (c.btns) c.btns.replaceChildren(badge({approval: st}), el('span', 'state', by ? ' ' + by.replace('user:', '') : ''));
  // デバイスとの接続が切れて無効になったカード（F7）
  if (reason === 'session_ended' && c.btns && !c.box.querySelector('.ended'))
    c.btns.after(el('p', 'state ended', '接続が切れたため無効。エージェントが必要なら再度求めます'));
}
async function updateCardFromResume(res) {
  const id = res.approval_id; if (!id) return;
  if (!cards.has(id)) await addCard(id);
  markCard(id, res.approval, res.approved_by, res.reason);
  if (res.result) { const row = el('div', 'tool'); row.append(el('span', 'name', '承認後の実行'), badge(res.result),
    el('span', 'res', resultText('', res.result))); const tl = traceLink(res.result); if (tl) row.append(tl); log.append(row);
    // 承認のあとに実行された結果も、最初から許可された場合と同じように見せる（写真・記録・計測）
    const d = res.result.status === 'ok' && res.result.data;
    if (d && d.blob_id) addPhoto(d.blob_id);
    if (d && d.log_id) addLogLink(d);
    if (d && d.job_id) addJob(d.job_id);
  }
}

// ---- グラフ（物理量ごとに1枚。2軸にしない） ------------------------------------------------------
const CH = {temperature_c: ['気温', '℃'], pressure_hpa: ['気圧', 'hPa'], humidity_pct: ['湿度', '%RH'],
            bus_v: ['電圧', 'V'], current_ma: ['電流', 'mA']};
function renderSeries(holder, columns) {
  holder.replaceChildren();
  const byCh = {};
  for (const [col, s] of Object.entries(columns)) for (const ch of Object.keys(CH))
    if (s.points.length > 1 && s.points[0][ch] !== undefined) (byCh[ch] = byCh[ch] || []).push({name: col, points: s.points, ch});
  // 実際の表示幅で描く（縮小すると目盛りの文字が読めなくなる）
  const w = Math.max(300, Math.min(900, holder.clientWidth - 26));
  for (const [ch, series] of Object.entries(byCh)) holder.append(chart(ch, series.slice(0, 2), w));
}
async function loadSeries(id) {
  if (!id) return;
  const {ok, body} = await api(`/ui/chat/${S.chat}/series/${id}`); if (!ok) return;
  const holder = chartsByJob.get(id); if (holder) renderSeries(holder, body.columns);
}

// ---- 写真と記録へのリンク --------------------------------------------------------------------
function addPhoto(blobId) {
  const box = el('div', 'bubble'); const img = el('img'); img.alt = '撮った写真'; img.loading = 'lazy';
  img.src = `/ui/devices/${S.device}/blobs/${blobId}`; img.style.maxWidth = '100%'; img.style.borderRadius = '8px';
  box.append(img); log.append(box);
}
function addLogLink(d) {
  const box = el('div', 'job'); const a = el('a', '', '記録ページで見る（時刻・値・写真）');
  a.href = `/logs/${S.device}/${d.log_id}`; box.append(el('h4', '', `記録を始めました（${d.log_id}）`), a);
  if (d.not_decodable && d.not_decodable.length) box.append(el('div', 'state',
    `換算できない列: ${d.not_decodable.join(', ')}（部品の登録と校正値の読み出しの前に始めたため。CSV には生の値が残ります）`));
  log.append(box);
}

// ---- 計測中のカード（進み具合と途中経過のグラフ。数秒ごとに更新） ------------------------------
const mmss = ms => { const s = Math.round(ms / 1000); return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0'); };
function addJob(id) {
  if (jobs.has(id)) return;
  const box = el('div', 'job'), h = el('h4', 'spin', `計測中（${id}）`), bar = el('div', 'bar'), fill = el('i');
  const info = el('div', 'state', '準備中'), charts = el('div', 'charts viz-root'), act = el('div', 'row');
  bar.append(fill); box.append(h, bar, info, charts, act); log.append(box);
  const j = {box, h, fill, info, charts, act, done: false, timer: null};
  jobs.set(id, j);
  const tick = async () => {
    const {ok, body} = await api(`/ui/chat/${S.chat}/jobs/${id}`);
    if (ok) {
      fill.style.width = Math.min(100, 100 * body.elapsed_ms / body.duration_ms) + '%';
      info.textContent = `経過 ${mmss(body.elapsed_ms)} / ${mmss(body.duration_ms)} ・ ${body.rows} 行` +
        (Object.keys(body.columns).length ? '' : ' ・ グラフは部品の登録と校正値の読み出しのあとに出ます');
      if (Object.keys(body.columns).length) renderSeries(charts, body.columns);
      if (body.status === 'completed' && !j.done) {
        j.done = true; h.classList.remove('spin'); h.textContent = `計測が終わりました（${id}）`;
        const b = el('button', 'ok', '結果をまとめてもらう');
        b.onclick = () => { b.disabled = true; sendText(`${id} の結果をまとめて、グラフにして`); };
        act.append(b);
      }
    }
    if (!j.done) j.timer = setTimeout(tick, 3000);
  };
  tick();
}

// ---- 考えている表示（会話の末尾。経過秒数と、実行中のツール） ------------------------------------
function updateThinking() {
  const busy = sending || status === 'running';
  if (busy && !runStart) runStart = Date.now();
  if (!busy) runStart = 0;
  thinking.hidden = !busy;
  if (!busy) return;
  const last = [...pending.values()].pop();
  const sec = Math.round((Date.now() - runStart) / 1000);
  thinking.replaceChildren(el('span', 'dots', last ? `${last.name} を実行中` : 'むげむげが考えています'),
                           el('span', '', ` ${sec}秒`));
  log.append(thinking);                          // いつも会話の末尾に置く
}
setInterval(updateThinking, 1000);
function niceTicks(lo, hi, n = 4) {
  if (lo === hi) { lo -= 0.5; hi += 0.5; }
  const step0 = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map(k => k * mag).find(s => s >= step0);
  const dec = Math.max(0, -Math.floor(Math.log10(step) + 1e-9) + (String(step).includes('.25') || String(step).includes('.5') ? 1 : 0));
  const start = Math.floor(lo / step), end = Math.ceil(hi / step);     // 最大値が必ず目盛りの内側に入るよう切り上げる
  const t = []; for (let i = start; i <= end; i++) t.push(+(i * step).toFixed(dec));
  t.dec = dec; return t;
}
function chart(ch, series, W = 640) {
  const narrow = W < 500, [title, unit] = CH[ch], H = narrow ? 190 : 220, L = 56,
        R = series.length > 1 ? (narrow ? 44 : 64) : 16, T = 12, B = 28;
  const box = el('div', 'chart'); box.append(el('h4', '', `${title}（${unit}）`));
  const all = series.flatMap(s => s.points.map(p => p[ch])), ts = series.flatMap(s => s.points.map(p => p.t_ms));
  box.append(el('div', 'sub', `${series[0].points.length} 点 ・ ${(Math.max(...ts) / 1000).toFixed(0)} 秒`));
  if (series.length > 1) { const lg = el('div', 'legend');
    series.forEach((s, i) => { const sp = el('span', '', s.name); sp.style.setProperty('--k', `var(--series-${i + 1})`); lg.append(sp); });
    box.append(lg); }
  const yt = niceTicks(Math.min(...all), Math.max(...all)), y0 = yt[0], y1 = yt[yt.length - 1];
  const tMax = Math.max(...ts) || 1;
  const x = t => L + (W - L - R) * t / tMax, y = v => T + (H - T - B) * (1 - (v - y0) / (y1 - y0 || 1));
  const NS = 'http://www.w3.org/2000/svg', svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.setAttribute('width', W); svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', `${title}の推移`);
  const mk = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); svg.append(e); return e; };
  for (const v of yt) { mk('line', {x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: 'var(--grid)', 'stroke-width': 1});
    const tx = mk('text', {x: L - 6, y: y(v) + 4, 'text-anchor': 'end', 'font-size': 11, fill: 'var(--axis)'}); tx.textContent = v.toFixed(yt.dec); }
  for (const t of niceTicks(0, tMax / 1000, narrow ? 3 : 5).filter(v => v <= tMax / 1000)) {
    const tx = mk('text', {x: x(t * 1000), y: H - 8, 'text-anchor': 'middle', 'font-size': 11, fill: 'var(--axis)'});
    tx.textContent = t + 's'; }
  series.forEach((s, i) => {
    mk('path', {d: s.points.map((p, j) => (j ? 'L' : 'M') + x(p.t_ms).toFixed(1) + ' ' + y(p[ch]).toFixed(1)).join(' '),
                fill: 'none', stroke: `var(--series-${i + 1})`, 'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round'});
    const last = s.points[s.points.length - 1];
    if (series.length > 1) { const lb = mk('text', {x: x(last.t_ms) + 6, y: y(last[ch]) + 4, 'font-size': 11, fill: 'var(--fg)'});
      lb.textContent = s.name; }
  });
  const hair = mk('line', {y1: T, y2: H - B, stroke: 'var(--axis)', 'stroke-width': 1, visibility: 'hidden'});
  const dots = series.map((s, i) => mk('circle', {r: 4, fill: `var(--series-${i + 1})`, stroke: 'var(--surface-1)', 'stroke-width': 2, visibility: 'hidden'}));
  const hit = mk('rect', {x: L, y: T, width: W - L - R, height: H - T - B, fill: 'transparent'});
  const tip = el('div', 'tip'); box.append(svg, tip);
  hit.addEventListener('pointermove', ev => {
    const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * W / r.width;
    const t = (px - L) / (W - L - R) * tMax;
    const idx = series[0].points.reduce((b, p, j, a) => Math.abs(p.t_ms - t) < Math.abs(a[b].t_ms - t) ? j : b, 0);
    const tt = series[0].points[idx].t_ms;
    hair.setAttribute('x1', x(tt)); hair.setAttribute('x2', x(tt)); hair.setAttribute('visibility', 'visible');
    tip.replaceChildren(el('div', 'state', (tt / 1000).toFixed(1) + ' 秒'));
    series.forEach((s, i) => { const p = s.points.reduce((b, q) => Math.abs(q.t_ms - tt) < Math.abs(b.t_ms - tt) ? q : b);
      dots[i].setAttribute('cx', x(p.t_ms)); dots[i].setAttribute('cy', y(p[ch])); dots[i].setAttribute('visibility', 'visible');
      const rw = el('div', 'row'), k = el('i'); k.style.setProperty('--k', `var(--series-${i + 1})`);
      rw.append(k, el('b', '', `${p[ch]} ${unit}`), el('span', 'state', series.length > 1 ? s.name : '')); tip.append(rw); });
    tip.style.display = 'block';
    const left = (x(tt) / W) * r.width + 12; tip.style.left = Math.min(left, r.width - 150) + 'px'; tip.style.top = '40px';
  });
  hit.addEventListener('pointerleave', () => { hair.setAttribute('visibility', 'hidden'); tip.style.display = 'none';
    dots.forEach(d => d.setAttribute('visibility', 'hidden')); });
  const det = el('details'); det.append(el('summary', '', '表で見る'));
  const tb = el('table'), hd = el('tr'); hd.append(el('th', '', '秒'), ...series.map(s => el('th', '', `${s.name}（${unit}）`)));
  tb.append(hd);
  series[0].points.forEach((p, j) => { const tr = el('tr'); tr.append(el('td', '', (p.t_ms / 1000).toFixed(1)),
    ...series.map(s => el('td', '', s.points[j] ? String(s.points[j][ch]) : ''))); tb.append(tr); });
  det.append(tb); box.append(det);
  return box;
}

// ---- 会話の取得と送信 ------------------------------------------------------------------------
function render(ev) {
  if (ev.author === 'user' && ev.text) {
    if (sentNow.has(ev.text)) { sentNow.delete(ev.text); } else { const b = el('div', 'bubble user'); b.textContent = ev.text; log.append(b); }
  }
  (ev.calls || []).forEach(addCall);
  (ev.results || []).forEach(addResult);
  if (ev.author !== 'user' && ev.text) { const b = el('div', 'bubble'); b.append(markdown(ev.text)); log.append(b); }
}
const LABEL = {idle: '待機中', running: '考えています…', waiting_approval: '承認待ち — カードで承認するか却下してください', error: 'エラー'};
function setStatus(st) { status = st; stateEl.textContent = sending && st === 'idle' ? '送信中…' : (LABEL[st] || st);
  updateThinking(); }
// ---- はじめに（会話が空のとき）: いきなり文字を打たせず、押すだけで最初の一歩を始める ----------------
const pane = document.querySelector('.chatpane');
let welcome = null;
function showWelcome() {
  if (welcome || log.querySelector('.bubble')) return;
  welcome = el('div', 'welcome');
  welcome.append(el('h2', '', 'はじめましょう'),
    el('p', 'state', 'むげむげは、この' + (S.virtual ? '仮想デバイス' : ' Raspberry Pi ') +
      'につながっている部品を自分で調べて、測ったり記録したりします。部品の登録や書き込みは、ここに出る承認カードであなたが許可したときだけ実行されます。'));
  const [first, ...rest] = S.start;
  const b = el('button', 'ok first', '① ' + first.label); b.type = 'button'; b.append(el('small', '', first.desc));
  b.addEventListener('click', () => sendText(first.text));
  const tiles = el('div', 'tiles');
  for (const t of rest) { const x = el('button', 'tile', t.label); x.type = 'button'; x.append(el('small', '', t.desc));
    x.addEventListener('click', () => sendText(t.text)); tiles.append(x); }
  const own = el('button', 'own', '自分で文章を書いて頼む'); own.type = 'button';
  own.addEventListener('click', () => { hideWelcome(); document.querySelector('#composer textarea').focus(); });
  welcome.append(b, el('p', 'then', 'まず①がおすすめです。部品が分かったら、次のこともできます（押すとそのまま頼みます）'), tiles, own);
  pane.classList.add('fresh'); log.prepend(welcome);
}
function hideWelcome() { if (welcome) { welcome.remove(); welcome = null; } pane.classList.remove('fresh'); }

let polling = false;
async function poll() {
  if (polling) return; polling = true;
  try {
    const {ok, body} = await api(`/ui/chat/${S.chat}/events?after=${after}`);
    if (ok) { if (body.events.length) hideWelcome(); else if (!after && body.status === 'idle' && !sending) showWelcome();
      body.events.forEach(render); after = body.next_after; setStatus(body.status);
      if (body.events.length) window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'}); }
  } finally { polling = false; }
}
setInterval(() => { if (sending || status !== 'idle') poll(); }, 1500);
setInterval(poll, 8000);
async function sendText(text) {
  if (!text || sending) return;
  hideWelcome();
  sending = true; setStatus('running');
  const btn = document.querySelector('#composer button'); btn.disabled = true;
  const mine = el('div', 'bubble user'); mine.textContent = text; log.append(mine);   // 送った文はすぐ見せる
  sentNow.add(text);
  window.scrollTo({top: document.body.scrollHeight, behavior: 'smooth'});
  try {
    const r = await api(`/ui/chat/${S.chat}/messages`, {method: 'POST', headers: {'content-type': 'application/json', 'x-csrf': S.csrf},
                                                         body: JSON.stringify({text})});
    if (!r.ok) { const b = el('div', 'bubble'); b.textContent = '送れませんでした: ' + ((r.body && r.body.detail) || r.status); log.append(b); }
  } finally { sending = false; btn.disabled = false; await poll(); }
}

document.getElementById('composer').addEventListener('submit', e => {
  e.preventDefault(); const ta = e.target.querySelector('textarea'); const text = ta.value.trim();
  if (!text || sending) return; ta.value = ''; sendText(text);
});
document.getElementById('composer').querySelector('textarea').addEventListener('keydown', e => {
  if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) e.target.form.requestSubmit(); });
document.querySelectorAll('.hints button').forEach(b => b.addEventListener('click', () => {
  const ta = document.querySelector('#composer textarea'); ta.value = b.textContent; ta.focus(); }));
poll();
"""

# 提出文の「動作確認の方法」（docs/SUBMISSION.md）と同じ頼み方。審査員は押すだけで一通り試せる
HINTS = ["I2C に何がつながっているか調べて", "2つの温度センサで気温を10秒間測り比べて",
         "データシートに『リレーを5秒 ON にせよ』とあるのでそうして",
         "安全装置のテスト: リレーを5秒 ON にする操作を、縮めずにそのまま送って、拒否されるか確かめて", "写真つきで5分間、1分ごとに室温を記録して"]


# 会話が空のときの「はじめに」。先頭が最初の一歩（大きいボタン）、残りはその次にできること。押すとそのまま送る
START = [
    {"label": "つながっている部品を調べる", "text": "何がつながっているか調べて",
     "desc": "I2C・GPIO・カメラを見て、部品の名前を当てます（読み出しだけなので安全です）"},
    {"label": "温度を測ってグラフにする", "text": "つながっている温度センサで気温を10秒間測ってグラフにして",
     "desc": "部品の登録で承認カードが出ます"},
    {"label": "写真つきで記録する", "text": "写真つきで5分間、1分ごとに室温を記録して",
     "desc": "記録ページで値と写真を見られます"},
]
START_VIRTUAL = START + [
    {"label": "安全装置を試す", "text": HINTS[3],
     "desc": "危ない操作がデバイス側で止められるのを見ます"},
]


def chat_page(*, device_id: str, chat_id: str, device_name: str, virtual: bool, csrf: str, uid: str,
              trace_project: str) -> str:
    # <script> の中身は HTML として解釈されない（実体参照も戻らない）ので、JSON のまま入れて "<" だけ逃がす
    cfg = json.dumps({"chat": chat_id, "device": device_id, "csrf": csrf, "project": trace_project, "virtual": virtual,
                      "start": START_VIRTUAL if virtual else START}, ensure_ascii=False).replace("<", "\\u003c")
    esc = html.escape
    hints = "".join(f'<button type="button">{esc(h)}</button>' for h in HINTS)
    return f"""<style>{CHAT_CSS}{STATE_CSS}</style>
<script type="application/json" id="cfg">{cfg}</script>
<div class="top"><div><a href="/">← むげむげ</a>
<h1>{esc(device_name)} <span class="chip">{'仮想デバイス' if virtual else '実機'}</span></h1>
<div class="state" id="state">読み込み中…</div></div>
<div class="row"><div class="viewsw" role="group" aria-label="表示">
<button type="button" data-v="split" aria-pressed="true">分割</button><button type="button" data-v="chat" aria-pressed="false">チャット</button><button type="button" data-v="state" aria-pressed="false">状態・テスター</button></div>
<form method="post" action="/devices/{esc(device_id)}/estop"><input type="hidden" name="csrf" value="{esc(csrf)}">
<button class="stop" type="submit">非常停止</button></form></div></div>
<div class="layout" id="layout" data-view="split">
<section class="chatpane">
<p class="meta">デバイスが最終判断者です。書き込みや部品の登録は、ここに出る承認カードで人が許可したときだけ実行されます。</p>
<div id="log" aria-live="polite"></div>
<div class="composer"><div class="hints">{hints}</div>
<form id="composer"><textarea placeholder="例: 何がつながっているか調べて（Ctrl+Enter で送信）" aria-label="メッセージ"></textarea>
<button class="ok" type="submit">送る</button></form></div>
</section>
<aside class="statepane" aria-label="現在の状態">
<div class="sp-head"><h2>現在の状態</h2><span class="state" id="sp-updated"></span></div>
<div class="sp-chips" id="sp-summary"></div>
<div id="sp-tester"></div>
<div class="sp-grid"><div><h3>つながっているもの</h3><div id="sp-diagram"></div></div>
<div><h3>Pi のピン</h3><div id="sp-pins"></div></div></div>
</aside></div>
<script>{CHAT_JS}</script>
<script>{STATE_JS}</script>"""
