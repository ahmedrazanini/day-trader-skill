#!/usr/bin/env python3
"""PSX Day Trader (trader-psx): the day-trader pack's method on the Pakistan Stock Exchange.

Live PSX bars come from SCS Trade's charting feed (https://chart.scstrade.com), the
TradingView datafeed behind https://www.scstrade.com/TechnicalAnalysis/TA_RealTimeCharting.aspx.
It is free and unofficial: it can lag a couple of minutes, change, or go away.

    python3 psx.py symbols                        # every PSX symbol -> trader/psx/symbols.json
    python3 psx.py daily                          # setups on daily closes -> trader/psx/watchlist.json
    python3 psx.py orb --at 10:15                 # 15-minute opening-range breakouts so far today
    python3 psx.py entry OGDC [--watch]           # waiting / triggered / expired
    python3 psx.py plan OGDC                      # entry, stop, target -> trader/psx/plan.json
    python3 psx.py timemachine                    # 10,000 replays + every past day the rule fired
    python3 psx.py size                           # shares for your max loss -> trader/psx/orders.json
    python3 psx.py check                          # PASS / BLOCK, and the ticket to type into your broker app
    python3 psx.py run OGDC                       # entry -> plan -> timemachine -> size -> check in one go

Nothing here places an order. It prints a ticket; you place it yourself, in your own
broker's app, if and only if you decide to. Not financial advice.

Standard library only, Python 3.8+.
"""

import argparse
import datetime
import gzip
import http.client
import json
import math
import os
import random
import ssl
import statistics
import sys
import time
import urllib.parse
import urllib.request

PKT = datetime.timezone(datetime.timedelta(hours=5), "PKT")  # Pakistan keeps no daylight saving
FEED = "https://chart.scstrade.com"
BAR = 5  # minutes
OUT_DIR = os.path.join("trader", "psx")
UA = "trader-psx/1.0 (day-trader-skill)"

# PSX regular sessions, Karachi time. The bars after the close (the closing-price session,
# e.g. 15:30 to 15:45) are left out. Override in config.json "sessions" (Ramadan timings differ).
SESSIONS = {
    "mon_thu": {"open": "09:30", "close": "15:30", "break": None},
    "fri": {"open": "09:15", "close": "16:30", "break": ["12:00", "14:10"]},
}

DEFAULTS = {
    "account_pkr": 500000,
    "max_loss_per_trade_pkr": 2500,
    "max_daily_loss_pkr": 7500,
    "max_trades_per_day": 3,
    "min_reward_risk": 1.0,
    "max_position_pct": 50,
    "cash_buffer_pct": 10,
    "require_stop": True,
    "lot_size": 1,
    "fee_pct_per_side": 0.15,
    "price_limit_pct": 10,
    "price_limit_min_pkr": 1,
    "or_minutes": 15,
    "last_entry_mins_before_close": 60,
    "flat_mins_before_close": 5,
    "min_price_pkr": 5,
    "min_avg_value_pkr": 10000000,
    "allow_short": False,
    "allow_margin": False,
    "blocked": [],
    "sessions": SESSIONS,
}

SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05
STRONG = 0.15
MANY_NEITHER = 0.40
POOL_DAYS = 60
MIN_POOL = 20
MIN_POOL_FRIDAY = 10  # the feed keeps ~17 weeks of 5-minute bars: about 14 Fridays

SETUP_NAMES = {
    "breakout_20d": "20-day closing-high breakout",
    "bull_flag": "bull flag",
    "reclaim_ma20": "reclaimed the 20-day average",
    "oversold": "oversold (RSI under 30)",
    "oversold_bounce": "oversold bounce",
}


class PSXError(Exception):
    pass


# ---------------------------------------------------------------- time

def pkt(ts):
    return datetime.datetime.fromtimestamp(int(ts), PKT)


def now_pkt(text=None):
    """--now "2026-11-11 10:15" (Karachi time) or the real clock."""
    if not text:
        return datetime.datetime.now(PKT)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(text.strip(), fmt).replace(tzinfo=PKT)
        except ValueError:
            pass
    raise PSXError("--now wants \"YYYY-MM-DD HH:MM\" in Karachi time, got %r" % text)


def mins(hhmm):
    """'09:30' or '9:30' or '2:10 PM' -> minutes after midnight."""
    s = hhmm.strip().upper()
    pm = s.endswith("PM")
    am = s.endswith("AM")
    s = s.replace("AM", "").replace("PM", "").strip()
    try:
        h, m = s.split(":")
        h, m = int(h), int(m)
    except ValueError:
        raise PSXError("a time looks like 10:15 or 2:10 PM, got %r" % hhmm)
    if pm and h < 12:
        h += 12
    if am and h == 12:
        h = 0
    return h * 60 + m


def clock(m):
    h, mm = divmod(int(m), 60)
    return "%d:%02d %s" % ((h - 1) % 12 + 1, mm, "AM" if h < 12 else "PM")


def hhmm(m):
    return "%02d:%02d" % divmod(int(m), 60)


def of_day(dt):
    return dt.hour * 60 + dt.minute


def session(cfg, day):
    """The day's regular session in minutes, or None on a weekend. Holidays are not in
    a calendar here: a weekday with no bars is reported as such."""
    wd = day.weekday()
    if wd >= 5:
        return None
    s = (cfg.get("sessions") or SESSIONS)["fri" if wd == 4 else "mon_thu"]
    brk = s.get("break")
    return {"open": mins(s["open"]), "close": mins(s["close"]),
            "break": (mins(brk[0]), mins(brk[1])) if brk else None, "friday": wd == 4}


def grid(sess):
    """Bar start minutes of a full regular session."""
    out = []
    m = sess["open"]
    while m + BAR <= sess["close"]:
        b = sess["break"]
        if not (b and b[0] <= m < b[1]):
            out.append(m)
        m += BAR
    return out


def last_entry(cfg, sess):
    return sess["close"] - int(cfg["last_entry_mins_before_close"])


def flat_by(cfg, sess):
    return sess["close"] - int(cfg["flat_mins_before_close"])


# ---------------------------------------------------------------- the feed

def _fname(sym, res):
    return "%s_%s.json" % (sym.replace(" ", "_"), res)


