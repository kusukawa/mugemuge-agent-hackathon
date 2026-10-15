"""記録ページのデータ: バッチを換算し、表示範囲に合わせて間引き、CSV に書き出す。

換算は記録の情報の decoders（Agent が開始時に登録する列ごとのデコーダと校正値）で行う。
登録のない列は換算できないので、画面では「換算できない列」として扱う（生の16進は CSV に残す）。
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone

from ..agent import parts as parts_mod

JST = timezone(timedelta(hours=9))
CHANNELS = {"temperature_c": ("気温", "℃"), "pressure_hpa": ("気圧", "hPa"), "humidity_pct": ("湿度", "%RH"),
            "bus_v": ("電圧", "V"), "current_ma": ("電流", "mA"), "shunt_mv": ("シャント電圧", "mV")}


def decode_rows(meta: dict, batches: list[dict]) -> tuple[list[dict], list[list], list[dict]]:
    """(列ごとの系列, 生の行, 写真) を返す。時刻は UNIX ミリ秒（記録の開始時刻＋t）。"""
    started = meta.get("started_ts") or 0
    cols = list(meta.get("columns", ["t_ms"]))[1:]
    decs = meta.get("decoders") or {}
    points: dict[str, list[dict]] = {c: [] for c in cols}
    raw: list[list] = []
    photos: list[dict] = []
    for b in batches:
        base = started or b.get("started_ts")      # 開始時刻は Gateway が補正・確定した記録の値を使う（F6）
        for r in b["rows"]:
            ts = base + r["t"]
            raw.append([ts, *r["v"]])
            for i, c in enumerate(cols):
                v = r["v"][i] if i < len(r["v"]) else None
                d = decs.get(c)
                if v is None or not d:
                    continue
                try:
                    points[c].append({"ts": ts, **parts_mod.decode(d["decoder"], v, d.get("calibration_hex", ""))})
                except parts_mod.DecodeError:
                    pass
        for p in b.get("photos", []):
            photos.append({"ts": base + p["t"], "blob_id": p["blob_id"]})
    raw.sort(key=lambda r: r[0])
    photos.sort(key=lambda p: p["ts"])
    for c in points:
        points[c].sort(key=lambda p: p["ts"])
    return [{"column": c, "part": (decs.get(c) or {}).get("part"), "points": points[c]} for c in cols], raw, photos


def downsample(pts: list[tuple[int, float]], lo: int, hi: int, max_points: int) -> list[list]:
    """[(ts, v)] を [ts, 平均, 最小, 最大] の列にする。点が多ければ等間隔の区間ごとにまとめる。"""
    pts = [p for p in pts if lo <= p[0] <= hi]
    if len(pts) <= max_points:
        return [[t, v, v, v] for t, v in pts]
    width = max(1, (hi - lo) / max_points)
    out, cur, key = [], [], None
    for t, v in pts:
        k = int((t - lo) // width)
        if key is not None and k != key:
            out.append(_bucket(cur))
            cur = []
        key = k
        cur.append((t, v))
    if cur:
        out.append(_bucket(cur))
    return out


def _bucket(cur: list[tuple[int, float]]) -> list:
    vs = [v for _, v in cur]
    return [cur[len(cur) // 2][0], round(sum(vs) / len(vs), 4), min(vs), max(vs)]


def series_for_view(meta: dict, batches: list[dict], lo: int | None, hi: int | None, max_points: int = 600) -> dict:
    cols, raw, photos = decode_rows(meta, batches)
    all_ts = [r[0] for r in raw] + [p["ts"] for p in photos]
    first, last = (min(all_ts), max(all_ts)) if all_ts else (meta.get("started_ts") or 0,) * 2
    lo = first if lo is None else lo
    hi = last if hi is None else hi
    out = []
    for c in cols:
        if not c["points"]:
            continue
        for ch in c["points"][0].keys() - {"ts"}:
            label, unit = CHANNELS.get(ch, (ch, ""))
            out.append({"column": c["column"], "part": c["part"], "channel": ch, "label": label, "unit": unit,
                        "points": downsample([(p["ts"], p[ch]) for p in c["points"]], lo, hi, max_points)})
    latest = {}
    for c in cols:
        if c["points"]:
            p = c["points"][-1]
            latest[c["column"]] = {"ts": p["ts"], **{k: v for k, v in p.items() if k != "ts"}}
    return {"range": [lo, hi], "extent": [first, last], "series": out, "latest": latest,
            "photos": [[p["ts"], p["blob_id"]] for p in photos if lo <= p["ts"] <= hi],
            "not_decodable": [c["column"] for c in cols if not (meta.get("decoders") or {}).get(c["column"])],
            "rows": sum(1 for r in raw if lo <= r[0] <= hi)}


def to_csv(meta: dict, batches: list[dict], photo_url) -> str:
    """時刻（日本時間）・t_ms・列ごとの換算値（と生の16進）・その時刻の写真の URL。"""
    cols, raw, photos = decode_rows(meta, batches)
    photo_at = {p["ts"]: p["blob_id"] for p in photos}
    decoded = {c["column"]: {p["ts"]: p for p in c["points"]} for c in cols}
    chans = {c["column"]: sorted(c["points"][0].keys() - {"ts"}) if c["points"] else [] for c in cols}
    buf = io.StringIO()
    w = csv.writer(buf)
    head = ["time_jst", "t_ms"]
    for c in cols:
        head += [f"{c['column']}.{ch}" for ch in chans[c["column"]]] + [f"{c['column']}.raw_hex"]
    w.writerow(head + ["photo_url"])
    started = meta.get("started_ts") or (raw[0][0] if raw else 0)
    times = sorted({r[0] for r in raw} | set(photo_at))
    rows_at = {r[0]: r for r in raw}
    for ts in times:
        line = [datetime.fromtimestamp(ts / 1000, JST).isoformat(timespec="seconds"), ts - started]
        r = rows_at.get(ts)
        for i, c in enumerate(cols):
            p = decoded[c["column"]].get(ts, {})
            line += [p.get(ch, "") for ch in chans[c["column"]]]
            line.append((r[i + 1] or "") if r else "")
        line.append(photo_url(photo_at[ts]) if ts in photo_at else "")
        w.writerow(line)
    return buf.getvalue()
