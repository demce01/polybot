"""
Terminal dashboard built with Rich.

Layout:
  ┌──────────────────────────────────────────────────────────────┐
  │  STATUS BAR  mode | balance | daily P&L | win rate           │
  ├────────────────┬─────────────────────────────────────────────┤
  │  MARKETS       │  OPEN POSITIONS  (with net P&L column)      │
  │  (live data)   │                                              │
  ├────────────────┴─────────────────────────────────────────────┤
  │  RESOLVED POSITIONS  (win / loss history)                     │
  ├──────────────────────────────────────────────────────────────┤
  │  ACTIVITY LOG                                                  │
  └──────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timezone
from typing import Optional

from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from models import BotState, MarketInfo, Position, PositionStatus, Side


# ── Colour scheme ────────────────────────────────────────────────────────────────
_GREEN = "bright_green"
_RED = "bright_red"
_YELLOW = "yellow"
_CYAN = "cyan"
_DIM = "dim white"
_BOLD = "bold"


def _pnl_colour(v: float) -> str:
    if v > 0:
        return _GREEN
    elif v < 0:
        return _RED
    return _DIM


def _fmt_usd(v: float) -> str:
    sign = "+" if v > 0 else ""
    return f"{sign}${v:.2f}"


def _fmt_pct(v: float) -> str:
    return f"{v:.1%}"


def _fmt_countdown(secs: float) -> str:
    if secs <= 0:
        return "[dim]expired[/]"
    m = int(secs) // 60
    s = int(secs) % 60
    if m > 0:
        return f"[cyan]{m}m {s:02d}s[/]"
    elif secs <= 10:
        return f"[bold red]{s}s[/]"
    return f"[yellow]{s}s[/]"


# ── Header / status bar ──────────────────────────────────────────────────────────

def _render_header(state: BotState) -> Panel:
    mode_str = (
        Text("● LIVE", style="bold bright_green")
        if state.mode == "LIVE"
        else Text("● PAPER", style="bold yellow")
    )

    if state.is_halted:
        mode_str = Text(f"✖ HALTED — {state.halt_reason}", style="bold red")

    balance_str = Text(f"${state.balance_usdc:.2f}", style="bold white")
    pnl_val = state.daily_pnl
    pnl_str = Text(_fmt_usd(pnl_val), style=_pnl_colour(pnl_val))
    wr_str = Text(_fmt_pct(state.win_rate), style=_CYAN)

    btc = f"BTC ${state.btc_price:,.2f}" if state.btc_price else "BTC --"
    eth = f"ETH ${state.eth_price:,.2f}" if state.eth_price else "ETH --"

    cols = Columns([
        Panel(mode_str, title="Mode", padding=(0, 1)),
        Panel(balance_str, title="Balance", padding=(0, 1)),
        Panel(pnl_str, title="Daily P&L", padding=(0, 1)),
        Panel(wr_str, title="Win Rate", padding=(0, 1)),
        Panel(
            Text(f"{state.total_trades}", style="bold white"),
            title="Trades",
            padding=(0, 1),
        ),
        Panel(Text(f"{btc}  {eth}", style=_DIM), title="CEX Prices", padding=(0, 1)),
    ], equal=True)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return Panel(cols, title=f"[bold]Polymarket Latency Arb Bot[/]  [dim]{ts}[/]", box=box.DOUBLE_EDGE)


# ── Markets table ────────────────────────────────────────────────────────────────

def _render_markets(state: BotState) -> Panel:
    t = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style="bold cyan",
        expand=True,
    )
    t.add_column("Market", style="white", min_width=28)
    t.add_column("Expires", justify="right", min_width=10)
    t.add_column("Price to Beat", justify="right", min_width=14)
    t.add_column("Current CEX", justify="right", min_width=12)
    t.add_column("Δ%", justify="right", min_width=8)
    t.add_column("Up Ask", justify="right", min_width=8)
    t.add_column("Down Ask", justify="right", min_width=9)
    t.add_column("Edge", justify="right", min_width=8)

    now = _time.time()
    for slug, snap in sorted(state.market_snapshots.items()):
        tr = snap.get("measurement_end", 0) - now
        if tr <= 0:
            continue
        name = slug.replace("-updown-", " ").replace("-", " ").upper()
        expires = _fmt_countdown(tr)
        ptb = f"${snap['price_to_beat']:,.2f}" if snap.get("price_to_beat") else "--"
        cex = f"${snap['cex_price']:,.2f}" if snap.get("cex_price") else "--"
        delta_pct = snap.get("delta_pct", 0.0)
        delta_str = Text(f"{delta_pct:+.2f}%", style=_GREEN if delta_pct >= 0 else _RED)
        up_ask_v = snap.get("up_ask")
        dn_ask_v = snap.get("down_ask")
        up_ask = f"{up_ask_v:.3f}" if up_ask_v is not None else "--"
        dn_ask = f"{dn_ask_v:.3f}" if dn_ask_v is not None else "--"
        edge = snap.get("edge", None)
        edge_str = (
            Text(f"{edge:.1%}", style=_GREEN if edge and edge > 0.05 else _YELLOW)
            if edge is not None else Text("--", style=_DIM)
        )

        t.add_row(name, expires, ptb, cex, delta_str, up_ask, dn_ask, edge_str)

    return Panel(t, title="[bold cyan]Active Markets[/]", box=box.ROUNDED)


# ── Open positions table ─────────────────────────────────────────────────────────

def _render_open_positions(positions: list[Position]) -> Panel:
    open_pos = [p for p in positions if p.status == PositionStatus.OPEN]

    t = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style="bold magenta",
        expand=True,
    )
    t.add_column("Market", style="white", min_width=22)
    t.add_column("Side", justify="center", min_width=6)
    t.add_column("Entry", justify="right", min_width=7)
    t.add_column("Shares", justify="right", min_width=7)
    t.add_column("Cost", justify="right", min_width=8)
    t.add_column("Net Win", justify="right", min_width=9)
    t.add_column("Max Loss", justify="right", min_width=9)
    t.add_column("Expires", justify="right", min_width=10)

    for pos in sorted(open_pos, key=lambda p: p.entry_time, reverse=True):
        market_name = pos.market.slug.replace("-updown-", " ").replace("-", " ").upper()[:24]
        side_str = Text(pos.side.value, style=_GREEN if pos.side == Side.UP else _RED)
        entry = f"{pos.entry_price:.3f}"
        shares = f"{pos.size_shares:.1f}"
        cost = f"${pos.cost_usd:.2f}"
        # Net profit if the position wins (payout - total spent including fees)
        net_win = pos.size_shares - pos.total_spent_usd
        net_win_str = Text(f"+${net_win:.2f}", style=_GREEN)
        # Maximum loss is the total spent (cost + fees)
        max_loss_str = Text(f"-${pos.total_spent_usd:.2f}", style=_RED)
        expires = _fmt_countdown(pos.market.time_remaining())

        t.add_row(market_name, side_str, entry, shares, cost, net_win_str, max_loss_str, expires)

    label = f"[bold magenta]Open Positions ({len(open_pos)})[/]"
    return Panel(t, title=label, box=box.ROUNDED)


# ── Resolved positions table ─────────────────────────────────────────────────────

def _render_resolved_positions(positions: list[Position]) -> Panel:
    resolved = [p for p in positions if p.status != PositionStatus.OPEN]

    t = Table(
        box=box.SIMPLE,
        show_header=True,
        header_style="bold blue",
        expand=True,
    )
    t.add_column("Market", style="white", min_width=26)
    t.add_column("Side", justify="center", min_width=6)
    t.add_column("Entry", justify="right", min_width=7)
    t.add_column("Shares", justify="right", min_width=7)
    t.add_column("Cost", justify="right", min_width=8)
    t.add_column("Result", justify="center", min_width=8)
    t.add_column("P&L", justify="right", min_width=10)

    # Show most recent first, cap at 15 rows
    for pos in sorted(resolved, key=lambda p: p.entry_time, reverse=True)[:15]:
        market_name = pos.market.slug.replace("-updown-", " ").replace("-", " ").upper()[:28]
        side_str = Text(pos.side.value, style=_GREEN if pos.side == Side.UP else _RED)
        entry = f"{pos.entry_price:.3f}"
        shares = f"{pos.size_shares:.1f}"
        cost = f"${pos.cost_usd:.2f}"

        won = pos.status == PositionStatus.RESOLVED_WIN
        result_str = Text("WIN" if won else "LOSS", style=_GREEN if won else _RED)
        pnl = pos.pnl_usd or 0.0
        pnl_str = Text(_fmt_usd(pnl), style=_pnl_colour(pnl))

        t.add_row(market_name, side_str, entry, shares, cost, result_str, pnl_str)

    wins = sum(1 for p in resolved if p.status == PositionStatus.RESOLVED_WIN)
    total_pnl = sum(p.pnl_usd or 0.0 for p in resolved)
    label = (
        f"[bold blue]Resolved Positions ({len(resolved)})[/]"
        + (f"  [dim]{wins}W / {len(resolved) - wins}L[/]" if resolved else "")
        + (f"  [{'bright_green' if total_pnl >= 0 else 'bright_red'}]{_fmt_usd(total_pnl)}[/]"
           if resolved else "")
    )
    return Panel(t, title=label, box=box.ROUNDED)


# ── Activity log ─────────────────────────────────────────────────────────────────

def _render_log(lines: list[str]) -> Panel:
    last = lines[-12:] if len(lines) > 12 else lines
    content = "\n".join(last) if last else "[dim]No activity yet[/dim]"
    return Panel(content, title="[bold]Activity Log[/]", box=box.ROUNDED, padding=(0, 1))


# ── Dashboard ────────────────────────────────────────────────────────────────────

class Dashboard:
    """
    Maintains a Rich `Live` context and refreshes the display when `update()` is called.
    """

    def __init__(self) -> None:
        self._console = Console()
        self._live: Optional[Live] = None

    def __enter__(self) -> "Dashboard":
        self._live = Live(
            console=self._console,
            refresh_per_second=10,
            screen=True,
        )
        self._live.__enter__()
        return self

    def __exit__(self, *args) -> None:
        if self._live:
            self._live.__exit__(*args)

    def update(
        self,
        state: BotState,
        open_positions: list[Position],
        resolved_positions: list[Position],
    ) -> None:
        if self._live is None:
            return

        layout = Layout()
        layout.split_column(
            Layout(_render_header(state), name="header", size=8),
            Layout(name="mid_top"),
            Layout(_render_resolved_positions(resolved_positions), name="resolved", size=10),
            Layout(_render_log(state.log_lines), name="log", size=8),
        )
        layout["mid_top"].split_row(
            Layout(_render_markets(state), name="markets", ratio=6),
            Layout(_render_open_positions(open_positions), name="open_pos", ratio=4),
        )

        self._live.update(layout)
