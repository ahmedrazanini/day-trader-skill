import datetime
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr

from _load import load, FIX

psx = load("trader-psx", "psx")

F = os.path.join(FIX, "trader-psx")  # real SCS Trade feed answers, saved Wed Nov 11 2026 after the close
CFG = dict(psx.DEFAULTS)
WED = datetime.date(2026, 11, 11)
FRI = datetime.date(2026, 11, 13)


def at(text):
    return psx.now_pkt(text)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = psx.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Sessions(unittest.TestCase):
    def test_monday_to_thursday_is_72_bars(self):
        s = psx.session(CFG, WED)
        g = psx.grid(s)
        self.assertEqual((psx.hhmm(g[0]), psx.hhmm(g[-1]), len(g)), ("09:30", "15:25", 72))
        self.assertFalse(s["friday"])

    def test_friday_has_the_break(self):
        g = psx.grid(psx.session(CFG, FRI))
        self.assertEqual(psx.hhmm(g[0]), "09:15")
        self.assertIn(psx.mins("11:55"), g)
        self.assertNotIn(psx.mins("12:00"), g)
        self.assertNotIn(psx.mins("14:05"), g)
        self.assertIn(psx.mins("14:10"), g)
        self.assertEqual(psx.hhmm(g[-1]), "16:25")
        self.assertEqual(len(g), 33 + 28)

    def test_weekend_is_closed(self):
        self.assertIsNone(psx.session(CFG, datetime.date(2026, 11, 14)))
        with self.assertRaises(psx.PSXError):
            psx.day_window(at("2026-11-14 10:30"), CFG)

    def test_range_not_done_before_9_45(self):
        with self.assertRaises(psx.PSXError) as e:
            psx.day_window(at("2026-11-11 09:40"), CFG)
        self.assertIn("9:45 AM", str(e.exception))

    def test_times(self):
        self.assertEqual(psx.mins("2:10 PM"), 14 * 60 + 10)
        self.assertEqual(psx.clock(15 * 60 + 25), "3:25 PM")

    def test_post_close_bars_are_left_out(self):
        feed = psx.Feed(offline=F)
        days = psx.intraday(feed.history("OGDC", "5", 0, 2 ** 31), at("2026-11-11 16:00"), CFG)
        self.assertEqual(max(days[WED]), psx.mins("15:25"))
        self.assertEqual(len(days[WED]), 72)
        self.assertNotIn(datetime.date(2026, 11, 9), days)  # Iqbal Day, no trading


class OpeningRange(unittest.TestCase):
    def setUp(self):
        self.feed = psx.Feed(offline=F)
        self.sess = psx.session(CFG, WED)

    def bars(self, sym, now):
        return psx.fetch_today(self.feed, sym, WED, at(now), CFG)

    def test_ogdc_triggers_at_10(self):
        o = psx.orb(self.bars("OGDC", "2026-11-11 10:30"), self.sess, psx.mins("10:30"), CFG)
        self.assertEqual(o["status"], "triggered")
        self.assertEqual((o["or_low"], o["or_high"], o["entry"], o["bar"], o["at"]),
                         (317.01, 318.25, 318.99, "09:55", "10:00 AM"))
        self.assertEqual(o["pct_over"], 0.23)

    def test_waiting_before_the_trigger(self):
        o = psx.orb(self.bars("OGDC", "2026-11-11 09:55"), self.sess, psx.mins("09:55"), CFG)
        self.assertEqual(o["status"], "waiting")
        self.assertEqual(o["or_high"], 318.25)

    def test_a_bar_still_forming_does_not_count(self):
        # at 9:59 the 9:55 bar is not complete yet
        o = psx.orb(self.bars("OGDC", "2026-11-11 09:59"), self.sess, psx.mins("09:59"), CFG)
        self.assertEqual(o["status"], "waiting")

    def test_a_missing_bar_makes_it_unusable(self):
        b = self.bars("OGDC", "2026-11-11 10:30")
        del b[psx.mins("09:50")]
        o = psx.orb(b, self.sess, psx.mins("10:30"), CFG)
        self.assertEqual(o["status"], "unusable")

    def test_expired_after_the_last_entry_time(self):
        b = {m: dict(x, c=min(x["c"], 300.0)) for m, x in self.bars("OGDC", "2026-11-11 16:00").items()}
        for m in list(b)[:3]:
            b[m]["h"] = 400.0
        o = psx.orb(b, self.sess, psx.mins("15:30"), CFG)
        self.assertEqual(o["status"], "expired")

    def test_scan(self):
        d = tempfile.mkdtemp()
        try:
            code, out, _ = run("orb", "--symbols", "OGDC,PSO,HBL,UBL", "--now", "2026-11-11 10:30",
                               "--offline", F, "--dir", d, "--quiet")
            self.assertEqual(code, 0)
            self.assertIn("4 of 4 charts usable", out)
            self.assertIn("PSO      range 360.00 to 366.00, closed 366.99 at 9:55 AM", out)
            wl = json.load(open(os.path.join(d, "watchlist.json")))
            self.assertEqual([h["symbol"] for h in wl["hits"]], ["PSO", "HBL", "OGDC", "UBL"])
        finally:
            shutil.rmtree(d)


