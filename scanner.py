"""Compatibility entrypoint for the momentum scanner."""

import sys

from scanner_core import scan_stocks


if __name__ == "__main__":
    universe = sys.argv[1] if len(sys.argv) > 1 else "sp500"
    min_rs = int(sys.argv[2]) if len(sys.argv) > 2 else 85
    scan_stocks(universe=universe, min_rs=min_rs)
