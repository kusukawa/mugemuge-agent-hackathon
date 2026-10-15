"""記録ジョブとカメラを Gateway・保管先まで通す: バッチの保存と ack、写真の組み立てと保管、記録の情報。"""
import asyncio

import pytest
from cloudrig import DeviceProc, Server, client, until

from mugemuge_agentd import logger as logger_mod
from mugemuge_agentd.approval import sign_grant
from mugemuge_agentd.hw.camera import FakeCamera
from mugemuge_agentd.hw.virtual import Environment, virtual_i2c
from mugemuge_agentd.manifest import parse_manifest
from services.common.auth import CallerAuth
from services.common.blobs import MemoryBlobStore
from services.common.store import MemoryStore
from services.gateway.app import create_app
from services.web.app import GatewayClient


@pytest.fixture
def cam_manifest(raw_manifest):
    raw = dict(raw_manifest)
    raw["camera"] = {"devices": [{"id": "cam0", "kind": "fake", "capture": "approval"}]}
    raw["log"] = {"min_interval_ms": 1000, "min_photo_interval_ms": 10000}
    return parse_manifest(raw)


def test_log_and_photos_reach_storage(cam_manifest, signing_key, tmp_path, monkeypatch):
    monkeypatch.setattr(logger_mod, "BATCH_MS", 1500)
    monkeypatch.setattr(logger_mod, "BATCH_ROWS", 2)

    async def go():
        store, blobs = MemoryStore(), MemoryBlobStore()
        app = create_app(store, CallerAuth(None, insecure=True), heartbeat_ms=1000, blobs=blobs)
        srv = await Server(app).start()
        gw = GatewayClient(srv.url, use_id_token=False)
        await store.create_pairing_code("123456", "user:t", 2_000_000_000_000)
        async with client(srv.url) as c:
            token = (await c.post("/v1/pair", json={"code": "123456", "device_id": "pi-desk-01"})).json()["token"]
        dev = DeviceProc(cam_manifest, srv.port, token, tmp_path / "audit.jsonl", cameras={"cam0": FakeCamera()})
        dev.d.executor.i2c = dev.d.logger.i2c = virtual_i2c(Environment())
        await dev.start()
        await until(lambda: "pi-desk-01" in app.state.hub.conns)

        async def exec(op, args):
            body = {"device_id": "pi-desk-01", "op": op, "args": args, "run_id": "r", "step_id": "s"}
            st, r = await gw.exec(body)
            if not r["ok"] and r["error"]["code"] == "approval_required":
                req = r["error"]["request"]
                body["approval"] = sign_grant(signing_key, kid="test-key", device_id=req["device_id"],
                                              session_id=req["session_id"], op=op, args=args, approved_by="user:t",
                                              iat=dev.d.clock.corrected_s())
                st, r = await gw.exec(body)
            assert r["ok"], r
            return r["data"]

        # 単発の撮影: 写真は result より前に届いて保管される
        shot = await exec("camera.capture", {"camera": "cam0", "width": 320, "height": 240})
        await until(lambda: ("pi-desk-01", shot["blob_id"]) in store.blob_meta)
        meta = store.blob_meta[("pi-desk-01", shot["blob_id"])]
        assert meta["sha256"] == shot["sha256"] and (await blobs.get(meta["path"]))[:4] == b"\x89PNG"

        # 写真付きの記録（開始時に1回承認）
        args = {"name": "室温", "interval_ms": 1000, "duration_ms": 4000,
                "reads": [{"name": "env", "bus": 1, "addr": 0x76, "reg": 0xF7, "len": 6}],
                "photo": {"camera": "cam0", "interval_ms": 10000, "width": 160, "height": 120}}
        started = await exec("log.start", args)
        log_id = started["log_id"]
        m = await store.get_log("pi-desk-01", log_id)
        assert m["status"] == "running" and m["name"] == "室温" and m["columns"] == ["t_ms", "env"]
        await until(lambda: store.logs[("pi-desk-01", log_id)].get("status") == "completed", timeout=10)
        batches = await store.list_log_batches("pi-desk-01", log_id)
        assert [b["seq"] for b in batches] == list(range(len(batches)))
        rows = [r for b in batches for r in b["rows"]]
        assert [r["t"] for r in rows] == [0, 1000, 2000, 3000]
        m = await store.get_log("pi-desk-01", log_id)
        assert m["rows"] == 4 and m["photos"] == 1 and m["last_row"]["t"] == 3000
        ph = m["last_photo"]["blob_id"]
        assert await blobs.get(store.blob_meta[("pi-desk-01", ph)]["path"])
        # ack が届いて Pi の spool は空になる
        await until(lambda: log_id not in dev.d.logger.pending(), timeout=5)

        # 換算方法の登録
        async with client(srv.url) as c:
            r = await c.post(f"/internal/devices/pi-desk-01/logs/{log_id}/decoders",
                             json={"decoders": {"env": {"decoder": "bmp280", "calibration_hex": "00", "part": "BMP280"}}})
            assert r.status_code == 200
            assert (await c.get(f"/internal/devices/pi-desk-01/logs")).json()["logs"][0]["decoders"]["env"]["part"] == "BMP280"
        await dev.stop()
        await srv.stop()
    asyncio.run(go())


