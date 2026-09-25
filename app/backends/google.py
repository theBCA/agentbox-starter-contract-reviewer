"""Google ADK backend, routed through OpenRouter.

There is no Gemini key here. The agent is built with Google's ADK and reaches
its model through OpenRouter: inside AgentBox that is `LiteLlm` pointed at
SecureProxy's `/v1`, which forwards to OpenRouter under the app's own virtual
key (`AGENTBOX_MODEL_PROVIDER=openrouter` is how the platform says so); outside
AgentBox it is a direct OpenRouter call with `OPENROUTER_API_KEY`. The page's
"Built with" pill says exactly that -- Google ADK, via OpenRouter.

Tools: the agent's toolkit (`app/agent_tools.py`) is handed to ADK as
`FunctionTool`s. ADK derives a tool's declaration from a Python callable's
signature and annotations, and the toolkit's specs arrive as JSON schemas from
the bridge, so each spec becomes a synthetic function carrying exactly the
signature its schema describes. The model sees the same names, descriptions
and parameters the Claude starter's model sees.
"""

from __future__ import annotations

import inspect
import os
from typing import Any

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool
from google.genai import types

from app.agent_tools import TOOL_GUIDANCE, Toolkit, ToolOutcome, ToolSpec, build_toolkit
from app.backends._secureproxy import configure_secureproxy
from app.backends._shared import (
    build_system_instructions,
    build_user_text,
    get_current_date_iso,
)

_SESSION_ID = "contract-reviewer-session"
_APP_NAME = "contract-reviewer"

_JSON_TYPES: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _build_model():
    proxy = configure_secureproxy("google")
    openrouter_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    gemini_key = (
        os.environ.get("GOOGLE_API_KEY", "").strip()
        or os.environ.get("GEMINI_API_KEY", "").strip()
    )
    model_name = os.environ.get("GOOGLE_MODEL", "").strip()

    # AgentBox tells a managed app WHERE its calls go via AGENTBOX_MODEL_PROVIDER
    # (a non-secret signal, distinct from the SDK-native GOOGLE_API_KEY name
    # AgentBox always injects). Native Gemini speaks a REST dialect OpenRouter
    # does not serve, so a google-adk app routed to OpenRouter must go through
    # LiteLlm's OpenAI-compatible client -- pointed at SecureProxy's own /v1
    # endpoint, which forwards to OpenRouter under the app's virtual key.
    # Without the signal this app raised `404 Not Found` on every call.
    provider = os.environ.get("AGENTBOX_MODEL_PROVIDER", "").strip().lower()
    if provider == "openrouter":
        from google.adk.models.lite_llm import LiteLlm

        proxy_url = os.environ.get("KOBIL_SECUREPROXY_URL", "").strip()
        virtual_key = os.environ.get("KOBIL_SECUREPROXY_API_KEY", "").strip()
        if proxy_url and virtual_key:
            return LiteLlm(
                model=f"openai/{model_name or 'google/gemini-2.5-flash'}",
                api_base=f"{proxy_url.rstrip('/')}/v1",
                api_key=virtual_key,
            )
        return LiteLlm(model=model_name or "openrouter/google/gemini-2.5-flash")

    if openrouter_key and proxy is None:
        from google.adk.models.lite_llm import LiteLlm

        return LiteLlm(model=model_name or "openrouter/google/gemini-2.5-flash")

    if gemini_key:
        from google.adk.models.google_llm import Gemini

        os.environ["GOOGLE_API_KEY"] = gemini_key
        return Gemini(
            model=model_name or "gemini-2.5-flash",
            base_url=os.environ.get("GOOGLE_API_BASE") or None,
        )

    raise RuntimeError(
        "google-adk backend requires OPENROUTER_API_KEY (or a Gemini key) when it "
        "runs outside AgentBox"
    )


def _function_for(spec: ToolSpec, toolkit: Toolkit):
    """A callable ADK can declare and call, shaped after the spec's schema.

    ADK reads `inspect.signature` and the annotations, so a synthetic function
    carries both: one keyword-only parameter per schema property, required
    ones without a default. The body hands the call to the toolkit, which is
    where every verdict is decided and recorded.
    """
    properties = spec.input_schema.get("properties") or {}
    required = set(spec.input_schema.get("required") or [])
    parameters = []
    annotations: dict[str, Any] = {}
    for name, schema in properties.items():
        py_type = _JSON_TYPES.get(str((schema or {}).get("type") or "string"), str)
        default = inspect.Parameter.empty if name in required else None
        parameters.append(
            inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=py_type,
            )
        )
        annotations[name] = py_type

    async def _call(**kwargs):
        arguments = {key: value for key, value in kwargs.items() if value is not None}
        text, _outcome = await toolkit.run(spec.name, arguments)
        return {"result": text}

    _call.__name__ = spec.name
    _call.__qualname__ = spec.name
    _call.__doc__ = spec.description
    _call.__signature__ = inspect.Signature(parameters)  # type: ignore[attr-defined]
    annotations["return"] = dict
    _call.__annotations__ = annotations
    return FunctionTool(func=_call)


