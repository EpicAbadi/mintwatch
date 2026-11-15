#!/usr/bin/env python3
"""MintWatch tests.

Unit tests run offline (no network). Live tests (marked) hit the public BSC
RPC and the agent's own HTTP server, and are only run with --live:

    python3 -m unittest discover -s tests -v            # unit only
    MINTWATCH_LIVE=1 python3 -m unittest discover -s tests -v   # unit + live
    ./tests/run_tests.sh [--live]

Live tests assert the agent's feed against independent eth_call reads made
directly in the test (not through the agent's code path), so a bug in the
agent's decoding cannot hide behind itself.
"""
import json
import os
import sys
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mintwatch  # noqa: E402

LIVE = os.environ.get("MINTWATCH_LIVE") == "1"

REGISTRY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "registry.json")


def u256(n):
    return "0x" + n.to_bytes(32, "big").hex()


def abi_string(s):
    b = s.encode()
    pad = (-len(b)) % 32
    return ("0x" + (32).to_bytes(32, "big").hex()
            + len(b).to_bytes(32, "big").hex() + b.hex() + "00" * pad)


class TestDecode(unittest.TestCase):
    def test_uint(self):
        self.assertEqual(mintwatch.decode_value(u256(123456), "auto"), 123456)
        self.assertEqual(mintwatch.decode_value(u256(0), "auto"), 0)

    def test_string(self):
        self.assertEqual(mintwatch.decode_value(abi_string("SOLVD"), "string"), "SOLVD")
        self.assertEqual(mintwatch.decode_value(abi_string(""), "string"), "")

    def test_bool(self):
        self.assertIs(mintwatch.decode_value(u256(1), "bool"), True)
        self.assertIs(mintwatch.decode_value(u256(0), "bool"), False)
        with self.assertRaises(ValueError):
            mintwatch.decode_value(u256(2), "bool")

    def test_address(self):
        raw = "0x" + "00" * 12 + "6aaafE417d0320fB7584FdeBa79f216c62FB40e9"
        self.assertEqual(mintwatch.decode_value(raw, "address"),
                         "0x6aaafe417d0320fb7584fdeba79f216c62fb40e9")

    def test_empty_is_none(self):
        self.assertIsNone(mintwatch.decode_value("0x", "auto"))

    def test_garbage_is_raw(self):
        self.assertEqual(mintwatch.decode_value("0x1234", "auto"), "0x1234")


class TestEvaluate(unittest.TestCase):
    def _entry(self, **kw):
        e = {"id": "t", "name": "T", "contract": "0x" + "11" * 20,
             "free": True, "needs": ["totalSupply()", "MAX_SUPPLY()"]}
        e.update(kw)
        return e

    def _reads(self, total, max_supply):
        return {"totalSupply()": {"ok": True, "raw": u256(total), "value": total},
                "MAX_SUPPLY()": {"ok": True, "raw": u256(max_supply), "value": max_supply}}

    def test_free_open_scores_high(self):
        ev = mintwatch.evaluate(self._entry(), self._reads(206, 3333), 100, 1700000000)
        self.assertEqual(ev["status"], "OPEN")
        self.assertEqual(ev["remaining"], 3127)
        self.assertGreaterEqual(ev["score"], 90)
        self.assertIn("free", ev["reasons"][0])

    def test_sold_out_scores_zero(self):
        ev = mintwatch.evaluate(self._entry(), self._reads(4096, 4096), 100, 1700000000)
        self.assertEqual(ev["status"], "CLOSED_SOLD_OUT")
        self.assertEqual(ev["score"], 0)

    def test_paused_scores_zero(self):
        reads = self._reads(10, 5000)
        reads["paused()"] = {"ok": True, "raw": u256(1), "value": 1}
        ev = mintwatch.evaluate(self._entry(), reads, 100, 1700000000)
        self.assertEqual(ev["status"], "CLOSED_PAUSED")
        self.assertEqual(ev["score"], 0)

    def test_paid_only(self):
        e = self._entry(free=False, free_exhausted="free allocation exhausted")
        ev = mintwatch.evaluate(e, {}, 100, 1700000000)
        self.assertEqual(ev["status"], "PAID_ONLY")
        self.assertLessEqual(ev["score"], 10)

    def test_unknown_when_no_reads(self):
        e = self._entry(free=False, needs=[])
        ev = mintwatch.evaluate(e, {}, 100, 1700000000)
        self.assertEqual(ev["status"], "UNKNOWN")
        self.assertIn("no free-mint claim", " ".join(ev["reasons"]))

    def test_revert_is_not_zero(self):
        # A reverted getter must not be treated as totalSupply == 0.
        reads = {"totalSupply()": {"ok": False, "error": "revert"},
                 "MAX_SUPPLY()": {"ok": True, "raw": u256(3333), "value": 3333}}
        ev = mintwatch.evaluate(self._entry(), reads, 100, 1700000000)
        self.assertEqual(ev["status"], "OPEN")  # free + no proof of exhaustion
        self.assertIsNone(ev["remaining"])
        self.assertIn("provisional", " ".join(ev["reasons"]))

    def test_scaled_display_keeps_raw(self):
        e = self._entry(scale=18, unit="HASH")
        ev = mintwatch.evaluate(
            e, self._reads(318789528439717294094193, 500000000000000000000000000),
            100, 1700000000)
        self.assertEqual(ev["total"], 318789528439717294094193)  # raw, reproducible
        self.assertEqual(ev["total_display"], 318789)
        self.assertEqual(ev["remaining_display"], 499681210)
        self.assertEqual(ev["display_unit"], "HASH")

    def test_address_validation(self):
        self.assertTrue(mintwatch.ADDRESS_RE.match("0x6aaafE417d0320fB7584FdeBa79f216c62FB40e9"))
        self.assertFalse(mintwatch.ADDRESS_RE.match("0x123"))
        self.assertFalse(mintwatch.ADDRESS_RE.match("6aaafE417d0320fB7584FdeBa79f216c62FB40e9"))


