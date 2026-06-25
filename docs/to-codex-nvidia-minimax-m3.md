# 给 Codex：接入 NVIDIA MiniMax-M3 文本模型

本文用于在另一个 `podcasts_bot` 代码副本中复现 NVIDIA MiniMax-M3 文本模型接入。目标是让英文频道的中文音频链路支持：

```text
本地 Whisper ASR -> NVIDIA MiniMax-M3 生成中文播客稿 -> DashScope Sambert TTS
```

## 背景

现有项目已经有文本模型抽象：

- `TEXT_PROVIDER=zhipu`：智谱 OpenAI 兼容接口
- `TEXT_PROVIDER=dashscope`：阿里百炼 OpenAI 兼容接口

需要新增：

- `TEXT_PROVIDER=nvidia`
- 默认模型：`minimaxai/minimax-m3`
- 接口地址：`https://integrate.api.nvidia.com/v1/chat/completions`

NVIDIA 接口是 OpenAI 兼容的 `/chat/completions`。请求必须带：

```http
Authorization: Bearer ${NVIDIA_API_KEY}
Accept: application/json
Content-Type: application/json
```

## 修改范围

只改以下文件：

- `src/podcast_sync.py`
- `.env.example`
- `README.md`

不要提交 `.env`，不要把真实 API Key 写进文档。

## 代码实现要求

在 `src/podcast_sync.py` 中新增 NVIDIA provider，保持和现有 `dashscope_chat_json`、`zhipu_chat_json` 一致的返回结构。

需要新增：

```python
def nvidia_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {env('NVIDIA_API_KEY', required=True)}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def nvidia_api_base() -> str:
    return env("NVIDIA_API_BASE", "https://integrate.api.nvidia.com/v1").rstrip("/")
```

需要新增：

```python
def nvidia_chat_json(messages: list[dict], temperature: float = 0.3) -> dict:
    payload = {
        "model": env("NVIDIA_MODEL", "minimaxai/minimax-m3"),
        "messages": messages,
        "max_tokens": int(env("NVIDIA_MAX_TOKENS", "8192")),
        "temperature": temperature,
        "top_p": float(env("NVIDIA_TOP_P", "0.95")),
        "stream": False,
        "response_format": {"type": "json_object"},
    }
    response = requests.post(
        f"{nvidia_api_base()}/chat/completions",
        headers=nvidia_headers(),
        json=payload,
        timeout=int(env("NVIDIA_TIMEOUT_SECONDS", "600")),
    )
    if not response.ok:
        raise RuntimeError(f"NVIDIA chat failed ({response.status_code}): {truncate_text(response.text, 500)}")
    data = response.json()
    content = data["choices"][0]["message"]["content"]
    try:
        return parse_json_object(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"NVIDIA chat returned invalid JSON: {truncate_text(content, 500)}") from exc
```

注意：

- `response_format={"type":"json_object"}` 已验证可用，用于降低 MiniMax-M3 输出非法 JSON 的概率。
- `NVIDIA_TIMEOUT_SECONDS` 推荐默认 `600`。完整中文播客稿生成可能超过 180 秒。
- 如果项目里还没有 `truncate_text` 或 `parse_json_object`，先复用现有等价函数；不要重复造复杂解析器。

在 `text_model_json` 中新增分支：

```python
if provider == "nvidia":
    return nvidia_chat_json(messages, temperature=temperature)
```

## `.env.example`

推荐默认链路：

```env
ASR_PROVIDER=local_whisper
TEXT_PROVIDER=nvidia
TTS_PROVIDER=dashscope
```

新增 NVIDIA 配置项：

```env
NVIDIA_API_KEY=
NVIDIA_API_BASE=https://integrate.api.nvidia.com/v1
NVIDIA_MODEL=minimaxai/minimax-m3
NVIDIA_MAX_TOKENS=8192
NVIDIA_TOP_P=0.95
NVIDIA_TIMEOUT_SECONDS=600
```

保留已有的智谱和 DashScope 配置项，作为可选 fallback。

## README 更新要点

README 需要说明：

- 当前已验证链路：本地 Whisper -> NVIDIA MiniMax-M3 -> DashScope Sambert TTS
- `TEXT_PROVIDER` 支持 `nvidia`、`zhipu`、`dashscope`
- `NVIDIA_API_KEY` 只放 `.env`
- 智谱 `429 Too Many Requests` 是限流问题
- DashScope `403 AllocationQuota.FreeTierOnly` 是免费额度或“仅免费额度”模式问题
- NVIDIA 长请求推荐 `NVIDIA_TIMEOUT_SECONDS=600`

## 验证步骤

先做语法检查：

```bash
.venv/bin/python -m py_compile src/podcast_sync.py
```

用最小请求验证 NVIDIA Key 和接口：

```bash
set -a; source .env; set +a; curl -sS -i -X POST "${NVIDIA_API_BASE:-https://integrate.api.nvidia.com/v1}/chat/completions" -H "Authorization: Bearer ${NVIDIA_API_KEY}" -H "Accept: application/json" -H "Content-Type: application/json" -d "{\"model\":\"${NVIDIA_MODEL:-minimaxai/minimax-m3}\",\"messages\":[{\"role\":\"user\",\"content\":\"只输出 JSON：{\\\"ok\\\":true}\"}],\"max_tokens\":256,\"temperature\":0.3,\"top_p\":0.95,\"stream\":false,\"response_format\":{\"type\":\"json_object\"}}"
```

期望返回：

```json
{"ok":true}
```

再跑一条英文频道视频：

```bash
scripts/run.sh --channel investopedia --url 'https://www.youtube.com/watch?v=VIDEO_ID'
```

成功后数据库中应为：

```text
status=uploaded
audio_language=zh
notified_at 不为空
```

可用以下命令确认：

```bash
sqlite3 data/podcasts.sqlite3 "select video_id, status, public_url is not null as has_public_url, notified_at is not null as notified, audio_language from videos where video_id='VIDEO_ID';"
```

## 已验证案例

在原机器上已验证：

```bash
scripts/run.sh --channel investopedia --url 'https://www.youtube.com/watch?v=mR4eOVo126Y'
```

结果：

```text
status=uploaded
audio_language=zh
notified=true
```
