"""Strip Yahoo Finance prices from a run database, or put them back from a fresh download.

The published dataset keeps every model call, prompt, decision, portfolio value and reflection,
and leaves out the market data the prompts were built from. Yahoo's terms allow personal use of
its data but not redistribution, so each reader downloads the prices again with yfinance.

    python scripts/dataset_prices.py strip   results/backtest/run.db  dataset/run_noprices.db
    python scripts/dataset_prices.py rebuild dataset/run_noprices.db  results/rebuilt/run.db

strip   removes slots.snapshot, slots.exec_price and corporate_actions, replaces the market block
        and the per-decision indicator lines of each prompt with placeholders, and stores SHA-256
        checksums of everything it removed.
rebuild downloads the same bars, recomputes the snapshots, fill prices and corporate actions with
        the framework's own code, fills the placeholders back in and checks every checksum.
        Then `build_reports` regenerates all reports and figures from the rebuilt database.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import date, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.live import arms as arms_module  # noqa: E402
from src.live.market import Snapshot, bar_close_at, build_snapshot  # noqa: E402

MARKET = "<<MARKET_DATA {slot} {symbol} indicators={flag}>>"
ROW = "<<ROW_INDICATORS {slot} {symbol}>>"
MARKET_RE = re.compile(r"<<MARKET_DATA (\S+) (\S+) indicators=(0|1)>>")
ROW_RE = re.compile(r"<<ROW_INDICATORS (\S+) (\S+)>>")
REMOVED_TABLE = "removed_checksums"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def snapshot_from_json(raw: str) -> Snapshot:
    data = json.loads(raw)
    for key, value in data.items():
        if isinstance(value, list):
            data[key] = tuple(value)
    return Snapshot(**data)


def arm_flags(db) -> dict:
    settings = json.loads(db.execute("SELECT value FROM meta WHERE key='settings'").fetchone()[0])
    return {arm["name"]: bool(arm.get("indicators", True)) for arm in settings["arms"]}


def price_fingerprint(rows) -> str:
    """One checksum per symbol over every fill price, so no single price can be read back from it."""
    return sha(json.dumps([[slot, round(price, 6) if price is not None else None] for slot, price in rows]))


def strip(src: str, dst: str) -> None:
    if os.path.exists(dst):
        sys.exit(f"{dst} already exists")
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    shutil.copyfile(src, dst)
    db = sqlite3.connect(dst)
    db.row_factory = sqlite3.Row
    flags = arm_flags(db)
    snaps = {(r["slot"], r["symbol"]): r["snapshot"] for r in db.execute("SELECT slot, symbol, snapshot FROM slots")}

    db.execute(f"CREATE TABLE {REMOVED_TABLE} (item TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")
    checks = {}
    for (slot, symbol), raw in snaps.items():
        if raw:
            checks[f"snapshot {slot} {symbol}"] = sha(raw)
    for (symbol,) in db.execute("SELECT DISTINCT symbol FROM slots").fetchall():
        rows = db.execute("SELECT slot, exec_price FROM slots WHERE symbol=? ORDER BY slot", (symbol,)).fetchall()
        checks[f"exec_price {symbol}"] = price_fingerprint(rows)
        actions = db.execute(
            "SELECT day, dividend, split FROM corporate_actions WHERE symbol=? ORDER BY day", (symbol,)
        ).fetchall()
        checks[f"corporate_actions {symbol}"] = sha(json.dumps([list(a) for a in actions]))

    rows_text = {}
    for key, raw in snaps.items():
        if raw:
            rows_text[key] = arms_module._row_indicators({"snapshot": raw})

    for row in db.execute("SELECT slot, symbol, arm, user_prompt FROM decisions").fetchall():
        prompt = row["user_prompt"]
        if not prompt:
            continue
        original = prompt
        indicators = flags.get(row["arm"], True)
        raw = snaps.get((row["slot"], row["symbol"]))
        if raw:
            block = arms_module._market_block(snapshot_from_json(raw), indicators)
            if prompt.count(block) != 1:
                sys.exit(f"market block not found once in {row['slot']} {row['symbol']} {row['arm']}")
            prompt = prompt.replace(block, MARKET.format(slot=row["slot"], symbol=row["symbol"], flag=int(indicators)))
        if indicators:
            for (slot, symbol), text in rows_text.items():
                if symbol == row["symbol"] and text and text in prompt:
                    prompt = prompt.replace(text, ROW.format(slot=slot, symbol=symbol))
        checks[f"user_prompt {row['slot']} {row['symbol']} {row['arm']}"] = sha(original)
        db.execute(
            "UPDATE decisions SET user_prompt=? WHERE slot=? AND symbol=? AND arm=?",
            (prompt, row["slot"], row["symbol"], row["arm"]),
        )

    db.executemany(f"INSERT INTO {REMOVED_TABLE} VALUES (?, ?)", sorted(checks.items()))
    db.execute("UPDATE slots SET snapshot=NULL, exec_price=NULL")
    db.execute("DELETE FROM corporate_actions")
    db.execute("INSERT OR REPLACE INTO meta VALUES ('prices', 'removed; run scripts/dataset_prices.py rebuild')")
    db.commit()
    leftover = db.execute(
        "SELECT COUNT(*) FROM decisions WHERE user_prompt LIKE '%Current price:%' OR user_prompt LIKE '%Technical indicators: RSI%'"
    ).fetchone()[0]
    db.execute("PRAGMA journal_mode=DELETE")
    db.close()
    con = sqlite3.connect(dst)
    con.execute("VACUUM")
    con.close()
    if leftover:
        sys.exit(f"{leftover} prompts still contain market data")
    print(f"stripped {len(checks)} items into {dst}")


def rebuild(src: str, dst: str, market=None) -> int:
    if os.path.exists(dst):
        sys.exit(f"{dst} already exists")
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    shutil.copyfile(src, dst)
    db = sqlite3.connect(dst)
    db.row_factory = sqlite3.Row
    checks = dict(db.execute(f"SELECT item, sha256 FROM {REMOVED_TABLE}").fetchall())
    slots = db.execute("SELECT slot, symbol, exec_at, status FROM slots ORDER BY slot").fetchall()
    if market is None:
        from src.live.backtest import HistoricalMarket

        first = min(datetime.fromisoformat(r["slot"]).date() for r in slots)
        market = HistoricalMarket(first)

    from src.live.clock import NY

    failures = []
    snaps = {}
    for row in slots:
        moment = datetime.fromisoformat(row["slot"]).replace(tzinfo=NY)
        key = f"snapshot {row['slot']} {row['symbol']}"
        if key not in checks:
            continue
        snap = build_snapshot(market, row["symbol"], moment, row["slot"])
        raw = json.dumps(snap.to_dict())
        if sha(raw) != checks[key]:
            failures.append(key)
        snaps[(row["slot"], row["symbol"])] = raw
        price = None
        if row["exec_at"] and row["status"] == "filled":
            price = bar_close_at(market, row["symbol"], datetime.fromisoformat(row["exec_at"]))
        db.execute("UPDATE slots SET snapshot=?, exec_price=? WHERE slot=? AND symbol=?",
                   (raw, price, row["slot"], row["symbol"]))

    for (symbol,) in db.execute("SELECT DISTINCT symbol FROM slots").fetchall():
        rows = db.execute("SELECT slot, exec_price FROM slots WHERE symbol=? ORDER BY slot", (symbol,)).fetchall()
        if price_fingerprint(rows) != checks.get(f"exec_price {symbol}"):
            failures.append(f"exec_price {symbol}")
        start = min(datetime.fromisoformat(r[0]).date() for r in rows)
        end = max(datetime.fromisoformat(r[0]).date() for r in rows)
        actions = sorted(
            (str(day), dividend, split) for day, dividend, split in market.corporate_actions(symbol)
            if start <= (day if isinstance(day, date) else date.fromisoformat(str(day))) <= end
        )
        if sha(json.dumps([list(a) for a in actions])) != checks.get(f"corporate_actions {symbol}"):
            failures.append(f"corporate_actions {symbol}")
        db.executemany("INSERT OR REPLACE INTO corporate_actions VALUES (?,?,?,?)",
                       [(symbol, day, dividend, split) for day, dividend, split in actions])

    for row in db.execute("SELECT slot, symbol, arm, user_prompt FROM decisions").fetchall():
        prompt = row["user_prompt"]
        if not prompt:
            continue

        def market_block(match):
            return arms_module._market_block(snapshot_from_json(snaps[(match[1], match[2])]), match[3] == "1")

        def row_block(match):
            return arms_module._row_indicators({"snapshot": snaps[(match[1], match[2])]})

        prompt = ROW_RE.sub(row_block, MARKET_RE.sub(market_block, prompt))
        key = f"user_prompt {row['slot']} {row['symbol']} {row['arm']}"
        if sha(prompt) != checks.get(key):
            failures.append(key)
        db.execute("UPDATE decisions SET user_prompt=? WHERE slot=? AND symbol=? AND arm=?",
                   (prompt, row["slot"], row["symbol"], row["arm"]))

    db.execute(f"DROP TABLE {REMOVED_TABLE}")
    db.execute("DELETE FROM meta WHERE key='prices'")
    db.commit()
    db.close()
    total = len(checks)
    print(f"rebuilt {dst}: {total - len(failures)} of {total} checksums match")
    for item in failures[:20]:
        print(f"  mismatch: {item}")
    if failures:
        print("Yahoo may have revised some bars since the run; mismatched items differ from what the model saw.")
    return 1 if failures else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["strip", "rebuild"])
    parser.add_argument("source")
    parser.add_argument("target")
    args = parser.parse_args()
    if args.command == "strip":
        strip(args.source, args.target)
    else:
        sys.exit(rebuild(args.source, args.target))


if __name__ == "__main__":
    main()
