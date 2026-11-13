# ChatGPT Plus 建单 HAR 对照分析

分析日期为 2026 年 11 月 13 日。文中时间统一使用香港时间 UTC+8。

本记录汇总三份浏览器 HAR 与一次项目实际建单记录，核对登录方式、认证会话、创建结账参数、Sentinel 数据和错误响应。这里的建单成功指创建接口返回 HTTP 200，并取得随后可以读取的结账会话。项目本地订单接口对应返回 HTTP 201。

最早失败的 `download` 同样采用了 Session 提取 Cookie 后导入浏览器的登录方式。另一份使用相同登录方式的 `download1` 建单成功，账号密码登录的 `download2` 也建单成功。`download1` 与 `download2` 使用同一个账号和设备 ID，最早失败的 `download` 使用另一个账号。项目实测使用成功样本的账号与同一枚访问令牌，也成功创建了菲律宾区 Plus 账单。

失败样本的直接证据是创建接口返回“异常活动”，随后账号查询返回 `token_revoked`，会话刷新继续返回同一枚已被撤销的访问令牌。触发上游异常活动判定的具体因素仍待确认。

## 样本与登录方式

登录方式依据用户的实际操作确认。JWT 中的认证时间用于描述令牌携带的认证历史。

| 样本 | 本轮登录或执行方式 | 账号与设备关系 | 创建结账结果 |
|---|---|---|---|
| `download` | Session 提取 Cookie 后导入浏览器 | 与两份浏览器成功样本的账号、用户及设备标识均不同 | HTTP 400，异常活动 |
| `download1` | Session 提取 Cookie 后导入浏览器 | 与 `download2` 的账号、用户及设备标识相同 | HTTP 200，取得结账会话 |
| `download2` | 账号密码登录浏览器 | 与 `download1` 的账号、用户及设备标识相同 | HTTP 200，取得结账会话 |
| 项目实测 | 读取 `session.txt`，调用项目现有建单接口 | 与 `download1` 为同一账号、同一访问令牌，建单设备 ID 与浏览器样本不同 | 上游 HTTP 200，本地接口 HTTP 201 |

身份核对比较了 JWT 的 `sub`、`https://api.openai.com/auth` 中的 `chatgpt_user_id` 与 `chatgpt_account_id`，以及建单请求中的 `oai-device-id`。文档保留一致性结果，原始身份标识保存在来源文件中。

| 样本 | 抓包时间范围 | 请求记录数 | 建单请求索引 |
|---|---|---:|---|
| `download` | 16:39:16.128 至 16:42:26.037 | 223 | `entries[85]` |
| `download1` | 16:58:30.798 至 16:59:12.019 | 156 | `entries[24]` |
| `download2` | 17:50:37.298 至 17:52:46.360 | 296 | `entries[119]` |
| 项目实测 | 17:44:01.605 至 17:44:26.250，按请求开始时间统计 | 6 | `entries[3]` |

`entries[n]` 使用 HAR 数组从 0 开始的索引。HTTP 请求发生的时间与请求完成时间之间存在响应耗时。

## 失败样本的请求时间线

`download` 在创建账单之前能够正常查询账号，创建结账时收到异常活动响应，随后账号查询报告令牌已被撤销。

| 时间 | HAR 索引 | 请求 | 结果 |
|---|---|---|---|
| 16:40:10.751 | `entries[76]` | `GET /backend-api/accounts/check/v4-2023-04-27` | HTTP 200，可读取账号状态 |
| 16:40:15.974 | `entries[80]` | `GET /backend-api/wham/accounts/check` | HTTP 200 |
| 16:40:20.374 | `entries[83]` | `POST /backend-api/sentinel/req`，flow 为 `chatgpt_checkout` | HTTP 200，返回挑战数据 |
| 16:40:20.800 | `entries[84]` | `POST /backend-api/sentinel/ping` | HTTP 200 |
| 16:40:21.032 | `entries[85]` | `POST /backend-api/payments/checkout` | HTTP 400，提示异常活动 |
| 16:40:29.662 | `entries[87]` | `GET /backend-api/wham/accounts/check` | HTTP 401，错误码为 `token_revoked` |
| 16:40:29.891 | `entries[88]` | `GET /api/auth/session` | HTTP 200，返回的 accessToken 与被拒绝的令牌相同 |

创建接口的错误正文为以下内容。

```json
{
  "detail": "Our systems have detected unusual activity. Please try again later."
}
```

随后账号接口的错误正文中包含以下字段。

