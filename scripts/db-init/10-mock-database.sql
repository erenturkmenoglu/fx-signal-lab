-- Runs once, when the database volume is first initialised.
-- A separate database for DATA_PROVIDER=mock: synthetic data never shares a database
-- with market data (the application enforces this, see backend/fxlab/storage/guard.py).
CREATE DATABASE fxlab_mock OWNER fxlab;
