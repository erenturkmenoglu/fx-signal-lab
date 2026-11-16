"""Live signals: the settings, the rule on a stretch of seconds and one second at a time, the channels."""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest

from fxlab.research import indicators as ind
from fxlab.research.live import (
    BAR_SECONDS,
    LIMITS,
    MIN_CHANNEL_BARS,
    LiveError,
    LiveSettings,
    Signal,
    Stepper,
    band_reach,
    channel,
    describe,
    payload,
    replay,
    scan,
)
from fxlab.timeutil import MINUTE_MS, SECOND_MS
from tests.conftest import T0

PIP = 0.0001


def seconds(n: int, seed: int, *, holes: float = 0.0, unusable: float = 0.0, start: int = T0):  # type: ignore[no-untyped-def]
    """A random walk of the bid on the one-second grid, with seconds missing and seconds unusable."""
    rng = np.random.default_rng(seed)
    step = rng.choice([-2, -1, 0, 0, 1, 2], size=n) * (PIP / 10)
    bid = np.round(1.16 + np.cumsum(step), 5)
    ask = np.round(bid + 0.8 * PIP, 5)
    ts = start + np.arange(n, dtype=np.int64) * SECOND_MS
    usable = rng.random(n) >= unusable
    keep = rng.random(n) >= holes
    return ts[keep], bid[keep], ask[keep], usable[keep]


def pushed(ts, bid, ask, usable, settings, stepper=None):  # type: ignore[no-untyped-def]
    stepper = stepper or Stepper(settings)
    out: list[Signal] = []
    for i in range(len(ts)):
        out.extend(stepper.push(int(ts[i]), float(bid[i]), float(ask[i]), bool(usable[i])))
    return out


QUICK = LiveSettings(rsi_period=5, rsi_buy_below=35, rsi_sell_above=65, bollinger_period=8, bollinger_width=1.5)
VARIANTS = [
    LiveSettings(),
    QUICK,
    LiveSettings.from_mapping({**QUICK.as_dict(), "rule": "either"}),
    LiveSettings.from_mapping({**QUICK.as_dict(), "bar_seconds": 5}),
    LiveSettings.from_mapping({**QUICK.as_dict(), "bar_seconds": 15, "rule": "either"}),
    LiveSettings(rsi_period=3, bollinger_period=30),
    LiveSettings(rsi_period=40, bollinger_period=4, bollinger_width=1.5, rsi_buy_below=45, rsi_sell_above=55),
]


# ------------------------------------------------------------------------------------ settings
def test_the_defaults_are_the_textbook_ones_on_one_second_bars() -> None:
    s = LiveSettings()
    assert (s.bar_seconds, s.rsi_period, s.rsi_buy_below, s.rsi_sell_above) == (1, 14, 30.0, 70.0)
    assert (s.bollinger_period, s.bollinger_width, s.rule) == (20, 2.0, "both")
    assert s.channel_bars() == (60, 1440, 14400)  # one hour, one day, ten days of one-minute bars
    assert s.first_signal_at == 20  # RSI from bar 14, bands from bar 19, and the bar before must be judged too
    assert LiveSettings.from_mapping({}) == s and LiveSettings.from_mapping(s.as_dict()) == s
    form = describe()
    assert (
        form["defaults"] == s.as_dict()
        and form["bar_seconds"] == list(BAR_SECONDS)
        and form["rules"] == ["both", "either"]
    )
    assert set(form["limits"]) == set(LIMITS) and form["limits"]["rsi_period"] == [2, 1000]


