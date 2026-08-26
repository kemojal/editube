"""Claude client for the AI creative director.

Separate from `ai_client.py` (Gemini) on purpose rather than hidden behind a
shared "AI" abstraction: the two are used for different things and their request
shapes have almost nothing in common. Gemini writes short JSON blobs for
metadata and captions; Claude reads a whole transcript and writes an edit plan,
which needs forced-tool structured output, prompt caching across passes, refusal
handling, and per-run token accounting. A lowest-common-denominator wrapper
would give up all four.

Three things about Claude Opus 5 shape this module and are easy to get wrong:

* **Sampling parameters are rejected.** `temperature`, `top_p` and `top_k` all
  return 400. Steering happens in the prompt; there is no knob to turn.
* **Thinking is on by default and has no token budget.** `budget_tokens` is
  gone (400). Depth is controlled with `output_config.effort`.
* **A refusal is a successful HTTP 200.** Safety classifiers can decline a
  request and return `stop_reason: "refusal"` with an empty or partial
  `content`. Code that reads `content[0]` without checking first breaks on it,
  so every read here goes through `_require_tool_input`.

The SDK import is lazy so a deployment with no `ANTHROPIC_API_KEY` never pays
for it, matching how `ai_client.py` treats the Gemini SDK.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Claude Opus 5. Overridable so a deployment can pin or downgrade without a
#: code change, but the director is written and evaluated against Opus 5.
MODEL = os.getenv("EDITUBE_CLAUDE_MODEL", "claude-opus-5")

#: `high` is the API default and the right floor for planning work. `medium` is
#: the cost-saving step for cheaper passes; see `director_prompts` for which
#: pass runs at which level.
DEFAULT_EFFORT = os.getenv("EDITUBE_CLAUDE_EFFORT", "high")

#: Planning output runs long. Anything above ~16k must stream or the request
#: risks an HTTP timeout, so this module always streams.
DEFAULT_MAX_TOKENS = int(os.getenv("EDITUBE_CLAUDE_MAX_TOKENS", "16000") or "16000")

#: Planning turns can run for minutes at high effort.
DEFAULT_TIMEOUT_SEC = float(os.getenv("EDITUBE_CLAUDE_TIMEOUT_SEC", "900") or "900")

#: Server-side refusal fallback. On a policy decline the API re-runs the request
#: on Anthropic's recommended substitute inside the same call, so a benign
#: request that trips a classifier still gets an answer instead of an exception.
#: `"default"` routes by refusal category rather than pinning a model, which
#: means no migration when the recommended substitute changes.
_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_SERVER_FALLBACK_ENABLED = (os.getenv("EDITUBE_CLAUDE_SERVER_FALLBACK", "1") or "1").strip() not in {
    "0",
    "false",
    "no",
}


class ClaudeError(RuntimeError):
    """Anything that stopped this module returning a usable plan."""


class ClaudeNotConfigured(ClaudeError):
    """No API key. Callers treat this as "the feature is off", not a failure."""


class ClaudeRefused(ClaudeError):
    """Safety classifiers declined, and the fallback chain declined too.

    Carries the category so a caller can tell a cyber/bio decline (retrying is
    pointless) from a transient one.
    """

    def __init__(self, category: str | None, explanation: str | None) -> None:
        self.category = category
        self.explanation = explanation
        super().__init__(
            f"Claude declined the request ({category or 'unspecified'})"
            + (f": {explanation}" if explanation else "")
        )


class ClaudeMalformedOutput(ClaudeError):
    """The model stopped without producing the tool call it was forced to make."""


@dataclass
class ClaudeUsage:
    """Token accounting for one run, summed across passes.

    `cache_read_input_tokens` is the number worth watching: if it stays zero
    across a multi-pass run, the cached prefix is being invalidated somewhere
    and every pass is paying full price for the same system prompt.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def add(self, raw: Any) -> None:
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            value = getattr(raw, name, None)
            if isinstance(value, int):
                setattr(self, name, getattr(self, name) + value)

    @property
    def cached(self) -> bool:
        return self.cache_read_input_tokens > 0

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
        }


@dataclass
class ClaudeResult:
    """One structured response: the validated tool input, plus what it cost."""

    data: dict[str, Any]
    usage: ClaudeUsage = field(default_factory=ClaudeUsage)
    model: str = MODEL
    stop_reason: str = ""


def available() -> bool:
    """Whether this deployment can call Claude at all.

    Callers gate on this rather than catching `ClaudeNotConfigured`, so a
    missing key reads as "the director is not enabled here" instead of an error
    on every video.
    """
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())


