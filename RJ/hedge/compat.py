"""Optional dependency detection. Import HAS_* flags and the actual libs from here."""

try:
    from scipy.stats import norm
    from scipy.optimize import brentq
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False
    norm = None
    brentq = None

try:
    from tabulate import tabulate
    HAS_TABULATE = True
except ImportError:
    HAS_TABULATE = False
    tabulate = None

try:
    import yfinance as yf
    HAS_YFINANCE = True
except ImportError:
    HAS_YFINANCE = False
    yf = None

try:
    from ib_insync import IB, Option as IBOption, util as ib_util
    HAS_IBKR = True
except ImportError:
    HAS_IBKR = False
    IB = None
    IBOption = None
    ib_util = None
