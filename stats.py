"""
stats.py — Trade performance summary from trades.csv

Usage:
  python stats.py              # reads trades.csv in current directory
  python stats.py --csv PATH   # specify CSV path
  python stats.py --days N     # limit to last N days (default: all)
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


# ─── Data loading ────────────────────────────────────────────────────────────────

def load_trades(csv_path: str, since: Optional[datetime] = None) -> list[dict]:
    """
    Load and pair OPEN/RESOLVE rows from trades.csv.
    Returns list of completed trade dicts (only rows with a matching RESOLVE).
    """
    path = Path(csv_path)
    if not path.exists():
        print(f"ERROR: {csv_path} not found. Run the bot first.")
        sys.exit(1)

    opens: dict[str, dict] = {}
    trades: list[dict] = []

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["event_type"] == "OPEN":
                opens[row["position_id"]] = row
            elif row["event_type"] == "RESOLVE":
                open_row = opens.get(row["position_id"])
                if open_row is None:
                    continue  # orphan — skip
                try:
                    t = {
                        "position_id": row["position_id"],
                        "market_slug": row["market_slug"],
                        "asset": row["asset"],
                        "interval": row["interval"],
                        "side": row["side"],
                        "entry_price": float(open_row["entry_price"]),
                        "size_shares": float(open_row["size_shares"]),
                        "cost_usd": float(open_row["cost_usd"]),
                        "fee_usd": float(open_row["fee_usd"]),
                        "total_spent": float(open_row["total_spent"]),
                        "resolved_side": row["resolved_side"],
                        "won": row["won"] == "1",
                        "pnl_usd": float(row["pnl_usd"]),
                        "open_time": datetime.fromisoformat(open_row["event_time"]),
                        "resolve_time": datetime.fromisoformat(row["event_time"]),
                    }
                    if since is None or t["open_time"] >= since:
                        trades.append(t)
                except (ValueError, KeyError):
                    continue

    return trades


# ─── Stats computation ────────────────────────────────────────────────────────────

def compute_stats(trades: list[dict]) -> dict:
    n = len(trades)
    if n == 0:
        return {}

    wins = [t for t in trades if t["won"]]
    losses = [t for t in trades if not t["won"]]
    total_pnl = sum(t["pnl_usd"] for t in trades)
    total_spent = sum(t["total_spent"] for t in trades)
    roi = total_pnl / total_spent if total_spent > 0 else 0.0

    # Running P&L and max drawdown
    sorted_trades = sorted(trades, key=lambda t: t["open_time"])
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted_trades:
        running += t["pnl_usd"]
        peak = max(peak, running)
        max_dd = min(max_dd, running - peak)

    # Streak analysis
    max_win_streak = max_loss_streak = 0
    cur_streak = 0
    last_won = None
    for t in sorted_trades:
        if t["won"] == last_won:
            cur_streak += 1
        else:
            cur_streak = 1
            last_won = t["won"]
        if t["won"]:
            max_win_streak = max(max_win_streak, cur_streak)
        else:
            max_loss_streak = max(max_loss_streak, cur_streak)

    # By asset
    by_asset: dict[str, list] = defaultdict(list)
    for t in trades:
        by_asset[t["asset"]].append(t)

    # By interval
    by_interval: dict[str, list] = defaultdict(list)
    for t in trades:
        by_interval[t["interval"]].append(t)

    # By hour of day
    by_hour: dict[int, list] = defaultdict(list)
    for t in trades:
        by_hour[t["open_time"].hour].append(t)

    # Avg pnl per winner / loser
    avg_win_pnl = sum(t["pnl_usd"] for t in wins) / len(wins) if wins else 0.0
    avg_loss_pnl = sum(t["pnl_usd"] for t in losses) / len(losses) if losses else 0.0
    profit_factor = (-sum(t["pnl_usd"] for t in wins) / sum(t["pnl_usd"] for t in losses)
                     if losses and sum(t["pnl_usd"] for t in losses) < 0 else float("inf"))

    return {
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / n,
        "total_pnl": total_pnl,
        "total_spent": total_spent,
        "roi": roi,
        "max_drawdown": max_dd,
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "avg_win_pnl": avg_win_pnl,
        "avg_loss_pnl": avg_loss_pnl,
        "profit_factor": profit_factor,
        "by_asset": dict(by_asset),
        "by_interval": dict(by_interval),
        "by_hour": dict(by_hour),
        "first_trade": sorted_trades[0]["open_time"] if sorted_trades else None,
        "last_trade": sorted_trades[-1]["open_time"] if sorted_trades else None,
    }


# ─── Display ─────────────────────────────────────────────────────────────────────

def _sub_stats(label: str, sub: list[dict]) -> None:
    n = len(sub)
    wins = sum(1 for t in sub if t["won"])
    pnl = sum(t["pnl_usd"] for t in sub)
    spent = sum(t["total_spent"] for t in sub)
    roi = pnl / spent if spent > 0 else 0.0
    print(f"    {label:<18}  n={n:4d}  wr={wins/n:.1%}  P&L={pnl:+.2f}  ROI={roi:+.1%}")


def print_summary(stats: dict, title: str = "Trade Performance Summary") -> None:
    if not stats:
        print("No completed trades found.")
        return

    s = stats
    sep = "─" * 56

    print(f"\n{'='*56}")
    print(f"  {title}")
    if s["first_trade"]:
        print(f"  {s['first_trade'].strftime('%Y-%m-%d')} → {s['last_trade'].strftime('%Y-%m-%d')}")
    print(f"{'='*56}")

    print(f"\n  {'Trades:':<22} {s['n']}")
    print(f"  {'Win / Loss:':<22} {s['wins']} / {s['losses']}")
    print(f"  {'Win rate:':<22} {s['win_rate']:.1%}")
    print(f"  {'Total P&L:':<22} ${s['total_pnl']:+.4f}")
    print(f"  {'Total spent:':<22} ${s['total_spent']:.2f}")
    print(f"  {'ROI:':<22} {s['roi']:+.2%}")
    print(f"  {'Max drawdown:':<22} ${s['max_drawdown']:.4f}")
    print(f"  {'Profit factor:':<22} {s['profit_factor']:.2f}")
    print(f"  {'Avg win P&L:':<22} ${s['avg_win_pnl']:+.4f}")
    print(f"  {'Avg loss P&L:':<22} ${s['avg_loss_pnl']:+.4f}")
    print(f"  {'Max win streak:':<22} {s['max_win_streak']}")
    print(f"  {'Max loss streak:':<22} {s['max_loss_streak']}")

    print(f"\n  {sep}")
    print(f"  By asset:")
    for asset, sub in sorted(s["by_asset"].items()):
        _sub_stats(asset, sub)

    print(f"\n  By interval:")
    for interval, sub in sorted(s["by_interval"].items()):
        _sub_stats(interval, sub)

    # Top 5 hours by trade count
    by_hour = s["by_hour"]
    if by_hour:
        print(f"\n  By hour (UTC) — top 5 most active:")
        top_hours = sorted(by_hour.items(), key=lambda x: len(x[1]), reverse=True)[:5]
        for hour, sub in top_hours:
            wins = sum(1 for t in sub if t["won"])
            pnl = sum(t["pnl_usd"] for t in sub)
            print(f"    {hour:02d}:00  n={len(sub):4d}  wr={wins/len(sub):.1%}  P&L={pnl:+.2f}")

    print()


# ─── Daily breakdown ─────────────────────────────────────────────────────────────

def print_daily(trades: list[dict]) -> None:
    if not trades:
        return

    by_day: dict[str, list] = defaultdict(list)
    for t in sorted(trades, key=lambda x: x["open_time"]):
        day = t["open_time"].strftime("%Y-%m-%d")
        by_day[day].append(t)

    print(f"\n{'─'*56}")
    print(f"  Daily breakdown:")
    print(f"{'─'*56}")
    print(f"  {'Date':<12}  {'N':>4}  {'W/L':>6}  {'WR':>6}  {'P&L':>10}  {'Spent':>8}")

    for day in sorted(by_day):
        sub = by_day[day]
        wins = sum(1 for t in sub if t["won"])
        pnl = sum(t["pnl_usd"] for t in sub)
        spent = sum(t["total_spent"] for t in sub)
        print(f"  {day}  {len(sub):>4}  {wins:>2}/{len(sub)-wins:<2}  "
              f"{wins/len(sub):>5.1%}  ${pnl:>+8.4f}  ${spent:>6.2f}")


# ─── CLI ─────────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Polymarket bot trade stats")
    p.add_argument("--csv", default="trades.csv", help="Path to trades.csv")
    p.add_argument("--days", type=int, default=0,
                   help="Only show last N days (default: all)")
    p.add_argument("--daily", action="store_true",
                   help="Show daily breakdown table")
    args = p.parse_args()

    since = None
    if args.days > 0:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)

    trades = load_trades(args.csv, since=since)

    title = "Trade Performance Summary"
    if args.days > 0:
        title += f" (last {args.days} days)"

    stats = compute_stats(trades)
    print_summary(stats, title)

    if args.daily:
        print_daily(trades)


if __name__ == "__main__":
    main()
