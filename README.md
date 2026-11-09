# LLM Finance Framework v0.9: 60-session backtest of Claude Haiku 5.5 (model outputs)

This dataset holds every model call of the backtest described in the v0.9 paper: Claude Haiku 5.5
(`claude-haiku-5-5`, through Claude Code), 60 New York sessions from 2026-08-10 to 2026-11-02,
one decision per session at 15:30, fills at the 16:00 close, on SPY and AAPL, with four LLM arms
(`baseline`, `memory_only`, `memory_feeling`, `memory_only_cot`) and two control arms
(`buy_and_hold`, `random`).

Code: [LLM Finance Framework v0.9.0](https://github.com/tns-research/llm-finance-framework/releases/tag/v0.9.0), DOI [10.5281/zenodo.24061577](https://doi.org/10.5281/zenodo.24061577).

## Contents

| File | What it is |
|---|---|
| `run_noprices.db` | SQLite run database without market data (schema below). |
| `RUN_MANIFEST.json` | Settings, versions and configuration fingerprint of the run. |
| `run.log` | Log of the run. |
| `dataset_prices.py` | Script that downloads the prices again and restores the full database. |
| `SHA256SUMS` | Checksums of the data files. |

Tables of `run_noprices.db`:

- `decisions`: 720 rows, one per slot, asset and arm. For the 480 LLM rows: system prompt, user prompt,
  raw model answer, token usage, parsed action, target exposure, probability of an up move, confidence,
  explanation, journal note, feeling, reasoning, exposure before and after, cost and portfolio value.
- `reflections`: 96 weekly and monthly written reflections, with their prompts.
- `portfolios`: final paper portfolios.
- `slots`: 120 decision slots with their execution time and status.
- `meta`: settings and run mode.
- `removed_checksums`: SHA-256 of every item removed from the original database.

## Market data is left out

The prices come from Yahoo Finance through `yfinance`. Yahoo's terms allow personal use of its data
and do not allow redistribution, so this dataset contains none of it. The removed items are:

- `slots.snapshot` (price and indicators each arm saw) and `slots.exec_price` (fill prices);
- `corporate_actions` (dividends);
- in each user prompt, the market data block and the indicator values of past decisions, replaced by
  placeholders such as `<<MARKET_DATA 2026-08-31T15:30 SPY indicators=1>>`.

Portfolio values, returns relative to buy-and-hold, and period summaries of the indicators
(averages and shares of overbought days) stay in the data. The model's own answers sometimes quote a
price; they are kept as written.

## Restore the full database

From a clone of the framework at the v0.9.0 release, with its requirements installed:

```bash
python scripts/dataset_prices.py rebuild run_noprices.db results/backtest/run.db
python -c "from src.live.reporting import build_reports; build_reports('results/backtest/run.db', 'results/backtest/report')"
```

The first command downloads the daily and hourly bars, recomputes each snapshot and fill price with the
framework's code, fills the placeholders back in, and checks all 604 checksums. On 2026-11-04 all 604
matched. The second command regenerates every report, table and figure of the backtest.

The bootstrap tests of the statistical validation use a fixed seed, so rebuilt reports match the
published ones exactly, apart from their generation timestamps.

One limit: Yahoo serves hourly bars for roughly the last 730 days only, so the rebuild should work until
about August 2028. After that, or if Yahoo revises past bars, the script lists the items that no longer
match.

The `git_sha` field of `RUN_MANIFEST.json` names the development commit the run was made with. The public
code to use is the v0.9.0 release: on 2026-11-09 a rebuild from a fresh clone of v0.9.0 matched all 604
checksums.

## License

Model outputs, prompts and derived tables: CC BY 4.0 (see `LICENSE`). The framework code is MIT-licensed in its own
repository.
