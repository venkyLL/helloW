# Vanna-Neutral 3–1–1 Hedge Calculator
## Product Requirements Document v1.0

**Product:** `RJ.py` — on-demand XSP options sizing engine  
**Portfolio:** $1,000,000 S&P 500  
**Underlying:** XSP (Mini S&P 500 Index, European cash-settled, Section 1256)  
**Status:** v3 built — scenario P&L and 1-year cost table added.

---

## 1. What It Does

Given current market inputs, output a complete trade-ready position sheet:
- Exact contract quantities for each leg
- Target delta bands
- Computed strike levels
- Estimated premiums
- Net cost for the full structure

---

## 2. Strategy Architecture

### 2.1 Put Spine — 6-Month Tenor, Rolled Every 3 Months

5-leg structure. Baseline quantities for $1M portfolio at VIX Zone 2 (normal).

| Leg | Action | Target δ | Base Qty | Approx Strike (XSP≈741) | Role |
|-----|--------|----------|----------|--------------------------|------|
| Long Anchor  (560P equiv) | BUY  | −0.35δ        | 20 | ~630 | Early Trigger / 30% Crash Wall |
| Long Bridge  (520P equiv) | BUY  | −0.375δ       | 20 | ~610 | Apex Plateau / Convexity Gap-fill |
| Short Subsidy(440P equiv) | SELL | −0.22δ        | 34 | ~575 | Cost Offset / Anti-Gamma Subsidy |
| Long Floor   (400P equiv) | BUY  | −0.10δ        | 14 | ~530 | Lower Wing / Tail Security |
| Short Tail   (330P equiv) | SELL | −0.05δ        | 10 | ~480 | Tail Decay / Final Credit |

**Payoff shape:**
- Hedge activates at −15% portfolio drawdown
- Peak trim at −30%
- Loss capped ~−20% at −50% market crash

### 2.2 Call Funding Engine — 2–3 Month Tenor, Rolled Monthly

3-tier call spread. Baseline for VIX Zone 2 (normal):

| Tier | Role | Short δ | Long δ | Units | Width |
|------|------|---------|--------|-------|-------|
| Tier 1 | High-Yield Generator | 4.5% | 1.5% | 30 | 30 pts |
| Tier 2 | Stability Layer      | 3.0% | 1.0% | 40 | 30 pts |
| Tier 3 | Tail Anchor          | 2.5% | 0.8% | 30 | 30 pts |

Target net credit: ~$5,000–$9,000/month depending on VIX regime.

---

## 3. VIX Zone Sizing

Applied at every quarterly roll. Emergency override if VIX jumps >8pts in <48hrs.

| Zone | VIX Range | Size Multiplier | Put Wing Tenor | Call Units Scale |
|------|-----------|-----------------|----------------|------------------|
| 1 | < 15  | 120% | 9–12 months | ×1.2 |
| 2 | 15–22 | 100% | 6 months    | ×1.0 (baseline) |
| 3 | 22–28 | 80%  | 6 months    | ×0.8 |
| 4 | 28–35 | 60%  | 3–4 months  | ×0.6 |
| 5 | > 35  | 40%  | 3 months    | ×0.4 |

**Zone 1 special rule:** Extend long wings (anchor, bridge, floor) to 9–12M tenor. Keep all short legs and call spreads at standard tenor.

### VIX-Adjusted Call Delta Bands

| Zone | VIX | T1 Short/Long | T2 Short/Long | T3 Short/Long | T1 Units | T2 Units | T3 Units |
|------|-----|---------------|---------------|---------------|----------|----------|----------|
| 1 | <15  | 5.0%/2.0% | 3.5%/1.5% | 3.0%/1.0% | 30 | 40 | 30 |
| 2 | 15–25| 4.5%/1.5% | 3.0%/1.0% | 2.5%/0.8% | 30 | 40 | 30 |
| 3 | 25–35| 4.0%/1.2% | 2.5%/0.8% | 2.0%/0.5% | 20 | 40 | 40 |
| 4 | >35  | 3.0%/1.0% | 2.0%/0.6% | 1.5%/0.4% | 10 | 30 | 60 |

