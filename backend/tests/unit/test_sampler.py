"""The 1-second observation sampler - the core data-quality guarantee of Phase 1."""

from __future__ import annotations

import random

from fxlab.domain.models import DataSource, Observation, ObsStatus
from fxlab.pipeline.sampler import SecondSampler
from tests.conftest import T0, make_tick

STALE = 5000


def sampler(start: int = T0, **kw: object) -> SecondSampler:
    return SecondSampler("EURUSD", STALE, start, **kw)  # type: ignore[arg-type]


def statuses(observations: list[Observation]) -> list[str]:
    return [o.status.value for o in observations]


def test_no_quote_ever_means_missing_not_invented() -> None:
    s = sampler()
    obs = s.advance(T0 + 3000)
    assert statuses(obs) == ["missing"] * 3
    assert [o.ts_ms for o in obs] == [T0, T0 + 1000, T0 + 2000]
    assert all(o.bid is None and o.ask is None and o.mid is None and o.age_ms is None for o in obs)
    assert all(o.n_quotes == 0 for o in obs)


def test_fresh_then_carried_then_stale() -> None:
    s = sampler()
    s.on_quote(make_tick(T0 + 200, 1.16000, 1.16008))
    obs = s.advance(T0 + 8000)
    assert statuses(obs) == ["fresh", "carried", "carried", "carried", "carried", "stale", "stale", "stale"]
    # the price is carried, never changed
    assert {(o.bid, o.ask) for o in obs} == {(1.16000, 1.16008)}
    # age is measured at the end of each window against the quote's source timestamp
    assert [o.age_ms for o in obs] == [800, 1800, 2800, 3800, 4800, 5800, 6800, 7800]
    assert all(o.quote_ts_ms == T0 + 200 for o in obs)


def test_carried_observations_never_contain_movement() -> None:
    s = sampler()
    s.on_quote(make_tick(T0 + 100, 1.16000, 1.16008))
    s.on_quote(make_tick(T0 + 600, 1.16010, 1.16018))
    fresh, carried = s.advance(T0 + 2000)
    assert fresh.status is ObsStatus.FRESH and fresh.n_quotes == 2
    assert (fresh.mid_open, fresh.mid_high, fresh.mid_low) == (
        make_tick(0, 1.16000, 1.16008).mid,
        fresh.mid,
        fresh.mid_open,
    )
    assert carried.status is ObsStatus.CARRIED
    assert carried.n_quotes == 0
    assert carried.mid_open is None and carried.mid_high is None and carried.mid_low is None
    assert carried.mid == fresh.mid


def test_intra_second_path_is_captured() -> None:
    s = sampler()
    for offset, bid in ((50, 1.16000), (300, 1.16030), (500, 1.15990), (900, 1.16010)):
        s.on_quote(make_tick(T0 + offset, bid, bid + 0.00008))
    (obs,) = s.advance(T0 + 1000)
    assert obs.n_quotes == 4
    assert obs.mid_open == make_tick(0, 1.16000, 1.16008).mid
    assert obs.mid_high == make_tick(0, 1.16030, 1.16038).mid
    assert obs.mid_low == make_tick(0, 1.15990, 1.15998).mid
    assert obs.mid == make_tick(0, 1.16010, 1.16018).mid  # close = last quote
    assert obs.age_ms == 100


def test_window_boundaries_are_half_open() -> None:
    s = sampler()
    s.on_quote(make_tick(T0 + 999))
    closed = s.on_quote(make_tick(T0 + 1000, 1.16020, 1.16028))  # exactly on the boundary => next window
    assert len(closed) == 1 and closed[0].ts_ms == T0 and closed[0].n_quotes == 1
    (second,) = s.advance(T0 + 2000)
    assert second.ts_ms == T0 + 1000 and second.n_quotes == 1 and second.bid == 1.16020


def test_advance_only_closes_finished_windows() -> None:
    s = sampler()
    s.on_quote(make_tick(T0 + 100))
    assert s.advance(T0 + 999) == []
    assert len(s.advance(T0 + 1000)) == 1
    assert s.advance(T0 + 1000) == []  # idempotent
    assert s.window_start_ms == T0 + 1000


def test_quote_arrival_closes_pending_windows_without_a_timer() -> None:
    s = sampler()
    s.on_quote(make_tick(T0 + 100))
    closed = s.on_quote(make_tick(T0 + 3500, 1.16010, 1.16018))
    assert statuses(closed) == ["fresh", "carried", "carried"]
    assert [o.ts_ms for o in closed] == [T0, T0 + 1000, T0 + 2000]