```json
{
  "error": {
    "message": "Encountered invalidated oauth token for user, failing request",
    "type": null,
    "code": "token_revoked",
    "param": null
  }
}
```

该访问令牌的 `iat` 为 11 月 13 日 16:26:39，`exp` 为 11 月 23 日 16:26:38。服务端在令牌记录的到期时间之前报告了撤销状态。会话接口返回 HTTP 200、包含 accessToken，并不足以确认认证已恢复，仍需比较令牌是否更新及受保护接口的实际返回结果。

这组时间线支持“升级请求遭到异常活动拦截，随后账号请求报告令牌已撤销”的判断。风控响应与撤销之间的内部处理机制、命中的规则和触发因素需要更多服务端证据。现有 HAR 能直接确认响应顺序与令牌复用情况。[失败 HAR](/C:/Users/hdd99/Downloads/download)

## 两份成功浏览器记录的对照

`download1` 与 `download2` 的登录用户、ChatGPT 账号及设备 ID 一致，建单请求使用不同的访问令牌与认证会话标识。两份创建结账的 JSON 正文仅 `purchase_attempt_id` 的值不同。

| 核对项 | `download1` | `download2` |
|---|---|---|
| 本轮操作方式 | Session 提取 Cookie 后导入浏览器 | 账号密码登录 |
| 访问令牌签发时间 `iat` | 11 月 13 日 14:07:50 | 11 月 13 日 17:50:57 |
| 访问令牌到期时间 `exp` | 11 月 23 日 14:07:50 | 11 月 23 日 17:50:57 |
| 密码认证时间 `pwd_auth_time` | 11 月 5 日 18:56:08.292 | 11 月 13 日 17:50:56.786 |
| 建单时令牌已签发时长 | 约 170.8 分钟 | 约 1.1 分钟 |
| 认证会话标识 | 延续原会话 | 更新为新会话标识 |
| 抓包中的 `/api/auth/session` | 没有对应记录 | 3 次，均返回 HTTP 200 |
| `oai-telemetry` | `[1,null]` | 8 项数值的数组 |
| 建单请求中记录的 Cookie 数量 | 0，Cookie 请求头也未记录 | 32 |
| 创建账单 | HTTP 200 | HTTP 200 |

`pwd_auth_time` 按毫秒时间戳转换，`iat` 与 `exp` 按秒时间戳转换。最早失败的 `download` 虽然本轮也采用 Cookie 导入，其令牌仍携带 11 月 13 日 16:26:36.479 的密码认证时间。因此，本轮登录方式应依据操作记录确认。

`download2` 的 JWT 用户资料中增加了 `given_name`、`family_name`、`picture` 等字段，`name` 字段的值也发生变化。认证会话字段 `session_id` 与 `chatgpt_login_finalizer_auth_session_id` 同时更新。这些属于本次观察到的认证上下文差异，资料字段变化对建单风控的独立影响仍待验证。

`download2` 记录了认证域的 Cloudflare 请求、页面初始化、聊天 Sentinel 的 `prepare` 和 `finalize` 请求，以及会话读取。其三次 `/api/auth/session` 返回的访问令牌与最终建单使用的令牌一致，响应还设置了分片 Session Cookie 和 `oai-client-session-epoch`。

`download1` 的记录集中在升级窗口及结账页加载阶段，Cookie 请求头与登录初始化记录存在缺口。该文件可用于核对建单认证头、正文与 Sentinel，实际发送的完整 Cookie 集合仍需其他记录佐证。[Cookie 导入成功 HAR](/C:/Users/hdd99/Downloads/download1)、[密码登录成功 HAR](/C:/Users/hdd99/Downloads/download2)

## 创建账单的正文与请求头

三份浏览器 HAR 均调用以下创建接口。

```http
POST https://chatgpt.com/backend-api/payments/checkout
Content-Type: application/json
Authorization: Bearer <访问令牌>
```

两份成功浏览器记录的共同请求正文如下，随机请求编号使用占位符表示。

```json
{
  "plan_name": "chatgptplusplan",
  "checkout_source": "chatgpt_web_merged",
  "entry_point": "all_plans_pricing_modal",
  "billing_details": {
    "country": "US",
    "currency": "USD"
  },
  "supports_saved_payment_link": true,
  "checkout_ui_mode": "custom",
  "checkout_appearance": "light",
  "cancel_url": "https://chatgpt.com/?codex_upgrade_location=sidebar+profile#pricing",
  "purchase_attempt_id": "<每次生成的 UUID>"
}
```

