"""デバイスの「今の状態」の写し（画面の「現在の状態」用）。

Gateway はデバイスとのやり取り（exec と result、event）をすべて見ているので、状態を問い合わせる操作を
デバイスに送らなくても、出力の値・バインド・計測ジョブ・非常停止・スキャン結果を追える。
hello の state を起点にして、成功した result と event で更新する。あくまで表示用の写しで、判定には使わない。
"""
from __future__ import annotations

import copy


class DeviceView:
    def __init__(self, manifest: dict, hello_state: dict):
        self.safe = {ln["line"]: ln.get("safe_value", 0) for ln in manifest["gpio"]["lines"]
                     if ln["direction"] == "output"}
        st = copy.deepcopy(hello_state)
        self.estop: bool = st.get("estop", False)
        self.outputs: dict[int, dict] = {o["line"]: o for o in st.get("outputs", [])}
        self.bindings: dict[tuple[int, int], dict] = {(b["bus"], b["addr"]): b for b in st.get("bindings", [])}
        self.jobs: list[str] = list(st.get("jobs", []))
        self.inputs: dict[int, int] = {}
        self.i2c_seen: dict[str, dict] = {}          # バス番号（文字列）→ {"found": [...], "ts": ...}

    def state(self) -> dict:
        return {"estop": self.estop, "outputs": [self.outputs[k] for k in sorted(self.outputs)],
                "bindings": list(self.bindings.values()), "jobs": list(self.jobs),
                "inputs": [{"line": k, "value": v} for k, v in sorted(self.inputs.items())]}

    def _revert(self, lines) -> None:
        for line in lines:
            if line in self.safe:
                self.outputs[line] = {"line": line, "value": self.safe[line]}

    def apply_result(self, exec_msg: dict, result: dict) -> bool:
        """成功した result で状態を更新する。変わったら True。"""
        op, args = exec_msg.get("op"), exec_msg.get("args") or {}
        if not result.get("ok"):
            if (result.get("error") or {}).get("code") == "fingerprint_mismatch":
                return self.bindings.pop((args.get("bus"), args.get("addr")), None) is not None
            return False
        data = result.get("data") or {}
        if op == "gpio.write":
            o = {"line": args["line"], "value": data.get("value", args["value"])}
            if data.get("revert_at_ms"):
                o["revert_at_ms"] = data["revert_at_ms"]
            self.outputs[args["line"]] = o
        elif op == "gpio.read":
            if args["line"] in self.safe:
                self.outputs.setdefault(args["line"], {"line": args["line"]})["value"] = data.get("value")
            else:
                self.inputs[args["line"]] = data.get("value")
        elif op == "i2c.bind":
            self.bindings[(args["bus"], args["addr"])] = {k: args[k] for k in
                                                          ("bus", "addr", "part", "fingerprint", "write_allow_regs")}
        elif op == "i2c.unbind":
            self.bindings.pop((args["bus"], args["addr"]), None)
        elif op == "i2c.scan":
            self.i2c_seen[str(data.get("bus", args.get("bus")))] = {"found": data.get("found", []),
                                                                     "ts": result.get("ts")}
        elif op == "sample.start":
            if data.get("job_id") and data["job_id"] not in self.jobs:
                self.jobs.append(data["job_id"])
        elif op == "sample.stop":
            self.jobs = [j for j in self.jobs if j != args.get("job_id")]
        elif op == "sys.estop":
            self.estop = True
            self._revert(self.safe)
            self.jobs = []
        elif op == "sys.estop_clear":
            self.estop = False
        else:
            return False
        return True

    def apply_event(self, msg: dict) -> bool:
        ev, d = msg.get("event"), msg.get("detail") or {}
        if ev == "output_reverted":
            self._revert([d.get("line")])
        elif ev == "deadman_tripped":
            self._revert(d.get("outputs_reverted", []))
            self.jobs = [j for j in self.jobs if j not in d.get("jobs_stopped", [])]
        elif ev == "binding_dropped":
            self.bindings.pop((d.get("bus"), d.get("addr")), None)
        elif ev == "job_end":
            self.jobs = [j for j in self.jobs if j != d.get("job_id")]
        elif ev == "estop_engaged":
            self.estop = True
            self._revert(d.get("outputs_reverted", []))
        elif ev == "estop_cleared":
            self.estop = False
        else:
            return False
        return True
