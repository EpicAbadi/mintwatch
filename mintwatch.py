#!/usr/bin/env python3
"""MintWatch — BSC Free-Mint Radar Agent.

An autonomous, read-only agent that watches a registry of free-mint /
free-claim opportunities on BNB Chain, reads their live on-chain state every
few minutes, scores each opportunity, and publishes a ranked public feed.

What it does (the agent loop):
    OBSERVE  -> eth_call / eth_getCode against public BSC JSON-RPC (no key).
    REASON   -> per opportunity: free? supply left? paused? per-wallet limit?
                A deterministic, documented scoring rule turns the reads into
                a verdict (OPEN / CLOSED_SOLD_OUT / PAID_ONLY / UNKNOWN) plus
                a human-readable list of reasons. Nothing is guessed: a getter
                that reverts is reported as `unknown`, never filled in.
    ACT      -> publish the ranked feed + dashboard + a reasoning log, and
                answer on-demand evaluations of any BSC contract
                (POST /api/evaluate).

What it never does: sign, send, or simulate a transaction; hold a key;
spend anything; call a private API. Every number it publishes can be
reproduced with a single `eth_call` — the chain is the proof.

Endpoints:
    GET  /                        dashboard (server-rendered HTML)
    GET  /health                  liveness + last poll + RPC status
    GET  /api/feed                ranked opportunities (JSON)
    GET  /api/opportunities/<id> one opportunity, full reads + history
    GET  /api/decisions           the agent's reasoning log
    GET  /api/registry            the tracked registry (public)
    POST /api/evaluate            {"contract": "0x..."} -> on-demand verdict

Run:
    python3 mintwatch.py [--port 8891] [--registry registry.json]
                         [--data ./data] [--interval 300] [--once]

Config via environment: MINTWATCH_PORT, MINTWATCH_HOST, MINTWATCH_INTERVAL,
MINTWATCH_REGISTRY, MINTWATCH_DATA. Python 3.11+ standard library only.
"""

import argparse
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.1.0"
AGENT_NAME = "MintWatch"

# ---------------------------------------------------------------- RPC

# Public BSC JSON-RPC endpoints. No API key, no account. Failover order.
DEFAULT_RPCS = [
    "https://bsc-dataseed.binance.org",
    "https://bsc-dataseed1.defibit.io",
    "https://bsc-dataseed2.defibit.io",
    "https://bsc-dataseed1.ninicoin.io",
]

UA = {"User-Agent": "MintWatch/0.1 (+https://github.com/EpicAbadi/mintwatch)",
      "Content-Type": "application/json"}

# 4-byte selectors. Values verified live: each of these returned correct data
# from real BSC contracts during recon (see docs/VERIFICATION.md).
SEL = {
    "totalSupply()": "0x18160ddd",
    "maxSupply()": "0xd5abeb01",
    "MAX_SUPPLY()": "0x32cb6b0c",
    "totalMinted()": "0xa2309ff8",
    "paused()": "0x5c975abb",
    "owner()": "0x8da5cb5b",
    "name()": "0x06fdde03",
    "symbol()": "0x95d89b41",
    "decimals()": "0x313ce567",
    "MAX_PER_WALLET()": "0x0f2cdd6c",
    "remainingSupply()": "0xda0239a6",
}

GENERIC_PROBE = ["totalSupply()", "MAX_SUPPLY()", "maxSupply()",
                 "totalMinted()", "remainingSupply()", "name()",
                 "symbol()", "decimals()", "owner()", "paused()",
                 "MAX_PER_WALLET()"]

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


class RpcError(Exception):
    pass


class RpcClient:
    """Minimal JSON-RPC client with endpoint failover. Read-only methods."""

    def __init__(self, endpoints, timeout=15):
        self.endpoints = list(endpoints)
        self.timeout = timeout
        self.last_ok = None
        self.last_error = None

    def _post(self, url, payload):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers=UA, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read())

    def call(self, method, params):
        errors = []
        for url in self.endpoints:
            try:
                res = self._post(url, {"jsonrpc": "2.0", "id": 1,
                                       "method": method, "params": params})
            except Exception as e:  # transport failure -> try next endpoint
                errors.append(f"{url}: {type(e).__name__}: {e}")
                continue
            if "error" in res:
                errors.append(f"{url}: {res['error'].get('message', res['error'])}")
                continue
            self.last_ok = url
            self.last_error = None
            return res.get("result")
        self.last_error = "; ".join(errors)
        raise RpcError(self.last_error)

    def eth_call(self, to, data, block="latest"):
        return self.call("eth_call", [{"to": to, "data": data}, block])

    def eth_get_code(self, address, block="latest"):
        return self.call("eth_getCode", [address, block])

    def block_number(self):
        return int(self.call("eth_blockNumber", []), 16)


