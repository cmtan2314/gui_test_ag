#!/usr/bin/env python3
"""
GUI to control AMR I150 via REST API (PySide6).
Robot: https://192.168.100.100:8081  — self-signed SSL (verify disabled).

Install:
    pip install PySide6 requests

Run:
    python robot_gui.py
"""
import json
import os
import sys
import time
from datetime import datetime

import requests
import urllib3

# Support multiple Qt backends: PySide6 (pip) or PyQt5 (apt install python3-pyqt5)
try:
    from PySide6.QtCore import QThread, Signal, QTimer
    from PySide6.QtWidgets import (
        QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
        QLabel, QLineEdit, QPushButton, QSpinBox, QComboBox,
        QTextEdit, QGroupBox,
    )
    QT_BACKEND = "PySide6"
except ImportError:
    try:
        from PySide2.QtCore import QThread, Signal, QTimer
        from PySide2.QtWidgets import (
            QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
            QLabel, QLineEdit, QPushButton, QSpinBox, QComboBox,
            QTextEdit, QGroupBox,
        )
        QT_BACKEND = "PySide2"
    except ImportError:
        from PyQt5.QtCore import QThread, pyqtSignal as Signal, QTimer
        from PyQt5.QtWidgets import (
            QApplication, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
            QLabel, QLineEdit, QPushButton, QSpinBox, QComboBox,
            QTextEdit, QGroupBox,
        )
        QT_BACKEND = "PyQt5"

# Robot uses a self-signed cert -> silence the InsecureRequest warning
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Lose connection longer than this threshold (seconds) -> sound the alarm
OFFLINE_ALARM_SEC = 30

# Robot standing still while its path is NOT empty for longer than this (seconds)
# -> treat as "stalled mid-route" (stopped before reaching the target node).
STALL_SEC = 5.0

# Log every GET /State to a JSONL file (one record per line) — to capture the
# real edgeStates/agvPosition structure of the robot. Placed next to the script.
STATE_LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "state_poll.jsonl")


# ─────────────────────────── Robot REST client ───────────────────────────
class RobotClient:
    def __init__(self, base_url="https://192.168.100.100:8081", robot_id="I150"):
        self.base_url = base_url.rstrip("/")
        self.robot_id = robot_id
        self.session = requests.Session()
        self.session.verify = False  # skip self-signed cert

    def _request(self, method, endpoint, json_body=None):
        url = self.base_url + endpoint
        try:
            resp = self.session.request(
                method, url, json=json_body,
                headers={"Content-Type": "application/json"},
                timeout=(5, 10),  # (connect, read)
            )
        except requests.exceptions.ConnectTimeout:
            return 0, "Không kết nối được robot (connect timeout) — kiểm tra mạng/IP."
        except requests.exceptions.ConnectionError:
            return 0, "Connection refused/unreachable — robot chưa bật hoặc sai IP/port."
        except requests.exceptions.RequestException as e:
            return 0, f"Lỗi request: {e}"
        try:
            data = resp.json()
        except ValueError:
            data = resp.text
        return resp.status_code, data

    def move_to_node(self, node_name):
        return self._request("POST", "/api/RobotManager/MoveToNode", {
            "robotId": self.robot_id,
            "nodeName": node_name,
            "finalAction": [],
        })

    def cancel_move(self):
        return self._request("DELETE", f"/api/RobotManager/MoveToNode/{self.robot_id}")

    def get_state(self):
        return self._request("GET", f"/api/RobotManager/State/{self.robot_id}")

    # ── helper: current order_id ──
    def current_order_id(self):
        code, data = self.get_state()
        if code != 200 or not isinstance(data, dict):
            return ""
        for info in data.get("data", {}).get("information", []):
            if info.get("infoType") == "order":
                for ref in info.get("infoReferences", []):
                    if ref.get("referenceKey") == "order_id":
                        return ref.get("referenceValue", "")
        return ""


# ───────────────────── Shared helpers for the workers ────────────────────
def _compute_moving(d):
    """True if the robot is MOVING, based on the /State data dict."""
    vel = d.get("velocity", {})
    return (bool(d.get("nodeStates")) or bool(d.get("edgeStates")) or
            abs(vel.get("vx", 0)) > 1e-6 or abs(vel.get("vy", 0)) > 1e-6 or
            abs(vel.get("omega", 0)) > 1e-6)


def _edge_direction(d):
    """From a /State poll: if the robot is on an edge -> return 'A → B', else ''.

    Probe the edge's start/end fields (real field names not yet confirmed), or
    split from edgeId like 'Node4-Node5' / 'A->B' / 'A_B'.
    """
    edges = d.get("edgeStates") or []
    if not edges:
        return ""
    e = edges[0]
    if not isinstance(e, dict):
        return str(e)

    def nid(x):
        return (x.get("nodeId") or x.get("id") or "") if isinstance(x, dict) else (x or "")

    start = nid(e.get("startNodeId") or e.get("startNode"))
    end = nid(e.get("endNodeId") or e.get("endNode"))
    eid = e.get("edgeId") or e.get("edgeName") or e.get("id") or ""
    if (not start or not end) and eid:
        for sep in ("->", "→", "_", "-"):
            if sep in eid:
                a, _, b = eid.partition(sep)
                start, end = a.strip(), b.strip()
                break
    if start and end:
        return f"{start} → {end}"
    return eid or "cạnh ?"


