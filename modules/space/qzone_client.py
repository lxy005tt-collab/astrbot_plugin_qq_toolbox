"""Small Qzone client. Protocol fields adapted from Zhalslar/astrbot_plugin_qzone.

See THIRD_PARTY.md. Never log Cookie, request forms, or unfiltered responses.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import html
import io
import re
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import url2pathname

import aiohttp
import json5
from PIL import Image

BASE = "https://user.qzone.qq.com"
LIST_URL = BASE + "/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msglist_v6"
DETAIL_URL = BASE + "/proxy/domain/taotao.qq.com/cgi-bin/emotion_cgi_msgdetail_v6"
BOARD_URL = BASE + "/proxy/domain/m.qzone.qq.com/cgi-bin/new/get_msgb"
UPLOAD_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
PUBLISH_URL = BASE + "/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_publish_v6"
COMMENT_URL = BASE + "/proxy/domain/taotao.qzone.qq.com/cgi-bin/emotion_cgi_re_feeds"
MAX_IMAGE_BYTES = 10 * 1024 * 1024


class QzoneError(ValueError):
    """A safe, user-visible error."""


class PublishUnknown(QzoneError):
    """The server may have accepted a post; do not retry automatically."""


def parse_response(text: str) -> dict:
    text = text.strip().lstrip("\ufeff")
    # JSONP and frameElement.callback(...) are data wrappers, never executable code.
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start or text.startswith("<"):
        raise QzoneError("空间未返回可识别的数据，可能需要重新登录 QQ 或完成空间验证。")
    try:
        data = json5.loads(text[start:end + 1])
    except (ValueError, RecursionError) as exc:
        raise QzoneError("空间响应格式异常，请稍后查看空间状态。") from exc
    if not isinstance(data, dict):
        raise QzoneError("空间响应不是有效的数据对象。")
    return data


def plain(value) -> str:
    text = str(value or "")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]*>", "", text)
    return html.unescape(text).strip()


def body(raw: dict) -> dict:
    return {**raw, **raw["data"]} if isinstance(raw.get("data"), dict) else raw


def comment_record(item: dict) -> dict:
    replies = item.get("list_3") or item.get("replyList") or []
    return {
        "id": str(item.get("tid") or item.get("id") or item.get("commentid") or item.get("commentId") or ""),
        "qq": str(item.get("uin") or ""),
        "name": plain(item.get("name") or item.get("nickname")),
        "text": plain(item.get("content") or item.get("ubbContent") or item.get("htmlContent")),
        "time": str(item.get("createTime2") or item.get("createTime") or item.get("pubtime") or item.get("created_time") or ""),
        "private": str(item.get("private") or item.get("isprivate") or "0").lower() in {"1", "true"},
        "replies": [comment_record(x) for x in replies if isinstance(x, dict)],
    }


def post_record(item: dict, uin: str) -> dict:
    tid = str(item.get("tid") or item.get("t1_tid") or "")
    return {
        "id": tid,
        "qq": str(item.get("uin") or uin),
        "text": plain(item.get("content")),
        "time": str(item.get("created_time") or item.get("createTime") or ""),
        "url": f"{BASE}/{uin}/mood/{tid}",
        "comment_count": int(item.get("cmtnum") or item.get("commentnum") or 0),
        "comments": [comment_record(x) for x in item.get("commentlist") or [] if isinstance(x, dict)],
    }


def validate_image(raw: bytes) -> bytes:
    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise QzoneError("单张配图须小于等于 10 MB，且不能为空。")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
                raise QzoneError("配图只支持 JPEG、PNG、WebP、GIF。")
            image.verify()
        # The upload CGI is most reliable with JPEG/PNG. GIF uses its first frame.
        with Image.open(io.BytesIO(raw)) as image:
            if image.format in {"JPEG", "PNG"}:
                return raw
            buffer = io.BytesIO()
            image.convert("RGBA").save(buffer, format="PNG")
            converted = buffer.getvalue()
            if len(converted) > MAX_IMAGE_BYTES:
                raise QzoneError("转换后的配图超过 10 MB，请缩小图片。")
            return converted
    except (OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise QzoneError("配图损坏或不是有效图片，未发布说说。") from exc


async def load_image(source: str) -> bytes:
    source = str(source).strip()
    try:
        if source.startswith("base64://"):
            raw = base64.b64decode(source[9:], validate=True)
        elif source.startswith("data:image/"):
            raw = base64.b64decode(source.split(",", 1)[1], validate=True)
        elif source.startswith(("https://", "http://")):
            # Separate, credential-free session: Qzone cookies never go to image hosts.
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
                async with session.get(source) as response:
                    response.raise_for_status()
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        data.extend(chunk)
                        if len(data) > MAX_IMAGE_BYTES:
                            raise QzoneError("配图超过 10 MB，未发布说说。")
                    raw = bytes(data)
        else:
            if source.startswith("file:"):
                parsed = urlparse(source)
                source = url2pathname(parsed.path)
                if parsed.netloc:
                    source = "//" + parsed.netloc + source
            path = Path(source).expanduser()
            if not path.is_absolute() or not path.is_file():
                raise QzoneError("配图不存在，请使用图片链接或本机图片的绝对路径。")
            if path.stat().st_size > MAX_IMAGE_BYTES:
                raise QzoneError("配图超过 10 MB，未发布说说。")
            raw = await asyncio.to_thread(path.read_bytes)
        return await asyncio.to_thread(validate_image, raw)
    except QzoneError:
        raise
    except (OSError, ValueError, IndexError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        raise QzoneError("配图读取失败，未发布说说；请检查图片是否过期或无法下载。") from exc


class QzoneClient:
    def __init__(self, onebot, timeout: int = 30):
        self.onebot = onebot
        self.timeout = timeout
        self.uin = ""
        self.cookies: dict[str, str] = {}
        self.gtk = ""

    async def login(self):
        try:
            result = await asyncio.wait_for(self.onebot.get_cookies(domain="user.qzone.qq.com"), timeout=20)
            if isinstance(result.get("data"), dict):
                result = result["data"]
            self.cookies = {k: v.value for k, v in SimpleCookie(result.get("cookies") or "").items()}
        except Exception as exc:
            raise QzoneError("NapCat 无法取得空间登录态，请确认机器人 QQ 在线。") from exc
        self.uin = self.cookies.get("uin", "").lstrip("oO0")
        secret = self.cookies.get("p_skey") or self.cookies.get("skey")
        if not self.uin.isdigit() or not secret:
            raise QzoneError("空间 Cookie 不完整，请重新登录 NapCat 的 QQ，或检查是否已开通空间。")
        value = 5381
        for ch in secret:
            value = ((value << 5) + value + ord(ch)) & 0x7fffffff
        self.gtk = str(value)

    def _safe_message(self, message) -> str:
        text = plain(message)
        for secret in [*self.cookies.values(), self.gtk]:
            if secret and len(secret) >= 4:
                text = text.replace(secret, "[REDACTED_SECRET]")
        text = re.sub(r"(?i)(p_skey|skey|cookie|token|g_tk)\s*[:=]\s*\S+", r"\1=[REDACTED_SECRET]", text)
        return text[:240]

    async def _request(self, method: str, url: str, *, params=None, data=None, publishing=False) -> dict:
        try:
            async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), timeout=aiohttp.ClientTimeout(total=self.timeout)) as session:
                async with session.request(
                    method, url, params=params, data=data, cookies=self.cookies,
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/138.0.0.0 Safari/537.36",
                             "Referer": f"{BASE}/{self.uin}", "Origin": BASE},
                    allow_redirects=False,
                ) as response:
                    if response.status != 200:
                        if publishing and response.status >= 500:
                            raise PublishUnknown("发布响应异常，结果未知；请先到 QQ 空间确认，勿立即重发。")
                        raise QzoneError(f"空间请求失败（HTTP {response.status}），请检查登录状态或空间验证。")
                    text = await response.text(errors="replace")
            try:
                raw = parse_response(text)
            except QzoneError as exc:
                if publishing:
                    raise PublishUnknown("发布响应无法确认；请先到 QQ 空间查看，勿立即重发。") from exc
                raise
            code = raw.get("code", raw.get("ret"))
            nested = raw.get("data")
            nested_code = nested.get("ret", nested.get("code")) if isinstance(nested, dict) else None
            if code is None:
                if publishing:
                    raise PublishUnknown("空间未返回明确发布状态；请先到空间确认，勿立即重发。")
                raise QzoneError("空间未返回明确状态，请重新登录 QQ 空间后重试。")
            if str(code) != "0" or (nested_code is not None and str(nested_code) != "0"):
                failed_code = code if str(code) != "0" else nested_code
                message = self._safe_message(raw.get("message") or raw.get("msg") or (nested.get("msg") if isinstance(nested, dict) else ""))
                raise QzoneError(f"空间接口失败（{failed_code}）：{message or '登录失效、无访问权限或需要空间验证。'}")
            return body(raw)
        except QzoneError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, UnicodeError) as exc:
            if publishing:
                raise PublishUnknown("发布连接中断，结果未知；请先到 QQ 空间确认，勿立即重发。") from exc
            raise QzoneError("连接 QQ 空间失败或超时，请稍后重试。") from exc

    async def list_posts(self, count: int = 5, offset: int = 0, *, include_private_comments=True) -> dict:
        raw = await self._request("GET", LIST_URL, params={
            "g_tk": self.gtk, "uin": self.uin, "ftype": 0, "sort": 0,
            "pos": offset, "num": count, "replynum": 100, "format": "json",
            "code_version": 1, "need_comment": 1, "need_private_comment": int(include_private_comments),
        })
        return {"total": int(raw.get("total") or 0), "offset": offset,
                "posts": [post_record(x, self.uin) for x in raw.get("msglist") or [] if isinstance(x, dict)]}

    async def detail(self, tid: str, *, include_private_comments=True) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tid):
            raise QzoneError("说说 ID 格式无效，请先查看空间动态获取 ID。")
        raw = await self._request("GET", DETAIL_URL, params={"g_tk": self.gtk, "uin": self.uin, "tid": tid, "format": "json",
            "need_private_comment": int(include_private_comments)})
        return post_record({**raw, "tid": tid}, self.uin)

    async def reply_comment(self, post: dict, parent: dict, target: dict, content: str) -> dict:
        if str(post.get('qq')) != self.uin:
            raise QzoneError('只允许回复机器人自己空间下的评论。')
        tid, comment_id = str(post.get('id', '')), str(parent.get('id', ''))
        if not all(re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) for value in (tid, comment_id)):
            raise QzoneError('说说或主评论 ID 无效，未提交评论。')
        target_qq, parent_qq = str(target.get('qq', '')), str(parent.get('qq', ''))
        if not all(value.isascii() and value.isdigit() for value in (self.uin, target_qq, parent_qq)):
            raise QzoneError('评论作者信息无效，未提交评论。')
        if target_qq == self.uin or parent.get('private') or target.get('private'):
            raise QzoneError('不自动回复自己或私密评论。')
        content = content.strip()
        if not 0 < len(content) <= 300 or '@{' in content:
            raise QzoneError('评论正文无效，未提交评论。')
        nick = re.sub(r'[,{}\r\n]', ' ', plain(target.get('name') or target_qq))[:40]
        mention = f'@{{uin:{target_qq},nick:{nick},auto:1}} '
        await self._request('POST', COMMENT_URL, params={'g_tk': self.gtk}, data={
            'hostUin': self.uin, 'topicId': f'{self.uin}_{tid}', 'content': mention + content,
            'format': 'json', 'qzreferrer': f'{BASE}/{self.uin}/main', 'commentId': comment_id,
            'replyUin': target_qq, 'commentUin': parent_qq,
        }, publishing=True)
        return {'accepted': True, 'post_id': tid, 'parent_comment_id': comment_id, 'reply_to': target_qq}

    async def guestbook(self, count: int = 10, offset: int = 0) -> dict:
        raw = await self._request("GET", BOARD_URL, params={
            "g_tk": self.gtk, "uin": self.uin, "hostUin": self.uin,
            "start": offset, "num": count, "format": "json", "inCharset": "utf-8", "outCharset": "utf-8",
        })
        return {"total": int(raw.get("total") or 0), "offset": offset,
                "messages": [comment_record(x) for x in raw.get("commentList") or [] if isinstance(x, dict)]}

    async def publish(self, content: str, images: list[bytes]) -> dict:
        data = {"syn_tweet_verson": "1", "paramstr": "1", "who": "1", "con": content,
                "feedversion": "1", "ver": "1", "ugc_right": "1", "to_sign": "0",
                "hostuin": self.uin, "code_version": "1", "format": "json", "qzreferrer": f"{BASE}/{self.uin}"}
        pic_bos, richvals = [], []
        for image in images:
            upload = await self._request("POST", UPLOAD_URL, data={
                "filename": "filename", "uploadtype": "1", "albumtype": "7", "skey": self.cookies.get("skey", ""),
                "uin": self.uin, "p_skey": self.cookies.get("p_skey", ""), "output_type": "json", "base64": "1",
                "picfile": base64.b64encode(image).decode("ascii"),
            })
            try:
                pic_bo = parse_qs(urlparse(upload["url"]).query)["bo"][0]
                fields = [upload[k] for k in ("albumid", "lloc", "sloc", "type", "height", "width")]
                richval = "," + ",".join(str(x) for x in fields) + f",,{upload['height']},{upload['width']}"
            except (KeyError, IndexError, TypeError) as exc:
                raise QzoneError("图片上传未返回完整凭据，未发布说说。") from exc
            pic_bos.append(pic_bo)
            richvals.append(richval)
        if images:
            data.update(pic_bo=",".join(pic_bos), richtype="1", richval="\t".join(richvals))
        raw = await self._request("POST", PUBLISH_URL, params={"g_tk": self.gtk, "uin": self.uin}, data=data, publishing=True)
        tid = str(raw.get("tid") or raw.get("t1_tid") or "")
        if not tid:
            raise PublishUnknown("空间返回成功但缺少说说 ID，请先到空间确认，勿立即重发。")
        return {"id": tid, "url": f"{BASE}/{self.uin}/mood/{tid}", "image_count": len(images)}


def post_fingerprint(message_key: str, content: str, images: list[bytes]) -> str:
    digest = hashlib.sha256((message_key + "\0" + content).encode())
    for image in images:
        digest.update(hashlib.sha256(image).digest())
    return digest.hexdigest()