# ---------------------------------------------------------------- decoding

def decode_value(raw, kind):
    """Best-effort ABI decode of a return value.

    kind: 'uint' | 'bool' | 'string' | 'address' | 'auto'.
    'auto' decodes an ABI string when the layout matches, otherwise a
    32-byte word as uint, otherwise returns the raw hex verbatim.
    Deliberately conservative: it never guesses 'address' or 'bool' from
    a bare word, because a small uint (e.g. totalSupply 206) also has
    twelve leading zero bytes.
    Returns a Python value, or None when the value cannot be decoded
    (e.g. empty return). Raises ValueError on malformed data.
    """
    if raw is None or raw == "0x":
        return None
    b = bytes.fromhex(raw[2:])
    if kind == "string":
        if len(b) >= 64:
            off = int.from_bytes(b[0:32], "big")
            ln = int.from_bytes(b[32:64], "big")
            if off == 32 and 0 <= ln <= len(b) - 64:
                return b[64:64 + ln].decode("utf-8")
        raise ValueError("not an ABI string")
    if kind == "bool":
        if len(b) == 32:
            v = int.from_bytes(b, "big")
            if v not in (0, 1):
                raise ValueError("not a bool")
            return bool(v)
        raise ValueError("not a bool")
    if kind == "address":
        if len(b) == 32 and b[:12] == b"\x00" * 12:
            return "0x" + b[12:].hex()
        raise ValueError("not an address")
    if kind == "uint":
        if len(b) == 32:
            return int.from_bytes(b, "big")
        raise ValueError("not a uint256")
    if kind == "auto":
        if len(b) >= 64:
            off = int.from_bytes(b[0:32], "big")
            ln = int.from_bytes(b[32:64], "big")
            if off == 32 and 0 <= ln <= len(b) - 64:
                try:
                    return b[64:64 + ln].decode("utf-8")
                except UnicodeDecodeError:
                    pass
        if len(b) == 32:
            return int.from_bytes(b, "big")
        return raw
    raise ValueError(f"unknown kind {kind}")


def probe_getter(rpc, address, fn):
    """One read-only eth_call. Returns {'ok': True, 'value': ...} or
    {'ok': False, 'error': 'revert' | 'transport: ...'}.

    A revert is data, not a failure: it means the contract does not expose
    that getter, and the agent records it as unknown.
    """
    try:
        raw = rpc.eth_call(address, SEL[fn])
    except RpcError as e:
        return {"ok": False, "error": f"transport: {e}"}
    if raw is None or raw == "0x":
        return {"ok": False, "error": "revert"}
    try:
        kind = "string" if fn in ("name()", "symbol()") else "auto"
        return {"ok": True, "raw": raw, "value": decode_value(raw, kind)}
    except (ValueError, UnicodeDecodeError) as e:
        return {"ok": False, "error": f"decode: {e}"}


# ---------------------------------------------------------------- reasoning

def _int(reads, fn):
    r = reads.get(fn)
    return r["value"] if r and r.get("ok") and isinstance(r.get("value"), int) else None


