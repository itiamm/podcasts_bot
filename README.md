# YouTube Podcast Audio

把指定 YouTube 频道的长视频转成 MP3，上传到 Cloudflare R2，并通过 Telegram 推送频道名、标题、发布时间和音频链接。英文频道会先转写英文，再改写成中文播客稿，最后合成中文音频。

当前已验证的英文频道链路：

```text
YouTube 音频 -> 本地 Whisper 转写 -> NVIDIA MiniMax-M3 改写中文播客稿 -> 阿里百炼 Sambert TTS -> R2 -> Telegram
```

## 当前规则

- 频道配置：`config/channels.json`
- 默认首次拉取最近 7 天视频
- 中文频道：保留原始音频
- 英文频道：转写英文，生成中文播客稿，再合成中文 MP3
- 后续运行：只处理新增视频
- 内容类型：只处理长视频，排除 Shorts
- 时长限制：超过 3 小时跳过
- MP3 音质：128k
- 音频保留：30 天后从 R2 删除
- 定时：每天 08:00
- 已验证文本模型：本地 Ollama `qwen3.5:4b`、NVIDIA `minimaxai/minimax-m3`
- 可选文本模型：智谱 GLM、阿里百炼 DashScope 兼容模式

## 初始化

```bash
cd /Users/bytedance/projects/podcasts
cp .env.example .env
chmod +x scripts/run.sh scripts/install_launchd.sh
```

编辑 `.env`，填入：

```env
R2_ACCESS_KEY_ID=你的 R2 Access Key ID
R2_SECRET_ACCESS_KEY=你的 R2 Secret Access Key
TELEGRAM_BOT_TOKEN=你的 Telegram Bot Token
NVIDIA_API_KEY=你的 NVIDIA API Key，用于生成中文播客稿
DASHSCOPE_API_KEY=你的阿里百炼 API Key，用于 TTS
```

不要把 `.env` 提交或发到聊天里。`ZHIPU_API_KEY` 只有在使用 `TEXT_PROVIDER=zhipu` 时才需要。

## 配置项速查

核心存储和通知：

| 配置项 | 用途 |
| --- | --- |
| `R2_ACCOUNT_ID` | Cloudflare R2 账号 ID |
| `R2_BUCKET_NAME` | R2 bucket 名称 |
| `R2_ENDPOINT` | R2 S3 兼容 endpoint |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | R2 上传凭证 |
| `PUBLIC_AUDIO_BASE_URL` | Telegram 中展示的公开音频 URL 前缀 |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | Telegram 推送配置 |

运行策略：

| 配置项 | 用途 |
| --- | --- |
| `MP3_AUDIO_QUALITY` | MP3 输出码率，默认 `128K` |
| `MAX_VIDEO_SECONDS` | 最大处理视频时长，默认 `10800` 秒 |
| `MIN_LONG_VIDEO_SECONDS` | 长视频最小时长，默认 `181` 秒 |
| `RETENTION_DAYS` | R2 音频保留天数，默认 `30` |
| `PODCAST_DOWNLOAD_DIR` | 本地临时下载和 TTS 分段目录 |
| `PODCAST_DB_PATH` | SQLite 状态库路径 |
| `CONTENT_LOOKBACK_DAYS` | 自动拉取内容时间窗口，默认 `7` 天；7 天外的视频会跳过 |
| `BOOTSTRAP_SOURCE` / `BOOTSTRAP_PLAYLIST_LIMIT` | 首次拉取频道视频的来源和上限 |

AI 链路：

