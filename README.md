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

## 手动处理会员视频

会员专享视频不能绕过权限下载；必须满足：

- 你的 YouTube 账号已经加入对应频道会员
- 本机 Chrome 已登录这个 YouTube 账号
- `.env` 中启用了 `YTDLP_COOKIES_FROM_BROWSER=chrome`

拿到会员视频 URL 后先 dry-run：

```bash
scripts/run.sh --dry-run --channel bafenban --url 'https://www.youtube.com/watch?v=VIDEO_ID'
```

确认能识别后正式处理：

```bash
scripts/run.sh --channel bafenban --url 'https://www.youtube.com/watch?v=VIDEO_ID'
```

脚本会下载音频、转成 128k MP3、上传 R2，并通过 Telegram 推送。

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
