"""長期の記録ジョブとカメラ: spool、ack、再送、デッドマンでは止まらないこと、写真の分割送信。"""
import asyncio
import base64
import hashlib
import json

import pytest
from conftest import SESSION, T0_S

from mugemuge_agentd import logger as logger_mod
from mugemuge_agentd.clock import Clock
from mugemuge_agentd.executor import Executor
from mugemuge_agentd.gate import Gate
from mugemuge_agentd.hw.camera import FakeCamera, make_cameras
from mugemuge_agentd.hw.gpio import FakeGPIO
from mugemuge_agentd.hw.virtual import Environment, virtual_i2c
from mugemuge_agentd.logger import Logger
from mugemuge_agentd.manifest import parse_manifest
from mugemuge_agentd.messages import message_errors
from mugemuge_agentd.policy import DeviceState
from mugemuge_agentd.safety import Safety
from mugemuge_agentd.sampler import Sampler

READS = [{"name": "env", "bus": 1, "addr": 0x76, "reg": 0xF7, "len": 6}]


@pytest.fixture
def cam_manifest(raw_manifest):
    raw = dict(raw_manifest)
    raw["camera"] = {"devices": [{"id": "cam0", "kind": "fake", "label": "卓上カメラ", "capture": "approval"}]}
    raw["log"] = {"min_interval_ms": 1000, "min_photo_interval_ms": 10000}
    return parse_manifest(raw)


class Link:
    """client.send_now の代わり。up のときだけ受け取る。"""

    def __init__(self):
        self.up, self.out = True, []

    def __call__(self, m):
        if not self.up:
            return False
        assert message_errors(m) == [], m
        self.out.append(m)
        return True

    def batches(self, log_id):
        return [m for m in self.out if m["type"] == "samples" and m["job_id"] == log_id]


def make_logger(manifest, tmp_path, link, events, monkeypatch):
    monkeypatch.setattr(logger_mod, "BATCH_MS", 200)
    monkeypatch.setattr(logger_mod, "BATCH_ROWS", 3)
    state = DeviceState(device_id="pi-desk-01", session_id=SESSION)
    bus = virtual_i2c(Environment())
    bus.write(1, 0x76, 0xF4, b"\x27")
    lg = Logger(manifest, state, bus, {"cam0": FakeCamera()}, Clock(), tmp_path / "spool", send=link, emit=events.append)
    return lg, state


def test_log_rows_photos_ack_and_resend(cam_manifest, tmp_path, monkeypatch):
    async def go():
        link, events = Link(), []
        lg, state = make_logger(cam_manifest, tmp_path, link, events, monkeypatch)
        info = lg.start({"name": "室温", "interval_ms": 50, "duration_ms": 600, "reads": READS,
                         "photo": {"camera": "cam0", "interval_ms": 250, "width": 160, "height": 120}})
        log_id = info["log_id"]
        assert info["columns"] == ["t_ms", "env"] and log_id in state.jobs
        await asyncio.sleep(0.2)
        link.up = False                         # 途中で通信が切れる → 送らずに spool に貯める
        await asyncio.sleep(0.25)
        assert lg.pending()[log_id] >= 1
        link.up = True
        await lg.wait_all()
        assert lg.resend(force=True) >= 1       # つながったら送り直す

        batches = link.batches(log_id)
        seqs = sorted({b["seq"] for b in batches})
        assert seqs == list(range(len(seqs)))   # 欠番なし（切断中の分も再送で届いた）
        rows = {r[0] for b in batches for r in b["rows"]}
        assert len(rows) == 12 and min(rows) == 0 and max(rows) == 550
        photos = [p for b in batches for p in b.get("photos", [])]
        assert len({p["blob_id"] for p in photos}) == 3          # 0, 250, 500ms
        # 写真は分割された blob として、そのバッチより前に届き、元に戻せる
        blob = []
        for m in link.out:                       # 再送で同じ写真が2回届くことがある。最初の1組を使う
            if m["type"] == "blob" and m["blob_id"] == photos[0]["blob_id"]:
                blob.append(m)
                if m["last"]:
                    break
        data = b"".join(base64.b64decode(m["data"]) for m in blob)
        assert blob[-1]["last"] and hashlib.sha256(data).hexdigest() == blob[-1]["sha256"] and data[:4] == b"\x89PNG"
        end = [e for e in events if e["event"] == "job_end"][0]["detail"]
        assert end == {"job_id": log_id, "reason": "completed", "rows": 12, "photos": 3}

        # ack は1つのバッチについての通知。そのバッチだけ消す。全部届いて終わった記録は spool から消える
        lg.ack(log_id, max(seqs))
        assert lg.pending()[log_id] == len(seqs) - 1
        for s_ in seqs:
            lg.ack(log_id, s_)
        assert log_id not in lg.pending()
    asyncio.run(go())