class Feed(object):
    """SCS Trade's UDF datafeed, or a folder of saved answers (--offline)."""

    def __init__(self, offline=None, pause=0.1, tries=4):
        self.offline = offline
        self.pause = pause
        self.tries = tries
        self.requests = 0
        self.conn = None

    def _connect(self):
        """One kept-alive HTTPS connection (through HTTPS_PROXY when set): a new TLS
        handshake per request makes the feed drop connections."""
        host = urllib.parse.urlparse(FEED).hostname
        ctx = ssl.create_default_context()
        proxy = urllib.request.getproxies().get("https")
        if proxy and not urllib.request.proxy_bypass(host):
            pu = urllib.parse.urlparse(proxy if "://" in proxy else "http://" + proxy)
            c = http.client.HTTPSConnection(pu.hostname, pu.port or 8080, timeout=20, context=ctx)
            c.set_tunnel(host, 443)
        else:
            c = http.client.HTTPSConnection(host, 443, timeout=20, context=ctx)
        return c

    def _get(self, path, params):
        query = path + "?" + urllib.parse.urlencode(params)
        err = None
        for i in range(self.tries):
            try:
                if self.conn is None:
                    self.conn = self._connect()
                self.conn.request("GET", query, headers={"User-Agent": UA, "Accept": "application/json"})
                r = self.conn.getresponse()
                text = r.read().decode("utf-8", "replace")
                self.requests += 1
                if r.status in (400, 404):
                    raise PSXError("the feed said %d for %s" % (r.status, query))
                if r.status != 200:
                    raise OSError("HTTP %d" % r.status)
                if self.pause:
                    time.sleep(self.pause)
                return json.loads(text)
            except (http.client.HTTPException, OSError, ValueError) as e:  # resets happen: reconnect, retry
                err = e
                if self.conn is not None:
                    self.conn.close()
                self.conn = None
                time.sleep(2 ** i)
        raise PSXError("could not reach %s%s: %s" % (FEED, query, err))

    def history(self, sym, res, frm, to):
        """{'t','o','h','l','c','v'} oldest first, or None when the feed has no data."""
        if self.offline:
            d = None
            for name in (_fname(sym, res), _fname(sym, res) + ".gz"):
                p = os.path.join(self.offline, name)
                if os.path.exists(p):
                    opener = gzip.open if p.endswith(".gz") else open
                    with opener(p, "rt", encoding="utf-8") as fh:
                        d = json.load(fh)
                    break
            if d is None:
                return None
        else:
            d = self._get("/history", {"symbol": sym, "resolution": res, "from": int(frm), "to": int(to)})
        if not isinstance(d, dict) or d.get("s") != "ok" or not d.get("t"):
            return None
        keep = [i for i, t in enumerate(d["t"]) if frm <= t <= to]
        return {k: [d[k][i] for i in keep] for k in ("t", "o", "h", "l", "c", "v") if k in d}

    def search(self, query):
        if self.offline:
            p = os.path.join(self.offline, "symbols.json")
            if not os.path.exists(p):
                return []
            with open(p, encoding="utf-8") as fh:
                rows = json.load(fh)
            return [{"symbol": s, "description": n} for s, n in rows.items() if query.upper() in s]
        return self._get("/search", {"query": query, "limit": 5000, "type": "stock", "exchange": "PSX"})


# ---------------------------------------------------------------- bars

def intraday(h, now, cfg):
    """{date: {start_minute: bar}} of COMPLETE 5-minute bars inside the regular session."""
    days = {}
    if not h:
        return days
    for i, t in enumerate(h["t"]):
        o, hi, lo, c = h["o"][i], h["h"][i], h["l"][i], h["c"][i]
        if None in (o, hi, lo, c) or t % (BAR * 60):
            continue
        st = pkt(t)
        if st + datetime.timedelta(minutes=BAR) > now:
            continue  # still forming
        sess = session(cfg, st.date())
        if not sess:
            continue
        m = of_day(st)
        if m not in set(grid(sess)):
            continue  # pre-open, the break, the post-close session
        days.setdefault(st.date(), {})[m] = {"m": m, "o": float(o), "h": float(hi), "l": float(lo),
                                              "c": float(c), "v": float(h["v"][i] or 0) if "v" in h else 0.0}
    return days


def daily(h, now, cfg):
    """[(date, close, volume)] of complete daily bars. Today counts only after the close."""
    out = {}
    if not h:
        return []
    today = now.date()
    sess = session(cfg, today)
    done = sess is None or of_day(now) >= sess["close"]
    for i, t in enumerate(h["t"]):
        c = h["c"][i]
        if c is None:
            continue
        d = datetime.datetime.fromtimestamp(int(t), datetime.timezone.utc).date()
        if d > today or (d == today and not done):
            continue
        out[d] = (float(c), float(h["v"][i] or 0) if "v" in h else 0.0)
    return [(d, c, v) for d, (c, v) in sorted(out.items())]


def prev_close(feed, sym, day, cfg):
    """LDCP: the last daily close before `day`."""
    end = datetime.datetime.combine(day, datetime.time(0), PKT)
    h = feed.history(sym, "D", int((end - datetime.timedelta(days=20)).timestamp()), int(end.timestamp()) - 1)
    rows = daily(h, end, cfg)
    return rows[-1][1] if rows else None


def price_limits(ldcp, cfg):
    if not ldcp:
        return None, None
    band = max(ldcp * float(cfg["price_limit_pct"]) / 100.0, float(cfg["price_limit_min_pkr"]))
    return round(ldcp + band, 2), round(ldcp - band, 2)


# ---------------------------------------------------------------- the daily setups (same rules as trader-scanner)

def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    g = [max(0.0, closes[i] - closes[i - 1]) for i in range(1, len(closes))]
    lo = [max(0.0, closes[i - 1] - closes[i]) for i in range(1, len(closes))]
    ag, al = sum(g[:n]) / n, sum(lo[:n]) / n
    for i in range(n, len(g)):
        ag = (ag * (n - 1) + g[i]) / n
        al = (al * (n - 1) + lo[i]) / n
    return 100 - 100 / (1 + ag / al) if al else 100.0


def find_setups(c):
    last = c[-1]
    hi20 = max(c[-21:-1])
    ma20 = sum(c[-20:]) / 20.0
    ma20_prev = sum(c[-21:-1]) / 20.0
    run = c[-6] / c[-16] - 1
    rng5 = (max(c[-5:]) - min(c[-5:])) / last
    r = rsi(c)
    r_prev = rsi(c[:-1])
    tags = []
    if last > hi20:
        tags.append("breakout_20d")
    if run > 0.10 and rng5 < 0.05:
        tags.append("bull_flag")
    if c[-2] < ma20_prev and last > ma20:
        tags.append("reclaim_ma20")
    if r is not None and r < 30:
        tags.append("oversold")
    if r_prev is not None and r_prev < 30 and last > c[-2]:
        tags.append("oversold_bounce")
    return tags, {"rsi": r, "high20": hi20, "ma20": ma20}


