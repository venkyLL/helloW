"""Streamlit dashboard for the Vanna-Neutral 3-1-1 Hedge Calculator."""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from datetime import date
import streamlit as st
import pandas as pd
import plotly.graph_objects as go

from hedge.legs import build_put_spine, build_call_engine, classify_vix, VIX_ZONE_NAMES
from hedge.output import check_roll_alerts, _fmt_dollar
from hedge.greeks import compute_portfolio_greeks, compute_vanna_neutral_adjustment
from hedge.scenario import compute_scenario_pnl, compute_1year_table

st.set_page_config(
    page_title="3-1-1 Hedge Calculator",
    page_icon="📊",
    layout="wide",
)

# ── Sidebar inputs ─────────────────────────────────────────────────────────────
st.sidebar.title("Inputs")

spot = st.sidebar.number_input("XSP Spot", value=741.28, step=1.0, format="%.2f")
vix  = st.sidebar.number_input("VIX", value=18.5, step=0.5, format="%.1f")

st.sidebar.markdown("---")
iv_override = st.sidebar.number_input(
    "IV % (blank = VIX × 1.1)", value=0.0, step=0.5, format="%.1f",
    help="6-month ATM implied vol. Leave 0 to use VIX × 1.1."
)
rate     = st.sidebar.number_input("Risk-Free Rate %", value=4.5, step=0.25, format="%.2f")
notional = st.sidebar.number_input("Notional ($)", value=1_000_000, step=100_000)

st.sidebar.markdown("---")
auto_vanna = st.sidebar.toggle("Auto Vanna-Neutral", value=False,
                                help="Adjusts Short Subsidy qty to neutralize portfolio vanna")

st.sidebar.markdown("---")
st.sidebar.markdown("**Roll Alerts**")
lrd_str = st.sidebar.text_input("Last Roll Date (YYYY-MM-DD)", value="")
lrs     = st.sidebar.number_input("Last Roll XSP Spot", value=0.0, step=1.0, format="%.2f")

st.sidebar.markdown("---")
st.sidebar.markdown("**Scenario**")
scenario_spot = st.sidebar.number_input(
    "Scenario XSP Spot (0 = off)", value=0.0, step=5.0, format="%.2f",
    help="Re-prices all legs at this hypothetical spot."
)

# ── Derived inputs ─────────────────────────────────────────────────────────────
calc_date  = date.today()
iv         = (iv_override / 100.0) if iv_override > 0 else (vix * 1.1) / 100.0
rate_dec   = rate / 100.0
last_roll_date  = date.fromisoformat(lrd_str) if lrd_str.strip() else None
last_roll_spot  = lrs if lrs > 0 else None

# ── Build positions ────────────────────────────────────────────────────────────
put_legs, zone, mult = build_put_spine(spot, vix, iv, rate_dec, notional, calc_date)
call_legs            = build_call_engine(spot, vix, iv, rate_dec, notional, calc_date)

# ── Vanna solve ────────────────────────────────────────────────────────────────
leg_greeks  = compute_portfolio_greeks(put_legs, call_legs, spot, iv, rate_dec, calc_date)
adjustment  = compute_vanna_neutral_adjustment(leg_greeks, put_legs, spot, iv, rate_dec, calc_date)

if auto_vanna and adjustment:
    put_legs, zone, mult = build_put_spine(
        spot, vix, iv, rate_dec, notional, calc_date,
        subsidy_qty=adjustment["recommended_qty"],
    )
    leg_greeks = compute_portfolio_greeks(put_legs, call_legs, spot, iv, rate_dec, calc_date)
    adjustment = None

alerts = check_roll_alerts(calc_date, spot, vix, last_roll_date, last_roll_spot)

net_put  = sum(l.est_total for l in put_legs)
net_call = sum(l.est_total for l in call_legs)

# ══════════════════════════════════════════════════════════════════════════════
#  HEADER
# ══════════════════════════════════════════════════════════════════════════════
st.title("Vanna-Neutral 3-1-1 Hedge Calculator")

ann_net = net_put * 2 + net_call * 12
carry_label = "Annual Carry" if ann_net >= 0 else "Annual Cost"

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("XSP Spot",   f"{spot:.2f}")
col2.metric("VIX",        f"{vix:.1f}",  delta=VIX_ZONE_NAMES[zone])
col3.metric("IV (6M)",    f"{iv*100:.1f}%")
col4.metric("VIX Zone",   f"Zone {zone}")
col5.metric(carry_label,  _fmt_dollar(ann_net))

st.caption(f"Date: {calc_date}  ·  Notional: ${notional:,.0f}  ·  Rate: {rate:.2f}%"
           + ("  ·  ✓ Vanna-Neutral applied" if auto_vanna else ""))

