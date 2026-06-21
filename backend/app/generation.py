import json
from time import perf_counter
from typing import Any

from psycopg.types.json import Jsonb

from .assets import save_asset
from .db import connection, transaction
from .e2b_agent import E2BAgentSandbox
from .model_config import AdSize, config_as_json, sync_models_from_config
from .openrouter import build_agent_messages, model_supports_images, model_supports_tools, request_agent_turn
from .settings import get_settings

MAX_MESSAGE_CHARS = 12000


class GenerationHarnessError(RuntimeError):
    def __init__(self, message: str, turns: int):
        super().__init__(message)
        self.turns = turns


async def generate_run(prompt: str) -> str:
    settings = get_settings()
    config = sync_models_from_config()
    mode = "openrouter-e2b-agent"

    if not settings.openrouter_api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not configured")
    if not settings.e2b_api_key:
        raise RuntimeError("E2B_API_KEY is not configured")

    with connection() as conn:
        row = conn.execute(
            """
            INSERT INTO generation_runs (prompt, status, requested_sizes, config_snapshot, generated_with, started_at)
            VALUES (%s, 'running', %s, %s, %s, now())
            RETURNING id
            """,
            (prompt, Jsonb([size.model_dump() for size in config.ad_sizes]), Jsonb(config_as_json(config)), mode),
        ).fetchone()
        conn.commit()
        run_id = str(row["id"])

    with connection() as conn:
        models = conn.execute(
            """
            SELECT id, slug, display_name, metadata
            FROM models
            WHERE slug = ANY(%s::text[])
            ORDER BY slug
            """,
            ([model.slug for model in config.models],),
        ).fetchall()

    try:
        failures = []
        successes = 0
        for model in models:
            supports_images = await model_supports_images(model["slug"])
            supports_tools = await model_supports_tools(model["slug"])
            try:
                await generate_set(run_id, model, prompt, config.ad_sizes, supports_images, supports_tools)
                successes += 1
            except Exception as error:
                failures.append(f"{model['slug']}: {error}")

        with connection() as conn:
            status = "completed" if successes else "failed"
            error = "; ".join(failures)[:4000] if failures else None
            conn.execute(
                "UPDATE generation_runs SET status = %s, error = %s, completed_at = now() WHERE id = %s",
                (status, error, run_id),
            )
            conn.commit()
    except Exception as error:
        with connection() as conn:
            conn.execute(
                "UPDATE generation_runs SET status = 'failed', error = %s, completed_at = now() WHERE id = %s",
                (str(error), run_id),
            )
            conn.commit()
        raise

    return run_id


async def generate_set(
    run_id: str,
    model: dict,
    prompt: str,
    sizes: list[AdSize],
    supports_images: bool,
    supports_tools: bool,
) -> None:
    started = perf_counter()
    generation_turns = 0
    with connection() as conn:
        row = conn.execute(
            """
            INSERT INTO output_sets (run_id, model_id, status, prompt)
            VALUES (%s, %s, 'running', %s)
            RETURNING id
            """,
            (run_id, model["id"], prompt),
        ).fetchone()
        conn.commit()
        output_set_id = str(row["id"])

    try:
        rendered, generation_turns = await generate_ads_with_openrouter_e2b_agent(
            model["slug"],
            prompt,
            sizes,
            supports_images=supports_images,
            supports_tools=supports_tools,
            model_settings=(model.get("metadata") or {}).get("settings", {}),
        )

        generation_ms = round((perf_counter() - started) * 1000)
        with transaction() as conn:
            for size in sizes:
                png = rendered.get(size.key)
                if not png:
                    raise RuntimeError(f"Missing rendered asset for {size.key}")
                saved = save_asset(output_set_id, size, png)
                conn.execute(
                    """
                    INSERT INTO ad_assets (output_set_id, size_key, label, width, height, storage_path, public_path, mime_type, checksum)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, 'image/png', %s)
                    """,
                    (
                        output_set_id,
                        size.key,
                        size.label,
                        size.width,
                        size.height,
                        saved["storage_path"],
                        saved["public_path"],
                        saved["checksum"],
                    ),
                )

            conn.execute(
                """
                UPDATE output_sets
                SET status = 'completed', completed_at = now(), generation_ms = %s, generation_turns = %s
                WHERE id = %s
                """,
                (generation_ms, generation_turns, output_set_id),
            )
    except Exception as error:
        generation_turns = generation_turns or getattr(error, "turns", 0) or None
        with connection() as conn:
            conn.execute(
                """
                UPDATE output_sets
                SET status = 'failed', error = %s, completed_at = now(), generation_turns = %s
                WHERE id = %s
                """,
                (str(error), generation_turns, output_set_id),
            )
            conn.commit()
        raise


