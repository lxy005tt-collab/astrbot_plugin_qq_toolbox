"""Request-local context that AstrBot omits from persisted conversation history."""
from astrbot.core.agent.message import TextPart


def set_temporary_text(request, marker: str, text: str) -> None:
    request.extra_user_content_parts[:] = [
        part for part in request.extra_user_content_parts
        if not getattr(part, "text", "").startswith(marker)
    ]
    if text:
        request.extra_user_content_parts.append(TextPart(text=marker + text).mark_as_temp())
