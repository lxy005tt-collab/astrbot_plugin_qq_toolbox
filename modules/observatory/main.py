"""Bot 观察台 v1.

Read-only observability for AstrBot's main chat chain:
- one trace per main reply round
- one request row per direct provider attempt (tool loop / retry / fallback)
- actual post-compression input snapshot, returned text, public reasoning
- API-reported usage and versioned Decimal cost estimates
- native history import, clearly separated from live capture

The plugin never calls a model, never sends QQ messages, never edits the main
chat context, and never writes to AstrBot's own database or config.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from astrbot import logger
from astrbot.api.event import AstrMessageEvent
from ...runtime import deferred_filter as filter
from astrbot.api.star import Context, StarTools
from ...runtime import SuiteModule as Star
from astrbot.core.config.default import VERSION
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from . import __version__, compat, pricing
from .collector import Collector
from .native_reader import NativeReader
from .page_api import PLUGIN_NAME, PageApi
from .storage import Storage

DEFAULT_SETTINGS: dict[str, Any] = {
    "capture_enabled": True,
    "record_incoming_messages": True,
    "record_outgoing_messages": True,
    "retention_chat_days": 30,
    "retention_reasoning_days": 7,
    "retention_snapshot_days": 7,
    "retention_usage_days": 180,
    "data_budget_gb": 2.0,
    "refresh_seconds": 10,
    "max_message_chars": 8000,
}


def _coerce_settings(config: Any) -> dict[str, Any]:
    raw = dict(config) if config else {}
    settings = dict(DEFAULT_SETTINGS)
    for key, default in DEFAULT_SETTINGS.items():
        if key not in raw:
            continue
        value = raw[key]
        try:
            if isinstance(default, bool):
                settings[key] = bool(value)
            elif isinstance(default, int):
                settings[key] = int(value)
            elif isinstance(default, float):
                settings[key] = float(value)
        except (TypeError, ValueError):
            settings[key] = default
    return settings


class BotObservatory(Star):
    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self.plugin_config = config
        self.settings = _coerce_settings(config)
        self.data_dir: Path = StarTools.get_data_dir(PLUGIN_NAME)
        self.storage = Storage(
            self.data_dir,
            defaults=self.settings,
            logger=logger,
            cost_resolver=pricing.resolve_cost,
        )
        self.collector = Collector(
            storage=self.storage,
            settings=self.settings,
            logger=logger,
        )
        self.native_reader: NativeReader | None = None
        self.page_api = PageApi(self)
        self._native_task: asyncio.Task | None = None
        self._closed = False
        self._routes_registered = False

    # -------------------------------------------------------------- lifecycle
    async def initialize(self) -> None:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            self.storage.open()
        except Exception as exc:
            logger.error("Bot 观察台无法打开存储：%s", exc, exc_info=True)
            self._closed = True
            return
        # The toolbox config is authoritative; its WebUI and AstrBot config agree.
        self.settings = self.storage.save_settings(self.settings)
        self.collector.update_settings(self.settings)
        self.native_reader = NativeReader(
            native_db_path=self._native_db_path(),
            storage=self.storage,
            logger=logger,
            max_message_chars=int(self.settings.get("max_message_chars") or 8000),
        )
        caps = compat.install(self.collector)
        self.collector.compat_state = compat.active_state()
        if not caps.get("ok"):
            logger.warning(
                "Bot 观察台兼容性检查未通过：%s", caps.get("warnings")
            )
        await self.collector.start()
        if not self._routes_registered:
            self.page_api.register()
            self._routes_registered = True
        self._native_task = asyncio.create_task(self._native_loop())
        logger.info(
            "Bot 观察台已加载：插件 %s，当前 AstrBot %s，数据目录 %s",
            __version__,
            VERSION,
            self.data_dir,
        )

    async def terminate(self) -> None:
        self._closed = True
        if self._native_task is not None:
            self._native_task.cancel()
            self._native_task = None
        try:
            await self.collector.stop()
        except Exception:
            pass
        compat.deactivate(self.collector, restore=True)
        try:
            self.storage.close()
        except Exception:
            pass

    def _native_db_path(self) -> Path:
        try:
            db = self.context.get_db()
            db_path = getattr(db, "db_path", None)
            if db_path:
                return Path(str(db_path))
        except Exception:
            pass
        return Path(get_astrbot_data_path()) / "data_v4.db"

    async def _native_loop(self) -> None:
        # Give the bot a moment to finish startup, then import only old history.
        first = True
        while not self._closed:
            if not first:
                try:
                    await asyncio.sleep(600)
                except asyncio.CancelledError:
                    break
            first = False
            try:
                if self.native_reader is None:
                    break
                result = await self.native_reader.import_all()
                if result.get("ok"):
                    logger.debug("Bot 观察台原生历史导入完成：%s", result.get("coverage"))
                elif result.get("reason"):
                    logger.debug("Bot 观察台原生历史导入跳过：%s", result.get("reason"))
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                logger.debug("Bot 观察台原生历史导入失败：%s", exc)
                try:
                    await self.storage.write_events(
                        [
                            {
                                "type": "gap",
                                "kind": "native_import_error",
                                "detail": f"{type(exc).__name__}: {exc}",
                            }
                        ]
                    )
                except Exception:
                    pass

    # ------------------------------------------------------------------ hooks
    @filter.event_message_type(filter.EventMessageType.ALL, priority=0)
    async def observe_incoming(self, event: AstrMessageEvent) -> None:
        """Copy received platform messages without affecting the pipeline."""
        if self._closed:
            return
        try:
            self.collector.record_incoming(event)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Bot 观察台记录收到消息失败：%s", exc)

    @filter.after_message_sent()
    async def observe_outgoing(self, event: AstrMessageEvent) -> None:
        """Record the chain that reached the send stage (evidence only)."""
        if self._closed:
            return
        try:
            self.collector.record_outgoing(event)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Bot 观察台记录发送结果失败：%s", exc)

    # -------------------------------------------------------------- settings
    def get_timezone(self) -> str:
        try:
            return str(self.context.get_config().get("timezone") or "Asia/Shanghai")
        except Exception:
            return "Asia/Shanghai"

    async def resolve_budget(self, scope: dict) -> dict:
        """Resolve the live per-session/provider context budget and trigger."""
        umo = (scope or {}).get("umo") or None
        conf = None
        try:
            conf = self.context.get_config(umo=umo)
        except Exception:
            conf = None
        conf = conf or {}
        runner_cfg = ((conf.get("agent_runner") or {}).get("config") or {}) if isinstance(conf, dict) else {}
        model_cfg = runner_cfg.get("model") or {}
        compression = runner_cfg.get("compression") or {}
        provider_id = str(model_cfg.get("provider_id") or "")
        provider = None
        try:
            provider = await self.context.get_using_provider_async(umo=umo)
        except Exception:
            provider = None
        if provider is None and provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
            except Exception:
                provider = None
        if provider is None:
            try:
                provider = self.context.get_using_provider(umo=umo)
            except Exception:
                provider = None
        budget = 0
        model = ""
        resolved_provider_id = provider_id
        if provider is not None:
            provider_config = getattr(provider, "provider_config", {}) or {}
            resolved_provider_id = str(provider_config.get("id") or provider_id)
            try:
                budget = int(provider_config.get("max_context_tokens") or 0)
            except (TypeError, ValueError):
                budget = 0
            try:
                model = str(provider.get_model() or "")
            except Exception:
                model = str(provider_config.get("model") or "")
        else:
            providers = conf.get("provider") if isinstance(conf, dict) else None
            if isinstance(providers, list):
                for item in providers:
                    if str(item.get("id") or "") == provider_id:
                        try:
                            budget = int(item.get("max_context_tokens") or 0)
                        except (TypeError, ValueError):
                            budget = 0
                        model = str(item.get("model") or "")
                        break
        strategy = str(compression.get("overflow_strategy") or "truncate_by_turns")
        max_turns = compression.get("max_turns", -1)
        keep_recent = compression.get("keep_recent_ratio", 0.15)
        threshold = 0.82
        trigger = int(budget * threshold) + 1 if budget > 0 else None
        return {
            "budget_tokens": budget or None,
            "trigger_tokens": trigger,
            "threshold": threshold,
            "strategy": strategy,
            "keep_recent_ratio": keep_recent,
            "max_turns": max_turns,
            "provider_id": resolved_provider_id,
            "model": model,
            "source": "live_session_config",
            "note": (
                "按当前会话/provider 的 max_context_tokens 与核心压缩阈值 0.82 计算；"
                "管理预算不是供应商模型真实上限。"
                if budget
                else "当前会话/provider 未设置 max_context_tokens，token 压缩未启用。"
            ),
        }

    # ---------------------------------------------------------------- prices
    def list_prices(self) -> list[dict]:
        with self.storage._lock:
            return pricing.list_prices(self.storage.conn)

    def save_price(self, payload: Any) -> dict:
        with self.storage._lock:
            return pricing.save_price(self.storage.conn, payload)
