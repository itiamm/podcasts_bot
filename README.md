# YouTube Podcast Audio

把指定 YouTube 频道的长视频转成 MP3，上传到 Cloudflare R2，并通过 Telegram 推送频道名、标题、发布时间和音频链接。英文频道可用本地 Whisper 转写、智谱 GLM 改写中文播客稿，再通过阿里百炼 TTS 生成中文音频。

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
ZHIPU_API_KEY=你的智谱 API Key
DASHSCOPE_API_KEY=你的阿里百炼 API Key，用于 TTS
```

不要把 `.env` 提交或发到聊天里。

如果 YouTube 返回 `Sign in to confirm you're not a bot`，在本机浏览器登录 YouTube 后，可以在 `.env` 里加：

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

## 手动处理会员视频

会员专享视频不能绕过权限下载；必须满足：

- 你的 YouTube 账号已经加入对应频道会员
- 本机 Chrome 已登录这个 YouTube 账号
- `.env` 中启用了 `YTDLP_COOKIES_FROM_BROWSER=chrome`

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
- 默认混合方案：本地 Whisper 转写，智谱 GLM 生成中文播客稿，阿里百炼 Sambert 合成音频。
- 英文原始音频只会临时保存在本地，任务结束后删除；最终保留中文 MP3。

## 中文音频配置

默认推荐配置：

```env
ASR_PROVIDER=local_whisper
TEXT_PROVIDER=zhipu
TTS_PROVIDER=dashscope
WHISPER_MODEL_PATH=/absolute/path/to/ggml-small.en.bin
ZHIPU_API_BASE=https://open.bigmodel.cn/api/paas/v4
ZHIPU_MODEL=glm-4.7-flash
DASHSCOPE_TTS_MODEL=sambert-zhide-v1
DASHSCOPE_TTS_FORMAT=wav
DASHSCOPE_TTS_SAMPLE_RATE=48000
DASHSCOPE_TTS_RATE=1.0
```

本地 Whisper 需要先安装 `whisper.cpp` 并下载模型文件。`WHISPER_MODEL_PATH` 必须指向本机模型文件。

Sambert 通过 DashScope Python SDK 调用，不走通用 HTTP TTS 端点。`sambert-zhide-v1` 是新闻男声，也可以替换成免费清单里的其他 `sambert-*` 音色。

如果要临时切回 macOS 本地 TTS 兜底，可以改成：

```env
TTS_PROVIDER=macos_say
MACOS_TTS_VOICE=Tingting
MACOS_TTS_RATE=185
```

如果要切回阿里百炼 ASR 和文本模型，可以改成：

```env
ASR_PROVIDER=dashscope
TEXT_PROVIDER=dashscope
DASHSCOPE_ASR_MODEL=qwen3-asr-flash-filetrans
DASHSCOPE_TRANSLATE_MODEL=qwen-plus
```

如果你在百炼控制台有业务空间专属域名，可以把 `DASHSCOPE_API_BASE` 改成专属域名，API Key 仍只放在 `.env`。

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