| 字段或环境 | `download` | `download1` | `download2` | 项目实测 |
|---|---|---|---|---|
| 套餐 | Plus | Plus | Plus | Plus |
| 国家与币种 | US、USD | US、USD | US、USD | PH、PHP |
| `checkout_ui_mode` | 未携带 | `custom` | `custom` | `custom` |
| `entry_point` | `all_plans_pricing_modal` | 相同 | 相同 | 相同 |
| 页面入口 | 账单设置 | 首页侧栏升级 | 首页侧栏升级 | 项目创建结账接口 |
| 取消后返回地址 | 账单设置中的定价入口 | 首页升级入口 | 首页升级入口 | `/?cta_tab=personal` |
| UA | Windows、Chrome 157 | Windows、Chrome 157 | Windows、Chrome 157 | Windows、Chrome 148 |
| `oai-language` | `zh-CN` | `zh-CN` | `zh-CN` | `en-PH` |
| `oai-telemetry` | 8 项数值数组 | `[1,null]` | 8 项数值数组 | `production` |
| 独立的 `openai-sentinel-so-token` 头 | 未记录 | 未记录 | 未记录 | 有值 |

`download` 的确少了 `checkout_ui_mode`，页面入口和 `cancel_url` 也不同。该样本还同时更换了账号、设备和访问令牌，当前比较无法将异常活动结果单独归因于某一个字段。

两份浏览器成功请求都记录了 `x-openai-rate-limit-join-workspace=1` 与 `x-openai-rejoin-pro-cta=1`。项目建单代码未添加这两个标志，本次项目实测仍获得 HTTP 200。它们在其他场景中的作用需要更多证据。

项目通过 `UpstreamCheckoutAdapter` 调用 `ChatGPTClient.create_checkout` 创建账单。该路径的 `oai-telemetry` 当前固定为 `production`，正文已经包含 `checkout_ui_mode=custom`，并根据所选地区生成国家和币种。付款协议中另有建单辅助方法，本次实测执行的是前述适配器入口。[建单适配器](/C:/Users/hdd99/Desktop/myshop/gpt-subscribe-studio/services/checkout/src/gss_checkout/adapter.py:164)、[正文构造](/C:/Users/hdd99/Desktop/myshop/gpt-subscribe-studio/packages/core/src/gss_core/chatgpt.py:381)、[请求头构造](/C:/Users/hdd99/Desktop/myshop/gpt-subscribe-studio/packages/core/src/gss_core/chatgpt.py:548)

## Sentinel 字段与关联关系

三份浏览器建单请求都携带 `openai-sentinel-token`，内容包括 `p`、`t`、`c`、`id` 和 `flow`，本次均核对到以下结果。

| 核对项 | 三份浏览器记录 | 项目实测 |
|---|---|---|
| `flow` | `chatgpt_checkout` | `chatgpt_checkout` |
| `p、t、c、id、flow` 是否均有值 | 是 | 是 |
| `c` 是否对应本次 Sentinel 响应的 `token` | 是 | 是 |
| `id` 是否与建单设备请求头一致 | 是 | 是 |
| 浏览器挑战响应中的 `turnstile.required` | 三份均为 `true` | 另有完整原始响应供复核 |
| 浏览器挑战响应中的 `proofofwork.required` | 三份均为 `true` | Node 返回 `powReq=true` |
| Node 返回的 `hasT` 和 `hasSo` | 浏览器 HAR 无此 Node 元数据 | 两者均为 `true` |

三个浏览器样本的 `c` 与前一条相同 flow 的挑战响应匹配，提交建单时仍处于挑战响应记录的有效时间内。这里核对的是字段存在性、关联关系与时间，最终接收结果以创建接口的响应为依据。

代码中仍有一个需要关注的边界条件。Node 桥允许把尚未取得的 `t` 写成空字符串，Python 的 `header_value` 只检查 `p` 与 `c`。在上游要求对应挑战的场景，应结合挑战要求观察 `t` 的取得情况。最早失败的浏览器 HAR 中 `t` 已有值，项目本次成功实测的 `t` 也有值。此前项目失败日志没有保存该字段状态，现有记录无法确认空 `t` 是否曾出现在那两次项目失败请求中。[Node 桥输出](/C:/Users/hdd99/Desktop/myshop/gpt-subscribe-studio/packages/core/src/gss_core/sentinel_assets/sentinel_bridge.js:536)、[Python 字段检查](/C:/Users/hdd99/Desktop/myshop/gpt-subscribe-studio/packages/core/src/gss_core/sentinel.py:116)