# ══════════════════════════════════════════════════════════════════════════════
#  POSITIONS
# ══════════════════════════════════════════════════════════════════════════════
st.markdown("---")
tab_pos, tab_greeks, tab_1yr, tab_scenario, tab_alerts = st.tabs(
    ["📋 Positions", "🔬 Greeks", "📅 1-Year Scenarios", "🎯 Scenario P&L", "🔔 Roll Alerts"]
)

with tab_pos:
    col_put, col_call = st.columns(2)

    with col_put:
        st.subheader("Put Spine")
        put_data = [{
            "Leg":    l.name.strip(),
            "Act":    l.action,
            "Qty":    l.quantity,
            "δ":      f"{l.target_delta:+.3f}",
            "Strike": int(l.computed_strike),
            "Expiry": l.expiry_date.strftime("%b %Y"),
            "Prem":   f"${l.est_premium:.2f}",
            "Total":  _fmt_dollar(l.est_total),
        } for l in put_legs]
        put_df = pd.DataFrame(put_data)
        st.dataframe(put_df, hide_index=True, use_container_width=True)
        st.metric("Net Put Spine Cost", _fmt_dollar(net_put),
                  delta=f"Ann. ×2: {_fmt_dollar(net_put * 2)}")

    with col_call:
        st.subheader("Call Engine")
        call_data = [{
            "Leg":    l.name.strip(),
            "Act":    l.action,
            "Qty":    l.quantity,
            "δ":      f"{l.target_delta:+.4f}",
            "Strike": int(l.computed_strike),
            "Expiry": l.expiry_date.strftime("%b %Y"),
            "Prem":   f"${l.est_premium:.2f}",
            "Total":  _fmt_dollar(l.est_total),
        } for l in call_legs]
        call_df = pd.DataFrame(call_data)
        st.dataframe(call_df, hide_index=True, use_container_width=True)
        st.metric("Net Call Credit", _fmt_dollar(net_call),
                  delta=f"Ann. ×12: {_fmt_dollar(net_call * 12)}")

# ══════════════════════════════════════════════════════════════════════════════
#  GREEKS
# ══════════════════════════════════════════════════════════════════════════════
with tab_greeks:
    total_delta = sum(g.delta for g in leg_greeks)
    total_gamma = sum(g.gamma for g in leg_greeks)
    total_vanna = sum(g.vanna for g in leg_greeks)
    total_theta = sum(g.theta for g in leg_greeks)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Net Delta",  f"{total_delta:+.1f}")
    c2.metric("Net Gamma",  f"{total_gamma:+.4f}")
    c3.metric("Net Vanna",  f"{total_vanna:+.1f}",
              delta="✓ neutral" if abs(total_vanna) < 50 else "✗ off")
    c4.metric("Net Theta/day", f"{total_theta:+.2f}",
              delta=f"≈ ${total_theta * 30:+.0f}/mo")

    greek_data = [{
        "Leg":    g.name.strip(),
        "Act":    g.action,
        "Delta":  f"{g.delta:+.1f}",
        "Gamma":  f"{g.gamma:+.4f}",
        "Vanna":  f"{g.vanna:+.1f}",
        "Θ/day":  f"{g.theta:+.2f}",
    } for g in leg_greeks]
    st.dataframe(pd.DataFrame(greek_data), hide_index=True, use_container_width=True)

    if adjustment:
        adj = adjustment
        st.warning(
            f"**Vanna Rebalance Suggestion** — "
            f"Short Subsidy: {adj['current_qty']} → {adj['recommended_qty']} contracts "
            f"({'▲ add' if adj['delta_qty'] > 0 else '▼ reduce'} {abs(adj['delta_qty'])}). "
            f"Projected vanna: {adj['projected_vanna']:+.1f}. "
            f"Enable **Auto Vanna-Neutral** in the sidebar to apply."
        )

    # Vanna waterfall chart
    st.subheader("Vanna by Leg")
    vanna_fig = go.Figure(go.Bar(
        x=[g.name.strip() for g in leg_greeks],
        y=[g.vanna for g in leg_greeks],
        marker_color=["#ef4444" if g.vanna < 0 else "#22c55e" for g in leg_greeks],
        text=[f"{g.vanna:+.0f}" for g in leg_greeks],
        textposition="outside",
    ))
    vanna_fig.add_hline(y=0, line_dash="dash", line_color="white", opacity=0.4)
    vanna_fig.update_layout(
        xaxis_tickangle=-30,
        yaxis_title="Vanna (position-scaled)",
        height=350,
        margin=dict(t=20, b=10),
    )
    st.plotly_chart(vanna_fig, use_container_width=True)

