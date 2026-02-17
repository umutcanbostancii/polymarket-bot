#!/usr/bin/env python3
"""Terminal TUI Dashboard - Rich Live Monitor for BTC 5-min Trading Bot."""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

try:
    from rich.console import Console
    from rich.columns import Columns
    from rich.layout import Layout
    from rich.live import Live
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
except ImportError:
    print("rich kütüphanesi gerekli: pip install rich>=13.0.0")
    sys.exit(1)

# ── Paths ─────────────────────────────────────────────────────────
STATUS_FILE = "/tmp/polymarket_bot_status.json"
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "trades.db")

# ── Cached data ───────────────────────────────────────────────────
_cache = {
    "status": {},
    "today": {},
    "trades": [],
    "positions": [],
    "pnl_history": [],
    "last_status_read": 0.0,
    "last_db_read": 0.0,
}

STATUS_INTERVAL = 5     # seconds
DB_INTERVAL = 10        # seconds


def _read_status() -> dict:
    """Read bot status JSON file."""
    now = time.time()
    if now - _cache["last_status_read"] < STATUS_INTERVAL:
        return _cache["status"]
    try:
        with open(STATUS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        _cache["status"] = data
    except (FileNotFoundError, json.JSONDecodeError, PermissionError):
        pass
    _cache["last_status_read"] = now
    return _cache["status"]


def _read_db():
    """Read trade data from SQLite."""
    now = time.time()
    if now - _cache["last_db_read"] < DB_INTERVAL:
        return
    if not os.path.exists(DB_PATH):
        _cache["last_db_read"] = now
        return
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5.0)
        conn.row_factory = sqlite3.Row

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Today stats
        total_row = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE DATE(ts)=?", (today,)
        ).fetchone()
        total = total_row[0] or 0

        r = conn.execute(
            """
            SELECT SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) as w,
                   SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END) as l,
                   COALESCE(SUM(pnl),0) as pnl,
                   COALESCE(MAX(pnl),0) as best,
                   COALESCE(MIN(pnl),0) as worst
            FROM trades
            WHERE DATE(ts)=? AND status='resolved'
            """,
            (today,),
        ).fetchone()
        wins = r[0] or 0
        _cache["today"] = {
            "trades": total,
            "wins": wins,
            "losses": r[1] or 0,
            "win_rate": (wins / total * 100) if total else 0,
            "pnl": r[2],
            "best": r[3],
            "worst": r[4],
        }

        # Recent trades
        rows = conn.execute(
            """
            SELECT id, ts as time, side, price, cost,
                   status, pnl
            FROM trades
            ORDER BY ts DESC
            LIMIT 10
            """
        ).fetchall()
        _cache["trades"] = [dict(r) for r in rows]

        # P&L history (7 days)
        rows = conn.execute(
            """
            SELECT DATE(ts) as date, COALESCE(SUM(pnl),0) as pnl
            FROM trades
            WHERE status='resolved'
              AND ts >= date('now', '-7 days')
            GROUP BY DATE(ts)
            ORDER BY date
            """
        ).fetchall()
        _cache["pnl_history"] = [dict(r) for r in rows]

        conn.close()
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        pass
    _cache["last_db_read"] = now


# ── Panel Builders ────────────────────────────────────────────────

def _fmt_pnl(val: float) -> Text:
    """Format P&L value with color."""
    s = f"${val:+.2f}"
    if val > 0:
        return Text(s, style="bold green")
    elif val < 0:
        return Text(s, style="bold red")
    return Text(s, style="dim")


