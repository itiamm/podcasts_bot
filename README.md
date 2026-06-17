# YouTube Podcast Audio

把指定 YouTube 频道的长视频转成 MP3，上传到 Cloudflare R2，并通过 Telegram 推送频道名、标题、发布时间和 MP3 链接。

## 当前规则

- 频道：
  - 八分半：首次拉取最近 30 天视频
  - NaNa说美股：首次拉取最近 3 天视频
- 后续运行：只处理新增视频
- 内容类型：只处理长视频，排除 Shorts
- 时长限制：超过 3 小时跳过
- MP3 音质：128k
- 音频保留：30 天后从 R2 删除
- 定时：每天 08:00

## 初始化

```bash
cd /Users/bytedance/Documents/trae_projects/podcasts
cp .env.example .env
chmod +x scripts/run.sh scripts/install_launchd.sh
```

编辑 `.env`，填入：

```env
R2_ACCESS_KEY_ID=你的 R2 Access Key ID
R2_SECRET_ACCESS_KEY=你的 R2 Secret Access Key
TELEGRAM_BOT_TOKEN=你的 Telegram Bot Token
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

先做 dry-run，只检查将处理哪些视频：

```bash
scripts/run.sh --dry-run
```

确认无误后正式运行：

```bash
scripts/run.sh
```

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
