# Dify 插件 🤖

## 简介

Dify 插件是为 XYBotV2 机器人框架设计的一个插件，它允许机器人与 Dify (一个 LLM 应用开发平台) 进行交互。通过这个插件，你可以让你的微信机器人具备强大的自然语言处理能力，例如文本生成、对话、语音处理和文件处理等。🚀

<img src="https://github.com/user-attachments/assets/a2627960-69d8-400d-903c-309dbeadf125" width="400" height="600">

## 特性

*   **多消息类型支持:** 支持文本、@消息、语音、图片、视频和文件消息的处理。💬
*   **Dify 集成:** 无缝对接 Dify 平台，利用其强大的 LLM 能力。🔗
*   **灵活的配置:** 允许配置 API 密钥、基础 URL、命令、提示语、价格、代理等。⚙️
*   **积分系统集成:** 可以配置是否管理员和白名单用户忽略积分检查。💰
*   **流式响应:** 使用 Dify 的流式响应模式，逐步返回结果，提升用户体验。✨
*   **语音合成 (TTS) 支持:** 可选的 TTS 功能，将文本回复转换为语音消息。🗣️
*   **文件上传:** 支持上传语音、图片、视频和文件到 Dify 进行处理。📤
*   **媒体文件处理:** 自动识别并发送回复中的链接指向的媒体文件（语音、图片、视频）。🖼️
*   **错误处理:** 完善的错误处理机制，当 Dify 返回错误时，能向用户提供清晰的错误信息。⚠️

## 安装

1.  确保你已经安装了 XYBotV2 机器人框架。 ✅
2.  将 `Dify` 插件文件夹复制到 XYBotV2 的 `plugins` 目录下。 📁

## 配置

1.  编辑 `main_config.toml` 文件，配置管理员列表：

    ```toml
    [XYBot]
    admins = ["your_wxid"] # 你的微信ID
    ```

2.  编辑 `plugins/Dify/config.toml` 文件，配置 Dify 插件：

    ```toml
    [Dify]
    enable = true  # 是否启用插件
    api-key = "YOUR_DIFY_API_KEY"  # 你的 Dify API 密钥
    base-url = "YOUR_DIFY_BASE_URL"  # 你的 Dify 基础 URL，例如 "https://your-dify-domain.com/v1"

    commands = ["dify", "对话"]  # 触发 Dify 插件的命令列表
    command-tip = "请在命令后添加你的问题"  # 命令提示语

    price = 1  # 每次调用 Dify 需要的积分
    admin_ignore = true  # 管理员是否忽略积分检查
    whitelist_ignore = true # 白名单用户是否忽略积分检查

    http-proxy = "http://your-proxy:port"  # HTTP 代理 (可选)

    # 语音配置
    tts-enable = false  # 是否启用 TTS
    tts-voice = 6       # TTS 音色 ID (小爱语音API)
    tts-type = "baidu"  # TTS 类型 (小爱语音API) 支持 "baidu"
    ```

## 使用方法

1.  在微信中向机器人发送配置的命令，例如 `dify 你好` 或者 `@机器人 dify 你好` (在群聊中)。💬
2.  机器人会将你的消息发送到 Dify，并将 Dify 的回复返回给你。 🤖
3.  如果启用了 TTS，机器人会将文本回复转换为语音消息。 🗣️

## 消息类型支持

*   **文本消息:**  直接发送文本消息给机器人。 📝
*   **@消息:** 在群聊中 @ 机器人并发送消息。 📢
*   **语音消息:**  发送语音消息给机器人。 🎤
*   **图片消息:**  发送图片消息给机器人。 🖼️
*   **视频消息:**  发送视频消息给机器人。 🎬
*   **文件消息:**  发送文件消息给机器人。 📄

## 积分系统

*   插件使用了 XYBotDB 来管理用户的积分。 📊
*   每次调用 Dify 插件会消耗用户一定数量的积分（可在配置文件中设置）。 💸
*   管理员和白名单用户可以配置为忽略积分检查。 🛡️

## 依赖

*   XYBotV2 机器人框架
*   `aiohttp`
*   `filetype`
*   `loguru`
*   `tomllib` (Python 3.11+)  or `toml` (Python < 3.11)
*   `WechatAPI`
*   `database.XYBotDB`
*   `utils.decorators`
*   `utils.plugin_base`

## Change Log

*   **1.0.0**  初始版本 🐣
*   **1.1.0 (2025-02-20)**  插件优先级，插件阻塞。 🚦
*   **1.2.0 (2025-02-22)**  有插件阻塞了，other-plugin-cmd可删了. 增加了语音合成(TTS)支持. 🎉

## 注意事项

*   请确保你的 Dify API 密钥和基础 URL 配置正确。🔑
*   语音合成功能依赖于第三方 API，请确保 API 可用。 🌐
*   如果遇到问题，请查看 XYBotV2 的日志文件 `logs/xybot.log`。 🔍

## 作者

*   HenryXiaoYang/老夏的金库 👨‍💻

**给个 ⭐ Star 支持吧！** 😊

**开源不易，感谢打赏支持！**

![image](https://github.com/user-attachments/assets/2dde3b46-85a1-4f22-8a54-3928ef59b85f)

![image](https://github.com/user-attachments/assets/2dde3b46-85a1-4f22-8a54-3928ef59b85f)

## License

MIT 📜