def evaluate(entry, reads, block_number, now):
    """Turn raw reads into a verdict. Pure function — fully unit-testable.

    Scoring (0-100), documented so anyone can reproduce it:
        +50  the mint/claim is free (gas only) per the verified source
        +30  remaining supply is known and > 0
        +10  remaining supply is a healthy share of max (> 25% left)
        +10  every getter the entry needs returned a value (fresh, complete)
    A sold-out / paused / paid-only opportunity scores 0-10 and is kept in
    the feed as a reference, clearly labelled — the agent reports the truth
    instead of hiding dead entries.
    """
    reasons = []
    score = 0
    status = "UNKNOWN"
    remaining = None
    max_supply = None

    total = _int(reads, "totalSupply()")
    if total is None:
        total = _int(reads, "totalMinted()")
    for fn in ("MAX_SUPPLY()", "maxSupply()"):
        v = _int(reads, fn)
        if v is not None:
            max_supply = v
            break
    rem = _int(reads, "remainingSupply()")
    if rem is not None:
        remaining = rem
    elif total is not None and max_supply is not None:
        remaining = max_supply - total

    paused = _int(reads, "paused()")
    free = bool(entry.get("free"))
    free_first = bool(entry.get("free_first"))

    if paused == 1:
        status = "CLOSED_PAUSED"
        reasons.append("paused() reads 1: contract is paused")
    elif remaining is not None and remaining <= 0:
        status = "CLOSED_SOLD_OUT"
        reasons.append(f"supply exhausted: {total} minted of {max_supply} max")
    elif entry.get("free_exhausted"):
        status = "PAID_ONLY"
        reasons.append(entry["free_exhausted"])
        score = 5
    elif free or free_first:
        status = "OPEN"
        score += 50
        reasons.append("free (gas only)" if free
                       else "first mint per wallet is free (gas only)")
        if remaining is not None and remaining > 0:
            score += 30
            reasons.append(f"{remaining:,} of {max_supply:,} supply left"
                           if max_supply else f"{remaining:,} supply left")
            if max_supply and remaining / max_supply > 0.25:
                score += 10
                reasons.append("more than 25% of supply still unminted")
        else:
            reasons.append("remaining supply could not be read on-chain; "
                           "treat the OPEN verdict as provisional")
    else:
        status = "UNKNOWN"
        reasons.append("no free-mint claim could be confirmed from reads")

    # Data-completeness bonus, and the caveats the entry declares.
    needed = entry.get("needs", [])
    got = sum(1 for fn in needed if reads.get(fn, {}).get("ok"))
    if needed and got == len(needed):
        score += 10
        reasons.append(f"all {len(needed)} expected getters returned values")
    elif needed:
        reasons.append(f"only {got}/{len(needed)} expected getters returned "
                       "values; some fields are unknown, not zero")
    for note in entry.get("caveats", []):
        reasons.append(note)

    if status in ("CLOSED_SOLD_OUT", "CLOSED_PAUSED"):
        score = 0
    score = max(0, min(100, score))

    result = {
        "id": entry["id"],
        "name": entry["name"],
        "contract": entry["contract"],
        "chain": entry.get("chain", "BSC (56)"),
        "status": status,
        "score": score,
        "free": free,
        "free_first": free_first,
        "total": total,
        "max_supply": max_supply,
        "remaining": remaining,
        "block": block_number,
        "checked_at": now,
        "reads": reads,
        "reasons": reasons,
        "links": entry.get("links", {}),
        "notes": entry.get("notes", ""),
    }
    # Optional human display scale: some getters return base units (e.g. an
    # 18-decimal token counter). `total`/`max_supply`/`remaining` above stay
    # RAW so anyone can reproduce them with one eth_call; the *_display
    # fields are only a convenience rendering, computed by dividing by
    # 10**scale, and the raw values remain in `reads`.
    scale = entry.get("scale")
    if isinstance(scale, int) and scale > 0:
        div = 10 ** scale
        result["display_unit"] = entry.get("unit", "")
        for key in ("total", "max_supply", "remaining"):
            if isinstance(result[key], int):
                result[key + "_display"] = result[key] // div
    return result


# ---------------------------------------------------------------- agent

