"""
session/registry.py — central registry of MarketSessionDetector classes.

Detectors register themselves at module-import time so adding a new market
is a one-file change: drop a `detectors/your_market.py` file containing a
detector class decorated with @register_detector, and the manager picks it
up automatically on the next backend restart.

The registry is intentionally minimal — just a dict keyed by market_type.
"""
from __future__ import annotations

import logging
from typing import Dict, Iterable, Type

from .base import MarketSessionDetector

log = logging.getLogger("session.registry")


_REGISTRY: Dict[str, Type[MarketSessionDetector]] = {}


def register_detector(cls: Type[MarketSessionDetector]) -> Type[MarketSessionDetector]:
    """
    Class decorator. Register a detector by its `market_type` attribute.

    Usage:
        @register_detector
        class KalshiBinary15mDetector(MarketSessionDetector):
            market_type = "kalshi_15m"
            ...
    """
    if not getattr(cls, "market_type", ""):
        raise ValueError(f"{cls.__name__} is missing the `market_type` class attribute")
    if cls.market_type in _REGISTRY:
        log.warning("Detector for market_type=%r already registered (%s) — replacing with %s",
                    cls.market_type, _REGISTRY[cls.market_type].__name__, cls.__name__)
    _REGISTRY[cls.market_type] = cls
    log.info("Registered session detector: %s -> %s", cls.market_type, cls.__name__)
    return cls


def get_detector_class(market_type: str) -> Type[MarketSessionDetector]:
    """Look up a registered detector class by market_type."""
    if market_type not in _REGISTRY:
        raise KeyError(
            f"No session detector registered for market_type={market_type!r}. "
            f"Known: {sorted(_REGISTRY)}"
        )
    return _REGISTRY[market_type]


def all_market_types() -> Iterable[str]:
    """Return the set of all registered market_type ids."""
    return list(_REGISTRY.keys())


def discover() -> None:
    """
    Force-import every module in the `detectors/` package so their
    @register_detector decorators run. The session manager calls this once
    at startup; you don't need to call it from elsewhere.
    """
    import importlib
    import pkgutil
    from . import detectors

    for module_info in pkgutil.iter_modules(detectors.__path__):
        if module_info.name.startswith("_"):
            continue
        importlib.import_module(f"{detectors.__name__}.{module_info.name}")