def test_settings_keep_what_is_left_out_and_take_whole_numbers_written_as_floats() -> None:
    s = LiveSettings.from_mapping({"rsi_period": 21.0, "bollinger_width": 3, "rule": "either", "bar_seconds": 5})
    assert s.rsi_period == 21 and isinstance(s.rsi_period, int) and s.bollinger_width == 3.0 and s.rule == "either"
    assert s.bar_seconds == 5 and s.bollinger_period == 20 and s.rsi_buy_below == 30.0


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "must be an object"),
        ({"stop_pips": 5}, "unknown setting(s): stop_pips"),
        ({"rule": "all"}, "rule must be one of: both, either"),
        ({"rsi_period": "14"}, "rsi_period must be a number"),
        ({"rsi_period": True}, "rsi_period must be a number"),
        ({"bollinger_width": math.nan}, "bollinger_width must be a number"),
        ({"rsi_period": 14.5}, "rsi_period must be a whole number"),
        ({"rsi_period": 1}, "rsi_period must be between 2 and 1000"),
        ({"bollinger_period": 5000}, "bollinger_period must be between 2 and 2000"),
        ({"bollinger_width": 0}, "bollinger_width must be between 0.1 and 10"),
        ({"bar_seconds": 2}, "bar_seconds must be one of: 1, 5, 15, 30, 60"),
        ({"rsi_buy_below": 70, "rsi_sell_above": 70}, "rsi_buy_below must be lower than rsi_sell_above"),
        ({"rsi_buy_below": 0}, "rsi_buy_below must be between 1 and 99"),
        ({"channel_short_hours": 30}, "the channels must grow"),
        ({"channel_long_days": 1, "channel_medium_hours": 24}, "the channels must grow"),
        ({"channel_width": 9}, "channel_width must be between 0.5 and 5"),
        # four prices: none can be two standard deviations from their average
        ({"bollinger_period": 4}, "bollinger_width must be below 1.73 with a bollinger_period of 4"),
        (
            {"bollinger_period": 2, "bollinger_width": 1},
            "bollinger_width must be below 1.00 with a bollinger_period of 2",
        ),
        ({"bollinger_width": 4.5}, "bollinger_width must be below 4.36 with a bollinger_period of 20"),
    ],
)
def test_settings_that_cannot_be_used_are_refused_with_the_reason(data: object, message: str) -> None:
    with pytest.raises(LiveError) as refused:
        LiveSettings.from_mapping(data)
    assert message in str(refused.value)


# ---------------------------------------------------------------------------------------- rule
def test_a_signal_is_raised_once_when_both_conditions_turn_true() -> None:
    """Flat, then a fall: the RSI drops below its level and the bid below the lower band."""
    flat = [1.16000, 1.16001] * 15  # thirty quiet seconds
    fall = [1.15990, 1.15980, 1.15970, 1.15960, 1.15950, 1.15950, 1.15950]
    bid = np.array(flat + fall)
    ts = T0 + np.arange(len(bid), dtype=np.int64) * SECOND_MS
    ask = bid + 0.00008
    signals = scan(ts, bid, ask, np.ones(len(bid), dtype=bool), LiveSettings())
    assert [s.side for s in signals] == ["buy"]
    (buy,) = signals
    at = len(flat)  # the first second of the fall
    assert buy.ts_ms == int(ts[at]) and buy.bid == 1.1599 and buy.ask == pytest.approx(1.15998)
    # what it carries is what the indicators say at that second, from the seconds up to it
    rsi = ind.rsi(bid[: at + 1], 14)[-1]
    middle, upper, lower = (band[-1] for band in ind.bollinger(bid[: at + 1], 20, 2.0))
    assert buy.rsi == rsi <= 30 and (buy.middle, buy.upper, buy.lower) == (middle, upper, lower) and buy.bid <= lower
    # the condition stays true for the seconds that follow; it is not signalled again
    assert ind.rsi(bid, 14)[at + 2] <= 30 and bid[at + 2] <= ind.bollinger(bid, 20, 2.0)[2][at + 2]
    # the mirror image is a sell
    mirrored = scan(ts, 2.32 - bid, 2.32 - bid + 0.00008, np.ones(len(bid), dtype=bool), LiveSettings())
    assert [s.side for s in mirrored] == ["sell"] and mirrored[0].ts_ms == buy.ts_ms and mirrored[0].rsi >= 70


def test_either_indicator_is_enough_when_the_rule_says_so() -> None:
    ts, bid, ask, usable = seconds(6000, seed=3)
    both = scan(ts, bid, ask, usable, QUICK)
    either = scan(ts, bid, ask, usable, LiveSettings.from_mapping({**QUICK.as_dict(), "rule": "either"}))
    assert 0 < len(both) < len(either)
    for s in both:  # both conditions really hold at the second of a signal
        assert (s.rsi <= 35 and s.bid <= s.lower) if s.side == "buy" else (s.rsi >= 65 and s.bid >= s.upper)
    for s in either:
        assert (s.rsi <= 35 or s.bid <= s.lower) if s.side == "buy" else (s.rsi >= 65 or s.bid >= s.upper)