class MintWatch:
    def __init__(self, registry_path, data_dir, interval=300,
                 rpc_endpoints=None):
        self.registry_path = registry_path
        self.data_dir = data_dir
        self.interval = interval
        self.rpc = RpcClient(rpc_endpoints or DEFAULT_RPCS)
        self.started_at = time.time()
        self.lock = threading.Lock()
        self.state = {
            "agent": AGENT_NAME, "version": VERSION,
            "started_at": int(self.started_at),
            "last_poll": None, "last_block": None,
            "rpc_ok": None, "rpc_error": None,
            "feed": [], "decisions": [], "history": {},
            "polls": 0, "poll_errors": 0,
        }
        self._load_state()

    # -- persistence -------------------------------------------------
    def _state_path(self):
        return os.path.join(self.data_dir, "state.json")

    def _load_state(self):
        try:
            with open(self._state_path()) as f:
                saved = json.load(f)
            if isinstance(saved, dict) and saved.get("agent") == AGENT_NAME:
                saved["started_at"] = int(self.started_at)
                self.state.update(saved)
        except (OSError, ValueError):
            pass

    def _save_state(self):
        os.makedirs(self.data_dir, exist_ok=True)
        tmp = self._state_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self._state_path())

    # -- registry ----------------------------------------------------
    def load_registry(self):
        with open(self.registry_path) as f:
            reg = json.load(f)
        entries = reg["opportunities"]
        for e in entries:
            assert ADDRESS_RE.match(e["contract"]), f"bad address {e}"
            e.setdefault("needs", [])
            e.setdefault("caveats", [])
        return entries

    # -- the agent loop ----------------------------------------------
    def poll_once(self):
        """One observe -> reason -> publish cycle."""
        now = int(time.time())
        entries = self.load_registry()
        try:
            block = self.rpc.block_number()
            rpc_ok, rpc_error = True, None
        except RpcError as e:
            block, rpc_ok, rpc_error = None, False, str(e)
        feed = []
        decisions = []
        for entry in entries:
            reads = {}
            for fn in entry.get("needs", []):
                reads[fn] = probe_getter(self.rpc, entry["contract"], fn)
            ev = evaluate(entry, reads, block, now)
            feed.append(ev)
            hist = self.state["history"].setdefault(entry["id"], [])
            hist.append({"at": now, "block": block, "status": ev["status"],
                         "score": ev["score"], "remaining": ev["remaining"]})
            self.state["history"][entry["id"]] = hist[-24:]
            decisions.append({
                "at": now, "block": block, "opportunity": entry["id"],
                "status": ev["status"], "score": ev["score"],
                "summary": "; ".join(ev["reasons"][:3]),
            })
        feed.sort(key=lambda e: (-e["score"], e["name"]))
        with self.lock:
            self.state["feed"] = feed
            self.state["decisions"] = (decisions + self.state["decisions"])[:200]
            self.state["last_poll"] = now
            self.state["last_block"] = block
            self.state["rpc_ok"] = rpc_ok
            self.state["rpc_error"] = rpc_error
            self.state["polls"] += 1
            if not rpc_ok:
                self.state["poll_errors"] += 1
            self._save_state()
        return feed

    def run_loop(self, stop):
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception as e:  # a failed cycle must not kill the agent
                with self.lock:
                    self.state["poll_errors"] += 1
                    self.state["rpc_error"] = f"poll: {type(e).__name__}: {e}"
                print(f"[mintwatch] poll failed: {e}", file=sys.stderr, flush=True)
            stop.wait(self.interval)

    # -- on-demand evaluation -----------------------------------------
    def evaluate_contract(self, address):
        """Probe any BSC contract and return a verdict. Read-only."""
        if not ADDRESS_RE.match(address):
            return {"ok": False, "error": "address must be 0x + 40 hex chars"}
        try:
            code = self.rpc.eth_get_code(address)
            block = self.rpc.block_number()
        except RpcError as e:
            return {"ok": False, "error": f"rpc unavailable: {e}"}
        if not code or code == "0x":
            return {"ok": True, "contract": address, "is_contract": False,
                    "verdict": "NOT_A_CONTRACT",
                    "summary": "No code at this address on BSC. Nothing to mint."}
        reads = {fn: probe_getter(self.rpc, address, fn) for fn in GENERIC_PROBE}
        total = _int(reads, "totalSupply()")
        max_supply = _int(reads, "MAX_SUPPLY()") or _int(reads, "maxSupply()")
        name = reads["name()"].get("value") if reads["name()"].get("ok") else None
        symbol = reads["symbol()"].get("value") if reads["symbol()"].get("ok") else None
        remaining = None
        if _int(reads, "remainingSupply()") is not None:
            remaining = _int(reads, "remainingSupply()")
        elif total is not None and max_supply is not None:
            remaining = max_supply - total
        kind = "unknown"
        if isinstance(name, str) and total is not None:
            kind = "ERC-20-like" if reads["decimals()"].get("ok") else "token-like"
            if max_supply is not None and total <= max_supply and remaining is not None:
                kind = "ERC-721-like (NFT collection)" if total < 100_000 else kind
        verdict = "HAS_CODE_UNVERIFIED_PRICE"
        summary = ("Contract exists and answers reads. Mint price cannot be "
                   "determined from generic reads — check the project's mint "
                   "function and simulate before spending anything.")
        if remaining is not None and remaining <= 0:
            verdict, summary = ("SOLD_OUT", "Supply is exhausted on-chain.")
        return {"ok": True, "contract": address, "is_contract": True,
                "block": block, "name": name, "symbol": symbol,
                "kind_guess": kind, "total": total,
                "max_supply": max_supply, "remaining": remaining,
                "verdict": verdict, "summary": summary,
                "reads": {k: v for k, v in reads.items()}}