def test_stale_threshold_boundary() -> None:
    s = sampler()
    s.on_quote(make_tick(T0))  # source ts exactly at the window start
    obs = s.advance(T0 + 6000)
    # ages 1000..6000; age == threshold (5000) is still carried, only strictly older is stale
    assert statuses(obs) == ["fresh", "carried", "carried", "carried", "carried", "stale"]


def test_new_quote_with_old_source_timestamp_is_stale_not_fresh() -> None:
    """A delayed feed delivers 'new' messages that describe an old market: never call that fresh."""
    s = sampler()
    s.on_quote(make_tick(T0 - 20_000, recv_ts_ms=T0 + 100))
    (obs,) = s.advance(T0 + 1000)
    assert obs.status is ObsStatus.STALE
    assert obs.n_quotes == 1 and obs.age_ms == 21_000


def test_closed_windows_are_never_reopened() -> None:
    """Causality: a quote processed after a window closed lands in the open window."""
    s = sampler()
    s.advance(T0 + 2000)  # windows T0 and T0+1000 are closed (missing)
    closed = s.on_quote(make_tick(T0 + 500, recv_ts_ms=T0 + 500))  # clock stepped backwards
    assert closed == []
    (obs,) = s.advance(T0 + 3000)
    assert obs.ts_ms == T0 + 2000 and obs.n_quotes == 1


def test_seed_provides_carry_forward_after_restart() -> None:
    s = sampler()
    s.seed(make_tick(T0 - 1500, 1.15900, 1.15908))
    obs = s.advance(T0 + 5000)
    assert statuses(obs) == ["carried", "carried", "carried", "stale", "stale"]
    assert all(o.n_quotes == 0 and o.bid == 1.15900 for o in obs)


def test_catchup_limit_skips_instead_of_flooding() -> None:
    s = sampler(max_catchup_s=60)
    s.on_quote(make_tick(T0 + 100))
    obs = s.advance(T0 + 3_600_000)  # the process was suspended for an hour
    assert len(obs) == 1 and obs[0].ts_ms == T0 and obs[0].status is ObsStatus.FRESH
    assert s.pop_skipped() == [(T0 + 1000, T0 + 3_600_000)]
    assert s.pop_skipped() == []
    assert s.window_start_ms == T0 + 3_600_000
    # within the limit nothing is skipped
    s2 = sampler(max_catchup_s=60)
    assert len(s2.advance(T0 + 60_000)) == 60 and s2.pop_skipped() == []


def test_replay_has_no_catchup_limit_and_tags_source() -> None:
    s = sampler(max_catchup_s=None, source=DataSource.BACKFILL)
    s.seed(make_tick(T0 - 100))
    obs = s.advance(T0 + 7_200_000)
    assert len(obs) == 7200
    assert {o.source for o in obs} == {DataSource.BACKFILL}


def test_grid_is_contiguous_and_timer_independent() -> None:
    """Property: the grid has exactly one row per second and does not depend on when the
    timer happened to fire relative to the quotes."""
    rng = random.Random(7)
    t = T0
    ticks = []
    for _ in range(3000):
        t += int(rng.expovariate(1 / 400)) + 1  # irregular arrivals, ~2.5 per second, with quiet gaps
        px = 1.16 + rng.uniform(-0.001, 0.001)
        ticks.append(make_tick(t - rng.randint(0, 80), px, px + 0.00008, recv_ts_ms=t))
    end = ticks[-1].recv_ts_ms + 10_000

    def run(timer_period_ms: int | None) -> list[Observation]:
        s = sampler()
        out: list[Observation] = []
        next_timer = T0
        for tick in ticks:
            if timer_period_ms:
                while next_timer <= tick.recv_ts_ms:
                    out.extend(s.advance(next_timer))
                    next_timer += timer_period_ms
            out.extend(s.on_quote(tick))
        out.extend(s.advance(end))
        return out

    reference = run(None)
    assert [o.ts_ms for o in reference] == list(range(T0, (end // 1000) * 1000, 1000))
    for period in (1000, 250, 1700):
        assert run(period) == reference
    # every quote is counted in exactly one observation
    assert sum(o.n_quotes for o in reference) == len(ticks)
    # no observation carries a price without a quote in force
    assert all(o.bid is not None for o in reference if o.status is not ObsStatus.MISSING)