def test_what_the_dashboard_gets_is_rounded_and_keeps_the_rules_word_for_which_condition_held() -> None:
    both = Signal(T0, "buy", 1.159961234, 1.160041234, 29.98765, 1.1600412349, 1.1600987651, 1.1599799999, "both")
    assert payload(both, 5) == {
        "t": T0,
        "side": "buy",
        "bid": 1.15996,
        "ask": 1.16004,
        "rsi": 29.99,
        "middle": 1.1600412,
        "upper": 1.1600988,
        "lower": 1.15998,
        "by": "both",
    }
    # an RSI a hair above its level rounds onto it: what held is not read back from the rounded numbers
    hair = payload(Signal(T0, "buy", 1.1, 1.1001, 30.004, 1.2, 1.3, 1.15, "band"), 5)
    assert (hair["rsi"], hair["by"]) == (30.0, "band")
    # an instrument quoted to three digits keeps three, and its bands five
    yen = payload(Signal(T0, "sell", 154.3051, 154.3129, 71.239, 154.250049, 154.300051, 154.2, "rsi"), 3)
    assert (yen["bid"], yen["ask"], yen["rsi"], yen["middle"], yen["upper"]) == (
        154.305,
        154.313,
        71.24,
        154.25005,
        154.30005,
    )


def test_every_signal_says_which_condition_held_and_it_is_one_the_rule_asked_for() -> None:
    ts, bid, ask, usable = seconds(6000, seed=21)
    either = LiveSettings.from_mapping({**QUICK.as_dict(), "rule": "either"})
    for settings in (QUICK, either):
        found = scan(ts, bid, ask, usable, settings)
        assert len(found) > 20
        for s in found:
            level = settings.rsi_buy_below if s.side == "buy" else settings.rsi_sell_above
            rsi_met = s.rsi <= level if s.side == "buy" else s.rsi >= level
            band_met = s.bid <= s.lower if s.side == "buy" else s.bid >= s.upper
            assert s.by == ("both" if rsi_met and band_met else "rsi" if rsi_met else "band")
        if settings.rule == "both":
            assert {s.by for s in found} == {"both"}
        else:
            assert {s.by for s in found} == {"both", "rsi", "band"}


def test_bands_wider_than_any_price_can_reach_are_refused_because_they_would_never_be_met() -> None:
    assert band_reach(20) == pytest.approx(math.sqrt(19)) and band_reach(2) == 1.0
    rng = np.random.default_rng(9)
    for n in (2, 3, 4, 8, 20):
        values = rng.normal(0, 1, size=(4000, n))
        values[0] = [5.0] + [0.0] * (n - 1)  # one value apart from all the others: the furthest there is
        z = np.abs(values - values.mean(axis=1, keepdims=True)) / values.std(axis=1, keepdims=True)
        assert z.max() == pytest.approx(band_reach(n)) and (z <= band_reach(n) + 1e-9).all()
        # just inside the reach is taken, the reach itself is not
        assert LiveSettings.from_mapping({"bollinger_period": n, "bollinger_width": round(band_reach(n) - 0.05, 3)})
        with pytest.raises(LiveError):
            LiveSettings.from_mapping({"bollinger_period": n, "bollinger_width": band_reach(n)})


def test_a_price_that_does_not_move_meets_no_band() -> None:
    """Over bars without any change both bands are the price itself: rounding must not decide a signal."""
    rng = np.random.default_rng(5)
    head = 1.1 + np.cumsum(rng.choice([-1, 0, 1], size=300)) * 0.00001
    fall = head[-1] - np.arange(1, 13) * 0.00001  # twelve falls in a row: the RSI is far below its level
    quiet = np.full(120, fall[-1])  # and then two minutes without a change
    bid = np.round(np.concatenate([head, fall, quiet]), 5)
    ts = T0 + np.arange(len(bid), dtype=np.int64) * SECOND_MS
    usable = np.ones(len(bid), dtype=bool)
    start_quiet = int(ts[len(head) + len(fall)])
    for rule in ("both", "either"):
        settings = LiveSettings.from_mapping({"rule": rule})
        found = scan(ts, bid, bid + 0.00008, usable, settings)
        assert pushed(ts, bid, bid + 0.00008, usable, settings) == found
        # nothing is signalled while nothing moves: the RSI keeps its value and the bands have no width
        assert [s for s in found if s.ts_ms >= start_quiet] == [], rule
    # the same at every price level, where rounding falls differently
    for level in (0.6543, 1.1, 1.23457, 154.305, 18.4567):
        flat = np.concatenate([level + np.cumsum(rng.choice([-1, 0, 1], size=200)) * 0.00001, np.full(200, level)])
        stamps = T0 + np.arange(len(flat), dtype=np.int64) * SECOND_MS
        ok = np.ones(len(flat), dtype=bool)
        found = scan(stamps, flat, flat + 0.00008, ok, LiveSettings.from_mapping({"rule": "either"}))
        assert [s for s in found if s.ts_ms >= int(stamps[200 + 20])] == [], level
        assert pushed(stamps, flat, flat + 0.00008, ok, LiveSettings.from_mapping({"rule": "either"})) == found


