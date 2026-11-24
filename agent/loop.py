"""The tool-calling agent loop.

One user turn may involve several model/tool rounds. Every tool call is
validated, classified, executed, and reported. The loop is bounded and honours
the user's Stop button between every step.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from pydantic import ValidationError

from agent.llm import LLMError, LLMResult, OllamaService, ToolCallRequest
from agent.prompts import build_draft_prompt, build_system_prompt
from app.chat_images import attach_prompt_images
from app.config import Settings
from app.security import truncate_text
from browser.session import BrowserSession
from database.db import Database
from tools.base import ToolCancelled, ToolContext, ToolError, ToolRegistry

logger = logging.getLogger(__name__)

_FILE_TOOLS = frozenset({"create_word_document", "create_pdf", "create_code_file"})
_DRAFT_NUDGES = 3
_DRAFT_NUDGE = (
    "You have not created any deliverable files yet. Continue now by calling the tools: "
    "read the listed files if you have not, then create each deliverable with "
    "create_code_file, create_word_document, or create_pdf. Do not describe what you "
    "will do; call the tool."
)
_DRAFT_CONTINUE = (
    "Your last reply was empty. You have created {count} file(s) so far. If any task or "
    "deliverable from the brief is still missing, create it now with a tool call. When "
    "every deliverable exists, reply with the review checklist (Deliverables, "
    "Requirements, Check before submitting)."
)

EmitFn = Callable[[str, str, dict[str, Any] | None], None]
ApproveFn = Callable[[str, dict[str, Any]], Awaitable[bool]]


@dataclass
class AgentOutcome:
    content: str = ""
    cancelled: bool = False
    error: str | None = None
    steps: int = 0
    tool_calls: int = 0
    model: str = ""
    # Stable id of the last assistant message this run streamed. The dashboard
    # uses it to finalise exactly one bubble instead of appending a second copy.
    message_id: str = ""


@dataclass
class AgentRun:
    """Mutable per-turn state shared with the API layer."""

    conversation_id: int
    user_message: str
    # "chat" for a normal turn; "draft" adds the draft-mode rules to the prompt.
    mode: str = "chat"
    # Tool context state to start from, e.g. the assignment a draft run is for.
    seed_state: dict[str, Any] = field(default_factory=dict)
    draft_assignment_id: int | None = None
    stop_requested: bool = False
    outcome: AgentOutcome = field(default_factory=AgentOutcome)

    def request_stop(self) -> None:
        self.stop_requested = True


class MoodleAgent:
    def __init__(
        self,
        *,
        settings: Settings,
        db: Database,
        browser: BrowserSession,
        registry: ToolRegistry,
        llm: OllamaService,
    ) -> None:
        self.settings = settings
        self.db = db
        self.browser = browser
        self.registry = registry
        self.llm = llm

    async def run(
        self,
        *,
        run: AgentRun,
        emit: EmitFn,
        approve: ApproveFn,
    ) -> AgentOutcome:
        ctx = ToolContext(
            settings=self.settings,
            db=self.db,
            browser=self.browser,
            jail=self._jail(),
            emit=emit,
            approve=approve,
            should_stop=lambda: run.stop_requested,
        )
        ctx.state.update(run.seed_state)

        history = list(
            self.db.get_recent_messages_for_prompt(run.conversation_id, limit=12)
        )
        if (
            not history
            or history[-1].get("role") != "user"
            or history[-1].get("content") != run.user_message
        ):
            history.append({"role": "user", "content": run.user_message})

        system_prompt = build_system_prompt(
            tool_names=self.registry.names(),
            workspace_root=str(self.settings.workspace_dir),
            model=self.settings.ollama_model,
        )
        if run.mode == "draft":
            system_prompt += build_draft_prompt()
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for message in history[-10:]:
            content = str(message.get("content") or "").strip()
            refs = message.get("images")
            if not content and not refs:
                continue
            entry: dict[str, Any] = {
                "role": message["role"],
                "content": truncate_text(content, 6000) if content else "",
            }
            if message["role"] == "user" and isinstance(refs, list) and refs:
                entry["images"] = refs
            messages.append(entry)

        # Vision input is re-encoded from disk each turn (the DB stores only a
        # reference), bounded so a long chat cannot swamp the context window.
        attached_images = attach_prompt_images(
            self.settings, run.conversation_id, messages
        )
        if attached_images:
            logger.info("Attached %s image(s) to the prompt", attached_images)

        emit(
            "status",
            f"Thinking with {self.settings.ollama_model}",
            {"model": self.settings.ollama_model},
        )
        outcome = AgentOutcome(model=self.settings.ollama_model)
        files_created = 0
        nudges = 0
        consecutive_failures = 0
        blocked_repeats = 0

        for step in range(1, self.settings.max_agent_iterations + 1):
            if run.stop_requested:
                outcome.cancelled = True
                return await self._finish(run, outcome, emit, "Stopped by user")

            outcome.steps = step
            ctx.log("agent", f"Model round {step}", {"model": self.settings.ollama_model})

            # One stable id per model round. Every delta and the final message
            # event carry it, so the dashboard can stream into exactly one
            # bubble and never append the same answer a second time.
            message_id = uuid.uuid4().hex[:12]
            outcome.message_id = message_id

            try:
                result = await self.llm.stream_chat(
                    messages,
                    tools=self.registry.schemas(),
                    on_delta=lambda delta, mid=message_id, round_no=step: emit(
                        "assistant_delta",
                        delta,
                        {"message_id": mid, "step": round_no},
                    ),
                    should_stop=lambda: run.stop_requested,
                )
            except LLMError as exc:
                outcome.error = exc.message
                # _finish emits the single "error" event; logging it here too
                # would duplicate the bubble and the activity row in the UI.
                return await self._finish(run, outcome, emit, exc.message)
            except Exception as exc:
                logger.exception("Unexpected model failure")
                outcome.error = f"Unexpected model failure: {exc}"
                return await self._finish(run, outcome, emit, outcome.error)

            if result.cancelled or run.stop_requested:
                outcome.cancelled = True
                return await self._finish(run, outcome, emit, "Stopped by user")

            if result.content:
                emit(
                    "assistant_message",
                    result.content,
                    {
                        "message_id": message_id,
                        "step": step,
                        "final": not result.tool_calls,
                    },
                )

            if (
                not result.tool_calls
                and run.mode == "draft"
                and (files_created == 0 or not result.content.strip())
                and nudges < _DRAFT_NUDGES
                and step < self.settings.max_agent_iterations
            ):
                # Small models often announce "I will retry" and then stop, or
                # return nothing halfway through. A draft is only useful once its
                # deliverables exist, so ask it to carry on instead of ending.
                nudges += 1
                if result.content:
                    messages.append({"role": "assistant", "content": result.content})
                if files_created == 0:
                    nudge = _DRAFT_NUDGE
                    reason = "No draft files yet; asking the model to continue"
                else:
                    nudge = _DRAFT_CONTINUE.format(count=files_created)
                    reason = "Empty model reply mid-draft; asking it to continue"
                messages.append({"role": "user", "content": nudge})
                ctx.log("agent", reason, None)
                continue

            if not result.tool_calls:
                outcome.content = result.content.strip()
                if not outcome.content:
                    # Reported once, by _finish.
                    outcome.error = (
                        "The model returned an empty response. Try rephrasing, or check that "
                        f"{self.settings.ollama_model} is installed and running."
                    )
                return await self._finish(run, outcome, emit, outcome.content)

            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": result.content or "",
                "tool_calls": [
                    {
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        }
                    }
                    for call in result.tool_calls
                ],
            }
            messages.append(assistant_message)

            for call in result.tool_calls:
                if run.stop_requested:
                    outcome.cancelled = True
                    return await self._finish(run, outcome, emit, "Stopped by user")
                outcome.tool_calls += 1
                tool_payload, tool_ok, blocked = await self._execute_tool(
                    ctx, call, emit, run
                )
                if call.name in _FILE_TOOLS and tool_ok:
                    files_created += 1
                messages.append(
                    {
                        "role": "tool",
                        "tool_name": call.name,
                        "content": tool_payload,
                    }
                )
                consecutive_failures = 0 if tool_ok else consecutive_failures + 1
                blocked_repeats = blocked_repeats + 1 if blocked else 0
                if blocked_repeats >= 2:
                    outcome.error = (
                        "Stopped because the model kept repeating a tool call that had "
                        "already failed twice. The real error is in the earlier tool result."
                    )
                    return await self._finish(run, outcome, emit, outcome.content)
                if consecutive_failures >= 8:
                    outcome.error = (
                        "Stopped after 8 consecutive tool failures to avoid a loop. "
                        "Check the browser login and the Moodle URLs, then try a smaller step."
                    )
                    return await self._finish(run, outcome, emit, outcome.content)

        outcome.error = (
            f"Stopped after {self.settings.max_agent_iterations} tool rounds to avoid a loop. "
            "Ask for a smaller next step."
        )
        return await self._finish(run, outcome, emit, outcome.content)

    def _jail(self):
        from app.security import PathJail

        return PathJail(
            {
                "workspace": self.settings.workspace_dir,
                "generated": self.settings.generated_dir,
            }
        )

    async def _execute_tool(
        self,
        ctx: ToolContext,
        call: ToolCallRequest,
        emit: EmitFn,
        run: AgentRun,
    ) -> tuple[str, bool, bool]:
        """Run one tool call.

        Returns ``(serialized_payload, ok, repeat_blocked)``. A call that has
        already failed twice with the exact same arguments is not run again: the
        model gets a clear "change approach" message instead of burning every
        remaining round on the same broken request.
        """

        try:
            signature = call.name + "|" + json.dumps(
                call.arguments, sort_keys=True, default=str
            )
        except Exception:
            signature = call.name
        failures: dict[str, int] = ctx.state.setdefault("tool_failure_signatures", {})
        if failures.get(signature, 0) >= 2:
            payload = {
                "ok": False,
                "error": (
                    "This exact tool call already failed twice in this run. Do not call it "
                    "again. Read the earlier error, change the arguments or the approach, "
                    "and if it still cannot work, tell the user what is blocking it."
                ),
                "repeat_blocked": True,
            }
            emit("tool_result", payload["error"], payload)
            ctx.log("tool", payload["error"], {"tool": call.name, "ok": False})
            return self._serialize(payload), False, True

        def finish(payload: dict[str, Any], message: str) -> tuple[str, bool, bool]:
            ok = bool(payload.get("ok"))
            if ok:
                failures.pop(signature, None)
            else:
                failures[signature] = failures.get(signature, 0) + 1
            emit("tool_result", message, payload)
            ctx.log("tool", message, {"tool": call.name, "ok": ok})
            return self._serialize(payload), ok, False

        spec = self.registry.get(call.name)
        if spec is None:
            payload = {
                "ok": False,
                "error": f"Unknown tool {call.name!r}. Available tools: {self.registry.names()}",
            }
            return finish(payload, f"Unknown tool: {call.name}")

        emit(
            "tool_start",
            f"Using {call.name}",
            {"tool": call.name, "arguments": call.arguments},
        )
        ctx.log("tool", f"Using {call.name}", {"arguments": call.arguments})

        try:
            args = spec.args_model.model_validate(call.arguments)
        except ValidationError as exc:
            payload = {
                "ok": False,
                "error": "Invalid arguments for this tool.",
                "details": exc.errors(include_url=False),
            }
            return finish(payload, f"Invalid arguments for {call.name}")

        try:
            ctx.check_cancel()
            if spec.requires_approval:
                approved = await ctx.require_approval(
                    spec.name, {"arguments": call.arguments}
                )
                if not approved:
                    raise ToolError("The user denied this action.")
            data = await spec.handler(ctx, args)
            payload = {"ok": bool(data.get("ok", True)), **data}
            message = str(payload.get("message") or f"{call.name} completed")
        except ToolCancelled:
            raise
        except ToolError as exc:
            payload = {"ok": False, "error": exc.message, **exc.data}
            message = exc.message
        except Exception as exc:
            logger.exception("Tool %s failed", call.name)
            payload = {
                "ok": False,
                "error": f"{call.name} failed unexpectedly: {exc}",
            }
            message = payload["error"]

        return finish(payload, message)

    def _serialize(self, payload: dict[str, Any]) -> str:
        text = json.dumps(payload, ensure_ascii=False, default=str)
        limit = self.settings.max_tool_result_chars
        if len(text) <= limit:
            return text
        preview = truncate_text(text, max(0, limit - 300), marker="")
        return json.dumps(
            {
                "ok": payload.get("ok", False),
                "truncated": True,
                "message": payload.get("message") or payload.get("error") or "",
                "preview": preview,
            },
            ensure_ascii=False,
        )

    async def _finish(
        self,
        run: AgentRun,
        outcome: AgentOutcome,
        emit: EmitFn,
        text: str,
    ) -> AgentOutcome:
        run.outcome = outcome
        if outcome.content:
            try:
                self.db.add_message(run.conversation_id, "assistant", outcome.content)
            except Exception:
                logger.exception("Could not save assistant message")
        if outcome.cancelled:
            emit(
                "stopped",
                "Agent stopped",
                {"steps": outcome.steps, "message_id": outcome.message_id},
            )
        elif outcome.error:
            emit("error", outcome.error, {"message_id": outcome.message_id})
        else:
            emit(
                "done",
                "Response complete",
                {
                    "steps": outcome.steps,
                    "tool_calls": outcome.tool_calls,
                    "model": outcome.model,
                    "message_id": outcome.message_id,
                },
            )
        return outcome
