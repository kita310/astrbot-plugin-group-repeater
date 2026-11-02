"""群聊复读机：同一个群里连续 N 个不同人发相同内容时，机器人跟着复读一次。

改进版逻辑（2026-11-02）：
* 记录连续发送相同内容的所有人（不限时间，只要内容连续）
* 当累积到 N 个不同的人时触发复读（可配置，默认 2）
* 复读后标记这次"复读事件"已完成，即使后面还有人继续发也不再跟
* 出现不同内容时，清空记录，重新开始
* 支持纯文本、QQ 表情、可选的图片复读
* 长度上限可配置（默认 500 字，避免长文被拦截）
"""

from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Face, Image, Plain
from astrbot.api.star import Context, Star, register

MAX_TRACKED_GROUPS = 500


def _int_config(config: dict, key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _image_key(component: Any) -> str:
    raw = str(getattr(component, "url", "") or getattr(component, "file", "") or "")
    return raw.split("?")[0] if raw else ""


def build_fingerprint(
    event: AstrMessageEvent, allow_images: bool = False
) -> tuple[str, list[Any], str] | None:
    try:
        messages = event.get_messages()
    except Exception:
        return None
    if not messages:
        return None

    parts, texts, replay = [], [], []
    for component in messages:
        if isinstance(component, Plain):
            text = (component.text or "").strip()
            if not text:
                continue
            parts.append("t:" + text)
            texts.append(text)
            replay.append(component)
        elif isinstance(component, Face):
            parts.append("f:%s" % getattr(component, "id", ""))
            replay.append(component)
        elif isinstance(component, Image):
            if not allow_images:
                return None
            key = _image_key(component)
            if not key:
                return None
            parts.append("i:" + key)
            replay.append(component)
        else:
            return None
    if not parts:
        return None
    return "|".join(parts), replay, "".join(texts)


@register(
    "astrbot_plugin_group_repeater",
    "kita",
    "群聊复读机：连续N人发相同内容时跟一次",
    "1.3.0",
)
class GroupRepeaterPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        # group_id -> {"fp": 指纹, "senders": [uid列表], "replay": 组件, "done": 是否已复读}
        self._state: OrderedDict[str, dict] = OrderedDict()
        # group_id -> 上次复读时间戳
        self._repeated_at: dict[str, float] = {}

    def _group_allowed(self, group_id: str) -> bool:
        allow = str(self.config.get("allow_groups", "") or "").strip()
        if allow:
            groups = {x.strip() for x in allow.replace("，", ",").split(",")}
            if group_id not in groups:
                return False
        deny = str(self.config.get("deny_groups", "") or "").strip()
        if deny:
            groups = {x.strip() for x in deny.replace("，", ",").split(",")}
            if group_id in groups:
                return False
        return True

    def _trim(self) -> None:
        while len(self._state) > MAX_TRACKED_GROUPS:
            old, _ = self._state.popitem(last=False)
            self._repeated_at.pop(old, None)

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        if not self.config.get("enable", True):
            return
        group_id = event.get_group_id()
        if not group_id or not self._group_allowed(group_id):
            return
        sender_id = str(event.get_sender_id() or "")
        if sender_id and sender_id == str(event.get_self_id() or ""):
            return

        allow_images = bool(self.config.get("repeat_images", False))
        built = build_fingerprint(event, allow_images)
        if built is None:
            return
        fingerprint, replay, text = built

        max_length = _int_config(self.config, "max_length", 500, 1, 5000)
        if len(text) > max_length:
            return

        state = self._state.get(group_id)
        # 内容变了：清空状态，开始新的记录
        if not state or state["fp"] != fingerprint:
            self._state[group_id] = {
                "fp": fingerprint,
                "senders": [sender_id] if sender_id else [],
                "replay": replay,
                "done": False,
            }
            self._trim()
            return

        # 内容相同：检查是不是新人
        senders = state["senders"]
        if sender_id and sender_id not in senders:
            senders.append(sender_id)

        # 已经复读过这次事件了，不再重复
        if state["done"]:
            return

        # 检查是否达到触发人数
        threshold = _int_config(self.config, "repeat_threshold", 2, 2, 10)
        if len(senders) < threshold:
            return

        # 检查冷却
        cooldown = _int_config(self.config, "cooldown_seconds", 20, 0, 3600)
        now = time.time()
        if cooldown and now - self._repeated_at.get(group_id, 0.0) < cooldown:
            logger.debug(f"[复读] 群 {group_id} 冷却中，跳过")
            return

        # 触发复读
        self._repeated_at[group_id] = now
        state["done"] = True
        logger.info(
            "[复读] 群 %s 累积 %d 人发送相同内容，触发复读（内容长度 %d 字）",
            group_id,
            len(senders),
            len(text),
        )
        event.stop_event()
        yield event.chain_result(replay)