| 配置项 | 用途 |
| --- | --- |
| `ASR_PROVIDER` | 转写 provider，支持 `local_whisper`、`dashscope` |
| `TEXT_PROVIDER` | 文本改写 provider，支持 `ollama`、`nvidia`、`zhipu`、`dashscope` |
| `TTS_PROVIDER` | 语音合成 provider，支持 `dashscope`、`macos_say` |
| `WHISPER_BIN` / `WHISPER_MODEL_PATH` | 本地 whisper.cpp 可执行文件和模型路径 |
| `WHISPER_LANGUAGE` / `WHISPER_EXTRA_ARGS` | 本地 Whisper 语言和额外参数 |
| `NVIDIA_API_KEY` / `NVIDIA_API_BASE` / `NVIDIA_MODEL` | NVIDIA MiniMax-M3 文本模型配置 |
| `NVIDIA_MAX_TOKENS` / `NVIDIA_TOP_P` / `NVIDIA_TIMEOUT_SECONDS` | NVIDIA 输出长度、采样和长请求超时配置 |
| `OLLAMA_API_BASE` / `OLLAMA_MODEL` / `OLLAMA_NUM_CTX` | 本地 Ollama 地址、模型和上下文窗口 |
| `OLLAMA_NUM_PREDICT` / `OLLAMA_TOP_P` / `OLLAMA_TIMEOUT_SECONDS` / `OLLAMA_KEEP_ALIVE` | Ollama 输出长度、采样、超时和模型保留时间 |
| `ZHIPU_API_KEY` / `ZHIPU_API_BASE` / `ZHIPU_MODEL` | 智谱文本模型配置 |
| `DASHSCOPE_API_KEY` / `DASHSCOPE_API_BASE` / `DASHSCOPE_COMPATIBLE_BASE` | 阿里百炼 ASR、文本兼容模式和 TTS 基础配置 |
| `DASHSCOPE_ASR_MODEL` / `DASHSCOPE_ASR_TIMEOUT_SECONDS` / `DASHSCOPE_ASR_POLL_SECONDS` | 阿里百炼 ASR 配置 |
| `DASHSCOPE_TRANSLATE_MODEL` | 阿里百炼文本模型配置 |
| `DASHSCOPE_TTS_MODEL` / `DASHSCOPE_TTS_FORMAT` / `DASHSCOPE_TTS_SAMPLE_RATE` / `DASHSCOPE_TTS_RATE` | 阿里百炼 TTS 配置 |
| `DASHSCOPE_WEBSOCKET_URL` / `DASHSCOPE_TTS_VOICE` / `DASHSCOPE_TTS_INSTRUCTIONS` | 阿里百炼 TTS 专属域名、音色和提示词配置 |
| `TRANSLATE_CHUNK_CHARS` | 长转写稿切块大小 |
| `CHINESE_PODCAST_TARGET_MINUTES` | 中文播客稿目标时长 |
| `TTS_SEGMENT_CHARS` | TTS 分段字符数 |
| `MACOS_TTS_VOICE` / `MACOS_TTS_RATE` | macOS 本地 TTS 音色和语速 |

YouTube 访问：

| 配置项 | 用途 |
| --- | --- |
| `YTDLP_COOKIES_FROM_BROWSER` | 从浏览器读取 YouTube cookies，默认留空；SSH/launchd 下 Chrome Keychain 可能无法解密 |
| `YTDLP_COOKIES` | 指向导出的 cookies 文件 |
| `YTDLP_JS_RUNTIMES` / `YTDLP_REMOTE_COMPONENTS` | yt-dlp 处理 YouTube 签名和组件的辅助配置 |

如果 YouTube 返回 `Sign in to confirm you're not a bot`，在本机浏览器登录 YouTube 后，可以临时在 `.env` 里加：

```env
YTDLP_COOKIES_FROM_BROWSER=chrome
YTDLP_JS_RUNTIMES=node
```

也可以使用导出的 cookies 文件：

```env
YTDLP_COOKIES=/absolute/path/to/youtube-cookies.txt
```

## 手动试跑

查看已启用频道：

```bash
scripts/run.sh --list-channels
```

先做 dry-run，只检查将处理哪些视频：

```bash
scripts/run.sh --dry-run
```

确认无误后正式运行：

```bash
scripts/run.sh
```

手动处理某个频道的一条视频：

```bash
scripts/run.sh --channel investopedia --url 'https://www.youtube.com/watch?v=VIDEO_ID'
```

## 手动处理会员视频

