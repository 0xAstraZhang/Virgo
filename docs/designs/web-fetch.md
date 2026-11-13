# web_fetch：原生优先的网页读取方案

状态：已实现，随 PR #27 提交。核对日期：2026-11-13。

使用入口见 [README 网页读取](../../README.md#网页读取)，验证入口见 [测试说明](../TESTING.md#web-reading-capabilities)。本文保留能力组合、权限及回退的设计依据；HTTP 的 PDF/JavaScript 支持仍为后续范围。

增加网页读取能力，让 agent 在搜索得到链接后读取网页，再基于实际读取内容回答。以能力抽象统一路由：优先使用当前连接已适配的原生能力；原生工具已覆盖读取时，不再同时提供冗余的 HTTP `web_fetch`。没有可用原生路径时才提供客户端后端。搜索与读取分别表达配置和授权，组合原生工具必须同时满足它覆盖的能力约束。

## 阅读入口

- [能力与路由](#能力与路由)：哪些 provider 可以直接接入。
- [能力抽象](#能力抽象)：统一选择、去重与组合能力的处理。
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
| OpenAI Responses / Codex、xAI Responses，已确认支持原生浏览 | 原生搜索工具中的网页读取能力 | 仅注入组合原生工具，不再提供客户端 `web_fetch` |
| Google Code Assist、未知模型、未适配连接 | 内置 HTTP | 不从其他 Google 产品推断当前 Code Assist 协议能力 |
| 兼容代理 | 内置 HTTP | 仅允许显式启用已实现、已验证的原生协议适配 |

DeepSeek 官方接口是 `POST https://api.deepseek.com/v1/tools/fetch`，请求体为 `{"url":"…"}`，返回 `title` 和 Markdown `content`。复用官方连接凭据解析方式，每次调用读取当前凭据，目标网页永远不会收到 provider 的 API key。[Fetch API](https://api-docs.deepseek.com/api/fetch/)

Anthropic 首版使用基础版本 `web_fetch_20250910`，启用 citations，并设置调用次数及内容 token 上限。更高版本的动态过滤暂不引入。原生工具只读取其协议允许的、已经出现在上下文中的 URL；不能通过伪造用户消息绕过限制。[Web fetch tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-fetch-tool)

OpenAI 的 `open_page` / `find_in_page` 是 `web_search_call` 的 action；xAI 的 `web_search` 也包含网页浏览。将已确认支持的操作登记为一个组合原生工具提供的能力；当它覆盖读取时，统一路由隐藏客户端 `web_fetch`。无需伪造独立 Fetch API，也不增加额外 LLM 调用。[OpenAI Web search](https://developers.openai.com/api/docs/guides/tools-web-search)、[xAI Web Search](https://docs.x.ai/developers/tools/web-search)

“原生优先”按读取能力判断，不要求每家厂商都有名为 `web_fetch` 的独立工具。模型可在服务端原生读取页面，即使接口不向 Virgo 暴露完整正文，也不因此额外注入 HTTP 工具。仅记录实际可观察的动作、引用与内容，不把生成摘要当作原文。

## 能力抽象

新增小型 Web 能力描述与统一规划器，适用于所有当前及后续 provider。路由不出现 `if provider == "openai"` 一类分支；协议细节留在各适配器中。它只管理联网工具，不扩展成通用插件框架。

建议内部模型（设计伪代码）：

```python
WebOffer(
    id="<adapter-specific stable id>",
    native=True,
    covers=frozenset({"search", "fetch", "find"}),
    declaration={"type": "<wire tool type>"},  # 与 backend 二选一
    controls={},                     # 原生参数 -> (逻辑能力, 配置项)
    evidence="actions_and_citations", # 或 document_content
)

# 将 resolver 注册到 virgo.ai.web.ADAPTERS。
# resolver(model) 返回当前端点、协议、模型实际支持的 WebOffer 列表。
# Runtime.request_context 调用 features.web.Web.prepare 统一规划。
```

- `covers` 表示实际已验证的操作能力，不能根据工具名字或“支持搜索”自动补上 fetch/find。模型、协议、端点、账号或凭据影响支持范围时，由适配器据此生成 offer；未知能力按不可用处理，显式兼容配置也须匹配已有适配。
- `native` 与执行方式独立：DeepSeek 原生独立 API 是 native + client，Anthropic 服务端 fetch 和支持浏览的组合工具是 native + hosted，内置 HTTP 是非 native + client。
- `declaration` 表示 hosted，`backend` 表示 client；`covers` 中的能力作为不可拆分集合处理。`controls` 声明可映射的预算参数，精确 URL 规则和钩子由统一权限逻辑过滤。`evidence` 描述宿主能取得正文还是只有动作与引用；它不决定是否提供重复工具。
- 协议适配器负责 offer 对应的请求声明或执行器、事件归一化及错误分类。规划器只读取描述，生成声明集合、客户端工具集合与禁用原因。
- 逻辑能力与模型工具名分开：角色的 `web_fetch` 授权映射为 fetch 能力，`web_search` 授权映射为 search 能力。组合工具需要它可执行的全部能力均获授权；不通过改名扩大子 agent 的权限。

### 统一选择与去重

1. 汇总当前 Runtime 的工具白名单、模式、权限及用户扩展，得到允许的能力集合。辅助审批、摘要、压缩调用的集合为空。
2. 过滤不能满足策略或已缓存为不可用的 offer。不可拆分的组合工具只要包含一项被禁用或无法授权的能力，就不能注入；不能靠 prompt 要求模型不调用来代替约束。
3. 对每个能力先应用显式后端选择，再在 auto/native 模式优先选择可用原生 offer。一个组合 offer 可以同时覆盖 search/fetch/find，其声明只生成一次；被它覆盖的客户端 `web_fetch` 不进入请求。
4. auto 仅为尚未覆盖的能力补客户端后端；native 存在未覆盖能力时给出具体原因。组合 offer 与固定客户端后端发生重叠时，先移除组合 offer，再为其余能力重算路径。
5. 扩展拥有的同名工具和已有钩子约束优先保留；存在重叠时排除内置组合 offer，不覆盖扩展行为。

`fetch off`、`nativeFetch: false`、固定 HTTP、精确 URL 审批或角色缺少 fetch 权限，都可能排除含 fetch 的组合原生工具。search 若允许 auto，可改走客户端搜索；若强制 native 且没有可单独搜索的适配，则明确报告冲突。反过来，search off 也不能偷偷启用不可拆分的 search+fetch 工具：fetch auto 使用独立客户端读取，fetch native 无合规路径时报错。配置界面展示最终路径与原因。

`find` 是页面阅读的附属操作，随 fetch 权限控制，首版不新增配置或客户端工具。规划器以能力覆盖去重，而非以工具名称去重；同一连接新增一种组合方式不需要修改选择算法。

## 工具契约

仅在规划器选择客户端读取路径时，向模型暴露以下工具；已选原生组合工具覆盖 fetch 时，不发送这份声明。客户端工具保持最小参数：

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

首版支持 HTML、纯文本、Markdown；HTML 去除脚本、样式和明显导航噪声，保留标题、段落、列表、代码和链接。正文提取使用标准库 HTMLParser，无新增依赖；通过 HTML、中文编码、代码段、相对链接及畸形链接的工具循环验证。它是保守的文本提取器，不承诺浏览器级排版或全文识别。

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
| `providers.<名称>.nativeFetch: false` | 禁用该连接的原生读取；含读取且不可拆分的原生工具也被排除，搜索重新选择合规路径 |
| `fetch.apiKeyEnv` | 可选的独立 DeepSeek 官方 key 环境变量；不沿用 `search.apiKeyEnv` |

`timeout`、`maxBytes` 是客户端执行限制；`maxContentTokens` 仅映射给声明支持文本内容预算的适配，不能作为 PDF 硬上限。`maxUses` 由 Runtime 限制客户端本轮 fetch 次数，并传给支持该字段的原生工具；客户端失败尝试也计数，工具结果续轮与 `pause_turn` 不重置预算。对无法分别限制读取次数的组合原生工具，展示 provider 自身限制，不把事后观察计数宣称为可执行的硬上限。需要严格读取次数限制时使用 `/fetch http` 或可表达该预算的独立原生工具；组合工具不会伪称实现同样限制。

新增 `/fetch [auto|http|deepseek|off]`，交互选择与持久化方式沿用 `/search`。`native` 保留为高级配置。搜索和读取开关分别表达意图：`/search off` 不等于关闭全部网络能力。不可拆分的组合工具由统一规划器处理冲突；`/fetch` 在非交互状态中显示上次请求的两项路径及已知降级原因；配置变化在下一次请求重新计算。

### 权限

客户端路径按实际 URL 进入 `web_fetch(...)` 规则。DeepSeek 独立 Fetch API 同样执行这些检查。拥有工具的扩展不被内置路由覆盖，子 agent 未获分配此工具时也不得获得对应原生能力。

所有 hosted offer 都按其声明的权限粒度审批：独立读取使用 fetch 权限，组合工具须覆盖搜索和读取的范围授权。服务端动作不能在每个 URL 发出前经过本地钩子；存在精确规则、结果钩子，或范围审批无法满足时，排除无法表达约束的整个 offer，auto 重算客户端路径，native 无可用路径则明确失败。plan 允许只读读取，仍遵守 deny。辅助审批、摘要、压缩请求不启用任何联网 offer。

内置 HTTP 默认只访问公网目标：阻止回环、私网、链路本地和保留地址，处理 IPv4/IPv6 与混合 DNS 答案；每次重定向重新检查目标及权限。DNS 检查与实际连接绑定到同一已验证地址，同时保持 TLS 主机名验证，不能只解析一次再让 HTTP 客户端另行解析。若无法满足绑定或代理环境的边界，明确失败，不放宽为未经验证的连接。

网页文本始终是不可信数据，不能修改系统指令、权限或触发秘密外传。内置请求不加载 cookie、netrc 或 provider Authorization；默认不自动继承环境代理。正文及下载文件进入现有会话输出目录，不写入项目源码或 Git。

### 失败与回退

1. 无原生适配、缺少原生凭据：auto 在请求前选客户端路径；native 报错。
2. 只有明确的“工具能力不支持”，且当前响应尚未开始任何服务端工具执行，才允许一次能力回退。
3. 401、402、403、429、超时、空内容、页面错误、工具调用失败或模型没有调用工具，都不是能力不支持。
4. DeepSeek 的 404 表示无法取得可用网页正文等情况，不能当作 Fetch API 不存在。403 表示安全策略拒绝；不自动切换后端绕过。[错误语义](https://api-docs.deepseek.com/guides/search/)
5. 服务端 search 或 fetch 任意一个已经开始后，传输中断均不得自动重放整个模型请求；保留已获得的结果和未完成状态。
6. 按实际 offer 缓存不支持状态，键包含适配 ID、工具版本、实际端点、协议、模型和凭据指纹。组合工具整体不支持时同时撤销它覆盖的能力；只有适配器能明确定位到某项操作不支持时才缩小覆盖范围。每个 offer 每次生成最多降级一次，另设整次生成的重试总上限，取消时不重试。重新规划必须先去除旧声明，再补客户端工具，不能让原生读取与 HTTP fetch 同时暴露。

工具错误返回给模型，明确区分未取得正文、只取得部分内容、内容类型不支持和网页不可访问。不给模型制造“已读全文”的成功结果。

## 实现落点

采用现有分层，在联网工具范围内引入能力描述和统一规划器；端点、凭据与协议差异封装在适配器，不散落在 search/fetch 服务和 UI 中。

| 位置 | 职责 |
|---|---|
| `tools/web_fetch.py`（新增） | 客户端工具、DeepSeek 请求、内置 HTTP 读取、正文解析与限制 |
| `ai/web.py`（新增）及 provider 适配器 | `WebOffer`、能力/约束描述、原生声明与事件归一化；不依赖 agent/harness |
| `features/web.py`（新增） | 统一规划 search/fetch 路径、组合能力去重、权限约束及 offer 不可用缓存 |
| `features/fetch.py`、`features/search.py`、`features/web_access.py` | 注册逻辑工具与配置，交给统一规划器选择；共享权限、调用 Runtime 绑定和 DeepSeek 凭据解析 |
| `ai/types.py` | 增加规划后的原生联网声明集合，每个 offer 只出现一次；兼容现有 `Context.web_search` 入口；增加覆盖所有服务端工具的 started 状态，旧历史 `search_started` 仍可读取 |
| `ai/providers/*` 的已支持适配器 | 声明自身已验证的能力组合；Anthropic fetch 块、OpenAI/xAI 浏览动作等映射为统一事件，保留原始协议对象 |
| `ai/transform.py`、来源 helper | 同 provider 原样回放；跨 provider 保留可读来源和正文，剥离不兼容内部块 |
| `harness/session.py` | 每次实际模型请求统一规划一次；回退移除失败 offer 后重新规划，共享重放保护与调用记账 |
| `harness/settings.py`、feature 注册、命令模块 | 默认配置、开关及 `/fetch` 选择器 |
| TUI / plain / JSON 事件层 | 区分搜索、读取和页内查找；展示 URL、backend、成功/失败、正文摘要和截断标记 |
| README、架构与测试文档 | 使用方法、内容范围、费用来源和验证入口 |

保留旧的 `AssistantMessage.search_started` 与 `search_unsupported` 兼容入口，新增 `native_started` 和 `fetch_unsupported`，统一服务端重放保护。跨 provider 转换也必须处理 fetch 文档引用：仅有字符/页码引用时，根据实际文档来源映射 URL，不能假设 citation 自带 URL。

去重不变量：当选中的原生 offer 覆盖 fetch 时，请求中没有内置客户端 `web_fetch`；同一原生声明不会因同时覆盖 search 和 fetch 被重复注入。回退后的新请求可以改用客户端工具，但不会与失效的原生声明并存。无法观察到原生正文时，只保留实际动作与引用，不声称已经取得可再次读取的完整副本。

## 先行端到端测试

本次实现先编写必要的失败用例，再实现对应行为。测试从真实 Virgo Session/CLI 入口驱动，连接本地假 provider / 网页 HTTP 服务，验证请求、权限、模型收到的结果、持久化和最终 UI。测试服务器使用测试专用传输注入，不引入产品级“允许私网”开关。

| 场景 | 验收结果 |
|---|---|
| 搜索 → 读取 → 回答 | 假网页中仅正文存在的事实进入后续模型上下文；答案引用实际来源 |
| DeepSeek 官方连接 | 只向官方 fetch 端点发送一次 URL；使用当前调用 agent 对应凭据；结果回到同一工具调用 |
| 凭据与代理 | 同名代理不自动接收原生声明；代理 key 不外发；官方别名、换 key、显式 key 和并行 Runtime 隔离正确 |
| Anthropic 原生 | 原生 fetch 与 search 可同时声明，无同名客户端 fetch；流事件、结果、引用与下一轮回放完整 |
| 原生组合能力 | OpenAI/xAI 已确认支持浏览的适配只注入一次原生工具；不暴露客户端 `web_fetch`，不发生 HTTP 网页读取 |
| 新适配无厂商特判 | 注册一个未知厂商名的假适配，分别声明独立 fetch、search+fetch、仅 search；无需修改规划器就能正确路由和去重 |
| 模式和能力 | auto/native/builtin/off、角色工具白名单、自定义同名工具、模型切换与回退模型均遵守配置 |
| 组合能力冲突 | search off、fetch off、nativeFetch false、固定 HTTP、精确 URL 规则与角色缺少某项权限，都在发送请求前排除不合规组合 offer；auto 合规降级，native 无可用路径明确报错 |
| 组合能力回退 | 整个 offer 不支持时撤销其所有能力并重算；声明与客户端工具不重叠，缓存和重试有界 |
| 精确权限 | deny、URL 规则、钩子、plan、范围审批失败、重定向目标变化在出网前生效 |
| 不支持与业务失败 | 明确能力不支持仅回退一次；404/403/401/402/429/超时/空内容均不冒充能力不支持 |
| 已执行后中断 | search 或 fetch 开始后中断，不重放原生调用；保留部分证据；取消不启动其他后端 |
| 内容和预算 | 中文编码、HTML 实体、脚本去除、相对链接、畸形 HTML、重定向循环、压缩超限、慢流和取消结果正确 |
| URL 与网络边界 | 非 HTTP、userinfo、超长 URL、私网/回环 IPv4/IPv6、混合 DNS、重绑定与跨域重定向均不能绕过检查 |
| 文本与二进制 | 内置 HTML/文本/Markdown 成功；PDF/二进制明确失败；原生 PDF 按实际内容保存且跨 provider 不传内部块 |
| 历史与显示 | 会话恢复、provider 切换、压缩、TUI/plain/JSON 都保留来源、错误和截断状态，不产生孤立工具结果 |
| 搜索回归 | 原有独立搜索路径与配置保持兼容；组合工具按新增的能力约束处理冲突，精确审批与辅助调用禁用不能被绕过 |

测试只验证可观察行为，不用假模型的固定回答作为事实核验成功的唯一依据。客户端和返回文档内容的原生路径，断言实际正文与来源进入回放；不暴露正文的组合原生路径，断言原生声明、读取动作、引用及持久化正确，并断言没有冗余 HTTP 工具或请求。假服务测试验证协议集成，不宣称证明真实 provider 的读取质量。

若 DNS 绑定或 HTML 解析确实需要模块测试，先在测试文件中列出该模块所有已识别的失败方式，再补测试和实现；端到端测试仍覆盖关键链路。测试通过真实会话、SDK、假 HTTP 服务和 Textual 入口验证实现，不添加占位测试。

## 实施顺序

1. 先写统一规划器的端到端用例，包括未知厂商假适配、组合能力去重、权限冲突和回退；再实现能力描述及规划器。
2. 接入 DeepSeek、Anthropic、OpenAI/xAI 已验证能力与内置 HTTP 后端，补齐共享重放保护、引用迁移和持久化。
3. 配置选择器与三种 UI 验收、搜索回归，完成用户文档。
4. 后续再评估内置 PDF、JavaScript 渲染、缓存及其他 provider 的独立 URL 读取适配。

本提案不据截图判断论文信息是否真实；截图只说明了“给出链接和摘要”的使用场景。功能目标是让后续回答具有可检查的读取证据。