async def generate_ads_with_openrouter_e2b_agent(
    model_slug: str,
    prompt: str,
    sizes: list[AdSize],
    *,
    supports_images: bool = False,
    supports_tools: bool = False,
    model_settings: dict[str, Any] | None = None,
    transcript: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, bytes], int]:
    max_turns = max(1, get_settings().generation_max_turns)
    messages = build_agent_messages(prompt, sizes, supports_images, supports_tools)
    sandbox = E2BAgentSandbox()
    last_result = "agent did not request a tool"
    if transcript is not None:
        transcript.append(
            {
                "event": "start",
                "model": model_slug,
                "supports_tools": supports_tools,
                "supports_images": supports_images,
                "model_settings": _json_safe(model_settings or {}),
                "messages": _json_safe(messages),
            }
        )

    try:
        for turn in range(1, max_turns + 1):
            try:
                turn_result = await request_agent_turn(
                    model_slug,
                    messages,
                    use_tools=supports_tools,
                    supports_images=supports_images,
                    model_settings=model_settings or {},
                )
            except Exception as error:
                result = {"ok": False, "error": f"invalid agent action: {error}"}
                last_result = _compact_tool_result(result)
                messages.append({"role": "user", "content": _tool_result_message("agent_action", result)})
                if transcript is not None:
                    transcript.append({"event": "invalid_action", "turn": turn, "result": _json_safe(result)})
                continue

            if turn_result["mode"] == "tools":
                if transcript is not None:
                    transcript.append(
                        {
                            "event": "openrouter_usage",
                            "turn": turn,
                            "response_id": turn_result.get("response_id"),
                            "usage": _json_safe(turn_result.get("usage")),
                        }
                    )
                completed = await _handle_native_tool_turn(
                    turn_result=turn_result,
                    messages=messages,
                    sandbox=sandbox,
                    sizes=sizes,
                    supports_images=supports_images,
                    turn=turn,
                    transcript=transcript,
                )
                if completed is not None:
                    return completed, turn
                if transcript is not None and turn_result.get("last_result"):
                    last_result = str(turn_result["last_result"])
                continue

            action = turn_result["action"]
            if transcript is not None:
                transcript.append(
                    {
                        "event": "openrouter_usage",
                        "turn": turn,
                        "response_id": turn_result.get("response_id"),
                        "usage": _json_safe(turn_result.get("usage")),
                    }
                )
            messages.append({"role": "assistant", "content": json.dumps(action)})
            if transcript is not None:
                transcript.append({"event": "assistant_action", "turn": turn, "action": _json_safe(action)})
            tool = action.get("tool")

            if tool == "bash":
                command = str(action.get("command") or "")
                if not command:
                    result = {"ok": False, "error": "bash tool requires command"}
                else:
                    result = sandbox.run_bash(command)
                last_result = _compact_tool_result(result)
                messages.append({"role": "user", "content": _tool_result_message("bash", result)})
                if transcript is not None:
                    transcript.append({"event": "tool_result", "turn": turn, "tool": "bash", "result": _json_safe(result)})
                continue

            if tool == "view_image":
                path = str(action.get("path") or "")
                result = _view_image_result(sandbox, path, supports_images)
                last_result = _compact_tool_result({key: value for key, value in result.items() if key != "data_url"})
                messages.append(_view_image_message(result, supports_images))
                if transcript is not None:
                    transcript.append({"event": "tool_result", "turn": turn, "tool": "view_image", "result": _json_safe(result)})
                continue

            if tool == "final":
                try:
                    rendered = sandbox.collect_outputs(sizes)
                    if transcript is not None:
                        transcript.append(
                            {
                                "event": "completed",
                                "turn": turn,
                                "files": {key: len(value) for key, value in rendered.items()},
                            }
                        )
                    return rendered, turn
                except Exception as error:
                    result = {"ok": False, "error": str(error)}
                    last_result = _compact_tool_result(result)
                    messages.append({"role": "user", "content": _tool_result_message("final", result)})
                    if transcript is not None:
                        transcript.append({"event": "tool_result", "turn": turn, "tool": "final", "result": _json_safe(result)})
                    continue

        if transcript is not None:
            transcript.append({"event": "failed", "turns": max_turns, "last_result": last_result})
        raise GenerationHarnessError(f"Failed to generate all required files after {max_turns} turns: {last_result}", max_turns)
    finally:
        sandbox.close()