def _client() -> Any:
    from anthropic import Anthropic  # lazy: see module docstring

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise ClaudeNotConfigured("ANTHROPIC_API_KEY is not set")
    return Anthropic(api_key=api_key, timeout=DEFAULT_TIMEOUT_SEC, max_retries=3)


def build_tool(
    name: str, description: str, schema: dict[str, Any], *, strict: bool = True
) -> dict[str, Any]:
    """A tool definition the model is forced to call.

    Forcing a tool call is how the plan comes back schema-valid *by
    construction* rather than by parsing prose and hoping. `strict` makes the
    API validate the arguments against the schema and re-prompt the model on a
    mismatch, which requires `additionalProperties: false` — so that is set here
    rather than left to every caller to remember.
    """
    prepared = dict(schema)
    if prepared.get("type") == "object":
        prepared.setdefault("additionalProperties", False)
    return {
        "name": name,
        "description": description,
        "input_schema": prepared,
        "strict": strict,
    }


def _system_blocks(system: str, *, cache: bool) -> list[dict[str, Any]]:
    """The system prompt, optionally marked as a cache breakpoint.

    Everything stable belongs in here — the instructions and the capability
    manifest — because caching is a prefix match and the transcript that follows
    changes every run. Opus 5's minimum cacheable prefix is 512 tokens, low
    enough that the manifest alone qualifies.
    """
    block: dict[str, Any] = {"type": "text", "text": system}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return [block]


