#!/usr/bin/env python3
"""
GUI điều khiển AMR I150 qua REST API (PySide6).
Robot: https://192.168.100.100:8081  — self-signed SSL (tắt verify).

Cài đặt:
    pip install PySide6 requests

Chạy:
    python robot_gui.py
"""
import sys
import time
from datetime import datetime

import requests
import urllib3

# Hỗ trợ nhiều backend Qt: PySide6 (pip) hoặc PyQt5 (apt install python3-pyqt5)
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

# Robot dùng self-signed cert -> tắt cảnh báo InsecureRequest
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Mất kết nối quá ngưỡng này (giây) -> hú còi cảnh báo
OFFLINE_ALARM_SEC = 30


# ─────────────────────────── Robot REST client ───────────────────────────
class RobotClient:
    def __init__(self, base_url="https://192.168.100.100:8081", robot_id="I150"):
        self.base_url = base_url.rstrip("/")
        self.robot_id = robot_id
        self.session = requests.Session()
        self.session.verify = False  # bỏ qua self-signed cert

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

    # ── helper: order_id hiện tại ──
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


# ───────────────────── Helpers dùng chung cho các worker ─────────────────
def _compute_moving(d):
    """True nếu robot ĐANG CHẠY theo dict data của /State."""
    vel = d.get("velocity", {})
    return (bool(d.get("nodeStates")) or bool(d.get("edgeStates")) or
            abs(vel.get("vx", 0)) > 1e-6 or abs(vel.get("vy", 0)) > 1e-6 or
            abs(vel.get("omega", 0)) > 1e-6)


def move_and_wait(client, node_name, timeout_s, stop_fn, log, status, wait_idle=False):
    """
    Gửi MoveToNode rồi poll /State đến khi robot tới đích. Dùng chung cho
    Move đơn lẻ (wait_idle=False: robot bận -> báo lỗi ngay) và cho Loop
    (wait_idle=True: robot bận -> chờ tới khi rảnh rồi mới gửi lệnh).

    stop_fn() -> bool : trả True để hủy. log/status : callable(str).
    return (ok: bool, msg: str).
    """
    # ── ĐỒNG BỘ: hỏi /State (nguồn sự thật chung) xem robot có rảnh không ──
    code, data = client.get_state()
    if code != 200 or not isinstance(data, dict):
        return False, f"Không đọc được /State để kiểm tra: {data}"
    if _compute_moving(data.get("data", {})):
        if not wait_idle:
            return False, ("⛔ Robot ĐANG CHẠY (theo /State) — không gửi lệnh mới. "
                           "Đợi nó dừng hoặc bấm Cancel trước.")
        # Loop: robot đang bận (có thể do board khác) -> chờ rảnh
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
    while not stop_fn():
        elapsed = time.time() - t0
        # timeout_s <= 0 => chờ vô hạn (chỉ dừng khi tới đích hoặc bị hủy)
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
            # Case 1: đường đi rỗng + đứng yên (>0.7s)
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


# ─────────────────────── Worker chạy trong thread ───────────────────────
class Task(QThread):
    """Chạy 1 hàm blocking trong thread, emit (ok, message)."""
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
    """Heartbeat: liên tục ping /State để biết robot còn sống hay đã chết."""
    beat = Signal(bool, bool, str)   # (online, moving, message)

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
                if code == 200 and isinstance(data, dict):
                    d = data.get("data", {})
                    vel = d.get("velocity", {})
                    estop = d.get("safetyState", {}).get("eStop", "?")
                    moving = _compute_moving(d)
                    self.beat.emit(True, moving, (
                        f"vx={vel.get('vx', 0):+.2f} vy={vel.get('vy', 0):+.2f} "
                        f"ω={vel.get('omega', 0):+.2f} | eStop={estop}"
                    ))
                else:
                    self.beat.emit(False, False, str(data))
            except Exception as e:
                self.beat.emit(False, False, str(e))
            # ngủ theo từng nhịp nhỏ để dừng cho nhạy
            waited = 0.0
            while waited < self.interval and not self._stop:
                time.sleep(0.1)
                waited += 0.1


