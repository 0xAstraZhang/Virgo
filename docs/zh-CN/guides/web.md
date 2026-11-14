# 联网搜索与网页读取

[文档首页](../README.md) · [English](../../en/guides/web.md)

Virgo 可以检索来源并读取公开网页。搜索和读取分别配置、分别授权。路由依据模型、接口协议、服务地址和功能开关判断；不能仅凭 provider 名称就在任意代理地址上启用原生能力。

## 选择搜索后端

```text
/search auto
/search duckduckgo
/search deepseek
/search off
```

不带参数的 `/search` 打开选择器。`ddg` 和 `ds` 分别是两个显式后端的别名。选择立即生效，并保存为用户配置，不改变当前对话模型。

| 选项 | 行为 |
| --- | --- |
| `auto` | 优先使用已适配的原生能力，否则使用 DuckDuckGo |
| `duckduckgo` | 使用内置 DuckDuckGo HTML 搜索后端 |
| `deepseek` | 使用官方 DeepSeek Search API，需要可用密钥 |
| `off` | 关闭搜索 |

适配器包含受支持的 Anthropic、OpenAI Responses/Codex 和 xAI 原生搜索，以及独立的 DeepSeek Search API。Google Code Assist 和未知的兼容连接默认使用内置回退路径。协议兼容不代表一定支持原生功能。

DeepSeek 搜索可搭配其他对话模型使用。先配置 `/login api deepseek` 或 `DEEPSEEK_API_KEY`；显式选择时若没有可用的官方连接，会提示错误并保留原选择。`search.apiKeyEnv` 可指定独立密钥环境变量。DuckDuckGo 无需密钥，但可能限流或要求浏览器验证；Virgo 会明确报告该错误。

```json
{
  "search": {
    "mode": "auto",
    "backend": "duckduckgo",
    "timeout": 15,
    "maxUses": 3
  }
}
```

`mode` 支持 `auto`、`native`、`builtin` 和 `off`。`native` 在没有获准的原生路径时报错，`builtin` 使用选定的内置后端。`features.search=false` 完全关闭该功能。内置搜索接受 `query` 和可选的 `limit`（默认 5，最多 10）。DeepSeek 查询最多 128 个 Unicode 字符，DuckDuckGo 最多 2000 个字符。内置响应预算为 1 MiB，超时最高 30 秒。`maxUses` 控制支持该参数的原生每轮次数限制，其中 Anthropic 的范围为 1–10。

## 选择读取后端

```text
/fetch auto
/fetch http
/fetch deepseek
/fetch off
```

选择器与 `/search` 一样保存用户配置。自动模式优先使用已适配的原生读取能力，可以是独立 fetch，也可以是搜索与浏览合并的能力。如果选中的原生工具已经能够读取网页，Virgo 不会再暴露重复的 HTTP fetch 工具。

```json
{
  "fetch": {
    "mode": "auto",
    "backend": "http",
    "timeout": 20,
    "maxBytes": 2097152,
    "maxUses": 3,
    "maxContentTokens": 8000
  }
}
```

`mode` 支持 `auto`、`native`、`builtin` 和 `off`，`backend` 为 `http` 或 `deepseek`。`features.fetch=false` 关闭读取功能。超时必须大于 0 且不超过 60 秒，`maxBytes` 上限为 16 MiB，`maxUses` 范围为 1–10。原生服务使用其支持的限制参数；组合工具不一定提供独立的读取次数限制。

客户端 `web_fetch` 工具接受 `url`，返回来源信息、标题、正文、后端、读取时间和截断状态。显示正文最多为 32 KiB／2000 行；截断后，已取得的正文保存到 `.virgo/outputs/`，可按 `output_path` 继续读取。读取时间不保证上游缓存已经刷新。

内置 HTTP 后端支持 HTML、纯文本和 Markdown，不执行 JavaScript、不使用浏览器会话，也不解析 PDF。它验证公网地址，将连接绑定到已检查的 IP，并以原主机名验证 TLS；每次重定向都会复核。它不读取 cookie、netrc 凭据或环境代理。默认最多五次重定向、20 秒，以及解压前后各 2 MiB。

DeepSeek Fetch 使用已保存的官方连接、`DEEPSEEK_API_KEY` 或独立的 `fetch.apiKeyEnv`，不会隐式复用 `search.apiKeyEnv`，也不会将代理凭据发送到官方地址。

## 权限与失败

内置工具会将实际查询或 URL 交给权限检查。服务端原生工具可能先按用户请求的范围审批，再由厂商决定具体查询。查询级、URL 级规则和自定义工具钩子可能要求使用客户端路径；原生范围无法获批时，`auto` 可以改用该路径。

搜索和读取保持独立。关闭读取、固定 HTTP 或要求 URL 级审批时，会排除无法遵循这些限制的组合工具。关闭搜索后仍可保留独立读取。`providers.<id>.nativeSearch` 和 `nativeFetch` 可以关闭某个连接的原生能力；显式启用需要端点和协议确实兼容。

只有明确的“不支持该能力”响应才会触发有界回退。认证失败、限流、超时和空结果不会静默切换厂商。服务端操作已经开始后，Virgo 会保留证据并停止自动重放。来源和可读引用在切换模型后仍保留于会话历史。

实现：[能力类型](../../../src/virgo/ai/web.py)、[路由](../../../src/virgo/features/web.py)、[搜索](../../../src/virgo/features/search.py)、[读取](../../../src/virgo/features/fetch.py)。