async def _handle_native_tool_turn(
    *,
    turn_result: dict[str, Any],
    messages: list[dict[str, Any]],
    sandbox: E2BAgentSandbox,
    sizes: list[AdSize],
    supports_images: bool,
    turn: int,
    transcript: list[dict[str, Any]] | None,
) -> dict[str, bytes] | None:
    assistant_message = turn_result["message"]
    messages.append(assistant_message)
    if transcript is not None:
        transcript.append({"event": "assistant_tool_calls", "turn": turn, "message": _json_safe(assistant_message)})

    actions = turn_result.get("actions", [])
    if not actions:
        result = {"ok": False, "error": "Model did not call a tool. Use bash, view_image, or final."}
        messages.append({"role": "user", "content": _tool_result_message("agent_action", result)})
        turn_result["last_result"] = _compact_tool_result(result)
        if transcript is not None:
            transcript.append({"event": "invalid_action", "turn": turn, "result": _json_safe(result)})
        return None

    follow_up_messages: list[dict[str, Any]] = []
    for action in actions:
        tool_call_id = action.get("tool_call_id")
        tool = action.get("tool")
        arguments = action.get("arguments", {})
        if "_argument_error" in arguments:
            result = {"ok": False, "error": arguments["_argument_error"]}
            _append_native_tool_result(messages, tool_call_id, str(tool), result)
            turn_result["last_result"] = _compact_tool_result(result)
            if transcript is not None:
                transcript.append({"event": "tool_result", "turn": turn, "tool": tool, "result": _json_safe(result)})
            continue

        if tool == "bash":
            command = str(arguments.get("command") or "")
            result = sandbox.run_bash(command) if command else {"ok": False, "error": "bash tool requires command"}
            _append_native_tool_result(messages, tool_call_id, "bash", result)
            turn_result["last_result"] = _compact_tool_result(result)
            if transcript is not None:
                transcript.append({"event": "tool_result", "turn": turn, "tool": "bash", "result": _json_safe(result)})
            continue

        if tool == "view_image":
            path = str(arguments.get("path") or "")
            result = _view_image_result(sandbox, path, supports_images)
            _append_native_tool_result(messages, tool_call_id, "view_image", {key: value for key, value in result.items() if key != "data_url"})
            if supports_images and result.get("ok"):
                follow_up_messages.append(_view_image_message(result, supports_images))
            turn_result["last_result"] = _compact_tool_result({key: value for key, value in result.items() if key != "data_url"})
            if transcript is not None:
                transcript.append({"event": "tool_result", "turn": turn, "tool": "view_image", "result": _json_safe(result)})
            continue

        if tool == "final":
            try:
                rendered = sandbox.collect_outputs(sizes)
                result = {"ok": True, "files": {key: len(value) for key, value in rendered.items()}}
                _append_native_tool_result(messages, tool_call_id, "final", result)
                if transcript is not None:
                    transcript.append(
                        {
                            "event": "completed",
                            "turn": turn,
                            "files": {key: len(value) for key, value in rendered.items()},
                        }
                    )
                return rendered
            except Exception as error:
                result = {"ok": False, "error": str(error)}
                _append_native_tool_result(messages, tool_call_id, "final", result)
                turn_result["last_result"] = _compact_tool_result(result)
                if transcript is not None:
                    transcript.append({"event": "tool_result", "turn": turn, "tool": "final", "result": _json_safe(result)})
                continue

        result = {"ok": False, "error": f"Unknown tool: {tool}"}
        _append_native_tool_result(messages, tool_call_id, str(tool), result)
        turn_result["last_result"] = _compact_tool_result(result)
        if transcript is not None:
            transcript.append({"event": "tool_result", "turn": turn, "tool": tool, "result": _json_safe(result)})

    messages.extend(follow_up_messages)
    return None


def _append_native_tool_result(messages: list[dict[str, Any]], tool_call_id: str | None, tool: str, result: dict[str, Any]) -> None:
    message = {
        "role": "tool",
        "content": _tool_result_message(tool, result),
    }
    if tool_call_id:
        message["tool_call_id"] = tool_call_id
    messages.append(message)


def _tool_result_message(tool: str, result: dict[str, Any]) -> str:
    return json.dumps({"tool_result": tool, "result": _trim_result(result)})


def _view_image_result(sandbox: E2BAgentSandbox, path: str, supports_images: bool) -> dict[str, Any]:
    if not path:
        return {"ok": False, "error": "view_image requires path"}
    if not supports_images:
        return {"ok": False, "error": "view_image is only available for models whose OpenRouter metadata includes image input"}
    return sandbox.view_image(path)


def _view_image_message(result: dict[str, Any], supports_images: bool) -> dict[str, Any]:
    if not supports_images or not result.get("ok"):
        return {"role": "user", "content": _tool_result_message("view_image", result)}
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": _tool_result_message(
                    "view_image",
                    {key: value for key, value in result.items() if key != "data_url"},
                ),
            },
            {"type": "image_url", "image_url": {"url": result["data_url"]}},
        ],
    }


def _compact_tool_result(result: dict[str, Any]) -> str:
    return json.dumps(_trim_result(result))[:MAX_MESSAGE_CHARS]


def _trim_result(result: dict[str, Any]) -> dict[str, Any]:
    trimmed: dict[str, Any] = {}
    for key, value in result.items():
        if isinstance(value, str):
            trimmed[key] = value[-MAX_MESSAGE_CHARS:]
        else:
            trimmed[key] = value
    return trimmed


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            if key == "data_url" and isinstance(item, str):
                safe[key] = f"{item[:80]}...<truncated {len(item)} chars>"
            else:
                safe[key] = _json_safe(item)
        return safe
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value
