"""Strategy research: indicators, strategies, the backtest engine and its statistics.

Nothing in this package talks to the provider or to the live feed.  It works on bars that
are already in the database (or on arrays in tests), so a backtest can run in a process of
its own without touching ingestion.
"""
