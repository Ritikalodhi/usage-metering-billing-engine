from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    code: str
    display_name: str
    api_call_limit: int
    token_limit: int


FREE = Plan(code="free", display_name="Free", api_call_limit=1_000, token_limit=100_000)
PRO = Plan(code="pro", display_name="Pro", api_call_limit=50_000, token_limit=5_000_000)

PLANS: dict[str, Plan] = {FREE.code: FREE, PRO.code: PRO}