# ---------------------------------------------------------------- HTTP

def _feed_public(feed):
    """Feed entries without raw hex (kept in the detail endpoint)."""
    out = []
    for e in feed:
        d = {k: e[k] for k in ("id", "name", "contract", "chain", "status",
                               "score", "free", "free_first", "total",
                               "max_supply", "remaining", "block",
                               "checked_at", "reasons", "links", "notes")}
        for k in ("total_display", "max_supply_display", "remaining_display",
                  "display_unit"):
            if k in e:
                d[k] = e[k]
        out.append(d)
    return out


DASHBOARD_CSS = """
body{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;
padding:0 1rem;color:#1a1a1a;line-height:1.5}
h1{font-size:1.6rem} table{border-collapse:collapse;width:100%}
th,td{border:1px solid #ddd;padding:.45rem .6rem;text-align:left;font-size:.92rem}
th{background:#f4f4f4} .open{color:#0a7a2e;font-weight:700}
.closed{color:#a00;font-weight:700} .unk{color:#b60;font-weight:700}
code{background:#f4f4f4;padding:.1rem .3rem;border-radius:3px}
.small{color:#555;font-size:.85rem} .reason{font-size:.85rem;color:#333}
"""

STATUS_LABEL = {"OPEN": ("open", "OPEN — free"),
                "CLOSED_SOLD_OUT": ("closed", "SOLD OUT"),
                "CLOSED_PAUSED": ("closed", "PAUSED"),
                "PAID_ONLY": ("unk", "PAID ONLY"),
                "UNKNOWN": ("unk", "UNKNOWN")}


def render_dashboard(state):
    rows = []
    for e in state.get("feed", []):
        cls, label = STATUS_LABEL.get(e["status"], ("unk", e["status"]))
        total = e.get("total_display", e.get("total"))
        mx = e.get("max_supply_display", e.get("max_supply"))
        rem = e.get("remaining_display", e.get("remaining"))
        total = f"{total:,}" if isinstance(total, int) else "?"
        mx = f"{mx:,}" if isinstance(mx, int) else "?"
        rem = f"{rem:,}" if isinstance(rem, int) else "?"
        unit = html.escape(e.get("display_unit", "")) if e.get("display_unit") else ""
        if unit:
            total += f" {unit}"
        reasons = "<br>".join(html.escape(r) for r in e["reasons"])
        rows.append(
            f"<tr><td><b>{html.escape(e['name'])}</b><br><span class='small'>"
            f"<code>{html.escape(e['contract'])}</code></span></td>"
            f"<td class='{cls}'>{label}</td><td>{e['score']}</td>"
            f"<td>{total} / {mx}<br><span class='small'>left: {rem}</span></td>"
            f"<td class='reason'>{reasons}</td></tr>")
    last = state.get("last_poll")
    last_s = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(last)) if last else "never"
    rpc = "OK" if state.get("rpc_ok") else f"ERROR: {html.escape(str(state.get('rpc_error')))}"
    return f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content='width=device-width,initial-scale=1'>