class PlanAndReplay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.feed = psx.Feed(offline=F)
        sess = psx.session(CFG, WED)
        o = psx.orb(psx.fetch_today(cls.feed, "OGDC", WED, at("2026-11-11 10:30"), CFG), sess, psx.mins("10:30"), CFG)
        ldcp = psx.prev_close(cls.feed, "OGDC", WED, CFG)
        cls.plan = psx.make_plan("OGDC", WED, sess, o, CFG, "or_mid", 1.0, ldcp)
        cls.plan3 = psx.make_plan("OGDC", WED, sess, o, CFG, "or_mid", 3.0, ldcp)
        cls.tm = psx.time_machine(cls.feed, cls.plan, at("2026-11-11 10:30"), CFG)

    def test_plan(self):
        p = self.plan
        self.assertEqual((p["entry"], p["stop"], p["target"], p["risk_per_share"]), (318.99, 317.63, 320.35, 1.36))
        self.assertEqual((p["ldcp"], p["lower_limit"], p["upper_limit"]), (317.02, 285.32, 348.72))
        self.assertEqual(p["flat_by"], "3:25 PM")
        self.assertTrue(any("0.70R" in n for n in p["notes"]))
        self.assertEqual(self.plan3["target"], 323.07)

    def test_price_limits(self):
        self.assertEqual(psx.price_limits(317.02, CFG), (348.72, 285.32))
        self.assertEqual(psx.price_limits(8.0, CFG), (9.0, 7.0))  # PKR 1 floor on cheap stocks

    def test_target_over_the_upper_limit_is_flagged(self):
        sess = psx.session(CFG, WED)
        o = {"status": "triggered", "entry": 100.0, "or_high": 99.0, "or_low": 90.0, "at": "10:00 AM",
             "bar": "09:55", "or_minutes": 15}
        p = psx.make_plan("X", WED, sess, o, CFG, "or_low", 2.0, 95.0)
        self.assertTrue(any("upper price limit" in n for n in p["notes"]))

    def test_time_machine_numbers(self):
        mc, h = self.tm["mc"], self.tm["history"]
        self.assertEqual(self.tm["pool"]["sessions"], 60)
        self.assertEqual(self.tm["pool"]["kind"], "Monday-to-Thursday")
        self.assertEqual((mc["p_target"], mc["p_stop"], mc["p_neither"]), (0.549, 0.4487, 0.0023))
        self.assertEqual((h["triggered"], h["target"], h["stop"], h["close"]), (34, 15, 18, 1))
        self.assertEqual(mc["until"], "3:25 PM")
        self.assertAlmostEqual(mc["fee_R"], 0.704, places=3)
        self.assertLess(mc["avg_R_after_fees"], 0)
        self.assertTrue(self.tm["verdict"].startswith("Leans toward the target"))
        self.assertTrue(any("loses money" in n for n in self.tm["notes"]))

    def test_same_seed_same_answer(self):
        again = psx.time_machine(self.feed, self.plan, at("2026-11-11 10:30"), CFG)
        self.assertEqual(again["mc"]["p_target"], self.tm["mc"]["p_target"])

    def test_3r_verdict_uses_break_even(self):
        tm = psx.time_machine(self.feed, self.plan3, at("2026-11-11 10:30"), CFG)
        self.assertIn("at 3R the target has to come first in 25 %", tm["verdict"])
        self.assertTrue(tm["verdict"].startswith("Leans toward the stop"))

    def test_size_and_check_block_at_1r(self):
        o = psx.size_order(self.plan, CFG)
        self.assertEqual(o["qty"], 782)  # capped by the 50 % position limit
        self.assertEqual(o["limited_by"], "cash / position limit")
        self.assertLessEqual(o["loss_at_stop_pkr"], CFG["max_loss_per_trade_pkr"])
        v, why = psx.risk_check(o, self.plan, CFG, at("2026-11-11 10:30"))
        self.assertEqual(v, "BLOCK")
        self.assertTrue(any(w.startswith("reward:risk after fees is 0.17") for w in why))

    def test_check_passes_at_3r_and_prints_a_ticket(self):
        o = psx.size_order(self.plan3, CFG)
        v, why = psx.risk_check(o, self.plan3, CFG, at("2026-11-11 10:30"))
        self.assertEqual((v, why), ("PASS", []))
        self.assertIn("BUY 782 OGDC, limit 318.99", psx.ticket(o))
        self.assertIn("stop-loss at 317.63", psx.ticket(o))

    def test_check_blocks_limits(self):
        o = psx.size_order(self.plan3, CFG)
        late = psx.risk_check(o, self.plan3, CFG, at("2026-11-11 14:35"))[1]
        self.assertTrue(any(w.startswith("window") for w in late))
        stale = psx.risk_check(o, self.plan3, CFG, at("2026-11-12 10:30"))[1]
        self.assertTrue(any(w.startswith("stale") for w in stale))
        busy = psx.risk_check(o, self.plan3, CFG, at("2026-11-11 10:30"), trades_today=3, pnl_today=-6000)[1]
        self.assertTrue(any(w.startswith("trades") for w in busy))
        self.assertTrue(any(w.startswith("daily loss") for w in busy))

    def test_lot_size_rounds_down(self):
        o = psx.size_order(self.plan3, dict(CFG, lot_size=500))
        self.assertEqual(o["qty"], 500)

    def test_size_by_max_loss(self):
        o = psx.size_order(self.plan3, dict(CFG, max_loss_per_trade_pkr=1000))
        self.assertEqual(o["limited_by"], "max loss")
        self.assertLessEqual(o["loss_at_stop_pkr"], 1000)