def test_spool_survives_restart(cam_manifest, tmp_path, monkeypatch):
    async def go():
        link, events = Link(), []
        link.up = False
        lg, _ = make_logger(cam_manifest, tmp_path, link, events, monkeypatch)
        log_id = lg.start({"name": "x", "interval_ms": 50, "duration_ms": 300, "reads": READS})["log_id"]
        await lg.wait_all()
        assert link.out == [] and lg.pending()[log_id] >= 1
        # デーモンが再起動した → 新しい Logger が同じ spool から送る
        link2 = Link()
        lg2, _ = make_logger(cam_manifest, tmp_path, link2, [], monkeypatch)
        assert lg2.resend(force=True) >= 1
        assert {r[0] for b in link2.batches(log_id) for r in b["rows"]} == {0, 50, 100, 150, 200, 250}
    asyncio.run(go())


def make_executor(manifest, tmp_path, monkeypatch, link, events):
    clock = Clock()
    clock.offset_ms = T0_S * 1000 - clock.now_ms()
    state = DeviceState(device_id="pi-desk-01", session_id=SESSION)
    gate = Gate(manifest, state, now_ms=clock.mono_ms())
    gpio = FakeGPIO()
    safety = Safety(manifest, state, gpio, clock, events.append)
    safety.init_outputs()
    bus = virtual_i2c(Environment())
    sampler = Sampler(state, bus, clock, events.append)
    monkeypatch.setattr(logger_mod, "BATCH_MS", 200)
    cams = make_cameras(manifest)
    lg = Logger(manifest, state, bus, cams, clock, tmp_path / "spool", send=link, emit=events.append)
    safety.stop_jobs = lambda r: sampler.stop_all(r) + ([] if r == "deadman" else lg.stop_all(r))
    ex = Executor(manifest, state, gate, bus, gpio, safety, sampler, None, clock, events.append, "0.1.0",
                  cameras=cams, logger=lg)
    return ex, lg, safety, state


def msg(op, args, approval=None, xid=[0]):
    xid[0] += 1
    m = {"v": 1, "type": "exec", "id": f"x-log-{xid[0]:06d}", "ts": 1, "op": op, "args": args, "run_id": "r", "step_id": "s"}
    if approval:
        m["approval"] = approval
    return m


def test_camera_capture_needs_approval_and_sends_blob(cam_manifest, tmp_path, monkeypatch, sign):
    async def go():
        link, events = Link(), []
        ex, *_ = make_executor(cam_manifest, tmp_path, monkeypatch, link, events)
        args = {"camera": "cam0", "width": 320, "height": 240}
        r = await ex.handle(msg("camera.capture", args))
        assert r["error"]["code"] == "approval_required" and "写真を1枚撮る" in r["error"]["request"]["summary"]
        r = await ex.handle(msg("camera.capture", args, sign("camera.capture", args)))
        assert r["ok"] and r["data"]["content_type"] == "image/png"
        blobs = [m for m in events if m.get("type") == "blob"]
        data = b"".join(base64.b64decode(m["data"]) for m in blobs)
        assert len(data) == r["data"]["bytes"] and hashlib.sha256(data).hexdigest() == r["data"]["sha256"]
        assert all(message_errors(m) == [] for m in blobs)
        r = await ex.handle(msg("camera.capture", {"camera": "cam9"}))
        assert r["error"]["code"] == "policy_denied" and r["error"]["detail"]["reason"] == "camera.not_listed"
        r = await ex.handle(msg("camera.capture", {"camera": "cam0", "width": 4000, "height": 3000}))
        assert r["error"]["detail"]["reason"] == "camera.size_exceeds_max"
    asyncio.run(go())


