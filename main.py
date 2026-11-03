from __future__ import annotations

import asyncio
import json
import random
import re
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

import aiohttp

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain, Record
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.permission import PermissionType


PLUGIN_NAME = "astrbot_plugin_breezeblue_tts"


class BreezeBlueTTS(Star):
    """用 BreezeBlue 把 AstrBot 的文字回复合成语音发送。"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.conf = config or {}
        self.enabled = bool(self.conf.get("enabled", True))
        self.api_key = str(self.conf.get("api_key", "") or "").strip()
        self.base_url = str(self.conf.get("base_url", "https://api.breeze.blue") or "").strip().rstrip("/")
        self.voice_id = str(self.conf.get("voice_id", "") or "").strip()
        self.model_id = str(self.conf.get("model_id", "breeze-tts-2") or "").strip()
        self.language_code = str(self.conf.get("language_code", "zh") or "").strip()
        self.output_format = str(self.conf.get("output_format", "mp3") or "mp3").strip()
        self.instructions = str(self.conf.get("instructions", "") or "").strip()
        self.guidance_scale = float(self.conf.get("guidance_scale", 4.0) or 0)
        self.send_mode = str(self.conf.get("send_mode", "text_and_voice") or "text_and_voice").strip()
        self.probability = max(0, min(100, int(self.conf.get("probability", 100) or 0)))
        self.max_chars = max(1, int(self.conf.get("max_chars", 500) or 500))
        self.min_chars = max(1, int(self.conf.get("min_chars", 1) or 1))
        self.timeout = max(5, int(self.conf.get("timeout", 120) or 120))
        self.clean_markdown = bool(self.conf.get("clean_markdown", True))
        self.strip_emoji = bool(self.conf.get("strip_emoji", True))
        self.skip_commands = bool(self.conf.get("skip_commands", True))
        self.default_profile = str(self.conf.get("default_profile", "default") or "default").strip()
        self.enable_mood_tool = bool(self.conf.get("enable_mood_tool", True))

        self.voice_profiles = self._parse_obj(self.conf.get("voice_profiles", "{}"), "voice_profiles")
        self.persona_profiles = self._parse_obj(self.conf.get("persona_profiles", "{}"), "persona_profiles")

        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.audio_dir = self.data_dir / "audio"
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.data_dir / "voice_state.json"
        self.voice_state = self._load_state()
        self.state_lock = asyncio.Lock()
        self._cleanup_old_audio()

        if not self.api_key:
            self.logger.warning(
                "[breezeblue_tts] 未配置 api_key，语音功能会跳过；请到插件配置填写 BreezeBlue API Key。"
            )
        if not self.voice_id and not self.voice_profiles:
            self.logger.warning(
                "[breezeblue_tts] 未配置 voice_id 或 voice_profiles，语音功能会跳过。"
            )

    # ------------------------------------------------------------------
    # 配置辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_obj(value: Any, name: str) -> dict:
        if isinstance(value, dict):
            return value
        if not value:
            return {}
        try:
            parsed = json.loads(str(value))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
        return {}

    def _load_state(self) -> dict:
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {
                        str(k): str(v)
                        for k, v in data.items()
                        if isinstance(v, str) and v.strip()
                    }
        except Exception as exc:
            self.logger.warning("[breezeblue_tts] 读取语音状态失败: %s", exc)
        return {}

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self.voice_state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.state_path)
        except Exception as exc:
            self.logger.warning("[breezeblue_tts] 保存语音状态失败: %s", exc)

    def _available_profiles(self) -> list[str]:
        names = [str(k) for k in self.voice_profiles.keys() if str(k).strip()]
        if "default" not in names:
            names.insert(0, "default")
        return names

    def _profile_description(self, name: str) -> str:
        profile = self.voice_profiles.get(name)
        if isinstance(profile, dict):
            desc = str(profile.get("description") or "").strip()
            if desc:
                return desc
        if name == "default":
            return "默认声音"
        return name

    async def _set_session_profile(self, umo: str, profile: str) -> tuple[bool, str]:
        profile = str(profile or "").strip()
        if not self.enable_mood_tool:
            return False, "按情绪切换声音的工具未启用。"
        if not profile:
            return False, "请提供声音配置名：" + "、".join(self._available_profiles())
        if profile not in self._available_profiles():
            return False, (
                f"没有声音配置「{profile}」。可用配置："
                + "、".join(self._available_profiles())
            )
        async with self.state_lock:
            self.voice_state[umo] = profile
            self._save_state()
        return True, f"当前会话已切换到「{profile}」（{self._profile_description(profile)}）。"

    def _profile_for(
        self,
        persona_id: str,
        umo: str = "",
    ) -> tuple[str, str, str, float]:
        """返回 (profile_name, voice_id, instructions, guidance_scale)。"""
        profile_name = self.default_profile or "default"
        if umo:
            selected = self.voice_state.get(umo)
            if selected in self._available_profiles():
                profile_name = selected
        if persona_id and not (umo and self.voice_state.get(umo)):
            mapped = self.persona_profiles.get(persona_id)
            if isinstance(mapped, str) and mapped.strip():
                profile_name = mapped.strip()

        profile = self.voice_profiles.get(profile_name)
        voice_id = self.voice_id
        instructions = self.instructions
        guidance = self.guidance_scale

        if isinstance(profile, str) and profile.strip():
            voice_id = profile.strip()
        elif isinstance(profile, dict):
            voice_id = str(profile.get("voice_id") or voice_id).strip()
            instructions = str(profile.get("instructions") or instructions).strip()
            if profile.get("guidance_scale") is not None:
                try:
                    guidance = float(profile["guidance_scale"])
                except (TypeError, ValueError):
                    pass
        return profile_name, voice_id, instructions, guidance

    async def _persona_id(self, event: AstrMessageEvent) -> str:
        try:
            umo = event.unified_msg_origin
            cid = await self.context.conversation_manager.get_curr_conversation_id(umo)
            if not cid:
                return ""
            conv = await self.context.conversation_manager.get_conversation(
                unified_msg_origin=umo,
                conversation_id=cid,
                create_if_not_exists=True,
            )
            pid = str(getattr(conv, "persona_id", "") or "").strip()
            return "" if pid == "[%None]" else pid
        except Exception as exc:
            self.logger.debug("[breezeblue_tts] 读取人格失败: %s", exc)
            return ""

    # ------------------------------------------------------------------
    # 文本和音频
    # ------------------------------------------------------------------
    def _clean_text(self, text: str) -> str:
        text = str(text or "")
        if self.clean_markdown:
            text = re.sub(r"```.*?```", "", text, flags=re.S)
            text = re.sub(r"`([^`]*)`", r"\1", text)
            text = re.sub(r"https?://\S+", "", text)
            text = re.sub(r"[*_~#>]+", "", text)
            text = re.sub(r"\[[^\]]{1,24}\]", "", text)
        if self.strip_emoji:
            text = re.sub(
                "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF]+",
                "",
                text,
            )
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _limit_text(self, text: str) -> str:
        if len(text) <= self.max_chars:
            return text
        window = text[: self.max_chars]
        for sep in ("。", "！", "？", "\n", "，", ",", " "):
            idx = window.rfind(sep)
            if idx >= max(1, int(self.max_chars * 0.5)):
                window = window[: idx + 1]
                break
        return window.strip()

    async def _synthesize(
        self,
        text: str,
        event: AstrMessageEvent | None = None,
    ) -> Path | None:
        if not self.enabled:
            return None
        if not self.api_key:
            self.logger.warning("[breezeblue_tts] 缺少 api_key，跳过语音合成")
            return None

        persona_id = await self._persona_id(event) if event else ""
        umo = event.unified_msg_origin if event else ""
        profile_name, voice_id, instructions, guidance = self._profile_for(
            persona_id, umo
        )
        if not voice_id:
            self.logger.warning("[breezeblue_tts] profile=%s 没有可用 voice_id，跳过", profile_name)
            return None

        text = self._clean_text(text)
        if len(text) < self.min_chars:
            return None
        if len(text) > self.max_chars:
            original_len = len(text)
            text = self._limit_text(text)
            self.logger.info(
                "[breezeblue_tts] 文本过长，截断 %s -> %s 字符", original_len, len(text)
            )
        if not text:
            return None

        url = f"{self.base_url}/v1/text-to-speech/{quote(voice_id, safe='')}"
        payload: dict[str, Any] = {"text": text}
        if self.model_id:
            payload["model_id"] = self.model_id
        if self.language_code:
            payload["language_code"] = self.language_code
        if instructions:
            payload["instructions"] = instructions
        if guidance > 0:
            payload["voice_settings"] = {"guidance_scale": guidance}

        headers = {
            "xi-api-key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        }
        params = {"output_format": self.output_format}
        timeout = aiohttp.ClientTimeout(total=self.timeout)

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                body = b""
                success = False
                for attempt in range(2):
                    url = (
                        f"{self.base_url}/v1/text-to-speech/"
                        f"{quote(voice_id, safe='')}"
                    )
                    async with session.post(
                        url,
                        json=payload,
                        params=params,
                        headers=headers,
                    ) as resp:
                        body = await resp.read()
                        if 200 <= resp.status < 300 and body:
                            success = True
                            break

                        detail = body[:500].decode("utf-8", "replace")
                        # 配置里的 profile 可能还是 README 示例占位符
                        # （如 voc_default）。遇到 Voice not found 时回退到
                        # 顶层 voice_id，避免一个占位配置让整个插件失效。
                        if (
                            resp.status == 404
                            and attempt == 0
                            and voice_id != self.voice_id
                            and self.voice_id
                        ):
                            self.logger.warning(
                                "[breezeblue_tts] profile=%s 的 voice=%s 不存在，"
                                "回退到默认 voice=%s",
                                profile_name,
                                voice_id,
                                self.voice_id,
                            )
                            voice_id = self.voice_id
                            continue

                        self.logger.error(
                            "[breezeblue_tts] TTS 请求失败 status=%s voice=%s: %s",
                            resp.status,
                            voice_id,
                            detail,
                        )
                        return None

                if not success:
                    self.logger.error("[breezeblue_tts] TTS 未返回有效音频")
                    return None
        except asyncio.TimeoutError:
            self.logger.error("[breezeblue_tts] TTS 请求超时（%ss）", self.timeout)
            return None
        except Exception as exc:
            self.logger.exception("[breezeblue_tts] TTS 请求异常: %s", exc)
            return None

        ext = "mp3"
        if self.output_format.startswith("wav"):
            ext = "wav"
        elif self.output_format.startswith("flac"):
            ext = "flac"
        elif self.output_format.startswith("opus"):
            ext = "opus"
        elif self.output_format.startswith("aac"):
            ext = "aac"

        path = self.audio_dir / f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.{ext}"
        try:
            path.write_bytes(body)
        except Exception as exc:
            self.logger.error("[breezeblue_tts] 保存音频失败: %s", exc)
            return None

        self.logger.info(
            "[breezeblue_tts] 合成完成 profile=%s voice=%s chars=%s bytes=%s",
            profile_name,
            voice_id,
            len(text),
            len(body),
        )
        return path

    def _cleanup_old_audio(self) -> None:
        cutoff = time.time() - 7 * 24 * 3600
        try:
            for path in self.audio_dir.iterdir():
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 事件钩子
    # ------------------------------------------------------------------
    @filter.on_decorating_result(priority=100000)
    async def on_decorating_result(self, event: AstrMessageEvent):
        if not self.enabled or not self.api_key:
            return
        result = event.get_result()
        if not result or not result.chain:
            return

        # 有图片、文件、已有语音等非纯文本内容时，不强行替换成语音
        if any(not isinstance(comp, Plain) for comp in result.chain):
            return

        if self.skip_commands:
            incoming = str(getattr(event, "message_str", "") or "").strip()
            if incoming.startswith("/"):
                return

        text = result.get_plain_text().strip()
        if not text:
            return
        if random.randint(1, 100) > self.probability:
            return

        audio = await self._synthesize(text, event)
        if not audio:
            return

        record = Record.fromFileSystem(str(audio))
        if self.send_mode == "voice_only":
            result.chain = [record]
        else:
            result.chain.append(record)

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("语音测试")
    async def voice_test(self, event: AstrMessageEvent):
        """合成一条测试语音，检查 BreezeBlue 配置是否可用。"""
        audio = await self._synthesize("这是一条 BreezeBlue 语音测试。", event)
        if not audio:
            yield event.plain_result(
                "语音合成失败。请检查 api_key、voice_id、base_url，并查看 AstrBot 日志。"
            )
            return
        yield event.chain_result([Record.fromFileSystem(str(audio))])

    @filter.command("语音状态")
    async def voice_status(self, event: AstrMessageEvent):
        """查看当前人格和会话对应的语音配置。"""
        persona_id = await self._persona_id(event)
        umo = event.unified_msg_origin
        profile, voice_id, _, guidance = self._profile_for(persona_id, umo)
        selected = self.voice_state.get(umo, "未单独设置")
        yield event.plain_result(
            "BreezeBlue 语音状态\n"
            f"启用：{self.enabled}\n"
            f"人格：{persona_id or '未绑定'}\n"
            f"会话选择：{selected}\n"
            f"实际声音配置：{profile}\n"
            f"voice_id：{voice_id or '未配置'}\n"
            f"发送方式：{self.send_mode}\n"
            f"引导强度：{guidance}\n"
            f"可用配置：{'、'.join(self._available_profiles())}"
        )

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("语音切换")
    async def voice_switch(self, event: AstrMessageEvent, profile: str = ""):
        """手动切换当前会话的语音配置。"""
        ok, message = await self._set_session_profile(
            event.unified_msg_origin, profile
        )
        yield event.plain_result(message if ok else f"切换失败：{message}")

    @filter.permission_type(PermissionType.ADMIN)
    @filter.command("语音重置")
    async def voice_reset(self, event: AstrMessageEvent):
        """清除当前会话的手动声音选择，恢复人格或默认声音。"""
        async with self.state_lock:
            self.voice_state.pop(event.unified_msg_origin, None)
            self._save_state()
        yield event.plain_result("当前会话的声音选择已重置。")

    @filter.llm_tool(name="breezeblue_list_voices")
    async def breezeblue_list_voices(self, event: AstrMessageEvent):
        """列出当前可用的 BreezeBlue 声音配置，以及当前会话正在使用哪一个。

        Returns:
            JSON 字符串，包含 current 和 profiles 数组。
        """
        if not self.enable_mood_tool:
            return "按情绪切换声音的工具未启用。"
        profile, _, _, _ = self._profile_for(
            await self._persona_id(event), event.unified_msg_origin
        )
        profiles = [
            {
                "profile": name,
                "description": self._profile_description(name),
            }
            for name in self._available_profiles()
        ]
        return json.dumps(
            {"current": profile, "profiles": profiles},
            ensure_ascii=False,
        )

    @filter.llm_tool(name="breezeblue_switch_voice")
    async def breezeblue_switch_voice(
        self,
        event: AstrMessageEvent,
        profile: str,
    ):
        """根据当前对话的情绪切换 BreezeBlue 声音配置。

        当用户明显开心、难过、撒娇、生气，或你希望换一种语气说话时使用。
        先用 breezeblue_list_voices 确认可用配置名，不要编造不存在的名字。

        Args:
            profile(string): 声音配置名，例如 default、撒娇、开心、难过、生气。
        """
        ok, message = await self._set_session_profile(
            event.unified_msg_origin, profile
        )
        return message if ok else f"切换失败：{message}"
