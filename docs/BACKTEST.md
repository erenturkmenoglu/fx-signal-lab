# Backtesting

A backtest answers one question: *had this rule been followed on past prices, under these
costs, what would have happened?* It is not a forecast. No strategy in this repository is
known to be profitable, and a good backtest is where the work starts, not where it ends.

The system only produces signals and reports. It places no orders.

## Quick start

On the server (the application keeps running; all of this only reads prices and writes bars):

```bash
# 1. download history once: one-minute bars, as far back as you want to test
sudo make history ARGS="--symbol EURUSD --timeframe 1m --start 2025-11-01 --end 2026-11-14"

# 2. what can be tested
sudo make strategies

# 3. run one (no dates = everything stored), with the last ten days listed
sudo make backtest ARGS="--strategy rsi_reversion --days 10"

# change a strategy parameter (-p) or a trading assumption (-x)
sudo make backtest ARGS="--strategy rsi_reversion -p rsi_period=9 -p oversold=25 -x max_clips=1"
sudo make backtest ARGS="--strategy session_breakout -p session=london --timeframe 15m --trades 20"
```

The download is split into jobs automatically; a year of minute bars is about 370 000 rows.
Running it again for the same range overwrites the same rows.

## In the browser: the strategy lab

The dashboard has a second page, **Strategy lab** (the address ends in `#lab`). It does what
the commands above do, from a phone or any browser:

1. **History.** If nothing is stored it offers to download the last twelve months of
   one-minute bars; afterwards "Update to now" fetches what is new.
2. **The form.** Strategy and its parameters, the bars it reads, the period, how the period is
   split, and every trading assumption from the table below. What was last entered is kept in
   the browser.
3. **Run backtest.** One run at a time; a year of minute bars takes a few seconds. The run
   happens in a process of its own (the same `fxlab backtest` command), so the live feed is
   not disturbed.
4. **The result.**
   - the table of totals: whole period, in-sample, out-of-sample, and one clip only;
   - the running total by trading day, with the one-clip-only line next to it and the result
     of every day below; clicking a day selects it;
   - the price chart of the selected day (the stored bid bars) with every entry (arrow), clip
     added (+n), target (circle), stop (square) and exit by the clock marked on it; clicking a
     trade in the list draws its average entry, target and stop as lines and zooms to it;
   - the days, the trades, how trades ended, results by hour of entry, and why signals were
     not traded.

## What is tested on what

