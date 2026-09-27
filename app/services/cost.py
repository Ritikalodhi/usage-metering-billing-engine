from app.config.pricing import PRICING


def price_tokens(
    input: int = 0,
    cached_input: int = 0,
    output: int = 0,
    reasoning: int = 0,
) -> dict[str, int]:
    """Price token quantities in integer micro-cents.

    Requirements:
    - Integer arithmetic only.
    - Input priced at PRICING.input.
    - Cached input priced separately at PRICING.cached_input.
    - Reasoning folded into the output bucket and priced at PRICING.output.
    - Categories calculated separately and summed last.
    - Returns per-category micro-cent breakdown plus total_micro_cents.
    """
    input_cost = input * PRICING.input
    cached_input_cost = cached_input * PRICING.cached_input
    output_cost = (output + reasoning) * PRICING.output
    total_micro_cents = input_cost + cached_input_cost + output_cost

    return {
        "input": input_cost,
        "cached_input": cached_input_cost,
        "output": output_cost,
        "total_micro_cents": total_micro_cents,
    }


def price_api_call(count: int = 1) -> int:
    """Price API call count in integer micro-cents."""
    return count * PRICING.api_call
