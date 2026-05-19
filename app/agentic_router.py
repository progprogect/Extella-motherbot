"""
Agentic Router — OpenAI function-calling loop over Extella experts.

Flow per user message:
  1. Use concept_text (Agent Execution Guide) as system prompt
  2. Build JSON Schema tools from expert signatures
  3. Run OpenAI chat.completions loop (max MAX_ITERATIONS)
  4. On tool_call: run_expert via Extella with user's token+target
  5. On key error in result: return {"status": "needs_key", "key_name": "..."}
  6. On finish_reason=stop: return {"status": "ok", "text": "..."}

Only concepts containing "FLOW:" or "EXPERTS:" blocks trigger this path.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

from .extella_client import ExtellaClient

if TYPE_CHECKING:
    from .database import Bot, BotExpert

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 6
MAX_TOOL_RETRIES = 2  # max retries per individual tool before giving up on it

# Patterns that indicate a missing / invalid API key in expert output
_KEY_ERROR_PATTERNS = [
    "api_key",
    "apikey",
    "authentication",
    "invalid_api_key",
    "incorrect api key",
    "unauthorized",
    "no api key",
    "key required",
    "api key required",
]

# Patterns specifically in "[Execution Error]" that hint at key issues
_EXEC_ERROR_KEY_HINTS = ["api_key", "key", "openai", "anthropic", "fal", "replicate"]


def _is_key_error(msg: str) -> bool:
    low = msg.lower()
    return any(p in low for p in _KEY_ERROR_PATTERNS)


def _extract_key_name(msg: str, expert_name: str = "") -> str:
    """Best-guess which key is needed from the error message and expert name."""
    combined = (msg + " " + expert_name).lower()
    if "fal" in combined:
        return "fal_api_key"
    if "anthropic" in combined or "claude" in combined:
        return "anthropic_api_key"
    if "replicate" in combined:
        return "replicate_api_key"
    # Default to OpenAI key
    return "api_key"


# Params whose names indicate they are the primary content input — treat as
# required when no meaningful default is provided (empty string or None).
_CONTENT_INPUT_PARAMS = frozenset({
    "url", "prompt", "text", "input_path", "file_url", "video_url",
    "audio_url", "image_url", "input_url", "query", "question",
    "input_text", "content", "source_url",
})


def _build_retry_hint(
    err_msg: str,
    fn_args: dict,
    expert_name: str,
    attempt: int,
    max_retries: int,
    required_params: list[str],
) -> str:
    """
    Build a structured error payload to return as a tool result.
    When retries remain, includes an explicit instruction for the model to fix
    and retry. When retries are exhausted, tells the model to use another tool.
    """
    low = err_msg.lower()
    hints: list[str] = []

    # Diagnose likely cause
    if "500" in err_msg or "internal server error" in low:
        if any(p for p in required_params if not fn_args.get(p)):
            missing = [p for p in required_params if not fn_args.get(p)]
            hints.append(
                f"Required parameter(s) were empty or missing: {missing}. "
                "Extract the correct value from the user's message and pass it."
            )
        else:
            hints.append(
                "The expert returned a server error (500). "
                "This may be a temporary issue — retry with the same or slightly adjusted parameters."
            )
    elif "timeout" in low:
        hints.append("The call timed out. Retry with a shorter/simpler input if possible.")
    elif err_msg:
        hints.append(f"Error detail: {err_msg[:200]}")

    retries_left = max_retries - attempt
    if retries_left > 0:
        action = (
            f"Retry this tool call with corrected parameters. "
            f"You have {retries_left} retry attempt(s) remaining for this tool."
        )
    else:
        action = (
            "Maximum retries for this tool reached. "
            "Do NOT call it again. Try a different tool or inform the user."
        )

    payload = {
        "status": "error",
        "message": err_msg[:200] if err_msg else "Unknown error",
        "hint": " ".join(hints),
        "action": action,
    }
    return json.dumps(payload, ensure_ascii=False)


def _build_tool_schema(
    expert_name: str,
    params: dict,
    api_description: str = "",
    display_name: str = "",
) -> dict:
    """Convert Extella expert_params dict to an OpenAI function tool schema.

    Description priority: api_description (from expert/get) → display_name → expert_name.
    Params whose names match _CONTENT_INPUT_PARAMS and have no meaningful
    default value are listed as required so the model always supplies them.
    """
    properties: dict = {}
    required_params: list[str] = []

    for pname, default_val in params.items():
        if pname.startswith("__"):
            continue  # skip internal params like __tg_bot_token__
        prop_type = "string"
        if isinstance(default_val, bool):
            prop_type = "boolean"
        elif isinstance(default_val, int):
            prop_type = "integer"
        elif isinstance(default_val, float):
            prop_type = "number"
        elif isinstance(default_val, list):
            prop_type = "array"
        properties[pname] = {"type": prop_type}

        # Mark as required if it is a content input param with no real default
        if pname in _CONTENT_INPUT_PARAMS and (
            default_val is None or default_val == "" or default_val == []
        ):
            required_params.append(pname)

    # OpenAI function names must be valid identifiers (no hyphens)
    fn_name = expert_name.replace("-", "_")
    # Use the richer API description when available; fall back to display name
    description = (api_description or display_name or f"Run {expert_name}")[:300]

    return {
        "type": "function",
        "function": {
            "name": fn_name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required_params,
            },
        },
    }


def _is_agent_guide(concept_text: str) -> bool:
    """Return True if concept text contains agentic routing markers."""
    if not concept_text:
        return False
    return "FLOW:" in concept_text or "EXPERTS:" in concept_text


async def _fetch_expert_schemas(
    experts: list,
    user_tok: str,
) -> dict[str, dict]:
    """
    Fetch full expert info for all experts in parallel using get_expert_info.

    Returns {expert_name: {"params": {param: default}, "description": str}}.

    Experts whose fetch fails (500, network error) or returns no params are
    excluded from the result so they are not offered as OpenAI tools.
    """
    cli = ExtellaClient(user_tok, profile_id="default", agent_id="agent_extella_default")

    async def fetch_one(exp) -> tuple[str, dict]:
        info = await cli.get_expert_info(exp.expert_name)
        return exp.expert_name, info

    results = await asyncio.gather(*[fetch_one(e) for e in experts], return_exceptions=True)
    out: dict[str, dict] = {}
    skipped: list[str] = []
    for item in results:
        if isinstance(item, Exception):
            logger.warning("[AGENT] fetch_expert_schemas error: %s", item)
            continue
        name, info = item
        if info and info.get("params"):
            out[name] = info
        else:
            # No params → expert/get returned 500 or expert doesn't exist in this account
            skipped.append(name)
    if skipped:
        logger.warning("[AGENT] experts skipped (no schema / server error): %s", skipped)
    return out


async def run_agentic_loop(
    bot: "Bot",
    user_message: str,
    concept_text: str,
    experts: list["BotExpert"],
    user_tok: str,
    target_id: str,
    openai_key: str,
    all_keys: dict,
    expert_schemas: dict[str, dict] | None = None,
) -> dict:
    """
    Run an OpenAI function-calling loop using concept_text as system prompt
    and Extella experts as tools.

    Returns one of:
      {"status": "ok", "text": "<assistant reply>"}
      {"status": "needs_key", "key_name": "api_key", "expert_name": "..."}
      {"status": "error", "message": "..."}
    """
    # Fetch schemas if not pre-fetched
    if expert_schemas is None:
        expert_schemas = await _fetch_expert_schemas(experts, user_tok)

    local_cli = ExtellaClient(user_tok, profile_id="default", agent_id="agent_extella_default")
    client = AsyncOpenAI(api_key=openai_key)

    # Build OpenAI tools list — only include experts with a valid schema.
    # Experts missing from expert_schemas had a 500/empty response and are skipped.
    tools: list[dict] = []
    # Map normalized OpenAI fn name → real Extella expert name
    fn_to_expert: dict[str, str] = {}

    for exp in experts:
        info = expert_schemas.get(exp.expert_name)
        if not info or not info.get("params"):
            logger.debug("[AGENT] skipping tool for %s (no schema)", exp.expert_name)
            continue
        schema = _build_tool_schema(
            exp.expert_name,
            info["params"],
            api_description=info.get("description", ""),
            display_name=exp.display_name or "",
        )
        tools.append(schema)
        fn_to_expert[schema["function"]["name"]] = exp.expert_name

    messages: list[dict] = [
        {"role": "system", "content": concept_text},
        {"role": "user", "content": user_message},
    ]

    # Guard: device must be configured
    if not user_tok or not target_id:
        return {"status": "needs_device", "expert_name": ""}

    logger.info("[AGENT] start | bot=%s | experts=%s | user_msg=%.80s",
                bot.id, list(fn_to_expert.values()), user_message)

    # Track how many times each tool has been called with an error response
    # so the model gets a "give up on this tool" message after MAX_TOOL_RETRIES failures.
    tool_error_counts: dict[str, int] = {}

    for iteration in range(MAX_ITERATIONS):
        try:
            response = await client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                tools=tools or None,
                tool_choice="auto" if tools else None,
                max_tokens=2000,
                temperature=0.3,
            )
        except Exception as exc:
            logger.error("[AGENT] OpenAI error iter=%d: %s", iteration, exc)
            return {"status": "error", "message": f"AI service error: {str(exc)[:200]}"}

        choice = response.choices[0]
        assist_msg = choice.message
        finish = choice.finish_reason

        # Append assistant turn to history (model_dump for serialisation)
        messages.append(assist_msg.model_dump(exclude_unset=True))

        # No tool calls → we're done
        if finish == "stop" or not assist_msg.tool_calls:
            text = assist_msg.content or "Done."
            logger.info("[AGENT] finish iter=%d | text=%.80s", iteration, text)
            return {"status": "ok", "text": text}

        # Execute each tool call sequentially (Extella local device — serial is safer)
        for tool_call in assist_msg.tool_calls:
            fn_name = tool_call.function.name
            real_name = fn_to_expert.get(fn_name, fn_name)

            try:
                fn_args: dict = json.loads(tool_call.function.arguments)
            except Exception:
                fn_args = {}

            # Inject stored API keys the expert may need
            allowed = set(((expert_schemas.get(real_name) or {}).get("params") or {}).keys())
            for k, v in all_keys.items():
                if k in allowed and k not in fn_args and v:
                    fn_args[k] = v

            logger.info("[AGENT] iter=%d tool_call %s(%s)",
                        iteration, real_name, list(fn_args.keys()))

            try:
                result = await local_cli.run_expert(
                    real_name, fn_args,
                    target=target_id, wait=True, timeout=120,
                )
            except Exception as exc:
                result = {"status": "error", "message": str(exc)}

            # Pass device-level and token errors straight up the chain
            if result.get("status") in ("needs_device", "device_offline", "token_invalid"):
                return result

            # --- Error handling with retry feedback ---
            if result.get("status") == "error":
                err_msg = result.get("message", "")

                # 401 → token problem, surface immediately
                if "401" in err_msg:
                    logger.warning("[AGENT] token invalid (401) for expert=%s", real_name)
                    return {"status": "token_invalid", "expert_name": real_name}

                # Check nested result field for [Execution Error] payloads
                inner = result.get("result", "")
                if isinstance(inner, str) and "[Execution Error]" in inner:
                    err_msg = inner

                # Missing API key → ask user
                if _is_key_error(err_msg):
                    key_name = _extract_key_name(err_msg, real_name)
                    logger.warning("[AGENT] missing key=%s for expert=%s", key_name, real_name)
                    return {
                        "status": "needs_key",
                        "key_name": key_name,
                        "expert_name": real_name,
                        "error_detail": err_msg[:200],
                    }

                # Retryable error (5xx, timeout) — give the model a diagnostic hint
                attempt = tool_error_counts.get(fn_name, 0)
                tool_error_counts[fn_name] = attempt + 1

                schema_params = (expert_schemas.get(real_name) or {}).get("params") or {}
                req_params = [
                    p for p in schema_params
                    if p in _CONTENT_INPUT_PARAMS
                    and (schema_params[p] is None or schema_params[p] == "")
                ]
                retry_content = _build_retry_hint(
                    err_msg=err_msg,
                    fn_args=fn_args,
                    expert_name=real_name,
                    attempt=attempt,
                    max_retries=MAX_TOOL_RETRIES,
                    required_params=req_params,
                )
                logger.warning(
                    "[AGENT] tool error iter=%d attempt=%d/%d expert=%s | %s",
                    iteration, attempt + 1, MAX_TOOL_RETRIES, real_name, err_msg[:100],
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": retry_content,
                })
                continue  # next tool_call in batch

            # Success — append result normally
            result_content = json.dumps(result, ensure_ascii=False)[:4000]
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result_content,
            })

    # Max iterations reached — return whatever text the agent produced last
    logger.warning("[AGENT] max iterations=%d reached for bot=%s", MAX_ITERATIONS, bot.id)
    last_text = next(
        (m.get("content") for m in reversed(messages)
         if m.get("role") == "assistant" and m.get("content")),
        "Reached maximum steps. Please try a more specific request.",
    )
    return {"status": "ok", "text": last_text}
