---
name: trader-psx
description: >-
  PSX Day Trader. The day-trader pack's method on the Pakistan Stock Exchange,
  on SCS Trade's live PSX chart feed: scans every PSX stock for daily setups,
  finds 15-minute opening-range breakouts during the Karachi session, times
  the entry bar by bar, sets the stop and target, replays the rest of the day
  10,000 times from the stock's own last 60 sessions (plus every past day the
  rule fired), sizes the trade in PKR to your max loss after broker fees, and
  checks it against your limits and PSX's daily price limits. It prints a
  ticket you place yourself in your broker's app; it never places an order.
  Use when the user says "PSX", "KSE-100", "Pakistan stock exchange",
  "Karachi", "scan PSX", "PSX breakouts", "day trade OGDC" (or any PSX
  ticker), "run the time machine on a PSX stock", or "size this PSX trade".
argument-hint: "[symbols | daily | orb | entry SYM | plan SYM | timemachine | size | check | run SYM]"
---

# trader-psx

The US skills in this pack read Yahoo Finance, which has no current PSX data
(its `.KA` charts stopped in July 2024). This skill runs the same method on
the PSX feed behind SCS Trade's real-time chart page
(https://www.scstrade.com/TechnicalAnalysis/TA_RealTimeCharting.aspx): live
1-minute and 5-minute bars about two minutes behind, about 84 sessions of
5-minute history, years of daily bars, the KSE-100 index and all ~590 PSX
symbols. Day trading loses most people money; this does not change that.

`${CLAUDE_SKILL_DIR}` below is this skill's folder (where this SKILL.md lives).

```bash
python3 "${CLAUDE_SKILL_DIR}/psx.py" symbols                  # every PSX symbol -> trader/psx/symbols.json
python3 "${CLAUDE_SKILL_DIR}/psx.py" daily                    # setups on daily closes -> trader/psx/watchlist.json
python3 "${CLAUDE_SKILL_DIR}/psx.py" daily --symbols OGDC,PPL,HUBC
python3 "${CLAUDE_SKILL_DIR}/psx.py" orb --watchlist          # breakouts so far among the daily hits
python3 "${CLAUDE_SKILL_DIR}/psx.py" orb --at 10:15 --symbols OGDC,PSO,HBL
python3 "${CLAUDE_SKILL_DIR}/psx.py" entry OGDC --watch       # bar by bar until it triggers or expires
python3 "${CLAUDE_SKILL_DIR}/psx.py" plan OGDC --stop or-mid --target 2R
python3 "${CLAUDE_SKILL_DIR}/psx.py" timemachine
python3 "${CLAUDE_SKILL_DIR}/psx.py" size
python3 "${CLAUDE_SKILL_DIR}/psx.py" check --trades-today 0 --pnl-today 0
python3 "${CLAUDE_SKILL_DIR}/psx.py" run OGDC --target 2R     # plan -> timemachine -> size -> check
```

Common flags: `--now "2026-11-11 10:30"` (Karachi time) to pin the clock,
`--offline DIR` (saved feed answers, used by the tests), `--dir` (default
`trader/psx`), `--config` (default `trader/psx/config.json`).

## Setup

Copy `config.example.json` to `trader/psx/config.json` and set your own
numbers. Ask the user for them before sizing anything:

| key | default | what |
| --- | --- | --- |
| `account_pkr` | 500,000 | the cash you trade with |
| `max_loss_per_trade_pkr` | 2,500 | the most one trade may lose at the stop, fees in |
| `max_daily_loss_pkr` | 7,500 | stop for the day after this much |
| `max_trades_per_day` | 3 | |
| `min_reward_risk` | 1.0 | checked AFTER fees |
| `max_position_pct` / `cash_buffer_pct` | 50 / 10 | |
| `fee_pct_per_side` | 0.15 | an estimate of commission plus exchange, CDC and SECP charges; set your broker's real rate. Taxes are not included |
| `lot_size` | 1 | set your broker's market lot if it is not 1 |
| `price_limit_pct` / `price_limit_min_pkr` | 10 / 1 | PSX's daily band: yesterday's close plus or minus 10 % or PKR 1, whichever is more. Check the current rule with your broker |
| `last_entry_mins_before_close` / `flat_mins_before_close` | 60 / 5 | no entries in the last hour; flat 5 minutes before the close |
| `sessions` | see the file | Karachi hours. Change them in Ramadan |

The network must allow `chart.scstrade.com`.

## The PSX session

Monday to Thursday 9:30 AM to 3:30 PM; Friday 9:15 AM to 12:00 noon and
about 2:10 PM to 4:30 PM, Karachi time (UTC+5, no daylight saving). The
closing-price session after the close (to 3:45 or 4:45) is left out. Public
holidays are not in a calendar: a weekday with no bars is reported as a
holiday or a feed problem, and the Time Machine pool simply has no bars that
day. Friday is its own kind of session: a Friday plan replays past Fridays
only, a Monday-to-Thursday plan replays past Monday-to-Thursday sessions.

## How each step works

1. **daily**: the trader-scanner rules on daily closes (20-day closing-high
   breakout, bull flag, reclaimed the 20-day average, RSI 14 under 30,
   oversold bounce), on stocks at PKR 5+ with PKR 10 million+ traded a day
   (20-day average). Indexes and ETFs are left out. Most traded first.
2. **orb**: the opening range is the first 15 minutes (three 5-minute bars).
   A hit is the first COMPLETED bar to close above the range high, before the
   last entry time. A chart is usable only when every 5-minute bar from the
   open to the cutoff has a trade: a missing bar means a thin or halted stock,
   and on PSX that rules out many small caps. Scan the daily watchlist or a
   list of names for a fast answer.
3. **entry**: the same rule on one stock: WAITING, TRIGGERED (the bar, its
   close and the time it closed) or EXPIRED.
4. **plan**: entry at the trigger close; stop at the range midpoint
   (`or-mid`, default), the range low (`or-low`) or a percent; target a
   multiple of the risk. It reads yesterday's close (LDCP) for today's price
   limits and warns when the target is above the upper limit, or when fees
   are a big share of the risk.
5. **timemachine**: 10,000 replays (seed 19) of the rest of the session,
   each 5-minute bar drawn from the stock's own bars at that time of day
   (plus or minus two bars) over its last 60 full sessions of the same kind.
   Stop and target inside one bar count as the stop. Then the same rule on
   those real sessions. It reports target first / stop first / neither, the
   average R, the average R AFTER FEES, and a verdict. Needs at least 20 full
   sessions (10 for a Friday: the feed keeps about 17 weeks of 5-minute bars,
   so about 14 Fridays, and it says the odds are rough).
6. **size**: shares = your max loss divided by (risk a share plus both
   sides' fees), capped by cash and position limits, rounded down to the lot.
7. **check**: PASS or BLOCK, with each reason: max loss, reward:risk after
   fees, trades today, daily loss, position size, the price limit, a stale
   plan, the entry window. On PASS it prints the ticket in plain words.

## Fees matter more on PSX

An opening-range stop on a large cap is often 0.3 % to 0.5 % of the price.
At 0.15 % a side, a round trip costs about 0.3 % of the price, which can be
0.7R: a 1R target then makes far less than the stop loses. On Nov 11 2026,
OGDC triggered at 10:00 AM at 318.99 with a 1.36 stop; at 1R the target
made about PKR 314 while the stop lost about PKR 1,810, so the check
BLOCKED it. At 3R it passed the rules, but the Time Machine leaned toward
the stop (the target came first in 19 % of the replays that hit a side
against the 25 % break-even) and the average after fees was negative. Say
these numbers plainly.

## What to tell the user

- The counts: fetched, usable, hits. Never round a partial scan up to "the
  whole market".
- For a plan: entry, stop, target, flat-by time, the price limits, the Time
  Machine odds and the average AFTER fees, the history, the size and the
  PKR loss at the stop.
- That the feed is free and unofficial and runs about two minutes behind:
  check the price in the broker app before acting.

## Safety

- Not financial advice. A PASS is a rules check, not a recommendation.
- This skill never places an order. There is no PSX broker connector here;
  the ticket is text. The user decides, and places it themselves in their
  own broker's app, only after they say yes to that exact trade.
- If their broker has no bracket orders, the stop-loss has to be entered by
  hand right after the buy fills. A stop is a plan, not a guarantee: a stock
  that hits its lower price limit may not trade at all.
- Long only. No short selling, margin or leverage.
- Ask for the account size and the max loss; never assume them for a real
  trade.