会员专享视频不能绕过权限下载；必须满足：

- 你的 YouTube 账号已经加入对应频道会员
- 本机 Chrome 已登录这个 YouTube 账号
- `.env` 中启用了可用 cookies。SSH/launchd 环境优先使用 `YTDLP_COOKIES=/absolute/path/to/youtube-cookies.txt`，避免 Chrome Keychain 解密失败

拿到会员视频 URL 后先 dry-run：

```bash
scripts/run.sh --dry-run --channel nana --url 'https://www.youtube.com/watch?v=VIDEO_ID'
```

确认能识别后正式处理：

```bash
scripts/run.sh --channel nana --url 'https://www.youtube.com/watch?v=VIDEO_ID'
```

脚本会下载音频、转成 128k MP3、上传 R2，并通过 Telegram 推送。

## 频道配置

频道统一维护在 `config/channels.json`。常用字段：

```json
{
  "key": "plain_bagel",
  "name": "The Plain Bagel",
  "homepage_url": "https://www.youtube.com/@ThePlainBagel",
  "channel_id": "",
  "bootstrap_days": 7,
  "language": "en",
  "generate_chinese_audio": true,
  "enabled": true
}
```

- `channel_id` 可以为空，脚本会用 `yt-dlp` 从频道主页解析。
- `generate_chinese_audio=true` 时，会按 `.env` 中的 provider 生成中文音频。
- 推荐混合方案：本地 Whisper 转写，本地 Ollama 或 NVIDIA MiniMax-M3 生成中文播客稿，阿里百炼 Sambert 合成音频。
- 英文原始音频只会临时保存在本地，任务结束后删除；最终保留中文 MP3。

## 中文音频配置

推荐配置：

```env
ASR_PROVIDER=local_whisper
TEXT_PROVIDER=ollama
TTS_PROVIDER=dashscope
WHISPER_MODEL_PATH=/absolute/path/to/ggml-small.en.bin
WHISPER_EXTRA_ARGS=-bs 1 -bo 1
OLLAMA_API_BASE=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3.5:4b
OLLAMA_NUM_CTX=8192
OLLAMA_NUM_PREDICT=4096
OLLAMA_TIMEOUT_SECONDS=900
TRANSLATE_CHUNK_CHARS=8000
DASHSCOPE_TTS_MODEL=sambert-zhide-v1
DASHSCOPE_TTS_FORMAT=wav
DASHSCOPE_TTS_SAMPLE_RATE=48000
DASHSCOPE_TTS_RATE=1.0
```

本地 Whisper 需要先安装 `whisper.cpp` 并下载模型文件。`WHISPER_MODEL_PATH` 必须指向本机模型文件。Mac mini M4 上已确认 `whisper.cpp` 会启用 Metal；`WHISPER_EXTRA_ARGS=-bs 1 -bo 1` 会降低 beam search 候选数，实测 120 秒样本约提升 13%，适合当前“转写后再交给 LLM 改写”的链路。

Sambert 通过 DashScope Python SDK 调用，不走通用 HTTP TTS 端点。`sambert-zhide-v1` 是新闻男声，也可以替换成免费清单里的其他 `sambert-*` 音色。

### Provider 配置说明

`ASR_PROVIDER` 支持：

- `local_whisper`：使用本地 `whisper.cpp`，推荐方案。需要设置 `WHISPER_MODEL_PATH`。
- `dashscope`：使用阿里百炼文件转写任务。需要 `DASHSCOPE_API_KEY` 和可公网访问的临时音频 URL。

`TEXT_PROVIDER` 支持：

- `ollama`：使用本机 Ollama，推荐 Mac mini 本地运行方案。需要先安装 Ollama 并拉取 `OLLAMA_MODEL`。
- `nvidia`：使用 NVIDIA 托管的 MiniMax-M3，当前已验证通过。需要 `NVIDIA_API_KEY`。
- `zhipu`：使用智谱 OpenAI 兼容接口。需要 `ZHIPU_API_KEY`，可能遇到 `429 Too Many Requests`。
- `dashscope`：使用阿里百炼兼容模式。需要 `DASHSCOPE_API_KEY`，免费额度耗尽或开启“仅使用免费额度”时会返回 `403 AllocationQuota.FreeTierOnly`。