@pytest.mark.parametrize("settings", VARIANTS)
def test_one_second_at_a_time_gives_exactly_the_signals_of_the_whole_stretch(settings: LiveSettings) -> None:
    """Live and replay agree value for value, with missing and unusable seconds in the way."""
    total = 0
    for seed in range(4):
        ts, bid, ask, usable = seconds(5000, seed, holes=0.004, unusable=0.004)
        whole = scan(ts, bid, ask, usable, settings)
        assert pushed(ts, bid, ask, usable, settings) == whole
        total += len(whole)
    assert total > 0


@pytest.mark.parametrize("settings", VARIANTS)
def test_nothing_later_is_used_and_a_replay_hands_over_to_the_live_steps(settings: LiveSettings) -> None:
    ts, bid, ask, usable = seconds(4000, seed=11, holes=0.003, unusable=0.003)
    whole = scan(ts, bid, ask, usable, settings)
    rng = np.random.default_rng(5)
    for cut in [0, 1, 2, 17, *rng.integers(20, len(ts), size=25).tolist(), len(ts) - 1, len(ts)]:
        head, stepper = replay(ts[:cut], bid[:cut], ask[:cut], usable[:cut], settings)
        # the signals of the first part do not change when more seconds are known ...
        assert head == [s for s in whole if cut and s.ts_ms <= int(ts[cut - 1])], cut
        # ... and the stepper the replay returns goes on exactly as if it had seen them one by one
        assert head + pushed(ts[cut:], bid[cut:], ask[cut:], usable[cut:], settings, stepper) == whole, cut


def test_a_stale_second_ends_the_run_and_nothing_is_signalled_until_the_indicators_are_back() -> None:
    settings = QUICK
    ts, bid, ask, usable = seconds(3000, seed=2)
    assert usable.all()
    clean = scan(ts, bid, ask, usable, settings)
    broken = usable.copy()
    stale = np.arange(400, 3000, 400)
    broken[stale] = False
    with_stale = scan(ts, bid, ask, broken, settings)
    quiet = settings.first_signal_at  # bars of the new run that cannot carry a signal
    for s in with_stale:
        position = (s.ts_ms - T0) // SECOND_MS
        since = position - stale[stale < position].max() if (stale < position).any() else position + 1
        assert since > quiet, (position, since)
    assert 0 < len(with_stale) < len(clean)
    # a missing second is the same as an unusable one
    kept = np.ones(len(ts), dtype=bool)
    kept[stale] = False
    assert scan(ts[kept], bid[kept], ask[kept], usable[kept], settings) == with_stale
    # nothing at all can be signalled before the indicators exist
    short = settings.first_signal_at
    assert scan(ts[:short], bid[:short], ask[:short], usable[:short], settings) == []