## 项目菲律宾区 Plus 实测

2026 年 11 月 13 日 17:44，在独立诊断进程中调用项目现有 `/api/orders` 接口，使用用户保存的 `session.txt` 和当时的代理配置，参数为 Plus、PH、正价模式。报价采用项目默认菲律宾账单地址。

该实测与 `download1` 的账号、用户标识和访问令牌一致，设备 ID 不同。实际建单 UA 为 Windows、Chrome 148，语言为 `en-PH`。项目对其同一建单请求中的设备头与 Sentinel 标识保持了一致。

| 顺序 | 上游请求 | 状态 | 约耗时 |
|---:|---|---:|---:|
| 1 | `GET /api/auth/session` | 200 | 4,884 ms |
| 2 | `GET /` | 200 | 4,725 ms |
| 3 | `POST /backend-api/sentinel/req` | 200 | 6,335 ms |
| 4 | `POST /backend-api/payments/checkout` | 200 | 5,375 ms |
| 5 | `GET /backend-api/payments/checkout/openai_llc/{checkout_id}` | 200 | 1,762 ms |
| 6 | `POST /backend-api/payments/checkout/taxes` | 200 | 2,739 ms |

项目本地接口返回 HTTP 201，订单终点状态为 `awaiting_payment`，应付金额为 **1100.00 PHP**。创建后的税费接口负责提交默认账单地址并取得报价。

完整记录包含 6 条上游 HTTP 请求，每条均保存请求与响应正文，记录检查未发现捕获错误。Sentinel 主令牌的五个字段均有值，`c` 与挑战响应一致，设备 ID 与建单请求头一致，Node 返回 `hasT=true`、`hasSo=true` 和 `powReq=true`。

本次项目请求使用 `oai-telemetry=production`、额外 SO 请求头、与浏览器不同的 UA 和设备 ID，仍在该账号与当时网络条件下获得成功响应。这一实测更新了此前仅依据静态代码提出的待验证点，具体差异对其他请求的影响仍需控制变量验证。

诊断进程读取了当时配置的代理，并按 PH 地区参数调用项目。HAR 保存的是应用层 HTTP 数据。代理实际出口 IP 的地理归属、浏览器与项目是否使用同一出口，以及完整 TLS 报文，需要其他网络记录佐证。

原始材料包括完整 HAR、项目接口输入与结果、各传输记录，以及 Node 桥的输入和完整输出。它们存放在本机 `runtime/local/diagnostics/checkout-20261113-174104` 目录，随项目 Git 忽略规则管理。[项目实测报告](/C:/Users/hdd99/Desktop/myshop/gpt-subscribe-studio/runtime/local/diagnostics/checkout-20261113-174104/report.md)、[项目实测 HAR](/C:/Users/hdd99/Desktop/myshop/gpt-subscribe-studio/runtime/local/diagnostics/checkout-20261113-174104/checkout.private.har)

## 客户端拦截与其他 HTTP 错误

三份浏览器记录均包含客户端拦截的请求。最早失败样本与 `download1` 的状态 0 记录全部带有 `net::ERR_BLOCKED_BY_CLIENT`。

| 样本 | 记录时长约值 | 状态 0 记录数 | 涉及路径 |
|---|---:|---:|---|
| `download` | 190 秒 | 198 | `/ces/v1/rgstr` 195 次，`/ces/statsc/flush` 3 次 |
| `download1` | 41 秒 | 38 | `/ces/v1/rgstr` 37 次，`/ces/statsc/flush` 1 次 |
| `download2` | 129 秒 | 88 | `/ces/v1/rgstr` 83 次，`/ces/statsc/flush` 2 次，`/unauth-mweb/events/business` 2 次，`/backend-api/bazaar/obi/sync-token` 1 次 |

两个浏览器成功样本同样存在上报请求被客户端拦截的情况，三个文件的记录时长和初始化范围也不同。当前数据无法将拦截数量单独对应到建单失败。

`download2` 还包含两条需要按目标服务定位的错误记录。

| 时间 | 目标与路径 | 状态 | 与建单的时间关系 |
|---|---|---:|---|
| 17:50:46 | `challenges.cloudflare.com` 下的挑战资源 | 401 | 位于创建账单之前 |
| 17:52:16 | `api.stripe.com/v1/consumers/sessions/start_verification` | 429 | 位于创建账单成功之后，响应提示尝试过多 |