def test_log_start_policy_deadman_and_estop(cam_manifest, tmp_path, monkeypatch, sign):
    async def go():
        link, events = Link(), []
        ex, lg, safety, state = make_executor(cam_manifest, tmp_path, monkeypatch, link, events)
        reads_only = {"name": "室温", "interval_ms": 1000, "duration_ms": 3_600_000, "reads": READS}
        r = await ex.handle(msg("log.start", reads_only))
        assert r["ok"] and r["policy"]["decision"] == "allow"       # 未知アドレスの読み出しだけなら自動
        log_id = r["data"]["log_id"]
        assert "log_" in log_id and log_id in state.jobs

        with_photo = {**reads_only, "photo": {"camera": "cam0", "interval_ms": 60_000}}
        r = await ex.handle(msg("log.start", with_photo))
        assert r["error"]["code"] == "approval_required"            # 写真付きは開始時に1回だけ承認
        assert "写真を 1分ごとに cam0 で" in r["error"]["request"]["summary"]
        r = await ex.handle(msg("log.start", {**reads_only, "photo": {"camera": "cam0", "interval_ms": 30_000}}))
        assert "写真を 30秒ごとに cam0 で" in r["error"]["request"]["summary"]     # 1分に丸めない
        for bad, reason in [({**reads_only, "interval_ms": 500}, "log.interval_too_short"),
                            ({**reads_only, "duration_ms": 30 * 86_400_000}, "log.duration_too_long"),
                            ({**with_photo, "photo": {"camera": "cam0", "interval_ms": 5000}}, "log.photo_interval_too_short")]:
            r = await ex.handle(msg("log.start", bad))
            assert r["error"]["code"] in ("policy_denied", "bad_request"), r
            if r["error"]["code"] == "policy_denied":
                assert r["error"]["detail"]["reason"] == reason

        await safety.trip_deadman(10_000)                            # デッドマンでは記録は止まらない
        assert log_id in state.jobs
        await safety.engage_estop("t")                               # 非常停止では止まる
        await lg.wait_all()
        assert log_id not in state.jobs
        end = [e for e in events if e.get("event") == "job_end" and e["detail"]["job_id"] == log_id][0]
        assert end["detail"]["reason"] == "estop"
    asyncio.run(go())


def test_ack_of_a_later_batch_keeps_a_lost_earlier_one(cam_manifest, tmp_path, monkeypatch):
    """バッチ0が途中で失われ、バッチ1だけ保存されて ack 1 が来ても、0 は消さない（SPEC_REVIEW.md B1）。"""
    async def go():
        link, events = Link(), []
        lg, _ = make_logger(cam_manifest, tmp_path, link, events, monkeypatch)
        log_id = lg.start({"name": "x", "interval_ms": 50, "duration_ms": 300, "reads": READS})["log_id"]
        await lg.wait_all()
        seqs = sorted(b["seq"] for b in link.batches(log_id))
        assert seqs[:2] == [0, 1]
        lg.ack(log_id, 1)
        left = sorted(int(f.stem.split("-")[1]) for f in (tmp_path / "spool" / log_id).glob("batch-*.json"))
        assert 0 in left and 1 not in left
        assert lg.resend(force=True) == len(left)      # 失われた 0 は再送される
    asyncio.run(go())


def test_sample_jobs_are_not_blocked_by_logs(manifest):
    """記録ジョブが2本動いていても計測ジョブは始められる（SPEC_REVIEW.md B4）。"""
    from mugemuge_agentd.policy import Job, Level, Policy
    st = DeviceState(device_id="d", session_id="s")
    st.jobs["log_a"] = Job("log_a", 1)
    st.jobs["log_b"] = Job("log_b", 1)
    args = {"period_ms": 100, "duration_ms": 1000, "reads": [{"name": "v", "bus": 1, "addr": 64, "reg": 2, "len": 2}]}
    assert Policy(manifest).evaluate("sample.start", args, st).level is Level.ALLOW
    st.jobs["job_1"] = Job("job_1", 10)
    st.jobs["job_2"] = Job("job_2", 10)
    assert Policy(manifest).evaluate("sample.start", args, st).reason == "sample.too_many_jobs"