def scan_daily(series, min_price, min_value):
    """series = {SYM: [(date, close, volume)]} -> (hits, counts, last_date). Pure."""
    hits, counts, last = [], {"fetched": 0, "illiquid": 0, "short": 0, "scanned": 0}, None
    for sym, rows in sorted(series.items()):
        if not rows:
            continue
        counts["fetched"] += 1
        closes = [r[1] for r in rows]
        value = sum(r[1] * r[2] for r in rows[-20:]) / min(20, len(rows))
        if closes[-1] < min_price or value < min_value:
            counts["illiquid"] += 1
            continue
        if len(closes) < 30:
            counts["short"] += 1
            continue
        counts["scanned"] += 1
        last = max(last, rows[-1][0]) if last else rows[-1][0]
        tags, n = find_setups(closes)
        if tags:
            hits.append({"symbol": sym, "close": round(closes[-1], 2), "rsi": round(n["rsi"], 1) if n["rsi"] else None,
                         "avg_value_pkr": round(value), "setups": tags, "date": rows[-1][0].isoformat()})
    hits.sort(key=lambda x: -x["avg_value_pkr"])
    return hits, counts, last


# ---------------------------------------------------------------- the opening-range breakout

def orb(day_bars, sess, cutoff, cfg):
    """The 15-minute opening range and the first completed bar to CLOSE above its high.
    day_bars = {start_minute: bar}; cutoff = minutes after midnight. Pure."""
    n = int(cfg["or_minutes"]) // BAR
    expected = [m for m in grid(sess) if m + BAR <= cutoff]
    res = {"or_minutes": n * BAR, "last_entry": hhmm(last_entry(cfg, sess)), "bars": len(expected)}
    if len(expected) < n:
        res.update(status="forming", reason="the opening range ends at %s" % clock(sess["open"] + n * BAR))
        return res
    missing = [hhmm(m) for m in expected if m not in day_bars]
    if missing:
        res.update(status="unusable", reason="no trades in %d five-minute bar(s) (%s): too thin, or halted"
                   % (len(missing), ", ".join(missing[:4]) + (" ..." if len(missing) > 4 else "")))
        return res
    rng = [day_bars[m] for m in expected[:n]]
    oh, ol = max(b["h"] for b in rng), min(b["l"] for b in rng)
    res.update(or_high=round(oh, 2), or_low=round(ol, 2), last=round(day_bars[expected[-1]]["c"], 2))
    for k, m in enumerate(expected[n:], start=n):
        b = day_bars[m]
        if b["c"] > oh:
            at = m + BAR
            if at > last_entry(cfg, sess):
                break
            res.update(status="triggered", entry=round(b["c"], 2), bar=hhmm(m), at=clock(at), at_min=at,
                       pct_over=round((b["c"] / oh - 1) * 100, 2), bar_index=k)
            return res
    if cutoff >= last_entry(cfg, sess):
        res.update(status="expired", reason="no close above %.2f before the last entry time, %s"
                   % (oh, clock(last_entry(cfg, sess))))
    else:
        res.update(status="waiting", reason="no 5-minute close above %.2f yet (last %.2f)" % (oh, res["last"]))
    return res


def day_window(now, cfg, at=None):
    """(day, session, cutoff minute) for an intraday check, or raise with a plain reason."""
    day = now.date()
    sess = session(cfg, day)
    if not sess:
        raise PSXError("PSX is closed on %s (weekend)." % now.strftime("%A"))
    cutoff = mins(at) if at else of_day(now)
    cutoff = min(cutoff, sess["close"])
    if cutoff < sess["open"] + int(cfg["or_minutes"]):
        raise PSXError("The %d-minute opening range is not done yet: PSX opens at %s, the range ends at %s Karachi time."
                       % (cfg["or_minutes"], clock(sess["open"]), clock(sess["open"] + int(cfg["or_minutes"]))))
    return day, sess, cutoff


def fetch_today(feed, sym, day, now, cfg):
    start = datetime.datetime.combine(day, datetime.time(0), PKT)
    h = feed.history(sym, "5", int(start.timestamp()), int(now.timestamp()))
    return intraday(h, now, cfg).get(day, {})


# ---------------------------------------------------------------- the plan

def paisa_away(x):
    """Round a long stop down to the paisa (away from the entry)."""
    return math.floor(round(x * 100, 4)) / 100.0


def make_plan(sym, day, sess, o, cfg, stop_rule="or_mid", target_r=1.0, ldcp=None):
    if o.get("status") != "triggered":
        raise PSXError("%s has no entry yet: %s" % (sym, o.get("reason", o.get("status"))))
    e = o["entry"]
    if stop_rule == "or_mid":
        s = paisa_away((o["or_high"] + o["or_low"]) / 2)
    elif stop_rule == "or_low":
        s = paisa_away(o["or_low"])
    elif stop_rule.endswith("%"):
        s = paisa_away(e * (1 - float(stop_rule[:-1]) / 100.0))
    else:
        raise PSXError("--stop is or-mid, or-low, or a percent like 1.5%%, got %r" % stop_rule)
    risk = round(e - s, 2)
    if risk <= 0:
        raise PSXError("the stop %.2f is not below the entry %.2f" % (s, e))
    t = round(e + target_r * risk, 2)
    up, down = price_limits(ldcp, cfg)
    notes = []
    fee_r = float(cfg["fee_pct_per_side"]) / 100.0 * (2 * e) / risk
    if fee_r >= 0.25:
        notes.append("fees of about %s %% a side cost %.2fR a round trip on this %.2f stop: a tight stop on PSX "
                     "needs a bigger target or a wider stop" % (cfg["fee_pct_per_side"], fee_r, risk))
    if up is not None and t > up:
        notes.append("the target %.2f is above today's upper price limit %.2f: it cannot fill today" % (t, up))
    if down is not None and s < down:
        notes.append("the stop %.2f is below today's lower price limit %.2f" % (s, down))
    return {"symbol": sym, "date": day.isoformat(), "side": "buy", "setup": "orb", "entry": e,
            "entry_time": o["at"], "entry_bar": o["bar"], "stop": s, "stop_rule": stop_rule,
            "target": t, "target_r": target_r, "risk_per_share": risk,
            "opening_range": {"minutes": o["or_minutes"], "high": o["or_high"], "low": o["or_low"]},
            "flat_by": clock(flat_by(cfg, sess)), "last_entry": clock(last_entry(cfg, sess)),
            "ldcp": ldcp, "upper_limit": up, "lower_limit": down, "notes": notes,
            "source": "SCS Trade chart feed (chart.scstrade.com), 5-minute bars"}


