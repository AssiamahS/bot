#!/usr/bin/env python3
"""Telegram alerts using tg_token + tg_chat_id from config.json."""

import json
from pathlib import Path
from urllib import request
from urllib.parse import quote


_cfg = None


def _load_cfg():
    global _cfg
    if _cfg is None:
        _cfg = json.loads((Path.home() / "hyperliquid-sol" / "config.json").read_text())
    return _cfg


def send(msg: str, silent: bool = False) -> bool:
    cfg = _load_cfg()
    token = cfg.get("tg_token")
    chat = cfg.get("tg_chat_id")
    if not token or not chat or token == "ENV":
        print(f"[notify-skipped] {msg}")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = json.dumps({
        "chat_id": chat, "text": msg,
        "parse_mode": "Markdown", "disable_notification": silent,
    }).encode()
    req = request.Request(url, data=body, headers={"Content-Type": "application/json"})
    try:
        with request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        print(f"[notify-fail] {e}: {msg}")
        return False


if __name__ == "__main__":
    import sys
    msg = " ".join(sys.argv[1:]) or "kim · notify wire test"
    ok = send(msg)
    print("sent" if ok else "failed (check tg_token + tg_chat_id in config.json)")