class TestRegistry(unittest.TestCase):
    def test_registry_loads_and_validates(self):
        with open(REGISTRY) as f:
            reg = json.load(f)
        self.assertGreaterEqual(len(reg["opportunities"]), 5)
        for e in reg["opportunities"]:
            self.assertTrue(mintwatch.ADDRESS_RE.match(e["contract"]), e["id"])
            self.assertTrue("free" in e or "free_first" in e, e["id"])
            self.assertIsInstance(e["name"], str)
            self.assertTrue(e["name"])


@unittest.skipUnless(LIVE, "live tests need --live")
class TestLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = mintwatch.MintWatch(REGISTRY, "/tmp/mintwatch-test-data",
                                        interval=3600)
        cls.agent.poll_once()
        cls.httpd = mintwatch.serve(cls.agent, "127.0.0.1", 0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=30) as r:
            return json.loads(r.read())

    def _rpc(self, method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                           "params": params}).encode()
        req = urllib.request.Request("https://bsc-dataseed.binance.org",
                                     data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["result"]

    def test_health(self):
        h = self._get("/health")
        self.assertEqual(h["status"], "ok")
        self.assertIsNotNone(h["last_block"])

    def test_feed_matches_chain(self):
        """Feed numbers must equal independent eth_call reads."""
        feed = self._get("/api/feed")["opportunities"]
        by_id = {o["id"]: o for o in feed}
        checks = {
            "unsolvable": ("0x9e95Da84f1bcc8E5570DAAc99A37EB3f95270904", 3333),
            "claf-eye": ("0x1c759C4e075103b5B250DB5114B832b17B99C456", 18000),
            "forge": ("0x85294Fd5CffB964E318302B0C03C8f4Ae7BF2fe3", 5000),
            "solvd": ("0x59661d7F8f996F687CD98c116cCaf66eFd254076", 4096),
        }
        for oid, (addr, max_supply) in checks.items():
            o = by_id[oid]
            if oid == "forge":
                # FORGE has no totalSupply(); the agent falls back to totalMinted().
                raw = self._rpc("eth_call", [{"to": addr, "data": "0xa2309ff8"}, "latest"])
                self.assertEqual(o["total"], int(raw, 16), oid)
            else:
                raw = self._rpc("eth_call", [{"to": addr, "data": "0x18160ddd"}, "latest"])
                chain_total = int(raw, 16) if raw != "0x" else None
                if chain_total is not None:
                    self.assertEqual(o["total"], chain_total, oid)
            raw_max = self._rpc("eth_call", [{"to": addr, "data": "0x32cb6b0c"}, "latest"])
            chain_max = int(raw_max, 16) if raw_max != "0x" else None
            self.assertEqual(o["max_supply"], max_supply, oid)
            if chain_max is not None:
                self.assertEqual(o["max_supply"], chain_max, oid)

    def test_statuses(self):
        feed = self._get("/api/feed")["opportunities"]
        by_id = {o["id"]: o for o in feed}
        self.assertEqual(by_id["solvd"]["status"], "CLOSED_SOLD_OUT")
        self.assertEqual(by_id["pacture"]["status"], "PAID_ONLY")
        self.assertEqual(by_id["unsolvable"]["status"], "OPEN")
        self.assertEqual(by_id["claf-eye"]["status"], "OPEN")
        self.assertEqual(by_id["forge"]["status"], "OPEN")
        self.assertGreater(by_id["unsolvable"]["score"], 70)

    def test_opportunity_detail_and_decisions(self):
        d = self._get("/api/opportunities/unsolvable")
        self.assertEqual(d["id"], "unsolvable")
        self.assertIn("reads", d)
        self.assertTrue(d["reads"]["totalSupply()"]["ok"])
        dec = self._get("/api/decisions")
        self.assertTrue(dec["decisions"])
        self.assertEqual(dec["decisions"][0]["opportunity"], "unsolvable")

    def test_evaluate_known_contract(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/evaluate",
            data=json.dumps({"contract": "0x1c759C4e075103b5B250DB5114B832b17B99C456"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            res = json.loads(r.read())
        self.assertTrue(res["is_contract"])
        self.assertEqual(res["name"], "The Claf Eye")
        self.assertEqual(res["max_supply"], 18000)
        # Total must equal an independent eth_call made here, not via the agent.
        raw = self._rpc("eth_call", [{"to": "0x1c759C4e075103b5B250DB5114B832b17B99C456",
                                      "data": "0x18160ddd"}, "latest"])
        self.assertEqual(res["total"], int(raw, 16))

    def test_evaluate_rejects_bad_input(self):
        for bad in ["not-an-address", "0x123", 123]:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/api/evaluate",
                data=json.dumps({"contract": bad}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    res = json.loads(r.read())
                self.assertFalse(res.get("ok", True), bad)
            except urllib.error.HTTPError as e:
                self.assertEqual(e.code, 400)

    def test_dashboard_renders(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=30) as r:
            body = r.read().decode()
        self.assertIn("MintWatch", body)
        self.assertIn("UNSOLVABLE", body)


if __name__ == "__main__":
    unittest.main()
