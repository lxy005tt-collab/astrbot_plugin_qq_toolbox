"""Collect only private exchanges whose normal QQ reply reached the dispatcher."""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from functools import wraps
import time

from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent


WATCH = 'qq_toolbox_diary_watch'


class PrivateDiaryMemory:
    def __init__(self, space, store):
        self.space, self.store = space, store
        self.intent = ContextVar(f'private_diary_{id(self)}',default=None)
        self.original = self.wrapper = None

    def install(self):
        self.store.start_collection()
        self.original = AiocqhttpMessageEvent.__dict__['_dispatch_send']
        original = self.original.__func__

        @wraps(original)
        async def dispatch(cls, bot, event, is_group, session_id, messages):
            result = await original(cls,bot,event,is_group,session_id,messages)
            intent = self.intent.get()
            target = session_id if session_id is not None else (event.get('user_id') if event else None)
            if intent is None or is_group or str(target) != intent['target']:
                return result
            text = ''.join(str(item.get('data',{}).get('text','')) for item in messages if item.get('type') == 'text')
            if any(item.get('type') == 'record' for item in messages):
                voice = self.space.context.owner.get_component('voice')
                receipt = voice._sending_voice.get() if voice else None
                if receipt and receipt['umo'] == intent['scope'] and receipt['source_message_id'] == intent['source_id']:
                    text += receipt['text']
            if not text.strip() and any(item.get('type') == 'image' for item in messages):
                text = '[发送了一张图片，未记录图片内容]'
            if text.strip():
                try:
                    await asyncio.to_thread(self.store.add_memory, scope=intent['scope'],persona=intent['persona'],
                                            source_id=intent['source_id'],incoming=intent['incoming'],outgoing=text,ts=time.time())
                except Exception as exc:
                    self.space.logger.warning('日记私聊素材记录失败（%s），正常聊天不受影响。',type(exc).__name__)
            return result

        self.wrapper = dispatch
        AiocqhttpMessageEvent._dispatch_send = classmethod(dispatch)

    def close(self):
        current = AiocqhttpMessageEvent.__dict__['_dispatch_send']
        if self.original is not None and current.__func__ is self.wrapper:
            AiocqhttpMessageEvent._dispatch_send = self.original
        self.original = self.wrapper = None

    async def watch(self, event, request):
        if event.get_platform_name() != 'aiocqhttp' or event.get_group_id() or event.get_extra(WATCH):
            return
        # Only the account selected for this space may supply diary material.
        if str(event.get_platform_id()) != str(self.space._platform().meta().id):
            return
        persona_id, persona, _, _ = await self.space.context.persona_manager.resolve_selected_persona(
            umo=event.unified_msg_origin, conversation_persona_id=getattr(getattr(request,'conversation',None),'persona_id',None),
            platform_name=event.get_platform_name(), provider_settings=self.space.context.get_config(umo=event.unified_msg_origin),
        )
        if persona is None and persona_id == 'default':
            persona = await self.space.context.persona_manager.get_default_persona_v3(umo=event.unified_msg_origin)
        if persona is None:
            return
        target_persona = str(self.space.config.get('diary',{}).get('persona_id')
                             or self.space.config.get('schedule_persona_id') or '').strip()
        if not target_persona:
            default = await self.space.context.persona_manager.get_default_persona_v3()
            target_persona = str(default.get('name') or 'default')
        if str(persona.get('name') or persona_id or 'default') != target_persona:
            return
        intent = dict(scope=event.unified_msg_origin,target=str(event.get_sender_id()),
                      source_id=str(event.message_obj.message_id),incoming=event.get_message_str(),
                      persona=str(persona.get('name') or persona_id or 'default'))
        original_send = event.send

        @wraps(original_send)
        async def send(message):
            token = self.intent.set(intent)
            try:
                return await original_send(message)
            finally:
                self.intent.reset(token)

        event.send = send
        event.set_extra(WATCH,True)
