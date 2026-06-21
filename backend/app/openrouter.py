import json
from datetime import UTC, datetime
from typing import Any

import httpx

from .model_config import AdSize
from .settings import get_settings

_MODEL_CAPABILITIES: dict[str, dict[str, Any]] | None = None


class OpenRouterRequestError(RuntimeError):
    pass


def build_agent_messages(
    prompt: str,
    sizes: list[AdSize],
    supports_images: bool,
    supports_tools: bool = False,
) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": system_prompt(supports_tools=supports_tools, supports_images=supports_images)},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "task": prompt,
                    "sandbox": "E2B Linux shell",
                    "output_dir": "/tmp/advertbench-output",
                    "required_outputs": [
                        {
                            "file": f"/tmp/advertbench-output/{size.key}.png",
                            "key": size.key,
                            "label": size.label,
                            "width": size.width,
                            "height": size.height,
                            "ratio": size.ratio,
                        }
                        for size in sizes
                    ],
                    "image_view_tool_available": supports_images,
                    "response_format": {
                        "tool": "bash | view_image | final" if not supports_tools else "Use native tool calls.",
                        "command": "required for bash",
                        "path": "required for view_image",
                        "status": "done, only for final",
                    },
                }
            ),
        },
    ]


def system_prompt(*, supports_tools: bool, supports_images: bool) -> str:
    if supports_tools and supports_images:
        return (
            "You are an autonomous ad-generation agent operating inside an E2B Linux sandbox. "
            "Use bash to inspect the environment and create the requested PNG files. "
            "Use view_image after creating images to check composition, legibility, and sizing before finalizing. "
            "Call final when finished."
        )
    if supports_tools:
        return (
            "You are an autonomous ad-generation agent operating inside an E2B Linux sandbox. "
            "Use bash to inspect the environment and create the requested PNG files. "
            "Call final when finished."
        )
    if supports_images:
        return (
            "You are an autonomous ad-generation agent operating inside an E2B Linux sandbox. "
            "Return exactly one JSON tool action per turn and no other text. "
            'Use {"tool":"bash","command":"<bash command>"} to run a shell command. '
            'Use {"tool":"view_image","path":"<path>"} after creating images to check composition, legibility, and sizing before finalizing. '
            'Use {"tool":"final","status":"done"} when finished. '
            "view_image results include the actual image in the next user message."
        )
    return (
        "You are an autonomous ad-generation agent operating inside an E2B Linux sandbox. "
        "Return exactly one JSON tool action per turn and no other text. "
        'Use {"tool":"bash","command":"<bash command>"} to run a shell command. '
        'Use {"tool":"final","status":"done"} when finished.'
    )


async def openrouter_model_capabilities() -> dict[str, dict[str, Any]]:
    global _MODEL_CAPABILITIES
    if _MODEL_CAPABILITIES is not None:
        return _MODEL_CAPABILITIES

    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"} if settings.openrouter_api_key else {}
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get("https://openrouter.ai/api/v1/models", headers=headers)
    if response.status_code >= 400:
        raise RuntimeError(f"OpenRouter model metadata failed: {response.status_code} {response.text}")

    _MODEL_CAPABILITIES = _parse_model_capabilities(response.json())
    return _MODEL_CAPABILITIES


def openrouter_model_capabilities_sync() -> dict[str, dict[str, Any]]:
    global _MODEL_CAPABILITIES
    if _MODEL_CAPABILITIES is not None:
        return _MODEL_CAPABILITIES

    settings = get_settings()
    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"} if settings.openrouter_api_key else {}
    with httpx.Client(timeout=30) as client:
        response = client.get("https://openrouter.ai/api/v1/models", headers=headers)
    if response.status_code >= 400:
        raise RuntimeError(f"OpenRouter model metadata failed: {response.status_code} {response.text}")
    _MODEL_CAPABILITIES = _parse_model_capabilities(response.json())
    return _MODEL_CAPABILITIES


