"""Exponential reconnect backoff.

Spec (XIU-52): 1 → 2 → 4 → 8 → 16 → 30 s, then stays at 30 s. Reset to 1 s
on a successful (post-register) connection so we don't keep backing off
forever after a transient hiccup.
"""
from __future__ import annotations


class ExponentialBackoff:
    """Stateful backoff producer.

    >>> b = ExponentialBackoff(initial=1.0, cap=30.0)
    >>> [b.next() for _ in range(7)]
    [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]
    >>> b.reset()
    >>> b.next()
    1.0
    """

    def __init__(self, *, initial: float = 1.0, factor: float = 2.0, cap: float = 30.0) -> None:
        if initial <= 0 or factor <= 1 or cap < initial:
            raise ValueError("invalid backoff parameters")
        self._initial = initial
        self._factor = factor
        self._cap = cap
        self._current: float | None = None

    def next(self) -> float:
        if self._current is None:
            self._current = self._initial
        else:
            self._current = min(self._current * self._factor, self._cap)
        return self._current

    def reset(self) -> None:
        self._current = None
