# VERIFICATION.md — MintWatch evidence log

How every claim in the submission was checked. All times WIB unless noted.
No claim below rests on memory or on the agent's own word alone.

## 1. Chain reads (registry build, 2026-11-15 ~20:40 WIB)

Recon script: `docs/recon.py` (read-only `eth_call` / `eth_getCode`).
Raw output: `docs/recon-2026-11-15.json`. RPC: public BSC dataseed, no key.

Block 133,743,052:

| Contract | Read | Result |
|---|---|---|
| SOLVD `0x5966…4076` | totalSupply / MAX_SUPPLY | 4096 / 4096 → sold out |
| FORGE `0x8529…2fe3` | totalMinted / MAX_SUPPLY / remainingSupply | 2538 / 5000 / 2462 → open |
| Pacture token `0x10b8…2f6` | totalSupply / MAX_SUPPLY | 104,350,000,000 / 143,000,000,000 (18 dec) → 10,435 of 11,000 shares minted; free allocation exhausted |
| Claf Eye `0x1c75…456` | totalSupply / MAX_SUPPLY / MAX_PER_WALLET | 7136 / 18000 / 30 → open |
| UNSOLVABLE `0x9e95…0904` | totalSupply / MAX_SUPPLY | 206 / 3333 → open |
| BAgent proxy `0x9CA1…0F4f` | totalMinted | 23264; day-slot getters all revert → slot availability unknown |
| BAGENT token `0xb50d…4444` | totalSupply / MAX_SUPPLY | 444,400,000,000 / 444,400,000,000 → fully minted |
| Agent Cards `0x19f1…FACd` | totalSupply | 8318; no max-supply getter → remaining unknown |
| Hash21 pool `0x6884…279D` | totalMinted | 318,789,528,439,717,294,094,193 base units = 318,789 HASH (18 dec) |
| Hash21 token `0x6a87…6D90` | totalSupply / decimals | same 318,789 HASH / 18 dec |

Cross-check: BAGENT and HASH totalSupply re-read on a second independent
RPC (`bsc-dataseed1.defibit.io`) — identical values. Keccak selectors used
in recon were verified against the standard empty/`abc` test vectors before
any contract call.

Two of the agent's own early readings were caught and corrected by tests,
not shipped: HASH supply is 318,789 (not 318M) and Pacture supply is
104.35B (not 104.35M) — both were factor-1000 slips in the author's head,
fixed after the unit test `test_scaled_display_keeps_raw` failed on the
arithmetic. The feed reports raw on-chain integers; scaled display values
are derived, never substituted.

## 2. Tests (2026-11-15 ~21:00 WIB, local VM)

```
python3 -m unittest discover -s tests            # 22 tests, OK (7 live skipped)
MINTWATCH_LIVE=1 python3 -m unittest discover -s tests   # 22 tests, OK
```

Live tests assert the feed against independent `eth_call` reads made inside
the test (not through the agent's code), including a sold-out contract, a
paid-only contract, three open contracts, and `POST /api/evaluate` on a
known contract plus rejection of malformed input.

## 3. Local end-to-end smoke (2026-11-15 ~21:05 WIB)

Server on 127.0.0.1:8891, one poll cycle. Feed returned exactly the
recon-derived verdicts: forge / claf-eye / unsolvable OPEN 100,
agent-cards / bagent / hash21 OPEN 60 (provisional), pacture PAID_ONLY 5,
solvd CLOSED_SOLD_OUT 0. `POST /api/evaluate` on SOLVD returned
`SOLD_OUT` with total 4096 / max 4096.

## 4. VPS deployment (2026-11-15 ~20:30–20:45 WIB)

- Host: user's Oracle VPS ("VPS" in all reports; IP never written down).
- Additive only: new units `mintwatch.service` + `cloudflared-mintwatch.service`;
  `myage.service` and the three giveaway watchers were not touched
  (verified still `active` after deploy).
- `mintwatch.service`: active, 10.6 MB RAM, polling every 300 s, state in
  `/home/ubuntu/mintwatch/data/state.json`.
- `cloudflared` 2026.11.0 installed to `/usr/local/bin/cloudflared`.

## 5. Public HTTPS endpoint (2026-11-15 ~20:45 WIB)

- URL: `https://layers-post-dad-kingston.trycloudflare.com`
- Tunnel registered: journal shows `Registered tunnel connection …
  location=sin09 protocol=quic`, service `active`.
- Verified from the internet (request issued from the VPS's own egress,
  i.e. a network outside the tunnel): `GET /health` → HTTP 200,
  `{"status":"ok",…,"last_block":133743948,"rpc_ok":true}`;
  `GET /api/feed` → HTTP 200, 8 opportunities, correct statuses/scores.
- Caveat, stated plainly: from this build VM's egress (shared proxy IP)
  and from the browser-fetch service, Cloudflare's edge returns HTTP 403
  ("Your request was blocked") for this `*.trycloudflare.com` hostname.
  The endpoint itself is healthy — the 403 is Cloudflare's IP-reputation
  filtering on those two datacenter egresses. A normal browser network
  (such as the VPS's own egress, verified above) gets HTTP 200.
- The tunnel URL is random and changes if the tunnel service restarts.
  Re-check before filing with:
  `sudo journalctl -u cloudflared-mintwatch.service --no-pager | grep -o
  'https://[a-z0-9-]*\.trycloudflare\.com' | sort -u`
- Better long-term option (needs the user, not done): point a subdomain
  (e.g. `wildcard.totsenpai.xyz`) at the VPS and use Let's Encrypt, for a
  stable hostname. `totsenpai.xyz` currently does not resolve to the VPS.

## 6. What is NOT verified / NOT done

- GitHub repo is prepared locally (git init + commit) but NOT yet public:
  `gh` is not logged in on this VM and direct GitHub SSH is reset by the
  VM egress proxy. Publishing to `github.com/EpicAbadi/mintwatch` needs
  the main agent / user.
- The AgentGames submission form itself has not been filed (needs the
  logged-in browser session). Field values are in `SUBMIT_PACKAGE.md`.
- No zkVerify integration exists (by design — see README / WRITEUP).
