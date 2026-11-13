# web_fetch：原生优先的网页读取方案

状态：设计提案，尚未实现。核对日期：2026-11-13。

增加 `web_fetch`，让 agent 在搜索得到链接后读取网页正文，再基于实际读取内容回答。默认优先使用当前 provider 已适配的原生读取能力；没有可用适配时使用内置 HTTP 读取。搜索与读取分别配置、分别授权。

## 阅读入口

- [能力与路由](#能力与路由)：哪些 provider 可以直接接入。
- [工具契约](#工具契约)：参数、输出和内容边界。
- [配置与权限](#配置与权限)：默认行为与回退条件。
- [实现落点](#实现落点)：接入现有代码的位置。
- [先行端到端测试](#先行端到端测试)：实现前先写的验收用例。

## 能力与路由

原生能力按实际端点、协议、模型和凭据判断，不能只看 provider 名称，也不能把支持搜索等同于支持指定 URL 读取。

| 当前连接 | 首版 auto 路径 | 接入方式 |
|---|---|---|
| DeepSeek 官方连接，具备官方 API key | DeepSeek Fetch API | 保留客户端 `web_fetch`，执行时调用官方接口 |
| Anthropic 官方连接，模型支持 web fetch | Anthropic 服务端工具 | 请求中注入原生声明，隐藏同名客户端工具 |
| OpenAI Responses / Codex、xAI Responses | 内置 HTTP `web_fetch`；原生搜索照常保留 | 搜索内部的浏览事件单独展示，不声明不存在的 fetch 接口 |
| Google Code Assist、未知模型、未适配连接 | 内置 HTTP | 不从其他 Google 产品推断当前 Code Assist 协议能力 |
| 兼容代理 | 内置 HTTP | 仅允许显式启用已实现、已验证的原生协议适配 |

DeepSeek 官方接口是 `POST https://api.deepseek.com/v1/tools/fetch`，请求体为 `{"url":"…"}`，返回 `title` 和 Markdown `content`。复用官方连接凭据解析方式，每次调用读取当前凭据，目标网页永远不会收到 provider 的 API key。[Fetch API](https://api-docs.deepseek.com/api/fetch/)

Anthropic 首版使用基础版本 `web_fetch_20250910`，启用 citations，并设置调用次数及内容 token 上限。更高版本的动态过滤暂不引入。原生工具只读取其协议允许的、已经出现在上下文中的 URL；不能通过伪造用户消息绕过限制。[Web fetch tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool)

OpenAI 的 `open_page` / `find_in_page` 是 `web_search_call` 的 action；xAI 的 `web_search` 也包含网页浏览。这里作出的设计选择是保留这些原生浏览能力，同时提供可确定调用、可返回正文的客户端 `web_fetch`。首版不额外调用一次 LLM 来把搜索包装成 Fetch API。[OpenAI Web search](https://developers.openai.com/api/docs/guides/tools-web-search)、[xAI Web Search](https://docs.x.ai/developers/tools/web-search)

“原生优先”意味着优先使用语义满足当前操作的已验证适配；不会为了命名一致而把模型生成的摘要当作抓取原文。后续确认独立读取接口后，可以新增适配，不改变工具名。

## 工具契约

客户端工具保持最小参数：

```json
{
  "name": "web_fetch",
  "arguments": {
    "url": "https://example.com/article"
  }
}
```

- 单次读取一个 URL；需要多个页面时使用现有工具并行执行机制。
- 只接受 HTTP(S)、有效主机名、无用户名或密码的 URL，最大 8192 字符；后端更严格的限制由适配器处理。
- 首版不增加 `prompt` 参数，不在工具内部调用模型做摘要；读取结果由当前对话模型分析。
- `read_only=True`，仍经过现有工具预检、权限复核、取消和结果钩子。

客户端成功结果结构：

```json
{
  "url": "https://example.com/article",
  "final_url": null,
  "title": "Example article",
  "content": "# Example article\n\nPage content.",
  "format": "markdown",
  "backend": "deepseek",
  "retrieved_at": "<UTC timestamp>",
  "truncated": false
}
```

`final_url` 仅在后端确实提供或本地实际跟随重定向时填入；不能把输入 URL 冒充最终地址。`retrieved_at` 对独立 API 表示本次结果取得时间，不保证上游缓存刷新时间。未知字段保持未知。

模型可见内容包含来源与正文；UI 的 `details` 记录 backend、耗时、字符数和截断状态。正文超过现有工具输出预算时，通过现有落盘机制保存已取得的完整文本，输出可读取路径及明确截断标记。下载本身超限则返回错误，不把未完成响应标成完整正文。

Anthropic 服务端结果保留原始块供同 provider 回放，同时建立来源和正文的可移植视图。它使用自己的参数 schema，不能直接套用客户端参数或承诺相同截断精度。PDF 原始块可保留，但切换 provider 时只能输出已取得的可读文本或“PDF 正文不可移植”的标记，不能把 base64 当文本。

### 内置读取范围

首版支持 HTML、纯文本、Markdown；HTML 去除脚本、样式和明显导航噪声，保留标题、段落、列表、代码和链接。正文提取选用小型维护中的解析依赖，实施时先用文档页、论文摘要页和代码页面的端到端样本验证，再确定依赖。

首版不执行 JavaScript、不读取浏览器 cookie、不处理登录网页；内置 PDF 解析留待后续。收到 PDF、图片或其他二进制时返回明确的不支持错误。Anthropic 原生 PDF 能力按其实际结果保留；DeepSeek 不额外承诺文档未说明的 PDF 支持。

拟定内置边界：总时限 20 秒、最多 5 次重定向、解压后最多 2 MiB。分块读取、取消信号和总截止时间共同生效。上限适用于客户端获取内容，不虚称能限制服务端工具的内部网络行为。

## 配置与权限

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

| 配置 | 语义 |
|---|---|
| `mode: auto` | 当前 provider 原生优先；无适配或缺少可用原生凭据时，使用配置的客户端后端 |
| `mode: native` | 只允许当前 provider 已适配的原生能力；不可用或无法满足权限时明确报错 |
| `mode: builtin` | 固定使用 `backend`，支持 `http` 和显式选择的 `deepseek` |
| `mode: off` / `features.fetch: false` | 不向模型提供 fetch 能力 |
| `providers.<名称>.nativeFetch: false` | 禁用该连接的原生 fetch；不影响原生搜索 |
| `fetch.apiKeyEnv` | 可选的独立 DeepSeek 官方 key 环境变量；不沿用 `search.apiKeyEnv` |

`timeout`、`maxBytes` 是客户端执行限制；`maxContentTokens` 是 Anthropic 原生文本内容预算，不能作为 PDF 硬上限。`maxUses` 由 Runtime 统计本轮 fetch 次数，并传给支持该字段的原生工具；工具结果续轮与 `pause_turn` 不重置预算，新用户请求才重置。客户端失败尝试也计数。

新增 `/fetch [auto|http|deepseek|off]`，交互选择与持久化方式沿用 `/search`。`native` 保留为高级配置。搜索和读取开关独立：`/search off` 不等于关闭全部网络能力；命令状态中同时显示两项，避免误解。

### 权限

客户端路径按实际 URL 进入 `web_fetch(...)` 规则。DeepSeek 独立 Fetch API 同样执行这些检查。拥有工具的扩展不被内置路由覆盖，子 agent 未获分配此工具时也不得获得对应原生能力。

Anthropic 服务端执行不能在每个 URL 发出前经过本地钩子，因此复用当前原生搜索的范围审批机制，但使用独立的 fetch 权限。存在精确 URL 规则、结果钩子，或范围审批无法满足时，auto 改为客户端路径；native 明确失败。plan 允许只读读取，仍遵守 deny。辅助审批、摘要、压缩请求不启用 fetch。

内置 HTTP 默认只访问公网目标：阻止回环、私网、链路本地和保留地址，处理 IPv4/IPv6 与混合 DNS 答案；每次重定向重新检查目标及权限。DNS 检查与实际连接绑定到同一已验证地址，同时保持 TLS 主机名验证，不能只解析一次再让 HTTP 客户端另行解析。若无法满足绑定或代理环境的边界，明确失败，不放宽为未经验证的连接。

网页文本始终是不可信数据，不能修改系统指令、权限或触发秘密外传。内置请求不加载 cookie、netrc 或 provider Authorization；默认不自动继承环境代理。正文及下载文件进入现有会话输出目录，不写入项目源码或 Git。

### 失败与回退

1. 无原生适配、缺少原生凭据：auto 在请求前选客户端路径；native 报错。
2. 只有明确的“工具能力不支持”，且当前响应尚未开始任何服务端工具执行，才允许一次能力回退。
3. 401、402、403、429、超时、空内容、页面错误、工具调用失败或模型没有调用工具，都不是能力不支持。
4. DeepSeek 的 404 表示无法取得可用网页正文等情况，不能当作 Fetch API 不存在。403 表示安全策略拒绝；不自动切换后端绕过。[错误语义](https://api-docs.deepseek.com/guides/search/)
5. 服务端 search 或 fetch 任意一个已经开始后，传输中断均不得自动重放整个模型请求；保留已获得的结果和未完成状态。
6. search / fetch 各自缓存不可用能力，缓存键包含实际端点、协议、模型和凭据指纹；同一请求中可以分别降级一次，总数有界，取消时不重试。

工具错误返回给模型，明确区分未取得正文、只取得部分内容、内容类型不支持和网页不可访问。不给模型制造“已读全文”的成功结果。

## 实现落点

采用现有分层，先提取确实共享的端点与凭据判断，不先重构全部 provider 能力系统。

| 位置 | 计划变更 |
|---|---|
| `tools/web_fetch.py`（新增） | 客户端工具、DeepSeek 请求、内置 HTTP 读取、正文解析与限制 |
| `features/fetch.py`（新增） | 按调用 Runtime 的实际请求模型路由，配置、权限、能力缓存和回退 |
| `features/search.py` 及共享 helper | 抽取官方端点验证、DeepSeek 凭据绑定；保持搜索行为与配置兼容 |
| `ai/types.py` | 新增独立 `Context.web_fetch`；增加覆盖所有服务端工具的 started 状态，旧历史 `search_started` 仍可读取 |
| `ai/providers/anthropic.py` | 原生声明、`web_fetch_tool_result` 成功与错误块、流事件及来源 |
| `ai/transform.py`、来源 helper | 同 provider 原样回放；跨 provider 保留可读来源和正文，剥离不兼容内部块 |
| `harness/session.py` | fetch prepare/fallback、按能力回退上限、共享服务端执行重放保护与调用记账 |
| `harness/settings.py`、feature 注册、命令模块 | 默认配置、开关及 `/fetch` 选择器 |
| TUI / plain / JSON 事件层 | 区分搜索、读取和页内查找；展示 URL、backend、成功/失败、正文摘要和截断标记 |
| README、架构与测试文档 | 使用方法、内容范围、费用来源和验证入口 |

当前 `AssistantMessage.search_started`、`search_unsupported`、`add_source_links` 和 Anthropic 结果配对均带有搜索专属判断，不能只注册工具而漏掉回放、重试和 UI。跨 provider 转换也必须处理 fetch 文档引用：仅有字符/页码引用时，根据实际文档来源映射 URL，不能假设 citation 自带 URL。

OpenAI/xAI 原生浏览与客户端读取可能在一轮中共同出现。只保证每种独立工具路径不重复注册；不会同时移除原生搜索来强行做到“全局只有一个联网工具”。无法观察到原生正文时，不声称已经取得可再次读取的完整副本。

## 先行端到端测试

按照项目约定，实施时先写失败测试，再写代码。首选从真实 Virgo Session/CLI 入口驱动，连接本地假 provider / 网页 HTTP 服务，验证请求、权限、模型收到的结果、持久化和最终 UI。测试服务器使用测试专用传输注入，不引入产品级“允许私网”开关。

| 场景 | 验收结果 |
|---|---|
| 搜索 → 读取 → 回答 | 假网页中仅正文存在的事实进入后续模型上下文；答案引用实际来源 |
| DeepSeek 官方连接 | 只向官方 fetch 端点发送一次 URL；使用当前调用 agent 对应凭据；结果回到同一工具调用 |
| 凭据与代理 | 同名代理不自动接收原生声明；代理 key 不外发；官方别名、换 key、显式 key 和并行 Runtime 隔离正确 |
| Anthropic 原生 | 原生 fetch 与 search 可同时声明，无同名客户端 fetch；流事件、结果、引用与下一轮回放完整 |
| 模式和能力 | auto/native/builtin/off、角色工具白名单、自定义同名工具、模型切换与回退模型均遵守配置 |
| 精确权限 | deny、URL 规则、钩子、plan、范围审批失败、重定向目标变化在出网前生效 |
| 不支持与业务失败 | 明确能力不支持仅回退一次；404/403/401/402/429/超时/空内容均不冒充能力不支持 |
| 已执行后中断 | search 或 fetch 开始后中断，不重放原生调用；保留部分证据；取消不启动其他后端 |
| 内容和预算 | 中文编码、HTML 实体、脚本去除、相对链接、畸形 HTML、重定向循环、压缩超限、慢流和取消结果正确 |
| URL 与网络边界 | 非 HTTP、userinfo、超长 URL、私网/回环 IPv4/IPv6、混合 DNS、重绑定与跨域重定向均不能绕过检查 |
| 文本与二进制 | 内置 HTML/文本/Markdown 成功；PDF/二进制明确失败；原生 PDF 按实际内容保存且跨 provider 不传内部块 |
| 历史与显示 | 会话恢复、provider 切换、压缩、TUI/plain/JSON 都保留来源、错误和截断状态，不产生孤立工具结果 |
| 搜索回归 | 原有 `/search`、搜索原生路由、精确查询审批与辅助调用禁用行为保持原有语义 |

测试只验证可观察行为，不用假模型的固定回答作为事实核验成功的唯一依据：必须断言下一次模型请求中确实包含服务器返回的正文及来源。

若 DNS 绑定或 HTML 解析确实需要模块测试，先在测试文件中列出该模块所有已识别的失败方式，再补测试和实现；端到端测试仍覆盖关键链路。不要为这份纯设计文档新增占位测试。

## 交付顺序

1. DeepSeek Fetch API 与内置 HTTP 后端、最小工具契约、权限和输出预算；先解决当前 DeepSeek 使用场景。
2. Anthropic 原生适配，并补齐共享重放保护、引用迁移和持久化。
3. 配置选择器与三种 UI 验收、搜索回归，完成用户文档。
4. 后续再评估内置 PDF、JavaScript 渲染、缓存及其他 provider 的独立 URL 读取适配。

本提案不据截图判断论文信息是否真实；截图只说明了“给出链接和摘要”的使用场景。功能目标是让后续回答具有可检查的读取证据。
