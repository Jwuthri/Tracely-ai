"""A tiny fake e-commerce 'database' + tools, shared by the example agents.

No real I/O — deterministic in-memory data so the examples are reproducible and free. The tool
functions are plain Python; each example wires them into its provider/harness's native tool-calling
format (the schemas below cover the common shapes). Import what you need:

    from _fake_db import QUESTION, OPENAI_TOOLS, observed_tools    # OpenAI / Mistral / LiteLLM
    from _fake_db import ANTHROPIC_TOOLS, BEDROCK_TOOLS            # Anthropic / Bedrock
    from _fake_db import get_order_status, check_inventory         # raw fns (Gemini / @observe)
"""

from __future__ import annotations

from typing import Any

# ── the "database" ───────────────────────────────────────────────────────────
ORDERS: dict[str, dict[str, Any]] = {
    "ORD-4471": {
        "status": "out_for_delivery",
        "eta": "today by 6pm",
        "items": ["SKU-COAT-01"],
        "customer": "ada@example.com",
    },
    "ORD-5588": {
        "status": "processing",
        "eta": "ships in ~2 days",
        "items": ["SKU-MUG-09"],
        "customer": "grace@example.com",
    },
}
INVENTORY: dict[str, dict[str, Any]] = {
    "SKU-COAT-01": {"name": "Alpine Winter Coat", "in_stock": 3, "price_usd": 129.0},
    "SKU-MUG-09": {"name": "Ceramic Mug", "in_stock": 0, "price_usd": 14.5},
}


# ── tools (plain Python — pass directly to frameworks that wrap them) ─────────
# Kept RAW (no @observe) so framework examples that auto-trace tool dispatch (LangChain `@tool`,
# LlamaIndex FunctionTool, CrewAI, OpenAI Agents SDK, Google ADK, Claude Agent SDK) don't get a
# duplicate inner TOOL span on top of the framework's own. Provider-SDK examples that dispatch
# tools themselves (OpenAI/Anthropic/Mistral/Bedrock/LiteLLM + drop-ins) build their dispatch map
# from `observed_tools()` below — the same fns decorated once with @observe(as_type="tool") — so
# each tool call becomes a real TOOL span under the agent root with no change at the call site.
def get_order_status(order_id: str) -> dict:
    """Look up an order's delivery status and ETA by its order id (e.g. ORD-4471)."""
    return ORDERS.get(order_id, {"error": f"no order {order_id}"})


def check_inventory(sku: str) -> dict:
    """Check current stock level and price for a product SKU (e.g. SKU-COAT-01)."""
    # tolerate both "SKU-COAT-01" and "COAT-01" — models often strip the prefix
    if sku in INVENTORY:
        return INVENTORY[sku]
    if f"SKU-{sku}" in INVENTORY:
        return INVENTORY[f"SKU-{sku}"]
    return {"error": f"no SKU {sku}"}


def compare_prices(sku_a: str, sku_b: str) -> dict:
    """Compare two SKUs' prices and report which is cheaper (the Billing Agent's tool)."""
    a, b = check_inventory(sku_a), check_inventory(sku_b)
    if "price_usd" not in a or "price_usd" not in b:
        return {"error": "one of the SKUs was not found"}
    cheaper = sku_a if a["price_usd"] <= b["price_usd"] else sku_b
    return {"cheaper": cheaper, "prices": {sku_a: a["price_usd"], sku_b: b["price_usd"]}}


TOOL_IMPLS = {
    "get_order_status": get_order_status,
    "check_inventory": check_inventory,
    "compare_prices": compare_prices,
}
# Which tools belong to which agent — the Support Agent looks things up; the Billing Agent compares
# prices. Examples give each agent only its own tools (`openai_tools(SUPPORT_TOOLS)`).
SUPPORT_TOOLS = ["get_order_status", "check_inventory"]
BILLING_TOOLS = ["compare_prices"]


_DESCRIPTIONS = {
    "get_order_status": "Look up an order's delivery status and ETA by its order id.",
    "check_inventory": "Check current stock level and price for a product SKU.",
    "compare_prices": "Compare the prices of two SKUs and report which is cheaper.",
}
_PARAMETERS: dict[str, dict] = {
    "get_order_status": {
        "type": "object",
        "properties": {"order_id": {"type": "string", "description": "e.g. ORD-4471"}},
        "required": ["order_id"],
    },
    "check_inventory": {
        "type": "object",
        "properties": {"sku": {"type": "string", "description": "e.g. SKU-COAT-01"}},
        "required": ["sku"],
    },
    "compare_prices": {
        "type": "object",
        "properties": {
            "sku_a": {"type": "string", "description": "first SKU, e.g. SKU-COAT-01"},
            "sku_b": {"type": "string", "description": "second SKU, e.g. SKU-MUG-09"},
        },
        "required": ["sku_a", "sku_b"],
    },
}


# ── per-format tool schemas (built from the shared definitions above) ──────────
# Each builder takes the tool-name list for one agent (default = all tools).
def openai_tools(names: list[str] | None = None) -> list[dict]:  # OpenAI / Mistral / LiteLLM
    return [
        {"type": "function", "function": {"name": n, "description": _DESCRIPTIONS[n], "parameters": _PARAMETERS[n]}}
        for n in (names or list(TOOL_IMPLS))
    ]


