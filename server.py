#!/usr/bin/env python3
"""
Dashboard server for Hyperliquid multi-bot setup.
Serves dashboard.html and status JSON from each bot folder.
"""
import http.server
import json
import os

PORT = 8082
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

STATUS_MAP = {
    "/status/sol.json": os.path.expanduser("~/hyperliquid-sol/trader_status.json"),
    "/status/btc.json": os.path.expanduser("~/hyperliquid-btc/trader_status.json"),
    "/status/eth.json": os.path.expanduser("~/hyperliquid-eth/trader_status.json"),
    "/status/all.json": os.path.expanduser("~/hyperliquid/trader_status.json"),
    # Legacy path for backwards compat
    "/trader_status.json": os.path.expanduser("~/hyperliquid/trader_status.json"),
}


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=BASE_DIR, **kwargs)

    def do_GET(self):
        path = self.path.split("?")[0]

        # Serve status JSON files from bot folders
        if path in STATUS_MAP:
            status_path = STATUS_MAP[path]
            try:
                with open(status_path) as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(data.encode())
            except FileNotFoundError:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"not found"}')
            return

        # Serve dashboard at root
        if path == "/" or path == "/dashboard" or path == "/dashboard.html":
            self.path = "/dashboard.html"

        super().do_GET()

    def log_message(self, format, *args):
        pass  # quiet


if __name__ == "__main__":
    print(f"Dashboard server on http://localhost:{PORT}")
    print(f"Serving status from: sol, btc, eth bot folders")
    server = http.server.HTTPServer(("", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped")
