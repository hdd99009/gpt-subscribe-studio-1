# 本机部署

源码版本：`adccdfc993e368f2c09bad7081885c6daff2d320`。

当前采用 Windows 本机独立环境运行：Python 3.11.9（项目要求 >=3.10）、项目内 `.venv`、本机 Node。
HTTP 依赖固定为 `curl_cffi==0.16.1`，支持项目使用的 `chrome150` 等浏览器配置；原上游锁定的 `0.13.0` 不支持这些配置，会在发请求前抛出 `ImpersonateError`。
本机 Docker Desktop 启动报 `dockerInference` 套接字错误，因此容器部署暂未启用。

在项目根目录运行以下 PowerShell 7 命令：

```powershell
& .\start-local.ps1
```

访问 <https://localhost:18443>。登录密码保存在 `runtime/local/ACCESS.txt`。
首次访问浏览器可能提示不信任本地证书；确认地址为本机 localhost 后可继续访问。
本机证书保存在 `runtime/local/certs/localhost.crt`，不会自动更改 Windows 信任证书存储。
证书有效期一年；如需重新生成，先停止服务，移走 `runtime/local/certs` 中的证书和私钥，再重新运行初始化脚本。

停止与自检：

```powershell
& .\.venv\Scripts\python.exe .\deploy\local\verify.py
& .\stop-local.ps1
```

停止时数据、密码和代理设置保留。Windows 重启后需重新运行启动脚本；启动后服务在后台运行，日志位于 `runtime/local/logs/`。
自检验证 HTTPS、登录、前端资源、后端状态和代理配置持久化，不会提交订单或付款。

如需从头安装本机依赖：

```powershell
& "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe" -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r deploy\server\requirements.txt
npm.cmd ci --prefix apps\web --no-audit --no-fund
npm.cmd run build --prefix apps\web
npm.cmd install --prefix runtime\local\node --no-audit --no-fund undici@6
& .\deploy\local\setup.ps1
& .\start-local.ps1
```

如果以后修复 Docker Desktop，也可改用以下容器部署。先停止本机服务，避免端口冲突：

```powershell
& .\deploy\local\setup.ps1
docker compose -f compose.local.yaml up -d --build
docker compose -f compose.local.yaml ps
```

容器部署使用单独的 Caddy 本地证书，位于 `runtime/local/caddy-data/caddy/pki/authorities/local/root.crt`。

后台代理设置支持粘贴代理串。填写你自己的菲律宾代理并保存、测试后，再创建 `Plus / 标准全价 / PH` 订单。
Plus 创建结账前必须在页面填写真实账单姓名、邮箱和地址。创建请求会携带 `billing`，在返回订单之前提交给上游核算税费。
支付页沿用这份地址；修改地址请点击“重新开始”并创建新结账。上游已创建结账但报价暂缺时，仍进入支付页并保留原结账，金额显示待取得报价；可以点击“刷新报价”，取得金额前付款按钮禁用。
税费更新接口只返回成功状态而没有金额时，会重新读取同一个结账的报价。刷新报价不会新建结账、提交卡片或确认支付。
已通过本机启动、停止、重复启动，以及 HTTPS 登录、前端资源、后端健康检查和代理配置持久化验证。
初始代理关闭，未进行真实建单或扣款验证。
程序可能改写代理用户名的地区和会话参数，需确认供应商格式兼容。
实际应付金额以订单报价为准；账单资料应与付款方式相符。

## 123Proxy 菲律宾线路

本机已增加 123Proxy 住宅代理支持：识别账号末尾的 `+ph`，保持国家码小写且位于最后，不追加通用的 `-region-` / `-sid-` 后缀。
本次连通性检查中，供应商提供的 `residential.123proxy.cn:32000` 使用 SOCKS5 可以连接，HTTP 超时。
在代理设置中使用以下格式（替换账号与密码，不能把四段式代理直接加在协议后面）：

```text
socks5h://账号+ph:代理密码@residential.123proxy.cn:32000
```

如果密码包含 `@`、`:`、`#` 等 URL 特殊字符，需要 URL 编码；可以在供应商控制台选择 URL 输出格式再复制。
当前配置已修正为 SOCKS5 / PH，并保留原密码；修改前的配置备份在 `runtime/local/data/` 内。
已通过后台“测试连接”，出口查询结果为菲律宾 `PH`；项目使用的 curl_cffi 传输也已验证可连接。
连通性检测使用供应商文档中的 IPv4 查询地址 `api.ipify.org`，避免 `api64.ipify.org` 在部分出口上超时。

只有 `+ph` 时未启用粘性会话，供应商可能为连续请求分配不同出口。
需要保持出口时，在供应商提取页开启 SESSION，复制其生成的完整路由用户名，例如 `账号-sess_12位字母数字_15+ph`。
项目保留该 SESSION，不在每次建单时自动生成新值；网络错误重试时仍可能轮换 SESSION。
123Proxy 官方说明 SESSION ID 为 12 位字母数字、时长 1–120 分钟，国家或 SESSION 变更可能需要 3–15 分钟同步。
参考：https://123proxy.cn/developers/products/residential-rotating-proxy/

本机配置：

- `compose.local.yaml`：独立 Compose 项目 `gss-local`，仅对本机开放 18443。
- `deploy/local/Dockerfile`：自动构建前端，安装 Python 3.12、Node 20 和运行依赖。
- `.env.local`：运行环境，全价模式，真实适配器启用。
- `runtime/local/config/container-auth.json`：后台密码，容器部署时只读挂载。
- `runtime/local/data/proxy-gateways.json`：后台可写的代理配置。
- `runtime/local/`：数据、密码、代理和本地证书，已被上游 `.gitignore` 和 `.dockerignore` 排除。

查看日志和停止：

```powershell
docker compose -f compose.local.yaml logs --tail 100
docker compose -f compose.local.yaml down
```

停止容器不会删除本机数据；Docker Desktop 运行时容器配置为自动重启。
初始化脚本可重复运行，不覆盖已有密码或代理。
