"""The Claude client's request shape and failure handling.

There is no API key in CI, so these fake the SDK surface. That is the right
level anyway: what can actually break here is not Anthropic's behaviour but
*ours* — sending a parameter Opus 5 rejects, reading `content` before checking
for a refusal, or letting a failed turn poison a conversation's history. Each of
those is a silent or confusing failure in production and a cheap assertion here.
"""

from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

import httpx

from app.services import claude_client as cc


class _Block:
    def __init__(self, type_: str, **kw: Any) -> None:
        self.type = type_
        for key, value in kw.items():
            setattr(self, key, value)


class _Usage:
    def __init__(self, **kw: int) -> None:
        self.input_tokens = kw.get("input_tokens", 0)
        self.output_tokens = kw.get("output_tokens", 0)
        self.cache_read_input_tokens = kw.get("cache_read_input_tokens", 0)
        self.cache_creation_input_tokens = kw.get("cache_creation_input_tokens", 0)


class _Message:
    def __init__(self, content, stop_reason="tool_use", usage=None, stop_details=None):
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage or _Usage()
        self.stop_details = stop_details
        self.model = "claude-opus-5"


class _Stream:
    def __init__(self, message: _Message) -> None:
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self._message


class _FakeClient:
    """Records every request and replays a scripted sequence of outcomes."""

    def __init__(self, outcomes) -> None:
        self.calls: list[dict[str, Any]] = []
        self._outcomes = list(outcomes)
        self.beta = mock.Mock()
        self.beta.messages.stream = self._stream

    def _stream(self, **kwargs):
        # `Conversation` passes its live history by reference (the real SDK
        # serialises it immediately, so that is fine in production). Snapshot it
        # here or every recorded call aliases the same list and shows the state
        # after the *last* turn.
        recorded = dict(kwargs)
        recorded["messages"] = list(kwargs.get("messages") or [])
        self.calls.append(recorded)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _Stream(outcome)


def _tool_use(data, name="submit_edit_plan"):
    return _Message([_Block("tool_use", id="toolu_1", name=name, input=data)])


def _bad_request(message: str) -> Exception:
    from anthropic import BadRequestError

    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return BadRequestError(message, response=httpx.Response(400, request=request), body=None)


TOOL = cc.build_tool(
    "submit_edit_plan",
    "Submit the plan.",
    {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
)


def _patch(client: _FakeClient):
    return mock.patch.object(cc, "_client", return_value=client)


class ConfigurationTests(unittest.TestCase):
    def test_available_follows_the_api_key(self) -> None:
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            self.assertTrue(cc.available())
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "   "}):
            self.assertFalse(cc.available())

    def test_a_missing_key_is_its_own_error(self) -> None:
        """Callers gate on this rather than treating it as a run failure."""
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}):
            with self.assertRaises(cc.ClaudeNotConfigured):
                cc._client()

    def test_the_model_is_opus_5(self) -> None:
        self.assertEqual(cc.MODEL, "claude-opus-5")


class ToolDefinitionTests(unittest.TestCase):
    def test_strict_schemas_forbid_extra_properties(self) -> None:
        """`strict` is rejected without it, so it is set here, not by callers."""
        self.assertIs(TOOL["input_schema"]["additionalProperties"], False)
        self.assertIs(TOOL["strict"], True)

    def test_an_explicit_additional_properties_is_respected(self) -> None:
        tool = cc.build_tool(
            "t", "d", {"type": "object", "properties": {}, "additionalProperties": True}
        )
        self.assertIs(tool["input_schema"]["additionalProperties"], True)


