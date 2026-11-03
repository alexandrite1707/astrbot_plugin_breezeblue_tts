# BreezeBlue 语音

把 AstrBot 的文字回复用 [BreezeBlue](https://breezeblue.ai) TTS 合成为语音，并通过 QQ 发送。

## 功能

- 文字回复自动转语音
- 纯语音 / 文字 + 语音两种发送方式
- 按人格绑定不同 `voice_id`
- 每个声音可配置独立的语气 `instructions`
- 管理员命令 `/语音测试`、`/语音状态`
- 没有 API Key 或 voice_id 时自动跳过，不影响正常文字聊天

## 安装

1. 将本目录放到：

```text
AstrBot/data/plugins/astrbot_plugin_breezeblue_tts
```

2. 在 AstrBot 插件管理里重载插件，或重启 AstrBot。

3. 在插件配置中填写：

```text
api_key: brz_...
voice_id: voc_...
```

BreezeBlue API Key 在开发者控制台创建：

```text
https://breezeblue.ai/app/developer
```

## 配置示例

### 固定一个声音

```json
{
  "api_key": "brz_xxx",
  "voice_id": "voc_xxx",
  "send_mode": "text_and_voice",
  "instructions": "用温柔、自然、略带亲密的语气说这句话。"
}
```

### 不同人格用不同声音

> 注意：`voc_default`、`voc_cute`、`voc_sad` 只是示例占位符，必须换成你账号里真实存在的 `voice_id`，否则会报 `Voice not found`。

`voice_profiles`：

```json
{
  "default": {
    "voice_id": "voc_你的真实声音ID",
    "instructions": "温柔、平静、语速稍慢。"
  },
  "撒娇": {
    "voice_id": "voc_另一个真实声音ID",
    "instructions": "撒娇、轻快、尾音软一点。"
  }
}
```

`persona_profiles`：

```json
{
  "甜美鲸鱼": "撒娇",
  "小鲸鱼": "default"
}
```

这样同一段回复会根据当前人格选择声音。

## 命令

```text
/语音测试   管理员：合成一条测试语音
/语音状态   查看当前人格、会话选择、实际声音配置
/语音切换 <配置名>  管理员：手动切换当前会话声音
/语音重置   管理员：清除当前会话的声音选择
```

## 按情绪切换声音（LLM 工具）

开启 `enable_mood_tool` 后，插件会向模型注册两个工具：

```text
breezeblue_list_voices   列出可用声音配置和当前配置
breezeblue_switch_voice  按当前情绪切换声音配置
```

当对话情绪明显变化时，模型可以自己调用 `breezeblue_switch_voice`，例如：

```text
用户开始撒娇 → profile="撒娇"
用户说很难过 → profile="难过"
用户生气 → profile="生气"
情绪平静下来 → profile="平静"
```

切换结果按会话（UMO）保存到：

```text
插件数据目录/voice_state.json
```

所以同一会话后续回复会继续使用该声音；发 `/语音重置` 可恢复默认。

> 如果所有情绪配置都填同一个 `voice_id`，切换的是语气和表演方式，不是音色本身。  
> 想让音色也变化，需要给不同情绪填不同的真实 `voice_id`。

## 注意

- 这个插件只负责“说话”，不负责“听”。要让她听到你的语音，需要另外启用 AstrBot 的语音转文字 Provider（Whisper / SenseVoice / MiMo 等）。
- BreezeBlue 的 `instructions` 是语气描述，不是角色扮演提示词；不要写太长。
- 长回复默认截断到 500 字再合成，避免单次请求超限。
- 生成的音频保存在插件数据目录的 `audio/` 下，启动时会清理 7 天前的文件。

## 发布到 AstrBot 插件市场

1. 将本目录推送到公开 GitHub 仓库。
2. 修改 `metadata.yaml` 里的 `repo` 为你的真实仓库地址。
3. 到 [AstrBot 插件发布页](https://cloud.astrbot.app/publish) 注册并提交仓库。
4. 仓库根目录必须有 `metadata.yaml`，且包含 `name`、`desc`、`version`、`author`。
5. 插件压缩包不超过 16MB，不要提交 `.git`、`__pycache__`、`node_modules` 等目录。
