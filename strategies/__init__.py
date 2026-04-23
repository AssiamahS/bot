"""Strategy library.

Every public strategy in this package exposes two functions:

    def signal(market_state: dict) -> float:
        '''Return a desired position in [-1, +1]. Negative = short.'''

    def size(account_equity: float, signal: float, market_state: dict) -> float:
        '''Return dollar notional to trade. Delegates to sizing.py.'''

A strategy that computes size without calling sizing.py is a bug. The MM
bot that lost $30 sized from a naked ORDER_SIZE_USD constant; see
docs/POSTMORTEM.md. Every position in this package must respect
MAX_SINGLE_POSITION_FRAC.
"""

__all__ = ["sizing"]
