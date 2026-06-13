"""Live market data: yfinance and IBKR TWS / IB Gateway."""

import time
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

from .compat import HAS_YFINANCE, HAS_IBKR, yf, IB, IBOption, ib_util


def _yf_last_price(ticker_sym: str) -> Optional[float]:
    t = yf.Ticker(ticker_sym)
    try:
        p = t.fast_info.get("lastPrice") or t.fast_info.get("regularMarketPrice")
        if p:
            return float(p)
    except Exception:
        pass
    hist = t.history(period="5d")
    if not hist.empty:
        return float(hist["Close"].iloc[-1])
    return None


def fetch_live_yfinance() -> Tuple[float, float]:
    """
    Fetch latest XSP spot and VIX from Yahoo Finance.
    Returns (xsp_spot, vix_level). Raises RuntimeError on failure.
    """
    if not HAS_YFINANCE:
        raise RuntimeError("yfinance not installed. Run: pip install yfinance")

    print("  [yfinance] Fetching XSP spot and VIX ...", flush=True)

    vix_level = _yf_last_price("^VIX")
    if not vix_level:
        raise RuntimeError("yfinance returned no data for ^VIX")

    xsp_spot = _yf_last_price("XSP")
    if not xsp_spot:
        spx = _yf_last_price("^GSPC")
        if not spx:
            raise RuntimeError("yfinance returned no data for XSP or ^GSPC")
        xsp_spot = spx / 10.0
        print("  [yfinance] XSP not available directly — using ^GSPC/10", flush=True)

    print(f"  [yfinance] XSP={xsp_spot:.2f}  VIX={vix_level:.2f}", flush=True)
    return float(xsp_spot), float(vix_level)


@dataclass
class IBKRLeg:
    expiry_str: str   # YYYYMMDD
    right: str        # 'P' or 'C'
    strike: float
    delta: float
    mid_price: float  # (bid+ask)/2, 0.0 if market closed


def _ibkr_expiry_str(expiry_date: date) -> str:
    return expiry_date.strftime("%Y%m%d")


def fetch_ibkr_chain(
    expiry_dates: List[date],
    option_rights: List[str],
    target_deltas: List[float],
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 10,
    timeout: int = 30,
) -> Dict[Tuple[str, str], IBKRLeg]:
    """
    Connect to TWS/IB Gateway, find the XSP strike whose live delta best matches
    each target. Returns dict keyed (expiry_str, right) → IBKRLeg.
    """
    if not HAS_IBKR:
        raise RuntimeError("ib_insync not installed. Run: pip install ib_insync")

    ib_util.logToConsole(level=0)
    ib = IB()
    print(f"  [IBKR] Connecting to TWS at {host}:{port} ...", flush=True)
    try:
        ib.connect(host, port, clientId=client_id, timeout=timeout)
    except Exception as e:
        raise RuntimeError(f"IBKR connection failed: {e}")

    results: Dict[Tuple[str, str], IBKRLeg] = {}

    try:
        for expiry_date, right, target_delta in zip(expiry_dates, option_rights, target_deltas):
            exp_str = _ibkr_expiry_str(expiry_date)
            print(f"  [IBKR] Requesting chain  XSP  {exp_str}  {right}  ...", flush=True)

            ref = IBOption("XSP", exp_str, 0, right, "SMART", "USD")
            details = ib.reqContractDetails(ref)
            if not details:
                print(f"  [IBKR] WARNING: no chain data for {exp_str} {right} — skipping")
                continue

            strikes = sorted(set(float(d.contract.strike) for d in details))
            if not strikes:
                print(f"  [IBKR] WARNING: empty strike list for {exp_str} {right}")
                continue

            contracts = [IBOption("XSP", exp_str, k, right, "SMART", "USD") for k in strikes]
            ib.qualifyContracts(*contracts)

            tickers = ib.reqTickers(*contracts)
            time.sleep(2)
            tickers = ib.reqTickers(*contracts)

            best_leg: Optional[IBKRLeg] = None
            best_diff = float("inf")

            for ticker, strike in zip(tickers, strikes):
                greeks = ticker.modelGreeks or ticker.lastGreeks
                if greeks is None:
                    continue
                live_delta = greeks.delta
                if live_delta is None:
                    continue
                diff = abs(live_delta - target_delta)
                if diff < best_diff:
                    best_diff = diff
                    bid = ticker.bid if ticker.bid and ticker.bid > 0 else 0.0
                    ask = ticker.ask if ticker.ask and ticker.ask > 0 else 0.0
                    mid = (bid + ask) / 2.0 if (bid + ask) > 0 else 0.0
                    best_leg = IBKRLeg(
                        expiry_str=exp_str,
                        right=right,
                        strike=strike,
                        delta=live_delta,
                        mid_price=mid,
                    )

            if best_leg:
                results[(exp_str, right)] = best_leg
                print(f"  [IBKR]   Best match: K={best_leg.strike:.0f}  δ={best_leg.delta:+.4f}"
                      f"  mid=${best_leg.mid_price:.2f}  (target δ={target_delta:+.4f})")
            else:
                print(f"  [IBKR] WARNING: could not match delta for {exp_str} {right}")

    finally:
        ib.disconnect()
        print("  [IBKR] Disconnected.", flush=True)

    return results
