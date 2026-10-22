"""Read paper positions and realized results without starting the bot."""

import json
import sqlite3
from pathlib import Path


def main():
    path = Path(__file__).resolve().parent / "logs" / "us-paper.sqlite"
    if not path.exists():
        print("No US paper trades yet. Start: python bot.py")
        return
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        for (raw,) in db.execute("SELECT data FROM trades ORDER BY rowid"):
            row = json.loads(raw)
            print(
                f"{row.get('trade_id', row['slug'])} | {row['outcome']} | {row['state']} | "
                f"remaining={row['quantity']} | cost=${row['entry_cost']} | "
                f"realized P&L={row['pnl'] if row['pnl'] is not None else 'pending'}"
            )


if __name__ == "__main__":
    main()