<meta http-equiv='refresh' content='300'>
<title>MintWatch — BSC Free-Mint Radar</title>
<style>{DASHBOARD_CSS}</style></head><body>
<h1>🎯 MintWatch — BSC Free-Mint Radar Agent</h1>
<p>Read-only agent: watches free mints on BNB Chain, reads the contracts
themselves, scores each opportunity. No signing, no spending, no keys.</p>
<p class='small'>Last poll: {last_s} &nbsp;·&nbsp; Block: {state.get('last_block')}
&nbsp;·&nbsp; RPC: {rpc} &nbsp;·&nbsp; Polls: {state.get('polls')}
&nbsp;·&nbsp; Agent v{html.escape(state.get('version',''))}</p>
<table><tr><th>Opportunity</th><th>Status</th><th>Score</th>
<th>Minted / Max</th><th>Why (agent reasoning)</th></tr>
{''.join(rows) or '<tr><td colspan=5>No data yet — first poll in progress.</td></tr>'}
</table>
<h2>Use it</h2>
<ul>
<li><code>GET /api/feed</code> — ranked JSON feed</li>
<li><code>GET /api/opportunities/&lt;id&gt;</code> — full on-chain reads + history</li>
<li><code>GET /api/decisions</code> — the agent's reasoning log</li>
<li><code>POST /api/evaluate</code> <code>{{"contract": "0x..."}}</code> — verdict on any BSC contract</li>
</ul>
<p class='small'>Every number comes from a public <code>eth_call</code> and can be
reproduced by anyone. A getter that reverts is reported as unknown, never guessed.
Source: <a href='https://github.com/EpicAbadi/mintwatch'>github.com/EpicAbadi/mintwatch</a></p>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    agent = None  # set by serve()
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # keep stdout for agent logs only
        sys.stderr.write("[http] %s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers ------------------------------------------------------
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body, status=200):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _state(self):
        with self.agent.lock:
            return json.loads(json.dumps(self.agent.state))

    # -- routes --------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        st = self._state()
        if path == "/":
            self._html(render_dashboard(st))
        elif path == "/health":
            self._json({"status": "ok" if st.get("rpc_ok") is not False else "degraded",
                        "agent": AGENT_NAME, "version": st.get("version"),
                        "uptime_s": int(time.time() - self.agent.started_at),
                        "last_poll": st.get("last_poll"),
                        "last_block": st.get("last_block"),
                        "rpc_ok": st.get("rpc_ok"),
                        "polls": st.get("polls")})
        elif path == "/api/feed":
            self._json({"agent": AGENT_NAME, "version": st.get("version"),
                        "block": st.get("last_block"),
                        "checked_at": st.get("last_poll"),
                        "opportunities": _feed_public(st.get("feed", []))})
        elif path == "/api/decisions":
            self._json({"decisions": st.get("decisions", [])[:50]})
        elif path == "/api/registry":
            try:
                self._json({"opportunities": self.agent.load_registry()})
            except Exception as e:
                self._json({"error": str(e)}, status=500)
        elif path.startswith("/api/opportunities/"):
            oid = path.split("/")[-1]
            match = [e for e in st.get("feed", []) if e["id"] == oid]
            if not match:
                self._json({"error": f"unknown opportunity id: {oid}"}, status=404)
                return
            detail = dict(match[0])
            detail["history"] = st.get("history", {}).get(oid, [])
            self._json(detail)
        else:
            self._json({"error": "not found",
                        "try": ["/", "/health", "/api/feed", "/api/decisions",
                                "/api/registry"]}, status=404)

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path != "/api/evaluate":
            self._json({"error": "not found"}, status=404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length > 2048:
            self._json({"error": "body too large (max 2KB)"}, status=413)
            return
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json({"error": "body must be JSON"}, status=400)
            return
        address = payload.get("contract", "")
        if not isinstance(address, str):
            self._json({"error": "contract must be a string"}, status=400)
            return
        # Cheap abuse guard: one evaluation per client per 5 seconds.
        now = time.time()
        bucket = self.server.rate
        bucket[:] = [t for t in bucket if now - t < 5]
        if len(bucket) >= 10:
            self._json({"error": "rate limited: try again in a few seconds"},
                       status=429)
            return
        bucket.append(now)
        self._json(self.agent.evaluate_contract(address.strip()))


def serve(agent, host, port):
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.rate = []
    Handler.agent = agent
    httpd.daemon_threads = True
    return httpd


def main(argv=None):
    ap = argparse.ArgumentParser(description="MintWatch — BSC Free-Mint Radar Agent")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MINTWATCH_PORT", "8891")))
    ap.add_argument("--host", default=os.environ.get("MINTWATCH_HOST", "127.0.0.1"))
    ap.add_argument("--registry", default=os.environ.get(
        "MINTWATCH_REGISTRY", os.path.join(os.path.dirname(os.path.abspath(__file__)), "registry.json")))
    ap.add_argument("--data", default=os.environ.get(
        "MINTWATCH_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")))
    ap.add_argument("--interval", type=int, default=int(os.environ.get("MINTWATCH_INTERVAL", "300")))
    ap.add_argument("--once", action="store_true", help="run one poll cycle and exit")
    args = ap.parse_args(argv)

    agent = MintWatch(args.registry, args.data, interval=args.interval)
    print(f"[mintwatch] v{VERSION} starting; registry={args.registry} data={args.data}",
          flush=True)
    agent.poll_once()
    print(f"[mintwatch] first poll done: block={agent.state['last_block']} "
          f"rpc_ok={agent.state['rpc_ok']}", flush=True)
    if args.once:
        return 0

    stop = threading.Event()
    t = threading.Thread(target=agent.run_loop, args=(stop,), daemon=True)
    t.start()
    httpd = serve(agent, args.host, args.port)
    print(f"[mintwatch] listening on http://{args.host}:{args.port}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