def _with_cache_breakpoint(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy `messages`, marking the last content block as a cache breakpoint.

    Copied rather than mutated for two reasons: breakpoints would otherwise
    accumulate one per pass and blow the four-per-request limit, and the stored
    history should stay a plain record of the conversation rather than carrying
    request-shaping metadata around with it.

    A message whose content is a bare string cannot take a breakpoint (there is
    no block to attach it to), so it is promoted to a single text block.
    """
    if not messages:
        return []
    head, last = messages[:-1], dict(messages[-1])
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [
            {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
        ]
    elif isinstance(content, list) and content:
        blocks = list(content)
        final = blocks[-1]
        if isinstance(final, dict):
            blocks[-1] = {**final, "cache_control": {"type": "ephemeral"}}
            last["content"] = blocks
        else:
            # An SDK content object (e.g. a thinking block) rather than a dict.
            # It cannot be annotated, so leave the turn uncached rather than
            # risk corrupting a block that has to replay unchanged.
            return list(messages)
    else:
        return list(messages)
    return head + [last]


def _require_tool_input(message: Any, tool_name: str) -> dict[str, Any]:
    """Pull the forced tool call out of a response, refusals handled first.

    The ordering matters: a refusal is an HTTP 200 whose `content` may be empty
    or a partial, so `stop_reason` has to be read before anything touches the
    content blocks.
    """
    stop_reason = getattr(message, "stop_reason", "") or ""
    if stop_reason == "refusal":
        details = getattr(message, "stop_details", None)
        raise ClaudeRefused(
            getattr(details, "category", None), getattr(details, "explanation", None)
        )

    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            data = getattr(block, "input", None)
            if isinstance(data, dict):
                return data

    # `max_tokens` is the usual cause: the model was still assembling the tool
    # call when the ceiling cut it off. Say so, because "no tool call" on its
    # own sends people looking at the schema instead of the budget.
    raise ClaudeMalformedOutput(
        f"Claude returned no `{tool_name}` call (stop_reason={stop_reason!r}). "
        "If this is stop_reason='max_tokens', raise max_tokens."
    )


def _looks_like_fallback_rejection(exc: Exception) -> bool:
    """Whether a 400 is about the server-side fallback rather than our request.

    The fallback beta is not available on every deployment surface. Losing the
    refusal safety net is survivable; failing every director run because of it
    is not, so a 400 that names it is retried once without.
    """
    text = str(getattr(exc, "message", "") or exc).lower()
    return "fallback" in text or _FALLBACK_BETA in text


def generate_structured(
    *,
    system: str,
    messages: list[dict[str, Any]],
    tool: dict[str, Any],
    effort: str = DEFAULT_EFFORT,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    cache_system: bool = True,
    model: str = MODEL,
) -> ClaudeResult:
    """Force one structured tool call and return its validated arguments.

    Always streams. Planning responses run long, and a non-streaming request
    with a large `max_tokens` risks an idle-connection timeout well before the
    model is finished — the SDK refuses some of them outright for that reason.
    """
    client = _client()
    tool_name = str(tool["name"])

    request: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": _system_blocks(system, cache=cache_system),
        "messages": messages,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": tool_name},
        # Adaptive is Opus 5's default; stated explicitly so the intent survives
        # a future model swap. Never `budget_tokens` — removed, and a 400.
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
        # Deliberately absent: temperature / top_p / top_k, all 400 on Opus 5.
    }

    def _call(with_fallback: bool) -> Any:
        kwargs = dict(request)
        if with_fallback:
            kwargs["betas"] = [_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        with client.beta.messages.stream(**kwargs) as stream:
            return stream.get_final_message()

    want_fallback = _SERVER_FALLBACK_ENABLED
    try:
        message = _call(want_fallback)
    except Exception as exc:  # noqa: BLE001 - narrowed immediately below
        from anthropic import BadRequestError

        if want_fallback and isinstance(exc, BadRequestError) and _looks_like_fallback_rejection(exc):
            logger.warning(
                "Server-side refusal fallback unavailable here; continuing without it: %s", exc
            )
            message = _call(False)
        else:
            raise

    usage = ClaudeUsage()
    usage.add(getattr(message, "usage", None))
    data = _require_tool_input(message, tool_name)
    return ClaudeResult(
        data=data,
        usage=usage,
        model=str(getattr(message, "model", model)),
        stop_reason=str(getattr(message, "stop_reason", "") or ""),
    )


class Conversation:
    """A multi-pass planning run that reuses one cached prefix.

    The director reads, then directs, then critiques. Running those as three
    separate conversations would re-send and re-bill the system prompt and the
    transcript every time; running them as turns of one conversation means every
    pass after the first reads the shared prefix from cache at about a tenth of
    the price.

    Two protocol details this handles so callers do not have to:

    * A forced tool call ends the turn with `stop_reason: "tool_use"`. The
      conversation cannot continue until a matching `tool_result` is sent back,
      even though the "result" here is just an acknowledgement — we already have
      what we wanted.
    * The assistant's content is appended **whole**, including thinking blocks.
      They must be replayed unchanged on the same model; editing or dropping
      them breaks the turn.
    """

    def __init__(self, system: str, *, model: str = MODEL) -> None:
        self.system = system
        self.model = model
        self.messages: list[dict[str, Any]] = []
        self.usage = ClaudeUsage()

    def ask(
        self,
        prompt: str,
        *,
        tool: dict[str, Any],
        effort: str = DEFAULT_EFFORT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict[str, Any]:
        """Add a turn, force `tool`, and return its arguments."""
        # Cache breakpoint on the turn *before* the new question, so everything
        # already established — including the transcript, which is the bulk of
        # the prefix and never changes after pass A — is read back rather than
        # re-billed. Marking the system prompt alone would leave the transcript
        # at full price on every subsequent pass, which on a long video is most
        # of the cost of running the director at all.
        history = _with_cache_breakpoint(self.messages)
        self.messages.append({"role": "user", "content": prompt})
        request_messages = history + [self.messages[-1]]

        client = _client()
        tool_name = str(tool["name"])
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": _system_blocks(self.system, cache=True),
            "messages": request_messages,
            "tools": [tool],
            "tool_choice": {"type": "tool", "name": tool_name},
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
        }

        def _call(with_fallback: bool) -> Any:
            kwargs = dict(request)
            if with_fallback:
                kwargs["betas"] = [_FALLBACK_BETA]
                kwargs["fallbacks"] = "default"
            with client.beta.messages.stream(**kwargs) as stream:
                return stream.get_final_message()

        want_fallback = _SERVER_FALLBACK_ENABLED
        try:
            message = _call(want_fallback)
        except Exception as exc:  # noqa: BLE001 - narrowed immediately below
            from anthropic import BadRequestError

            if (
                want_fallback
                and isinstance(exc, BadRequestError)
                and _looks_like_fallback_rejection(exc)
            ):
                logger.warning("Server-side refusal fallback unavailable here: %s", exc)
                message = _call(False)
            else:
                # The failed turn must not stay in the history, or a retry
                # replays a user message the model never answered.
                self.messages.pop()
                raise

        self.usage.add(getattr(message, "usage", None))
        try:
            data = _require_tool_input(message, tool_name)
        except ClaudeError:
            self.messages.pop()
            raise

        content = getattr(message, "content", None) or []
        self.messages.append({"role": "assistant", "content": content})
        tool_use_id = next(
            (
                getattr(block, "id", None)
                for block in content
                if getattr(block, "type", None) == "tool_use"
            ),
            None,
        )
        if tool_use_id:
            self.messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": "Recorded.",
                        }
                    ],
                }
            )
        return data
