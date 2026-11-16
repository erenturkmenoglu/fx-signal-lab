# Live signals

The **Live signals** page watches one rule on the live prices and marks the second at which
it turns true: a green triangle for a buy, a red one for a sell, and a line in the table
below the chart. It is the simple page: one chart of today, three trend channels, one
rule, a handful of settings.

It signals; it does nothing else. No order is sent anywhere, there is no stop and no
target, and no profit or loss is worked out. Whether the rule makes money is not known
and is not claimed (see [What is not verified](#what-is-not-verified)).

## The rule

Two indicators are read on the **bid** at the close of each bar. A bar is one second
unless the settings say otherwise.

| | Buy | Sell |
|---|---|---|
| RSI (Wilder, 14 bars) | at or below 30 | at or above 70 |
| Bollinger bands (20 bars, 2 standard deviations) | bid at or below the lower band | bid at or above the upper band |

* With **RSI and Bollinger together** (the default) both have to hold on the same bar.
  With **either one**, one of them is enough.
* A signal is raised **once**, on the bar where its condition turns from false to true,
  and not again while the condition stays true.
* Only prices that may feed signals count: fresh or carried quotes, inside market hours,
  and, while prices are arriving, only as long as the data-quality gate allows signals
  (the gate of the Live page: feed connected, data not stale, no suspected clock skew).
  A bar whose closing second fails that **ends the run**: the indicators start again
  after it and nothing is signalled until both are defined on bars of the new run alone
  (21 bars with the defaults).
* **Bands need movement.** When the price did not change at all over the bars the bands
  are built from, both bands are the price itself and which side of them the price is on
  would be decided by rounding. Such a bar meets neither band.
* Every value depends on the bars up to that bar and on nothing later.

The table says for each signal which condition held (`RSI 24.5 ≤ 30, bid ≤ lower band
1.09980`); that is the rule's own record, taken from the exact numbers and not read back
from the rounded ones on the screen.

### Settings

Under **Settings** on the page; they start at the defaults and are kept on the server.

| Setting | Default | May be |
|---|---|---|
| Bars the indicators read | 1 s | 1, 5, 15, 30 or 60 s |
| RSI period | 14 bars | 2 to 1000 |
| RSI buy at or below / sell at or above | 30 / 70 | 1 to 99, buy below sell |
| Bollinger period | 20 bars | 2 to 2000 |
| Bollinger width | 2 σ | 0.1 to 10, and below √(period − 1) |
| An order is signalled by | RSI and Bollinger together | together, or either one |
| Short, medium, long channel | 1 h, 24 h, 10 d | 0.25 to 48 h, 1 to 240 h, 1 to 30 d, each longer than the one before |
| Channel width | 2 σ | 0.5 to 5 |

The limit on the width of the bands is arithmetic: among *n* prices none can lie further
from their average than √(*n* − 1) of their standard deviations (1.73 for four prices,
4.36 for twenty). Bands as wide as that could never be met, so such settings are refused
with that explanation.

Longer bars are aligned to the clock (a 15-second bar closes at :14, :29, :44, :59) and
are judged at their close only. The chart stays on seconds whatever the bars are.

New settings apply to the whole trading day: its signals are worked out again from the
stored seconds, so the table and the triangles change with them.

## How often it signals

The textbook settings were made for daily bars. On one-second bars they fire constantly,
and they are most sensitive after a quiet spell: when a quote has not moved for a while,
the bands are narrow and the RSI's averages are small, so a single tick can satisfy both.

Counts for one trading day (86 400 seconds), default settings, on **random prices** (five
days each; these are not market data and say nothing about profit):

| Bars | A price that moves every second | A quote that changes in one second of five |
|---|---|---|
| 1 s | 1 550 to 1 850 (one every 50 s) | 3 900 to 4 000 (one every 22 s) |
| 5 s | 270 to 330 | 400 to 445 |
| 15 s | 95 to 115 (one every 13 min) | 95 to 115 |
| 30 s | 40 to 70 | 40 to 55 |
| 60 s | 15 to 35 (about one an hour) | 20 to 30 |

With "either one" on one-second bars: 4 400 to 6 300 a day. What the count is on real
quotes is not known until the page has run on them; the page shows the count and the
pace of the day at the top.

## The trend channels

Three straight channels, short, medium and long. Each is a least-squares line through the
last bars of its length, with a band of two standard deviations of the distances from the
line on either side. They are fitted to **one-minute bid closes**:

* stored one-minute bars before the seconds in memory: the provider's bars (bid bars)
  where they exist, otherwise the bid close of the bars this system built;
* the completed minutes of the seconds in memory after that.

The fit is in bars, so a weekend is no distance. A channel that finds fewer bars than its
length uses what there is and says so ("only 3.3 h of bars stored"); with fewer than ten
bars it is not drawn. Download history on the Live page to give the long channel its ten
days.

**A channel is a description of the past, drawn as it stands now.** It is fitted again
every minute, and on the chart of today it is drawn back over the earlier hours with its
present slope. Earlier in the day it stood elsewhere. The channels do not feed the rule.

The legend names each channel, how many pips its line rises or falls in an hour, and
where the bid stands in it: a small gauge from the lower edge (left) to the upper edge
(right), and "64% up" or "3.2 pips above". Pressing a legend entry hides or shows that
channel.

## The chart

* **5 min, 15 min, 1 h, Day** show the seconds of the trading day (it starts at 17:00
  New York), one bid a second, and follow the newest price. **10 d** shows the minutes the
  long channel is fitted to, with today's signals at their minute. Moving or zooming the
  chart by hand lets the button go; pressing it again brings the range back.
* The price scale makes room for the channel that matches the range (the short one up to
  an hour, the medium one for the day, all three for the ten days). A longer channel is
  many times wider than the last minutes of prices and would flatten them to a line, so
  its edge shows only where the price comes near it. The gauges in the legend say where
  the price stands in every channel at all times.
* A second without a usable price is a **hole in the line**, never a bridge. (The chart
  library joins points across empty ones by itself; the line is drawn so that it does
  not. The one-second line of the Live page had this fault since the first phase and is
  corrected with it.)
* When triangles crowd (a whole day of a rule that fires every minute) they are drawn
  small and under the price, so the price stays in sight.
* Pressing a time in the table takes the chart to that signal and rings it.

## One rule, live and afterwards

The rule exists in two forms that are held to the same answer by tests, value for value:

* `replay()` works a stretch of stored seconds at once (the day so far, when a page asks
  for it, after new settings, at the start of a trading day);
* `Stepper.push()` takes one second at a time while prices arrive, and what it signals is
  sent to every open page at once.

A signal raised live is the signal a later replay of the same seconds finds, with one
exception that follows from what is stored. The data-quality gate of a past second is not
kept in the database: when the day is read again (restart, new settings, a repaired gap),
a second counts by its own quality alone, fresh or carried and inside market hours. A
second that was held back live for another reason (clock skew suspected) can therefore
take part afterwards, and a gap that was filled in from the provider's history brings
seconds the live rule never saw. The list of a day can differ in those places after a
reload. It never differs because of anything later than the bar.

The seconds of the trading day are kept in memory for an instrument once somebody has
opened the page (about 2 MB), with enough seconds before the day for the indicators to
be warm when it starts.

## Routes

| | |
|---|---|
| `GET /api/live/view?symbol=` | the seconds of the trading day, its signals, the channels, the settings; `history=true` adds the minutes behind the channels, `seconds=false` leaves the seconds out |
| `GET /api/live/channels?symbol=` | the three channels as they stand now (the page asks once a minute) |
| `GET /api/live/settings` | the settings, their defaults and limits |
| `PUT /api/live/settings` | keep new settings (what is left out stays); every open page is told to ask again |
| `/ws/live`, message `{"type": "signal", "symbol", "signal"}` | a signal, at the second it is raised |

Long answers and the dashboard's own code travel compressed (gzip): a day of seconds is
about 0.8 MB as text and about 0.1 MB on the wire.

## What is tested

* The rule on hand-written prices (one signal on a fall, none twice in a row, a stale
  second ends the run, longer bars are judged at their close, a price that does not move
  meets no band) and on random ones: one second at a time gives exactly the signals of
  the whole stretch, for seven kinds of settings, with seconds missing and unusable.
* The service against a real database: the day read and judged, new seconds continuing
  it, the change of trading day, seconds arriving while the day is read, market hours,
  channels from provider bars and own bars, settings kept across a restart.
* The whole application on the simulator, in the test suite and again in CI in the
  container: signals arrive on the stream and are found unchanged in the day's picture.
* The page in a real browser on desktop and phone sizes, on synthetic prices.

## What is not verified

* **Nothing here has run on real Massive quotes.** Counts, the look of a real day, and
  how the rule behaves around quiet hours, the rollover and the weekly open are unknown
  until it has.
* **Whether the rule is worth following.** There is no backtest of it: the backtests of
  this project run on one-minute history with entries and exits, and this rule has
  neither exits nor costs. A signal on one-second bars also cannot be acted on at its
  price: orders are placed by hand seconds later, and a buy pays the ask, which is why
  the table shows both.
* The gate of past seconds is not stored (see above).
* One instrument is watched at a time per page; more pairs work the same way but have not
  been looked at together.
