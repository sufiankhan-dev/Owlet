"""Phase 6 tests: the tool-calling loop with a deterministic fake model.

No Ollama call is made here; this isolates loop/validation/approval behavior.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent.llm import LLMError, LLMResult, ToolCallRequest
from agent.loop import AgentRun, MoodleAgent
from app.config import load_settings
from database.db import Database
from tools.base import StrictArgs, ToolContext, ToolError, ToolRegistry, ToolSpec


class EchoArgs(StrictArgs):
    text: str = ""


class DangerArgs(StrictArgs):
    pass


class FakeLLM:
    def __init__(self, results: list[LLMResult]) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    async def stream_chat(self, messages, **kwargs):
        self.calls.append({"messages": [dict(m) for m in messages], **kwargs})
        if not self.results:
            return LLMResult(content="No more fake results")
        return self.results.pop(0)


@pytest.fixture()
def base(tmp_path: Path):
    settings = replace(
        load_settings(),
        project_root=tmp_path,
        browser_profile_dir=tmp_path / "profile",
    )
    settings.ensure_directories()
    db = Database(tmp_path / "test.db")
    conversation_id = db.create_conversation("Test chat")
    db.add_message(conversation_id, "user", "Please use the echo tool.")
    registry = ToolRegistry()
    executed: list[str] = []

    async def echo(_ctx: ToolContext, args: EchoArgs) -> dict:
        executed.append(args.text)
        return {"ok": True, "message": "echoed", "text": args.text}

    async def danger(_ctx: ToolContext, _args: DangerArgs) -> dict:
        executed.append("danger")
        return {"ok": True, "message": "danger ran"}

    registry.register(
        ToolSpec(
            name="echo",
            description="Echo text",
            args_model=EchoArgs,
            handler=echo,
        )
    )
    registry.register(
        ToolSpec(
            name="danger",
            description="A consequential action",
            args_model=DangerArgs,
            handler=danger,
            read_only=False,
            requires_approval=True,
        )
    )

    yield settings, db, conversation_id, registry, executed
    db.close()


def _run(agent: MoodleAgent, run: AgentRun, events: list, approve=None):
    async def approve_yes(_action, _details):
        return True

    def emit(kind, message, data):
        events.append((kind, message, data))

    return asyncio.run(agent.run(run=run, emit=emit, approve=approve or approve_yes))


def test_tool_call_then_final_answer(base) -> None:
    settings, db, conversation_id, registry, executed = base
    fake = FakeLLM(
        [
            LLMResult(tool_calls=[ToolCallRequest(name="echo", arguments={"text": "hello"})]),
            LLMResult(content="All done."),
        ]
    )
    agent = MoodleAgent(
        settings=settings, db=db, browser=None, registry=registry, llm=fake
    )
    events: list = []
    outcome = _run(
        agent,
        AgentRun(conversation_id=conversation_id, user_message="Please use the echo tool."),
        events,
    )

    assert outcome.content == "All done."
    assert outcome.tool_calls == 1
    assert executed == ["hello"]

    second_messages = fake.calls[1]["messages"]
    assert second_messages[-2]["role"] == "assistant"
    assert second_messages[-2]["tool_calls"][0]["function"]["name"] == "echo"
    assert second_messages[-1]["role"] == "tool"
    assert second_messages[-1]["tool_name"] == "echo"
    assert json.loads(second_messages[-1]["content"])["ok"] is True

    stored = db.get_messages(conversation_id)
    assert stored[-1]["content"] == "All done."
    assert any(kind == "done" for kind, _msg, _data in events)


def test_invalid_arguments_are_reported_to_model(base) -> None:
    settings, db, conversation_id, registry, executed = base
    fake = FakeLLM(
        [
            LLMResult(tool_calls=[ToolCallRequest(name="echo", arguments={"text": ["not", "a", "string"]})]),
            LLMResult(content="I will retry."),
        ]
    )
    agent = MoodleAgent(
        settings=settings, db=db, browser=None, registry=registry, llm=fake
    )
    outcome = _run(
        agent,
        AgentRun(conversation_id=conversation_id, user_message="Please use the echo tool."),
        [],
    )

    assert outcome.content == "I will retry."
    assert executed == []
    tool_message = fake.calls[1]["messages"][-1]
    assert json.loads(tool_message["content"])["ok"] is False


def test_approval_denial_blocks_tool(base) -> None:
    settings, db, conversation_id, registry, executed = base
    fake = FakeLLM(
        [
            LLMResult(tool_calls=[ToolCallRequest(name="danger", arguments={})]),
            LLMResult(content="The user said no."),
        ]
    )
    agent = MoodleAgent(
        settings=settings, db=db, browser=None, registry=registry, llm=fake
    )

    async def approve_no(_action, _details):
        return False

    outcome = _run(
        agent,
        AgentRun(conversation_id=conversation_id, user_message="Do the dangerous thing."),
        [],
        approve=approve_no,
    )

    assert outcome.content == "The user said no."
    assert executed == []
    tool_payload = json.loads(fake.calls[1]["messages"][-1]["content"])
    assert tool_payload["ok"] is False
    assert "denied" in tool_payload["error"].lower()


def test_unknown_tool_is_reported(base) -> None:
    settings, db, conversation_id, registry, executed = base
    fake = FakeLLM(
        [
            LLMResult(tool_calls=[ToolCallRequest(name="rm_rf", arguments={})]),
            LLMResult(content="I cannot do that."),
        ]
    )
    agent = MoodleAgent(
        settings=settings, db=db, browser=None, registry=registry, llm=fake
    )
    outcome = _run(
        agent,
        AgentRun(conversation_id=conversation_id, user_message="Delete everything."),
        [],
    )

    assert outcome.content == "I cannot do that."
    payload = json.loads(fake.calls[1]["messages"][-1]["content"])
    assert payload["ok"] is False
    assert "Unknown tool" in payload["error"]


def test_cancelled_result_is_not_saved_as_answer(base) -> None:
    settings, db, conversation_id, registry, _executed = base
    fake = FakeLLM([LLMResult(content="partial", cancelled=True)])
    agent = MoodleAgent(
        settings=settings, db=db, browser=None, registry=registry, llm=fake
    )
    outcome = _run(
        agent,
        AgentRun(conversation_id=conversation_id, user_message="Please use the echo tool."),
        [],
    )
    assert outcome.cancelled is True
    assert outcome.content == ""


def test_model_failure_emits_one_error_event(base) -> None:
    settings, db, conversation_id, registry, _executed = base

    class MissingModelLLM:
        async def stream_chat(self, messages, **kwargs):
            raise LLMError(
                "Model 'qwen3.5:9b' is not installed. Run: ollama pull qwen3.5:9b",
                kind="model_missing",
            )

    agent = MoodleAgent(
        settings=settings, db=db, browser=None, registry=registry, llm=MissingModelLLM()
    )
    events: list = []
    outcome = _run(
        agent,
        AgentRun(conversation_id=conversation_id, user_message="Look at this."),
        events,
    )

    assert outcome.error and "qwen3.5:9b" in outcome.error
    errors = [event for event in events if event[0] == "error"]
    assert len(errors) == 1, "the error must not be emitted twice"
    assert not any(kind == "done" for kind, _msg, _data in events)
    # The failed turn is not stored as an assistant reply.
    assert [row["role"] for row in db.get_messages(conversation_id)] == ["user"]



def test_draft_mode_nudges_a_model_that_stops_before_creating_files(base) -> None:
    settings, db, conversation_id, registry, _executed = base
    fake = FakeLLM(
        [
            LLMResult(content="I will retry reading the files."),
            LLMResult(content="Here is my plan."),
            LLMResult(content="Thinking."),
            LLMResult(content="Still nothing."),
        ]
    )
    agent = MoodleAgent(settings=settings, db=db, browser=None, registry=registry, llm=fake)
    events: list = []
    outcome = _run(
        agent,
        AgentRun(conversation_id=conversation_id, user_message="Draft it", mode="draft"),
        events,
    )

    # Three nudges, then the fourth empty-handed answer ends the run.
    assert len(fake.calls) == 4
    assert outcome.content == "Still nothing."
    nudge = fake.calls[1]["messages"][-1]
    assert nudge["role"] == "user"
    assert "not created any deliverable" in nudge["content"]
    assert fake.calls[1]["messages"][-2] == {
        "role": "assistant",
        "content": "I will retry reading the files.",
    }


class StreamingFakeLLM:
    """Streams through on_delta so the event ids can be checked, no network."""

    def __init__(
        self,
        results: list[tuple[list[str], LLMResult]],
    ) -> None:
        self.results = list(results)
        self.calls: list[dict] = []

    async def stream_chat(self, messages, **kwargs):
        self.calls.append({"messages": [dict(m) for m in messages], **kwargs})
        chunks, result = self.results.pop(0)
        for chunk in chunks:
            kwargs["on_delta"](chunk)
        return result


def test_streamed_round_shares_one_message_id(base) -> None:
    settings, db, conversation_id, registry, _executed = base
    fake = StreamingFakeLLM(
        [(["Hel", "lo"], LLMResult(content="Hello"))]
    )
    agent = MoodleAgent(settings=settings, db=db, browser=None, registry=registry, llm=fake)
    events: list = []
    outcome = _run(
        agent,
        AgentRun(conversation_id=conversation_id, user_message="Say hello."),
        events,
    )

    deltas = [data for kind, _msg, data in events if kind == "assistant_delta"]
    assert "".join(message for kind, message, _data in events if kind == "assistant_delta") == "Hello"
    assert len(deltas) == 2
    delta_ids = {data["message_id"] for data in deltas}
    assert len(delta_ids) == 1, "every delta of a round must share one id"
    assert all(data["step"] == 1 for data in deltas)

    finals = [(msg, data) for kind, msg, data in events if kind == "assistant_message"]
    assert len(finals) == 1, "one model round emits exactly one assistant message"
    assert finals[0][0] == "Hello"
    assert finals[0][1]["message_id"] == delta_ids.pop()
    assert finals[0][1]["final"] is True

    done = [data for kind, _msg, data in events if kind == "done"]
    assert len(done) == 1
    assert done[0]["message_id"] == outcome.message_id
    assert outcome.message_id == finals[0][1]["message_id"], "done finalises the streamed bubble"


def test_tool_rounds_use_one_message_id_each(base) -> None:
    settings, db, conversation_id, registry, executed = base
    fake = StreamingFakeLLM(
        [
            (
                ["Let me ", "check."],
                LLMResult(
                    content="Let me check.",
                    tool_calls=[ToolCallRequest(name="echo", arguments={"text": "hi"})],
                ),
            ),
            (["All ", "done."], LLMResult(content="All done.")),
        ]
    )
    agent = MoodleAgent(settings=settings, db=db, browser=None, registry=registry, llm=fake)
    events: list = []
    outcome = _run(
        agent,
        AgentRun(conversation_id=conversation_id, user_message="Please use the echo tool."),
        events,
    )

    finals = [(msg, data) for kind, msg, data in events if kind == "assistant_message"]
    assert [msg for msg, _data in finals] == ["Let me check.", "All done."]
    assert finals[0][1]["step"] == 1
    assert finals[0][1]["final"] is False, "a round that calls a tool is not final"
    assert finals[1][1]["step"] == 2
    assert finals[1][1]["final"] is True
    assert finals[0][1]["message_id"] != finals[1][1]["message_id"]

    deltas = [data for kind, _msg, data in events if kind == "assistant_delta"]
    assert {data["message_id"] for data in deltas} == {
        finals[0][1]["message_id"],
        finals[1][1]["message_id"],
    }
    done = [data for kind, _msg, data in events if kind == "done"][0]
    assert done["message_id"] == finals[1][1]["message_id"] == outcome.message_id
    assert executed == ["hi"]


def test_cancelled_round_stops_with_the_streamed_id(base) -> None:
    settings, db, conversation_id, registry, _executed = base
    fake = StreamingFakeLLM([(["partial"], LLMResult(content="partial", cancelled=True))])
    agent = MoodleAgent(settings=settings, db=db, browser=None, registry=registry, llm=fake)
    events: list = []
    outcome = _run(
        agent,
        AgentRun(conversation_id=conversation_id, user_message="Please use the echo tool."),
        events,
    )

    assert outcome.cancelled is True
    stopped = [data for kind, _msg, data in events if kind == "stopped"]
    assert len(stopped) == 1
    deltas = [data for kind, _msg, data in events if kind == "assistant_delta"]
    assert stopped[0]["message_id"] == deltas[0]["message_id"]
    assert stopped[0]["message_id"] == outcome.message_id
    assert not any(kind in ("assistant_message", "done") for kind, _m, _d in events)


def test_chat_mode_is_never_nudged(base) -> None:
    settings, db, conversation_id, registry, _executed = base
    fake = FakeLLM([LLMResult(content="Just an answer.")])
    agent = MoodleAgent(settings=settings, db=db, browser=None, registry=registry, llm=fake)
    outcome = _run(
        agent,
        AgentRun(conversation_id=conversation_id, user_message="Please use the echo tool."),
        [],
    )
    assert len(fake.calls) == 1
    assert outcome.content == "Just an answer."


def test_draft_mode_continues_after_an_empty_reply_mid_draft(base) -> None:
    settings, db, conversation_id, registry, _executed = base

    class FileArgs(StrictArgs):
        filename: str = ""

    async def create_code_file(_ctx: ToolContext, args: FileArgs) -> dict:
        return {"ok": True, "message": f"Created {args.filename}"}

    registry.register(
        ToolSpec(name="create_code_file", description="Write", args_model=FileArgs, handler=create_code_file)
    )
    fake = FakeLLM(
        [
            LLMResult(tool_calls=[ToolCallRequest(name="create_code_file", arguments={"filename": "a.py"})]),
            LLMResult(content=""),
            LLMResult(tool_calls=[ToolCallRequest(name="create_code_file", arguments={"filename": "b.py"})]),
            LLMResult(content="## Deliverables\n- a.py\n- b.py"),
        ]
    )
    agent = MoodleAgent(settings=settings, db=db, browser=None, registry=registry, llm=fake)
    events: list = []
    outcome = _run(
        agent,
        AgentRun(conversation_id=conversation_id, user_message="Draft it", mode="draft"),
        events,
    )

    assert outcome.error is None
    assert outcome.content.startswith("## Deliverables")
    assert "created 1 file(s) so far" in fake.calls[2]["messages"][-1]["content"]
    assert [kind for kind, _m, _d in events].count("error") == 0
