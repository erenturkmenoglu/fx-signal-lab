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
sudo make backtest ARGS="--strategy rsi_reversion -p rsi_period=9 -p oversold=25 -x stop_pips=8"
sudo make backtest ARGS="--strategy session_breakout -p session=london --timeframe 15m --trades 20"
```

The download is split into jobs automatically; a year of minute bars is about 370 000 rows.
Running it again for the same range overwrites the same rows.

## In the browser: the strategy lab

The dashboard has a second page, **Strategy lab** (the address ends in `#lab`). It does what
the commands above do, from a phone or any browser:

1. **History.** If nothing is stored it offers to download the last twelve months of
   one-minute bars. Afterwards "Update to now" fetches what is new and "Get 12 months more"
   the year before the first stored bar. Any period can be typed in: when it reaches outside
   what is stored, the page says so and offers to download exactly the missing days. A run
   uses the days that are stored.
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

## In the browser: the signal board

The third page, **Signal board** (`#board`), is the simple one. It asks for a period and has
one button. Behind it every strategy runs on bars of every length (1m, 5m, 15m, 30m, 1h) with
its default parameters and the trading rules of the instrument: with fifteen strategies, 71
methods. The run happens in a process of its own and goes on if the page is closed; the last
finished board stays on the server until the application restarts, so a phone sees what a
desktop ran. A year of minute bars takes about ten seconds.

- **How each method did.** One line per method: net result, how many trades *worked* (closed
  with a profit after the spread) and how many *failed*, the result on the earlier and on the
  later days of the period separately, winning and losing days, worst day, largest loss.
  A switch shows the same signals traded with one clip only. `RAND` lines enter at random:
  they are what the trading rules make on their own.
- **Day by day.** For every trading day: which method made the most (known only afterwards),
  how many methods made and lost money, and for the methods ticked in the table above the
  trades that worked and failed and the result of the day.
- **Every signal.** A price chart of one day, one week, two weeks or a month ending with the
  day clicked, and under it one lane per strategy. A triangle marks a signal that was traded
  (up: buy, down: sell; teal: its trade made money, red: it lost), a small tick one that was
  not (under the line: buy, over it: sell). Pointing at a bar names the signals of that
  moment; clicking it lists them with the reason each was or was not traded.

Three things on that page are there because a board invites a particular mistake:

1. **The best of 71 looks good by chance.** On prices that are pure noise, a month of this
   board shows 10 of the 66 real methods above zero and the best one at about +2 000 USD;
   over a quarter one is left, over a year none. (With the first rule set, which added to
   positions, the same month showed half of them above zero and the best at +18 000: many
   small wins, the rare large loss not yet arrived.) Read the later-days column and a
   longer period before believing a line.
2. **The best method of a day is hindsight.** Under the day table the page tries the obvious
   use of it: each day trade the method that was best the day before. If that does no better
   than any method on any day, the table shows luck moving around.
3. **Random entries are on the board.** A method that cannot beat them has no information.

The commands behind the page are `fxlab board --request -` and `fxlab signals --request -`
(JSON on standard input, JSON out), so the page and a terminal cannot disagree.

A result stays on its page while another page is looked at: the strategy lab keeps its last
report and the board its tables and chart until the browser tab is closed or reloaded. The
last board is also kept by the server, so it is there again after a reload.

## What is tested on what