def anthropic_tools(names: list[str] | None = None) -> list[dict]:
    return [
        {"name": n, "description": _DESCRIPTIONS[n], "input_schema": _PARAMETERS[n]}
        for n in (names or list(TOOL_IMPLS))
    ]


def bedrock_tools(names: list[str] | None = None) -> list[dict]:  # AWS Bedrock (converse)
    return [
        {"toolSpec": {"name": n, "description": _DESCRIPTIONS[n], "inputSchema": {"json": _PARAMETERS[n]}}}
        for n in (names or list(TOOL_IMPLS))
    ]


SYSTEM = "You are a customer-support agent. Use the tools to look up real data before answering; be concise."
BILLING_SYSTEM = "You are a billing agent. Use compare_prices to answer pricing questions; be concise."
QUESTION = "Where is my order ORD-4471, and is the Alpine Winter Coat (SKU-COAT-01) back in stock?"

# Follow-up turns for the MULTI-TURN conversation. Each turn is threaded with the prior turns (see
# `Conversation` below) so it's a real dialogue — the model sees system + prev user + prev assistant
# + … + this turn's question. The questions stay SELF-CONTAINED (each names its own order id / SKUs)
# so every turn still grades on its own even though it now carries the conversation's context.
FOLLOWUPS = [
    "Thanks! Can you also check on my other order, ORD-5588?",
    "Is the Ceramic Mug (SKU-MUG-09) in stock?",
    "Between the Alpine Winter Coat (SKU-COAT-01) and the Ceramic Mug (SKU-MUG-09), which is cheaper?",
]

# The conversation is handled by two agents: the Support Agent takes the first turns, then hands the
# pricing-comparison turn to the Billing Agent. `TURNS` pairs each user message with the slug of the
# agent that should answer it — examples iterate it, so both agents show up in the trace.
TURNS: list[tuple[str, str]] = [
    (QUESTION, "support-agent"),
    (FOLLOWUPS[0], "support-agent"),
    (FOLLOWUPS[1], "support-agent"),
    (FOLLOWUPS[2], "billing-agent"),
]


class Conversation:
    """The conversation's memory — its prior user/assistant turns — so each new turn is threaded with
    the context that came before it instead of being an isolated one-shot. Without this, every turn
    would be sent as just `[system, user]` and a follow-up like "check my *other* order" would have no
    prior turn to refer to; with it the model sees `system + prev user + prev assistant + … + new user`.

    Only the *clean* turns are carried forward — the user's question and the assistant's final answer,
    which is what a chat app persists. The intra-turn tool-call scaffolding stays inside the turn that
    produced it (so threaded history never contains a dangling tool call without its result).

    Usage (in the per-turn loop):
        history = Conversation()
        for question, slug in TURNS:
            answer = run(question, ...)          # build the turn's input from `history.prior()`
            history.record(question, answer)
    """

    def __init__(self) -> None:
        self.turns: list[dict[str, str]] = []

    def prior(self) -> list[dict[str, str]]:
        """The conversation so far as plain `{role, content}` messages — prepend before the new user
        message to thread the turn (a fresh list each call, safe to splice into a provider payload)."""
        return list(self.turns)

    def record(self, question: str, answer: str) -> None:
        """Append a completed turn (the user question + the assistant's final answer)."""
        self.turns.append({"role": "user", "content": question})
        self.turns.append({"role": "assistant", "content": answer or ""})

# The DECLARED agent catalog a user sends with the conversation via `tracely.trace(agents=AGENTS)`.
# Shape: [{name, description, tools: {tool_name: {name, description, parameters}}}] — surfaced in the
# Conversation Agents panel and usable in evaluation (@AGENTS).
def _catalog(name: str, description: str, names: list[str]) -> dict:
    return {
        "name": name,
        "description": description,
        "tools": {n: {"name": n, "description": _DESCRIPTIONS[n], "parameters": _PARAMETERS[n]} for n in names},
    }


AGENTS = [
    _catalog("Support Agent", "Handles customer order and inventory inquiries.", SUPPORT_TOOLS),
    _catalog("Billing Agent", "Answers pricing/refund questions; consulted by the Support Agent.", BILLING_TOOLS),
]


def run_tool(name: str, args: dict) -> dict:
    """Dispatch a model-requested tool call to the raw fake-DB impls (no span)."""
    fn = TOOL_IMPLS.get(name)
    return fn(**args) if fn else {"error": f"unknown tool {name}"}


def observed_tools(names: list[str] | None = None) -> dict:
    """The fake-DB tools, each wrapped with `@tracely.observe(as_type="tool")` — so a hand-rolled
    tool-calling loop auto-emits a TOOL span per call (input=args, output=result), nested under the
    agent run. Pass a name list to wrap just one agent's tools.

    This is the whole point: the ONLY change a user makes to trace their own tool calls is decorating
    their tool functions once — the dispatch stays exactly as they wrote it. Provider-SDK examples
    (OpenAI/Anthropic/Mistral/Bedrock/LiteLLM/Gemini + the drop-ins) build their dispatch map from
    this. Framework examples (LangChain/LlamaIndex/CrewAI/Agents SDKs) keep the raw `TOOL_IMPLS` so
    they don't double-trace on top of the framework's own tool spans."""
    import tracely_sdk as tracely

    return {n: tracely.observe(TOOL_IMPLS[n], name=n, as_type="tool") for n in (names or list(TOOL_IMPLS))}
