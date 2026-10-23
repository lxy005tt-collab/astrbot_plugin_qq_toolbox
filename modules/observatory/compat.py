"""Minimal, reversible wrappers around the installed AstrBot 4.29.x runtime.

Why a wrapper is needed:
- ``on_llm_request`` fires before compression and before the runner builds the
  final provider payload; ``on_llm_response`` fires once per reply, not once
  per direct model call. Only the runner's provider-call boundary sees the
  post-compression payload, per-attempt usage, and streamed reasoning deltas.

What is wrapped (main-chat-only by construction):
- ``ToolLoopAgentRunner.reset``: creates one reply trace per main agent run.
- ``ToolLoopAgentRunner._iter_llm_responses``: one direct provider attempt
  (primary, retry, or fallback). Auxiliary plugin LLM calls never pass here.
- ``ContextManager._run_compression``: records compression before/after sizes
  for the main agent's context manager.

Every wrapper has a transparent pass-through when no observatory collector is
active. Failures in the collector are swallowed; a model reply must never be
delayed or broken by observation. ``uninstall`` only restores a method when the
current attribute is still our own wrapper, so other plugins' wrappers are not
clobbered.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from astrbot import logger
from astrbot.core.agent.context.manager import ContextManager
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner

_ACTIVE_COLLECTOR: Any = None
_INSTALLED = False
_ORIGINALS: dict[str, Any] = {}
_CAPS: dict[str, Any] = {}
_ERRORS_LOGGED: set[str] = set()


def _log_once(key: str, message: str, exc: BaseException | None = None) -> None:
    if key in _ERRORS_LOGGED:
        return
    _ERRORS_LOGGED.add(key)
    try:
        logger.warning("observatory capture error (%s): %s", key, message)
        if exc is not None:
            logger.debug("observatory capture traceback", exc_info=exc)
    except Exception:
        pass


def probe_capabilities() -> dict[str, Any]:
    warnings: list[str] = []
    try:
        from astrbot.core.config.default import VERSION

        version = str(VERSION)
    except Exception:
        version = "unknown"

    iter_fn = ToolLoopAgentRunner.__dict__.get("_iter_llm_responses")
    reset_fn = ToolLoopAgentRunner.__dict__.get("reset")
    compress_fn = ContextManager.__dict__.get("_run_compression")
    if iter_fn is None:
        warnings.append("ToolLoopAgentRunner._iter_llm_responses missing")
    if reset_fn is None:
        warnings.append("ToolLoopAgentRunner.reset missing")
    if compress_fn is None:
        warnings.append("ContextManager._run_compression missing")

    iter_params: list[str] = []
    try:
        if iter_fn is not None:
            iter_params = list(inspect.signature(iter_fn).parameters)
            if "self" not in iter_params:
                warnings.append("unexpected _iter_llm_responses signature")
    except (TypeError, ValueError):
        warnings.append("cannot inspect _iter_llm_responses signature")

    return {
        "ok": iter_fn is not None and reset_fn is not None,
        "astrbot_version": version,
        "has_iter_llm_responses": iter_fn is not None,
        "has_reset": reset_fn is not None,
        "has_run_compression": compress_fn is not None,
        "iter_params": iter_params,
        "accepts_include_model": "include_model" in iter_params,
        "installed": _INSTALLED,
        "warnings": warnings,
    }


def install(collector: Any) -> dict[str, Any]:
    """Idempotently install wrappers and bind the active collector."""
    global _ACTIVE_COLLECTOR, _INSTALLED, _CAPS
    if not _CAPS:
        _CAPS = probe_capabilities()
    if not _CAPS.get("ok"):
        return dict(_CAPS)
    if not _INSTALLED:
        _ORIGINALS["reset"] = ToolLoopAgentRunner.__dict__.get("reset")
        _ORIGINALS["iter"] = ToolLoopAgentRunner.__dict__.get(
            "_iter_llm_responses"
        )
        _ORIGINALS["compress"] = ContextManager.__dict__.get("_run_compression")
        ToolLoopAgentRunner.reset = _reset
        ToolLoopAgentRunner._iter_llm_responses = _iter_llm_responses
        ContextManager._run_compression = _run_compression
        _INSTALLED = True
    _ACTIVE_COLLECTOR = collector
    _CAPS["installed"] = True
    return dict(_CAPS)


def deactivate(collector: Any = None, *, restore: bool = False) -> None:
    global _ACTIVE_COLLECTOR, _INSTALLED
    if collector is not None and _ACTIVE_COLLECTOR is not collector:
        return
    _ACTIVE_COLLECTOR = None
    if not restore:
        return
    if ToolLoopAgentRunner.__dict__.get("_iter_llm_responses") is _iter_llm_responses:
        ToolLoopAgentRunner._iter_llm_responses = _ORIGINALS["iter"]
    if ToolLoopAgentRunner.__dict__.get("reset") is _reset:
        ToolLoopAgentRunner.reset = _ORIGINALS["reset"]
    if ContextManager.__dict__.get("_run_compression") is _run_compression:
        ContextManager._run_compression = _ORIGINALS["compress"]
    _INSTALLED = False


def active_state() -> dict[str, Any]:
    caps = dict(_CAPS)
    caps["installed"] = _INSTALLED
    caps["wrapper_in_place"] = (
        ToolLoopAgentRunner.__dict__.get("_iter_llm_responses") is _iter_llm_responses
    )
    caps["reset_wrapper_in_place"] = ToolLoopAgentRunner.__dict__.get("reset") is _reset
    caps["compression_wrapper_in_place"] = (
        ContextManager.__dict__.get("_run_compression") is _run_compression
    )
    caps["collector_bound"] = _ACTIVE_COLLECTOR is not None
    return caps


# --------------------------------------------------------------------- wrappers
async def _reset(self: Any, *args: Any, **kwargs: Any) -> None:
    original = _ORIGINALS.get("reset")
    if original is None:  # pragma: no cover - install guard
        return None
    result = await original(self, *args, **kwargs)
    collector = _ACTIVE_COLLECTOR
    if collector is not None:
        try:
            collector.on_runner_reset(self)
        except BaseException as exc:  # noqa: BLE001 - observation must not break chat
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            _log_once("reset", repr(exc), exc)
    return result


async def _iter_llm_responses(self: Any, *args: Any, **kwargs: Any):
    collector = _ACTIVE_COLLECTOR
    if collector is None:
        async for response in _ORIGINALS["iter"](self, *args, **kwargs):
            yield response
        return

    cap = None
    try:
        cap = collector.begin_request(self, include_model=bool(kwargs.get("include_model", True)))
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        _log_once("begin_request", repr(exc), exc)

    generator = _ORIGINALS["iter"](self, *args, **kwargs)
    completed = False
    try:
        async for response in generator:
            if cap is not None:
                try:
                    collector.observe_response(cap, response)
                except BaseException as exc:  # noqa: BLE001
                    if isinstance(
                        exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
                    ):
                        raise
                    _log_once("observe_response", repr(exc), exc)
            yield response
        completed = True
    except asyncio.CancelledError:
        if cap is not None:
            try:
                collector.finish_request(
                    cap, outcome="cancelled", error="request cancelled"
                )
            except Exception:  # noqa: BLE001
                pass
        raise
    except GeneratorExit:
        if cap is not None:
            try:
                collector.finish_request(
                    cap, outcome="cancelled", error="consumer closed the stream"
                )
            except Exception:  # noqa: BLE001
                pass
        raise
    except BaseException as exc:
        if cap is not None:
            try:
                collector.finish_request(
                    cap,
                    outcome="error",
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception:  # noqa: BLE001
                pass
        raise
    else:
        if cap is not None:
            try:
                collector.finish_request(cap, outcome="completed")
            except BaseException as exc:  # noqa: BLE001
                if isinstance(
                    exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
                ):
                    raise
                _log_once("finish_request", repr(exc), exc)
    finally:
        if cap is not None and not completed:
            try:
                collector.finish_request(cap, outcome="incomplete", error="no final response")
            except Exception:  # noqa: BLE001
                pass
        try:
            await generator.aclose()
        except (Exception, GeneratorExit):
            pass


async def _run_compression(
    self: Any, messages: Any, prev_tokens: Any = 0, *args: Any, **kwargs: Any
):
    original = _ORIGINALS.get("compress")
    if original is None:  # pragma: no cover
        return messages
    result = await original(self, messages, prev_tokens, *args, **kwargs)
    collector = _ACTIVE_COLLECTOR
    if collector is not None:
        try:
            collector.on_compression(self, messages, prev_tokens, result)
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                raise
            _log_once("compression", repr(exc), exc)
    return result