class Friday(unittest.TestCase):
    def test_friday_replays_past_fridays_only(self):
        feed = psx.Feed(offline=F)
        sess = psx.session(CFG, datetime.date(2026, 11, 6))
        day = datetime.date(2026, 11, 6)
        o = psx.orb(psx.fetch_today(feed, "OGDC", day, at("2026-11-06 11:00"), CFG), sess, psx.mins("11:00"), CFG)
        self.assertEqual((o["status"], o["entry"], o["at"]), ("triggered", 315.01, "10:55 AM"))
        plan = psx.make_plan("OGDC", day, sess, o, CFG, "or_mid", 2.0, psx.prev_close(feed, "OGDC", day, CFG))
        self.assertEqual(plan["flat_by"], "4:25 PM")
        tm = psx.time_machine(feed, plan, at("2026-11-06 11:00"), CFG)
        self.assertEqual((tm["pool"]["kind"], tm["pool"]["sessions"]), ("Friday", 14))
        self.assertTrue(all(datetime.date.fromisoformat(t["date"]).weekday() == 4 for t in tm["history"]["trades"]))
        self.assertTrue(any("treat these odds as rough" in n for n in tm["notes"]))


class Daily(unittest.TestCase):
    def test_setups(self):
        up = [100 + i for i in range(40)]
        self.assertIn("breakout_20d", psx.find_setups(up)[0])
        down = [200 - 2 * i for i in range(40)]
        self.assertIn("oversold", psx.find_setups(down)[0])

    def test_scan_liquidity_filter(self):
        d0 = datetime.date(2026, 9, 1)
        rows = lambda price, vol: [(d0 + datetime.timedelta(days=i), price + i, vol) for i in range(40)]
        hits, c, _ = psx.scan_daily({"BIG": rows(100, 1e6), "THIN": rows(100, 10), "SHORT": rows(100, 1e6)[:20]},
                                    5, 1e7)
        self.assertEqual([h["symbol"] for h in hits], ["BIG"])
        self.assertEqual((c["illiquid"], c["short"]), (1, 1))

    def test_daily_cli_offline(self):
        d = tempfile.mkdtemp()
        try:
            code, out, _ = run("daily", "--symbols", "OGDC,PSO,HBL,UBL", "--now", "2026-11-11 16:00",
                               "--offline", F, "--dir", d, "--quiet")
            self.assertEqual(code, 0)
            self.assertIn("closes through 2026-11-11", out)
            self.assertIn("PSO", out)
        finally:
            shutil.rmtree(d)

    def test_today_counts_only_after_the_close(self):
        feed = psx.Feed(offline=F)
        h = feed.history("OGDC", "D", 0, 2 ** 31)
        self.assertEqual(psx.daily(h, at("2026-11-11 11:00"), CFG)[-1][0], datetime.date(2026, 11, 10))
        self.assertEqual(psx.daily(h, at("2026-11-11 16:00"), CFG)[-1][0], WED)


class Pipeline(unittest.TestCase):
    def test_run_never_places_an_order(self):
        d = tempfile.mkdtemp()
        try:
            code, out, _ = run("run", "OGDC", "--target", "3R", "--now", "2026-11-11 10:30",
                               "--offline", F, "--dir", d, "--replays", "2000")
            self.assertEqual(code, 0)
            self.assertIn("Risk check: PASS", out)
            self.assertIn("for YOU to place in your own broker's app", out)
            self.assertIn("Nothing has been placed. Not financial advice.", out)
            for f in ("plan.json", "timemachine.json", "orders.json"):
                self.assertTrue(os.path.exists(os.path.join(d, f)), f)
        finally:
            shutil.rmtree(d)

    def test_run_blocks_at_1r(self):
        d = tempfile.mkdtemp()
        try:
            code, out, _ = run("run", "OGDC", "--now", "2026-11-11 10:30", "--offline", F, "--dir", d,
                               "--replays", "2000")
            self.assertEqual(code, 2)
            self.assertIn("Risk check: BLOCK", out)
            self.assertNotIn("Ticket", out)
        finally:
            shutil.rmtree(d)

    def test_example_config_is_safe(self):
        cfg = json.load(open(os.path.join(os.path.dirname(F), "..", "..", "trader-psx", "config.example.json")))
        self.assertIs(cfg["require_stop"], True)
        self.assertIs(cfg["allow_short"], False)
        self.assertIs(cfg["allow_margin"], False)
        self.assertEqual(set(cfg) - set(psx.DEFAULTS), set())


if __name__ == "__main__":
    unittest.main()
