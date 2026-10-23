from __future__ import annotations

import asyncio
import importlib
import inspect
import json
from pathlib import Path
from sys import maxsize

from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star

from .runtime import ModuleContext, PLUGIN_NAME, SectionConfig, bind_handlers
from . import __version__
from .webui import ToolboxWebUI
from .model_routing import ModelRouter, migrate_model_config
from .storage_paths import migrate_legacy_data

DEFINITIONS = json.loads((Path(__file__).parent / "modules_manifest.json").read_text(encoding="utf-8"))
COMPONENT_CLASSES = []
for definition in DEFINITIONS:
    module = importlib.import_module(".modules." + definition["key"] + ".main", __package__)
    COMPONENT_CLASSES.append((definition, getattr(module, definition["class_name"])))


class QQToolbox(Star):
    DEFINITIONS = DEFINITIONS
    DECLARED_TOOLS = {}

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._modules = {}
        self._started = []
        self._routes = []
        self._tool_components = dict(self.DECLARED_TOOLS)

    def get_component(self, name):
        definition = next((item for item in self.DEFINITIONS if name in (item["key"], item["legacy"])), None)
        return self._modules.get(definition["key"]) if definition else None

    async def initialize(self):
        migrate_model_config(self.config)
        self.model_router = ModelRouter(self.context, self.config)
        self.webui = ToolboxWebUI(self)
        self.webui.register()
        route = "/" + PLUGIN_NAME + "/suite/status"
        handler = self.status_api
        self.context.register_web_api(route, handler, ["GET"], "QQ Bot 工具箱模块状态")
        self._routes.append((route, handler, ("GET",)))
        if not self.config.get("enabled", True):
            self._hide_disabled_tools()
            return
        try:
            await asyncio.to_thread(migrate_legacy_data)
            for definition, component_class in COMPONENT_CLASSES:
                key = definition["key"]
                section = SectionConfig(self.config, key)
                if not section.get("_module_enabled", True):
                    continue
                component = component_class(ModuleContext(self, definition), section)
                self._modules[key] = component
                self._started.append(component)
                initialize = getattr(component, "initialize", None)
                if initialize:
                    await initialize()
                self.logger.info("[QQToolbox] 已初始化：%s", definition["display_name"])
            self._hide_disabled_tools()
            self.logger.info("[QQToolbox] 八合一加载完成，启用 %s/8 个模块。", len(self._modules))
        except BaseException:
            await self.terminate()
            raise

    def _hide_disabled_tools(self):
        manager = self.context.get_llm_tool_manager()
        for tool in manager.func_list:
            key = self._tool_components.get(tool.name)
            if key is not None and key not in self._modules:
                tool.active = False

    @filter.on_llm_request(priority=maxsize + 32)
    async def restrict_disabled_module_tools(self, event: AstrMessageEvent, request: ProviderRequest):
        if request.func_tool is None:
            return
        for name, key in self._tool_components.items():
            if key not in self._modules:
                request.func_tool.remove_tool(name)

    async def status_api(self):
        space = self.get_component("space")
        result = {"version": __version__, "enabled": bool(self.config.get("enabled", True)), "module_count": len(self._modules),
                  "modules": {item["key"]: item["key"] in self._modules for item in self.DEFINITIONS},
                  "tool_ownership": dict(self._tool_components)}
        if space:
            result["space"] = (await space.web_status())["data"]
        return {"status": "ok", "data": result}

    async def terminate(self):
        errors = []
        for component in reversed(self._started):
            terminate = getattr(component, "terminate", None)
            if terminate:
                try:
                    result = terminate()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    errors.append(type(exc).__name__)
        self._started.clear()
        self._modules.clear()
        owned = {(route, id(handler), methods) for route, handler, methods in self._routes}
        self.context.registered_web_apis[:] = [entry for entry in self.context.registered_web_apis
            if (entry[0], id(entry[1]), tuple(entry[2])) not in owned]
        self._routes.clear()
        if errors:
            raise RuntimeError("QQ Bot 工具箱清理失败：" + ", ".join(errors))


HANDLER_ORIGINS = bind_handlers(QQToolbox, COMPONENT_CLASSES)
