# main.py
import regex as re
import math
import random
import asyncio
from collections import defaultdict, deque
from typing import List, Dict, Any

from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star
from astrbot.api import AstrBotConfig, logger
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.message_components import Plain, BaseMessageComponent, Reply, Record
from astrbot.core.star.session_llm_manager import SessionServiceManager


class MessageSplitterPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

        self._message_queues = defaultdict(deque)
        self._last_smart_reply_mark = {}

        self.protected_pairs = []
        for pair_str in self._get_cfg("protected_pairs", []):
            if not pair_str: continue
            chars = str(pair_str)[:2]
            if len(chars) < 2: continue
            self.protected_pairs.append((chars[0], chars[1]))

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

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self._get_cfg("inject_kaomoji_prompt", True): return
        instruction = (
            "\n【特别注意】如果你需要输出颜文字（如 (QAQ)），请务必使用三对反引号包裹，"
            "格式如：```(QAQ)```。这能确保颜文字作为一个整体被发送，不会被分段工具切断。"
        )
        req.system_prompt += instruction

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        setattr(event, "__is_llm_reply", True)

    def _is_model_generated_reply(self, event: AstrMessageEvent, result) -> bool:
        if not result: return False
        is_model_result = getattr(result, "is_model_result", None)
        if callable(is_model_result):
            try: return bool(is_model_result())
            except: pass
        content_type = getattr(result, "result_content_type", None)
        if content_type is not None:
            type_name = getattr(content_type, "name", "")
            return type_name in {"LLM_RESULT", "AGENT_RUNNER_ERROR", "AGENT_RUNNER_RESULT", "TOOL_RESULT", "TOOL_CALL"}
        return getattr(event, "__is_llm_reply", False)

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
        is_llm_reply = self._is_model_generated_reply(event, result)
        if split_scope == "llm_only" and not is_llm_reply:
            logger.debug("[Splitter] 跳过: 非LLM回复 (scope={}, is_llm={})".format(split_scope, is_llm_reply))
            return

        # --- 2. 长度校验 ---
        total_text_len = sum(len(c.text) for c in result.chain if isinstance(c, Plain))
        max_len_no_split = self._get_cfg("max_length_no_split", 0)
        if max_len_no_split > 0 and total_text_len < max_len_no_split:
            logger.debug("[Splitter] 跳过: 文本过短 ({}<{})".format(total_text_len, max_len_no_split))
            return
        max_len_disable = self._get_cfg("max_length_to_disable", 0)
        if max_len_disable > 0 and total_text_len > max_len_disable:
            logger.debug("[Splitter] 跳过: 文本过长 ({}>{})".format(total_text_len, max_len_disable))
            return

        setattr(result, "__splitter_processed", True)
        enable_reply = self._get_cfg("enable_reply", True)
        enable_smart = self._get_cfg("enable_smart_reply", False)
        max_segs = self._get_cfg("max_segments", 7)
        min_seg_cancel = self._get_cfg("min_segment_cancel", 0)

        logger.info("[Splitter] 原文本: {}".format("".join(c.text for c in result.chain if isinstance(c, Plain)).replace('\n', '\\n')))

        # --- 3. 分段前清理 ---
        clean_regex = self._get_cfg("clean_before_regex", "")
        if clean_regex:
            for comp in result.chain:
                if isinstance(comp, Plain) and comp.text:
                    comp.text = re.sub(clean_regex, "", comp.text, flags=re.DOTALL)

        # 脱敏处理
        for comp in result.chain:
            if isinstance(comp, Plain) and comp.text:
                comp.text = comp.text.replace("​ ​", "__ZWSP_DOUBLE__").replace("​", "__ZWSP_SINGLE__")

        # --- 4. 构建正则 ---
        split_pattern = self._get_cfg("split_regex", r"[。？！?!\\n…]+")

        # --- 5. 执行切分 ---
        strategies = {
            "image": self._get_cfg("image_strategy", "单独"),
            "at": self._get_cfg("at_strategy", "跟随下段"),
            "face": self._get_cfg("face_strategy", "嵌入"),
            "default": self._get_cfg("other_media_strategy", "跟随下段"),
        }

        segments = self._split_chain(result.chain, split_pattern, strategies)
        logger.info("[Splitter] 切分完成: {}段, text_len={}".format(len(segments), total_text_len))

        # 碎片段合并
        if min_seg_cancel > 0 and len(segments) > 1:
            i = 0
            while i < len(segments) - 1:
                seg_len = sum(len(c.text) for c in segments[i] if isinstance(c, Plain))
                if seg_len < min_seg_cancel:
                    segments[i + 1] = segments[i] + segments[i + 1]
                    segments.pop(i)
                    logger.debug("[Splitter] 碎片段合并: {}字 < 阈值{}".format(seg_len, min_seg_cancel))
                else:
                    i += 1

        # 强制分段上限控制
        if max_segs > 0 and len(segments) > max_segs:
            merged_last = []
            for seg in segments[max_segs - 1:]:
                merged_last.extend(seg)

            optimized_last = []
            for comp in merged_last:
                if optimized_last and isinstance(comp, Plain) and isinstance(optimized_last[-1], Plain):
                    optimized_last[-1] = Plain(optimized_last[-1].text + comp.text)
                else:
                    optimized_last.append(comp)

            segments = segments[:max_segs - 1] + [optimized_last]

        # --- 6. 回复处理 ---
        source_id = str(getattr(event.message_obj, "message_id", "") or "")

        if enable_reply and segments and source_id:
            if enable_smart:
                if self._should_add_smart_reply(event): self._prepend_reply(segments[0], source_id)
            else:
                self._prepend_reply(segments[0], source_id)

        # --- 7. 后处理 (At/清理/TTS) ---
        at_strategy = strategies.get("at", "跟随下段")
        at_needs_proc = at_strategy in ["接下文", "跟随下段", "嵌入"] and any(type(c).__name__.lower() == "at" for c in result.chain)

        clean_after = self._get_cfg("clean_after_regex", "")
        for seg in segments:
            if self._get_cfg("trim_segment_edge_blank_lines", True): self._trim_segment_edge_blank_lines(seg)
            for comp in seg:
                if isinstance(comp, Plain) and comp.text:
                    comp.text = comp.text.replace("__ZWSP_DOUBLE__", "​ ​").replace("__ZWSP_SINGLE__", "​")
                    if clean_after:
                        comp.text = re.sub(clean_after, "", comp.text, flags=re.DOTALL)

        if len(segments) <= 1 and not at_needs_proc:
            final = segments[0] if segments else []
            result.chain.clear(); result.chain.extend(final); return

        # --- 8. 发送 ---
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

    def _split_chain(self, chain: List[BaseMessageComponent], pattern: str, strategies: Dict[str, str]) -> List[List[BaseMessageComponent]]:
        compiled = re.compile(pattern)
        pair_split_len = self._get_cfg("protected_split_length", 0)
        segments = []; buffer = []
        for comp in chain:
            if isinstance(comp, Plain):
                if not comp.text: continue
                self._process_text(comp.text, compiled, pair_split_len, segments, buffer)
            else:
                c_type = type(comp).__name__.lower()
                if "reply" in c_type:
                    buffer.append(comp)
                    continue
                strategy = strategies.get(c_type, strategies.get("default", "跟随下段"))
                if strategy == "单独":
                    if buffer: segments.append(buffer[:]); buffer.clear()
                    segments.append([comp])
                elif strategy == "跟随上段":
                    if buffer: buffer.append(comp); segments.append(buffer[:]); buffer.clear()
                    elif segments: segments[-1].append(comp)
                    else: segments.append([comp])
                elif strategy in ["跟随下段", "接下文"]:
                    if buffer: segments.append(buffer[:]); buffer.clear()
                    buffer.append(comp)
                else: buffer.append(comp)
        if buffer: segments.append(buffer)
        return [s for s in segments if s]

    def _process_text(self, text: str, compiled, pair_split_len: int, segments: list, buffer: list):
        stack = []; i = 0; n = len(text); chunk = ""

        while i < n:
            if text.startswith("```", i):
                idx = text.find("```", i + 3)
                if idx != -1: chunk += text[i:idx+3]; i = idx+3; continue
                else: chunk += text[i:]; break
            if text.startswith("<" + "think>", i):
                idx = text.find("</" + "think>", i + 7)
                if idx != -1: chunk += text[i:idx+8]; i = idx+8; continue
                else: chunk += text[i:]; break

            match = compiled.match(text, pos=i)
            if match:
                delim = match.group()
                should = not stack or "\n" in delim
                if should and "\n" not in delim and re.match(r"^[ \t.?!,;:\-']+$", delim):
                    p_c = text[i-1] if i > 0 else ""; n_c = text[i+len(delim)] if i+len(delim) < n else ""
                    if re.match(r"^[a-zA-Z0-9 \t.?!,;:\-']$", p_c) and re.match(r"^[a-zA-Z0-9 \t.?!,;:\-']$", n_c): should = False
                if should:
                    chunk += delim; buffer.append(Plain(chunk))
                    segments.append(buffer[:]); buffer.clear(); chunk = ""; i += len(delim)
                else:
                    chunk += delim; i += len(delim)
                continue

            char = text[i]
            pair_closed_pos = -1
            if stack and char == stack[-1][0]:
                pair_closed_pos = stack[-1][1]
                stack.pop()
            else:
                for o, c in self.protected_pairs:
                    if char == o:
                        stack.append((c, i))
                        break

            chunk += char; i += 1

            if pair_closed_pos >= 0 and not stack and pair_split_len > 0 and (i - pair_closed_pos) >= pair_split_len:
                buffer.append(Plain(chunk))
                segments.append(buffer[:]); buffer.clear(); chunk = ""

        if chunk: buffer.append(Plain(chunk))