def test_longer_bars_are_judged_at_their_close_only() -> None:
    settings = LiveSettings.from_mapping({**QUICK.as_dict(), "bar_seconds": 5})
    ts, bid, ask, usable = seconds(20000, seed=8)
    signals = scan(ts, bid, ask, usable, settings)
    assert signals and all((s.ts_ms // SECOND_MS + 1) % 5 == 0 for s in signals)
    # the same as the rule on the closes alone
    closes = (ts // SECOND_MS + 1) % 5 == 0
    one = LiveSettings.from_mapping({**QUICK.as_dict(), "bar_seconds": 1})
    renumbered = T0 + np.arange(int(closes.sum()), dtype=np.int64) * SECOND_MS
    alone = scan(renumbered, bid[closes], ask[closes], usable[closes], one)
    assert [(s.side, s.bid, s.rsi, s.lower) for s in alone] == [(s.side, s.bid, s.rsi, s.lower) for s in signals]
    # a second inside a bar that is unusable changes nothing; an unusable close ends the run
    inside = usable.copy()
    inside[~closes] = False
    assert scan(ts, bid, ask, inside, settings) == signals


def test_the_same_side_is_not_signalled_on_two_bars_in_a_row() -> None:
    for settings in VARIANTS:
        ts, bid, ask, usable = seconds(6000, seed=21)
        step = settings.bar_seconds * SECOND_MS
        by_side: dict[str, list[int]] = {"buy": [], "sell": []}
        for s in scan(ts, bid, ask, usable, settings):
            by_side[s.side].append(s.ts_ms)
        for times in by_side.values():
            assert all(later - earlier > step for earlier, later in itertools.pairwise(times))


def test_the_rsi_averages_are_the_memory_of_the_rsi() -> None:
    x = np.round(1.16 + np.cumsum(np.random.default_rng(4).normal(0, 0.0001, 300)), 5)
    gain, loss = ind.rsi_averages(x, 14)
    assert np.isnan(gain[:14]).all() and not np.isnan(gain[14:]).any()
    with np.errstate(divide="ignore", invalid="ignore"):
        assert np.array_equal(ind.rsi(x, 14)[14:], (100.0 - 100.0 / (1.0 + gain / loss))[14:])
    # one more value: the averages go on by Wilder's step, without the history
    more = np.append(x, x[-1] + 0.00007)
    stepped = (gain[-1] * 13 + 0.00007 if more[-1] > x[-1] else gain[-1] * 13) / 14
    assert ind.rsi_averages(more, 14)[0][-1] == pytest.approx(stepped, rel=1e-12)
    short = ind.rsi_averages(x[:14], 14)
    assert np.isnan(short[0]).all() and np.isnan(short[1]).all()


# ------------------------------------------------------------------------------------ channels
def test_a_channel_is_the_line_through_the_newest_bars_with_its_width() -> None:
    n = 600
    ts = T0 + np.arange(n, dtype=np.int64) * MINUTE_MS
    line = 1.16 + 0.00001 * np.arange(n)
    wave = 0.0003 * np.sin(np.arange(n) / 5.0)
    found = channel(ts, line + wave, 120, 2.0)
    assert found is not None and found["bars"] == found["asked"] == 120
    assert (found["from"], found["to"]) == (int(ts[-120]), int(ts[-1]))
    assert found["rise_per_bar"] == pytest.approx(0.00001, rel=0.1)  # the wave tilts the fit a little
    assert found["end"] - found["start"] == pytest.approx(found["rise_per_bar"] * 119)
    # two standard deviations of a sine wave of amplitude 3 pips around its line
    assert found["half"] == pytest.approx(2 * 0.0003 / math.sqrt(2), rel=0.1)
    middle, upper, _lower, _slope = ind.regression_channel((line + wave)[-120:], 120, 2.0)
    assert found["end"] == middle[-1] and found["half"] == upper[-1] - middle[-1]
    # on a straight line the channel has no width and lies on the line
    straight = channel(ts, line, 300, 2.0)
    assert straight is not None and straight["half"] == pytest.approx(0, abs=1e-9)
    assert (straight["start"], straight["end"]) == (pytest.approx(line[-300]), pytest.approx(line[-1]))


def test_a_channel_uses_the_bars_there_are_and_says_how_many() -> None:
    ts = T0 + np.arange(50, dtype=np.int64) * MINUTE_MS
    close = 1.16 + 0.00002 * np.arange(50)
    found = channel(ts, close, 14400, 2.0)
    assert found is not None and found["bars"] == 50 and found["asked"] == 14400 and found["from"] == T0
    assert channel(ts[: MIN_CHANNEL_BARS - 1], close[: MIN_CHANNEL_BARS - 1], 60, 2.0) is None
    assert channel(ts[:0], close[:0], 60, 2.0) is None


def test_a_channel_says_where_its_line_stands_at_the_start_of_the_day() -> None:
    ts = T0 + np.arange(200, dtype=np.int64) * MINUTE_MS
    close = 1.16 + 0.00001 * np.arange(200)
    day = int(ts[150])
    found = channel(ts, close, 120, 2.0, day)
    assert found is not None and found["from"] == int(ts[80])
    assert found["day"] == {"from": day, "start": pytest.approx(close[150])}
    inside = channel(ts, close, 30, 2.0, day)  # the whole channel lies inside the day
    assert inside is not None and inside["day"] == {"from": inside["from"], "start": pytest.approx(inside["start"])}
    assert channel(ts, close, 30, 2.0, int(ts[-1]) + MINUTE_MS)["day"] is None  # type: ignore[index]