def test_client_does_not_hold_log_batches(manifest):
    """切断中、記録ジョブのバッチは client が保持しない（spool から再送するので二重になる: B9）。"""
    from mugemuge_agentd.client import Client
    c = Client.__new__(Client)
    from collections import deque
    c._held_events, c._held_samples = deque(maxlen=10), deque(maxlen=10)
    c._hold({"type": "samples", "job_id": "log_abc", "seq": 0, "rows": []})
    c._hold({"type": "samples", "job_id": "job_1", "seq": 0, "rows": []})
    assert [m["job_id"] for m in c._held_samples] == ["job_1"]


def test_job_ids_are_unique_across_restarts(manifest, tmp_path, monkeypatch):
    """デーモンを2回起動し、それぞれ計測と記録を始める。Gateway には別々の計測・記録として残る（SPEC_FIXES_2.md F1）。"""
    from cloudrig import DeviceProc, Server, client, until

    from services.common.auth import CallerAuth
    from services.common.blobs import MemoryBlobStore
    from services.common.store import MemoryStore
    from services.gateway.app import create_app
    from services.web.app import GatewayClient
    monkeypatch.setattr(logger_mod, "BATCH_MS", 100)
    reads = [{"name": "v", "bus": 1, "addr": 0x40, "reg": 2, "len": 2}]

    async def go():
        store = MemoryStore()
        app = create_app(store, CallerAuth(None, insecure=True), heartbeat_ms=1000, blobs=MemoryBlobStore())
        srv = await Server(app).start()
        gw = GatewayClient(srv.url, use_id_token=False)
        await store.create_pairing_code("123456", "user:t", 2_000_000_000_000)
        async with client(srv.url) as c:
            token = (await c.post("/v1/pair", json={"code": "123456", "device_id": "pi-desk-01"})).json()["token"]
        ids = []
        for _ in range(2):                                   # 同じ spool・同じ Pi（boot_id も同じ）で2回起動
            dev = await DeviceProc(manifest, srv.port, token, tmp_path / "audit.jsonl").start()
            await until(lambda: "pi-desk-01" in app.state.hub.conns and app.state.hub.conns["pi-desk-01"].session_id)

            async def ex(op, args):
                st, r = await gw.exec({"device_id": "pi-desk-01", "op": op, "args": args, "run_id": "r", "step_id": "s"})
                assert r["ok"], r
                return r["data"]
            job = (await ex("sample.start", {"period_ms": 50, "duration_ms": 200, "batch_ms": 100, "reads": reads}))["job_id"]
            lg = (await ex("log.start", {"name": "x", "interval_ms": 1000, "duration_ms": 1500, "reads": reads}))["log_id"]
            await until(lambda: store.logs.get(("pi-desk-01", lg), {}).get("status") == "completed", timeout=10)
            await until(lambda: lg not in dev.d.logger.pending(), timeout=5)
            ids.append((job, lg))
            await dev.stop()
            await until(lambda: "pi-desk-01" not in app.state.hub.conns)
        (job1, log1), (job2, log2) = ids
        assert job1 != job2 and log1 != log2
        s1, s2 = await store.list_samples("pi-desk-01", job1), await store.list_samples("pi-desk-01", job2)
        assert s1 and s2 and {d["job_id"] for d in s1} == {job1} and {d["job_id"] for d in s2} == {job2}
        assert [d["seq"] for d in s1] == list(range(len(s1)))
        b1, b2 = await store.list_log_batches("pi-desk-01", log1), await store.list_log_batches("pi-desk-01", log2)
        assert len(b1) == len(b2) and sum(len(b["rows"]) for b in b1) == 2
        assert store.logs[("pi-desk-01", log1)]["status"] == store.logs[("pi-desk-01", log2)]["status"] == "completed"
        await srv.stop()
    asyncio.run(go())


