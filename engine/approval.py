"""Human approval before entries. Exits never wait.

The bot proposes; you decide. Each pending trade is pushed to your phone and
listed on a small local page you tap through.

Two rules built in rather than configurable:

  EXITS ARE NEVER GATED. A stop that waits for a tap is not a stop. Only
  entries go through here.

  A TIMEOUT ALWAYS RESOLVES. If you do not answer, the request expires on
  its own — default reject. A queue that grows while you are asleep would
  fire a dozen stale orders the moment you looked at your phone.

The approve/reject log is the point as much as the safety is. Every decision
is a labelled example of your judgment on a real setup, recorded next to the
features that produced it. That is the dataset that could eventually encode
what you actually do — and it is not obtainable any other way.
"""

from __future__ import annotations

import html
import json
import pathlib
import socket
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DECISION_LOG = pathlib.Path("data/mosquito/approvals.jsonl")


@dataclass
class Request:
    id: str
    symbol: str
    setup: str
    price: float
    shares: int
    stop: float
    target: float
    reason: str
    created_at: float = field(default_factory=time.time)
    features: dict = field(default_factory=dict)

    approved: bool | None = None
    decided_at: float | None = None

    @property
    def age(self) -> float:
        return time.time() - self.created_at

    @property
    def notional(self) -> float:
        return self.shares * self.price


class ApprovalQueue:
    """Pending entries, plus the tiny web server you tap through."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.timeout = cfg.get("timeout_seconds", 120)
        self.on_timeout_approve = cfg.get("on_timeout", "reject") == "approve"
        self.pending: dict[str, Request] = {}
        self.lock = threading.Lock()
        self.server = None
        self._counter = 0

    # ------------------------------------------------------------- queueing

    def submit(self, **kwargs) -> Request:
        with self.lock:
            self._counter += 1
            request = Request(id=f"r{self._counter}", **kwargs)
            self.pending[request.id] = request
        return request

    def decide(self, request_id: str, approved: bool) -> bool:
        with self.lock:
            request = self.pending.get(request_id)
            if request is None or request.approved is not None:
                return False
            request.approved = approved
            request.decided_at = time.time()
        self._log(request, "manual")
        return True

    def resolve(self, request: Request) -> bool:
        """Has this been answered? Applies the timeout if not.

        Non-blocking: the caller polls. Blocking here would stall the alert
        handler and back up the whole feed behind one undecided trade.
        """
        if request.approved is not None:
            return request.approved
        if request.age >= self.timeout:
            request.approved = self.on_timeout_approve
            request.decided_at = time.time()
            self._log(request, "timeout")
        return bool(request.approved)

    def is_settled(self, request: Request) -> bool:
        return request.approved is not None or request.age >= self.timeout

    def cleanup(self) -> None:
        with self.lock:
            self.pending = {
                rid: r for rid, r in self.pending.items()
                if r.approved is None and r.age < self.timeout * 3
            }

    def _log(self, request: Request, how: str) -> None:
        """Every decision, with the features that prompted it. This file is
        the record of your judgment on real setups."""
        DECISION_LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "at": datetime.now(ET).isoformat(),
            "symbol": request.symbol,
            "setup": request.setup,
            "price": request.price,
            "shares": request.shares,
            "stop": request.stop,
            "target": request.target,
            "reason": request.reason,
            "approved": request.approved,
            "resolved_by": how,
            "seconds_to_decide": round(request.age, 1),
            **request.features,
        }
        with DECISION_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")

    # ------------------------------------------------------------ web server

    def start_server(self) -> str:
        port = self.cfg.get("port", 8765)
        queue = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):        # silence request logging
                pass

            def do_GET(self):
                parsed = urlparse(self.path)
                params = parse_qs(parsed.query)

                if parsed.path in ("/approve", "/reject"):
                    request_id = params.get("id", [""])[0]
                    queue.decide(request_id, parsed.path == "/approve")
                    self.send_response(303)
                    self.send_header("Location", "/")
                    self.end_headers()
                    return

                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(queue._page().encode("utf-8"))

        self.server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

        return f"http://{_lan_ip()}:{port}"

    def _page(self) -> str:
        with self.lock:
            waiting = [r for r in self.pending.values()
                       if r.approved is None and r.age < self.timeout]

        if not waiting:
            body = "<p class='none'>Nothing waiting.</p>"
        else:
            cards = []
            for r in sorted(waiting, key=lambda x: x.created_at):
                left = max(0, int(self.timeout - r.age))
                cards.append(f"""
                <div class="card">
                  <div class="head">
                    <span class="sym">{html.escape(r.symbol)}</span>
                    <span class="left">{left}s left</span>
                  </div>
                  <div class="why">{html.escape(r.reason)}</div>
                  <div class="nums">
                    {r.shares:,} sh @ ${r.price:.2f} &middot; ${r.notional:,.0f}<br>
                    stop {r.stop:.2f} &middot; target {r.target:.2f}
                  </div>
                  <div class="btns">
                    <a class="no" href="/reject?id={r.id}">Skip</a>
                    <a class="yes" href="/approve?id={r.id}">Buy</a>
                  </div>
                </div>""")
            body = "".join(cards)

        return f"""<!DOCTYPE html><html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="3">
<title>Approvals</title>
<style>
 body{{font:16px -apple-system,system-ui,sans-serif;background:#0f1115;
      color:#e6e8eb;margin:0;padding:16px}}
 h1{{font-size:15px;color:#8b93a1;font-weight:500;margin:0 0 16px}}
 .none{{color:#5a6270;text-align:center;padding:48px 0}}
 .card{{background:#181b21;border:1px solid #272b33;border-radius:12px;
        padding:14px;margin-bottom:12px}}
 .head{{display:flex;justify-content:space-between;align-items:baseline}}
 .sym{{font-size:22px;font-weight:600;letter-spacing:.5px}}
 .left{{font-size:13px;color:#e0a458}}
 .why{{color:#8b93a1;font-size:13px;margin:6px 0 10px}}
 .nums{{font-size:14px;line-height:1.5;margin-bottom:14px}}
 .btns{{display:flex;gap:10px}}
 .btns a{{flex:1;text-align:center;padding:14px;border-radius:10px;
          text-decoration:none;font-weight:600}}
 .yes{{background:#2f9e5e;color:#fff}}
 .no{{background:#272b33;color:#c3c9d3}}
</style></head><body>
<h1>Pending approvals &middot; {datetime.now(ET):%H:%M:%S}</h1>
{body}
</body></html>"""


def _lan_ip() -> str:
    """The address the phone can reach, not 127.0.0.1."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:                                     # noqa: BLE001
        return "localhost"