def _append_state_log(code, data):
    """Append one JSON line (timestamp + code + response) to STATE_LOG_FILE per poll.

    A file-write error must not kill the poller -> wrap in try/except, swallow it.
    """
    try:
        rec = {"ts": datetime.now().isoformat(timespec="milliseconds"),
               "code": code, "data": data}
        with open(STATE_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass  # a logging error is fine, don't interrupt the heartbeat


def move_and_wait(client, node_name, timeout_s, stop_fn, log, status, wait_idle=False):
    """
    Send MoveToNode then poll /State until the robot reaches the target. Shared
    by single Move (wait_idle=False: robot busy -> fail immediately) and by Loop
    (wait_idle=True: robot busy -> wait until idle before sending the command).

    stop_fn() -> bool : return True to cancel. log/status : callable(str).
    return (ok: bool, msg: str).
    """
    # ── SYNC: ask /State (the shared source of truth) whether the robot is idle ──
    code, data = client.get_state()
    if code != 200 or not isinstance(data, dict):
        return False, f"Không đọc được /State để kiểm tra: {data}"
    if _compute_moving(data.get("data", {})):
        if not wait_idle:
            return False, ("⛔ Robot ĐANG CHẠY (theo /State) — không gửi lệnh mới. "
                           "Đợi nó dừng hoặc bấm Cancel trước.")
        # Loop: robot is busy (possibly another board) -> wait until idle
        status("⏳ Robot đang bận — chờ rảnh trước khi gửi lệnh...")
        while not stop_fn():
            time.sleep(0.3)
            code, data = client.get_state()
            if code == 200 and isinstance(data, dict) and not _compute_moving(data.get("data", {})):
                break
        if stop_fn():
            return False, "Đã hủy khi chờ rảnh"

    init_order = client.current_order_id()
    to_txt = "∞ (vô hạn)" if timeout_s <= 0 else f"{timeout_s}s"
    code, data = client.move_to_node(node_name)
    if code != 200:
        return False, f"MoveToNode failed: {data}"
    t0 = time.time()
    log(f"[OK] MoveToNode -> {node_name} | timeout={to_txt} | "
        f"bắt đầu {datetime.now().strftime('%H:%M:%S')}, đang chờ tới đích...")

    order_changed = False
    stall_since = None      # time the robot started standing still with path remaining
    stall_warned = False    # log the stall warning only once per stall episode
    while not stop_fn():
        elapsed = time.time() - t0
        # timeout_s <= 0 => wait forever (only stops on arrival or cancel)
        if timeout_s > 0 and elapsed > timeout_s:
            return False, (f"[TIMEOUT] lúc {datetime.now().strftime('%H:%M:%S')} — "
                           f"quá {timeout_s}s chưa tới đích (đã chạy {elapsed:.1f}s)")
        code, data = client.get_state()
        if code != 200:
            status(f"🔄 {elapsed:4.1f}s | ⚠ đọc State lỗi: {data}")
        if code == 200 and isinstance(data, dict):
            d = data.get("data", {})
            vel = d.get("velocity", {})
            stopped = (abs(vel.get("vx", 0)) < 1e-6 and
                       abs(vel.get("vy", 0)) < 1e-6 and
                       abs(vel.get("omega", 0)) < 1e-6)
            n_nodes = len(d.get("nodeStates", []))
            n_edges = len(d.get("edgeStates", []))
            estop = d.get("safetyState", {}).get("eStop", "?")
            status(
                f"🔄 {elapsed:4.1f}s | vx={vel.get('vx', 0):+.2f} "
                f"vy={vel.get('vy', 0):+.2f} ω={vel.get('omega', 0):+.2f} | "
                f"còn {n_nodes} node/{n_edges} edge | eStop={estop}"
            )
            # Stall detection: standing still but path NOT empty = stopped mid-route
            # (not yet at the target node). AMRs pause briefly for obstacles, so only
            # warn after STALL_SEC of continuous standstill; reset once it moves again.
            has_path = bool(d.get("nodeStates") or d.get("edgeStates"))
            if stopped and has_path:
                if stall_since is None:
                    stall_since = time.time()
                stall_dur = time.time() - stall_since
                if stall_dur >= STALL_SEC:
                    warn = (f"⚠ Robot DỪNG GIỮA CHỪNG {stall_dur:.0f}s chưa tới node "
                            f"(còn {n_nodes} node/{n_edges} edge, eStop={estop})")
                    status(warn)
                    if not stall_warned:
                        log(warn)
                        stall_warned = True
            else:
                stall_since = None
                stall_warned = False
            # Case 1: empty path + standing still (>0.7s)
            if (elapsed >= 0.7 and not d.get("nodeStates") and
                    not d.get("edgeStates") and stopped):
                return True, f"[OK] Đã tới đích sau {elapsed:.1f}s (path rỗng, đứng yên)"
            # Case 2: Finished Order
            for info in d.get("information", []):
                if info.get("infoType") != "order":
                    continue
                oid = ""
                for ref in info.get("infoReferences", []):
                    if ref.get("referenceKey") == "order_id":
                        oid = ref.get("referenceValue", "")
                finished = "Finished Order" in info.get("infoDescription", "")
                if oid and oid != init_order:
                    order_changed = True
                if order_changed and finished:
                    return True, f"[OK] Đã tới đích sau {elapsed:.1f}s (Finished Order)"
                if not order_changed and elapsed > 1.5 and finished:
                    return True, f"[OK] Robot đã ở node đích (sau {elapsed:.1f}s)"
        time.sleep(0.1 if elapsed < 1 else 0.3)
    return False, "Đã hủy chờ"


# ─────────────────────── Workers running in threads ──────────────────────
class Task(QThread):
    """Run a blocking function in a thread, emit (ok, message)."""
    done = Signal(bool, str)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self):
        try:
            ok, msg = self.fn()
            self.done.emit(ok, msg)
        except Exception as e:
            self.done.emit(False, f"Exception: {e}")


