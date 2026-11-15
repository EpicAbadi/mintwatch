# MintWatch — a free-mint radar agent for BNB Chain

*Wildcard submission, AgentGames (zkVerify). Operator: @etotsenpai.
Agent: MintWatch v0.1.0. All claims below are checkable against the chain;
the evidence log is `docs/VERIFICATION.md`.*

## The problem

Free mints on BNB Chain are first-come-first-served and routinely sell out
in minutes. The information a hunter needs — is this mint still free, is
any supply left — lives in three different places that disagree with each
other: X posts, project websites, and the contract. Websites say "free"
after the free allocation is gone; posts quote a max supply the contract
does not have; and nobody wants to read a contract by hand at 07:00 when
888 daily slots open.

The missing piece is small and specific: one place that answers, from the
contract itself, "free? how much left? what do we actually know?" — and
that says plainly when it does not know.

## Who it helps

Agent-ID hunters and small-wallet users on BSC — people who mint with gas
money only and cannot afford to burn BNB finding out a mint is closed.
Concretely, this agent was built by someone who has done this by hand for
weeks (SOLVD, FORGE, Pacture, Claf Eye, UNSOLVABLE, BAgent, Agent Cards,
Hash21) and kept hitting the same failure: the website said one thing, the
contract said another, and the contract was right.

## What the agent does

MintWatch runs a loop every five minutes: observe, reason, publish.

**Observe.** It reads a registry of eight tracked opportunities with
read-only `eth_call`s against public BSC JSON-RPC — no API key, no account,
no wallet. Typical reads: `totalSupply`, `MAX_SUPPLY`, `totalMinted`,
`remainingSupply`, `paused`, `name`, `symbol`.

**Reason.** A deterministic scoring rule turns the reads into a verdict:

- `OPEN` — free (gas only) and supply remains. Score 90–100 when the
  remaining supply is known; 60 when it cannot be read on-chain, labelled
  provisional.
- `CLOSED_SOLD_OUT` — minted equals max. Score 0, kept in the feed as a
  labelled reference instead of silently dropped.
- `PAID_ONLY` — the free allocation is exhausted and only paid shares
  remain. Score 5.
- `UNKNOWN` — no free-mint claim could be confirmed.

Every verdict carries its reasons in plain language ("3,127 of 3,333
supply left; more than 25% of supply still unminted"). A getter that
reverts is recorded as `unknown` — never as zero, never guessed. The
reasoning log is public at `/api/decisions`.

**Publish.** A dashboard, a ranked JSON feed, per-opportunity detail with
full reads and history, and an on-demand evaluator: `POST /api/evaluate
{"contract": "0x..."}` probes any BSC contract and returns a verdict —
is it a contract at all, what are its supply reads, is it sold out, and
what the reads could not establish (mint price, for example, is not
determinable from generic reads, and the agent says so).

**Live state at submission time** (block 133,743,948): three free mints
open — UNSOLVABLE (3,127 of 3,333 left), The Claf Eye (10,864 of 18,000;
first per wallet free), Forge Robotics (2,462 of 5,000; first per wallet
free). Three more open but provisional (Agent Cards, BAgent daily claim,
Hash21 — remaining supply not readable from generic getters). Pacture is
paid-only; SOLVD is sold out.

## How it works

Python 3 standard library only — no dependencies. One file
(`mintwatch.py`, ~700 lines) plus a JSON registry. A poller thread runs
the cycle and persists state atomically; a threaded HTTP server serves the
dashboard and API. It runs as a systemd service on a small VPS (10.6 MB
RAM) behind a Cloudflare tunnel for public HTTPS. It never signs, sends,
or simulates a transaction, holds no key, and spends nothing.

The feed is reproducible by design: every published number is a raw
on-chain integer, and the README shows the exact `eth_call` to re-check
any of them. The test suite (22 tests) includes live tests that assert the
feed against independent `eth_call`s made inside the test, so a decoding
bug in the agent cannot hide behind itself. During development that
discipline caught two real errors — a factor-1000 slip in reading HASH
and PACTURE supplies — before they shipped.

## Key decisions

1. **The contract is the source of truth, the website is a rumour.**
   Two entries exist to prove it: the Hash21 pool reports 318,789 HASH
   minted, and the BAgent token reads fully minted (444.4B = max) — both
   differ from what the projects' own pages implied. The agent reports
   the chain and flags the discrepancy instead of smoothing it over.
2. **Admit ignorance in the open.** BAgent's daily slot availability and
   Agent Cards' remaining supply cannot be read from public getters (25
   candidate getters probed; all reverted except an uninterpreted
   `quote()`). Those entries score 60 with a "provisional" label rather
   than a fabricated number. A radar that invents data is worse than none.
3. **Keep dead entries visible.** Sold-out and paid-only mints stay in the
   feed, labelled. Deleting them would make the feed look better and be
   less useful: "is it over?" is a question the feed must answer too.
4. **No dependencies, no keys, no spend.** The whole point is that anyone
   can run this from the README alone. A dependency or a secret would
   make the demo unreproducible for a reviewer.
5. **zkVerify: deliberately not used.** See below.

## What it cannot do

It cannot tell you the mint price of an arbitrary contract (generic reads
do not expose it), it cannot discover new mints by itself (the registry is
curated; new contracts arrive via `/api/evaluate` and registry updates),
it cannot mint for you, and its "provisional" verdicts genuinely mean
"check the site" — for BAgent, at the 07:00 WIB reset, and for Hash21,
where the free offer itself is a site claim rather than a chain fact.

## zkVerify

Not used — deliberately, and this is stated rather than hidden. Every
claim MintWatch publishes is a direct on-chain read: anyone can re-run the
same `eth_call` and get the same answer. The chain itself is the proof.
zkTLS would add nothing here because the data source is not an HTTPS API
response; it is the blockchain. There is no off-chain claim in this agent
that needs to become checkable, so there is nothing honest for zkVerify
to prove. Claiming an integration we do not have would be fiction, and a
verifiability bonus earned by fiction would be worthless.

## Links

- Agent (live): https://layers-post-dad-kingston.trycloudflare.com
  (`/`, `/health`, `/api/feed`, `/api/opportunities/<id>`,
  `/api/decisions`, `POST /api/evaluate`)
- Repo: https://github.com/EpicAbadi/mintwatch
- Evidence: `docs/VERIFICATION.md` in the repo.
