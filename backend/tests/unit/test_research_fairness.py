"""On prices that cannot be predicted, a backtest must report the costs and nothing else.

A driftless random walk has no memory: whatever a strategy does on it, the expected result
before costs is zero.  So over thousands of trades the engine must show a loss of about one
spread per clip traded - not less (that would be information from the future leaking into
signals or fills) and not much more (that would be the conservative fill rules punishing
strategies beyond what the data justifies).

This is the test that would catch a look-ahead error anywhere between the bars and the
trade list: such an error turns noise into profit.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from fxlab.research import engine
from fxlab.research.bars import Bars, resample
from fxlab.research.engine import Execution
from fxlab.research.strategies import STRATEGIES
from fxlab.timeutil import DAY_MS, MINUTE_MS
from tests.conftest import utc_ms

PIP = 0.0001
PIP_VALUE = 100.0  # USD a pip on one clip of 1 mn EUR
#: how far the average result of a clip may lie from "minus the spread", in pips.  With some
#: thousands of trades whose results scatter by about 6 pips, chance alone moves it by 0.1 to 0.2.
TOLERANCE = 0.4


def random_walk(seed: int, days: int = 30) -> Bars:
    """One-minute bars cut from a finer walk (12 steps a minute), 06:00 to 20:00 UTC on weekdays.

    The highs and lows belong to the same path as the opens and closes, as they do in a market.
    """
    rng = np.random.default_rng(seed)
    stamps: list[int] = []
    day = utc_ms(2026, 6, 1)  # a Monday
    while len(stamps) < days * 840:
        if (day // DAY_MS + 3) % 7 < 5:
            stamps += [day + minute * MINUTE_MS for minute in range(6 * 60, 20 * 60)]
        day += DAY_MS
    n = len(stamps)
    path = np.round(1.16 + np.cumsum(rng.normal(0, 0.35 * PIP, n * 12)), 5).reshape(n, 12)
    opens = np.concatenate(([1.16], path[:-1, -1]))
    return Bars(
        np.asarray(stamps, dtype=np.int64),
        opens,
        np.maximum(opens, path.max(axis=1)),
        np.minimum(opens, path.min(axis=1)),
        path[:, -1],
        60,
    )


#: random walks of 30 days each.  A strategy that trades twice a day needs more of them than one
#: that trades ten times, to get to the same number of trades.
WALKS = {"session_breakout": 110}
_walks: list[tuple[Bars, Bars]] = []


def walks_for(key: str) -> list[tuple[Bars, Bars]]:
    wanted = WALKS.get(key, 24)
    while len(_walks) < wanted:  # made once, shared by the tests of this module
        base = random_walk(len(_walks))
        _walks.append((base, resample(base, 300)))
    return _walks[:wanted]


def pips_a_clip(walks: list[tuple[Bars, Bars]], key: str, ex: Execution) -> tuple[float, int]:
    """Average result of one clip in pips, and the number of trades it is the average of."""
    clips = trades = 0
    money = 0.0
    for base, five in walks:
        outcome = engine.run(base, five, STRATEGIES[key].signals(five, PIP), ex)
        trades += len(outcome.trades)
        clips += sum(t.clips for t in outcome.trades)
        money += sum(t.pnl for t in outcome.trades)
    return money / PIP_VALUE / clips, trades


@pytest.mark.parametrize("key", list(STRATEGIES))
@pytest.mark.parametrize("spread", [0.0, 1.0])
def test_on_a_random_walk_a_strategy_loses_the_spread_and_nothing_else(key: str, spread: float) -> None:
    ex = Execution(spread_pips=spread, max_clips=1, min_potential_pips=0.0)
    result, trades = pips_a_clip(walks_for(key), key, ex)
    assert trades > 2000, "too few trades for the average to mean anything"
    assert abs(result + spread) < TOLERANCE, f"{key}: {result:+.3f} pips a clip where {-spread:+.1f} is expected"


@pytest.mark.parametrize("key", ["rsi_reversion", "bollinger_reversion"])
def test_adding_to_positions_does_not_create_an_edge_either(key: str) -> None:
    """More clips, a higher share of winners, the same expectation: no money comes from averaging down."""
    walks = walks_for(key)
    one = Execution(spread_pips=0.0, max_clips=1, min_potential_pips=0.0)
    five = replace(one, max_clips=5)
    result, trades = pips_a_clip(walks, key, five)
    assert trades > 2000 and abs(result) < TOLERANCE, f"{key}: {result:+.3f} pips a clip where 0 is expected"

    def share_won(ex: Execution) -> float:
        won = total = 0
        for base, bars in walks[:8]:
            for t in engine.run(base, bars, STRATEGIES[key].signals(bars, PIP), ex).trades:
                won += t.pnl > 0
                total += 1
        return won / total

    assert share_won(five) > share_won(one) + 0.1  # it does win more often: that is the temptation
