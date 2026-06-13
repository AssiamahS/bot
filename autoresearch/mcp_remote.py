#!/usr/bin/env python3
"""Remote MCP server for the funding-harvest bots.

Streamable-HTTP MCP exposed through a cloudflared tunnel so claude.ai
(web + iOS app) can read bot state as a custom connector.

Read-only by design: status, logs, fills, funding. No order placement,
no arbitrary file reads, no shell. Secrets in config.json (private key,
telegram token) are never returned by any tool.

Auth: two layers —
  1. The MCP endpoint lives under a random URL path (SECRET) → anyone
     without the full URL gets 404s.
  2. OAuth 2.0 PKCE flow (required by MCP spec 2025-03-26) implemented
     as a bypass provider: registration and authorization are auto-approved,
     no login screen. The issued bearer token is the second layer.
"""

import json
import re
import secrets as _secrets
import subprocess
import time
from pathlib import Path

import requests
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyHttpUrl

HERE = Path(__file__).resolve().parent
LOG_DIR = HERE / "live_logs"
CONFIG = HERE.parent / "config.json"
API = "https://api.hyperliquid.xyz/info"

# random path segment = URL-based auth token, kept out of git (repo is public).
# Rotate by deleting mcp_secret.txt, restarting, and updating the connector URL.
_SECRET_FILE = HERE / "mcp_secret.txt"
if not _SECRET_FILE.exists():
    _SECRET_FILE.write_text(_secrets.token_urlsafe(24))
SECRET = _SECRET_FILE.read_text().strip()
PORT = 8765

COINS = ["xyz:SILVER", "xyz:MU", "xyz:NVDA", "xyz:AAPL", "xyz:TSLA"]


def _read_tunnel_url(max_wait: int = 60) -> str:
    """Read the cloudflared quick-tunnel URL from tunnel_url.txt, retrying until available.

    hl_tunnel_wrapper.sh writes the URL the moment cloudflared logs it, so this
    file is always a clean single-line value rather than a grepped log.
    """
    url_file = HERE / "tunnel_url.txt"
    for _ in range(max_wait):
        if url_file.exists():
            url = url_file.read_text().strip()
            if url.startswith("https://"):
                return url
        time.sleep(1)
    raise RuntimeError(f"tunnel URL not found in {url_file} after {max_wait}s")


TUNNEL_URL = _read_tunnel_url()


_STATE_FILE = HERE / "mcp_oauth_state.json"


class BypassOAuthProvider:
    """OAuth provider that auto-approves everything.

    Real auth = secret URL path. OAuth satisfies the MCP spec 2025-03-26
    requirement for remote servers without adding a login wall.

    State is persisted to mcp_oauth_state.json so clients survive MCP server
    restarts (which happen on tunnel URL rotation via WatchPaths).
    """

    def __init__(self):
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._codes: dict[str, AuthorizationCode] = {}
        self._access_tokens: dict[str, AccessToken] = {}
        self._refresh_tokens: dict[str, RefreshToken] = {}
        self._load()

    def _load(self) -> None:
        if not _STATE_FILE.exists():
            return
        try:
            raw = json.loads(_STATE_FILE.read_text())
            now = time.time()
            self._clients = {
                k: OAuthClientInformationFull.model_validate(v)
                for k, v in raw.get("clients", {}).items()
            }
            self._access_tokens = {
                k: AccessToken.model_validate(v)
                for k, v in raw.get("access_tokens", {}).items()
                if v.get("expires_at", now + 1) > now
            }
            self._refresh_tokens = {
                k: RefreshToken.model_validate(v)
                for k, v in raw.get("refresh_tokens", {}).items()
                if v.get("expires_at", now + 1) > now
            }
        except Exception:
            pass

    def _save(self) -> None:
        try:
            _STATE_FILE.write_text(json.dumps({
                "clients": {k: v.model_dump(mode="json") for k, v in self._clients.items()},
                "access_tokens": {k: v.model_dump(mode="json") for k, v in self._access_tokens.items()},
                "refresh_tokens": {k: v.model_dump(mode="json") for k, v in self._refresh_tokens.items()},
            }, indent=2))
        except Exception:
            pass

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info
        self._save()

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        code = _secrets.token_urlsafe(32)
        self._codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + 300,
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if code and code.client_id == client.client_id:
            return code
        return None

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)
        access = _secrets.token_urlsafe(32)
        refresh = _secrets.token_urlsafe(32)
        expires = int(time.time()) + 3600
        self._access_tokens[access] = AccessToken(
            token=access, client_id=client.client_id,
            scopes=authorization_code.scopes, expires_at=expires,
        )
        self._refresh_tokens[refresh] = RefreshToken(
            token=refresh, client_id=client.client_id,
            scopes=authorization_code.scopes, expires_at=expires + 86400,
        )
        self._save()
        return OAuthToken(access_token=access, token_type="bearer",
                          expires_in=3600, refresh_token=refresh)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        rt = self._refresh_tokens.get(refresh_token)
        if rt and rt.client_id == client.client_id:
            return rt
        return None

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        self._refresh_tokens.pop(refresh_token.token, None)
        access = _secrets.token_urlsafe(32)
        new_refresh = _secrets.token_urlsafe(32)
        expires = int(time.time()) + 3600
        self._access_tokens[access] = AccessToken(
            token=access, client_id=client.client_id,
            scopes=refresh_token.scopes, expires_at=expires,
        )
        self._refresh_tokens[new_refresh] = RefreshToken(
            token=new_refresh, client_id=client.client_id,
            scopes=refresh_token.scopes, expires_at=expires + 86400,
        )
        self._save()
        return OAuthToken(access_token=access, token_type="bearer",
                          expires_in=3600, refresh_token=new_refresh)

    async def load_access_token(self, token: str) -> AccessToken | None:
        t = self._access_tokens.get(token)
        if t and (t.expires_at is None or t.expires_at > time.time()):
            return t
        return None

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self._access_tokens.pop(token.token, None)
        self._refresh_tokens.pop(token.token, None)
        self._save()