# ══════════════════════════════════════════════════════════════════════════════
#  1-YEAR SCENARIO TABLE + CHART
# ══════════════════════════════════════════════════════════════════════════════
with tab_1yr:
    rows = compute_1year_table(
        spot=spot, put_legs=put_legs, call_legs=call_legs,
        notional=notional, net_put_per_roll=net_put, net_call_per_month=net_call,
    )

    moves    = [f"{r['pct']:+d}%" for r in rows]
    port_pnl = [r["portfolio"]  for r in rows]
    hedge    = [r["hedge_pnl"]  for r in rows]
    net      = [r["net_pnl"]    for r in rows]

    fig = go.Figure()
    fig.add_trace(go.Bar(name="Portfolio", x=moves, y=port_pnl,
                         marker_color="#60a5fa", opacity=0.7))
    fig.add_trace(go.Bar(name="Hedge P&L", x=moves, y=hedge,
                         marker_color="#a78bfa", opacity=0.7))
    fig.add_trace(go.Scatter(name="Net P&L", x=moves, y=net,
                             mode="lines+markers", line=dict(color="#f59e0b", width=2),
                             marker=dict(size=8)))
    fig.add_hline(y=0, line_dash="dash", line_color="white", opacity=0.4)
    fig.update_layout(
        barmode="group",
        yaxis_title="P&L ($)",
        yaxis_tickformat="$,.0f",
        height=400,
        legend=dict(orientation="h", y=1.05),
        margin=dict(t=30, b=10),
    )
    st.plotly_chart(fig, use_container_width=True)

    table_data = [{
        "Move":      f"{r['pct']:+d}%",
        "XSP":       f"{r['spot_new']:.0f}",
        "Portfolio": _fmt_dollar(r["portfolio"]),
        "Put P&L":   _fmt_dollar(r["put_pnl"]),
        "Call P&L":  _fmt_dollar(r["call_pnl"]),
        "Hedge P&L": _fmt_dollar(r["hedge_pnl"]),
        "Net P&L":   _fmt_dollar(r["net_pnl"]),
        "Net %":     f"{r['net_pnl'] / notional * 100:+.1f}%",
    } for r in rows]
    st.dataframe(pd.DataFrame(table_data), hide_index=True, use_container_width=True)
    st.caption("Assumes put spine rolled 2× and call engine rolled 12× at entry premiums; payoffs at intrinsic.")

# ══════════════════════════════════════════════════════════════════════════════
#  SCENARIO P&L
# ══════════════════════════════════════════════════════════════════════════════
with tab_scenario:
    if scenario_spot > 0:
        leg_results, hedge_pnl, portfolio_pnl, net_pnl = compute_scenario_pnl(
            spot_orig=spot, spot_scenario=scenario_spot,
            iv=iv, rate=rate_dec, calc_date=calc_date,
            put_legs=put_legs, call_legs=call_legs, notional=notional,
        )
        pct_move = (scenario_spot - spot) / spot * 100

        s1, s2, s3 = st.columns(3)
        s1.metric("Portfolio P&L", _fmt_dollar(portfolio_pnl),
                  delta=f"{pct_move:+.1f}% move")
        s2.metric("Hedge P&L", _fmt_dollar(hedge_pnl))
        s3.metric("Net P&L", _fmt_dollar(net_pnl),
                  delta=f"{net_pnl / notional * 100:+.1f}% of notional")

        scen_data = [{
            "Leg":         r.name.strip(),
            "Act":         r.action,
            "Qty":         r.quantity,
            "Strike":      int(r.strike),
            "Entry $":     f"${r.entry_price:.2f}",
            "Scenario $":  f"${r.scenario_price:.2f}",
            "P&L":         _fmt_dollar(r.pnl),
        } for r in leg_results]
        st.dataframe(pd.DataFrame(scen_data), hide_index=True, use_container_width=True)

        # P&L bar chart per leg
        leg_names = [r.name.strip() for r in leg_results]
        leg_pnls  = [r.pnl for r in leg_results]
        bar_fig = go.Figure(go.Bar(
            x=leg_names, y=leg_pnls,
            marker_color=["#22c55e" if p >= 0 else "#ef4444" for p in leg_pnls],
            text=[_fmt_dollar(p) for p in leg_pnls],
            textposition="outside",
        ))
        bar_fig.add_hline(y=0, line_dash="dash", line_color="white", opacity=0.4)
        bar_fig.update_layout(
            xaxis_tickangle=-30,
            yaxis_title="P&L ($)",
            yaxis_tickformat="$,.0f",
            height=350,
            margin=dict(t=20, b=10),
        )
        st.plotly_chart(bar_fig, use_container_width=True)
    else:
        st.info("Enter a Scenario XSP Spot in the sidebar to see per-leg P&L at that price.")

# ══════════════════════════════════════════════════════════════════════════════
#  ROLL ALERTS
# ══════════════════════════════════════════════════════════════════════════════
with tab_alerts:
    for status, name, msg in alerts:
        if status == "ALERT":
            st.error(f"**{name}** — {msg}")
        elif status == "WARN":
            st.warning(f"**{name}** — {msg}")
        elif status == "OK":
            st.success(f"**{name}** — {msg}")
        else:
            st.info(f"**{name}** — {msg}")

    st.caption(
        "Disclaimer: All strikes and premiums are estimates based on Black-Scholes "
        "theoretical mid-market values. This is not financial advice."
    )