class StatusPoller(QThread):
    """Heartbeat: continuously ping /State to know if the robot is alive or dead."""
    beat = Signal(bool, bool, str, str)   # (online, moving, message, edge)

    def __init__(self, client, interval=2.0):
        super().__init__()
        self.client = client
        self.interval = interval
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        while not self._stop:
            try:
                code, data = self.client.get_state()
                _append_state_log(code, data)   # dump each poll to the JSONL file
                if code == 200 and isinstance(data, dict):
                    d = data.get("data", {})
                    vel = d.get("velocity", {})
                    estop = d.get("safetyState", {}).get("eStop", "?")
                    moving = _compute_moving(d)
                    edge = _edge_direction(d)
                    self.beat.emit(True, moving, (
                        f"vx={vel.get('vx', 0):+.2f} vy={vel.get('vy', 0):+.2f} "
                        f"ω={vel.get('omega', 0):+.2f} | eStop={estop}"
                    ), edge)
                else:
                    self.beat.emit(False, False, str(data), "")
            except Exception as e:
                self.beat.emit(False, False, str(e), "")
            # sleep in small ticks so stopping is responsive
            waited = 0.0
            while waited < self.interval and not self._stop:
                time.sleep(0.1)
                waited += 0.1


class ButtonTopicSubscriber(QThread):
    """Subscribe to a std_msgs/String topic (/button_state) and emit each
    message's data — the button letter 'A'/'B'/'X'/'Y' published by the dora
    bridge — to feed the task state machine.

    rclpy is imported lazily so the GUI still runs without a sourced ROS 2
    environment: if it's missing, this reports once via `error` and exits,
    and the on-screen A/B/X/Y buttons keep working for manual testing.
    """
    message = Signal(str)      # the button letter received on the topic
    error = Signal(str)

    def __init__(self, topic="/button_state"):
        super().__init__()
        self.topic = topic
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            import rclpy
            from rclpy.node import Node
            from std_msgs.msg import String
        except Exception as e:
            self.error.emit(f"ROS2 (rclpy) không có -> bỏ qua topic {self.topic}: {e}")
            return
        try:
            rclpy.init()
        except Exception:
            pass   # context may already be initialized elsewhere in-process
        node = Node("robot_gui_button_sub")
        node.create_subscription(
            String, self.topic,
            lambda m: self.message.emit(m.data), 10,
        )
        try:
            while not self._stop and rclpy.ok():
                rclpy.spin_once(node, timeout_sec=0.1)
        finally:
            node.destroy_node()
            try:
                rclpy.shutdown()
            except Exception:
                pass


class MoveWorker(QThread):
    """MoveToNode then poll State until the robot arrives (section 4 logic)."""
    log = Signal(str)
    status = Signal(str)      # live status each poll cycle
    done = Signal(bool, str)

    def __init__(self, client, node_name, timeout_s=60):
        super().__init__()
        self.client = client
        self.node_name = node_name
        self.timeout_s = timeout_s
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            self._run()
        except Exception as e:
            self.done.emit(False, f"MoveWorker exception: {e}")

    def _run(self):
        ok, msg = move_and_wait(
            self.client, self.node_name, self.timeout_s,
            lambda: self._stop, self.log.emit, self.status.emit,
            wait_idle=False,
        )
        self.done.emit(ok, msg)


class LoopWorker(QThread):
    """Run back and forth between 2 nodes for N loops (0 = infinite).

    Each loop = go to node A then node B. Before each command it checks whether
    the robot is idle (wait_idle=True) — if busy (another board), wait until idle.
    """
    log = Signal(str)
    status = Signal(str)
    done = Signal(bool, str)
    progress = Signal(int, int)   # (current loop, total loops; 0 = infinite)

    def __init__(self, client, node_a, node_b, loops, timeout_s=0):
        super().__init__()
        self.client = client
        self.nodes = [node_a, node_b]
        self.loops = loops        # number of loops; <=0 => infinite
        self.timeout_s = timeout_s
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            self._run()
        except Exception as e:
            self.done.emit(False, f"LoopWorker exception: {e}")

    def _run(self):
        total_txt = "∞" if self.loops <= 0 else str(self.loops)
        loop_no = 0
        while not self._stop:
            if self.loops > 0 and loop_no >= self.loops:
                self.done.emit(True, f"[OK] Hoàn tất {self.loops} vòng lặp.")
                return
            loop_no += 1
            self.progress.emit(loop_no, self.loops)
            for node in self.nodes:
                if self._stop:
                    self.done.emit(False, f"⏹ Đã dừng loop (vòng {loop_no}).")
                    return
                self.log.emit(f"── Vòng {loop_no}/{total_txt} — tới {node} ──")
                ok, msg = move_and_wait(
                    self.client, node, self.timeout_s,
                    lambda: self._stop, self.log.emit, self.status.emit,
                    wait_idle=True,
                )
                self.log.emit(msg)
                if not ok:
                    if self._stop:
                        self.done.emit(False, f"⏹ Đã dừng loop (vòng {loop_no}).")
                    else:
                        self.done.emit(False, f"⚠ Loop dừng vì lỗi ở {node}: {msg}")
                    return
        self.done.emit(False, "⏹ Đã dừng loop.")