# ---------------------------------------------------------------- the time machine (same method as trader-timemachine)

def full_sessions(days, cfg, before, friday):
    """Full past sessions of the same kind (Friday or Monday to Thursday), oldest first:
    (date, [bar for each grid slot])."""
    out, short = [], 0
    for d in sorted(days):
        if d >= before:
            continue
        sess = session(cfg, d)
        if not sess or sess["friday"] != friday:
            continue
        g = grid(sess)
        D = days[d]
        if all(m in D for m in g):
            out.append((d, [D[m] for m in g]))
        else:
            short += 1
    return out, short


def build_pool(sessions, n):
    pool = [[] for _ in range(n)]
    for _, D in sessions:
        for i in range(1, n):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2):
    """Monte Carlo replays of bars k+1..last for a long trade. Stop and target inside one
    bar count as the stop (conservative)."""
    n = len(pool)
    risk = entry - stop
    r_target = round((target - entry) / risk, 4)

    def slot(rng, i):
        return i if window == 0 else min(n - 1, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    outcomes, closes, paths = [], [], []
    for r in range(replays):
        p, oc, path = entry, None, [entry]
        for i in range(k + 1, last + 1):
            o, h, l, c = rng.choice(pool[slot(rng, i)])
            if oc is None:
                if p * l <= stop:
                    oc = "stop"
                elif p * h >= target:
                    oc = "target"
            p = p * c
            if r < SAMPLE_PATHS:
                path.append(round(p, 2))
        outcomes.append(oc or "close")
        closes.append(p)
        if r < SAMPLE_PATHS:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes) if o == "target" or (o == "close" and p > entry))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else (p - entry) / risk for o, p in zip(outcomes, closes)]
    srt = sorted(closes)
    rng = random.Random(seed)
    allp = []
    for _ in range(BAND_PATHS):
        p, path = entry, [entry]
        for i in range(k + 1, last + 1):
            p = p * rng.choice(pool[slot(rng, i)])[3]
            path.append(p)
        allp.append(path)
    bands = [[round(pct(sorted(pp[j] for pp in allp), q), 2) for q in BAND_Q] for j in range(last - k + 1)]
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    return {"replays": replays, "seed": seed, "window": window, "bars_left": last - k,
            "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
            "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
            "median_close": round(pct(srt, 0.5), 2), "p5_close": round(pct(srt, 0.05), 2),
            "p95_close": round(pct(srt, 0.95), 2), "avg_R": round(statistics.mean(Rs), 3),
            "r_at_target": r_target, "bands": bands, "sample_paths": paths,
            "hist": {"lo": round(lo_, 2), "hi": round(hi_, 2), "bins": bins}}


def history_replay(sessions, plan, g, last):
    """The plan's rule on every past session: entry = the first bar after the opening
    range to close above it (up to the last entry bar), then stop first, then target,
    bar by bar, else out at the flat-by bar's close."""
    n = plan["opening_range"]["minutes"] // BAR
    rule, R_t = plan["stop_rule"], plan["target_r"]
    stop_pct = plan["risk_per_share"] / plan["entry"]
    trades = []
    for d, D in sessions:
        oh = max(b["h"] for b in D[:n])
        ol = min(b["l"] for b in D[:n])
        kk = next((i for i in range(n, last + 1) if D[i]["c"] > oh), None)
        if kk is None:
            continue
        if plan.get("_last_entry_index") is not None and kk > plan["_last_entry_index"]:
            continue
        e = round(D[kk]["c"], 2)
        s = paisa_away((oh + ol) / 2) if rule == "or_mid" else paisa_away(ol) if rule == "or_low" else \
            paisa_away(e * (1 - stop_pct))
        risk = e - s
        if risk <= 0:
            continue
        t = round(e + R_t * risk, 2)
        oc = None
        for b in D[kk + 1:last + 1]:
            if b["l"] <= s:
                oc = "stop"
                break
            if b["h"] >= t:
                oc = "target"
                break
        cl = D[last]["c"]
        R = R_t if oc == "target" else -1.0 if oc == "stop" else (cl - e) / risk
        trades.append({"date": d.isoformat(), "time": clock(g[kk] + BAR), "entry": e, "stop": s, "target": t,
                       "outcome": oc or "close", "R": round(R, 2)})
    return {"sessions": len(sessions), "triggered": len(trades),
            "target": sum(1 for x in trades if x["outcome"] == "target"),
            "stop": sum(1 for x in trades if x["outcome"] == "stop"),
            "close": sum(1 for x in trades if x["outcome"] == "close"),
            "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
            "trades": trades}


def lean(mc):
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    share, even = pt / (pt + ps), 1.0 / (1.0 + r)
    return share - even, share, even


def verdict(mc, until):
    lv = lean(mc)
    if lv is None:
        return "No replay reached the target or the stop by %s." % until
    d = lv[0]
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    r = mc.get("r_at_target") or 1.0
    odds = "target first %.1f %% vs stop first %.1f %%" % (100 * mc["p_target"], 100 * mc["p_stop"])
    if abs(r - 1.0) < 0.005:
        return "%s: %s." % (head, odds)
    return ("%s: at %gR the target has to come first in %.0f %% of the replays that hit one side; it did in %.0f %% (%s)."
            % (head, round(r, 2), 100 * lv[2], 100 * lv[1], odds))


def tm_notes(mc):
    notes = []
    if mc["p_neither"] > MANY_NEITHER:
        notes.append("%.1f %% of the replays hit neither by %s; the price then decides those."
                     % (100 * mc["p_neither"], mc["until"]))
    if mc.get("avg_R_after_fees") is not None and mc["avg_R_after_fees"] < 0:
        notes.append("After fees the average is %+.3fR: on these odds, taking this trade again and again loses money."
                     % mc["avg_R_after_fees"])
    return notes