class MoveWorker(QThread):
    """MoveToNode rồi poll State đến khi robot tới đích (logic mục 4)."""
    log = Signal(str)
    status = Signal(str)      # trạng thái live mỗi vòng poll
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
    """Chạy đi-chạy-lại giữa 2 node N vòng (0 = vô hạn).

    Mỗi vòng = tới node A rồi tới node B. Trước mỗi lệnh tự kiểm tra robot có
    rảnh không (wait_idle=True) — nếu bận (do board khác) thì chờ tới khi rảnh.
    """
    log = Signal(str)
    status = Signal(str)
    done = Signal(bool, str)
    progress = Signal(int, int)   # (vòng hiện tại, tổng số vòng; 0 = vô hạn)

    def __init__(self, client, node_a, node_b, loops, timeout_s=0):
        super().__init__()
        self.client = client
        self.nodes = [node_a, node_b]
        self.loops = loops        # số vòng; <=0 => vô hạn
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


# ─────────────────────────────── GUI ────────────────────────────────────
class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AMR I150 — Control Panel")
        self.setMinimumWidth(480)
        self.client = RobotClient()
        self._tasks = []       # giữ ref thread để không bị GC
        self.move_worker = None
        self.loop_worker = None

        root = QVBoxLayout(self)

        # ── Kết nối ──
        conn = QGroupBox("Kết nối")
        cg = QGridLayout(conn)
        self.ip_edit = QLineEdit("192.168.100.100")
        self.ip_edit.setMinimumWidth(160)     # không cho co lại khi text status dài
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
        # đèn báo kết nối (heartbeat)
        self.led = QLabel()
        self.led.setFixedSize(22, 22)
        self.conn_lbl = QLabel("Đang khởi động theo dõi...")
        self.conn_lbl.setStyleSheet("font-weight: bold;")
        hb = QHBoxLayout()
        hb.setContentsMargins(0, 0, 0, 0)
        hb.addWidget(self.led)
        hb.addWidget(self.conn_lbl, 1)
        cg.addLayout(hb, 2, 0, 1, 4)
        self._set_led("gray")   # trạng thái ban đầu: chưa biết
        # IP/Port/ID đổi lúc nào cũng áp dụng ngay cho heartbeat
        self.ip_edit.editingFinished.connect(self._sync_client)
        self.port_edit.editingFinished.connect(self._sync_client)
        self.id_edit.editingFinished.connect(self._sync_client)
        root.addWidget(conn)

        # ── Di chuyển ──
        move = QGroupBox("Di chuyển")
        mg = QHBoxLayout(move)
        self.node_combo = QComboBox()
        self.node_combo.setEditable(True)
        self.node_combo.addItems(["Node1", "Node2", "Node3", "Node4", "Node5"])
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(0, 86400)   # 0 = chờ vô hạn
        self.timeout_spin.setValue(0)
        self.timeout_spin.setSuffix(" s")
        self.timeout_spin.setSpecialValueText("∞ (vô hạn)")  # hiển thị khi = 0
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

        # ── Chạy lặp 2 node (ping-pong) ──
        loop = QGroupBox("Chạy lặp 2 node (A ↔ B)")
        lg = QGridLayout(loop)
        self.node_a_combo = QComboBox()
        self.node_a_combo.setEditable(True)
        self.node_a_combo.addItems(["Node1", "Node2", "Node3", "Node4", "Node5"])
        self.node_a_combo.setCurrentText("Node1")
        self.node_b_combo = QComboBox()
        self.node_b_combo.setEditable(True)
        self.node_b_combo.addItems(["Node1", "Node2", "Node3", "Node4", "Node5"])
        self.node_b_combo.setCurrentText("Node2")
        self.loop_spin = QSpinBox()
        self.loop_spin.setRange(0, 100000)      # 0 = vô hạn
        self.loop_spin.setValue(0)
        self.loop_spin.setSuffix(" vòng")
        self.loop_spin.setSpecialValueText("∞ (vô hạn)")
        self.loop_spin.setToolTip("Số vòng lặp. 1 vòng = tới A rồi tới B. 0 = chạy mãi tới khi bấm Dừng.")
        self.btn_loop_start = QPushButton("▶ Bắt đầu Loop")
        self.btn_loop_stop = QPushButton("⏹ Dừng Loop")
        self.btn_loop_start.clicked.connect(self.on_loop_start)
        self.btn_loop_stop.clicked.connect(self.on_loop_stop)
        self.loop_lbl = QLabel("—")
        lg.addWidget(QLabel("Node A:"), 0, 0)
        lg.addWidget(self.node_a_combo, 0, 1)
        lg.addWidget(QLabel("Node B:"), 0, 2)
        lg.addWidget(self.node_b_combo, 0, 3)
        lg.addWidget(QLabel("Số vòng:"), 1, 0)
        lg.addWidget(self.loop_spin, 1, 1)
        lg.addWidget(self.loop_lbl, 1, 2)
        hb2 = QHBoxLayout()
        hb2.addWidget(self.btn_loop_start)
        hb2.addWidget(self.btn_loop_stop)
        lg.addLayout(hb2, 1, 3)
        root.addWidget(loop)

        # ── Trạng thái + log ──
        self.status_lbl = QLabel("Sẵn sàng.")
        self.status_lbl.setStyleSheet("font-weight: bold;")
        root.addWidget(self.status_lbl)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        root.addWidget(self.log_box, 1)

        # ── Còi cảnh báo mất kết nối ──
        self._offline_since = None     # thời điểm bắt đầu mất kết nối
        self._alarm_on = False
        self._alarm_timer = QTimer(self)
        self._alarm_timer.setInterval(700)   # hú lặp lại mỗi 0.7s
        self._alarm_timer.timeout.connect(self._beep)

        # ── Heartbeat: theo dõi kết nối liên tục ──
        self._prev_online = None       # None=chưa biết, True/False
        self._robot_moving = False     # robot có đang chạy không (theo /State)
        self._sync_client()
        self.poller = StatusPoller(self.client, interval=2.0)
        self.poller.beat.connect(self._on_beat)
        self.poller.start()

    # ── helpers ──
    def _sync_client(self):
        ip = self.ip_edit.text().strip()
        port = self.port_edit.text().strip() or "8081"
        # Cho phép nhập cả "https://ip:port" hoặc chỉ "ip"
        if ip.startswith("http"):
            self.client.base_url = ip.rstrip("/")
        else:
            self.client.base_url = f"https://{ip}:{port}"
        self.client.robot_id = self.id_edit.text().strip() or "I150"

    def log(self, text):
        self.log_box.append(text)

    def _set_led(self, color, bright=True):
        """Vẽ đèn LED tròn. color: 'green' | 'red' | 'gray'."""
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
        """Kêu 1 tiếng còi (chuông hệ thống + ký tự BEL cho terminal)."""
        app = QApplication.instance()
        if app is not None:
            app.beep()
        # fallback: một số Linux tắt chuông Qt -> gửi BEL ra terminal
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
        self._beep()                 # kêu ngay tiếng đầu
        self._alarm_timer.start()    # rồi lặp lại

    def _stop_alarm(self):
        if not self._alarm_on:
            return
        self._alarm_on = False
        self._alarm_timer.stop()
        now = datetime.now().strftime("%H:%M:%S")
        self.log(f"🔇 [{now}] Tắt còi (đã kết nối lại).")

    def _on_beat(self, online, moving, msg):
        """Cập nhật đèn kết nối + trạng thái bận; khóa Move khi robot đang chạy."""
        now = datetime.now().strftime("%H:%M:%S")
        short = msg if len(msg) <= 70 else msg[:67] + "..."   # tránh kéo giãn layout
        # nhấp nháy: mỗi nhịp đổi sáng/tối để thấy rõ nó đang chạy live
        self._pulse = not getattr(self, "_pulse", False)
        self._robot_moving = moving if online else False
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
            # đếm thời gian mất kết nối
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
            # quá ngưỡng -> hú còi
            if down >= OFFLINE_ALARM_SEC and not self._alarm_on:
                self._start_alarm(down)
        # khóa nút Move/Loop khi robot đang chạy (dù do board khác) hoặc offline
        loop_running = bool(self.loop_worker and self.loop_worker.isRunning())
        busy = self._robot_moving or self._any_move_busy()
        # Loop tự xử lý chờ-rảnh nên nút Move đơn lẻ khóa khi bận; nút bắt đầu
        # loop chỉ khóa khi đang có move đơn lẻ hoặc loop đang chạy (không khóa
        # theo _robot_moving để còn khởi động được loop khi board khác đang chạy).
        self.btn_move.setEnabled(online and not busy)
        self.btn_loop_start.setEnabled(online and not self._any_move_busy())
        self.btn_loop_stop.setEnabled(loop_running)
        self._prev_online = online

    def _any_move_busy(self):
        """True nếu đang có lệnh Move đơn lẻ HOẶC Loop chạy."""
        return ((self.move_worker and self.move_worker.isRunning()) or
                (self.loop_worker and self.loop_worker.isRunning()))

    def closeEvent(self, event):
        """Dừng các thread nền trước khi thoát để khỏi crash."""
        if hasattr(self, "_alarm_timer"):
            self._alarm_timer.stop()
        if hasattr(self, "poller"):
            self.poller.stop()
            self.poller.wait(3000)
        if self.move_worker and self.move_worker.isRunning():
            self.move_worker.stop()
            self.move_worker.wait(3000)
        if self.loop_worker and self.loop_worker.isRunning():
            self.loop_worker.stop()
            self.loop_worker.wait(3000)
        super().closeEvent(event)

    def _run(self, fn):
        """Chạy fn trong thread, log kết quả."""
        task = Task(fn)
        task.done.connect(self._on_task_done)
        task.finished.connect(lambda t=task: self._tasks.remove(t))
        self._tasks.append(task)
        task.start()

    def _on_task_done(self, ok, msg):
        self.log(msg)
        self.status_lbl.setText(msg if ok else f"⚠ {msg}")
        # lệnh xong -> cho phép Move lại (heartbeat cũng sẽ tự cập nhật)
        self.btn_move.setEnabled(True)

    # ── actions ──
    def on_move(self):
        self._sync_client()
        node = self.node_combo.currentText().strip()
        if not node:
            return
        if self._any_move_busy():
            self.log("⚠ Đang có lệnh move/loop chạy, dừng trước đã.")
            return
        if self._robot_moving:
            self.log("⛔ Robot đang chạy (theo /State) — không gửi lệnh mới.")
            return
        self.btn_move.setEnabled(False)   # khóa ngay, khỏi bấm 2 lần
        self.status_lbl.setText(f"Đang di chuyển tới {node}...")
        self.move_worker = MoveWorker(self.client, node, self.timeout_spin.value())
        self.move_worker.log.connect(self.log)
        self.move_worker.status.connect(self.status_lbl.setText)  # live status
        self.move_worker.done.connect(self._on_task_done)
        self.move_worker.start()

    def on_cancel(self):
        self._sync_client()
        if self.move_worker and self.move_worker.isRunning():
            self.move_worker.stop()
        def fn():
            code, data = self.client.cancel_move()
            return code == 200, f"Cancel HTTP {code}: {data}"
        self._run(fn)

    # ── Loop 2 node ──
    def on_loop_start(self):
        self._sync_client()
        a = self.node_a_combo.currentText().strip()
        b = self.node_b_combo.currentText().strip()
        if not a or not b:
            self.log("⚠ Chọn đủ 2 node A và B cho vòng lặp.")
            return
        if a == b:
            self.log("⚠ Node A và B giống nhau — chọn 2 node khác nhau.")
            return
        if self._any_move_busy():
            self.log("⚠ Đang có lệnh move/loop chạy, dừng trước đã.")
            return
        loops = self.loop_spin.value()
        self.loop_worker = LoopWorker(self.client, a, b, loops, self.timeout_spin.value())
        self.loop_worker.log.connect(self.log)
        self.loop_worker.status.connect(self.status_lbl.setText)
        self.loop_worker.progress.connect(self._on_loop_progress)
        self.loop_worker.done.connect(self._on_loop_done)
        self.btn_loop_start.setEnabled(False)
        self.btn_loop_stop.setEnabled(True)
        self.btn_move.setEnabled(False)
        txt = "∞" if loops <= 0 else str(loops)
        self.log(f"▶ Bắt đầu loop {a} ↔ {b}, {txt} vòng.")
        self.loop_worker.start()

    def on_loop_stop(self):
        self._sync_client()
        if self.loop_worker and self.loop_worker.isRunning():
            self.loop_worker.stop()
            self.log("⏹ Yêu cầu dừng loop — gửi Cancel để robot dừng ngay...")
        def fn():
            code, data = self.client.cancel_move()
            return code == 200, f"Cancel HTTP {code}: {data}"
        self._run(fn)

    def _on_loop_progress(self, n, total):
        t = "∞" if total <= 0 else str(total)
        self.loop_lbl.setText(f"Vòng {n}/{t}")

    def _on_loop_done(self, ok, msg):
        self.log(msg)
        self.status_lbl.setText(msg if ok else f"⚠ {msg}")
        self.loop_lbl.setText("—")
        self.btn_loop_start.setEnabled(True)
        self.btn_loop_stop.setEnabled(False)
        self.btn_move.setEnabled(True)


def install_gui_excepthook(window):
    """Mọi exception chưa bắt được -> đẩy thẳng ra ô log của GUI."""
    import traceback

    def hook(exc_type, exc, tb):
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        window.log("‼ LỖI (uncaught):\n" + msg)
        window.status_lbl.setText("‼ Có lỗi — xem log bên dưới")
        # vẫn in ra terminal để debug
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    install_gui_excepthook(win)
    win.show()
    # PySide6/PySide2 dùng exec(), PyQt5 cũ dùng exec_()
    run = getattr(app, "exec", None) or app.exec_
    sys.exit(run())


if __name__ == "__main__":
    main()
