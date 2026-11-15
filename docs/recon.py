#!/usr/bin/env python3
"""One-off recon: probe candidate free-mint contracts on BSC via public RPC.

Read-only eth_call / eth_getCode. No keys, no signing.
Writes recon.json next to this script.
"""
import json, sys, urllib.request

RPC = "https://bsc-dataseed.binance.org"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) MintWatch-recon/0.1", "Content-Type": "application/json"}

SEL = {
    "totalSupply()": "0x18160ddd", "maxSupply()": "0xd5abeb01",
    "MAX_SUPPLY()": "0x32cb6b0c", "totalMinted()": "0xa2309ff8",
    "mintPrice()": "0x6817c76c", "price()": "0xa035b1fe",
    "paused()": "0x5c975abb", "owner()": "0x8da5cb5b",
    "name()": "0x06fdde03", "symbol()": "0x95d89b41",
    "decimals()": "0x313ce567", "MAX_PER_WALLET()": "0x0f2cdd6c",
    "maxPerWallet()": "0x453c2310", "remainingSupply()": "0xda0239a6",
    "availableSupply()": "0x7ecc2b56",
}

CONTRACTS = {
    "solvd": "0x59661d7F8f996F687CD98c116cCaf66eFd254076",
    "forge": "0x85294Fd5CffB964E318302B0C03C8f4Ae7BF2fe3",
    "pacture-mint": "0x95AF9fe16fE5c1741b5E3F1b3b045a8c2470eca9",
    "pacture-token": "0x10b80088fDAf9363d83671BE66b64f231f9e92f6",
    "claf-eye": "0x1c759C4e075103b5B250DB5114B832b17B99C456",
    "unsolvable": "0x9e95Da84f1bcc8E5570DAAc99A37EB3f95270904",
    "bagent-proxy": "0x9CA1A3eeA74829aA5011c56386b226DB472D0F4f",
    "bagent-token": "0xb50dC583cC6F83750c36789F52d51E12dA664444",
    "agent-cards": "0x19f125dA1985033f102CA7746Cbd1F6f512CFACd",
    "hash21-pool": "0x6884D40B10c55A6c2faB2D3c829d41c3C798279D",
    "hash21-token": "0x6a87DE92deb752c6162CF80Bd16E3eFd4BDc6D90",
}

def rpc(method, params):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers=UA, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

def call(to, data):
    try:
        res = rpc("eth_call", [{"to": to, "data": data}, "latest"])
        if "error" in res:
            return {"error": res["error"].get("message", str(res["error"]))}
        return {"raw": res.get("result", "0x")}
    except Exception as e:
        return {"error": f"transport: {e}"}

def decode(raw):
    """Best-effort ABI decode: string | uint256 | bool | address | raw."""
    if not raw or raw == "0x":
        return None
    b = bytes.fromhex(raw[2:])
    if len(b) >= 64:
        # string: offset 0x20, then length, then bytes
        off = int.from_bytes(b[0:32], "big")
        if off == 32 and len(b) >= 64:
            ln = int.from_bytes(b[32:64], "big")
            if 0 <= ln <= len(b) - 64:
                try:
                    return b[64:64 + ln].decode("utf-8")
                except UnicodeDecodeError:
                    pass
    if len(b) == 32:
        v = int.from_bytes(b, "big")
        return v
    return raw

def main():
    out = {"rpc": RPC, "contracts": {}}
    bn = rpc("eth_blockNumber", [])
    out["blockNumber"] = int(bn["result"], 16) if "result" in bn else bn
    print("block:", out["blockNumber"])
    for name, addr in CONTRACTS.items():
        entry = {"address": addr}
        code = rpc("eth_getCode", [addr, "latest"])
        entry["code_len"] = (len(code.get("result", "0x")) - 2) // 2 if "result" in code else code
        entry["getters"] = {}
        for fn, sel in SEL.items():
            r = call(addr, sel)
            if "error" in r:
                entry["getters"][fn] = {"error": r["error"]}
            else:
                entry["getters"][fn] = {"raw": r["raw"], "decoded": decode(r["raw"])}
        out["contracts"][name] = entry
        ts = entry["getters"]["totalSupply()"].get("decoded")
        print(f"{name:15s} code={entry['code_len']:6d}B totalSupply={ts}")
    with open("recon.json", "w") as f:
        json.dump(out, f, indent=2)
    print("wrote recon.json")

if __name__ == "__main__":
    sys.exit(main())