# ───────────────── Task state machine (pick & place) ─────────────────────
# State pattern: one class per state. Each state processes an incoming message
# and returns the next state (or itself if the message is ignored / guard-blocked).
# Messages are the button letters "A"/"B"/"X"/"Y" (e.g. from a /button_state topic).
# Mirrors sm/pick_place_sm.puml:
#     IDLE --A[connected]--> MOVING_TO_A --X[reached_A&stopped]--> PICK
#     --B[pick_done]--> MOVING_TO_B --Y[reached_B&stopped]--> PLACE
#     --A--> MOVING_TO_A (loop)
class State:
    """Base state. Subclasses override process() and (optionally) on_enter()."""

    name = "STATE"

    def on_enter(self, ctx):
        """Called right after the machine enters this state. Robot commands go here."""

    def process(self, ctx, message):
        """Handle an incoming message; return the next State (default: stay put)."""
        ctx.log(f"[SM] ignore '{message}' in {self.name} (no transition)")
        return self


class IdleState(State):
    name = "IDLE"

    def process(self, ctx, message):
        if message == "A":
            if ctx.guard("robot_connected"):
                return MovingToAState()
            ctx.log("[SM] block A: robot not connected")
            return self
        return super().process(ctx, message)


class MovingToAState(State):
    name = "MOVING_TO_A"
    MAX_RETRY = 10      # give up after this many failed moves -> Cancel + IDLE

    def __init__(self):
        self.retries = 0

    def on_enter(self, ctx):
        ctx.log("[SM] -> MOVING_TO_A")
        ctx.command("move_to_pick")

    def process(self, ctx, message):
        if message == "MOVE_OK":
            ctx.log("[SM] move_to_pick OK — đã tới A")
            return self
        if message == "MOVE_FAIL":
            self.retries += 1
            if self.retries >= self.MAX_RETRY:
                ctx.log(f"[SM] move_to_pick lỗi {self.retries} lần -> Cancel + về IDLE")
                ctx.command("cancel")
                return IdleState()
            ctx.log(f"[SM] move_to_pick lỗi lần {self.retries}/{self.MAX_RETRY} — retry sau 1s")
            ctx.command("retry_move_pick")
            return self
        if message == "X":
            if ctx.guard("reached_A") and ctx.guard("robot_stopped"):
                return PickState()
            ctx.log("[SM] block X: need reached_A & robot_stopped")
            return self
        return super().process(ctx, message)


class PickState(State):
    name = "PICK"

    def on_enter(self, ctx):
        ctx.log("[SM] -> PICK")
        ctx.command("run_pick")

    def process(self, ctx, message):
        if message == "B":
            if ctx.guard("pick_done"):
                return MovingToBState()
            ctx.log("[SM] block B: need pick_done")
            return self
        return super().process(ctx, message)


class MovingToBState(State):
    name = "MOVING_TO_B"
    MAX_RETRY = 10      # give up after this many failed moves -> Cancel + IDLE

    def __init__(self):
        self.retries = 0

    def on_enter(self, ctx):
        ctx.log("[SM] -> MOVING_TO_B")
        ctx.command("move_to_place")

    def process(self, ctx, message):
        if message == "MOVE_OK":
            ctx.log("[SM] move_to_place OK — đã tới B")
            return self
        if message == "MOVE_FAIL":
            self.retries += 1
            if self.retries >= self.MAX_RETRY:
                ctx.log(f"[SM] move_to_place lỗi {self.retries} lần -> Cancel + về IDLE")
                ctx.command("cancel")
                return IdleState()
            ctx.log(f"[SM] move_to_place lỗi lần {self.retries}/{self.MAX_RETRY} — retry sau 1s")
            ctx.command("retry_move_place")
            return self
        if message == "Y":
            if ctx.guard("reached_B") and ctx.guard("robot_stopped"):
                return PlaceState()
            ctx.log("[SM] block Y: need reached_B & robot_stopped")
            return self
        return super().process(ctx, message)


class PlaceState(State):
    name = "PLACE"

    def on_enter(self, ctx):
        ctx.log("[SM] -> PLACE")
        ctx.command("run_place")

    def process(self, ctx, message):
        if message == "A":
            if ctx.guard("place_done"):
                return MovingToAState()
            ctx.log("[SM] block A: need place_done")
            return self
        return super().process(ctx, message)


