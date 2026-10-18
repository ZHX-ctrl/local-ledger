"""Core package for the local two-currency ledger.

The public classes are loaded lazily so focused modules such as ``ledger_app.fx``
can be imported without pulling in storage integrations.
"""

__all__ = ["Ledger", "LedgerError"]


def __getattr__(name: str):
    if name in __all__:
        from .service import Ledger, LedgerError

        return {"Ledger": Ledger, "LedgerError": LedgerError}[name]
    raise AttributeError(name)