| | |
|---|---|
| Prices | the provider's historical aggregate bars (`source = massive_agg`), which Massive builds from **bid** quotes. Never the live series, never simulated data. |
| Bars the strategy reads | `--timeframe` (default: the strategy's usual one, 5m). Built from the base bars, aligned to the UTC clock. |
| Bars the trades are simulated on | `--base` (default 1m). Finer bars decide more exactly what happened first. |
| Costs | spread, slippage and commission from `config/instruments.yaml`, changeable per run. |

For EUR/USD the assumptions agreed on 15 November 2026 are: the trading venue quotes about
0.5 pips and charges no commission; tests assume a **1.0 pip** spread to stay on the safe
side; a signal is only taken when it sees **at least 5 pips** of profit after that spread; a
clip is **1 mn EUR**; a position may be built up to **5 clips**.

## How a trade is simulated

Every rule errs against the strategy when the data cannot decide.

1. **A signal belongs to a closed bar.** The strategy sees bars up to and including the one
   that just closed, nothing later. The tests cut every price series short at many points and
   require all earlier answers to stay identical.
2. **The order is filled later, at a real price.** At the open of the first base bar that
   starts at or after *bar close + reaction delay* (default 5 s). With one-minute base bars
   that is one minute after the bar closed; with a delay of 0 it is the first price after the
   close. Never inside the bar that gave the signal.
3. **Spread.** A buy pays the ask (bid + spread) and is sold at the bid; a sell is made at the
   bid and bought back at the ask.
4. **Distances are net.** Target, stop and the step between clips are measured between prices
   actually traded. A 5 pip target on a 1 mn EUR position is 500 USD after the spread.
5. **One position at a time, built from clips.** When the price goes against the position by
   `add_step_pips` from the last clip, another clip is added, up to `max_clips`.
6. **Exits, always of the whole position:**
   - *target*: `target_pips` beyond the average entry; it moves closer each time a clip is added;
   - *stop*: `stop_pips` beyond the last clip that could be added; fixed when the position opens;
   - *time*: after `max_hold_minutes`;
   - *end of day*: at `flat_time` (16:45 New York for spot FX, a quarter of an hour before the
     trading day ends). Nothing is opened between that time and the end of the day.
7. **Inside one bar the worse order of events is assumed:**
   - stop and target both touched: the stop came first;
   - the bar reaches the stop: every remaining clip was bought on the way down;
   - target and a clip level both touched: the target came first, with the smaller position;
   - a clip is added: the nearer target it creates only counts from the next bar on;
   - a bar that opens beyond the stop is closed at its open, however far that is;
   - a target is filled at its level, never better.

With the default settings a long position looks like this (prices relative to the first
clip, in pips; one pip on one clip is 100 USD):

| clips | bought at | average | target (bid) | profit at target | loss at the stop (-30) |
|---|---|---|---|---|---|
| 1 | 0 | 0 | +5 | 500 | - |
| 2 | 0, -5 | -2.5 | +2.5 | 1 000 | - |
| 3 | ... -10 | -5 | 0 | 1 500 | - |
| 4 | ... -15 | -7.5 | -2.5 | 2 000 | - |
| 5 | ... -20 | -10 | -5 | 2 500 | **-10 000** |

The stop always costs the full ladder: by the time the price is there, all five clips have
been bought.

## Trading assumptions (`-x name=value`)

| name | default | meaning |
|---|---|---|
| `spread_pips` | 1.0 | bid/ask spread charged on every round trip |
| `slippage_pips` | 0 (EUR/USD) | extra loss on stops and timed exits (market orders); targets are limit orders |
| `commission_per_million` | 0 | quote currency per 1 mn base, per side |
| `min_potential_pips` | 5 | a signal must see at least this much after the spread |
| `clip_units` | 1 000 000 | size of one clip in base currency |
| `max_clips` | 5 | 1 = never add to a position |
| `add_step_pips` | 5 | distance between clips |
| `target_pips` | 5 | profit target from the average entry |
| `stop_pips` | 10 | stop beyond the last possible clip |
| `max_hold_minutes` | 240 | longest time in a position |
| `cooldown_minutes` | 5 | pause after a position is closed |
| `reaction_delay_s` | 5 | time between the signal and the order |
| `entry_start_utc`, `entry_end_utc` | 06:00, 20:00 | hours (UTC) in which positions are opened |
| `flat_time` | 16:45 | everything is closed at this time (instrument's session time zone) |
| `long`, `short` | true | switch a direction off |

`pip_size`, the session time zone and the end of the trading day come from the instrument
and cannot be changed for a run.

## Strategies

`fxlab strategies` prints them with every parameter and its range. Each one is a pure
function from bars to signals in `backend/fxlab/research/strategies.py`.

| key | buys / sells when | "potential" it reports |
|---|---|---|
| `rsi_reversion` | the RSI enters the oversold / overbought zone (or, with `trigger=leave`, comes back out of it) | distance back to a moving average |
| `bollinger_reversion` | a bar closes below the lower / above the upper band | distance to the middle band |
| `ema_momentum` | the fast EMA crosses above / below the slow one, optionally only while the ADX shows a trend | a multiple of the average true range |
| `session_breakout` | the first close above / below the range of the hours before London, New York or Tokyo opens | height of that range |

Signals are events, not states: an RSI that stays oversold for an hour signals once.

The potential is what decides whether a signal is worth its cost (`min_potential_pips`);
the exit is the engine's target and stop, the same for every strategy.

## Reading the report

```
(money in USD)                   Whole period      In-sample  Out-of-sample      No adding
Trades                                    330            226            104            376
Net result                            -92,570        -49,460        -43,110        -41,880
Winning trades                          67.3%          65.5%          71.2%          55.6%
Profit factor                            0.63           0.68           0.55           0.70
Largest loss                          -10,000        -10,000        -10,000         -1,000
```

(These numbers are from random prices, where every strategy must lose its costs. They are an
example of the layout, nothing else.)

- **In-sample / out-of-sample.** The period is cut in two: by default the last 3 trading days
  in 10 are kept back (`--split 2026-09-01` sets the day, `--split none` switches it off).
  Choose parameters looking at the in-sample column only; the out-of-sample column is the
  check. Every time a parameter is changed after looking at the out-of-sample column, that
  column becomes a little more in-sample. A result that exists only in-sample is a result of
  the choosing.
- **No adding.** The same signals traded with one clip. Adding to losing positions raises the
  share of winning trades and makes the rare loss several times larger; this column shows how
  much of a result is the signals and how much is the adding.
- **Do not judge by the share of winning trades.** Read profit factor (money won per unit of
  money lost), average per trade, largest loss and largest fall from a high together.
- **Signals.** Why signals were not traded. A result built on few trades is an anecdote; the
  report says so below 30.
- **Days.** A day is a trading day (it ends at 17:00 New York) and a trade belongs to the
  day it was closed in. Days without a trade count as zero in the daily statistics.
- **Sharpe.** Mean daily result over its day-to-day spread, scaled to a year (x sqrt 252).
  Not reported for fewer than 20 days.

`--json` prints everything: the resolved request, every trade with its fills, the daily
table, the running total.

## What adding to a position does to the numbers

With the defaults, a full ladder that works makes 2 500 USD and one that fails loses 10 000.
Four full winners pay for one full loser; the position needs to come back more than four
times out of five *from 20 pips under water* just to break even. Markets that trend for a day
produce exactly the loser. The backtest takes the stop seriously (it is always filled, at
the stop or worse) so that this shows in the numbers; a ladder without a stop has no largest
loss to show, which is not the same as not having one.

## How the engine itself is tested

- **By hand.** Thirty-five tests on bars written out by hand, with every fill and
  the money worked out in comments: both directions, every kind of exit, every rule about
  what happens inside one bar.
- **Against a second simulation.** On random markets with gaps and missing minutes the
  engine (array searches, floating point) must agree, fill for fill and dollar for dollar,
  with a plain bar-by-bar loop that counts in whole numbers.
- **Against deliberate defects.** Seventeen wrong versions of the engine (target before
  stop, buying at the bid, filling inside the signal bar, ...) are each caught by the examples.
- **On prices that cannot be predicted.** On driftless random walks every strategy must lose
  the spread and nothing else. Measured over 2 300 to 5 200 trades per strategy: between
  -0.11 and +0.06 pips per clip without a spread, between -1.18 and -0.85 with a spread of
  1.0. A look-ahead error anywhere between the bars and the trade list shows up here as
  profit from noise; two such errors planted on purpose moved the figure by 1.5 to 3.5 pips.
- **Look-ahead, directly.** Every indicator and every strategy is recomputed on series cut
  short at many points; nothing before the cut may change.

## Limits, and what has not been verified

- **No real history has been run through this yet.** Everything above is on hand-written
  and random prices and against a real database; the first run on Massive's EUR/USD history
  happens on the server.
- **Bid basis is taken from Massive's documentation**, not yet checked against their quote
  history. If the bars were mid prices instead, every level would shift by half a spread; the
  cost of a round trip would be the same.
- **Minute bars hide the order of events inside a minute.** The rules above always take the
  worse order, which makes results on quiet markets slightly too bad and never too good.
  Five-second history (`--timeframe 5s`, then `--base 5s`) narrows this for short periods.
- **The spread is constant.** Real spreads widen around news and at the daily rollover. The
  default entry hours and flat time keep trades away from the rollover; news is not modelled.
- **Fills are assumed at the quoted price for any size.** 5 mn EUR in EUR/USD is ordinarily
  filled at the touch; around news it is not.
- **Indicators need history.** The first bars of a period produce no signals until the
  indicators have warmed up (20 to 50 bars with the defaults).
- **Live signals will use the same functions** (step 4 of the roadmap). Live bars are built
  from mid prices and backtest bars from bids; that difference has to be handled there.

## Adding a strategy

1. Write a function `(bars, params, pip_size) -> Signals` in `research/strategies.py`. The
   value for bar `i` may use bars `0..i` only.
2. Register it in `STRATEGIES` with its parameters (name, label, default, range).
3. Add it to nothing else: `tests/unit/test_research_strategies.py` runs the look-ahead test
   on every registered strategy. Add a test with bars written by hand for what it should do.
