#!/usr/bin/env python3
"""
Vanna-Neutral 3-1-1 Hedge Calculator
=====================================
Entry point — logic lives in the rj/ package.

Usage:
  python RJ.py --spot 741.28 --vix 18.5
  python RJ.py --live
  python RJ.py --live --ibkr --ibkr-port 4001
  python RJ.py --scenario 650

Dependencies: pip install scipy tabulate
Optional:     pip install yfinance ib_insync
"""

from hedge.cli import main

if __name__ == "__main__":
    main()
