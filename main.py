from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.provider import LLMResponse
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.api.message_components import Plain
import re
import time
from typing import Optional, Dict, Any

# 内置过滤规则
EMOTICON_PATTERNS = [
    r"[（(][^（()]*[）)]",
    r"[＞>][＿_][＜<]",
    r"[＾^][＿_][＾^]",
    r"[oO][＿_][oO]",
    r"[xX][＿_][xX]",
    r"[－-][＿_][－-]",
    r"[（(][；;][＿_][；;][）)]",
]

SPECIAL_CHAR_PATTERNS = [
    r"[★☆♪♫♬♩♡♥❤️💖💕💗💓💝💟💜💛💚💙🧡🤍🖤🤎💔❣️💋]",
    r"[→←↑↓↖↗↘↙↔↕↺↻]",
]

FILTER_WORDS = ["orz", "OTZ", "QAQ", "QWQ", "TAT", "TUT"]

DEFAULT_REPLACEMENTS = ["233|哈哈哈", "666|厉害", "999|很棒", "6|厉害", "555|呜呜呜"]


@register(
    "tts_sanitizer", "柯尔", "TTS文本过滤插件，自动清理不适合TTS朗读的内容", "0.2"
)
class TTSSanitizerPlugin(Star):
    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)

        # 使用AstrBot的配置系统，如果没有则使用默认配置
        if isinstance(config, AstrBotConfig):
            self.config = config
        else:
            # 回退到默认配置
            self.config = self._get_default_config()

        self._compile_patterns()

    def _get_default_config(self) -> Dict[str, Any]:
        """获取默认配置"""
        return {
            "enabled": True,
            "max_length": 200,
            "max_processing_length": 10000,
            "emoticon_patterns": EMOTICON_PATTERNS,
            "filter_words": FILTER_WORDS,
            "replacement_words": DEFAULT_REPLACEMENTS,
            "filter_special_chars": True,
            "special_char_patterns": SPECIAL_CHAR_PATTERNS,
            "filter_repeats": True,
            "max_repeat_count": 2,
            "debug_mode": False,
        }

    async def initialize(self):
        """异步插件初始化方法"""
        logger.info(
            f"TTS文本过滤插件已启动 - 最大字数: {self.config.get('max_length', 200)}"
        )
        logger.info(
            f"当前配置: 启用={self.config.get('enabled', True)}, 调试模式={self.config.get('debug_mode', False)}"
        )

    def _compile_patterns(self):
        """编译正则表达式和解析替换配置"""
        try:
            # 编译颜文字模式
            patterns = self.config.get("emoticon_patterns", EMOTICON_PATTERNS)
            self.emoticon_regex = [re.compile(p) for p in patterns]

            # 编译特殊符号模式
            if self.config.get("filter_special_chars", True):
                patterns = self.config.get(
                    "special_char_patterns", SPECIAL_CHAR_PATTERNS
                )
                self.special_regex = [re.compile(p) for p in patterns]
            else:
                self.special_regex = []

            # 编译重复字符模式
            if self.config.get("filter_repeats", True):
                count = self.config.get("max_repeat_count", 2)
                self.repeat_regex = re.compile(f"(.)\\1{{{count},}}")
            else:
                self.repeat_regex = None

            # 解析替换配置
            self.replacements = self._parse_replacements()

        except Exception as e:
            logger.warning(f"编译配置失败: {e}")
            self.emoticon_regex = self.special_regex = []
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
        if not text or len(text) > max_processing_length:  # 防护
            return ""

        # 1. 过滤颜文字
        for regex in self.emoticon_regex:
            text = regex.sub("", text)

        # 2. 直接过滤的词汇（完全移除）
        filter_words = self.config.get("filter_words", FILTER_WORDS)
        for word in filter_words:
            text = text.replace(word, "")

        # 3. 替换词汇（替换为其他内容）
        for original, replacement in self.replacements.items():
            text = text.replace(original, replacement)

        # 4. 过滤特殊符号
        for regex in self.special_regex:
            text = regex.sub("", text)

        # 5. 处理重复字符
        if self.repeat_regex:
            count = self.config.get("max_repeat_count", 2)
            text = self.repeat_regex.sub(lambda m: m.group(1) * count, text)

        # 6. 清理多余空格
        return re.sub(r"\s+", " ", text).strip()

    def should_skip_tts(self, text: str) -> bool:
        """检查是否跳过TTS"""
        max_len = self.config.get("max_length", 200)
        return not text.strip() or (max_len > 0 and len(text) > max_len)

    @filter.on_decorating_result(priority=-1001)
    async def filter_for_tts_only(self, event: AstrMessageEvent):
        """为TTS提供过滤后的内容，不修改原始消息"""
        if not self.config.get("enabled", True):
            return

        start_time = time.time()
        debug = self.config.get("debug_mode", False)

        try:
            result = event.get_result()
            if not result or not hasattr(result, "chain") or not result.chain:
                return

            # 为TTS创建过滤后的消息链副本
            tts_chain = []
            has_filtered_content = False

            for comp in result.chain:
                if isinstance(comp, Plain) and getattr(comp, "text", ""):
                    original_text = comp.text
                    filtered_text = self.filter_text(original_text)

                    # 检查是否应该跳过TTS
                    if self.should_skip_tts(filtered_text):
                        if debug:
                            logger.info(f"🚫 TTS过滤: 文本过长，跳过TTS")
                        return

                    if filtered_text != original_text:
                        # 创建新的Plain组件用于TTS
                        tts_chain.append(Plain(text=filtered_text))
                        has_filtered_content = True
                        if debug:
                            logger.info(
                                f"🔧 TTS过滤: '{original_text[:20]}...' -> '{filtered_text[:20]}...'"
                            )
                    else:
                        # 文本未变化，直接复制
                        tts_chain.append(Plain(text=original_text))
                else:
                    # 非文本组件直接复制
                    tts_chain.append(comp)

            # 将过滤后的内容存储到事件上下文中，供TTS插件使用
            if has_filtered_content and tts_chain:
                # 使用事件上下文存储过滤后的消息链
                # 这样TTS插件可以从上下文中读取过滤后的内容
                try:
                    # 尝试使用事件上下文存储
                    if hasattr(event, 'set_metadata'):
                        event.set_metadata("tts_filtered_chain", tts_chain)
                    elif hasattr(event, 'context'):
                        event.context.set_plugin_data("tts_sanitizer", "filtered_chain", tts_chain)
                    else:
                        # 回退方案：直接修改消息链（但只影响TTS）
                        result.chain = tts_chain
                        if debug:
                            logger.warning("⚠️ 使用回退方案：直接修改消息链")
                    
                    if debug:
                        logger.info(f"✅ TTS过滤: 已创建过滤后的消息链，包含 {len(tts_chain)} 个组件")
                except Exception as e:
                    if debug:
                        logger.warning(f"⚠️ 存储过滤内容失败: {e}")
                    # 如果存储失败，使用回退方案
                    result.chain = tts_chain

        except Exception as e:
            logger.error(f"TTS过滤处理错误: {e}")
        finally:
            if time.time() - start_time > 0.1:
                logger.warning(f"TTS过滤耗时过长: {time.time() - start_time:.3f}s")

    @filter.command("tts_filter_test")
    async def test_filter(self, event: AstrMessageEvent):
        """测试过滤功能"""
        full_msg = event.message_str.strip()

        # 提取用户输入
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

        # 显示配置信息
        filter_words = self.config.get("filter_words", FILTER_WORDS)
        replacements_info = [f"{k}→{v}" for k, v in list(self.replacements.items())[:3]]
        if len(self.replacements) > 3:
            replacements_info.append(f"等{len(self.replacements)}个")

        result = f"""📝 原文 ({len(user_input)} 字符):
{user_input}

🔧 过滤后 ({len(filtered)} 字符):
{filtered or "(空文本)"}

⚙️ 当前配置:
• 直接过滤: {", ".join(filter_words[:3])}{"等" if len(filter_words) > 3 else ""}
• 替换规则: {", ".join(replacements_info) if replacements_info else "无"}

📊 处理结果:
• 字符压缩率: {round((len(user_input) - len(filtered)) / len(user_input) * 100, 1) if user_input else 0}%
• TTS状态: {"❌ 跳过" if skip else "✅ 可朗读"}

🔄 新机制说明:
• 原始消息保持不变，仅TTS使用过滤后的内容
• 避免了文本恢复的时机问题
• 更稳定可靠的过滤机制"""

        yield event.plain_result(result)

    @filter.command("tts_filter_stats")
    async def show_stats(self, event: AstrMessageEvent):
        """显示插件状态和配置信息"""
        # 统计当前配置
        filter_words = self.config.get("filter_words", FILTER_WORDS)
        replacement_count = len(self.replacements)

        result = f"""📊 TTS过滤插件状态
        
🔧 状态:
• 启用: {"✅" if self.config.get("enabled", True) else "❌"}
• 字数限制: {self.config.get("max_length", 200)}
• 处理长度限制: {self.config.get("max_processing_length", 10000)}
• 调试模式: {"✅" if self.config.get("debug_mode", False) else "❌"}

⚙️ 配置:
• 直接过滤词汇: {len(filter_words)} 个
• 替换词汇: {replacement_count} 个
• 颜文字过滤: {"✅" if self.emoticon_regex else "❌"}
• 特殊符号过滤: {"✅" if self.config.get("filter_special_chars", True) else "❌"}

🔄 过滤机制:
• 使用消息链副本机制，不修改原始消息
• 为TTS提供过滤后的内容
• 避免了文本恢复的时机问题"""

        yield event.plain_result(result)

    @filter.command("tts_filter_reload")
    async def reload_config(self, event: AstrMessageEvent):
        """重新加载配置"""
        try:
            self._compile_patterns()
            yield event.plain_result("✅ 配置已重新加载")
        except Exception as e:
            yield event.plain_result(f"❌ 重新加载失败: {e}")

    async def terminate(self):
        """插件销毁"""
        logger.info("TTS过滤插件已停止")