def test_no_ack_without_photo_and_interrupted_logs(cam_manifest, tmp_path):
    """写真が保管されていないバッチには ack を返さない（B5）。再接続時に動いていない「記録中」は中断にする（B6）。"""
    import json

    import websockets

    async def go():
        store = MemoryStore()
        app = create_app(store, CallerAuth(None, insecure=True), heartbeat_ms=1000, blobs=MemoryBlobStore())
        srv = await Server(app).start()
        await store.create_pairing_code("123456", "user:t", 2_000_000_000_000)
        async with client(srv.url) as c:
            token = (await c.post("/v1/pair", json={"code": "123456", "device_id": "pi-desk-01"})).json()["token"]
        await store.put_log("pi-desk-01", "log_old0000001", {"status": "running", "last_batch_ts": 5})
        from mugemuge_agentd.manifest import manifest_sha256
        hello = {"v": 1, "type": "hello", "id": "h-test-000001", "ts": 1_791_300_000_000, "device_id": "pi-desk-01",
                 "agent": {"name": "t", "version": "0"}, "protocols": [1], "boot_id": "b", "manifest": cam_manifest,
                 "manifest_sha256": manifest_sha256(cam_manifest),
                 "state": {"estop": False, "bindings": [], "jobs": ["log_live000001"], "outputs": [{"line": 17, "value": 0}]}}
        await store.put_log("pi-desk-01", "log_live000001", {"status": "running"})
        async with websockets.connect(f"ws://127.0.0.1:{srv.port}/v1/device", subprotocols=["mugemuge.v1"],
                                      additional_headers={"Authorization": f"Bearer {token}"}) as ws:
            await ws.send(json.dumps(hello))
            assert json.loads(await ws.recv())["type"] == "welcome"
            await until(lambda: store.logs[("pi-desk-01", "log_old0000001")]["status"] == "interrupted")
            assert store.logs[("pi-desk-01", "log_live000001")]["status"] == "running"
            batch = {"v": 1, "type": "samples", "id": "s-test-000001", "ts": 1_791_300_000_001, "job_id": "log_live000001", "seq": 0,
                     "rows": [[0, "00"]], "photos": [{"t": 0, "blob_id": "blob_MISSING0001"}]}
            await ws.send(json.dumps(batch))
            await asyncio.sleep(0.3)
            got = []
            try:
                while True:
                    got.append(json.loads(await asyncio.wait_for(ws.recv(), 0.3)))
            except asyncio.TimeoutError:
                pass
            assert not [m for m in got if m["type"] == "ack"]                 # 写真がないので ack しない
            assert (await store.list_log_batches("pi-desk-01", "log_live000001"))[0]["seq"] == 0
            # spool が満杯で捨てたバッチの範囲は、記録の「欠落」として残る（F5）
            for i, (ev, det) in enumerate((("spool_rows_dropped", {"job_id": "log_live000001", "seq_from": 3, "seq_to": 5}),
                                           ("spool_photos_paused", {"job_id": "log_live000001", "photo_bytes": 9,
                                                                    "limit_bytes": 10}))):
                await ws.send(json.dumps({"v": 1, "type": "event", "id": f"e-test-00000{i}", "ts": 1_791_300_000_002 + i,
                                          "event": ev, "detail": det}))
            await until(lambda: store.logs[("pi-desk-01", "log_live000001")].get("photos_paused_ts"))
            assert store.logs[("pi-desk-01", "log_live000001")]["dropped"] == [[3, 5]]
        await srv.stop()
    asyncio.run(go())