def test_spool_full_pauses_photos_then_drops_oldest_rows_with_event(cam_manifest, tmp_path, monkeypatch):
    """spool の上限: 写真が 80% を超えたら写真を止め（event）、全体が 100% を超えたら最も古い未確認のバッチから捨てる（event）（F5）。"""
    from mugemuge_agentd.logger import fit_long_side
    assert fit_long_side(1280, 960, 640) == (640, 480) and fit_long_side(320, 240, 640) == (320, 240)

    async def go():
        link, events = Link(), []
        link.up = False                                     # 通信断: ack が来ないので spool に溜まる
        lg, _ = make_logger(cam_manifest, tmp_path, link, events, monkeypatch)
        lg.spool_limit = 6000                               # 小さくして再現する
        shots = []
        cam = lg.cameras["cam0"]
        orig = cam.capture

        def capture(w, h):
            shots.append((w, h))
            return orig(w, h)
        cam.capture = capture
        log_id = lg.start({"name": "x", "interval_ms": 50, "duration_ms": 2000, "reads": READS,
                           "photo": {"camera": "cam0", "interval_ms": 100, "width": 1280, "height": 960}})["log_id"]
        await lg.wait_all()
        assert shots and all(max(w, h) <= cam_manifest["log"]["photo_max_px"] for w, h in shots)
        names = [e["event"] for e in events]
        assert names.count("spool_photos_paused") >= 1
        drops = [e["detail"] for e in events if e["event"] == "spool_rows_dropped"]
        assert drops and all(d["job_id"] == log_id and d["seq_from"] <= d["seq_to"] for d in drops)
        assert drops[0]["seq_from"] == 0                    # 最も古いバッチから捨てる
        assert lg._spool_bytes() <= lg.spool_limit
        left = sorted(int(f.stem.split("-")[1]) for f in (tmp_path / "spool" / log_id).glob("batch-*.json"))
        dropped = {s for d in drops for s in range(d["seq_from"], d["seq_to"] + 1)}
        assert left and not (set(left) & dropped) and max(left) > max(dropped)
        for e in events:
            assert message_errors(e) == [], e
    asyncio.run(go())


def test_started_ts_is_recomputed_after_wall_clock_jump(manifest, tmp_path, monkeypatch):
    """記録の途中で壁時計が跳んでも（NTP 同期）、送るバッチの started_ts は単調時計の経過時間から計算し直す（F6）。"""
    class JumpClock(Clock):
        jump = 0

        def now_ms(self):
            return super().now_ms() + self.jump

    async def go():
        link, events = Link(), []
        monkeypatch.setattr(logger_mod, "BATCH_MS", 100)
        st = DeviceState(device_id="pi-desk-01", session_id=SESSION)
        clock = JumpClock()
        clock.jump = -86_400_000                            # 起動直後: 1日遅れ
        lg = Logger(manifest, st, virtual_i2c(Environment()), {}, clock, tmp_path / "spool", send=link,
                    emit=events.append)
        info = lg.start({"name": "x", "interval_ms": 1000, "duration_ms": 3000, "reads": READS})
        assert info["started_ts"] < Clock().now_ms() - 86_000_000
        await asyncio.sleep(1.2)
        clock.jump = 0                                      # NTP が同期した
        await lg.wait_all()
        last = link.batches(info["log_id"])[-1]
        assert abs(last["started_ts"] - (Clock().now_ms() - 3000)) < 1500
    asyncio.run(go())