def time_machine(feed, plan, now, cfg, replays=10000, seed=19, window=2):
    day = datetime.date.fromisoformat(plan["date"])
    sess = session(cfg, day)
    if not sess:
        raise PSXError("the plan's date %s is a weekend" % plan["date"])
    g = grid(sess)
    entry_m = mins(plan["entry_bar"])
    if entry_m not in g:
        raise PSXError("the plan's entry bar %s is not a bar of the session" % plan["entry_bar"])
    k = g.index(entry_m)
    fb = flat_by(cfg, sess)
    last = max(i for i, m in enumerate(g) if m + BAR <= fb)
    if last <= k:
        raise PSXError("no bars left between the entry and the flat-by time %s" % clock(fb))
    end = datetime.datetime.combine(day, datetime.time(0), PKT)
    h = feed.history(plan["symbol"], "5", int((end - datetime.timedelta(days=130)).timestamp()),
                     int(end.timestamp()) - 1)
    days = intraday(h, end, cfg)
    sessions, short = full_sessions(days, cfg, day, sess["friday"])
    sessions = sessions[-POOL_DAYS:]
    kind = "Friday" if sess["friday"] else "Monday-to-Thursday"
    need = MIN_POOL_FRIDAY if sess["friday"] else MIN_POOL
    if len(sessions) < need:
        raise PSXError("only %d full %s sessions of %s in the feed (need %d): too thin for a fair replay"
                       % (len(sessions), kind, plan["symbol"], need))
    pool = build_pool(sessions, len(g))
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed, window)
    mc["until"] = clock(g[last] + BAR)
    mc["fee_R"] = round(float(cfg["fee_pct_per_side"]) / 100.0 * 2 * plan["entry"] / plan["risk_per_share"], 3)
    mc["avg_R_after_fees"] = round(mc["avg_R"] - mc["fee_R"], 3)
    le = last_entry(cfg, sess)
    plan = dict(plan, _last_entry_index=max(i for i, m in enumerate(g) if m + BAR <= le))
    hist = history_replay(sessions, plan, g, last)
    plan.pop("_last_entry_index", None)
    return {"plan": plan, "pool": {"sessions": len(sessions), "kind": kind, "skipped_short": short,
                                    "from": sessions[0][0].isoformat(), "to": sessions[-1][0].isoformat()},
            "mc": mc, "history": hist, "verdict": verdict(mc, mc["until"]),
            "notes": tm_notes(mc) + ([("Only %d past %s sessions to draw from: treat these odds as rough."
                                       % (len(sessions), kind))] if len(sessions) < MIN_POOL else []),
            "checked_at": now.isoformat(timespec="minutes")}


# ---------------------------------------------------------------- sizing and the risk check