class TaskStateMachine:
    """Context holding the current State; feeds it incoming messages.

    guard_fn(name)->bool answers conditions (reached_A, robot_stopped, ...);
    defaults to always-True until real signals are wired. log_fn(str) routes logs.
    """

    def __init__(self, log_fn=None, guard_fn=None, command_fn=None):
        self._log_fn = log_fn or (lambda s: None)
        self._guard_fn = guard_fn or (lambda name: True)
        self._command_fn = command_fn or (lambda action: None)
        self.state = IdleState()

    # -- hooks the states call back into --
    def log(self, msg):
        self._log_fn(msg)

    def guard(self, name):
        return bool(self._guard_fn(name))

    def command(self, action):
        """Ask the owner (GUI) to run a robot command, e.g. 'move_to_pick'."""
        self._command_fn(action)

    @property
    def state_name(self):
        return self.state.name

    def process(self, message):
        """Feed an incoming message ('A'/'B'/'X'/'Y'); return True if state changed."""
        message = (message or "").strip().upper()
        nxt = self.state.process(self, message)
        if nxt is self.state:
            return False
        self.log(f"[SM] {self.state.name} --{message}--> {nxt.name}")
        self.state = nxt
        nxt.on_enter(self)
        return True

    def reset(self):
        """Back to IDLE."""
        self.state = IdleState()
        self.log("[SM] reset to IDLE")