---

## 4. Inputs

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `--spot` | float | required* | XSP spot price (e.g. 741.28). Not needed with `--live`. |
| `--vix` | float | required* | CBOE VIX level (e.g. 18.5). Not needed with `--live`. |
| `--iv` | float % | VIX × 1.1 | 6M ATM implied vol. If omitted, falls back to VIX×1.1/100 |
| `--rate` | float % | 4.5 | Risk-free rate |
| `--notional` | float | 1,000,000 | Portfolio size in USD. All quantities scale linearly. |
| `--date` | YYYY-MM-DD | today | Calculation date |
| `--last-roll-date` | YYYY-MM-DD | None | Enables roll alerts |
| `--last-roll-spot` | float | None | XSP spot at last roll. Enables rally/drop alerts |
| `--live` | flag | False | Auto-fetch XSP spot + VIX from Yahoo Finance via yfinance |
| `--ibkr` | flag | False | Connect to IBKR TWS/Gateway for live option chain + real delta matching |
| `--ibkr-host` | string | 127.0.0.1 | TWS/Gateway host |
| `--ibkr-port` | int | 7497 | TWS live: 7497, TWS paper: 7496, IB Gateway live: 4001, paper: 4002 |
| `--ibkr-client-id` | int | 10 | IBKR API client ID |
| `--scenario` | float | None | Hypothetical XSP spot — prints per-leg hedge P&L vs. portfolio loss at that level |

\* `--spot` and `--vix` are not required when `--live` is used. Omitting all three triggers interactive mode.

---

## 5. Core Business Logic

### 5.1 Quantity Scaling
```
scaled_qty = round( base_qty × (notional / 1_000_000) × vix_multiplier )
```
Always round to nearest integer. No fractional contracts.