def size_order(plan, cfg):
    e, s, t = plan["entry"], plan["stop"], plan["target"]
    fee = float(cfg["fee_pct_per_side"]) / 100.0
    per_share = (e - s) + fee * (e + s)  # the loss at the stop, both sides' fees in
    lot = max(1, int(cfg["lot_size"]))
    acct = float(cfg["account_pkr"])
    spend = acct * (1 - float(cfg["cash_buffer_pct"]) / 100.0)
    spend = min(spend, acct * float(cfg["max_position_pct"]) / 100.0)
    by_loss = int(float(cfg["max_loss_per_trade_pkr"]) // per_share)
    by_cash = int(spend // (e * (1 + fee)))
    qty = min(by_loss, by_cash) // lot * lot
    why = "max loss" if by_loss <= by_cash else "cash / position limit"
    return {"symbol": plan["symbol"], "side": "buy", "qty": qty, "limit": e, "stop": s, "target": t,
            "value_pkr": round(qty * e, 2), "fees_est_pkr": round(qty * fee * (e + s), 2),
            "loss_at_stop_pkr": round(qty * per_share, 2),
            "gain_at_target_pkr": round(qty * ((t - e) - fee * (e + t)), 2),
            "net_reward_risk": round(((t - e) - fee * (e + t)) / per_share, 2),
            "limited_by": why, "lot_size": lot, "flat_by": plan["flat_by"], "date": plan["date"]}


def risk_check(order, plan, cfg, now, trades_today=0, pnl_today=0.0):
    """-> (PASS|BLOCK, [reasons]). Pure."""
    bad = []
    e, s, t, q = order["limit"], order["stop"], order["target"], order["qty"]
    if order["symbol"] in (cfg.get("blocked") or []):
        bad.append("blocked: %s is on your blocked list" % order["symbol"])
    if q <= 0:
        bad.append("size: 0 shares fit your max loss of PKR %s at %.2f a share risk"
                   % (fmt(cfg["max_loss_per_trade_pkr"]), e - s))
    if cfg.get("require_stop") and not s:
        bad.append("stop: no stop")
    if order["loss_at_stop_pkr"] > float(cfg["max_loss_per_trade_pkr"]) + 0.005:
        bad.append("loss: PKR %s at the stop is over your PKR %s max" % (
            fmt(order["loss_at_stop_pkr"]), fmt(cfg["max_loss_per_trade_pkr"])))
    rr = (t - e) / (e - s) if e > s else 0
    net = order.get("net_reward_risk", rr)
    if net + 1e-9 < float(cfg["min_reward_risk"]):
        bad.append("reward:risk after fees is %.2f, under your %.2f (%.2f before fees: the target makes PKR %s, "
                   "the stop loses PKR %s)" % (net, float(cfg["min_reward_risk"]), rr,
                                              fmt(order["gain_at_target_pkr"]), fmt(order["loss_at_stop_pkr"])))
    if trades_today >= int(cfg["max_trades_per_day"]):
        bad.append("trades: %d already today, your max is %d" % (trades_today, int(cfg["max_trades_per_day"])))
    if -pnl_today + order["loss_at_stop_pkr"] > float(cfg["max_daily_loss_pkr"]) + 0.005:
        bad.append("daily loss: today's PKR %s loss plus this stop would pass your PKR %s daily max"
                   % (fmt(max(0, -pnl_today)), fmt(cfg["max_daily_loss_pkr"])))
    if order["value_pkr"] > float(cfg["account_pkr"]) * float(cfg["max_position_pct"]) / 100.0 + 0.005:
        bad.append("position: over %s%% of the account" % cfg["max_position_pct"])
    if plan.get("upper_limit") is not None and t > plan["upper_limit"]:
        bad.append("price limit: the target %.2f is above today's upper limit %.2f" % (t, plan["upper_limit"]))
    day = datetime.date.fromisoformat(plan["date"])
    sess = session(cfg, day)
    if now.date() != day:
        bad.append("stale: the plan is for %s, today is %s" % (plan["date"], now.date().isoformat()))
    elif sess and of_day(now) >= last_entry(cfg, sess):
        bad.append("window: past the last entry time, %s" % clock(last_entry(cfg, sess)))
    return ("BLOCK" if bad else "PASS"), bad


def ticket(order):
    return ("BUY %d %s, limit %.2f. Right after it fills: SELL %d %s with a stop-loss at %.2f, and a limit SELL at "
            "%.2f for the target (or watch it and sell yourself). Sell anything still open by %s."
            % (order["qty"], order["symbol"], order["limit"], order["qty"], order["symbol"], order["stop"],
               order["target"], order["flat_by"]))


# ---------------------------------------------------------------- files and printing

def fmt(x):
    return "{:,.0f}".format(float(x)) if float(x) == int(float(x)) else "{:,.2f}".format(float(x))


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def write_json(path, doc):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, default=str)
        fh.write("\n")
    os.replace(tmp, path)


def load_config(path):
    cfg = dict(DEFAULTS)
    user = read_json(path, {}) or {}
    cfg.update(user)
    return cfg


def out(a, name):
    return os.path.join(a.dir, name)


def is_stock(sym, desc):
    d = desc.upper()
    return bool(sym) and " " not in sym and "INDEX" not in d and "ETF" not in d and "NEWS" not in d \
        and not sym.endswith("ETF")


def read_symbols(a, feed):
    if getattr(a, "symbols", None):
        return [s.strip().upper() for s in a.symbols.replace(" ", ",").split(",") if s.strip()]
    if getattr(a, "watchlist", False):
        wl = read_json(out(a, "watchlist.json"))
        if not wl:
            raise PSXError("no %s yet: run `psx.py daily` first, or pass --symbols" % out(a, "watchlist.json"))
        return [h["symbol"] for h in wl["hits"]]
    doc = read_json(out(a, "symbols.json"))
    if not doc:
        doc = cmd_symbols(a, feed, quiet=True)
    return [s for s, d in sorted(doc["symbols"].items()) if is_stock(s, d)]


def say(a, msg):
    if not getattr(a, "quiet", False):
        print(msg, file=sys.stderr)


# ---------------------------------------------------------------- commands

def cmd_symbols(a, feed, quiet=False):
    syms = {}
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789":
        for x in feed.search(ch):
            s = (x.get("symbol") or "").strip()
            if s:
                syms[s] = (x.get("description") or "").strip()
    stocks = sum(1 for s, d in syms.items() if is_stock(s, d))
    doc = {"as_of": now_pkt(a.now).isoformat(timespec="minutes"), "source": FEED, "symbols": syms}
    write_json(out(a, "symbols.json"), doc)
    if not quiet:
        print("PSX symbols from SCS Trade: %d in all, %d stocks (indexes and ETFs left out of scans)." % (len(syms), stocks))
        print("Wrote %s." % out(a, "symbols.json"))
    return doc


def cmd_daily(a, feed, cfg):
    now = now_pkt(a.now)
    syms = read_symbols(a, feed)
    if a.limit:
        syms = syms[:a.limit]
    frm = int((now - datetime.timedelta(days=120)).timestamp())
    series, failed = {}, 0
    say(a, "Fetching daily bars for %d PSX symbols..." % len(syms))
    for i, s in enumerate(syms, 1):
        try:
            series[s] = daily(feed.history(s, "D", frm, int(now.timestamp())), now, cfg)
        except PSXError:
            failed += 1
        if i % 100 == 0:
            say(a, "  %d of %d" % (i, len(syms)))
    hits, c, last = scan_daily(series, float(a.min_price), float(a.min_value))
    doc = {"mode": "daily", "as_of": now.isoformat(timespec="minutes"), "closes_through": str(last),
           "requested": len(syms), "failed": failed, "counts": c, "hits": hits}
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        print("PSX daily scan, %s Karachi time, closes through %s (SCS Trade daily bars)."
              % (now.strftime("%a %Y-%m-%d %H:%M"), last))
        print("Fetched %d of %d charts (%d failed or had no data). %d passed the liquidity filter "
              "(price PKR %s+ and PKR %sM+ traded a day); %d scanned."
              % (c["fetched"], len(syms), len(syms) - c["fetched"], c["fetched"] - c["illiquid"],
                 fmt(a.min_price), fmt(a.min_value / 1e6), c["scanned"]))
        per = {k: sum(1 for h in hits if k in h["setups"]) for k in SETUP_NAMES}
        print("%d charts matched a setup: %s." % (len(hits), ", ".join("%d %s" % (per[k], SETUP_NAMES[k]) for k in SETUP_NAMES)))
        print("Most traded first (PKR traded a day, 20-day average):")
        for h in hits[:a.show]:
            print("  %-8s PKR %9s  RSI %5s  %-14s  %s" % (h["symbol"], "{:,.2f}".format(h["close"]),
                  h["rsi"] if h["rsi"] is not None else "-", "PKR %sM" % fmt(round(h["avg_value_pkr"] / 1e6, 1)),
                  ", ".join(SETUP_NAMES[t] for t in h["setups"])))
        if len(hits) > a.show:
            print("  ... and %d more in the watchlist." % (len(hits) - a.show))
    if not a.no_write:
        write_json(out(a, "watchlist.json"), doc)
        say(a, "Wrote %s." % out(a, "watchlist.json"))
    print("A scan is a list of charts that match a rule, not a list of stocks to buy. Not financial advice.")
    return 0


def cmd_orb(a, feed, cfg):
    now = now_pkt(a.now)
    day, sess, cutoff = day_window(now, cfg, a.at)
    syms = read_symbols(a, feed)
    if a.limit:
        syms = syms[:a.limit]
    when = min(now, datetime.datetime.combine(day, datetime.time(0), PKT) + datetime.timedelta(minutes=cutoff))
    rows, counts = [], {"usable": 0, "unusable": 0, "empty": 0, "failed": 0}
    say(a, "Fetching today's 5-minute bars for %d symbols..." % len(syms))
    for i, s in enumerate(syms, 1):
        try:
            bars = fetch_today(feed, s, day, when, cfg)
        except PSXError:
            counts["failed"] += 1
            continue
        if not bars:
            counts["empty"] += 1
            continue
        o = orb(bars, sess, cutoff, cfg)
        if o["status"] == "unusable":
            counts["unusable"] += 1
            continue
        counts["usable"] += 1
        if o["status"] == "triggered":
            vol = sum(b["v"] * b["c"] for b in bars.values() if b["m"] + BAR <= cutoff)
            rows.append(dict(o, symbol=s, value_pkr=round(vol)))
        if i % 50 == 0:
            say(a, "  %d of %d" % (i, len(syms)))
    rows.sort(key=lambda r: -r["value_pkr"])
    if counts["usable"] == 0 and counts["empty"] == len(syms) - counts["failed"]:
        print("No bars at all today for these symbols: a PSX holiday, or the feed is down.")
        return 1
    doc = {"mode": "orb", "date": day.isoformat(), "cutoff": hhmm(cutoff), "counts": counts,
           "hits": [{"symbol": r["symbol"], "entry": r["entry"], "at": r["at"], "or_high": r["or_high"],
                     "or_low": r["or_low"], "pct_over": r["pct_over"], "value_pkr": r["value_pkr"]} for r in rows]}
    if a.json:
        print(json.dumps(doc, indent=2))
    else:
        print("PSX opening-range breakouts, %s, bars complete by %s Karachi time (%d-minute range)."
              % (day.strftime("%a %Y-%m-%d"), clock(cutoff), cfg["or_minutes"]))
        print("%d of %d charts usable (%d too thin: a 5-minute bar with no trades; %d with no bars today; %d failed)."
              % (counts["usable"], len(syms), counts["unusable"], counts["empty"], counts["failed"]))
        print("%d closed above their opening range. Most traded so far first:" % len(rows))
        for r in rows[:a.show]:
            print("  %-8s range %s to %s, closed %s at %s (+%.2f %%)  PKR %sM traded"
                  % (r["symbol"], "{:,.2f}".format(r["or_low"]), "{:,.2f}".format(r["or_high"]),
                     "{:,.2f}".format(r["entry"]), r["at"], r["pct_over"], fmt(round(r["value_pkr"] / 1e6, 1))))
    if not a.no_write and rows:
        write_json(out(a, "watchlist.json"), doc)
        say(a, "Wrote %s." % out(a, "watchlist.json"))
    print("A breakout is a rule that fired, not a reason to buy. Next: psx.py plan SYMBOL. Not financial advice.")
    return 0


def describe_entry(sym, o, day):
    st = o["status"]
    if st == "triggered":
        return ("%s TRIGGERED at %s: the %s bar closed %.2f, %.2f %% over the opening-range high %.2f (range %.2f to %.2f)."
                % (sym, o["at"], o["bar"], o["entry"], o["pct_over"], o["or_high"], o["or_low"], o["or_high"]))
    if st == "waiting":
        return "%s WAITING: range %.2f to %.2f; %s. Last entry %s." % (sym, o["or_low"], o["or_high"], o["reason"],
                                                                      clock(mins(o["last_entry"])))
    return "%s %s: %s." % (sym, st.upper(), o.get("reason", ""))


def cmd_entry(a, feed, cfg):
    sym = a.symbol.upper()
    while True:
        now = now_pkt(a.now)
        day, sess, cutoff = day_window(now, cfg, a.at)
        bars = fetch_today(feed, sym, day, now, cfg)
        if not bars:
            print("%s has no bars today yet: a holiday, a halt, or the feed lags." % sym)
            return 1
        o = orb(bars, sess, cutoff, cfg)
        print("%s  %s" % (now.strftime("%H:%M:%S"), describe_entry(sym, o, day)))
        if not a.watch or o["status"] != "waiting" or a.now:
            return 0
        time.sleep(max(15, a.every))


def build_plan(a, feed, cfg, now):
    sym = a.symbol.upper()
    day, sess, cutoff = day_window(now, cfg, getattr(a, "at", None))
    bars = fetch_today(feed, sym, day, now, cfg)
    if not bars:
        raise PSXError("%s has no bars today: a holiday, a halt, or the feed lags" % sym)
    o = orb(bars, sess, cutoff, cfg)
    ldcp = prev_close(feed, sym, day, cfg)
    return make_plan(sym, day, sess, o, cfg, a.stop.replace("-", "_") if not a.stop.endswith("%") else a.stop,
                     float(a.target.rstrip("Rr")), ldcp)


def print_plan(p):
    print("%s plan, %s: buy at %.2f (the %s close), stop %.2f (%s), target %.2f (%gR). Risk %.2f a share. "
          "Flat by %s." % (p["symbol"], p["date"], p["entry"], p["entry_time"], p["stop"],
                           p["stop_rule"].replace("_", "-"), p["target"], p["target_r"], p["risk_per_share"], p["flat_by"]))
    if p.get("ldcp"):
        print("  Yesterday's close %.2f; today's price limits %.2f to %.2f." % (p["ldcp"], p["lower_limit"], p["upper_limit"]))
    for n in p["notes"]:
        print("  Note: " + n)


def cmd_plan(a, feed, cfg):
    p = build_plan(a, feed, cfg, now_pkt(a.now))
    print_plan(p)
    write_json(out(a, "plan.json"), p)
    say(a, "Wrote %s." % out(a, "plan.json"))
    return 0


def print_tm(doc):
    mc, h, pool = doc["mc"], doc["history"], doc["pool"]
    p = doc["plan"]
    print("Time Machine, %s: %s replays of the rest of the day, each 5-minute bar drawn from %s's own bars at that "
          "time of day over its last %d full %s sessions (%s to %s), seed %d."
          % (p["symbol"], "{:,}".format(mc["replays"]), p["symbol"], pool["sessions"], pool["kind"], pool["from"],
             pool["to"], mc["seed"]))
    print("  target first %.1f %%, stop first %.1f %%, still open at %s %.1f %%. Average %+.3fR."
          % (100 * mc["p_target"], 100 * mc["p_stop"], mc["until"], 100 * mc["p_neither"], mc["avg_R"]))
    print("  Fees: about %.2fR a round trip, so the average after fees is %+.3fR."
          % (mc["fee_R"], mc["avg_R_after_fees"]))
    print("  Close at %s: median %.2f, 90 %% of replays between %.2f and %.2f."
          % (mc["until"], mc["median_close"], mc["p5_close"], mc["p95_close"]))
    if h["triggered"]:
        print("  The same rule on those %d real sessions: it fired %d times, %d hit the target, %d the stop, %d neither "
              "(average %+.3fR)." % (h["sessions"], h["triggered"], h["target"], h["stop"], h["close"], h["avg_R"]))
    else:
        print("  The same rule never fired on those %d sessions." % h["sessions"])
    print("Verdict: " + doc["verdict"])
    for n in doc["notes"]:
        print("  " + n)
    print("A simulation from the last %d sessions, not a prediction: news, a result announcement or a price limit "
          "can do what those days never did." % pool["sessions"])


def cmd_timemachine(a, feed, cfg):
    plan = read_json(out(a, "plan.json"))
    if not plan:
        raise PSXError("no %s: run `psx.py plan SYMBOL` first" % out(a, "plan.json"))
    doc = time_machine(feed, plan, now_pkt(a.now), cfg, a.replays, a.seed)
    if a.json:
        print(json.dumps({k: v for k, v in doc.items()}, indent=2, default=str))
    else:
        print_tm(doc)
    write_json(out(a, "timemachine.json"), doc)
    return 0


def print_order(o, cfg):
    print("Size: %d %s at %.2f = PKR %s (limited by your %s; lot size %d)."
          % (o["qty"], o["symbol"], o["limit"], fmt(o["value_pkr"]), o["limited_by"], o["lot_size"]))
    print("  At the stop %.2f: lose about PKR %s. At the target %.2f: make about PKR %s. Both after estimated fees "
          "of %s %% a side (PKR %s); taxes not included."
          % (o["stop"], fmt(o["loss_at_stop_pkr"]), o["target"], fmt(o["gain_at_target_pkr"]),
             cfg["fee_pct_per_side"], fmt(o["fees_est_pkr"])))


def cmd_size(a, feed, cfg):
    plan = read_json(out(a, "plan.json"))
    if not plan:
        raise PSXError("no %s: run `psx.py plan SYMBOL` first" % out(a, "plan.json"))
    o = size_order(plan, cfg)
    print_order(o, cfg)
    write_json(out(a, "orders.json"), {"orders": [o]})
    return 0


def cmd_check(a, feed, cfg):
    plan = read_json(out(a, "plan.json"))
    od = read_json(out(a, "orders.json"))
    if not plan or not od:
        raise PSXError("run `psx.py plan SYMBOL` and `psx.py size` first")
    o = od["orders"][0]
    verdict_, why = risk_check(o, plan, cfg, now_pkt(a.now), a.trades_today, a.pnl_today)
    print("%s %s" % (verdict_, o["symbol"]))
    for w in why:
        print("  " + w)
    if verdict_ == "PASS":
        print("Ticket, for YOU to place in your own broker's app, only if you decide to:")
        print("  " + ticket(o))
        print("Nothing has been placed. If your broker has no bracket orders, enter the stop-loss yourself right "
              "after the buy fills. Not financial advice.")
    return 0 if verdict_ == "PASS" else 2


def cmd_run(a, feed, cfg):
    now = now_pkt(a.now)
    p = build_plan(a, feed, cfg, now)
    print_plan(p)
    write_json(out(a, "plan.json"), p)
    print()
    doc = time_machine(feed, p, now, cfg, a.replays, a.seed)
    print_tm(doc)
    write_json(out(a, "timemachine.json"), doc)
    print()
    o = size_order(p, cfg)
    print_order(o, cfg)
    write_json(out(a, "orders.json"), {"orders": [o]})
    print()
    v, why = risk_check(o, p, cfg, now, a.trades_today, a.pnl_today)
    print("Risk check: %s" % v)
    for w in why:
        print("  " + w)
    if v == "PASS":
        print("Ticket, for YOU to place in your own broker's app, only if you decide to:")
        print("  " + ticket(o))
    print("Nothing has been placed. Not financial advice.")
    return 0 if v == "PASS" else 2


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(prog="psx.py", description="PSX Day Trader: scan, time, plan, replay and size "
                                 "PSX day trades on SCS Trade's live 5-minute bars. Never places an order.")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--dir", default=OUT_DIR, help="where the files go (default trader/psx)")
        p.add_argument("--config", default=None, help="config.json (default <dir>/config.json)")
        p.add_argument("--offline", default=None, help="a folder of saved feed answers instead of the network")
        p.add_argument("--now", default=None, help='pretend it is this Karachi time, "2026-11-11 10:15"')
        p.add_argument("--quiet", action="store_true")
        return p

    common(sub.add_parser("symbols", help="every PSX symbol -> symbols.json"))
    for name in ("daily", "orb"):
        p = common(sub.add_parser(name, help="daily setups" if name == "daily" else "opening-range breakouts"))
        g = p.add_mutually_exclusive_group()
        g.add_argument("--symbols", help="comma-separated tickers")
        g.add_argument("--watchlist", action="store_true", help="the hits in <dir>/watchlist.json")
        p.add_argument("--limit", type=int, default=0)
        p.add_argument("--show", type=int, default=30)
        p.add_argument("--json", action="store_true")
        p.add_argument("--no-write", action="store_true")
        if name == "daily":
            p.add_argument("--min-price", type=float, default=None)
            p.add_argument("--min-value", type=float, default=None, help="PKR traded a day, 20-day average")
        else:
            p.add_argument("--at", default=None, help="cut the clock at HH:MM Karachi time")
    p = common(sub.add_parser("entry", help="waiting / triggered / expired"))
    p.add_argument("symbol")
    p.add_argument("--at", default=None)
    p.add_argument("--watch", action="store_true", help="check again every --every seconds until it triggers")
    p.add_argument("--every", type=int, default=30)
    for name in ("plan", "run"):
        p = common(sub.add_parser(name, help="entry, stop, target" if name == "plan" else "the whole pipeline"))
        p.add_argument("symbol")
        p.add_argument("--at", default=None)
        p.add_argument("--stop", default="or-mid", help="or-mid (default), or-low, or a percent like 1.5%%")
        p.add_argument("--target", default="1R", help="a multiple of the risk, like 1R or 1.5R")
        if name == "run":
            p.add_argument("--replays", type=int, default=10000)
            p.add_argument("--seed", type=int, default=19)
            p.add_argument("--trades-today", type=int, default=0)
            p.add_argument("--pnl-today", type=float, default=0.0)
    p = common(sub.add_parser("timemachine", help="10,000 replays + the history"))
    p.add_argument("--replays", type=int, default=10000)
    p.add_argument("--seed", type=int, default=19)
    p.add_argument("--json", action="store_true")
    common(sub.add_parser("size", help="shares for your max loss"))
    p = common(sub.add_parser("check", help="PASS / BLOCK and the ticket"))
    p.add_argument("--trades-today", type=int, default=0)
    p.add_argument("--pnl-today", type=float, default=0.0, help="today's realized P&L in PKR (a loss is negative)")

    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    cfg = load_config(a.config or os.path.join(a.dir, "config.json"))
    if a.cmd == "daily":
        a.min_price = cfg["min_price_pkr"] if a.min_price is None else a.min_price
        a.min_value = cfg["min_avg_value_pkr"] if a.min_value is None else a.min_value
    feed = Feed(offline=a.offline, pause=0 if a.offline else 0.05)
    try:
        if a.cmd == "symbols":
            cmd_symbols(a, feed)
            return 0
        return {"daily": cmd_daily, "orb": cmd_orb, "entry": cmd_entry, "plan": cmd_plan,
                "timemachine": cmd_timemachine, "size": cmd_size, "check": cmd_check, "run": cmd_run}[a.cmd](a, feed, cfg)
    except PSXError as e:
        print(str(e), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
