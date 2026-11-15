# MintWatch — BSC Free-Mint Radar Agent

An autonomous, read-only agent that watches free-mint / free-claim
opportunities on BNB Chain, reads their live on-chain state, scores each
opportunity, and publishes a ranked public feed.

Built for the [AgentGames Wildcard](https://agentgames.zkverify.io/events/wildcard)
(zkVerify). No zkVerify integration — see [Why no zkVerify](#why-no-zkverify)
for the honest reason.

## The problem

Free mints on BSC are first-come-first-served and sell out in minutes, but
the information about them is scattered across X posts, project sites and
contract reads. A hunter needs one question answered, fast and honestly:
*is this mint still free, is any supply left, and what does the contract
itself say?* MintWatch answers exactly that, from the contract as source of
truth — not from a website's marketing copy.

## What it does

Every 5 minutes the agent runs one observe → reason → publish cycle:

1. **Observe** — `eth_call` / `eth_getCode` against public BSC JSON-RPC
   (no API key, no account). Reads `totalSupply`, `MAX_SUPPLY`,
   `totalMinted`, `remainingSupply`, `paused`, `name`, `symbol`, … per
   contract in `registry.json`.
2. **Reason** — a deterministic, documented scoring rule turns the reads
   into a verdict: `OPEN` / `CLOSED_SOLD_OUT` / `CLOSED_PAUSED` /
   `PAID_ONLY` / `UNKNOWN`, plus a human-readable list of reasons.
   Nothing is guessed: a getter that reverts is reported as `unknown`,
   never filled in as zero.
3. **Publish** — the ranked feed, a dashboard, and a reasoning log over HTTP.

It also answers on-demand evaluations: `POST /api/evaluate` with
`{"contract": "0x..."}` probes any BSC contract and returns a verdict
(supply left? sold out? what the reads could and could not establish).

## What it never does

Sign, send, or simulate a transaction. Hold a key. Spend anything. Call a
private API. Every number it publishes can be reproduced with a single
`eth_call` — the chain is the proof.

## Scoring (0–100)

| Rule | Points |
|---|---|
| Mint/claim is free (gas only), per the verified source | +50 |
| Remaining supply is known and > 0 | +30 |
| More than 25% of max supply still unminted | +10 |
| Every expected getter returned a value | +10 |

Sold-out / paused opportunities score 0 and stay in the feed as labelled
references. Paid-only opportunities score 5. A free opportunity whose
remaining supply cannot be read scores 60 and is labelled provisional.

## Run it

Python 3.11+, standard library only — no dependencies to install.

```bash
python3 mintwatch.py                 # listens on 127.0.0.1:8891
python3 mintwatch.py --port 8891 --interval 300
python3 mintwatch.py --once          # one poll cycle, print nothing, exit
```

Environment: `MINTWATCH_PORT`, `MINTWATCH_HOST`, `MINTWATCH_INTERVAL`,
`MINTWATCH_REGISTRY`, `MINTWATCH_DATA`.

## Endpoints

| Route | What it returns |
|---|---|
| `GET /` | Dashboard (server-rendered HTML) |
| `GET /health` | Liveness, uptime, last poll, last block, RPC status |
| `GET /api/feed` | Ranked opportunities (JSON) |
| `GET /api/opportunities/<id>` | One opportunity: full reads + history |
| `GET /api/decisions` | The agent's reasoning log |
| `GET /api/registry` | The tracked registry |
| `POST /api/evaluate` | `{"contract": "0x..."}` → on-demand verdict |

## Verify it yourself

```bash
# Feed, then re-check one number independently (no agent code involved):
curl -s https://<endpoint>/api/feed | python3 -m json.tool
# totalSupply of UNSOLVABLE, direct eth_call:
curl -s https://bsc-dataseed.binance.org -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"eth_call",
       "params":[{"to":"0x9e95Da84f1bcc8E5570DAAc99A37EB3f95270904",
                  "data":"0x18160ddd"},"latest"]}'
# 0xce = 206 minted; MAX_SUPPLY (0x32cb6b0c) = 0xd05 = 3333.
```

## Tests

```bash
./tests/run_tests.sh            # unit tests (offline)
./tests/run_tests.sh --live     # + live tests vs public BSC RPC
```

Live tests assert the feed against independent `eth_call` reads made in the
test itself, so a bug in the agent's decoding cannot hide behind itself.

## Registry

`registry.json` lists the tracked opportunities with the contract address,
the getters to read, and the verified facts the score rests on (free?
per-wallet limit? known max?). Add a new one by appending an entry —
`needs` lists the getters, `caveats` lists what the agent must admit it
does not know. Chain state at registry build time (2026-11-15, block
133,743,052) is recorded in `docs/VERIFICATION.md`.

## Deploy

See `systemd/mintwatch.service` (agent) and
`systemd/cloudflared-mintwatch.service` (public HTTPS via Cloudflare quick
tunnel — outbound-only, no inbound firewall change). Both are additive:
they touch no other service.

## Why no zkVerify

Deliberately not used, and stated plainly. Every claim MintWatch publishes
is a direct on-chain read: anyone can re-run the same `eth_call` and get the
same answer — the chain itself is the proof. zkTLS would add nothing here
because the data source is not an HTTPS API response; it is the blockchain.
Claiming a zkVerify integration we do not have would be fiction.

## License

MIT. See `LICENSE`.