class RequestShapeTests(unittest.TestCase):
    """Opus 5 rejects several parameters outright; none may be sent."""

    def _request(self) -> dict[str, Any]:
        client = _FakeClient([_tool_use({"ok": True})])
        with _patch(client):
            cc.generate_structured(system="S" * 40, messages=[{"role": "user", "content": "go"}], tool=TOOL)
        return client.calls[0]

    def test_no_sampling_parameters_are_sent(self) -> None:
        request = self._request()
        for banned in ("temperature", "top_p", "top_k"):
            self.assertNotIn(banned, request)

    def test_thinking_is_adaptive_with_no_token_budget(self) -> None:
        request = self._request()
        self.assertEqual(request["thinking"], {"type": "adaptive"})
        self.assertNotIn("budget_tokens", request["thinking"])

    def test_effort_travels_inside_output_config(self) -> None:
        """Effort is nested, not top-level — a top-level `effort` is ignored."""
        client = _FakeClient([_tool_use({"ok": True})])
        with _patch(client):
            cc.generate_structured(
                system="S", messages=[{"role": "user", "content": "go"}], tool=TOOL, effort="xhigh"
            )
        self.assertEqual(client.calls[0]["output_config"], {"effort": "xhigh"})
        self.assertNotIn("effort", client.calls[0])

    def test_the_settings_are_namespaced_to_this_project(self) -> None:
        """`CLAUDE_EFFORT` is already taken — Claude Code sets it.

        An un-namespaced name silently reconfigures the director for anyone
        running the worker from a shell that has one set, which is exactly how
        this was found.
        """
        source = (
            __import__("pathlib").Path(cc.__file__).read_text()
        )
        for setting in ("MODEL", "EFFORT", "MAX_TOKENS", "TIMEOUT_SEC", "SERVER_FALLBACK"):
            self.assertIn(f'os.getenv("EDITUBE_CLAUDE_{setting}"', source)
            self.assertNotIn(f'os.getenv("CLAUDE_{setting}"', source)

    def test_the_tool_call_is_forced(self) -> None:
        request = self._request()
        self.assertEqual(request["tool_choice"], {"type": "tool", "name": "submit_edit_plan"})

    def test_the_system_prompt_is_a_cache_breakpoint(self) -> None:
        request = self._request()
        self.assertEqual(request["system"][-1]["cache_control"], {"type": "ephemeral"})

    def test_caching_can_be_turned_off(self) -> None:
        client = _FakeClient([_tool_use({"ok": True})])
        with _patch(client):
            cc.generate_structured(
                system="S", messages=[{"role": "user", "content": "go"}], tool=TOOL,
                cache_system=False,
            )
        self.assertNotIn("cache_control", client.calls[0]["system"][-1])

    def test_it_always_streams(self) -> None:
        """A long planning turn on a non-streaming request risks a timeout."""
        client = _FakeClient([_tool_use({"ok": True})])
        with _patch(client):
            cc.generate_structured(system="S", messages=[{"role": "user", "content": "go"}], tool=TOOL)
        client.beta.messages.create.assert_not_called()


class RefusalTests(unittest.TestCase):
    def test_a_refusal_is_raised_before_content_is_read(self) -> None:
        """A refusal is an HTTP 200 whose content may be empty or partial.

        This message has no tool call at all; reading content first would raise
        the wrong error and hide why the request actually failed.
        """
        message = _Message(
            [], stop_reason="refusal", stop_details=_Block("refusal", category="cyber", explanation="no")
        )
        client = _FakeClient([message])
        with _patch(client), self.assertRaises(cc.ClaudeRefused) as caught:
            cc.generate_structured(system="S", messages=[{"role": "user", "content": "x"}], tool=TOOL)
        self.assertEqual(caught.exception.category, "cyber")

    def test_a_refusal_with_partial_content_still_refuses(self) -> None:
        message = _Message(
            [_Block("text", text="I can help with")],
            stop_reason="refusal",
            stop_details=_Block("refusal", category=None, explanation=None),
        )
        client = _FakeClient([message])
        with _patch(client), self.assertRaises(cc.ClaudeRefused):
            cc.generate_structured(system="S", messages=[{"role": "user", "content": "x"}], tool=TOOL)

    def test_a_truncated_turn_says_so(self) -> None:
        """`max_tokens` is the usual cause of a missing forced tool call."""
        client = _FakeClient([_Message([_Block("text", text="...")], stop_reason="max_tokens")])
        with _patch(client), self.assertRaises(cc.ClaudeMalformedOutput) as caught:
            cc.generate_structured(system="S", messages=[{"role": "user", "content": "x"}], tool=TOOL)
        self.assertIn("max_tokens", str(caught.exception))


