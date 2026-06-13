"""Formatters, roll alerts, and the main position-sheet printer."""

from datetime import date, timedelta
from typing import List, Optional, Tuple

from .compat import HAS_TABULATE, tabulate
from .legs import VIX_ZONE_NAMES, XSP_MULTIPLIER


def _fmt_dollar(n: float) -> str:
    return f"+${n:,.0f}" if n >= 0 else f"-${abs(n):,.0f}"


def _fmt_prem(n: float) -> str:
    return f"${n:.2f}"


def _alert_prefix(status: str) -> str:
    return {"OK": "[OK]   ", "WARN": "[WARN] ", "ALERT": "[!!!!] ", "INFO": "[INFO] "}.get(status, "       ")


def check_roll_alerts(
    calc_date: date, spot: float, vix: float,
    last_roll_date: Optional[date], last_roll_spot: Optional[float],
) -> List[Tuple[str, str, str]]:
    alerts = []

    if last_roll_date is None:
        alerts.append(("INFO", "Roll Alerts", "Provide --last-roll-date and --last-roll-spot to enable roll alerts."))
        return alerts

    days_since_roll = (calc_date - last_roll_date).days
    next_roll = last_roll_date + timedelta(days=90)

    if days_since_roll >= 90:
        alerts.append(("ALERT", "Scheduled Roll",
                       f"Due NOW — {days_since_roll} days since last roll. Full rebuild required."))
    else:
        days_left = (next_roll - calc_date).days
        alerts.append(("OK", "Scheduled Roll",
                       f"Next roll due {next_roll.strftime('%Y-%m-%d')} ({days_left} days)."))

    if last_roll_spot:
        pct_change = (spot - last_roll_spot) / last_roll_spot * 100
        if pct_change <= -10 and days_since_roll <= 10:
            alerts.append(("ALERT", "Fast Drop Rule",
                           f"Spot dropped {pct_change:.1f}% in {days_since_roll}d. "
                           f"Roll SHORT SUBSIDY down to restore –0.22δ. Do NOT touch anchor."))
        elif pct_change <= -10:
            alerts.append(("WARN", "Fast Drop Rule",
                           f"Spot down {pct_change:.1f}% since last roll. "
                           f"Monitor short subsidy delta — roll if delta breaches –0.25."))
        else:
            alerts.append(("OK", "Fast Drop Rule",
                           f"No action. Spot {pct_change:+.1f}% from last roll."))

        if pct_change >= 10:
            alerts.append(("ALERT", "Rally Rule",
                           f"Spot rallied {pct_change:.1f}%. Roll anchor PUT UP to restore –15% deductible. "
                           f"Roll call spread up."))
        else:
            alerts.append(("OK", "Rally Rule",
                           f"No action. Spot {pct_change:+.1f}% from last roll."))

    if vix > 35:
        alerts.append(("ALERT", "VIX Shock",
                       f"VIX={vix:.1f} (Zone 5). Apply 40% sizing immediately — do not wait for quarterly roll."))
    elif vix > 28:
        alerts.append(("WARN", "VIX Shock",
                       f"VIX={vix:.1f} (Zone 4). Consider reducing to 60% sizing if VIX jumped >8pts recently."))
    else:
        alerts.append(("OK", "VIX Shock",
                       f"VIX={vix:.1f} — no shock override needed."))

    return alerts


