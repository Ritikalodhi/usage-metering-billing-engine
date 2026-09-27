from dataclasses import dataclass


@dataclass(frozen=True)
class Pricing:
    """Rates in integer micro-cents (1 cent = 1_000_000 µ¢). No floats."""

    input: int = 300  # $3.00 / 1M
    cached_input: int = 30  # $0.30 / 1M
    output: int = 1500  # $15.00 / 1M
    reasoning: int = 1500  # billed as output
    api_call: int = 200  # $0.002 / call


PRICING = Pricing()
