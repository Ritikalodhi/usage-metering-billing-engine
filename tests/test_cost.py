from app.config.pricing import PRICING
from app.services.cost import price_api_call, price_tokens


def test_cached_input_rate_is_ten_percent_of_normal_input_rate():
    """1. cached-input rate is exactly 10% of the normal input rate."""
    token_count = 100_000

    input_cost = price_tokens(input=token_count)
    cached_cost = price_tokens(cached_input=token_count)

    # Unit rate check
    assert PRICING.cached_input * 10 == PRICING.input
    assert PRICING.cached_input == 30
    assert PRICING.input == 300

    # Calculated cost check
    assert cached_cost["cached_input"] * 10 == input_cost["input"]
    assert cached_cost["total_micro_cents"] * 10 == input_cost["total_micro_cents"]
    assert cached_cost["total_micro_cents"] == 3_000_000
    assert input_cost["total_micro_cents"] == 30_000_000


def test_reasoning_tokens_priced_identically_to_output_tokens():
    """2. reasoning tokens are priced identically to output tokens, one-for-one."""
    token_count = 50_000

    output_only = price_tokens(output=token_count)
    reasoning_only = price_tokens(reasoning=token_count)

    assert output_only["output"] == reasoning_only["output"]
    assert output_only["total_micro_cents"] == reasoning_only["total_micro_cents"]

    # Combined reasoning + output test
    split_result = price_tokens(output=30_000, reasoning=20_000)
    assert split_result["output"] == output_only["output"]
    assert split_result["total_micro_cents"] == output_only["total_micro_cents"]


def test_regression_naive_sum_times_input_rate_differs_from_total():
    """3. regression test proving that (input + cached_input + output + reasoning) * input_rate

    does NOT equal the calculator's total.
    """
    input_tokens = 1_000
    cached_input_tokens = 500
    output_tokens = 200
    reasoning_tokens = 100

    total_tokens = input_tokens + cached_input_tokens + output_tokens + reasoning_tokens
    naive_total = total_tokens * PRICING.input  # (1000 + 500 + 200 + 100) * 300 = 540_000

    result = price_tokens(
        input=input_tokens,
        cached_input=cached_input_tokens,
        output=output_tokens,
        reasoning=reasoning_tokens,
    )
    actual_total = result["total_micro_cents"]  # 765_000

    # Naive formula drastically misprices cached input (underpriced) and output/reasoning (underpriced)
    assert naive_total != actual_total
    assert naive_total == 540_000
    assert actual_total == 765_000


def test_worked_example_literal_micro_cents():
    """4. a worked example using specific input/cached_input/output/reasoning counts

    with the expected micro-cent total written as a literal number in the test.

    Worked breakdown:
      - 1,000 input tokens @ 300 µ¢/token = 300,000 µ¢ ($0.003)
      - 500 cached input tokens @ 30 µ¢/token = 15,000 µ¢ ($0.00015)
      - 200 output tokens + 100 reasoning tokens = 300 tokens @ 1500 µ¢/token = 450,000 µ¢ ($0.0045)
      - Total = 300,000 + 15,000 + 450,000 = 765,000 µ¢ ($0.00765)
    """
    result = price_tokens(
        input=1_000,
        cached_input=500,
        output=200,
        reasoning=100,
    )

    assert result["input"] == 300_000
    assert result["cached_input"] == 15_000
    assert result["output"] == 450_000
    assert result["total_micro_cents"] == 765_000


def test_price_api_call():
    """Verify price_api_call defaults to count=1 and computes count * PRICING.api_call."""
    assert PRICING.api_call == 200
    assert price_api_call() == 200
    assert price_api_call(1) == 200
    assert price_api_call(5) == 1_000
    assert price_api_call(100) == 20_000
