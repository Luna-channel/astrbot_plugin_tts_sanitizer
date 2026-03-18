from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.config.astrbot_config import AstrBotConfig
import asyncio
import re
from typing import Optional, Dict, Any

# 内置默认值（与 _conf_schema.json 保持一致）
DEFAULT_REMOVE_PATTERNS = [
    r"[（(][^（()]*[）)]",
    r"[＞>][＿_][＜<]",
    r"[＾^][＿_][＾^]",
    r"[oO][＿_][oO]",
    r"[xX][＿_][xX]",
    r"[－-][＿_][－-]",
    r"[★☆♪♫♬♩♡♥❤️💖💕💗💓💝💟💜💛💚💙🧡🤍🖤🤎💔❣️💋]",
    r"[→←↑↓↖↗↘↙↔↕↺↻]",
]

DEFAULT_FILTER_WORDS = [
    "ω", "Ω", "σ", "Σ", "ε", "д", "Д",
    "´", "`", "＝", "∀", "∇",
    "orz", "OTZ", "QAQ", "QWQ", "TAT", "TUT", "www",
]

DEFAULT_REPLACEMENTS = ["233|哈哈哈", "666|厉害", "999|很棒", "555|呜呜呜"]


@register(
    "tts_sanitizer", "柯尔", "TTS文本过滤插件 - 透明包装TTS Provider，不修改消息链", "1.0"
)
class TTSSanitizerPlugin(Star):
    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)

        if isinstance(config, AstrBotConfig):
            self.config = config
        else:
            self.config = self._get_default_config()

        self._compile_patterns()
        # 记录已包装的 provider，用于卸载时恢复
        self._wrapped_providers: list = []

    def _get_default_config(self) -> Dict[str, Any]:
        """获取默认配置"""
        return {
            "enabled": True,
            "max_length": 200,
            "max_processing_length": 10000,
            "remove_patterns": DEFAULT_REMOVE_PATTERNS,
            "filter_words": DEFAULT_FILTER_WORDS,
            "replacement_words": DEFAULT_REPLACEMENTS,
            "max_repeat_count": 2,
            "debug_mode": False,
        }

    async def initialize(self):
        """异步插件初始化方法"""
        logger.info(
            f"TTS文本过滤插件 v1.0 已启动 - 最大字数: {self.config.get('max_length', 200)}"
        )
        logger.info(
            f"当前配置: 启用={self.config.get('enabled', True)}, 调试模式={self.config.get('debug_mode', False)}"
        )
        logger.info(
            "📢 工作模式: 透明包装 TTS Provider，不修改消息链，不产生额外消息"
        )

    # =========================================================================
    # 核心：TTS Provider 包装
    # =========================================================================

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self):
        """AstrBot 加载完成后，包装所有 TTS Provider 的 get_audio 方法"""
        self._wrap_all_providers()

    def _wrap_all_providers(self):
        """包装所有 TTS Provider"""
        try:
            providers = self.context.get_all_tts_providers()
        except Exception as e:
            logger.warning(f"TTS过滤: 获取 TTS Provider 失败: {e}")
            return

        if not providers:
            logger.info("TTS过滤: 未发现 TTS Provider，等待后续加载")
            return

        wrapped_count = 0
        for provider in providers:
            if self._wrap_provider(provider):
                wrapped_count += 1

        if wrapped_count > 0:
            logger.info(f"TTS过滤: 已包装 {wrapped_count} 个 TTS Provider")
        else:
            logger.info("TTS过滤: 所有 TTS Provider 已包装过，无需重复操作")

    def _wrap_provider(self, provider) -> bool:
        """包装单个 TTS Provider，返回是否成功包装"""
        if getattr(provider, '_tts_sanitizer_wrapped', False):
            return False

        original_get_audio = provider.get_audio
        plugin = self

        async def wrapped_get_audio(text: str) -> str:
            if not plugin.config.get('enabled', True) or not text:
                return await original_get_audio(text)

            # 超过最大朗读字数则跳过（返回空，TTS Provider 自行处理）
            max_len = plugin.config.get('max_length', 200)
            if max_len > 0 and len(text) > max_len:
                if plugin.config.get('debug_mode', False):
                    logger.info(f"🚫 TTS过滤: 文本 {len(text)} 字超过限制 {max_len}，跳过朗读")
                return await original_get_audio("")

            filtered = plugin.filter_text(text)

            if plugin.config.get('debug_mode', False) and filtered != text:
                logger.info(
                    f"🔧 TTS过滤: '{text[:50]}' → '{filtered[:50]}'"
                )

            if not filtered.strip():
                return await original_get_audio("")

            return await original_get_audio(filtered)

        provider.get_audio = wrapped_get_audio
        provider._tts_sanitizer_wrapped = True
        provider._tts_sanitizer_original_get_audio = original_get_audio

        # 包装 get_audio_stream（Live Mode 支持）
        if provider.support_stream():
            self._wrap_provider_stream(provider)

        self._wrapped_providers.append(provider)
        return True

    def _wrap_provider_stream(self, provider):
        """包装 TTS Provider 的流式 get_audio_stream 方法（Live Mode）"""
        original_get_audio_stream = provider.get_audio_stream
        plugin = self

        async def wrapped_get_audio_stream(
            text_queue: "asyncio.Queue[str | None]",
            audio_queue: "asyncio.Queue[bytes | tuple[str, bytes] | None]",
        ) -> None:
            # 创建过滤中间队列
            filtered_queue: asyncio.Queue[str | None] = asyncio.Queue()

            async def filter_worker():
                """从 text_queue 读取文本，过滤后放入 filtered_queue"""
                while True:
                    text = await text_queue.get()
                    if text is None:
                        await filtered_queue.put(None)
                        break
                    if not plugin.config.get('enabled', True):
                        await filtered_queue.put(text)
                        continue
                    filtered = plugin.filter_text(text)
                    if plugin.config.get('debug_mode', False) and filtered != text:
                        logger.info(
                            f"🔧 TTS流式过滤: '{text[:30]}' → '{filtered[:30]}'"
                        )
                    if filtered.strip():
                        await filtered_queue.put(filtered)
                    # 过滤后为空的段直接丢弃

            # 启动过滤 worker
            filter_task = asyncio.create_task(filter_worker())
            try:
                await original_get_audio_stream(filtered_queue, audio_queue)
            finally:
                if not filter_task.done():
                    filter_task.cancel()

        provider.get_audio_stream = wrapped_get_audio_stream
        provider._tts_sanitizer_original_get_audio_stream = original_get_audio_stream

    def _unwrap_all_providers(self):
        """恢复所有被包装的 TTS Provider"""
        restored_count = 0
        for provider in self._wrapped_providers:
            if hasattr(provider, '_tts_sanitizer_original_get_audio'):
                provider.get_audio = provider._tts_sanitizer_original_get_audio
                del provider._tts_sanitizer_original_get_audio

            if hasattr(provider, '_tts_sanitizer_original_get_audio_stream'):
                provider.get_audio_stream = provider._tts_sanitizer_original_get_audio_stream
                del provider._tts_sanitizer_original_get_audio_stream

            if hasattr(provider, '_tts_sanitizer_wrapped'):
                del provider._tts_sanitizer_wrapped

            restored_count += 1

        self._wrapped_providers.clear()
        if restored_count > 0:
            logger.info(f"TTS过滤: 已恢复 {restored_count} 个 TTS Provider")

    # =========================================================================
    # 过滤逻辑（保持不变）
    # =========================================================================

    def _compile_patterns(self):
        """编译正则表达式和解析替换配置"""
        try:
            # 编译正则过滤规则（合并后的 remove_patterns）
            patterns = self.config.get("remove_patterns", DEFAULT_REMOVE_PATTERNS)
            self.remove_regex = [re.compile(p) for p in patterns]

            # 编译重复字符压缩（0=关闭）
            count = self.config.get("max_repeat_count", 2)
            if count > 0:
                self.repeat_regex = re.compile(f"(.)\\1{{{count},}}")
            else:
                self.repeat_regex = None

            self.replacements = self._parse_replacements()

        except Exception as e:
            logger.warning(f"编译配置失败: {e}")
            self.remove_regex = []
            self.repeat_regex = None
            self.replacements = {}

    def _parse_replacements(self):
        """解析替换词汇配置"""
        replacements = {}
        replacement_list = self.config.get("replacement_words", DEFAULT_REPLACEMENTS)

        for item in replacement_list:
            if isinstance(item, str) and "|" in item:
                try:
                    original, replacement = item.split("|", 1)
                    original = original.strip()
                    replacement = replacement.strip()
                    if original and replacement:
                        replacements[original] = replacement
                except ValueError:
                    logger.warning(f"无效的替换配置格式: {item}")

        return replacements

    def filter_text(self, text: str) -> str:
        """过滤文本"""
        max_processing_length = self.config.get("max_processing_length", 10000)
        if not text or len(text) > max_processing_length:
            return ""

        # 1. 正则过滤（颜文字、括号内容、特殊符号等）
        for regex in self.remove_regex:
            text = regex.sub("", text)

        # 2. 直接过滤的字符和词汇（omega、颜文字用字、网络用语等）
        filter_words = self.config.get("filter_words", DEFAULT_FILTER_WORDS)
        for word in filter_words:
            text = text.replace(word, "")

        # 3. 替换词汇（替换为其他内容）
        for original, replacement in self.replacements.items():
            text = text.replace(original, replacement)

        # 4. 重复字符压缩
        if self.repeat_regex:
            count = self.config.get("max_repeat_count", 2)
            text = self.repeat_regex.sub(lambda m: m.group(1) * count, text)

        # 5. 清理多余空格
        return re.sub(r"\s+", " ", text).strip()

    def should_skip_tts(self, text: str) -> bool:
        """检查是否跳过TTS"""
        max_len = self.config.get("max_length", 200)
        return not text.strip() or (max_len > 0 and len(text) > max_len)

    # =========================================================================
    # 命令
    # =========================================================================

    @filter.command("tts_filter_test")
    async def test_filter(self, event: AstrMessageEvent):
        """测试过滤功能"""
        full_msg = event.message_str.strip()

        for cmd in ["/tts_filter_test", "tts_filter_test"]:
            if full_msg.startswith(cmd):
                user_input = full_msg[len(cmd) :].strip()
                break
        else:
            user_input = full_msg

        if not user_input:
            yield event.plain_result(
                "请输入测试文本，例如：\n/tts_filter_test 你好(＾_＾)测试233"
            )
            return

        filtered = self.filter_text(user_input)
        skip = self.should_skip_tts(filtered)

        filter_words = self.config.get("filter_words", DEFAULT_FILTER_WORDS)
        replacements_info = [f"{k}→{v}" for k, v in list(self.replacements.items())[:3]]
        if len(self.replacements) > 3:
            replacements_info.append(f"等{len(self.replacements)}个")

        result = f"""📝 原文 ({len(user_input)} 字符):
{user_input}

🔧 过滤后 ({len(filtered)} 字符):
{filtered or "(空文本)"}

⚙️ 当前配置:
• 正则规则: {len(self.remove_regex)} 条
• 过滤字符/词汇: {len(filter_words)} 个
• 替换规则: {", ".join(replacements_info) if replacements_info else "无"}
• 重复压缩: {"关闭" if not self.repeat_regex else f">{self.config.get('max_repeat_count', 2)}次→{self.config.get('max_repeat_count', 2)}次"}

📊 处理结果:
• 字符压缩率: {round((len(user_input) - len(filtered)) / len(user_input) * 100, 1) if user_input else 0}%
• TTS状态: {"❌ 跳过" if skip else "✅ 可朗读"}"""

        yield event.plain_result(result)

    @filter.command("tts_filter_stats")
    async def show_stats(self, event: AstrMessageEvent):
        """显示插件状态和配置信息"""
        filter_words = self.config.get("filter_words", DEFAULT_FILTER_WORDS)
        replacement_count = len(self.replacements)
        wrapped_count = len(self._wrapped_providers)
        repeat_count = self.config.get("max_repeat_count", 2)

        result = f"""📊 TTS过滤插件状态 v1.0

🔧 状态:
• 启用: {"✅" if self.config.get("enabled", True) else "❌"}
• 最大朗读字数: {self.config.get("max_length", 200)}（0=无限制）
• 调试模式: {"✅" if self.config.get("debug_mode", False) else "❌"}
• 已包装 Provider: {wrapped_count} 个

⚙️ 配置:
• 正则过滤规则: {len(self.remove_regex)} 条
• 过滤字符/词汇: {len(filter_words)} 个
• 替换词汇: {replacement_count} 个
• 重复压缩: {"关闭" if repeat_count == 0 else f">{repeat_count}次→{repeat_count}次"}

💡 工作模式: Provider 透明包装（不修改消息链）"""

        yield event.plain_result(result)

    @filter.command("tts_filter_reload")
    async def reload_config(self, event: AstrMessageEvent):
        """重新加载配置并重新包装 Provider"""
        try:
            self._compile_patterns()
            # 重新包装（检查是否有新 Provider）
            self._wrap_all_providers()
            yield event.plain_result(
                f"✅ 配置已重新加载，已包装 {len(self._wrapped_providers)} 个 Provider"
            )
        except Exception as e:
            yield event.plain_result(f"❌ 重新加载失败: {e}")

    async def terminate(self):
        """插件销毁时恢复所有 TTS Provider"""
        self._unwrap_all_providers()
        logger.info("TTS过滤插件已停止，所有 TTS Provider 已恢复原始状态")
