"""Example agent entrypoint for `tracely replay`.

`tracely replay --entrypoint weather_agent:run` calls one of these with each promoted case's
recorded input, inside an env=ci agent span the CLI opens for you — so the function just emits
its child llm/tool spans (via call_llm / call_tool) and returns the answer.

These use `tracely.call_llm` / `tracely.call_tool`, which in HERMETIC replay serve the recorded
output and never invoke the live function. We make the live model RAISE on purpose: if replay is
hermetic, it's never called; with `--live` it blows up — proving CI isn't hitting the real model.

    tracely replay planner --entrypoint weather_agent:run          # fixed   -> gate PASS
    tracely replay planner --entrypoint weather_agent:run_broken   # regressed-> gate FAIL
    tracely replay planner --entrypoint weather_agent:run --live    # -> live model raises

Replay is STRICT: a call the recording lacks is an execution problem (INCOMPLETE), never a live
call. `run` adds the get_weather call the *original* failing recording never had, so it only
PASSes once the case has been re-recorded from a fixed run (`POST /api/cases/{id}/recapture`,
which seed_regression.py does) — a recording of the bug cannot stand in for the fix's new call.
"""

from __future__ import annotations

import tracely_sdk as tracely


def _live_model(prompt: str) -> str:
    raise RuntimeError("LIVE model call — would cost money / be nondeterministic in CI")


def run(prompt: str) -> str:
    """Fixed agent: consults the model (served from the recorded fixture in hermetic replay — the
    live model is never called), then actually calls get_weather (the fix the silent case wanted)."""
    answer = tracely.call_llm("gpt-4o", lambda: _live_model(prompt), input=prompt)
    tracely.call_tool("get_weather", lambda: '{"tempF": 64}')
    return answer


def run_broken(prompt: str) -> str:
    """Regression: answers straight from the model WITHOUT calling get_weather — the silent
    failure the promoted case was built to catch. The gate FAILs (required tool missing)."""
    return tracely.call_llm("gpt-4o", lambda: _live_model(prompt), input=prompt)


def run_handles(prompt: str) -> str:
    """Error-HANDLING fix: get_weather errors (served from the fixture, raises ToolError), but the
    agent CATCHES it and returns a safe answer — so the run outcome is clean. PASS under a case
    with allow_tool_errors (the tool may fail; the agent must handle it)."""
    try:
        w = tracely.call_tool("get_weather", lambda: '{"tempF": 64}')
    except tracely.ToolError:
        return "Sorry, I couldn't reach the weather service — please try again shortly."
    return f"It's {w} in San Francisco."


def run_crashes(prompt: str) -> str:
    """The unfixed agent: does NOT catch the tool error, so the ToolError propagates and the run
    crashes (run-level error). FAILs the case."""
    w = tracely.call_tool("get_weather", lambda: '{"tempF": 64}')
    return f"It's {w} in San Francisco."


def run_light(prompt: str) -> str:
    """Efficient version — reports modest token usage. Establishes the green-gate baseline."""
    tracely.call_llm("gpt-4o", lambda: _live_model(prompt), input=prompt, usage=(100, 20))
    tracely.call_tool("get_weather", lambda: '{"tempF": 64}')
    return "It's 64°F and sunny in San Francisco."


def run_heavy(prompt: str) -> str:
    """A 'works but costs way more' regression — same tools (gate still PASSes) but ~10x the tokens
    (verbose prompting). The gate stays green on fail-to-pass but raises a soft 'tokens +N%' warning."""
    tracely.call_llm("gpt-4o", lambda: _live_model(prompt), input=prompt, usage=(900, 300))
    tracely.call_tool("get_weather", lambda: '{"tempF": 64}')
    return "It's 64°F and sunny in San Francisco."


def run_multi(prompt: str) -> str:
    """Calls get_weather TWICE with different args. Exercises faithful fixtures: each call is
    served its own recorded output (by args), and the 2nd call — which ERRORED in production —
    replays as an errored span, so the gate reproduces the failure (with v1 name-keyed fixtures
    both calls got the same output and the error was silently dropped)."""
    tracely.call_llm("gpt-4o", lambda: _live_model(prompt), input=prompt)
    sf = tracely.call_tool("get_weather", lambda: '{"tempF": 64}', args='{"city":"SF"}')
    nyc = tracely.call_tool("get_weather", lambda: '{"tempF": 50}', args='{"city":"NYC"}')
    return f"SF={sf} NYC={nyc}"