class ServerFallbackTests(unittest.TestCase):
    def test_the_refusal_fallback_is_requested_by_default(self) -> None:
        client = _FakeClient([_tool_use({"ok": True})])
        with _patch(client):
            cc.generate_structured(system="S", messages=[{"role": "user", "content": "x"}], tool=TOOL)
        self.assertEqual(client.calls[0]["fallbacks"], "default")
        self.assertIn(cc._FALLBACK_BETA, client.calls[0]["betas"])

    def test_a_deployment_without_the_beta_degrades_instead_of_failing(self) -> None:
        """Losing the safety net is survivable; failing every run is not."""
        client = _FakeClient([_bad_request("fallbacks: unsupported beta"), _tool_use({"ok": True})])
        with _patch(client):
            result = cc.generate_structured(
                system="S", messages=[{"role": "user", "content": "x"}], tool=TOOL
            )
        self.assertEqual(result.data, {"ok": True})
        self.assertEqual(len(client.calls), 2)
        self.assertNotIn("fallbacks", client.calls[1])

    def test_an_unrelated_bad_request_is_not_retried(self) -> None:
        """Retrying a real request error would hide it behind a second failure."""
        client = _FakeClient([_bad_request("max_tokens must be positive")])
        from anthropic import BadRequestError

        with _patch(client), self.assertRaises(BadRequestError):
            cc.generate_structured(system="S", messages=[{"role": "user", "content": "x"}], tool=TOOL)
        self.assertEqual(len(client.calls), 1)


class UsageTests(unittest.TestCase):
    def test_usage_is_reported(self) -> None:
        message = _tool_use({"ok": True})
        message.usage = _Usage(input_tokens=120, output_tokens=40, cache_read_input_tokens=900)
        client = _FakeClient([message])
        with _patch(client):
            result = cc.generate_structured(
                system="S", messages=[{"role": "user", "content": "x"}], tool=TOOL
            )
        self.assertEqual(result.usage.input_tokens, 120)
        self.assertTrue(result.usage.cached)

    def test_a_run_with_no_cache_reads_reports_no_cache(self) -> None:
        """The signal that a multi-pass run is re-billing its prefix."""
        client = _FakeClient([_tool_use({"ok": True})])
        with _patch(client):
            result = cc.generate_structured(
                system="S", messages=[{"role": "user", "content": "x"}], tool=TOOL
            )
        self.assertFalse(result.usage.cached)


