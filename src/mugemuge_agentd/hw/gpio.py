"""GPIO（PROTOCOL.md 5.1節 gpio.*、8.3節）。

configure() でマニフェストの全ラインを一度に要求し、出力は safe_value で初期化する。
どの実装もブロッキングで、スレッドから呼ばれる前提。
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass


class GPIOError(OSError):
    pass


@dataclass(frozen=True)
class LineConfig:
    line: int
    direction: str          # "output" / "input"
    safe_value: int = 0     # 出力のときだけ意味を持つ
    bias: str = "none"      # "none" / "pull_up" / "pull_down"


def line_configs(manifest: dict) -> list[LineConfig]:
    return [
        LineConfig(ln["line"], ln["direction"], ln.get("safe_value", 0), ln.get("bias", "none"))
        for ln in manifest["gpio"]["lines"]
    ]


class GPIO(ABC):
    @abstractmethod
    def configure(self, lines: list[LineConfig]) -> None:
        """ラインを要求する。出力は safe_value にしてから出力にする。"""

    @abstractmethod
    def read(self, line: int) -> int: ...

    @abstractmethod
    def write(self, line: int, value: int) -> None: ...

    def close(self) -> None:
        pass


class GpiodGPIO(GPIO):
    """実機。libgpiod v2 の Python バインディング（pip の gpiod 2.x）。"""

    def __init__(self, chip: str, consumer: str = "mugemuge-agentd"):
        import gpiod  # 実機でだけ必要

        self._gpiod = gpiod
        self.path = chip if chip.startswith("/") else f"/dev/{chip}"
        self.consumer = consumer
        self._req = None
        self._outputs: set[int] = set()
        self._lock = threading.Lock()

    def configure(self, lines):
        g = self._gpiod
        from gpiod.line import Bias, Direction, Value

        bias = {"none": Bias.DISABLED, "pull_up": Bias.PULL_UP, "pull_down": Bias.PULL_DOWN}
        config = {}
        for lc in lines:
            if lc.direction == "output":
                config[lc.line] = g.LineSettings(
                    direction=Direction.OUTPUT,
                    output_value=Value.ACTIVE if lc.safe_value else Value.INACTIVE,
                )
                self._outputs.add(lc.line)
            else:
                config[lc.line] = g.LineSettings(direction=Direction.INPUT, bias=bias[lc.bias])
        if not config:
            return      # GPIO を使わないマニフェスト。チップには触れない
        with self._lock:
            if self._req is not None:
                self._req.release()
            try:
                self._req = g.request_lines(self.path, consumer=self.consumer, config=config)
            except OSError as e:
                raise GPIOError(e.errno, f"cannot request lines on {self.path}: {e.strerror}") from None

    def read(self, line):
        from gpiod.line import Value

        with self._lock:
            try:
                return 1 if self._req.get_value(line) == Value.ACTIVE else 0
            except OSError as e:
                raise GPIOError(e.errno, f"gpio read {line}: {e.strerror}") from None

    def write(self, line, value):
        from gpiod.line import Value

        if line not in self._outputs:
            raise GPIOError(1, f"line {line} is not an output")
        with self._lock:
            try:
                self._req.set_value(line, Value.ACTIVE if value else Value.INACTIVE)
            except OSError as e:
                raise GPIOError(e.errno, f"gpio write {line}: {e.strerror}") from None

    def close(self):
        with self._lock:
            if self._req is not None:
                self._req.release()
                self._req = None


class FakeGPIO(GPIO):
    """模擬 GPIO。history に (line, value) の書き込み履歴を残す。入力は inputs を書き換えて模擬する。"""

    def __init__(self) -> None:
        self.values: dict[int, int] = {}
        self.outputs: set[int] = set()
        self.inputs: dict[int, int] = {}
        self.history: list[tuple[int, int]] = []
        self.closed = False
        self._lock = threading.Lock()

    def configure(self, lines):
        with self._lock:
            for lc in lines:
                if lc.direction == "output":
                    self.outputs.add(lc.line)
                    self.values[lc.line] = lc.safe_value
                    self.history.append((lc.line, lc.safe_value))
                else:
                    self.inputs.setdefault(lc.line, 1 if lc.bias == "pull_up" else 0)

    def read(self, line):
        with self._lock:
            if line in self.outputs:
                return self.values[line]
            if line not in self.inputs:
                raise GPIOError(22, f"line {line} is not requested")
            return self.inputs[line]

    def write(self, line, value):
        with self._lock:
            if line not in self.outputs:
                raise GPIOError(1, f"line {line} is not an output")
            self.values[line] = value
            self.history.append((line, value))

    def close(self):
        self.closed = True