| | |
|---|---|
| Prices | the provider's historical aggregate bars (`source = massive_agg`), which Massive builds from **bid** quotes. Never the live series, never simulated data. |
| Bars the strategy reads | `--timeframe` (default: the strategy's usual one, 5m). Built from the base bars, aligned to the UTC clock. |
| Bars the trades are simulated on | `--base` (default 1m). Finer bars decide more exactly what happened first. |
| Costs | spread, slippage and commission from `config/instruments.yaml`, changeable per run. |

For EUR/USD the owner's assumptions of 15 November 2026 are: the trading venue quotes about
0.5 pips and charges no commission; tests assume a **1.0 pip** spread to stay on the safe
side; a signal is only taken when it sees **at least 5 pips** of profit after that spread; an
order is **1 mn EUR**; the profit is taken at **5 pips** and a trade that goes **5 pips**
the other way is closed. A position is **one order**: nothing is added to it. (The first rule
set of that day allowed up to five clips, added as the price went against the position. What
that did to the results is the section "What adding to a position does to the numbers".)
These rules are the `rules` of the instrument in `config/instruments.yaml`.

## How a trade is simulated

Every rule errs against the strategy when the data cannot decide.

0. **Indicators are settled before the period begins.** A run reads the bars of the three
   weeks before the period too (as far as they are stored) and lets every strategy and the
   engine work through them. Only signals known inside the period, and their trades, are
   reported. Without this the first days of a period would have fewer signals than the same
   days in the middle of a longer one (a MACD on hourly bars needs more than a day before it
   says anything), and a short period would be unfair to the methods that look far back.
   When less than five days are stored before a period the report says so.
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

With one clip (the default) a trade ends 5 pips up or 5 pips down, 500 USD either way. With
a ladder of five clips five pips apart and the stop ten pips beyond the last of them (the
first rule set, `-x max_clips=5 -x stop_pips=10`) a long position looks like this (prices
relative to the first clip, in pips; one pip on one clip is 100 USD):

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
| `max_clips` | 1 | 1 = never add to a position |
| `add_step_pips` | 5 | distance between clips (only with more than one) |
| `target_pips` | 5 | profit target from the average entry |
| `stop_pips` | 5 | stop beyond the last possible clip; with one clip: beyond the entry |
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

| key (short name) | family | buys / sells when | "potential" it reports |
|---|---|---|---|
| `rsi_reversion` (RSI) | reversion | the RSI enters the oversold / overbought zone (or, with `trigger=leave`, comes back out of it) | distance back to a moving average |
| `bollinger_reversion` (BB) | reversion | a bar closes below the lower / above the upper band | distance to the middle band |
| `stochastic_reversion` (STO) | reversion | %K crosses above %D below the oversold line / below %D above the overbought line | distance back to the middle of the recent range |
| `cci_reversion` (CCI) | reversion | the CCI comes back up through -100 / down through +100 | distance back to the moving average of the typical price |
| `ema_momentum` (EMA) | trend | the fast EMA crosses above / below the slow one, optionally only while the ADX shows a trend | a multiple of the average true range |
| `macd_cross` (MACD) | trend | the MACD line crosses above / below its signal line | a multiple of the average true range |
| `dmi_cross` (DMI) | trend | +DI crosses above / below -DI while the ADX is above a threshold | a multiple of the average true range |
| `supertrend_flip` (ST) | trend | the Supertrend line flips from above the price to below it / the other way | a multiple of the average true range |
| `session_breakout` (SB) | breakout | the first close above / below the range of the hours before London, New York or Tokyo opens | height of that range |
| `donchian_breakout` (DON) | breakout | a close above the highest high / below the lowest low of the last N bars | height of that channel |
| `squeeze_breakout` (SQZ) | breakout | a quiet stretch ends (the Bollinger bands had been inside the Keltner channel): the close is then above / below the average | a multiple of the average true range |
| `pivot_reversion` (PIV) | level | a bar dips to the support (S1) worked out from the day before and closes back above it / pokes above the resistance (R1) and closes back below | distance to the pivot |
| `round_number` (RND) | level | the price comes to a round number (every 50 pips) from above / below and is turned away (`mode=fade`), or closes through it (`mode=break`) | a parameter |
| `session_drift` (TOD) | time | never looks at the price: sells at the European open, buys at the New York open | the move expected, a parameter |
| `random_entry` (RAND) | benchmark | at random moments, in a random direction | none: it passes every filter |

`random_entry` is not a strategy. It is the line the others are measured against: what the
target, the stop, the adding and the costs produce when the entries carry no information.

Signals are events, not states: an RSI that stays oversold for an hour signals once.

The potential is what decides whether a signal is worth its cost (`min_potential_pips`);
the exit is the engine's target and stop, the same for every strategy.

### Where these come from, and what is known about them

The indicators are the ones every charting package has; they are here because they are what
practitioners actually look at, not because any of them is known to work. Four findings from
the academic literature shaped the list and how to read the results:

- **Intraday rules find patterns but not profits after costs.** Neely and Weller (2003,
  *Journal of International Money and Finance* 22(2), 223-237,
  [doi:10.1016/S0261-5606(02)00101-8](https://doi.org/10.1016/S0261-5606(02)00101-8)) chose
  intraday trading rules with a genetic program and with an optimized linear forecasting
  model and tested them out of sample. With realistic transaction costs and trading hours
  they found no evidence of excess returns, although the rules did discover stable patterns
  in the data. This is the prior for everything on the board.
- **Tested on many rules at once, with a correction for it.** Hsu, Taylor and Wang (2016,
  *Journal of International Economics* 102, 188-208,
  [doi:10.1016/j.jinteco.2016.03.012](https://doi.org/10.1016/j.jinteco.2016.03.012)) ran
  more than 21 000 rules on daily data of 30 currencies over 45 years, with a stepwise test
  against data snooping. They report predictability and excess profitability that vary
  between sub-periods and groups of currencies; summaries of the paper put the end of
  predictability in developed-market currencies in the 1990s. Their method is the reason
  this page warns about the best of 71: a result has to survive the number of things tried.
  Their data are daily; nothing in that paper speaks for rules on minute bars.
- **Round numbers.** Osler (2003, *Journal of Finance* 58(5), 1791-1819,
  [doi:10.1111/1540-6261.00588](https://doi.org/10.1111/1540-6261.00588)) looked at the
  stop-loss and take-profit orders of a large dealing bank: take-profit orders cluster *at*
  round numbers, stop-loss orders just *beyond* them. That gives a reason for both a fade
  and a break at such levels, which is what `round_number` tests.
- **Time of day.** Ranaldo (2009, *Journal of Banking & Finance* 33(12), 2199-2206;
  [Swiss National Bank working paper 2007-03](https://www.snb.ch/en/publications/research/working-papers/2007/working_paper_2007_03))
  reports that currencies tend to lose value during their own country's working hours and
  gain during the other country's. `session_drift` is that finding as a rule; it is the only
  method here that does not look at the price at all.

Not on the list, and why: Williams %R is the stochastic %K upside down; VWAP, on-balance
volume and money flow need traded volume, which spot FX quotes do not have (the rule against
inventing volume applies); Ichimoku and Parabolic SAR can be added the same way as the rest.

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
- **No adding** (shown when a run allows more than one clip). The same signals traded with
  one clip. Adding to losing positions raises the
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

The first rule set bought more as the price fell: up to five clips, the profit taken 5 pips
above the average entry, the stop 10 pips below the last clip. Its results looked a
particular way, which the owner noticed on the first real look: the trades that worked made
a little, the trades that failed lost a lot. That is not something the market did. It is
what those rules do on any prices:

- a trade that works makes 500 USD (one clip) to 2 500 USD (five clips);
- a trade that reaches the stop has bought all five clips by then and loses 10 000 USD;
- so one stop wipes out twenty one-clip wins, and the position has to come back more than
  four times out of five *from 20 pips under water* just to break even. A market that
  starts to trend (a rate decision, a political surprise: a new story) produces exactly
  the loser, and the rule keeps buying into it.

Measured on a year of prices that are pure noise (a random walk; not market data), all
methods together:

| rules | trades that worked | average win | average loss | largest loss | per trade |
|---|---|---|---|---|---|
| five clips, stop 10 beyond the last | 75.7% | +779 | -3 666 | -10 000 | -295 |
| one clip, stop 10 | 57.6% | +490 | -907 | -1 000 | -101 |
| **one clip, stop 5 (the rules now)** | 39.9% | +494 | -494 | -500 | -100 |
| one clip, target 10, stop 5 | 28.5% | +900 | -492 | -500 | -95 |

Two things to read from it. Adding bought a high share of winning trades with a tail of
large losses and three times the cost per trade. And a stop does not change what a trade
is worth on average: on noise every one-clip line loses the spread, 100 USD, whatever the
stop and the target. A nearer stop makes losses small and frequent instead of large and
rare. What can change the average is the entry: taking fewer of the trades that fail,
which is why the reasons for failing are worth measuring.

With the stop as near as the target a method has to be right more than half the time after
the spread. On noise it is right four times in ten.

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
  the spread and nothing else. Measured over 2 300 to 5 200 trades per strategy for the first
  four: between -0.11 and +0.06 pips per clip without a spread, between -1.18 and -0.85 with
  a spread of 1.0. A look-ahead error anywhere between the bars and the trade list shows up
  here as profit from noise; two such errors planted on purpose moved the figure by 1.5 to
  3.5 pips. All fifteen strategies pass this test, each over more than 2 000 trades.
- **The board against the single backtest, and the chart against the board.** Each line of a
  board must be the backtest of that method, number for number; the trades drawn in a window
  must be the trades the board counts for those days; a period reported with a run-in must
  be the later part of the longer report.
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
- **Indicators need history.** A run reads three weeks before its period for this. At the
  very beginning of the stored history there is nothing to read: the first days after the
  first stored bar have fewer signals, and the report warns about it.
- **The board uses default parameters.** It compares methods as they come, which is the
  honest way to look at many of them at once; tuning one belongs in the strategy lab, on the
  in-sample part. The last board is kept in memory only: after a restart it is run again.
- **A board is many tests at once.** Nothing here corrects for that beyond the earlier/later
  split and the random entries; a formal correction (as in Hsu, Taylor and Wang) is not built.
- **Live signals will use the same functions** (step 4 of the roadmap). Live bars are built
  from mid prices and backtest bars from bids; that difference has to be handled there.

## Adding a strategy

1. Write a function `(bars, params, pip_size) -> Signals` in `research/strategies.py`. The
   value for bar `i` may use bars `0..i` only.
2. Register it in `STRATEGIES` with its parameters (name, label, default, range).
3. Add it to nothing else: `tests/unit/test_research_strategies.py` runs the look-ahead test
   on every registered strategy. Add a test with bars written by hand for what it should do.