def print_position_sheet(
        spot: float, vix: float, iv: float, rate: float, notional: float,
        calc_date: date, put_legs, call_legs, zone: int, mult: float,
        last_roll_date, last_roll_spot, alerts,
        data_source: str = "manual",
):
    W = 72
    LINE = "=" * W
    DASH = "-" * W

    print(f"\n{LINE}")
    print(f"  VANNA-NEUTRAL 3-1-1 HEDGE CALCULATOR")
    print(LINE)
    print(f"  Date       : {calc_date.strftime('%Y-%m-%d')}    Notional : ${notional:,.0f}")
    print(f"  XSP Spot   : {spot:.2f}           VIX      : {vix:.1f}")
    print(f"  IV (6M)    : {iv * 100:.1f}%            Rfr      : {rate * 100:.2f}%")
    print(f"  VIX ZONE   : {zone} — {VIX_ZONE_NAMES[zone]}  (sizing: {mult * 100:.0f}%)")
    print(f"  Data Source: {data_source}")
    print(DASH)

    # ── PUT SPINE ────────────────────────────────────────────────────────────
    put_expiries = sorted(set(l.expiry_date for l in put_legs))
    exp_str = ", ".join(f"{e.strftime('%b %d %Y')} ({(e - calc_date).days}d)" for e in put_expiries)
    print(f"\n  PUT SPINE  — {exp_str}")
    print(DASH)

    put_headers = ["Leg", "Act", "Qty", "Target δ", "Strike", "Expiry", "Prem/sh", "Total"]
    put_rows = []
    for l in put_legs:
        put_rows.append([
            l.name, l.action, l.quantity,
            f"{l.target_delta:+.3f}", f"{l.computed_strike:.0f}",
            l.expiry_date.strftime("%b %Y"),
            _fmt_prem(l.est_premium), _fmt_dollar(l.est_total),
        ])

    net_put = sum(l.est_total for l in put_legs)
    put_rows.append(["─" * 22, "", "", "", "", "", "", ""])
    put_rows.append(["NET PUT SPINE", "", sum(l.quantity for l in put_legs),
                     "", "", "", "", _fmt_dollar(net_put)])

    if HAS_TABULATE:
        print(tabulate(put_rows, headers=put_headers, tablefmt="simple",
                       colalign=("left", "center", "right", "center", "right", "center", "right", "right")))
    else:
        print(f"  {'Leg':<26} {'Act':^4} {'Qty':>4} {'δ':>8} {'Strike':>7} {'Expiry':^8} {'Prem':>7} {'Total':>10}")
        for r in put_rows:
            print(f"  {str(r[0]):<26} {str(r[1]):^4} {str(r[2]):>4} {str(r[3]):>8} {str(r[4]):>7} {str(r[5]):^8} {str(r[6]):>7} {str(r[7]):>10}")

    # ── CALL ENGINE ──────────────────────────────────────────────────────────
    call_expiry = call_legs[0].expiry_date if call_legs else calc_date
    call_dte = (call_expiry - calc_date).days
    print(f"\n  CALL ENGINE — {call_expiry.strftime('%b %d %Y')} ({call_dte}d)")
    print(DASH)

    call_headers = ["Leg", "Act", "Qty", "Target δ", "Strike", "Prem/sh", "Total"]
    call_rows = []
    for l in call_legs:
        call_rows.append([
            l.name, l.action, l.quantity,
            f"{l.target_delta:+.4f}", f"{l.computed_strike:.0f}",
            _fmt_prem(l.est_premium), _fmt_dollar(l.est_total),
        ])

    net_call = sum(l.est_total for l in call_legs)
    call_rows.append(["─" * 22, "", "", "", "", "", ""])
    call_rows.append(["NET CALL CREDIT", "", sum(l.quantity for l in call_legs),
                      "", "", "", _fmt_dollar(net_call)])

    if HAS_TABULATE:
        print(tabulate(call_rows, headers=call_headers, tablefmt="simple",
                       colalign=("left", "center", "right", "center", "right", "right", "right")))
    else:
        print(f"  {'Leg':<26} {'Act':^4} {'Qty':>4} {'δ':>8} {'Strike':>7} {'Prem':>7} {'Total':>10}")
        for r in call_rows:
            print(f"  {str(r[0]):<26} {str(r[1]):^4} {str(r[2]):>4} {str(r[3]):>8} {str(r[4]):>7} {str(r[5]):>7} {str(r[6]):>10}")

    # ── SUMMARY ──────────────────────────────────────────────────────────────
    print(f"\n{LINE}")
    print("  COST SUMMARY")
    print(DASH)

    ann_call = net_call * 12
    ann_put = net_put * 2
    ann_net = ann_put + ann_call
    ann_pct = ann_net / notional * 100

    total_put_contracts = sum(l.quantity for l in put_legs)
    total_call_contracts = sum(l.quantity for l in call_legs)
    total_contracts = total_put_contracts + total_call_contracts

    print(f"  Net put spine cost (6M cycle)    : {_fmt_dollar(net_put)}")
    print(f"  Net call engine credit (monthly) : {_fmt_dollar(net_call)}")
    print(f"  Annualized put cost (×2 rolls)   : {_fmt_dollar(ann_put)}")
    print(f"  Annualized call income (×12)     : {_fmt_dollar(ann_call)}")
    print(f"  ─────────────────────────────────────────────")
    carry_label = "POSITIVE CARRY" if ann_net >= 0 else "Net annual cost"
    print(f"  {carry_label:<32} : {_fmt_dollar(ann_net)}  ({ann_pct:+.2f}% of notional)")
    print(f"")
    print(f"  Total open contracts at any time : {total_contracts}")
    print(f"    Put spine : {total_put_contracts} contracts")
    print(f"    Call engine: {total_call_contracts} contracts")

    gross_put_notional = sum(
        l.computed_strike * l.quantity * XSP_MULTIPLIER
        for l in put_legs if l.action == "BUY"
    )
    coverage_pct = gross_put_notional / notional * 100
    if coverage_pct < 50 or coverage_pct > 500:
        print(f"\n  [WARN] Long put notional ${gross_put_notional:,.0f} ({coverage_pct:.0f}% of portfolio)")
        print(f"         Check contract sizing vs. portfolio.")

    # ── ROLL ALERTS ──────────────────────────────────────────────────────────
    print(f"\n{LINE}")
    print("  ROLL ALERTS")
    print(DASH)
    for status, name, msg in alerts:
        prefix = _alert_prefix(status)
        line = f"  {prefix}{name:<18}: {msg}"
        if len(line) <= W + 4:
            print(line)
        else:
            print(f"  {prefix}{name:<18}:")
            words = msg.split()
            cur = "    " + " " * 26
            for w in words:
                if len(cur) + len(w) + 1 > W:
                    print(cur)
                    cur = "    " + " " * 26 + w
                else:
                    cur += " " + w
            if cur.strip():
                print(cur)

    print(f"\n{LINE}")
    print("  DISCLAIMER: All strikes and premiums are ESTIMATES based on")
    print("  Black-Scholes theoretical mid-market values. Actual execution")
    print("  prices will differ. Use limit orders. Verify with live quotes.")
    print("  This is not financial advice.")
    print(f"{LINE}\n")