# ─────────────────────────────── GUI ────────────────────────────────────
class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AMR I150 — Control Panel")
        self.setMinimumWidth(480)
        self.client = RobotClient()
        self._tasks = []       # keep thread refs so they aren't GC'd
        self.move_worker = None
        self.loop_worker = None
        # Whether the robot has actually ARRIVED at the pick/place node — set True
        # only when a MoveWorker reports arrival, reset when that move (re)starts.
        # Real source for the state machine's reached_A / reached_B guards.
        self._reached = {"pick": False, "place": False}

        root = QVBoxLayout(self)

        # ── Connection ──
        conn = QGroupBox("Kết nối")
        cg = QGridLayout(conn)
        self.ip_edit = QLineEdit("192.168.100.100")
        self.ip_edit.setMinimumWidth(160)     # don't let it shrink when the status text is long
        self.port_edit = QLineEdit("8081")
        self.port_edit.setFixedWidth(70)
        self.id_edit = QLineEdit("I150")
        self.id_edit.setMinimumWidth(160)
        cg.addWidget(QLabel("Robot IP:"), 0, 0)
        cg.addWidget(self.ip_edit, 0, 1)
        cg.addWidget(QLabel("Port:"), 0, 2)
        cg.addWidget(self.port_edit, 0, 3)
        cg.addWidget(QLabel("Robot ID:"), 1, 0)
        cg.addWidget(self.id_edit, 1, 1)
        # connection indicator LED (heartbeat)
        self.led = QLabel()
        self.led.setFixedSize(22, 22)
        self.conn_lbl = QLabel("Đang khởi động theo dõi...")
        self.conn_lbl.setStyleSheet("font-weight: bold;")
        hb = QHBoxLayout()
        hb.setContentsMargins(0, 0, 0, 0)
        hb.addWidget(self.led)
        hb.addWidget(self.conn_lbl, 1)
        cg.addLayout(hb, 2, 0, 1, 4)
        self._set_led("gray")   # initial state: unknown
        # apply IP/Port/ID changes to the heartbeat immediately
        self.ip_edit.editingFinished.connect(self._sync_client)
        self.port_edit.editingFinished.connect(self._sync_client)
        self.id_edit.editingFinished.connect(self._sync_client)
        root.addWidget(conn)

        # ── Move ──
        move = QGroupBox("Di chuyển")
        mg = QHBoxLayout(move)
        self.node_combo = QComboBox()
        self.node_combo.setEditable(True)
        self.node_combo.addItems(["Node1", "Node2", "Node3", "Node4", "Node5"])
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(0, 86400)   # 0 = wait forever
        self.timeout_spin.setValue(0)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setSpecialValueText("∞ (vô hạn)")  # shown when = 0
        self.timeout_spin.setToolTip("Thời gian tối đa chờ robot tới đích. 0 = chờ mãi tới khi tới nơi hoặc bấm Cancel.")
        self.btn_move = QPushButton("MoveToNode")
        self.btn_cancel = QPushButton("Cancel")
        self.btn_move.clicked.connect(self.on_move)
        self.btn_cancel.clicked.connect(self.on_cancel)
        mg.addWidget(QLabel("Node:"))
        mg.addWidget(self.node_combo, 1)
        mg.addWidget(QLabel("Timeout:"))
        mg.addWidget(self.timeout_spin)
        mg.addWidget(self.btn_move)
        mg.addWidget(self.btn_cancel)
        root.addWidget(move)

        # ── Test: pick/place nodes used by the state machine ──
        test = QGroupBox("Test")
        tg = QGridLayout(test)
        self.node_pick_combo = QComboBox()      # Node A = nơi nhặt (pick)
        self.node_pick_combo.setEditable(True)
        self.node_pick_combo.setCurrentText("Node A")
        self.node_place_combo = QComboBox()     # Node B = nơi thả (place)
        self.node_place_combo.setEditable(True)
        self.node_place_combo.setCurrentText("Node B")
        tg.addWidget(QLabel("Node pick (Node A):"), 0, 0)
        tg.addWidget(self.node_pick_combo, 0, 1)
        tg.addWidget(QLabel("Node place (Node B):"), 1, 0)
        tg.addWidget(self.node_place_combo, 1, 1)
        root.addWidget(test)

        # ── Task state machine (fed only from the ROS2 /button_state topic) ──
        self.task_sm = TaskStateMachine(
            log_fn=self._sm_log, guard_fn=self._sm_guard, command_fn=self._sm_command,
        )
        sm_box = QGroupBox("State machine (nhận từ /button_state)")
        smg = QVBoxLayout(sm_box)
        self.sm_state_lbl = QLabel(f"State: {self.task_sm.state_name}")
        self.sm_state_lbl.setStyleSheet("font-weight: bold; font-size: 14px;")
        smg.addWidget(self.sm_state_lbl)
        root.addWidget(sm_box)

        # ── Status + log ──
        self.status_lbl = QLabel("Sẵn sàng.")
        self.status_lbl.setStyleSheet("font-weight: bold;")
        root.addWidget(self.status_lbl)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        root.addWidget(self.log_box, 1)

        # ── Disconnect alarm ──
        self._offline_since = None     # time the disconnection started
        self._alarm_on = False
        self._alarm_timer = QTimer(self)
        self._alarm_timer.setInterval(700)   # repeat the beep every 0.7s
        self._alarm_timer.timeout.connect(self._beep)

        # ── Retry a failed state-machine move every 1s ──
        self._retry_action = None      # pending SM move to retry ('move_to_pick'/'place')
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.setInterval(1000)   # try again 1s after a failure
        self._retry_timer.timeout.connect(self._retry_sm_move)

        # ── Heartbeat: monitor the connection continuously ──
        self._prev_online = None       # None=unknown, True/False
        self._prev_edge = None         # previous edge being traversed (to log only on change)
        self._robot_moving = False     # whether the robot is moving (per /State)
        self._robot_online = False     # whether the heartbeat currently reaches the robot
        self._sync_client()
        self.poller = StatusPoller(self.client, interval=2.0)
        self.poller.beat.connect(self._on_beat)
        self.poller.start()

        # ── Feed the state machine from the ROS2 /button_state topic ──
        self.button_sub = ButtonTopicSubscriber("/button_state")
        self.button_sub.message.connect(self.on_sm_message)
        self.button_sub.error.connect(self.log)
        self.button_sub.start()

    # ── helpers ──
    def _sync_client(self):
        ip = self.ip_edit.text().strip()
        port = self.port_edit.text().strip() or "8081"
        # Allow entering either "https://ip:port" or just "ip"
        if ip.startswith("http"):
            self.client.base_url = ip.rstrip("/")
        else:
            self.client.base_url = f"https://{ip}:{port}"
        self.client.robot_id = self.id_edit.text().strip() or "I150"

    def log(self, text):
        self.log_box.append(text)

    def _set_led(self, color, bright=True):
        """Draw a round LED. color: 'green' | 'red' | 'gray'."""
        shades = {
            "green": ("#2ecc40", "#1a7a20"),
            "red":   ("#ff4136", "#a01010"),
            "gray":  ("#bbbbbb", "#888888"),
        }
        on, off = shades.get(color, shades["gray"])
        c = on if bright else off
        self.led.setStyleSheet(
            f"background-color: {c}; border-radius: 11px; "
            f"border: 1px solid rgba(0,0,0,0.35);"
        )

    def _beep(self):
        """Sound one beep (system bell + BEL character for the terminal)."""
        app = QApplication.instance()
        if app is not None:
            app.beep()
        # fallback: some Linux disable the Qt bell -> send BEL to the terminal
        try:
            sys.stdout.write("\a")
            sys.stdout.flush()
        except Exception:
            pass

    def _start_alarm(self, down):
        if self._alarm_on:
            return
        self._alarm_on = True
        now = datetime.now().strftime("%H:%M:%S")
        self.log(f"📣 [{now}] CẢNH BÁO: mất kết nối > {OFFLINE_ALARM_SEC}s "
                 f"(đã {down:.0f}s) — HÚ CÒI!")
        self.status_lbl.setText(f"📣 MẤT KẾT NỐI > {OFFLINE_ALARM_SEC}s — kiểm tra robot!")
        self._beep()                 # beep right away for the first tone
        self._alarm_timer.start()    # then repeat

    def _stop_alarm(self):
        if not self._alarm_on:
            return
        self._alarm_on = False
        self._alarm_timer.stop()
        now = datetime.now().strftime("%H:%M:%S")
        self.log(f"🔇 [{now}] Tắt còi (đã kết nối lại).")

    def _on_beat(self, online, moving, msg, edge=""):
        """Update the connection LED + busy state; lock Move while the robot is moving."""
        now = datetime.now().strftime("%H:%M:%S")
        # log the edge the robot is traversing (straight from the /State poll), only on change
        if online and edge and edge != getattr(self, "_prev_edge", None):
            self.log(f"🚚 [{now}] đang đi {edge}")
        self._prev_edge = edge if online else None
        short = msg if len(msg) <= 70 else msg[:67] + "..."   # avoid stretching the layout
        # blink: toggle bright/dim each beat to show it's running live
        self._pulse = not getattr(self, "_pulse", False)
        self._robot_moving = moving if online else False
        self._robot_online = online     # heartbeat connection state, read by the SM guard
        if online:
            self._set_led("green", bright=self._pulse)
            tag = "🏃 ĐANG CHẠY" if moving else "🟢 rảnh"
            self.conn_lbl.setText(f"ONLINE {tag} ({now}) — {short}")
            self.conn_lbl.setStyleSheet("font-weight: bold; color: #1a9e1a;")
            if self._prev_online is False:
                down = time.time() - self._offline_since if self._offline_since else 0
                self.log(f"🟢 [{now}] Robot kết nối LẠI (đã mất {down:.0f}s).")
            self._offline_since = None
            self._stop_alarm()
        else:
            self._set_led("red", bright=self._pulse)
            # count how long the connection has been lost
            if self._offline_since is None:
                self._offline_since = time.time()
            down = time.time() - self._offline_since
            remain = OFFLINE_ALARM_SEC - down
            if remain > 0:
                extra = f"mất {down:.0f}s — còi sau {remain:.0f}s"
            else:
                extra = f"⚠ MẤT KẾT NỐI {down:.0f}s — ĐANG HÚ CÒI"
            self.conn_lbl.setText(f"OFFLINE ({now}) [{extra}] — {short}")
            self.conn_lbl.setStyleSheet("font-weight: bold; color: #d11;")
            if self._prev_online is True:
                self.log(f"🔴 [{now}] MẤT KẾT NỐI robot! ({msg})")
            # over the threshold -> sound the alarm
            if down >= OFFLINE_ALARM_SEC and not self._alarm_on:
                self._start_alarm(down)
        # lock the Move button when the robot is moving (even another board) or offline
        busy = self._robot_moving or self._any_move_busy()
        self.btn_move.setEnabled(online and not busy)
        self._prev_online = online

    def _any_move_busy(self):
        """True if a single Move command is running."""
        return bool(self.move_worker and self.move_worker.isRunning())

    def closeEvent(self, event):
        """Stop background threads before exiting to avoid a crash."""
        if hasattr(self, "_alarm_timer"):
            self._alarm_timer.stop()
        if hasattr(self, "_retry_timer"):
            self._retry_timer.stop()
        if hasattr(self, "poller"):
            self.poller.stop()
            self.poller.wait(3000)
        if hasattr(self, "button_sub"):
            self.button_sub.stop()
            self.button_sub.wait(3000)
        if self.move_worker and self.move_worker.isRunning():
            self.move_worker.stop()
            self.move_worker.wait(3000)
        if self.loop_worker and self.loop_worker.isRunning():
            self.loop_worker.stop()
            self.loop_worker.wait(3000)
        super().closeEvent(event)

    def _run(self, fn):
        """Run fn in a thread, log the result."""
        task = Task(fn)
        task.done.connect(self._on_task_done)
        task.finished.connect(lambda t=task: self._tasks.remove(t))
        self._tasks.append(task)
        task.start()

    def _on_task_done(self, ok, msg):
        self.log(msg)
        self.status_lbl.setText(msg if ok else f"⚠ {msg}")
        # command done -> allow Move again (the heartbeat will also update on its own)
        self.btn_move.setEnabled(True)

    # ── Task state machine ──
    def _sm_log(self, msg):
        """State-machine log -> terminal (stdout) only, not the GUI log box."""
        print(msg, flush=True)

    def _feed_sm(self, message):
        """Feed a message/event into the state machine, then refresh the state label.
        Used for both button letters (A/B/X/Y) and move events (move_ok/move_fail)."""
        self.task_sm.process(message)
        self.sm_state_lbl.setText(f"State: {self.task_sm.state_name}")

    def on_sm_message(self, message):
        """A button (or /button_state topic) letter -> feed the state machine."""
        self._feed_sm(message)

    def _sm_guard(self, name):
        """Answer a state-machine guard from real signals:
          robot_connected : heartbeat reaches the robot
          robot_stopped   : heartbeat says velocity ~= 0
          reached_A/B     : the MoveWorker reported arrival at the pick/place node
                            (a stall mid-route never sets this -> blocks PICK/PLACE)
        pick_done is still a stub (True) until gripper feedback is wired."""
        if name == "robot_connected":
            return self._robot_online
        if name == "robot_stopped":
            return not self._robot_moving
        if name == "reached_A":
            return self._reached["pick"]
        if name == "reached_B":
            return self._reached["place"]
        return True

    # ── actions ──
    def on_move(self):
        self._start_move(self.node_combo.currentText().strip())

    def _start_move(self, node, sm_target=None):
        """Send a REAL MoveToNode: spawn a MoveWorker that commands the node then
        polls /State until it arrives. Shared by the MoveToNode button AND by the
        state machine's on_enter hooks, so the SM actually drives the robot.

        sm_target ('pick'/'place'/None): when set, this move belongs to the state
        machine — reset that reached flag now and set it only if the robot arrives.
        """
        self._sync_client()
        if not node:
            self.log("⚠ Node rỗng — không gửi MoveToNode.")
            return
        if self._any_move_busy() or self._robot_moving:
            # Transient (another move running / robot still moving). For an SM move,
            # try again in 1s instead of dropping it (not a hard failure, so it does
            # NOT count toward the retry limit). Arming the timer is async -> safe to
            # do from inside on_enter.
            self.log("⏳ Bận (move khác / robot đang chạy) — hoãn MoveToNode 1s.")
            if sm_target:
                self._retry_action = "move_to_pick" if sm_target == "pick" else "move_to_place"
                self._retry_timer.start()
            return
        if sm_target:
            self._reached[sm_target] = False   # not there yet; set on arrival
        self.btn_move.setEnabled(False)   # lock immediately to prevent a double click
        self.status_lbl.setText(f"Đang di chuyển tới {node}...")
        self.move_worker = MoveWorker(self.client, node, self.timeout_spin.value())
        self.move_worker.log.connect(self.log)
        self.move_worker.status.connect(self.status_lbl.setText)  # live status
        self.move_worker.done.connect(
            lambda ok, msg, t=sm_target: self._on_move_done(ok, msg, t)
        )
        self.move_worker.start()

    def _on_move_done(self, ok, msg, sm_target=None):
        """MoveWorker finished. Log + re-enable Move, mark the pick/place node as
        reached only if the robot actually arrived (ok), and report the result back
        to the state machine as a move_ok / move_fail event so the MOVING superstate
        can advance or retry. Runs on the main thread (queued signal) -> not
        re-entrant with an in-flight process()."""
        self._on_task_done(ok, msg)
        if not sm_target:
            return
        self._reached[sm_target] = bool(ok)
        if ok:
            self.log(f"[SM] đã tới node {sm_target} -> reached_{sm_target}=True")
        self._feed_sm("move_ok" if ok else "move_fail")

    def _sm_command(self, action):
        """Robot commands requested by the state machine (on_enter / retry / give-up):
          move_to_pick/place   : issue a real MoveToNode to the Test-group node
          retry_move_pick/place: re-issue that move 1s later (via the retry timer)
          cancel               : stop + cancel the robot (SM gave up after MAX_RETRY)
          run_pick/run_place   : gripper action (TODO — not wired yet)."""
        if action == "move_to_pick":
            node = self.node_pick_combo.currentText().strip()
            self.log(f"[SM] MoveToNode (pick) -> {node}")
            self._start_move(node, sm_target="pick")
        elif action == "move_to_place":
            node = self.node_place_combo.currentText().strip()
            self.log(f"[SM] MoveToNode (place) -> {node}")
            self._start_move(node, sm_target="place")
        elif action in ("retry_move_pick", "retry_move_place"):
            self._retry_action = ("move_to_pick" if action == "retry_move_pick"
                                  else "move_to_place")
            self._retry_timer.start()
        elif action == "cancel":
            self._sm_cancel()
        elif action in ("run_pick", "run_place"):
            self.log(f"[SM] {action} (TODO: chưa gắn lệnh gripper)")

    def _retry_sm_move(self):
        """Retry timer fired: re-issue the pending SM move, but only if the SM is
        still in the matching MOVING state (bail out if it moved on / gave up)."""
        action = self._retry_action
        self._retry_action = None
        if not action:
            return
        expected = {"move_to_pick": "MOVING_TO_A", "move_to_place": "MOVING_TO_B"}[action]
        if self.task_sm.state_name != expected:
            return
        # Cancel first, THEN re-issue the move: clears the failed/stuck order so the
        # fresh MoveToNode isn't rejected for one already pending.
        self._sync_client()
        code, _ = self.client.cancel_move()
        self.log(f"🔁 [SM] Cancel (HTTP {code}) rồi retry {action}")
        self._sm_command(action)

    def _sm_cancel(self):
        """Stop + cancel robot motion — used when the SM gives up after MAX_RETRY."""
        self._retry_action = None
        self._retry_timer.stop()
        self._sync_client()
        if self.move_worker and self.move_worker.isRunning():
            self.move_worker.stop()
        def fn():
            code, data = self.client.cancel_move()
            return code == 200, f"[SM] Cancel HTTP {code}: {data}"
        self._run(fn)

    def on_cancel(self):
        self._sync_client()
        if self.move_worker and self.move_worker.isRunning():
            self.move_worker.stop()
        # A manual Cancel must also kill any pending SM retry, otherwise the retry
        # timer would re-issue a move 1s later (robot moves again after Cancel).
        # Send the SM back to IDLE so it matches the robot being stopped.
        self._retry_timer.stop()
        self._retry_action = None
        self.task_sm.reset()
        self.sm_state_lbl.setText(f"State: {self.task_sm.state_name}")
        def fn():
            code, data = self.client.cancel_move()
            return code == 200, f"Cancel HTTP {code}: {data}"
        self._run(fn)


def install_gui_excepthook(window):
    """Any uncaught exception -> push straight to the GUI log box."""
    import traceback

    def hook(exc_type, exc, tb):
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        window.log("‼ LỖI (uncaught):\n" + msg)
        window.status_lbl.setText("‼ Có lỗi — xem log bên dưới")
        # still print to the terminal for debugging
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    install_gui_excepthook(win)
    win.show()
    # PySide6/PySide2 use exec(), old PyQt5 uses exec_()
    run = getattr(app, "exec", None) or app.exec_
    sys.exit(run())


if __name__ == "__main__":
    main()