async def _prepare(user_input: str, question: str | None):
    """Build the runner, the toolkit and the opening message -- shared by
    `process` and `stream`, so the two cannot run different agents."""
    import asyncio

    toolkit = await asyncio.to_thread(build_toolkit)
    instructions = (
        f"{await build_system_instructions()}\n\n"
        f"{TOOL_GUIDANCE}\n\n"
        f"Today's date in ISO 8601 format is {get_current_date_iso()}."
    )
    agent = LlmAgent(
        name="contract_reviewer",
        model=_build_model(),
        instruction=instructions,
        tools=[_function_for(spec, toolkit) for spec in toolkit.specs],
    )
    session_service = InMemorySessionService()
    runner = Runner(agent=agent, app_name=_APP_NAME, session_service=session_service)
    await session_service.create_session(
        app_name=_APP_NAME, user_id=_SESSION_ID, session_id=_SESSION_ID
    )
    new_message = types.Content(
        role="user", parts=[types.Part(text=build_user_text(user_input, question))]
    )
    return runner, new_message, toolkit


def _parts(event) -> list:
    content = getattr(event, "content", None)
    parts = getattr(content, "parts", None) if content else None
    return list(parts or [])


def _event_text(event) -> str:
    return "".join(part.text for part in _parts(event) if getattr(part, "text", None))


def _is_final_response(event) -> bool:
    marker = getattr(event, "is_final_response", None)
    if callable(marker):
        try:
            return bool(marker())
        except Exception:  # noqa: BLE001 - a probe must never break the run
            return False
    return bool(marker)


def events_of(event, toolkit: Toolkit) -> list[dict]:
    """The console events one ADK event amounts to: a `tool` per function
    call, a `tool_result` per function response (carrying the toolkit's own
    outcome for that call), and nothing for text -- text is handled by the
    stream, which has to tell partial from final."""
    out: list[dict] = []
    for part in _parts(event):
        call = getattr(part, "function_call", None)
        if call is not None and getattr(call, "name", None):
            out.append(
                {
                    "type": "tool",
                    "id": str(getattr(call, "id", "") or ""),
                    "name": str(call.name),
                    "input": dict(getattr(call, "args", None) or {}),
                }
            )
            continue
        response = getattr(part, "function_response", None)
        if response is not None and getattr(response, "name", None):
            name = str(response.name)
            outcome = toolkit.take_outcome(name)
            if outcome is None:
                payload = getattr(response, "response", None) or {}
                outcome = ToolOutcome(name, "tool_ok", True, str(payload)[:2000])
            out.append(
                {
                    "type": "tool_result",
                    "id": str(getattr(response, "id", "") or ""),
                    **outcome.as_event(),
                }
            )
    return out


async def stream(user_input: str, question: str | None):
    """Yield the agent's turns as they happen, for POST /process/stream.

    ADK emits incremental text as events with `partial=True` and then repeats
    the whole answer in a final, non-partial event. Partial events become
    `token`s and the final one the single `result`. When the configured model
    does not stream, no partial event ever arrives -- hence `saw_streaming`,
    which makes the first non-partial event the token stream instead of
    dropping it. A model-level error arrives as data on the event and is
    raised, so the app's 403 mapping sees it.
    """
    runner, new_message, toolkit = await _prepare(user_input, question)

    saw_streaming = False
    final_text = ""
    async for event in runner.run_async(
        user_id=_SESSION_ID, session_id=_SESSION_ID, new_message=new_message
    ):
        if getattr(event, "error_code", None) or getattr(event, "error_message", None):
            raise RuntimeError(
                f"{getattr(event, 'error_code', '') or 'error'}: {getattr(event, 'error_message', '') or ''}"
            )
        for item in events_of(event, toolkit):
            yield item

        text = _event_text(event)
        if not text:
            continue
        if getattr(event, "partial", False):
            saw_streaming = True
            yield {"type": "token", "text": text}
            continue
        if not saw_streaming:
            yield {"type": "token", "text": text}
        if not saw_streaming or _is_final_response(event):
            final_text = text

    if final_text:
        yield {"type": "result", "text": final_text}


async def process(user_input: str, question: str | None) -> dict:
    final, said, tool_calls = "", [], []
    async for event in stream(user_input, question):
        kind = event.get("type")
        if kind == "result":
            final = str(event.get("text") or "")
        elif kind == "token":
            said.append(str(event.get("text") or ""))
        elif kind == "tool_result":
            tool_calls.append(
                {
                    k: event.get(k)
                    for k in ("name", "signal", "ok", "detail", "approval_id")
                }
            )
    answer = final or "".join(said)
    if not answer.strip():
        raise RuntimeError("the agent returned no text")
    return {"answer": answer, "tool_calls": tool_calls}