`TTS_PROVIDER` 支持：

- `dashscope`：使用阿里百炼 TTS，推荐方案。当前配置默认走 `sambert-zhide-v1`。
- `macos_say`：使用 macOS 本地语音作为兜底，不依赖云端 TTS。

如果要临时切回 macOS 本地 TTS 兜底，可以改成：

```env
TTS_PROVIDER=macos_say
MACOS_TTS_VOICE=Tingting
MACOS_TTS_RATE=185
```

如果要使用本地 Ollama 文本模型，可以改成：

```env
TEXT_PROVIDER=ollama
OLLAMA_API_BASE=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3.5:4b
OLLAMA_NUM_CTX=8192
OLLAMA_NUM_PREDICT=4096
TRANSLATE_CHUNK_CHARS=8000
```

如果要切回智谱文本模型，可以改成：

```env
TEXT_PROVIDER=zhipu
ZHIPU_API_KEY=你的智谱 API Key
ZHIPU_API_BASE=https://open.bigmodel.cn/api/paas/v4
ZHIPU_MODEL=glm-4.7-flash
```

如果要切回阿里百炼 ASR 和文本模型，可以改成：

```env
ASR_PROVIDER=dashscope
TEXT_PROVIDER=dashscope
DASHSCOPE_ASR_MODEL=qwen3-asr-flash-filetrans
DASHSCOPE_TRANSLATE_MODEL=qwen-plus
```

如果要使用 NVIDIA 托管的 MiniMax-M3 生成中文播客稿，可以改成：

```env
TEXT_PROVIDER=nvidia
NVIDIA_API_KEY=你的 NVIDIA API Key
NVIDIA_API_BASE=https://integrate.api.nvidia.com/v1
NVIDIA_MODEL=minimaxai/minimax-m3
NVIDIA_MAX_TOKENS=8192
NVIDIA_TOP_P=0.95
NVIDIA_TIMEOUT_SECONDS=600
```

如果你在百炼控制台有业务空间专属域名，可以把 `DASHSCOPE_API_BASE` 改成专属域名，API Key 仍只放在 `.env`。

## 常见问题

### 智谱返回 429

`429 Too Many Requests` 说明智谱文本模型被限流，通常和 RPM、TPM、并发、余额或免费额度有关。可以稍后重试，或切换到 `TEXT_PROVIDER=nvidia`。

### 阿里百炼返回 403

如果返回 `AllocationQuota.FreeTierOnly`，说明免费额度已耗尽，或控制台开启了“仅使用免费额度”。需要在阿里百炼控制台补齐付费信息、关闭该模式，或不要把文本模型切到 `dashscope`。

### NVIDIA 长请求超时

完整播客稿生成可能超过 180 秒，推荐设置：

```env
NVIDIA_TIMEOUT_SECONDS=600
```

NVIDIA 接口使用 `response_format={"type":"json_object"}`，但模型仍可能偶发返回非法 JSON。脚本会抛出清晰错误，可以直接重跑同一条 URL。

### 失败后能否重跑

可以。`failed`、`failed_metadata`、`notification_failed` 和 `processing` 状态不会被视为已完成，同一条 URL 后续可以再次处理。

## 安装每天 08:00 定时任务

```bash
scripts/install_launchd.sh
```

查看日志：

```bash
tail -f logs/launchd.out.log
tail -f logs/launchd.err.log
```

## 只执行过期清理

```bash
scripts/run.sh --cleanup-only
```

## 给 Codex 的实现说明

如果要在另一台机器或另一个代码副本中复现 NVIDIA MiniMax-M3 文本模型接入，参考 [docs/to-codex-nvidia-minimax-m3.md](file:///Users/bytedance/projects/podcasts/docs/to-codex-nvidia-minimax-m3.md)。
