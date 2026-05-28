# main.py
import math
import random
import asyncio
import re
from collections import defaultdict, deque
from typing import List, Dict, Any

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig, logger
from astrbot.api.message_components import Plain, BaseMessageComponent, Reply, Record
from astrbot.core.star.session_llm_manager import SessionServiceManager


class MessageSplitterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._message_queues = defaultdict(deque)
        self._last_smart_reply_mark = {}

    def _get_cfg(self, key: str, default: Any = None) -> Any:
        categories = [
            "basic_settings", "split_settings", "clean_settings",
            "reply_media_settings", "delay_settings"
        ]
        for cat in categories:
            cat_obj = self.config.get(cat)
            if isinstance(cat_obj, dict) and key in cat_obj:
                return cat_obj[key]
        return self.config.get(key, default)

    def _get_conversation_key(self, event: AstrMessageEvent) -> str:
        return str(getattr(event, "unified_msg_origin", "") or "")

    def _get_message_queue(self, event: AstrMessageEvent):
        return self._message_queues[self._get_conversation_key(event)]

    def _remember_incoming_message(self, event: AstrMessageEvent) -> None:
        message_id = getattr(event.message_obj, "message_id", None)
        if not message_id: return
        queue = self._get_message_queue(event)
        queue.append(str(message_id))
        if len(queue) > 200: queue.popleft()

    def _mark_bot_reply(self, event: AstrMessageEvent, base_message_id: str) -> None:
        if not base_message_id: return
        conv_key = self._get_conversation_key(event)
        mark = "__bot_reply__{}".format(base_message_id)
        queue = self._message_queues[conv_key]
        if self._last_smart_reply_mark.get(conv_key) != mark:
            queue.append(mark)
            self._last_smart_reply_mark[conv_key] = mark
            if len(queue) > 200: queue.popleft()

    def _should_add_smart_reply(self, event: AstrMessageEvent) -> bool:
        if not self._get_cfg("enable_smart_reply", False): return False
        platform_name = str(getattr(event, "get_platform_name", lambda: "")() or "")
        if platform_name.lower() == "dingtalk": return False
        message_id = getattr(event.message_obj, "message_id", None)
        if not message_id: return False
        queue = self._get_message_queue(event)
        queue_str = [str(x) for x in queue]
        msg_id = str(message_id)
        if msg_id not in queue_str: return False
        idx = queue_str.index(msg_id)
        pushed = len(queue_str) - idx - 1
        return pushed > 0

    def _has_reply_component(self, chain: List[BaseMessageComponent]) -> bool:
        return any(isinstance(c, Reply) for c in chain)

    def _prepend_reply(self, chain: List[BaseMessageComponent], message_id: str) -> None:
        if message_id and not self._has_reply_component(chain):
            chain.insert(0, Reply(id=message_id))

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def on_message(self, event: AstrMessageEvent):
        self_id_getter = getattr(event, "get_self_id", None)
        sender_id_getter = getattr(event, "get_sender_id", None)
        try:
            self_id = self_id_getter() if callable(self_id_getter) else None
            sender_id = sender_id_getter() if callable(sender_id_getter) else None
        except:
            self_id, sender_id = None, None
        if self_id is not None and sender_id is not None and str(sender_id) == str(self_id):
            return
        self._remember_incoming_message(event)

    @filter.on_decorating_result(priority=-100000000000000000)
    async def on_decorating_result(self, event: AstrMessageEvent):
        result = event.get_result()
        if not result or not result.chain:
            logger.debug("[Splitter] 跳过: result 或 chain 为空")
            return
        if getattr(result, "__splitter_processed", False):
            logger.debug("[Splitter] 跳过: 已处理过")
            return

        # --- 1. 基础校验 ---
        umo = event.unified_msg_origin
        blacklist = self._get_cfg("conversation_blacklist", [])
        whitelist = self._get_cfg("conversation_whitelist", [])
        if umo in blacklist:
            logger.debug("[Splitter] 跳过: 对话在黑名单中")
            return
        if whitelist and umo not in whitelist:
            logger.debug("[Splitter] 跳过: 对话不在白名单中")
            return
        if not self._get_cfg("enable_group_split", True) and event.message_obj.group_id:
            logger.debug("[Splitter] 跳过: 群聊分段已关闭")
            return

        split_scope = self._get_cfg("split_scope", "llm_only")
        if split_scope == "llm_only" and not result.is_llm_result():
            logger.debug("[Splitter] 跳过: 非LLM回复 (scope={})".format(split_scope))
            return

        # --- 2. 长度校验 ---
        total_text_len = sum(len(c.text) for c in result.chain if isinstance(c, Plain))
        max_len_disable = self._get_cfg("max_length_to_disable", 0)
        if max_len_disable > 0 and total_text_len > max_len_disable:
            logger.debug("[Splitter] 跳过: 文本过长 ({}>{})".format(total_text_len, max_len_disable))
            return

        setattr(result, "__splitter_processed", True)
        enable_reply = self._get_cfg("enable_reply", True)
        enable_smart = self._get_cfg("enable_smart_reply", False)

        logger.info("[Splitter] 原文本: {}".format("".join(c.text for c in result.chain if isinstance(c, Plain)).replace('\n', '\\n')))

        # --- 3. 分段前清理 ---
        clean_regex = self._get_cfg("clean_before_regex", "")
        if clean_regex:
            for comp in result.chain:
                if isinstance(comp, Plain) and comp.text:
                    comp.text = re.sub(clean_regex, "", comp.text, flags=re.DOTALL)

        # --- 5. 归一化转义换行 ---
        for comp in result.chain:
            if isinstance(comp, Plain) and comp.text:
                comp.text = comp.text.replace("\\n", "\n").replace("\\r", "\r")

        # --- 6. 切分 ---
        strategies = {
            "image": self._get_cfg("image_strategy", "单独"),
            "at": self._get_cfg("at_strategy", "跟随下段"),
            "face": self._get_cfg("face_strategy", "嵌入"),
            "default": self._get_cfg("other_media_strategy", "跟随下段"),
        }

        segments = self._split_chain(result.chain, strategies)
        logger.info("[Splitter] 切分完成: {}段, text_len={}".format(len(segments), total_text_len))

        # --- 7. 回复处理 ---
        source_id = str(getattr(event.message_obj, "message_id", "") or "")

        if enable_reply and segments and source_id:
            if enable_smart:
                if self._should_add_smart_reply(event): self._prepend_reply(segments[0], source_id)
            else:
                self._prepend_reply(segments[0], source_id)

        # --- 8. 后处理 (At/清理/TTS) ---
        at_strategy = strategies.get("at", "跟随下段")
        at_needs_proc = at_strategy in ["接下文", "跟随下段", "嵌入"] and any(type(c).__name__.lower() == "at" for c in result.chain)

        clean_after_chars = "".join(self._get_cfg("clean_after_chars", []))
        for seg in segments:
            if self._get_cfg("trim_segment_edge_blank_lines", True): self._trim_segment_edge_blank_lines(seg)
            if clean_after_chars:
                for comp in seg:
                    if isinstance(comp, Plain) and comp.text:
                        comp.text = comp.text.rstrip(clean_after_chars)
                for comp in seg:
                    if isinstance(comp, Plain) and comp.text:
                        comp.text = comp.text.rstrip("".join(clean_after_chars))

        if len(segments) <= 1 and not at_needs_proc:
            final = segments[0] if segments else []
            result.chain.clear(); result.chain.extend(final); return

        # --- 9. 发送 ---
        for i in range(len(segments) - 1):
            seg_chain = segments[i]
            text_content = "".join([c.text for c in seg_chain if isinstance(c, Plain)])
            if not text_content.strip(" \t\r\n​") and not any(not isinstance(c, Plain) for c in seg_chain): continue

            try:
                seg_chain = await self._process_tts_for_segment(event, seg_chain)
                self._log_segment(i + 1, len(segments), seg_chain, "主动发送")
                mc = MessageChain(); mc.chain = seg_chain
                await self.context.send_message(event.unified_msg_origin, mc)
                await asyncio.sleep(self.calculate_delay(text_content))
            except Exception as e:
                logger.error(f"[Splitter] 发送失败: {e}")

        if enable_reply and enable_smart and source_id: self._mark_bot_reply(event, source_id)

        last_seg = segments[-1]
        result.chain.clear(); result.chain.extend(last_seg)

    def _log_segment(self, index: int, total: int, chain: List[BaseMessageComponent], method: str):
        content = "".join([c.text if isinstance(c, Plain) else f"[{type(c).__name__}]" for c in chain])
        logger.info("[Splitter] 第 {}/{} 段 ({}): {}".format(index, total, method, content.replace('\n', '\\n')))

    def _trim_segment_edge_blank_lines(self, segment: List[BaseMessageComponent]) -> None:
        f_p = next((c for c in segment if isinstance(c, Plain)), None)
        l_p = next((c for c in reversed(segment) if isinstance(c, Plain)), None)
        if f_p and f_p.text: f_p.text = re.sub(r'^(?:[ \t]*\r?\n)+', '', f_p.text)
        if l_p and l_p.text: l_p.text = re.sub(r'(?:\r?\n[ \t]*)+$', '', l_p.text)

    async def _process_tts_for_segment(self, event: AstrMessageEvent, segment: List[BaseMessageComponent]) -> List[BaseMessageComponent]:
        if not self._get_cfg("enable_tts_for_segments", True): return segment
        try:
            all_cfg = self.context.get_config(event.unified_msg_origin)
            tts_cfg = all_cfg.get("provider_tts_settings", {})
            if not tts_cfg.get("enable", False): return segment
            tts_prov = self.context.get_using_tts_provider(event.unified_msg_origin)
            if not tts_prov or not await SessionServiceManager.should_process_tts_request(event): return segment
            if random.random() > float(tts_cfg.get("trigger_probability", 1.0)): return segment
            dual = tts_cfg.get("dual_output", False)
            new_seg = []
            for comp in segment:
                if isinstance(comp, Plain) and len(comp.text) > 1:
                    try:
                        path = await tts_prov.get_audio(comp.text)
                        if path:
                            new_seg.append(Record(file=path, url=path))
                            if dual: new_seg.append(comp)
                        else: new_seg.append(comp)
                    except: new_seg.append(comp)
                else: new_seg.append(comp)
            return new_seg
        except: return segment

    def calculate_delay(self, text: str) -> float:
        strategy = self._get_cfg("delay_strategy", "linear")
        if strategy == "random": return random.uniform(self._get_cfg("random_min", 1.0), self._get_cfg("random_max", 3.0))
        if strategy == "log": return min(self._get_cfg("log_base", 0.5) + self._get_cfg("log_factor", 0.8) * math.log(len(text) + 1), 5.0)
        if strategy == "linear": return self._get_cfg("linear_base", 0.5) + (len(text) * self._get_cfg("linear_factor", 0.1))
        return self._get_cfg("fixed_delay", 1.5)

    def _split_chain(self, chain: List[BaseMessageComponent], strategies: Dict[str, str]) -> List[List[BaseMessageComponent]]:
        segments = []
        buffer = []
        for comp in chain:
            if isinstance(comp, Plain):
                if not comp.text:
                    continue
                parts = comp.text.split('\n')
                for j, part in enumerate(parts):
                    if j > 0:
                        if buffer:
                            segments.append(buffer[:])
                            buffer.clear()
                    if part:
                        buffer.append(Plain(part))
            else:
                c_type = type(comp).__name__.lower()
                if "reply" in c_type:
                    buffer.append(comp)
                    continue
                strategy = strategies.get(c_type, strategies.get("default", "跟随下段"))
                if strategy == "单独":
                    if buffer:
                        segments.append(buffer[:])
                        buffer.clear()
                    segments.append([comp])
                elif strategy == "跟随上段":
                    if buffer:
                        buffer.append(comp)
                        segments.append(buffer[:])
                        buffer.clear()
                    elif segments:
                        segments[-1].append(comp)
                    else:
                        segments.append([comp])
                elif strategy in ["跟随下段", "接下文"]:
                    if buffer:
                        segments.append(buffer[:])
                        buffer.clear()
                    buffer.append(comp)
                else:
                    buffer.append(comp)
        if buffer:
            segments.append(buffer)
        return [s for s in segments if s]
