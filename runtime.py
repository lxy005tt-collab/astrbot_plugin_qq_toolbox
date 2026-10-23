"""One plugin identity, configuration owner, event registry and lifecycle."""
from __future__ import annotations

import functools
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

from astrbot.api.event import filter as native_filter
from astrbot.core.utils.plugin_kv_store import PluginKVStoreMixin

PLUGIN_NAME = "astrbot_plugin_qq_toolbox"
LEGACY_BY_KEY = {item["key"]: item["legacy"] for item in json.loads((Path(__file__).parent / "modules_manifest.json").read_text(encoding="utf-8"))}


class DeferredFilter:
    """Record component decorators; register only the toolbox's delegating methods."""
    DECORATORS = {"command", "custom_filter", "permission_type", "event_message_type", "on_llm_request", "on_llm_response",
                  "on_decorating_result", "on_using_llm_tool", "on_llm_tool_respond", "after_message_sent",
                  "on_plugin_loaded", "llm_tool", "on_astrbot_loaded", "regex", "platform_adapter_type"}

    def __getattr__(self, name):
        if name not in self.DECORATORS:
            return getattr(native_filter, name)

        def factory(*args, **kwargs):
            def record(function):
                specs = list(getattr(function, "__suite_filters__", ()))
                specs.append((name, args, kwargs))
                function.__suite_filters__ = specs
                return function
            return record
        return factory


deferred_filter = DeferredFilter()


class SectionConfig(dict):
    """A component's normal dict API, persisted inside the one toolbox config."""
    def __init__(self, root, key):
        self.root, self.key = root, key
        super().__init__(root.get(key, {}))

    def save_config(self, *args, **kwargs):
        self.root[self.key] = dict(self)
        return self.root.save_config(*args, **kwargs)


class SuiteModule(PluginKVStoreMixin):
    """Internal component, deliberately not an AstrBot Star."""
    def __init__(self, context, config=None):
        self.context = context
        self.logger = context.owner.logger
        self.name = PLUGIN_NAME
        self.legacy_name = context.definition["legacy"]
        # Retain the old KV namespace, notably the QQ voice delivery receipts.
        self.plugin_id = context.definition["author"] + "/" + self.legacy_name


class ModuleContext:
    def __init__(self, owner, definition):
        self.owner, self.definition = owner, definition

    def __getattr__(self, name):
        return getattr(self.owner.context, name)

    @property
    def model_router(self):
        return self.owner.model_router

    def get_registered_star(self, name):
        definition = next((item for item in self.owner.DEFINITIONS if name in (item["key"], item["legacy"])), None)
        if definition is not None:
            module = self.owner.get_component(definition["key"])
            if module is None:
                return None
            return SimpleNamespace(name=definition["legacy"], activated=True, star_cls=module, config=module.config if hasattr(module, "config") else module.plugin_config)
        return self.owner.context.get_registered_star(name)

    def add_llm_tools(self, *tools):
        self.owner.context.add_llm_tools(*tools)
        for tool in tools:
            tool.handler_module_path = self.owner.__class__.__module__
            self.owner._tool_components[tool.name] = self.definition["key"]

    def register_web_api(self, route, view_handler, methods, desc):
        # Native view/page bridges prefix the toolbox's plugin ID automatically.
        for legacy in ("astrbot_plugin_openai_image", "astrbot_plugin_observatory"):
            if route.startswith("/" + legacy + "/"):
                route = "/" + PLUGIN_NAME + route[len(legacy) + 1:]
                break
        if route.startswith("/qzone_helper/"):
            route = "/" + PLUGIN_NAME + "/space/" + route[len("/qzone_helper/"):]
        self.owner.context.register_web_api(route, view_handler, methods, desc)
        self.owner._routes.append((route, view_handler, tuple(methods)))


def bind_handlers(plugin_class, definitions_and_classes):
    """Replay original filters on wrappers, keeping priorities, signatures and tools."""
    mapping = {}
    for definition, component_class in definitions_and_classes:
        key = definition["key"]
        for method_name, method in vars(component_class).items():
            specs = getattr(method, "__suite_filters__", ())
            if not specs:
                continue

            def make_wrapper(function, component_key, is_tool):
                if inspect.isasyncgenfunction(function):
                    @functools.wraps(function)
                    async def generator(self, *args, **kwargs):
                        component = self.get_component(component_key)
                        if component is not None:
                            async for item in function(component, *args, **kwargs):
                                yield item
                    return generator

                @functools.wraps(function)
                async def callback(self, *args, **kwargs):
                    component = self.get_component(component_key)
                    if component is None:
                        return "该功能模块已关闭。" if is_tool else None
                    return await function(component, *args, **kwargs)
                return callback

            wrapper_name = key + "__" + method_name
            wrapper = make_wrapper(method, key, any(name == "llm_tool" for name, _, _ in specs))
            wrapper.__module__ = plugin_class.__module__
            wrapper.__name__ = wrapper_name
            wrapper.__qualname__ = plugin_class.__name__ + "." + wrapper_name
            wrapper.__signature__ = inspect.signature(method, eval_str=True)
            wrapper.__annotations__ = inspect.get_annotations(method, eval_str=True)
            for decorator_name, args, kwargs in specs:
                wrapper = getattr(native_filter, decorator_name)(*args, **kwargs)(wrapper)
                if decorator_name == "llm_tool":
                    tool_name = kwargs.get("name") or method_name
                    plugin_class.DECLARED_TOOLS[tool_name] = key
            setattr(plugin_class, wrapper_name, wrapper)
            mapping[wrapper_name] = (definition["legacy"], method_name)
    return mapping
