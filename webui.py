"""Authenticated toolbox settings, with native plugin reload and rollback."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from astrbot.api.web import error_response, request

from .runtime import PLUGIN_NAME
from .modules.space.fixed_publisher import parse_when
from .modules.space.interval_publisher import interval_bounds
from .model_routing import validate_model_changes


def revision(config):
    return hashlib.sha256(json.dumps(config, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def secret_field(key, spec):
    return bool(spec.get("secret")) or (spec.get("type") in ("string", "text") and any(word in key.lower() for word in ("api_key", "apikey", "password", "token", "secret")))


def public_config(config, schema, prefix=()):
    values, secrets = {}, []
    for key, spec in schema.items():
        value = config.get(key, deepcopy(spec.get("default")))
        path = (*prefix, key)
        if spec.get("type") == "object":
            values[key], nested = public_config(value or {}, spec.get("items", {}), path)
            secrets.extend(nested)
        elif secret_field(key, spec):
            values[key] = ""
            if value:
                secrets.append(list(path))
        else:
            values[key] = deepcopy(value)
    return values, secrets


def public_schema(schema):
    result = deepcopy(schema)
    for key, spec in result.items():
        if secret_field(key, spec):
            spec.update(default="", secret=True)
        if spec.get("type") == "object":
            spec["items"] = public_schema(spec.get("items", {}))
    return result


def validate_value(value, spec, label):
    kind = spec.get("type")
    valid = {
        "bool": lambda: isinstance(value, bool),
        "int": lambda: type(value) is int,
        "float": lambda: type(value) in (int, float) and math.isfinite(value),
        "string": lambda: isinstance(value, str),
        "text": lambda: isinstance(value, str),
        "list": lambda: isinstance(value, list),
        "template_list": lambda: isinstance(value, list),
    }.get(kind)
    if valid is None or not valid():
        raise ValueError(f"{label} 的格式不正确。")
    if "options" in spec and value not in spec["options"]:
        raise ValueError(f"{label} 需要选择列表中的选项。")
    slider = spec.get("slider", {})
    if kind in ("int", "float") and (("min" in slider and value < slider["min"]) or ("max" in slider and value > slider["max"])):
        raise ValueError(f"{label} 超出允许范围。")
    if kind == "list" and isinstance(spec.get("items"), dict) and "type" in spec["items"]:
        for item in value:
            validate_value(item, spec["items"], label)
    if kind == "template_list":
        templates = spec.get("templates", {})
        for item in value:
            if not isinstance(item, dict):
                raise ValueError(f"{label} 的任务格式不正确。")
            template = templates.get(item.get("__template_key", next(iter(templates), "")))
            if template is None:
                raise ValueError("未知的任务类型。")
            fields = template.get("items", {})
            for name, content in item.items():
                if name in ("id", "__template_key"):
                    if not isinstance(content, str):
                        raise ValueError("任务标识格式不正确。")
                elif name not in fields:
                    raise ValueError("任务包含未知设置项。")
                else:
                    validate_value(content, fields[name], fields[name].get("description", name))


def apply_changes(config, schema, changes):
    if not isinstance(changes, list) or not changes:
        raise ValueError("没有需要保存的设置。")
    candidate = deepcopy(dict(config))
    touched = set()
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("设置变更格式不正确。")
        path = change.get("path")
        if not isinstance(path, list) or not path or not all(isinstance(k, str) for k in path):
            raise ValueError("设置路径不正确。")
        fields, target = schema, candidate
        for key in path[:-1]:
            spec = fields.get(key, {})
            if spec.get("type") != "object":
                raise ValueError("未知设置项。")
            fields = spec.get("items", {})
            target = target.setdefault(key, {})
        key = path[-1]
        if key not in fields or fields[key].get("invisible"):
            raise ValueError("未知或不可编辑的设置项。")
        spec = fields[key]
        value = change.get("value")
        if secret_field(key, spec) and value == "" and not change.get("clear_secret"):
            continue  # Empty password inputs always retain the stored credential.
        validate_value(value, spec, spec.get("description", key))
        target[key] = deepcopy(value)
        touched.add(path[0])
    if "space" in touched:
        space = candidate["space"]
        interval_bounds(space)
        identities = set()
        for task in space.get("fixed_tasks", []):
            if not task.get("enabled", True):
                continue
            if not str(task.get("instruction", "")).strip():
                raise ValueError("定点任务需要填写主题、创作要求或原文。")
            parse_when(str(task.get("when", "")))
            identity = task.get("id") or revision(task)
            if identity in identities:
                raise ValueError("存在重复的定点任务。")
            identities.add(identity)
        if 'diary' in space:
            from .modules.space.diary import diary_time
            diary_time(space['diary'].get('time','23:00'))
    if "observatory" in touched:
        from .modules.observatory.page_api import INT_SETTINGS, FLOAT_SETTINGS
        for key, (low, high) in {**INT_SETTINGS, **FLOAT_SETTINGS}.items():
            if key in candidate["observatory"] and not low <= candidate["observatory"][key] <= high:
                raise ValueError(f"观察台设置 {key} 必须在 {low}～{high} 之间。")
    validate_model_changes(candidate, changes)
    return candidate


class ToolboxWebUI:
    def __init__(self, owner):
        self.owner = owner
        self.schema = json.loads((Path(__file__).parent / "_conf_schema.json").read_text("utf-8"))
        context = owner.context
        if not hasattr(context, "_qq_toolbox_config_lock"):
            context._qq_toolbox_config_lock = asyncio.Lock()
        self.lock = context._qq_toolbox_config_lock

    def register(self):
        for suffix, handler, method in (("suite/config", self.get_config, "GET"), ("suite/config/save", self.save_config, "POST"), ("suite/space/read", self.read_space, "POST"), ("suite/providers", self.providers, "GET"),
                                        ('suite/space/diary/draft',self.diary_draft,'POST'), ('suite/space/diary/publish',self.diary_publish,'POST')):
            route = "/" + PLUGIN_NAME + "/" + suffix
            self.owner.context.register_web_api(route, handler, [method], "QQ Bot 工具箱设置")
            self.owner._routes.append((route, handler, (method,)))

    def snapshot(self):
        config, secrets = public_config(self.owner.config, self.schema)
        return {"config": config, "secrets_set": secrets, "schema": public_schema(self.schema),
                "revision": revision(self.owner.config), "modules": self.owner.DEFINITIONS}

    async def get_config(self):
        if not request.username:
            return error_response("请先登录 AstrBot 管理面板。", status_code=401)
        return {"status": "ok", "data": self.snapshot()}

    async def providers(self):
        if not request.username:
            return error_response("请先登录 AstrBot 管理面板。", status_code=401)
        return {"status": "ok", "data": self.owner.model_router.catalog()}

    async def read_space(self):
        if not request.username:
            return error_response("请先登录 AstrBot 管理面板。", status_code=401)
        space = self.owner.get_component("space")
        if not space or not space.config.get("enabled", True):
            return error_response("请先启用 QQ 空间助手。", status_code=400)
        payload = await request.json(default={})
        if not isinstance(payload, dict) or payload.get("kind") not in ("posts", "guestbook", "comments"):
            return error_response("请选择空间动态、评论或留言板。", status_code=400)
        count, offset = payload.get("count", 10), payload.get("offset", 0)
        if type(count) is not int or type(offset) is not int or not 1 <= count <= 100 or offset < 0:
            return error_response("分页参数不正确。", status_code=400)
        from .modules.space.qzone_client import QzoneError
        try:
            client = await space._client()
            if payload["kind"] == "posts":
                data = await client.list_posts(count, offset)
            elif payload["kind"] == "guestbook":
                data = await client.guestbook(count, offset)
            else:
                data = await client.detail(str(payload.get("post_id", "")))
            return {"status": "ok", "data": data}
        except QzoneError as exc:
            return error_response(str(exc), status_code=400)
        except Exception:
            return error_response("空间读取失败，请检查 QQ 连接后重试。", status_code=502)

    async def diary_draft(self):
        return await self._diary_action(False)

    async def diary_publish(self):
        return await self._diary_action(True)

    async def _diary_action(self,publish):
        if not request.username:
            return error_response('请先登录 AstrBot 管理面板。',status_code=401)
        space = self.owner.get_component('space')
        if not space:
            return error_response('请先启用 QQ 空间助手。',status_code=400)
        from .modules.space.qzone_client import QzoneError
        try:
            if publish:
                payload = await request.json(default={})
                day = payload.get('day') if isinstance(payload,dict) else None
                entry = space._diary_store.entry(day) if isinstance(day,str) else None
                if not entry or not entry['plan'].get('content'):
                    raise QzoneError('请先生成日记草稿，查看内容后再发布。')
                data = await space.publish_diary(day)
            else:
                data = await space.prepare_diary()
            return {'status':'ok','data':data}
        except QzoneError as exc:
            return error_response(str(exc),status_code=400)
        except Exception:
            return error_response('日记操作失败，请检查日记模型和空间连接。',status_code=502)

    async def _reload(self, manager):
        # Native reload(name) falls back to reloading ALL plugins if name is missing.
        if self.owner.context.get_registered_star(PLUGIN_NAME):
            return await manager.reload(PLUGIN_NAME)
        return await manager.reload_failed_plugin(PLUGIN_NAME)

    async def save_config(self):
        if not request.username:
            return error_response("请先登录 AstrBot 管理面板。", status_code=401)
        payload = await request.json(default={})
        if not isinstance(payload, dict):
            return error_response("请求格式不正确。", status_code=400)
        async with self.lock:
            config = self.owner.config
            if payload.get("revision") != revision(config):
                return error_response("设置已在其他页面更新，请重新读取后再保存。", status_code=409)
            current = self.owner.context.get_registered_star(PLUGIN_NAME)
            if current and current.star_cls is not self.owner:
                return error_response("插件已经重载，请重新读取设置。", status_code=409)
            config_path = getattr(config, "config_path", None)
            if config_path and revision(json.loads(Path(config_path).read_text("utf-8-sig"))) != revision(config):
                return error_response("磁盘设置已更新，请在 AstrBot 重载插件后再保存。", status_code=409)
            try:
                candidate = apply_changes(config, self.schema, payload.get("changes"))
            except Exception as exc:
                # Validation errors contain labels only, never credential values.
                from .modules.space.qzone_client import QzoneError
                if isinstance(exc, (ValueError, QzoneError)):
                    return error_response(str(exc), status_code=400)
                raise
            manager = getattr(self.owner.context, "_star_manager", None)
            if manager is None or not current:
                return error_response("插件管理器暂不可用，请在 AstrBot 插件设置页保存。", status_code=503)
            previous = deepcopy(dict(config))
            config.clear()
            config.update(candidate)
            try:
                config.save_config()
                ok, _ = await self._reload(manager)
                if not ok:
                    raise RuntimeError("reload failed")
            except Exception:
                config.clear()
                config.update(previous)
                restored = False
                try:
                    config.save_config()
                    restored, _ = await self._reload(manager)
                except Exception:
                    pass
                message = "应用失败，已恢复原设置。" if restored else "应用失败，已尝试恢复原设置，请在 AstrBot 插件页检查加载状态。"
                return error_response(message, status_code=500)
            return {"status": "ok", "data": {"applied": True, "message": "已保存并应用，八个模块使用最新设置。"}}