def test_camera_rate_limits(raw_manifest):
    """camera.capture は min_interval_ms と max_per_hour で rate_limited（retry_after_ms 付き）。
    記録ジョブの写真の間隔は開始時に min_interval_ms 以上かを検査する（I2）。"""
    import copy

    from mugemuge_agentd.policy import Level, Policy
    raw = copy.deepcopy(raw_manifest)
    raw["camera"] = {"devices": [{"id": "cam0", "kind": "fake", "capture": "allow", "min_interval_ms": 2000,
                                  "max_per_hour": 3}]}
    raw["log"] = {"min_interval_ms": 1000, "min_photo_interval_ms": 10000}
    m = parse_manifest(raw)
    st = DeviceState(device_id="pi-desk-01", session_id=SESSION)
    gate = Gate(m, st, now_ms=T0_S * 1000)
    t = T0_S * 1000
    shot = {"camera": "cam0", "width": 160, "height": 120}

    def check(now):
        return gate.check("camera.capture", shot, None, now_ms=now, device_now_s=now // 1000)
    assert check(t).may_execute
    v = check(t + 500)
    assert v.error_code == "rate_limited" and v.retry_after_ms == 1500
    assert check(t + 2000).may_execute and check(t + 4000).may_execute
    v = check(t + 6000)                                   # 1時間に3枚まで
    assert v.error_code == "rate_limited" and v.retry_after_ms == 3_600_000 - 6000
    assert check(t + 3_600_000).may_execute               # 最初の1枚が1時間より前になった
    # 記録の写真の間隔が min_interval_ms より短ければ拒否（min_photo_interval_ms は満たしていても）
    raw["camera"]["devices"][0]["min_interval_ms"] = 20000
    pol = Policy(parse_manifest(raw))
    d = pol.evaluate("log.start", {"name": "x", "interval_ms": 1000, "duration_ms": 60000, "reads": READS,
                                   "photo": {"camera": "cam0", "interval_ms": 15000}}, st)
    assert d.level is Level.DENY and d.reason == "camera.interval_below_min"


def test_resend_window_is_bounded(manifest, tmp_path, monkeypatch):
    """長い通信断のあと、未確認のバッチは同時に4つまで送り、ack が返るたびに次を送る。
    30秒の再送タイマは、送ったが ack のないものにだけかかる（I3）。"""
    async def go():
        link, events = Link(), []
        link.up = False
        lg, _ = make_logger(manifest, tmp_path, link, events, monkeypatch)
        log_id = lg.start({"name": "x", "interval_ms": 50, "duration_ms": 1500, "reads": READS})["log_id"]
        await lg.wait_all()
        total = lg.pending()[log_id]
        assert total >= 8
        link.up = True
        assert lg.resend(force=True) == logger_mod.SEND_WINDOW
        sent = [b["seq"] for b in link.batches(log_id)]
        assert sent == list(range(logger_mod.SEND_WINDOW))      # 古い順
        assert lg.resend() == 0                                  # 枠が埋まっている間は送らない
        lg.ack(log_id, sent[0])                                  # ack で1つ空き、次を1つ送る
        assert [b["seq"] for b in link.batches(log_id)][-1] == logger_mod.SEND_WINDOW
        # 送ったが ack のないものは、30秒たったら送り直す（まだ送っていないものより先に古い順）
        monkeypatch.setattr(logger_mod, "RESEND_AFTER_S", 0)
        before = len(link.batches(log_id))
        assert lg.resend() == logger_mod.SEND_WINDOW
        assert [b["seq"] for b in link.batches(log_id)][before:] == [1, 2, 3, 4]
        monkeypatch.setattr(logger_mod, "RESEND_AFTER_S", 30)
        # ack を返し続ければ全部届く。どの時点でも ack 待ちは4つまで
        acked = {sent[0]}
        while lg.pending().get(log_id):
            assert lg._in_flight() <= logger_mod.SEND_WINDOW
            nxt = min(int(f.stem.split("-")[1]) for f in (tmp_path / "spool" / log_id).glob("batch-*.json"))
            lg.ack(log_id, nxt)
            acked.add(nxt)
        assert acked == set(range(total))
    asyncio.run(go())


def test_wall_clock_step_forward_does_not_reset_camera_limits(raw_manifest, tmp_path, monkeypatch):
    """壁時計が1時間進んでも、カメラの min_interval_ms は単調時計で測る（壁時計だと上限がすぐ解けた: P3）。"""
    import copy
    raw = copy.deepcopy(raw_manifest)
    raw["camera"] = {"devices": [{"id": "cam0", "kind": "fake", "capture": "allow", "min_interval_ms": 600_000,
                                  "max_per_hour": 120}]}
    m = parse_manifest(raw)

    async def go():
        ex, *_ = make_executor(m, tmp_path, monkeypatch, Link(), [])
        args = {"camera": "cam0", "width": 160, "height": 120}
        assert (await ex.handle(msg("camera.capture", args)))["ok"]
        real_time = __import__("time").time
        monkeypatch.setattr("mugemuge_agentd.clock.time.time", lambda: real_time() + 3600)
        r = await ex.handle(msg("camera.capture", args))
        assert r["error"]["code"] == "rate_limited" and r["error"]["retry_after_ms"] > 590_000
    asyncio.run(go())
