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


def price_tokens(
    input: int = 0,
    cached_input: int = 0,
    output: int = 0,
    reasoning: int = 0,
) -> dict[str, int]:
    """Price token quantities. Reasoning is folded into the output bucket first.

    Categories are priced separately and summed last:
    in*300 + cached*30 + (out + reasoning)*1500
    """
    input_cost = input * PRICING.input
    cached_input_cost = cached_input * PRICING.cached_input
    output_cost = (output + reasoning) * PRICING.output
    return {
        "input": input_cost,
        "cached_input": cached_input_cost,
        "output": output_cost,
        "total_micro_cents": input_cost + cached_input_cost + output_cost,
    }
