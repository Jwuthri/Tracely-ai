"""Drop-in Atlas Cloud tracing.

Atlas Cloud's API is OpenAI-compatible — the only difference from a vanilla OpenAI client is the
`base_url`. So this module is a thin convenience preset around `tracely_sdk.openai`: pass your
Atlas Cloud key, get a traced client pointed at `https://api.atlascloud.ai/v1`.

    from tracely_sdk.atlascloud import AtlasCloud            # a pre-wrapped, base-url-preset client
    client = AtlasCloud(api_key=...)
    client.chat.completions.create(model="deepseek-ai/deepseek-v4-flash", messages=[...])

    # or wrap an OpenAI() you already built — anything sent to `https://api.atlascloud.ai/v1`:
    from tracely_sdk.openai import wrap_openai
    from openai import OpenAI
    client = wrap_openai(OpenAI(api_key=..., base_url="https://api.atlascloud.ai/v1"))

One key reaches DeepSeek, GLM, Kimi, Qwen and MiniMax, so the `model` you pass is recorded verbatim
(e.g. `zai-org/glm-5.3-flash`) and the backend can attribute usage to the underlying lab for free.
`GET https://api.atlascloud.ai/v1/models` is public, so the id list needs no key to browse.
"""

from __future__ import annotations

from typing import Any

from .openai import wrap_openai

try:
    import openai as _openai
except ImportError as e:  # pragma: no cover
    raise ImportError("tracely_sdk.atlascloud requires the OpenAI SDK — pip install openai") from e

DEFAULT_BASE_URL = "https://api.atlascloud.ai/v1"


def AtlasCloud(*args: Any, **kwargs: Any) -> Any:
    """A traced `openai.OpenAI(...)` pre-pointed at Atlas Cloud."""
    kwargs.setdefault("base_url", DEFAULT_BASE_URL)
    return wrap_openai(_openai.OpenAI(*args, **kwargs))


def AsyncAtlasCloud(*args: Any, **kwargs: Any) -> Any:
    """A traced `openai.AsyncOpenAI(...)` pre-pointed at Atlas Cloud."""
    kwargs.setdefault("base_url", DEFAULT_BASE_URL)
    return wrap_openai(_openai.AsyncOpenAI(*args, **kwargs))