class ConversationTests(unittest.TestCase):
    def test_passes_share_one_history_so_the_prefix_stays_cached(self) -> None:
        client = _FakeClient([_tool_use({"ok": True}), _tool_use({"ok": False})])
        with _patch(client):
            convo = cc.Conversation("system prompt")
            convo.ask("pass A", tool=TOOL)
            convo.ask("pass B", tool=TOOL)

        second = client.calls[1]
        self.assertEqual(second["system"], client.calls[0]["system"])
        # user A, assistant A, tool_result A, user B
        self.assertEqual(len(second["messages"]), 4)
        self.assertEqual(second["messages"][0]["content"], "pass A")
        self.assertEqual(second["messages"][-1]["content"], "pass B")

    def test_the_assistant_turn_is_replayed_whole_including_thinking(self) -> None:
        """Thinking blocks must go back unchanged or the next turn is rejected."""
        thinking = _Block("thinking", thinking="considering the hook")
        message = _Message([thinking, _Block("tool_use", id="toolu_9", name="submit_edit_plan", input={"ok": True})])
        client = _FakeClient([message, _tool_use({"ok": True})])
        with _patch(client):
            convo = cc.Conversation("system prompt")
            convo.ask("pass A", tool=TOOL)
            convo.ask("pass B", tool=TOOL)

        assistant = client.calls[1]["messages"][1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertIn(thinking, assistant["content"])

    def test_a_forced_tool_call_is_acknowledged_so_the_turn_can_continue(self) -> None:
        client = _FakeClient([_tool_use({"ok": True}), _tool_use({"ok": True})])
        with _patch(client):
            convo = cc.Conversation("system prompt")
            convo.ask("pass A", tool=TOOL)
            convo.ask("pass B", tool=TOOL)

        result = client.calls[1]["messages"][2]
        self.assertEqual(result["role"], "user")
        self.assertEqual(result["content"][0]["type"], "tool_result")
        self.assertEqual(result["content"][0]["tool_use_id"], "toolu_1")

    def test_the_established_history_is_marked_for_caching(self) -> None:
        """Otherwise pass B re-bills the transcript, which is most of the run."""
        client = _FakeClient([_tool_use({"ok": True}), _tool_use({"ok": True})])
        with _patch(client):
            convo = cc.Conversation("system prompt")
            convo.ask("pass A", tool=TOOL)
            convo.ask("pass B", tool=TOOL)

        history = client.calls[1]["messages"][:-1]
        last_block = history[-1]["content"][-1]
        self.assertEqual(last_block["cache_control"], {"type": "ephemeral"})

    def test_the_new_question_is_not_itself_a_breakpoint(self) -> None:
        """It is the volatile part; caching it would cache nothing useful."""
        client = _FakeClient([_tool_use({"ok": True}), _tool_use({"ok": True})])
        with _patch(client):
            convo = cc.Conversation("system prompt")
            convo.ask("pass A", tool=TOOL)
            convo.ask("pass B", tool=TOOL)

        self.assertEqual(client.calls[1]["messages"][-1]["content"], "pass B")

    def test_breakpoints_do_not_accumulate_across_passes(self) -> None:
        """Four per request is a hard limit; one per pass would blow it."""
        client = _FakeClient([_tool_use({"ok": True}) for _ in range(5)])
        with _patch(client):
            convo = cc.Conversation("system prompt")
            for index in range(5):
                convo.ask(f"pass {index}", tool=TOOL)

        for call in client.calls:
            marked = sum(
                1
                for message in call["messages"]
                for block in (message["content"] if isinstance(message["content"], list) else [])
                if isinstance(block, dict) and "cache_control" in block
            )
            # Plus the system prompt's own breakpoint, so the request total
            # stays at two however many passes have run.
            self.assertLessEqual(marked, 1)

    def test_the_stored_history_stays_free_of_request_metadata(self) -> None:
        """It is a record of the conversation, not a request under construction."""
        client = _FakeClient([_tool_use({"ok": True}), _tool_use({"ok": True})])
        with _patch(client):
            convo = cc.Conversation("system prompt")
            convo.ask("pass A", tool=TOOL)
            convo.ask("pass B", tool=TOOL)

        serialised = repr(convo.messages)
        self.assertNotIn("cache_control", serialised)

    def test_usage_accumulates_across_passes(self) -> None:
        first, second = _tool_use({"ok": True}), _tool_use({"ok": True})
        first.usage = _Usage(output_tokens=100)
        second.usage = _Usage(output_tokens=50, cache_read_input_tokens=800)
        client = _FakeClient([first, second])
        with _patch(client):
            convo = cc.Conversation("system prompt")
            convo.ask("A", tool=TOOL)
            convo.ask("B", tool=TOOL)

        self.assertEqual(convo.usage.output_tokens, 150)
        self.assertEqual(convo.usage.cache_read_input_tokens, 800)

    def test_a_failed_pass_leaves_no_orphan_turn_in_the_history(self) -> None:
        """Otherwise a retry replays a user message the model never answered."""
        client = _FakeClient(
            [_Message([], stop_reason="refusal", stop_details=_Block("refusal", category="bio", explanation=None)),
             _tool_use({"ok": True})]
        )
        with _patch(client):
            convo = cc.Conversation("system prompt")
            with self.assertRaises(cc.ClaudeRefused):
                convo.ask("doomed", tool=TOOL)
            self.assertEqual(convo.messages, [])
            convo.ask("recovered", tool=TOOL)

        self.assertEqual(len(client.calls[1]["messages"]), 1)
        self.assertEqual(client.calls[1]["messages"][0]["content"], "recovered")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