_oauth = BypassOAuthProvider()

# streamable_http_path is relative ("/mcp") — the outer Starlette mount at
# /{SECRET} promotes every route to /{SECRET}/mcp, /{SECRET}/.well-known/...,
# /{SECRET}/register, etc., keeping them all away from the Cloudflare-blocked
# root /.well-known/ paths.
mcp = FastMCP(
    "hl-bot-remote",
    host="127.0.0.1",
    port=PORT,
    streamable_http_path="/mcp",
    # quick-tunnel hostname rotates; rebinding check off, secret path is the gate
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    auth_server_provider=_oauth,
    auth=AuthSettings(
        # issuer has a path component so /.well-known/oauth-authorization-server
        # lands at /{SECRET}/.well-known/... not at root
        issuer_url=AnyHttpUrl(f"{TUNNEL_URL}/{SECRET}"),
        # None = skip creating the RFC 9728 protected-resource metadata endpoint,
        # which would otherwise be placed at root /.well-known/ (also blocked)
        resource_server_url=None,
        client_registration_options=ClientRegistrationOptions(enabled=True),
    ),
)


def wallet_address() -> str:
    return json.loads(CONFIG.read_text())["wallet_address"]


def post(payload: dict) -> dict | list:
    r = requests.post(API, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def slug(coin: str) -> str:
    return coin.replace(":", "_")


@mcp.tool()
def bot_status() -> str:
    """Live status: which bot processes are running, equity, and open
    positions on the xyz dex (TSLA/NVDA/AAPL/MU/SILVER funding bots)."""
    out = {}

    procs = subprocess.run(
        ["pgrep", "-afl", "live_funding.py"], capture_output=True, text=True
    ).stdout
    running = re.findall(r"--coin (\S+) --usd (\S+)", procs)
    out["bots_running"] = [{"coin": c, "usd": u} for c, u in running]
    out["bots_expected"] = COINS

    addr = wallet_address()
    spot = post({"type": "spotClearinghouseState", "user": addr})
    usdc_free = next(
        (float(b["total"]) - float(b["hold"])
         for b in spot.get("balances", []) if b["coin"] == "USDC"),
        0.0,
    )
    ch = post({"type": "clearinghouseState", "user": addr, "dex": "xyz"})
    out["equity"] = {
        "spot_usdc_free": round(usdc_free, 2),
        "xyz_perp_account_value": round(float(ch["marginSummary"]["accountValue"]), 2),
    }
    out["positions"] = [
        {
            "coin": p["position"]["coin"],
            "size": p["position"]["szi"],
            "entry": p["position"]["entryPx"],
            "unrealized_pnl": p["position"]["unrealizedPnl"],
            "funding_paid": p["position"]["cumFunding"]["sinceOpen"],
        }
        for p in ch.get("assetPositions", [])
    ]
    return json.dumps(out, indent=2)


@mcp.tool()
def funding_rates(top_n: int = 10) -> str:
    """Current hourly funding (and APY) for the bot's markets, plus the
    top |APY| markets across the whole xyz HIP-3 dex."""
    meta, ctxs = post({"type": "metaAndAssetCtxs", "dex": "xyz"})
    rows = []
    for a, c in zip(meta["universe"], ctxs):
        try:
            fr = float(c["funding"])
        except (KeyError, TypeError, ValueError):
            continue
        rows.append({
            "coin": a["name"],
            "mid": c.get("midPx"),
            "funding_hr_pct": round(fr * 100, 5),
            "apy_pct": round(fr * 24 * 365 * 100, 1),
            "max_leverage": a.get("maxLeverage"),
        })
    ours = [r for r in rows if r["coin"] in COINS]
    hot = sorted(rows, key=lambda r: -abs(r["apy_pct"]))[:top_n]
    return json.dumps({"bot_markets": ours, "top_funding_xyz": hot}, indent=2)


@mcp.tool()
def recent_fills(hours: int = 24) -> str:
    """Trade fills on the bot wallet over the last N hours, with per-coin
    counts and net closed PnL."""
    addr = wallet_address()
    since = int((time.time() - hours * 3600) * 1000)
    fills = post({"type": "userFillsByTime", "user": addr, "startTime": since})
    summary: dict[str, dict] = {}
    for f in fills:
        s = summary.setdefault(f["coin"], {"fills": 0, "closed_pnl": 0.0, "fees": 0.0})
        s["fills"] += 1
        s["closed_pnl"] += float(f.get("closedPnl", 0))
        s["fees"] += float(f.get("fee", 0))
    for s in summary.values():
        s["closed_pnl"] = round(s["closed_pnl"], 4)
        s["fees"] = round(s["fees"], 4)
    recent = [
        {
            "time": time.strftime("%m-%d %H:%M", time.gmtime(f["time"] / 1000)),
            "coin": f["coin"],
            "side": f["side"],
            "px": f["px"],
            "sz": f["sz"],
            "closed_pnl": f.get("closedPnl"),
        }
        for f in fills[-30:]
    ]
    return json.dumps(
        {"hours": hours, "total_fills": len(fills), "by_coin": summary,
         "last_30_fills": recent},
        indent=2,
    )


@mcp.tool()
def tail_log(coin: str = "xyz:SILVER", lines: int = 40) -> str:
    """Tail the runner log for one bot. coin must be one of the bot's
    markets (xyz:SILVER, xyz:MU, xyz:NVDA, xyz:AAPL, xyz:TSLA)."""
    if coin not in COINS:
        return f"unknown coin {coin!r}; valid: {COINS}"
    path = LOG_DIR / f"{slug(coin)}_runner.log"
    if not path.exists():
        return f"no log at {path}"
    lines = max(1, min(int(lines), 400))
    text = path.read_text(errors="replace").splitlines()
    return "\n".join(text[-lines:])


@mcp.tool()
def perf_journal(coin: str = "", events: int = 60) -> str:
    """Recent structured bot events (opens/closes/quotes/errors) from the
    newest jsonl journal. Empty coin = all five markets interleaved."""
    coins = [coin] if coin in COINS else COINS
    rows = []
    for c in coins:
        files = sorted(LOG_DIR.glob(f"{slug(c)}_2*.jsonl"))
        if not files:
            continue
        for line in files[-1].read_text(errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    rows.sort(key=lambda r: r.get("ts") or r.get("time") or "")
    events = max(1, min(int(events), 300))
    return json.dumps(rows[-events:], indent=2)


if __name__ == "__main__":
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Mount

    # Wrap the inner FastMCP app under /{SECRET} so every route — MCP endpoint,
    # OAuth metadata, register, authorize, token — is served under the secret
    # prefix and never at root /.well-known/ (blocked by Cloudflare quick tunnels).
    inner = mcp.streamable_http_app()
    app = Starlette(routes=[Mount(f"/{SECRET}", app=inner)])
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")