def _fmt_uptime(seconds: int) -> str:
    if seconds <= 0:
        return "0m"
    h, m = divmod(seconds // 60, 60)
    if h > 0:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def build_header(status: dict) -> Panel:
    """Header: mode, BTC price, connections, uptime."""
    running = status.get("running", False)
    mode = status.get("mode", "???")

    if running:
        mode_text = Text(f" {mode} ", style="bold white on green" if mode == "PAPER" else "bold white on red")
    else:
        mode_text = Text(" STOPPED ", style="bold white on red")

    uptime = _fmt_uptime(status.get("uptime_seconds", 0))

    btc = status.get("btc_price")
    btc_str = f"${btc:,.2f}" if btc else "N/A"

    binance_ok = status.get("btc_connected", False)
    poly_ok = status.get("poly_healthy", False)
    binance_icon = Text("Binance:", style="dim") + Text("OK" if binance_ok else "X", style="green" if binance_ok else "red")
    poly_icon = Text("  Poly:", style="dim") + Text("OK" if poly_ok else "X", style="green" if poly_ok else "red")

    line1 = Text("  BTC 5-MIN TRADING BOT    ", style="bold cyan")
    line1.append_text(mode_text)
    line1.append(f"  ▲ {uptime}", style="dim")

    line2 = Text(f"  BTC: {btc_str}", style="bold yellow")
    line2.append("   ")
    line2.append_text(binance_icon)
    line2.append_text(poly_icon)

    content = Text()
    content.append_text(line1)
    content.append("\n")
    content.append_text(line2)

    return Panel(content, style="cyan", padding=(0, 1))


def build_today_table(today: dict) -> Table:
    """Today's stats table."""
    t = Table(title="TODAY", title_style="bold white", show_header=False, box=None, padding=(0, 1))
    t.add_column("key", style="dim", width=12)
    t.add_column("val", min_width=10)

    trades = today.get("trades", 0)
    wins = today.get("wins", 0)
    losses = today.get("losses", 0)
    wr = today.get("win_rate", 0)
    pnl = today.get("pnl", 0)
    best = today.get("best", 0)
    worst = today.get("worst", 0)

    t.add_row("Trades", str(trades))
    t.add_row("W / L", f"{wins} / {losses}")

    wr_style = "green" if wr >= 55 else ("yellow" if wr >= 45 else "red")
    t.add_row("Win Rate", Text(f"{wr:.1f}%", style=wr_style))
    t.add_row("P&L", _fmt_pnl(pnl))
    t.add_row("Best", _fmt_pnl(best))
    t.add_row("Worst", _fmt_pnl(worst))

    return t


def build_risk_table(status: dict) -> Table:
    """Risk status table."""
    t = Table(title="RISK", title_style="bold white", show_header=False, box=None, padding=(0, 1))
    t.add_column("key", style="dim", width=16)
    t.add_column("val", min_width=10)

    halted = status.get("risk_halted", False)
    reason = status.get("risk_reason", "")
    daily_pnl = status.get("daily_pnl", 0)
    consec = status.get("consecutive_losses", 0)

    if halted:
        t.add_row("Status", Text("HALTED", style="bold red"))
        t.add_row("Reason", Text(reason[:30], style="red"))
    else:
        t.add_row("Status", Text("OK", style="bold green"))

    t.add_row("Daily P&L", _fmt_pnl(daily_pnl))
    consec_style = "green" if consec < 3 else ("yellow" if consec < 5 else "red")
    max_consec = 3
    t.add_row("Consec Losses", Text(f"{consec} / {max_consec}", style=consec_style))
    t.add_row("Daily Limit", Text("$100.00", style="dim"))
    active_pos = status.get("active_positions", 0)
    t.add_row("Active Pos", Text(f"{active_pos} / 3", style="green" if active_pos < 3 else "yellow"))

    return t


def build_execution_health(status: dict) -> Table:
    """PDF s.36: Execution Health metrics."""
    t = Table(title="EXECUTION HEALTH", title_style="bold white", show_header=False, box=None, padding=(0, 1))
    t.add_column("key", style="dim", width=16)
    t.add_column("val", min_width=10)

    mon = status.get("monitoring", {})
    alarms = status.get("alarms", [])

    opp = mon.get("opportunities_per_min", 0)
    exe = mon.get("executions_per_min", 0)
    rate = mon.get("execution_rate_pct", 0)
    latency = mon.get("avg_latency_ms", 0)
    fq = mon.get("avg_fill_quality", 0)
    dd = mon.get("drawdown_pct", 0)
    profit = mon.get("total_profit", 0)

    t.add_row("Opp/min", Text(f"{opp}", style="cyan"))
    t.add_row("Exec/min", Text(f"{exe}", style="cyan"))

    rate_style = "green" if rate >= 30 else ("yellow" if rate >= 15 else "red")
    t.add_row("Exec Rate", Text(f"{rate:.0f}%", style=rate_style))
    t.add_row("Avg Latency", Text(f"{latency:.0f}ms", style="green" if latency < 100 else "yellow"))

    fq_style = "green" if fq > 0 else ("yellow" if fq > -0.5 else "red")
    t.add_row("Fill Quality", Text(f"{fq:+.3f}", style=fq_style))

    dd_style = "green" if dd < 5 else ("yellow" if dd < 15 else "red")
    t.add_row("Drawdown", Text(f"{dd:.1f}%", style=dd_style))
    t.add_row("Total Profit", _fmt_pnl(profit))

    ai = status.get("ai_enabled", False)
    t.add_row("AI", Text("ON" if ai else "OFF", style="green" if ai else "dim"))

    if alarms:
        for alarm in alarms[:2]:
            t.add_row("ALARM", Text(alarm[:25], style="bold red"))
    else:
        t.add_row("Alarms", Text("none", style="green"))

    return t


def build_stats_risk(status: dict, today: dict) -> Panel:
    """Side-by-side today stats + risk + execution health panel."""
    left = build_today_table(today)
    middle = build_risk_table(status)
    right = build_execution_health(status)
    cols = Columns([left, middle, right], expand=True, equal=True)
    return Panel(cols, style="blue")


def build_trades_table(trades: list) -> Panel:
    """Recent trades table."""
    t = Table(title="RECENT TRADES", title_style="bold white", box=None, padding=(0, 1))
    t.add_column("#", style="dim", width=5, justify="right")
    t.add_column("Time", width=10)
    t.add_column("Side", width=12)
    t.add_column("Price", width=7, justify="right")
    t.add_column("Cost", width=9, justify="right")
    t.add_column("P&L", width=9, justify="right")
    t.add_column("", width=3)

    if not trades:
        t.add_row("", "", Text("No trades yet", style="dim"), "", "", "", "")
    else:
        for tr in trades:
            tid = str(tr.get("id", ""))
            ts_raw = tr.get("time", "")
            try:
                ts_str = ts_raw[11:19] if len(ts_raw) > 19 else ts_raw[-8:]
            except Exception:
                ts_str = "?"

            side = tr.get("side", "?")
            side_style = "green" if "UP" in side.upper() else "red"

            price = tr.get("price", 0)
            cost = tr.get("cost", 0)
            pnl = tr.get("pnl", 0)
            status = tr.get("status", "open")

            if status == "resolved":
                icon = Text("W", style="bold green") if pnl >= 0 else Text("L", style="bold red")
            else:
                icon = Text("~", style="yellow")

            t.add_row(
                tid,
                ts_str,
                Text(side, style=side_style),
                f"{price:.2f}",
                f"${cost:.2f}",
                _fmt_pnl(pnl) if status == "resolved" else Text("open", style="yellow"),
                icon,
            )

    return Panel(t, style="blue")


def build_pnl_history(history: list) -> Panel:
    """7-day P&L trend."""
    t = Table(title="7-DAY P&L TREND", title_style="bold white", box=None, padding=(0, 1))
    t.add_column("Date", width=12)
    t.add_column("P&L", width=10, justify="right")
    t.add_column("", width=3)  # bar

    total = 0.0
    if not history:
        t.add_row(Text("No data", style="dim"), "", "")
    else:
        max_abs = max(abs(d.get("pnl", 0)) for d in history) or 1
        for d in history:
            date = d.get("date", "?")
            pnl = d.get("pnl", 0)
            total += pnl

            # Mini bar
            bar_len = int(abs(pnl) / max_abs * 8) + 1
            bar_char = "█" * bar_len
            bar_style = "green" if pnl >= 0 else "red"

            try:
                short = datetime.strptime(date, "%Y-%m-%d").strftime("%b %d")
            except Exception:
                short = date

            t.add_row(short, _fmt_pnl(pnl), Text(bar_char, style=bar_style))

    t.add_row("", "", "")
    total_style = "bold green" if total >= 0 else "bold red"
    t.add_row(Text("Total", style="bold"), Text(f"${total:+.2f}", style=total_style), "")

    return Panel(t, style="blue")


def build_footer() -> Panel:
    """Footer with timestamp and controls."""
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    txt = Text(f"  Last update: {now}  │  Refresh: {STATUS_INTERVAL}s  │  Ctrl+C = quit", style="dim")
    return Panel(txt, style="dim cyan", padding=(0, 0))


def build_dashboard() -> Layout:
    """Compose full dashboard layout."""
    _read_db()
    status = _read_status()
    today = _cache.get("today", {})
    trades = _cache.get("trades", [])
    history = _cache.get("pnl_history", [])

    layout = Layout()
    layout.split_column(
        Layout(build_header(status), name="header", size=5),
        Layout(build_stats_risk(status, today), name="stats", size=15),
        Layout(build_trades_table(trades), name="trades", size=15),
        Layout(build_pnl_history(history), name="pnl", size=14),
        Layout(build_footer(), name="footer", size=3),
    )
    return layout


def main():
    console = Console()
    console.clear()

    try:
        with Live(build_dashboard(), console=console, refresh_per_second=1, screen=True) as live:
            while True:
                time.sleep(STATUS_INTERVAL)
                live.update(build_dashboard())
    except KeyboardInterrupt:
        pass

    console.print("\n[dim]Dashboard kapatıldı.[/dim]")


if __name__ == "__main__":
    main()
