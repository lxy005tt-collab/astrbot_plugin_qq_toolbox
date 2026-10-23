"""Per-feature model connections; credentials never enter the provider catalog."""
from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import urlsplit

import aiohttp

NATIVE = "后台模型"
CUSTOM = "自定义接口"
REUSE_IMAGE = "复用画图模块"
ROLES = {
    "reply_mention": ("艾特回应判断", ("reply", "mention_model"), ("reply", "mention_judge_provider_id"), "text"),
    "reply_active": ("主动接话判断", ("reply", "active_context_judge", "model_connection"), ("reply", "active_context_judge", "provider_id"), "text"),
    "reply_followup": ("回复后跟进判断", ("reply", "follow_up", "model_connection"), (), "text"),
    "life_judge": ("日程判断", ("life", "schedule_judge", "model_connection"), ("life", "schedule_judge", "provider_id"), "text"),
    "voice_polish": ("语音表达优化", ("voice", "speech_optimization", "model_connection"), ("voice", "speech_optimization", "provider_id"), "text"),
    "space_writing": ("空间文案", ("space", "writing_model"), ("space", "schedule_provider_id"), "text"),
    "space_diary": ("空间日记写作", ("space", "diary", "model_connection"), (), "text"),
    "image_polish": ("绘图提示词润色", ("image", "polish_model"), (), "text"),
    "image_order": ("改图顺序判断", ("image", "order_model"), (), "text"),
    "image_research": ("绘图资料检索", ("image", "research_model"), (), "research"),
    "image_generation": ("生成与编辑图片", ("image", "generation_model"), (), "image"),
    "space_image": ("空间配图", ("space", "image_model"), (), "space_image"),
}


def at(obj, path, default=None):
    for key in path:
        if not isinstance(obj, dict) or key not in obj:
            return default
        obj = obj[key]
    return obj


def defaults(source=NATIVE):
    return {"source": source, "provider_id": "", "base_url": "", "api_key": "", "model": "", "timeout_seconds": 60}