def test_started_ts_is_corrected_when_device_clock_is_wrong(cam_manifest, tmp_path):
    """デバイスの時計が1日遅れていても（NTP 同期の前）、記録の開始時刻は受信時刻との差で補正される。
    時計が合ったバッチが来たら1回だけ確定し、そのあと古い値が届いても変えない（F6）。"""
    import json
    import time

    import websockets

    async def go():
        store = MemoryStore()
        app = create_app(store, CallerAuth(None, insecure=True), heartbeat_ms=1000, blobs=MemoryBlobStore())
        srv = await Server(app).start()
        await store.create_pairing_code("123456", "user:t", 2_000_000_000_000)
        async with client(srv.url) as c:
            token = (await c.post("/v1/pair", json={"code": "123456", "device_id": "pi-desk-01"})).json()["token"]
        from mugemuge_agentd.manifest import manifest_sha256
        now = int(time.time() * 1000)
        late = 86_400_000                                    # デバイスの時計は1日遅れている
        hello = {"v": 1, "type": "hello", "id": "h-test-000001", "ts": now - late, "device_id": "pi-desk-01",
                 "agent": {"name": "t", "version": "0"}, "protocols": [1], "boot_id": "b", "manifest": cam_manifest,
                 "manifest_sha256": manifest_sha256(cam_manifest),
                 "state": {"estop": False, "bindings": [], "jobs": ["log_k3v9q2xa_1"], "outputs": [{"line": 17, "value": 0}]}}
        await store.put_log("pi-desk-01", "log_k3v9q2xa_1", {"status": "running"})

        def batch(seq, ts, started):
            return json.dumps({"v": 1, "type": "samples", "id": f"s-test-00000{seq}", "ts": ts, "job_id": "log_k3v9q2xa_1",
                               "seq": seq, "rows": [[seq * 1000, "00"]], "started_ts": started})
        meta = lambda: store.logs[("pi-desk-01", "log_k3v9q2xa_1")]
        async with websockets.connect(f"ws://127.0.0.1:{srv.port}/v1/device", subprotocols=["mugemuge.v1"],
                                      additional_headers={"Authorization": f"Bearer {token}"}) as ws:
            await ws.send(json.dumps(hello))
            assert json.loads(await ws.recv())["type"] == "welcome"
            # 1. 同期の前: 開始10秒後のバッチ。ts も started_ts も1日遅れ → 受信時刻との差で補正（仮）
            await ws.send(batch(0, now - late, now - late - 10_000))
            await until(lambda: meta().get("started_ts"))
            assert abs(meta()["started_ts"] - (now - 10_000)) < 5_000
            assert meta()["clock_corrected"] is True and meta()["started_ts_final"] is False
            # 2. 同期のあと: デバイスは単調時計で計算し直した正しい開始時刻を送る → 確定
            await ws.send(batch(1, now, now - 10_000))
            await until(lambda: meta().get("started_ts_final"))
            assert meta()["started_ts"] == now - 10_000
            # 3. 確定のあとに古い値（再起動のあとの再送など）が届いても変えない
            await ws.send(batch(2, now, now - late))
            await until(lambda: meta().get("rows") == 3)
            assert meta()["started_ts"] == now - 10_000
        await srv.stop()
    asyncio.run(go())
