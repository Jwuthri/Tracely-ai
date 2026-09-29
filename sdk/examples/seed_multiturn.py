"""Multi-turn support conversation (NO API key needed) — the showcase for the rolling summary +
declared agents.

Emits ONE conversation of several turns via the MANUAL Tracely SDK (agent / thinking / tool / llm
spans with deterministic fake content — no provider key, no cost). Each turn is its own trace under
the same `conversation`, so:
  • the rolling summary accumulates across the turns (see the "Rolling summary" column), and
  • the declared agent catalog (sent once via `tracely.trace(agents=...)`) shows in the Conversation
    Agents panel and is available to evaluation as `@AGENTS`.

    TRACELY_API=http://localhost:8000 uv run python sdk/examples/seed_multiturn.py
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

import tracely_sdk as tracely
from _fake_db import AGENTS, check_inventory, compare_prices, get_order_status

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_PROJECT_ROOT / ".env", override=True)

API = os.environ.get("TRACELY_API", "http://localhost:8000")
KEY = os.environ.get("TRACELY_KEY", "tracely_dev_key")

tracely.init(endpoint=API, api_key=KEY, service_name="support-agent", env="prod", instrument=False)

# The shared two-agent catalog (Support Agent + Billing Agent) — declared by the user and sent with
# the conversation; the Support Agent hands the pricing turn off to the Billing Agent.
CATALOG = AGENTS

CONV = "support-multiturn-demo"


def _turn(
    index: int,
    agent_slug: str,
    user_msg: str,
    reasoning: str,
    tool_name: str,
    tool_args: dict,
    tool_out: object,
    answer: str,
    *,
    handoff_from: str | None = None,
) -> None:
    """One conversation turn = one trace: agent root → thinking → tool → llm answer. The catalog is
    declared once (turn 0) via `tracely.trace(agents=...)`, which flows it onto every span."""
    with tracely.trace(
        agent=agent_slug,
        conversation=CONV,
        turn=index,
        user="ada@example.com",
        agents=CATALOG if index == 0 else None,
    ):
        with tracely.agent(agent_slug, handoff_from=handoff_from) as root:
            tracely.set_io(root, input=user_msg, output=answer)
            with tracely.thinking() as th:
                tracely.set_io(th, output=reasoning)
            with tracely.tool(tool_name) as tl:
                tracely.set_io(tl, input=tool_args, output=tool_out)
            with tracely.llm("gpt-5.4-mini") as gen:
                tracely.set_io(gen, input=user_msg, output=answer)
                tracely.set_usage(gen, input_tokens=120 + index * 20, output_tokens=60)
    print(f"turn {index} [{agent_slug}]: {answer}")


def main() -> None:
    o1 = get_order_status("ORD-4471")
    inv1 = check_inventory("SKU-COAT-01")
    _turn(
        0, "support-agent",
        "Where is my order ORD-4471, and is the Alpine Winter Coat (SKU-COAT-01) back in stock?",
        "Customer asks about order ORD-4471 status and coat stock — look up both.",
        "get_order_status", {"order_id": "ORD-4471"}, o1,
        f"Order ORD-4471 is {o1['status'].replace('_', ' ')} ({o1['eta']}). "
        f"The Alpine Winter Coat has {inv1['in_stock']} in stock at ${inv1['price_usd']}.",
    )

    o2 = get_order_status("ORD-5588")
    _turn(
        1, "support-agent",
        "Thanks! Can you also check on my other order, ORD-5588?",
        "Follow-up about a second order ORD-5588 — look it up.",
        "get_order_status", {"order_id": "ORD-5588"}, o2,
        f"Order ORD-5588 is {o2['status']} — {o2['eta']}.",
    )

    inv2 = check_inventory("SKU-MUG-09")
    _turn(
        2, "support-agent",
        "Is the item in that order (the Ceramic Mug, SKU-MUG-09) in stock?",
        "Check inventory for the mug in order ORD-5588.",
        "check_inventory", {"sku": "SKU-MUG-09"}, inv2,
        f"The Ceramic Mug is currently out of stock ({inv2['in_stock']} on hand), priced ${inv2['price_usd']}.",
    )

    # Turn 3 is handed off to the Billing Agent to compare prices — a multi-agent turn.
    _turn(
        3, "billing-agent",
        "Got it. Between the coat and the mug, which one is cheaper?",
        "Pricing question — compare the coat ($129.00) and the mug ($14.50).",
        "compare_prices", {"sku_a": "SKU-COAT-01", "sku_b": "SKU-MUG-09"},
        compare_prices("SKU-COAT-01", "SKU-MUG-09"),
        "The Ceramic Mug is cheaper — $14.50 vs $129.00 for the coat.",
        handoff_from="support-agent",
    )

    tracely.flush()
    print(
        f"sent — 4-turn conversation '{CONV}'. Open Tracely → Traces → the conversation: the rolling "
        "summary accumulates across turns, and the Agents panel shows the declared catalog."
    )


if __name__ == "__main__":
    main()
