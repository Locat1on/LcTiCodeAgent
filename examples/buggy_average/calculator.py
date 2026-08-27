from collections.abc import Sequence


def average(values: Sequence[float]) -> float:
    """Return the arithmetic mean of values."""
    return sum(values) / len(values)