`download2` 的 ChatGPT 创建接口在 17:52:03.402 发起请求并返回 HTTP 200。最早失败的 `download` 则由 ChatGPT 账号接口明确返回 `token_revoked`。定位认证失败时需要同时保留目标域名、请求路径、错误码和请求顺序。

## 与此前项目失败日志的关联

此前本地订单记录显示，11 月 13 日 16:03:30 与 16:19:31 两次 Plus、PH 建单进入 `rejected`，错误为“Session 无效或已过期”。日志还记录了 `access_token_changed=False`，以及一次“Session 恢复仍返回已被拒绝的访问令牌”。

当前代码在创建结账遇到 HTTP 401 时，直接转换为统一的 Session 失效提示。订阅查询则已经具有调用认证恢复并重试的处理。创建失败日志缺少具体上游请求路径和完整错误正文，这两次项目失败与后续浏览器失败样本之间的账号对应关系仍需其他记录确认。[本地日志](/C:/Users/hdd99/Desktop/myshop/gpt-subscribe-studio/runtime/local/logs/server.err.log)、[建单 401 处理](/C:/Users/hdd99/Desktop/myshop/gpt-subscribe-studio/packages/core/src/gss_core/chatgpt.py:967)、[订阅查询恢复逻辑](/C:/Users/hdd99/Desktop/myshop/gpt-subscribe-studio/packages/core/src/gss_core/chatgpt.py:1883)

## 当前结论与后续复核依据

| 判断事项 | 当前记录支持的结论 | 仍需确认的部分 |
|---|---|---|
| Cookie 导入与建单结果 | 同一登录方式存在失败和成功样本，项目也成功使用保存的会话建单 | 导入工具、会话来源和账号状态的独立影响 |
| 账号密码登录与 Cookie 导入 | 两份成功记录为同一账号、同一设备，建单正文一致，认证历史与会话标识发生变化 | 登录过程的完整交互和实际 Cookie 差异 |
| 最早失败的直接表现 | 创建接口返回异常活动，后续账号接口报告令牌撤销，刷新继续返回相同令牌 | 风控规则及导致撤销的内部原因 |
| 缺少 `checkout_ui_mode` | 只出现在最早失败的浏览器样本中 | 与账号、设备、页面入口等变量分开验证后的影响 |
| `oai-telemetry` 差异 | 两种数组形式和项目的 `production` 值均出现在成功样本中 | 不同账号或上下文下的接受条件 |
| Sentinel 数据 | 三个浏览器样本的字段关联完整，项目实测取得了 `t` 和 SO | 此前项目失败请求的实际字段状态 |
| CES 等请求被客户端拦截 | 失败与成功浏览器样本均存在 | 具体拦截来源及其独立影响 |

后续定位同类失败时，最有价值的记录是创建接口的原始状态码和脱敏错误正文、失败前后访问令牌是否改变、账号与设备标识的一致性、上游挑战要求，以及 Sentinel 各字段是否取得。受控对照需要固定账号、设备、浏览器配置、出口与账单地区，再核对单个参数或登录步骤的变化。

目前应保持“异常活动与令牌撤销已观察到，具体触发因素仍待验证”的判断范围。现有样本尚不足以确定某一种登录方式、某个请求头或某一类账号必然导致风控。

## 来源与文件版本

原始 HAR 可能包含认证信息、Cookie、设备标识和其他个人数据。本分析文档使用字段名称、时间、状态码及一致性结果呈现证据，原始字段值保存在本机来源文件中。

| 来源 | 文件大小 | SHA256 |
|---|---:|---|
| [download](/C:/Users/hdd99/Downloads/download) | 14,293,497 字节 | `7f125cfadf430afd58f29d1655093828391761d5f7ca48108a70dbb6642fd04b` |
| [download1](/C:/Users/hdd99/Downloads/download1) | 11,136,786 字节 | `4b67023a58df817fc821e12a39d36c4dc9653af5610012f88adb029350fd8a42` |
| [download2](/C:/Users/hdd99/Downloads/download2) | 27,206,361 字节 | `d742ccb8b1d1b6a2cd725832ce8ae78df2f7ebde027d620763ee610b263c76b0` |
| [项目实测 HAR](/C:/Users/hdd99/Desktop/myshop/gpt-subscribe-studio/runtime/local/diagnostics/checkout-20261113-174104/checkout.private.har) | 2,502,095 字节 | `451025550684d8839361547367a124f29ab30fa693352b46ad782aff7d1c278e` |

这些校验值用于识别本次分析对应的文件版本。后续若同名 HAR 被覆盖，应根据新记录重新核对时间线和比较结果。