def api_root(value):
    value = str(value or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("接口地址需填写 http(s) 根地址，不含账号、查询参数或片段，例如 https://example.com/v1。")
    for suffix in ("/chat/completions", "/images/generations", "/images/edits", "/responses"):
        if value.endswith(suffix):
            return value[:-len(suffix)]
    return value


def migrate_model_config(config):
    if config.get("_model_config_version", 0) >= 1:
        return
    image = config.get("image", {})
    for role, (_, path, legacy, kind) in ROLES.items():
        section = config
        for key in path[:-1]:
            section = section.setdefault(key, {})
        binding = section.get(path[-1])
        if not isinstance(binding, dict):
            binding = defaults(REUSE_IMAGE if role == "space_image" else CUSTOM if kind == "image" else NATIVE)
        # Explicitly completed new connections take precedence over legacy fields.
        if not (binding.get("provider_id") or binding.get("base_url") or binding.get("model") or binding.get("api_key")):
            if legacy and at(config, legacy):
                binding["provider_id"] = at(config, legacy)
            if role in ("image_generation", "image_research") and (image.get("api_key") or image.get("model")):
                binding.update(source=CUSTOM, base_url=image.get("base_url", ""), api_key=image.get("api_key", ""),
                    model=image.get("model", "") if role == "image_generation" else image.get("web_research_model", ""))
        section[path[-1]] = binding
    config["_model_config_version"] = 1
    config.save_config()


class ModelRouter:
    def __init__(self, context, config):
        self.context, self.config = context, config

    def settings(self, role):
        _, path, _, _ = ROLES[role]
        return at(self.config, path, {})

    async def generate(self, role, *, prompt, system_prompt="", umo=None, contexts=None, request_max_retries=1):
        settings = self.settings(role)
        if settings.get("source", NATIVE) == NATIVE:
            provider_id = str(settings.get("provider_id", "")).strip()
            if not provider_id:
                if umo:
                    provider_id = await self.context.get_current_chat_provider_id(umo=umo)
                else:
                    provider = await self.context.get_using_provider_async()
                    if provider is None:
                        raise ValueError("没有可用的后台文字模型，请先配置或为此功能填写自定义接口。")
                    provider_id = provider.meta().id
            return await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt, system_prompt=system_prompt,
                contexts=contexts or [], request_max_retries=request_max_retries)
        connection = self.resolve(settings)
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.extend(contexts or [])
        messages.append({"role": "user", "content": prompt})
        headers = {"Authorization": "Bearer " + connection["api_key"]} if connection["api_key"] else {}
        # No SDK retries or alternate-provider fallback: a role owns its endpoint.
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=connection["timeout_seconds"])) as session:
                async with session.post(connection["base_url"] + "/chat/completions", headers=headers,
                    json={"model": connection["model"], "messages": messages, "stream": False}, allow_redirects=False) as response:
                    if response.status != 200:
                        raise ValueError(f"自定义文字接口返回 HTTP {response.status}，请检查此功能的模型配置。")
                    try:
                        body = await response.json(content_type=None)
                    except (ValueError, UnicodeError) as exc:
                        raise ValueError("自定义文字接口未返回有效 JSON。") from exc
            content = body["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("文字接口未返回正文。")
            return SimpleNamespace(completion_text=content)
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("文字接口返回格式不兼容。") from exc
        except aiohttp.ClientError as exc:
            raise ValueError("自定义文字接口连接失败，请检查地址与网络。") from exc

    def resolve(self, settings, *, require_openai=False):
        source = settings.get("source", NATIVE)
        if source == CUSTOM:
            model = str(settings.get("model", "")).strip()
            if not model:
                raise ValueError("请填写此功能使用的模型名称。")
            return {"base_url": api_root(settings.get("base_url")), "api_key": str(settings.get("api_key", "")).strip(),
                    "model": model, "timeout_seconds": max(1, min(600, float(settings.get("timeout_seconds", 60))))}
        if source != NATIVE:
            raise ValueError("请为此功能选择接口来源。")
        provider_id = str(settings.get("provider_id", "")).strip()
        if require_openai and not provider_id:
            raise ValueError("请为此功能选择后台的 OpenAI 兼容模型。")
        if not provider_id:
            provider = self.context.get_using_provider()
        else:
            provider = self.context.get_provider_by_id(provider_id)
        if provider is None:
            raise ValueError("所选后台模型暂不可用，请检查 AstrBot 提供商配置。")
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
        if require_openai and not isinstance(provider, ProviderOpenAIOfficial):
            raise ValueError("生图和联网检索需选择 OpenAI 兼容的后台提供商，或填写自定义接口。")
        provider_config = provider.provider_config
        return {"base_url": api_root(provider_config.get("api_base") or "https://api.openai.com/v1"),
                "api_key": provider.get_current_key(), "model": provider.get_model(),
                "timeout_seconds": max(1, min(600, float(settings.get("timeout_seconds", 60))))}

    def catalog(self):
        from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
        result = {"chat": [], "tts": []}
        for kind, getter in (("chat", "get_all_providers"), ("tts", "get_all_tts_providers")):
            for provider in getattr(self.context, getter, lambda: [])():
                try:
                    meta = provider.meta()
                    result[kind].append({"id": meta.id, "model": provider.get_model(),
                        "openai_compatible": isinstance(provider, ProviderOpenAIOfficial)})
                except Exception:
                    continue
        return result


def validate_model_changes(config, changes):
    changed_paths = [tuple(item["path"]) for item in changes]
    for _, path, _, _ in ROLES.values():
        if not any(item[:len(path)] == path for item in changed_paths):
            continue
        settings = at(config, path, {})
        if settings.get("source") == CUSTOM:
            api_root(settings.get("base_url"))
            if not str(settings.get("model", "")).strip():
                raise ValueError("自定义接口需要填写模型名称。")
