#!/usr/bin/env python3
"""
Robot GIẢ để test GUI/heartbeat mà không cần robot thật.
Chạy:  python3 mock_robot.py
Rồi trong GUI, ô "Robot IP" gõ:  http://127.0.0.1:8099
(dùng http:// để khỏi cần SSL cert) -> đèn sẽ chuyển XANH.
"""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8099
_t0 = time.time()


def state_payload():
    # giả lập velocity nhích nhẹ cho giống robot đang sống
    t = time.time() - _t0
    moving = (int(t) % 10) < 5   # 5s chạy, 5s đứng
    return {
        "data": {
            "information": [{
                "infoType": "order",
                "infoDescription": "[order] Finished Order",
                "infoReferences": [
                    {"referenceKey": "order_id", "referenceValue": "ORD-MOCK-001"}
                ],
            }],
            "nodeStates": [{"nodeId": "Node5"}] if moving else [],
            "edgeStates": [],
            "velocity": {"vx": 0.30 if moving else 0.0, "vy": 0.0,
                         "omega": 0.05 if moving else 0.0},
            "safetyState": {"eStop": "NONE"},
        }
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if "/State/" in self.path:
            self._send(state_payload())
        else:
            self._send({"code": 0, "message": "ok"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        print(f"[MOCK] POST {self.path}")
        self._send({"code": 0, "message": "success",
                    "data": {"orderId": "ORD-MOCK-001"}})

    def do_DELETE(self):
        print(f"[MOCK] DELETE {self.path}")
        self._send({"code": 0, "message": "cancelled"})

    def log_message(self, *args):
        pass  # tắt log rác của http.server


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[MOCK] Robot giả chạy tại http://127.0.0.1:{PORT}")
    print("       Trong GUI gõ IP:  http://127.0.0.1:8099")
    print("       Ctrl+C để dừng.")
    srv.serve_forever()