### 5.2 Strike Calculation
Use Black-Scholes delta inversion (bisection or Brent's method):
- For puts: solve `N(d1) − 1 = target_delta` for K
- For calls: solve `N(d1) = target_delta` for K
- Round all strikes to nearest 5-point increment
- Search bounds for puts: `K ∈ [spot×0.20, spot×1.05]`
- Search bounds for calls: `K ∈ [spot×0.95, spot×1.80]`

### 5.3 Expiry Date Logic
- XSP expires on the **3rd Friday of each month**
- Put spine: nearest monthly expiry ~180 days out (or 270d for Zone 1 wings)
- Call engine: nearest monthly expiry ~60–75 days out
- **Avoid October expiries** for put spine quarterly rolls

### 5.4 Roll Trigger Alerts
Evaluate when `--last-roll-date` and `--last-roll-spot` are provided:

| Trigger | Condition | Action |
|---------|-----------|--------|
| Scheduled Roll | days since roll ≥ 90 | Full rebuild at new delta bands |
| Fast Drop | Spot down >10% in <10 days | Roll short subsidy DOWN to restore −0.22δ. Do NOT touch anchor. |
| Rally | Spot up >10% from last roll | Roll anchor UP to restore −15% deductible. Roll call spread up. |
| VIX Shock | VIX jumped >8pts in <48hrs | Apply VIX-regime reduction immediately |
| Delta Band Breach | Short subsidy δ outside −0.20 to −0.25 | Roll short subsidy immediately |

---

## 6. Output Format

```
========================================================================
  VANNA-NEUTRAL 3-1-1 HEDGE CALCULATOR
========================================================================
  Date     : YYYY-MM-DD    Notional : $1,000,000
  XSP Spot : 741.28        VIX      : 18.5
  IV (6M)  : 19.2%         Rfr      : 4.50%
  VIX ZONE : 2 — Normal (15–22)  (sizing: 100%)
------------------------------------------------------------------------

  PUT SPINE — [Expiry date] ([DTE]d)
------------------------------------------------------------------------
  Leg                   Act  Qty  Target δ  Strike  Expiry   Prem/sh   Total
  Long Anchor (560P)    BUY   20   −0.350     [K]   Dec 2026   [$x]   −[$x]
  Long Bridge (520P)    BUY   20   −0.375     [K]   Dec 2026   [$x]   −[$x]
  Short Subsidy (440P) SELL   34   −0.220     [K]   Dec 2026   [$x]   +[$x]
  Long Floor (400P)     BUY   14   −0.100     [K]   Dec 2026   [$x]   −[$x]
  Short Tail (330P)    SELL   10   −0.050     [K]   Dec 2026   [$x]   +[$x]
                                                          NET: −[$xx,xxx]

  CALL ENGINE — [Expiry date] ([DTE]d)
------------------------------------------------------------------------
  [3 tiers × short + long leg, net credit]

========================================================================
  COST SUMMARY
  Net put spine cost (6M cycle)    : −$xx,xxx
  Net call engine credit (monthly) : +$x,xxx
  Annualized put cost (×2 rolls)   : −$xxx,xxx
  Annualized call income (×12)     : +$xx,xxx
  Net annual cost/carry            : ±$x,xxx  (±x.xx% of notional)
  Total open contracts             : 298 (98 puts + 200 calls)
========================================================================
  ROLL ALERTS
  [OK]    Scheduled Roll   : Next roll due YYYY-MM-DD (N days)
  [ALERT] Fast Drop Rule   : ...
========================================================================
  DISCLAIMER: Estimates only. Use limit orders. Not financial advice.
========================================================================
```

---

## 7. Module Structure

```
hedge_calc.py            # current: monolithic script, BS pricing only
├── input_handler        # parse CLI args / interactive prompts / defaults
├── vix_classifier       # zone 1–5, multiplier, tenor, call bands
├── bs_pricer            # BS price, delta, delta→strike inversion
├── spine_builder        # 5-leg put spine
├── call_engine_builder  # 3-tier call engine
├── cost_summarizer      # net cost, annualized, % notional
├── roll_alert_engine    # roll trigger evaluation
└── output_formatter     # CLI table, JSON, CSV output modes
```

---

## 8. v2 Feature: Live Data ✅ BUILT

### 8.1 Data Sources

**Yahoo Finance (`yfinance`)** — free, no auth, 15-min delayed. Activated with `--live`:
- XSP spot: tries `yf.Ticker("XSP").fast_info["lastPrice"]` directly; falls back to `^GSPC / 10` (XSP is not always listed on Yahoo)
- VIX: `yf.Ticker("^VIX").fast_info["lastPrice"]` with 5-day history fallback
- Data source label shown in position sheet header

**IBKR TWS / IB Gateway (`ib_insync`)** — real-time, requires TWS or IB Gateway running locally. Activated with `--ibkr`:
- Connects via `IB.connect(host, port, clientId)`
- Requests contract details for XSP options at each target expiry
- Requests model greeks (delta) per strike via `reqTickers()`
- Picks the strike with `min |live_delta − target_delta|`
- Returns `IBKRLeg(expiry_str, right, strike, delta, mid_price)` per expiry+right combo

### 8.2 Live Data Flow (as built)

```
--live flag:
  1. fetch_live_yfinance() → (xsp_spot, vix_level)
  2. Continue to BS-based spine/call building with live inputs

--ibkr flag (after --live or manual spot/vix):
  1. Compute put expiry + call expiry dates from VIX zone logic
  2. fetch_ibkr_chain(expiry_dates, rights, target_deltas, host, port)
     a. Connect to TWS/Gateway
     b. For each (expiry, right): reqContractDetails → enumerate strikes
     c. reqTickers on all strikes → wait 2s → refresh
     d. For each ticker: read modelGreeks.delta, compute mid=(bid+ask)/2
     e. Pick strike with min |live_delta − target_delta|
     f. Return dict keyed (expiry_str_YYYYMMDD, right) → IBKRLeg
  3. build_put_spine(..., ibkr_chain=...) — uses live strike+mid if match within 0.15δ
  4. build_call_engine(..., ibkr_chain=...) — uses live strike+mid if match within 0.10δ
  5. Falls back to BS inversion if no IBKR match or IBKR unavailable

Per-leg price source tag: role field shows [live] or [BS]
```

### 8.3 Delta Matching (as built)

IBKR path — uses real model greeks from TWS:
```python
# In fetch_ibkr_chain():
greeks = ticker.modelGreeks or ticker.lastGreeks
live_delta = greeks.delta
diff = abs(live_delta - target_delta)
# pick min diff across all strikes
```

### 8.4 Dependencies for v2

```
pip install yfinance      # --live flag
pip install ib_insync     # --ibkr flag
pip install scipy tabulate  # core (unchanged)
```

All imports are guarded — script runs without any optional package installed.

---

## 9. Scenario P&L Analysis (`--scenario`) ✅ BUILT

### 9.1 Purpose
Re-prices all legs at a hypothetical XSP spot using Black-Scholes (same expiry dates, same IV, same calc date — only spot changes). Shows what the hedge is worth right now if the market moves to that level.

### 9.2 Usage
```
python3 RJ.py --spot 743.15 --vix 17.68 --scenario 594
python3 RJ.py --live --scenario 594
```

### 9.3 Output Format
```
========================================================================
  SCENARIO ANALYSIS  —  XSP 743.15 → 594.00  (-20.1%)
========================================================================
Leg                    Act   Qty  Strike  Entry $  Scen $       P&L
Long Anchor (560P)     BUY    20     730   $27.31  $123.31  +$191,990
...
TOTAL HEDGE P&L                                              +$173,909
------------------------------------------------------------------------
  Portfolio loss (-20.1% × $1,000,000)  : -$200,700
  Hedge P&L                             : +$173,909
  ──────────────────────────────────────────────────
  NET LOSS                              :  -$26,791  (-2.7% of notional)
========================================================================
```

### 9.4 Logic
- Each leg repriced via `bs_put_price` or `bs_call_price` at `spot_scenario`
- BUY legs: P&L = `(new_price − entry_price) × qty × 100`
- SELL legs: P&L = `(entry_price − new_price) × qty × 100`
- Portfolio P&L = `notional × (spot_scenario − spot_orig) / spot_orig`

---

## 10. 1-Year Cost Table ✅ BUILT

### 10.1 Purpose
Shows the total 1-year net outcome across market scenarios from −25% to +25% in 5% increments. Printed automatically on every run — no flag required.

### 10.2 Assumptions
- Put spine rolled **twice** (two 6M rolls at the same entry premiums)
- Call engine rolled **12 times** (monthly at same entry premiums)
- Payoffs at year-end based on **intrinsic value** at the scenario spot (T=0)
- IV and strike levels held constant across rolls

### 10.3 Output Format
```
========================================================================
  1-YEAR SCENARIO COST TABLE  (flat premiums, 2 put rolls + 12 call rolls)
========================================================================
Move    XSP    Portfolio    Put P&L    Call P&L  Hedge P&L    NET P&L   Net %
-25%    557    -$250,000  +$172,812   +$65,865  +$238,677   -$11,323   -1.1%
-20%    595    -$200,000  +$135,655   +$65,865  +$201,519    +$1,519   +0.2%
-15%    632    -$150,000   +$76,820   +$65,865  +$142,684    -$7,316   -0.7%
-10%    669    -$100,000   +$42,874   +$65,865  +$108,738    +$8,738   +0.9%
 -5%    706     -$50,000   -$33,795   +$65,865   +$32,069   -$17,931   -1.8%
 +0% ◀  743          +$0  -$139,825   +$65,865   -$73,961   -$73,961   -7.4%
 +5%    780     +$50,000  -$139,825   +$65,865   -$73,961   -$23,961   -2.4%
+10%    817    +$100,000  -$139,825   +$65,865   -$73,961   +$26,039   +2.6%
+15%    855    +$150,000  -$139,825   +$65,865   -$73,961   +$76,039   +7.6%
+20%    892    +$200,000  -$139,825   -$51,935  -$191,761    +$8,239   +0.8%
+25%    929    +$250,000  -$139,825  -$269,135  -$408,961  -$158,961  -15.9%
```

### 10.4 Key Observations (at XSP=743, VIX=17.68)
| Zone | Net outcome | Notes |
|---|---|---|
| −25% | −1.1% | Near full protection |
| −20% | +0.2% | Hedge fully covers portfolio loss |
| −15% to −10% | −0.7% to +0.9% | Partial protection band |
| −5% to 0% | −1.8% to −7.4% | Insurance cost zone |
| +5% to +15% | −2.4% to +7.6% | Portfolio gains dominate |
| +20% | +0.8% | Call engine just absorbed; rally rule should trigger |
| +25% | −15.9% | **Danger zone** — short calls deep ITM; roll up required |

### 10.5 Logic
```python
put_pnl_1y  = (net_put_per_roll × 2) + intrinsic_put_payoff(spot_new)
call_pnl_1y = (net_call_per_month × 12) + intrinsic_call_payoff(spot_new)
hedge_pnl   = put_pnl_1y + call_pnl_1y
net_pnl     = portfolio_pnl + hedge_pnl
```

---

## 11. Acceptance Criteria (updated)

| # | Test | Input | Expected |
|---|------|-------|----------|
| 1 | Baseline sizing | spot=741, vix=18, iv=19.2% | Spine: 20/20/34/14/10. Net debit $25k–$40k range. |
| 2 | Zone 1 scaling | vix=12 | Spine: 24/24/41/17/12. Wing tenor = 9–12M. |
| 3 | Zone 5 scaling | vix=40 | Spine: 8/8/14/6/4. All tenors = 3M. |
| 4 | Delta inversion | spot=741, iv=20%, T=0.493, δ=−0.35 | Strike within ±10pts of analytic BS solution. |
| 5 | Notional scaling | notional=$2M, vix=18 | All qtys ×2 baseline (40/40/68/28/20). |
| 6 | Call delta adjustment | vix=30 | T1 short δ=4.0%, T2=2.5%, reduced units. |
| 7 | Fast drop alert | last_spot=741, spot=650, days=8 | Fast Drop [ALERT] fires. Roll short subsidy. |
| 8 | Cost summary | any valid inputs | Shows per-cycle, annualized, and % of notional. |
| 9 | Live fetch (v2) | `--live` flag | XSP and VIX auto-populated from yfinance (^GSPC/10 fallback). |
| 10 | IBKR chain matching (v2) | `--ibkr` flag | Strikes from real market deltas; premiums from live bid/ask mids. |
| 11 | Scenario analysis (v3) | `--scenario 594` | Per-leg P&L, hedge total, portfolio loss, net shown at that spot. |
| 12 | 1-year cost table (v3) | automatic | Table from −25% to +25% in 5% steps; flat at 0% = annual insurance cost. |

---

## 10. Roll Reference

### Quarterly Roll Calendar (starting June 2026)

| Roll | Close | Open | Notes |
|------|-------|------|-------|
| Initial | — | Nov 2026 | First 6M puts (skip Oct) |
| Q3 2026 | Nov 2026 | Feb 2027 | 3M roll |
| Q4 2026 | Feb 2027 | May 2027 | 3M roll |
| Q1 2027 | May 2027 | Nov 2027 | 6M roll (skip Oct) |
| Pattern | — | — | 6M → 3M → 3M → 6M → … |

### Execution Rules (Non-Negotiable)
- Limit orders only. Never market orders.
- Execute **short legs first** (collect premium before paying for longs).
- For calls: work legs separately, not as a 4-leg combo.
- SMART routing. No midpoint pegging, no autoadjust.
- Slippage target: <$0.02/leg Tier 1–2 calls; <$0.01/leg Tier 3.
- Avoid rolling during: FOMC week, CPI week, quad witching.

### Do Nothing Scenarios
- SPY flat (±3%)
- Slow bleed −5% to −12% over months (worst env for any hedge — let it ride)
- SPY drops −20% to −50% during crash (convexity is working — do not touch)
- SPY drops >−50% (tail is capped — no action)

### Scenario Decision Table

| SPY Move | Speed | Action |
|----------|-------|--------|
| +10% to +20% | Any | Roll anchor UP. Roll short subsidy up. Roll call spread up 5–10% OTM. |
| −10% to −20% | Fast (<10d) | Roll short subsidy DOWN to restore −0.20δ. Do NOT touch anchor. |
| −5% to −12% | Slow (months) | Do nothing. Roll on schedule only. |
| −20% to −50% | Any | Do nothing. Let convexity work. |
| >−50% | Any | Do nothing. Tail is capped. |
| VIX > 30 | — | Reduce size 20–30%. Re-enter when vol normalizes. |

---

## 11. Key Formulas

### Black-Scholes Put Price
```
P  = K·e^(−rT)·N(−d2) − S·N(−d1)
d1 = [ln(S/K) + (r + 0.5σ²)·T] / (σ√T)
d2 = d1 − σ√T
Put Delta = N(d1) − 1
```

### Delta Inversion (Bisection)
```
Given target_delta, find K such that:
  f(K) = bs_put_delta(S, K, T, r, σ) − target_delta = 0

Bounds: K_low = S×0.20,  K_high = S×1.05
Tolerance: 0.0001 delta units
Round final K to nearest 5-point increment
```

### Quantity Scaling
```
scaled_qty = round( base_qty × (notional / 1_000_000) × vix_multiplier )
```

### OTM Percentage
```
OTM% = (S − K) / S × 100    [puts: S > K means OTM]
```

---

## 12. Current File

`RJ.py` — monolithic Python script, ~860 lines. Committed on `main` (`2c4685e`).

**Current state (v3):**
- ✅ CLI with argparse + interactive mode
- ✅ VIX zone classification (5 zones)
- ✅ Black-Scholes delta inversion for put strikes
- ✅ Black-Scholes delta inversion for call strikes
- ✅ VIX-adjusted call delta bands
- ✅ VIX-adjusted put quantities
- ✅ Zone 1 wing tenor extension (9M)
- ✅ 3rd Friday expiry logic, avoids October
- ✅ Roll alerts (scheduled, fast drop, rally, VIX shock)
- ✅ Formatted CLI output with tabulate
- ✅ Cost summary (per-cycle, annualized, % notional)
- ✅ `--live` flag: XSP spot + VIX from yfinance (`^GSPC/10` fallback)
- ✅ `--ibkr` flag: live option chain + real delta matching via `ib_insync`
- ✅ Per-leg price source tag `[live]` / `[BS]` in output
- ✅ Data source line in position sheet header
- ✅ `--scenario SPOT`: per-leg BS reprice at hypothetical spot, net P&L vs. portfolio
- ✅ 1-year cost table: −25% to +25% in 5% steps, printed automatically every run

**Not yet built (v4+):**
- ❌ JSON / CSV output modes for broker import
- ❌ Module split (currently monolithic)
- ❌ Portfolio-level Greeks aggregation (total delta, gamma, vanna, theta)
- ❌ IV surface / skew-adjusted strikes from IBKR
- ❌ Position diff / rebalance mode (compare vs. existing holdings)