def _parse_model_capabilities(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    capabilities: dict[str, dict[str, Any]] = {}
    for model in body.get("data", []):
        model_id = model.get("id")
        if not model_id:
            continue
        input_modalities = model.get("architecture", {}).get("input_modalities", []) or []
        supported_parameters = model.get("supported_parameters", []) or []
        created = model.get("created")
        capabilities[model_id] = {
            "supportsImages": "image" in input_modalities,
            "supportsTools": "tools" in supported_parameters,
            "releaseDate": datetime.fromtimestamp(created, UTC).date().isoformat() if created else None,
            "inputModalities": input_modalities,
            "supportedParameters": supported_parameters,
        }
    return capabilities


async def model_supports_images(model_slug: str) -> bool:
    capabilities = await openrouter_model_capabilities()
    return bool(capabilities.get(model_slug, {}).get("supportsImages"))


async def model_supports_tools(model_slug: str) -> bool:
    capabilities = await openrouter_model_capabilities()
    return bool(capabilities.get(model_slug, {}).get("supportsTools"))


def agent_tools(supports_images: bool) -> list[dict[str, Any]]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "Run one bash command inside the E2B Linux sandbox.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "The bash command to execute.",
                        }
                    },
                    "required": ["command"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "final",
                "description": "Declare that all required PNG files are complete and ready for collection.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "enum": ["done"]},
                    },
                    "required": ["status"],
                    "additionalProperties": False,
                },
            },
        },
    ]
    if supports_images:
        tools.insert(
            1,
            {
                "type": "function",
                "function": {
                    "name": "view_image",
                    "description": "Inspect a PNG image created inside the E2B sandbox.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Absolute path to the image in the sandbox.",
                            }
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
            },
        )
    return tools


async def request_agent_turn(
    model_slug: str,
    messages: list[dict[str, Any]],
    *,
    use_tools: bool,
    supports_images: bool,
    model_settings: dict[str, Any],
) -> dict[str, Any]:
    if use_tools:
        return await _request_agent_tool_turn(
            model_slug,
            messages,
            supports_images=supports_images,
            model_settings=model_settings,
        )
    body = await _request_agent_action_body(model_slug, messages, model_settings=model_settings)
    return {"mode": "json", "action": parse_json_action(body), "usage": body.get("usage"), "response_id": body.get("id")}


async def _request_agent_tool_turn(
    model_slug: str,
    messages: list[dict[str, Any]],
    *,
    supports_images: bool,
    model_settings: dict[str, Any],
) -> dict[str, Any]:
    payload = _with_model_settings(
        {
            "model": model_slug,
            "messages": messages,
            "tools": agent_tools(supports_images),
            "tool_choice": "auto",
            "temperature": 0.7,
        },
        model_settings,
    )
    body = await _post_chat_completion(payload)
    message = body.get("choices", [{}])[0].get("message", {})
    tool_calls = message.get("tool_calls") or []
    assistant_message = {
        "role": "assistant",
        "content": message.get("content"),
    }
    if tool_calls:
        assistant_message["tool_calls"] = tool_calls

    actions = []
    for call in tool_calls:
        function = call.get("function", {}) or {}
        raw_arguments = function.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
        except json.JSONDecodeError:
            arguments = {"_argument_error": f"Invalid JSON arguments: {raw_arguments}"}
        actions.append(
            {
                "tool": function.get("name"),
                "arguments": arguments if isinstance(arguments, dict) else {},
                "tool_call_id": call.get("id"),
                "raw": call,
            }
        )

    return {
        "mode": "tools",
        "message": assistant_message,
        "actions": actions,
        "usage": body.get("usage"),
        "response_id": body.get("id"),
    }


async def request_agent_action(model_slug: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    return parse_json_action(await _request_agent_action_body(model_slug, messages))


async def _request_agent_action_body(
    model_slug: str,
    messages: list[dict[str, Any]],
    *,
    model_settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return await _post_chat_completion(
        _with_model_settings(
            {
                "model": model_slug,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "temperature": 0.7,
            },
            model_settings or {},
        )
    )


def parse_json_action(body: dict[str, Any]) -> dict[str, Any]:
    content = body.get("choices", [{}])[0].get("message", {}).get("content")
    if not content:
        raise RuntimeError("OpenRouter returned no message content")

    if isinstance(content, list):
        content = "\n".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)

    try:
        action = json.loads(str(content))
    except json.JSONDecodeError as error:
        raise RuntimeError(f"OpenRouter returned invalid JSON action: {content}") from error

    if not isinstance(action, dict) or action.get("tool") not in {"bash", "view_image", "final"}:
        raise RuntimeError(f"OpenRouter returned invalid tool action: {action}")
    return action


async def _post_chat_completion(payload: dict[str, Any]) -> dict[str, Any]:
    settings = get_settings()
    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {settings.openrouter_api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": settings.app_base_url,
                "X-Title": "AdvertBench",
            },
            json=payload,
        )

    if response.status_code >= 400:
        raise OpenRouterRequestError(f"OpenRouter failed: {response.status_code} {response.text}")
    return response.json()


def _with_model_settings(payload: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    reserved = {"model", "messages", "tools", "tool_choice", "response_format"}
    return {**payload, **{key: value for key, value in settings.items() if key not in reserved}}
