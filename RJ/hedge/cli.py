"""CLI entry point: argument parsing, interactive mode, and main()."""

import argparse
import sys
from datetime import date
from typing import Optional

from .compat import HAS_SCIPY, HAS_TABULATE, HAS_IBKR
from .data import fetch_live_yfinance, fetch_ibkr_chain
from .legs import classify_vix, nearest_expiry, PUT_LEGS, build_put_spine, build_call_engine
from .output import check_roll_alerts, print_position_sheet
from .scenario import compute_scenario_pnl, print_scenario_sheet, compute_1year_table, print_1year_table
from .greeks import compute_portfolio_greeks, compute_vanna_neutral_adjustment, print_greeks_summary


def parse_args():
    p = argparse.ArgumentParser(
        description="Vanna-Neutral 3-1-1 Hedge Calculator for a $1M SPX portfolio.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python RJ.py --spot 741.28 --vix 18.5
  python RJ.py --live                              # auto-fetch XSP + VIX via yfinance
  python RJ.py --live --ibkr                       # yfinance + IBKR TWS option chain
  python RJ.py --live --ibkr --ibkr-port 4001      # IB Gateway (paper: 4002, live: 4001)
  python RJ.py --spot 741.28 --vix 18.5 --iv 19.2 --rate 4.5 --notional 1000000
  python RJ.py --spot 741.28 --vix 18.5 --last-roll-date 2026-03-15 --last-roll-spot 710
  python RJ.py  (interactive mode — prompts for all inputs)
        """
    )
    p.add_argument("--spot",            type=float, help="XSP spot price (e.g. 741.28)")
    p.add_argument("--vix",             type=float, help="VIX level (e.g. 18.5)")
    p.add_argument("--iv",              type=float, default=None,
                   help="6M ATM implied vol in %% (e.g. 19.2). Defaults to VIX×1.1.")
    p.add_argument("--rate",            type=float, default=4.5,
                   help="Risk-free rate in %% (default: 4.5)")
    p.add_argument("--notional",        type=float, default=1_000_000,
                   help="Portfolio notional in USD (default: 1000000)")
    p.add_argument("--date",            type=str,   default=None,
                   help="Calculation date YYYY-MM-DD (default: today)")
    p.add_argument("--last-roll-date",  type=str,   default=None,
                   help="Date of last roll YYYY-MM-DD (enables roll alerts)")
    p.add_argument("--last-roll-spot",  type=float, default=None,
                   help="XSP spot at last roll (enables rally/drop alerts)")
    p.add_argument("--live",            action="store_true",
                   help="Fetch XSP spot and VIX live from Yahoo Finance (yfinance)")
    p.add_argument("--ibkr",            action="store_true",
                   help="Fetch live option chain + real deltas from IBKR TWS/Gateway (ib_insync)")
    p.add_argument("--ibkr-host",       type=str,   default="127.0.0.1")
    p.add_argument("--ibkr-port",       type=int,   default=7497,
                   help="TWS port 7497 (paper 7496) or IB Gateway 4001 (paper 4002). Default: 7497")
    p.add_argument("--ibkr-client-id",  type=int,   default=10)
    p.add_argument("--scenario",        type=float, default=None,
                   help="Hypothetical XSP spot price — shows hedge P&L vs. portfolio loss at that level")
    p.add_argument("--auto-vanna",      action="store_true",
                   help="Auto-adjust Short Subsidy quantity to achieve vanna-neutral portfolio")
    return p.parse_args()


def prompt_float(label: str, default: Optional[float] = None) -> float:
    default_str = f" [{default}]" if default is not None else ""
    while True:
        raw = input(f"  {label}{default_str}: ").strip()
        if raw == "":
            if default is not None:
                return default
            print("    Please enter a number.")
            continue
        try:
            return float(raw)
        except ValueError:
            print("    Please enter a number.")


def prompt_optional_float(label: str) -> Optional[float]:
    raw = input(f"  {label}: ").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        print("    Invalid number — skipping.")
        return None


def interactive_mode():
    print("\n  === Vanna-Neutral 3-1-1 Hedge Calculator (Interactive) ===\n")
    spot = prompt_float("XSP Spot Price")
    vix = prompt_float("VIX Level")
    iv_raw = prompt_optional_float("6M Implied Vol % (Enter to use VIX×1.1)")
    iv = (iv_raw / 100.0) if iv_raw else None
    rate = prompt_float("Risk-Free Rate %", 4.5) / 100.0
    notional = prompt_float("Portfolio Notional ($)", 1_000_000)
    date_raw = input(f"  Calculation Date [today={date.today()}]: ").strip()
    calc_date = date.fromisoformat(date_raw) if date_raw else date.today()
    lrd_raw = input("  Last Roll Date YYYY-MM-DD (Enter to skip): ").strip()
    last_roll_date = date.fromisoformat(lrd_raw) if lrd_raw else None
    lrs_raw = input("  Last Roll XSP Spot (Enter to skip): ").strip()
    last_roll_spot = float(lrs_raw) if lrs_raw else None
    return spot, vix, iv, rate, notional, calc_date, last_roll_date, last_roll_spot


def main():
    missing = []
    if not HAS_SCIPY:
        missing.append("scipy")
    if not HAS_TABULATE:
        missing.append("tabulate")
    if missing:
        print(f"\n[WARNING] Missing optional packages: {', '.join(missing)}")
        print(f"  Install with: pip install {' '.join(missing)}")
        print("  Falling back to built-in implementations (slightly less accurate).\n")

    args = parse_args()

    # ── Step 1: resolve spot + VIX ────────────────────────────────────────────
    data_source_parts = []

    if args.live:
        try:
            spot, vix = fetch_live_yfinance()
            data_source_parts.append("yfinance (live)")
        except RuntimeError as e:
            print(f"[ERROR] {e}")
            sys.exit(1)
    elif args.spot is not None and args.vix is not None:
        spot = args.spot
        vix = args.vix
        data_source_parts.append("manual")
    else:
        spot, vix, _iv, _rate, _notional, _calc_date, _lrd, _lrs = interactive_mode()
        args.iv = _iv * 100.0 if _iv else None
        args.rate = _rate * 100.0
        args.notional = _notional
        args.date = _calc_date.isoformat()
        args.last_roll_date = _lrd.isoformat() if _lrd else None
        args.last_roll_spot = _lrs
        data_source_parts.append("interactive")

    iv = (args.iv / 100.0) if args.iv else None
    rate = args.rate / 100.0
    notional = args.notional
    calc_date = date.fromisoformat(args.date) if args.date else date.today()
    last_roll_date = date.fromisoformat(args.last_roll_date) if args.last_roll_date else None
    last_roll_spot = args.last_roll_spot

    if iv is None:
        iv = (vix * 1.1) / 100.0
    elif iv > 1.0:
        iv = iv / 100.0

    if rate > 1.0:
        rate = rate / 100.0

    # ── Step 2: optional IBKR chain fetch ─────────────────────────────────────
    ibkr_chain = None

    if args.ibkr:
        if not HAS_IBKR:
            print("[ERROR] ib_insync not installed. Run: pip install ib_insync")
            sys.exit(1)

        zone_tmp, _, put_tenor_tmp, _ = classify_vix(vix)
        put_expiry = nearest_expiry(calc_date, put_tenor_tmp, avoid_october=True)
        call_expiry = nearest_expiry(calc_date, 2.2, avoid_october=False)

        chain_requests_expiries = []
        chain_requests_rights = []
        chain_requests_deltas = []
        seen = set()
        for leg_def in PUT_LEGS:
            key = (put_expiry, "P")
            if key not in seen:
                seen.add(key)
                chain_requests_expiries.append(put_expiry)
                chain_requests_rights.append("P")
                chain_requests_deltas.append(leg_def["target_delta"])
        key = (call_expiry, "C")
        if key not in seen:
            seen.add(key)
            chain_requests_expiries.append(call_expiry)
            chain_requests_rights.append("C")
            chain_requests_deltas.append(0.04)

        try:
            ibkr_chain = fetch_ibkr_chain(
                expiry_dates=chain_requests_expiries,
                option_rights=chain_requests_rights,
                target_deltas=chain_requests_deltas,
                host=args.ibkr_host,
                port=args.ibkr_port,
                client_id=args.ibkr_client_id,
            )
            data_source_parts.append(f"IBKR TWS {args.ibkr_host}:{args.ibkr_port}")
        except RuntimeError as e:
            print(f"[WARN] IBKR fetch failed ({e}). Falling back to BS estimates.")

    data_source = " + ".join(data_source_parts) if data_source_parts else "manual"

    # ── Step 3: build positions ────────────────────────────────────────────────
    put_legs, zone, mult = build_put_spine(spot, vix, iv, rate, notional, calc_date,
                                           ibkr_chain=ibkr_chain)
    call_legs = build_call_engine(spot, vix, iv, rate, notional, calc_date,
                                  ibkr_chain=ibkr_chain)

    alerts = check_roll_alerts(calc_date, spot, vix, last_roll_date, last_roll_spot)

    print_position_sheet(
        spot, vix, iv, rate, notional, calc_date,
        put_legs, call_legs, zone, mult,
        last_roll_date, last_roll_spot, alerts,
        data_source=data_source,
    )

    # ── Step 4: portfolio Greeks ───────────────────────────────────────────────
    leg_greeks = compute_portfolio_greeks(put_legs, call_legs, spot, iv, rate, calc_date)
    adjustment = compute_vanna_neutral_adjustment(leg_greeks, put_legs, spot, iv, rate, calc_date)

    if args.auto_vanna and adjustment:
        print(f"\n  [auto-vanna] Rebuilding with Short Subsidy qty "
              f"{adjustment['current_qty']} → {adjustment['recommended_qty']} contracts ...")
        put_legs, zone, mult = build_put_spine(
            spot, vix, iv, rate, notional, calc_date,
            ibkr_chain=ibkr_chain,
            subsidy_qty=adjustment["recommended_qty"],
        )
        leg_greeks = compute_portfolio_greeks(put_legs, call_legs, spot, iv, rate, calc_date)
        adjustment = None  # already applied — suppress suggestion

    print_greeks_summary(leg_greeks, adjustment=adjustment)

    # ── Step 5: 1-year cost table ──────────────────────────────────────────────
    net_put = sum(l.est_total for l in put_legs)
    net_call = sum(l.est_total for l in call_legs)
    year_rows = compute_1year_table(
        spot=spot, put_legs=put_legs, call_legs=call_legs,
        notional=notional, net_put_per_roll=net_put, net_call_per_month=net_call,
    )
    print_1year_table(year_rows, notional)

    # ── Step 6: optional scenario analysis ────────────────────────────────────
    if args.scenario is not None:
        leg_results, hedge_pnl, portfolio_pnl, net_pnl = compute_scenario_pnl(
            spot_orig=spot, spot_scenario=args.scenario,
            iv=iv, rate=rate, calc_date=calc_date,
            put_legs=put_legs, call_legs=call_legs, notional=notional,
        )
        print_scenario_sheet(
            spot_orig=spot, spot_scenario=args.scenario,
            leg_results=leg_results, hedge_pnl=hedge_pnl,
            portfolio_pnl=portfolio_pnl, net_pnl=net_pnl, notional=notional,
        )
