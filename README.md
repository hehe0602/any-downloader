# any-downloader · 高速多并发下载器

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)](#)

一个**通用、开箱即用**的多任务下载器：粘贴链接就能下，内置浏览器会话免去手工抓 Cookie，
已接入夸克 / UC / 百度 / 123 / 迅雷 / 蓝奏六个网盘的分享解析；底层专为突破网盘 CDN
**单连接限速** 而设计——Python + asyncio + httpx 后端，pywebview + Vue3 毛玻璃前端。

> **要改代码？** 先看 [docs/开发文档.md](docs/开发文档.md)：线程模型、模块职责、接口契约、数据格式、扩展步骤、调试与测试方法都在那里。本文只讲怎么用、怎么排查。
>
> **要接手继续开发？** 直接看 [docs/交接说明.md](docs/交接说明.md)：当前状态、已实锤的结论、
> 未验证的风险点、代码地图与下一步建议。

## 为什么 IDM 32 线程还是慢

- 服务器按 **TCP 连接** 限速（约 40~50 KB/s），IDM 频繁断连重连，大量握手与请求头开销；
- 本下载器：**64 个常驻 Worker** 抢占式从动态分块队列取任务，连接 **Keep-Alive 复用**，没有长木板效应。

## 安装与运行

需要 Python 3.10+（已在 3.13 验证）。Windows 下用 PowerShell：

```powershell
# 1. 克隆仓库
git clone https://github.com/hehe0602/any-downloader.git
cd any-downloader

# 2. 安装依赖并启动
py -3.13 -m pip install -r requirements.txt
py -3.13 main.py
```

也可以直接双击项目目录下的 **`启动下载器.bat`**：自动挑选可用 Python、首次运行自动安装依赖（清华镜像）、出错时暂停显示报错。

命令行模式（可用于脚本/无界面）：

```powershell
py -3.13 main.py --cli "https://example.com/file.zip" -o "D:\file.zip" -t 64 -c 4
# 可选：--referer "https://pan.quark.cn/" --cookie "..." --http2
# 想用并发自适应就加 --adaptive（默认关闭：CLI 里 -t 是显式指定，应当立即生效）；
# CLI 不读也不写 GUI 的任务索引 tasks_state.json，两边同时跑不会互相覆盖。
```

## 夸克网盘使用要点

输入框同时接受两种链接：**签名直链**（沿用下面的老流程）和**分享链接**
（`https://pan.quark.cn/s/…`，见下一节「分享链接一键解析」）。

1. 直链方式：在浏览器中让网盘开始下载，从下载请求里复制 **直链 URL**（带签名的 CDN 地址）；
2. **内置会话凭据（推荐，完全不用 F12）**：新建下载 → 高级选项 → 「打开网盘登录」扫码一次，
   之后粘贴链接即可，Cookie / UA / Referer 由程序自动补齐（详见「免 cURL」一节）；
3. **cURL 一键导入（兜底）**：F12 → Network → 右键那条 `dl-*.drive.quark.cn` 请求 → Copy →
   **Copy as cURL**，粘贴到「高级选项 → 浏览器 cURL 一键导入」框里，点「解析并填充」，
   URL / UA / Referer / Cookie / 其余请求头一次性补齐；
4. **Referer 自动填充**：识别到 `quark.cn` 域名会自动带上 `https://pan.quark.cn/`，`uc.cn` 自动带 `https://drive.uc.cn/`，也可在「高级选项」手动覆盖；
5. 请求默认携带完整浏览器指纹（UA、Accept-Language、Sec-Ch-Ua、Sec-Fetch-* 等），减少被反爬识别；
   一旦用内置会话的真实 Edge UA，会自动关闭这套 Chrome 客户端提示，避免指纹自相矛盾；
6. **412 / 403 排查**：点「诊断直链」——它会用「带 Cookie」和「不带 Cookie」各打一次 `Range: bytes=0-1`，
   直接告诉你到底缺什么。典型结论：带 Cookie 通过 / 不带被拒 = 就是缺登录态；
7. **429 限流**：自动指数退避重试并临时下调并发，阶梯恢复；若持续触发请手动降低并发数；
8. **默认 HTTP/1.1 + Keep-Alive**：64 个独立连接才能各自吃满 40KB/s。
   HTTP/2 会把请求多路复用到少数连接上，反而共享限速，仅在确认对方按“请求”而非“连接”限速时开启。
9. **并发自适应（默认开）**：不再一上来就把几百条连接全开，而是从 8 路起步、按实测速度
   逐档上探，发现“再加也不快”就停在那一档（任务卡片上会写明「并发自适应：定在 N 路」）。
   有些 CDN 会把“同一个直链太多并发”当成风控，这时候低并发反而更快；
   吃到 429 时不会一次就认怂：会在原档再观察一轮，仍然被限速才收敛；
10. **并发数可热改**：任务卡片上直接填数字点「改并发」，下载中立即生效（在途分块跑完为止），
    不用停下来重建任务；
11. **优先 IPv4**：高级选项里的开关只解析 A 记录。同一条直链的 IPv6 与 IPv4 走的可能是
    完全不同的 CDN 节点，实测遇到过 IPv6 路径只有 3MB/s、换 IPv4 就正常的情况
    （SNI 与证书校验仍按域名进行，不影响安全性）；
12. **429 的 Retry-After 会被睡满**：全局只短暂停一下（上限 6 秒），剩下的等待由命中限速的
    那条连接自己补足，既不会因为几百条连接一起停摆而卡死，也不会 6 秒就打回去招来更重的限速。

## 分享链接一键解析（多网盘）

不用再先去浏览器里取直链：直接把分享链接粘进下载框，程序自动识别并解析。

- **已接入网盘**：夸克、UC 网盘（同构流程）、百度网盘、123 云盘、迅雷云盘、蓝奏云；
  输入框对签名直链原样放行；识别为对应分享链接才走解析；
- **未登录自动弹登录窗**：粘贴分享链接后检测到未登录，程序**直接弹出内置登录窗**，
  不用再先进「高级选项」手动点「打开网盘登录」；扫码完成后点面板上的「我已登录，重新解析」即可；
- **提取码自动识别**：链接带 `?pwd=xxxx` 直接用；没带且分享有提取码时，面板弹出提取码输入框，
  输错会提示重输；
- **文件夹可直接进入**：分享列表里的文件夹不再是灰色不可点——单击文件夹（或点「进入文件夹」）
  即可查看里面的文件，顶部**面包屑**显示当前路径，点任意一级返回；已浏览过的目录有缓存，
  来回切换不重复请求；
- **多选批量下载**：勾选框可同时选中多个文件（可跨多个文件夹累积），面板实时显示
  已选数量与总体积，也支持「全选当前目录」；点「开始下载」后批量签发直链，
  每个文件创建一个下载任务（仅勾选 1 个文件时，文件名输入框才会自动回填）；
- **下载目录自动记住**：上次选的保存目录、并发数、分块大小会写进 `app_settings.json`，
  下次启动自动回填，不用反复选目录；首次使用兜底到项目下 `下载/` 文件夹；
- **按网盘差异说明**：
  - 夸克 / UC：完整流程（列表 + 目录 + 直链签发，**不转存、不往你网盘里写东西**）；
  - 123 云盘：匿名可列目录、可签发直链；
  - 蓝奏云：免登录，抓页面签直链；
  - 百度网盘：**登录后可列目录 + 签发直链**（不转存）；未登录只能匿名列目录，
    签发会被百度回 `9019 need verify`，程序会自动引导登录；
  - 迅雷云盘：匿名可列目录，直链需登录（网页端匿名无直链入口）；
- **后续网盘按同一插件接口接入**：解析流程与前端面板不绑定具体网盘，新增网盘时
  只需在后端插件包补一个模块（开发文档 10.2 节有步骤）。

### 百度网盘（2026-11 接入直链）

- **直接粘分享链**：`https://pan.baidu.com/s/1xxxx?pwd=abcd`；提取码没带会弹输入框，
  输错会提示重输。列表走 `/share/wxlist`（老的 `/share/list` 对真实分享已回 `errno=2`，
  所以之前「能识别但列不出来」），文件夹可以进入，多选可批量下载。
- **直链签发必须登录**：百度对匿名请求一律回 `9019 need verify`。在
  「高级选项 → 打开网盘登录」扫码一次，再点「我已登录，重新解析」即可。签发链路是
  `api/gettemplatevariable`（bdstoken）→ `share/tplconfig`（sign/timestamp）→
  `api/sharedownload`（带 `sekey`）→ `dlink`。
- **UA 固定为 `pan.baidu.com`**：下载任务会自动设置，别在高级选项里改。
  这不是反爬偏好，是百度 CDN 的硬要求——浏览器 UA 下超过 50MB 会直接 403。
- **不转存**：不会往你的网盘里写任何东西；直链约 8 小时过期，续传时程序会自动重新签发。
- **速度预期**：百度非 SVIP 是**账号级限速**（不是单连接限速），本项目的 64 并发
  对百度**不会叠加提速**；SVIP 账号按账号自身速度。遇到 `errno=-20` 是风控验证码，
  在登录窗打开 `pan.baidu.com` 手动过一次验证或等一段时间再试。

## 免 cURL：内置浏览器会话（WebView2）

网盘的 CDN 直链在服务端校验登录态，纯 HTTP 请求即使把 UA / Referer / Sec-Fetch-* / HTTP2
组合试遍也只会拿到 412 —— 这一点在本机实测过（7 种头组合 × 多种 UA / Referer / Origin，全部 412）。
缺的确实是浏览器会话 Cookie。

因此下载器自带一个会话窗口：

```
高级选项 → 打开网盘登录 → 扫码 → 之后直接粘直链下载
```

- **同引擎**：Windows 上 pywebview 跑的就是 WebView2（Edge 内核），和你日常用的 Edge 同源；
- **登录一次长期有效**：WebView2 的配置目录固定在项目下 `会话数据/`（`main.py` 用
  `private_mode=False` + `storage_path` 打开），关掉程序再开依然在登录态；
- **按目标直链精确取 Cookie**：取凭据时把 `GetCookiesAsync` 的目标 URI 换成要下载的那条直链，
  再按 RFC 6265 的 domain / path / secure 规则过滤，只有真正会发给该 CDN 的 Cookie 才会被带上；
- **绝不读你的浏览器 Cookie 库**：不需要解密、不需要管理员权限。新版 Edge（本机 155）的
  Cookie 是 App-Bound 加密的 `v20`，本地根本解不开；而且 `Network/Cookies` 在 Edge 运行时
  连复制都会被拒（同目录 `History` 却可以读），所以「解密 Cookie 库」这条路本身就不成立；
- **长下载会自动续命**：下载途中若遇到 403/412（会话 Cookie 轮换），引擎会用同一会话重新取一次
  Cookie 再重试（30 秒内最多一次），不会让跑了几小时的任务因会话过期而报废；
- **兜底仍在**：`高级选项 → cURL 一键导入` 与手工 Cookie / UA / Referer 完全保留，
  且「读取会话凭据」只是把值填进表单，你随时能看到并改。

适用站点（`app/sites.py`）：夸克网盘、UC 网盘、百度网盘、123 云盘、迅雷云盘、蓝奏云。
六家均已接入「分享链接 → 文件列表 / 目录下钻」解析；其中夸克 / UC / 百度 / 123 / 蓝奏
可直接签发直链下载（百度需登录一次，且下载 UA 固定 `pan.baidu.com`）；迅雷的直链签发
仍需登录后再对齐（会提示先登录，不会静默失败）。

### 还是 412？按这三步走

1. **先重启下载器**（会话/注入逻辑在这次改动里）——旧进程里跑的还是老代码；
2. **确认登录过的是「应用内的登录窗」**（不是 Edge）。Edge 里登录与下载器无关：
   `新建下载 → 高级选项 → 打开网盘登录`，扫完码即可，**只需一次**；
   登录态存在 WebView2 配置目录，重启后程序会自动用隐藏窗把 Cookie 读出来，不用再开窗；
3. 粘直链 → 开始。若仍 412，点任务卡片上的 **「诊断」**：它会用任务自己的配置把
   **Cookie × Referer 四种组合**各打一次 `Range: bytes=0-1`，并显示每一组的真实状态码：

   ```
   Cookie+Referer · 412   ← 现在下载器走的就是这条
   仅 Cookie      · 206   ← 说明多带的 Referer 才是 412 的原因
   仅 Referer     · 412
   裸请求          · 412
   ```

   出现可用组合时，卡片上会多一个 **「按此修正并重试」**，点它即可（例如自动去掉 Referer）。
   这不是猜：Edge 地址栏直接下载时**不带 Referer**，而夸克 CDN 对 Referer 很敏感。

> 诊断面板只出现两组（"仅 Referer / 裸请求"）说明当时请求里**没有 Cookie**——
> 也就是没读到会话。点「重试」会重新读一次；仍不行就去登录窗扫码。

> 任务是在登录前建的也没关系——「重试」现在会重新读一次会话凭据再发请求，
> 不需要删掉重建。

### 最小化到系统托盘

点右上角 **×** 不再退出，而是收进 Windows 系统托盘，**下载在后台继续**：

- 托盘图标：紫色圆角方块 + 白色闪电（`assets/tray.ico`，和界面 logo 同造型，纯脚本生成）；
- 悬停提示：`高速下载器 · 2 个任务下载中 · 12.3 MB/s`（每 3 秒刷新一次）；
- 双击图标 → 恢复窗口；右键 → 「显示主界面」/「退出下载器」；
- 首次收起会弹一次气泡提示，避免"点了 × 程序不见了"的困惑；
- 托盘不可用（非 Windows / 后端异常）时自动退化为原来的"关闭即退出"，不会卡住；
- **退出**：托盘右键 → 退出下载器。退出时下载任务转 `paused` 并落检查点，下次「继续」续传。

顺带加了**单实例保护**：托盘常驻时再点一次 `启动下载器.bat` 不会开出第二个实例
（提示去托盘恢复），避免两个进程抢同一个 WebView2 配置目录。

![托盘图标](assets/tray_preview.png)

### 退出行为

- 点 **×**：藏到托盘（见上节），进程与下载都继续；
- 托盘「退出下载器」：销毁所有窗口 → 保存检查点 → 进程退出；
- 关闭窗口时会**一并收走**可见登录窗与隐藏读取窗，不会出现"窗关了进程还挂着"；
- 退出后不留 WebView2 孤儿进程（实测进程数 6 → 6 回到基线）；
- 唯一边角情况：若恰好在"412 自动重取会话 Cookie"的瞬间退出，后台线程最多再跑几秒才结束，
  窗口已消失、不影响下次启动。

### 深度诊断：区分「凭据不对」还是「被拦指纹」

Cookie 已经带上还是 412？点任务卡片上的 **「深度诊断」**，它会用**任务自己的配置**跑两组对照：

**A. 请求头组合（4 组）** —— 判断是 Cookie 还是 Referer 的问题：

```
Cookie+Referer · 412
仅 Cookie      · 412
仅 Referer     · 412
裸请求          · 412
```

**B. 传输栈（5 种）** —— 判断是不是服务端在按 TLS/JA3 指纹拦截：

| 传输栈 | 说明 |
|---|---|
| `httpx` | Python + OpenSSL（引擎默认） |
| `httpx+ChromeTLS` | 把 TLS1.2 密码套件顺序换成 Chrome 的顺序 |
| `curl(Schannel)` | Windows 自带 `curl.exe`，走系统原生 TLS |
| `curl_cffi(Chrome)` | libcurl-impersonate，复现 Chrome 的完整 TLS/HTTP2 指纹 |
| `浏览器栈` | WebView2 页面上下文里直接 `fetch`（Chromium 原生网络栈 + 你的登录态） |

结论会自动翻译成人话，例如：

> 浏览器栈能下、httpx、curl_cffi 不行 → 服务端在拦非浏览器 TLS 指纹，问题在传输层，不是 Cookie/Referer。

**自动兜底**：下载时如果探测到 412/403，引擎会**自动**把传输栈切成 `curl_cffi`（Chrome 指纹）再试一次，
任务卡片上会提示「已自动切换为 Chrome TLS 指纹后端」。所以多数情况下你不需要点任何按钮。
也可以手动点 **「改用 Chrome 指纹重试」**。

> `curl_cffi` 已列入 `requirements.txt`，`启动下载器.bat` 首次运行会自动装。

### 环境前提

- Windows + WebView2 运行时（Win11 自带；Win10 若缺，装 Edge 即会装上）；
- 支持 pywebview ≥ 5.1；会话功能在 `--cli` 模式下不可用（CLI 请继续用 `--cookie`）。

### cURL 导入说明

- 兼容 Chrome / Edge / Firefox 的 Copy as cURL：单/双引号、`\` 与 `^` 行续行、`-H / --header`、`-b / --cookie`、`-A / --user-agent`、`-e / --referer`、`-o` 文件名、`--http2`、`-k` 等；
- 自动丢弃不可复用项：`:authority` 等 HTTP/2 伪头、`Host`、`Content-Length`、`Accept-Encoding`、`Connection`；
- 代理、POST 请求体、超时/限速等与下载无关的选项会在 Toast 里给出「已忽略」提示，不阻断导入；
- 丢弃 `:authority` 等伪头不影响下载：`Host` 由 URL 自动推导。

### 状态码排查表

| 状态码 | 含义 | 处理方式 |
|---|---|---|
| 206 | 服务器接受 Range，分块下载正常 | — |
| 200 | 服务器忽略 Range 请求 | 不支持多线程，自动退化为单连接整文件下载 |
| 403 | 直链失效或缺少登录态 | 点「诊断直链」；或重新获取直链 |
| 412 | 防盗链 / 反爬校验失败（本机实测=缺会话 Cookie，或 TLS 指纹被拦） | 先登录内置会话；仍 412 就点「深度诊断」看是 Cookie 还是传输栈 |
| 416 | Range 越界（预检大小不符或检查点来自旧文件） | 移除任务后重新开始；分块边界已按总长夹紧到 `size-1`，正常请求不会越界 |
| 429 | 请求过于频繁 | 自动指数退避、临时降并发后阶梯恢复；持续触发请手动调低并发数 |

## 架构

```
main.py                 入口：GUI / CLI（会话数据存 会话数据/；GUI 单实例互斥体）
assets/tray.ico         托盘图标（脚本生成，不依赖 Pillow 等绘图库）
app/
  config.py             下载配置（并发、分块、UA/Referer/Cookie/HTTP2/指纹开关）
  settings.py           本地偏好持久化（保存目录 / 并发 / 分块，app_settings.json 原子写）
  sites.py              网盘站点表：域名归属、登录入口、默认 Referer、登录态标志 Cookie
  parsers/              分享链接解析插件包：base（异常/数据结构/基类）、registry（注册表/分类）、
                        quark（夸克）、uc（UC）、baidu（百度）、pan123（123 云盘）、
                        xunlei（迅雷云盘）、lanzou（蓝奏云）
  session.py            内置 WebView2 会话：登录窗 + 隐藏读取窗 + 按目标 URL 精确取 Cookie
  transports.py         传输后端适配：httpx（默认）/ curl_cffi（Chrome TLS 指纹）
  transport_probe.py    传输栈探测：httpx、httpx+ChromeTLS、curl.exe(Schannel)、curl_cffi
  tray.py               Windows 系统托盘（NotifyIcon 经 pythonnet 调用，零新增依赖）
  curl_parser.py        浏览器 Copy as cURL 解析器（URL/UA/Referer/Cookie/额外头）
  checkpoint.py         检查点原子写入（.tmp -> os.replace）；按最终文件名、身份=文件名+大小+ETag
  taskstore.py          任务索引持久化（tasks_state.json）：重启后任务列表还在、点继续续传
  netbackend.py         首选 IPv4：只解析 A 记录的 httpcore 网络后端（SNI/证书仍按域名）
  downloader.py         核心引擎
    ├─ build_headers    统一构造请求头（探测与分块共用；可关浏览器指纹）
    ├─ DynamicLimiter   403/429 自动降并发（×0.75，全局暂停上限 6s），之后阶梯恢复；并发可热改
    ├─ 并发自适应        8 路起步按实测吞吐逐档上探，到拐点就停（默认开）
    ├─ 动态分块队列      2~5MB 块，Worker 池按上限建好、实际并行由限流器控制
    ├─ 预分配文件        truncate，每个 Worker 独立文件句柄按 offset 直写
    └─ 监控协程          速度(近 60 秒滑动平均)、ETA、进度、周期落盘 + 落盘水位线
  manager.py            独占事件循环线程；任务生命周期；事件增量推送；分享链接解析；
                        直链诊断（Cookie × Referer 四组合）；信号与关机事件
  api.py                pywebview js_api：设置/会话/解析/诊断 + 同步解包 Manager 的 Future
web/                    Vue3 + 毛玻璃 UI（vendor 内自带 Vue，可离线）
tests/test_resume_e2e.py 端到端回归：本地 Range 服务器验证续传/换直链/断电水位线/自适应/任务索引
tests/test_quark_fix.py  夸克直链回归：PC 客户端 UA / __puus 刷新 / 「不再转存」的守护断言
tests/test_baidu.py      百度直链回归：wxlist 列目录 / 提取码 / sharedownload 签名 / dlink UA / 112·118 重试
docs/开发文档.md         开发文档：线程模型 / 接口契约 / 数据格式 / 扩展步骤 / 调试方法
docs/交接说明.md         交接说明：现状 / 已实锤结论 / 未验证风险 / 代码地图 / 下一步建议
```

### 开发文档

代码级的说明集中在 [docs/开发文档.md](docs/开发文档.md)，按需查：
**接手继续开发**先看 [docs/交接说明.md](docs/交接说明.md)（现状、风险点、代码地图、下一步）。

| 章节 | 内容 |
|---|---|
| 2 总体架构 | 分层图、**线程模型**（UI 线程 / `download-loop` / 会话读取线程 / 托盘线程）与跨线程硬约束 |
| 3 核心引擎 | `DownloadConfig` 字段表、**状态机**、分块调度、重试退避矩阵、传输栈兜底、测速口径、`snapshot()` 字段表 |
| 4 下载管理器 | `_submit`/Future 约定、任务生命周期 API、**凭据注入链路**、诊断实现、**事件协议**（`task_update` / `task_removed` / `toast`）、优雅退出 |
| 5 前端桥 | `JsApi` 方法表、前端约定（无构建、`pywebviewready`、`_done` 增量集合）、如何加一个卡片按钮 |
| 6 内置会话 | 三类窗口、按任意 URL 取 Cookie 的技巧、RFC 6265 过滤、登录态判定、`browser_probe` 原理、**接入新网盘** |
| 7 传输层 | 引擎用到的 httpx 子集、4 种探测栈、加新后端的步骤 |
| 8 托盘与单实例 | NotifyIcon 的线程模型、关窗拦截、互斥体 |
| 9 数据格式 | `.ckpt.json` schema、脏检查点判定、运行期目录 |
| 10 扩展指南 | 加传输后端 / 加网盘 / 改测速参数 / 加事件 / 加诊断维度 |
| 11 调试与测试 | 不启 GUI 直接跑引擎、最小 Range 服务器、合成时钟测窗口、真实 WebView2 前端调试、验证清单、**常见坑** |

### 断点续传

- 每完成一块（或每 1 秒、暂停/退出时）更新 `文件名.ckpt.json`，原子写入；
- 进度包含在途分块已收到的字节，显示平滑；
- **任务列表会记住**：任务索引写在项目根 `tasks_state.json`，关程序（包括正常关机）后
  重开，任务还在、状态是「已暂停」、进度照旧，点「继续」接着下；
- **换直链也能续**：续传认的是「文件名 + 总大小 + ETag」，不再要求签名直链逐字相同 ——
  网盘直链几小时就换一条，旧逻辑因此每次都判成脏进度、从 0 重下并把已下数据覆盖掉。
  分享任务点「继续」时还会自动重新签发直链（`share_ref`）；
- 判定为脏进度（文件名/大小/ETag 不一致，或输出文件缺失）时：清空已完成块、从头下载，
  不向服务器发送越界 Range 请求；**内存里的旧进度也会一起清掉**——否则那些块会被当成
  “已完成”跳过，最后得到一个大小正确、内容大片为零的假完成文件；
- **数据文件被删/搬走后重启**：进度按 0 显示（不会假装还有进度），已完成的卡片会降回
  「已暂停」并提示重新下载；点「继续」会老老实实重下一遍；
- **旧版的 `download.ckpt.json` 会被认领**：老版本把检查点固定写在
  `<保存目录>\download.ckpt.json`（那时还不知道真实文件名）。升级后第一次续传时，
  只要「本地大小对得上 + 直链能佐证是同一个文件」，就会把它改名成
  `真实文件名.ckpt.json` 并接着下；认不准就不认，也不会误删（见开发文档 9.1）。

### 速度显示（近 1 分钟平均）

- 监控协程每 0.5 秒采样一次「累计收到字节」，速度 = **最近 60 秒的滑动平均**
  （窗口没满时按「自开始以来的平均」算，所以一开始就有读数）；
- 只统计真正从网络收到的字节（含重试），不受分块完成/中断造成的进度跳变影响；
- 暂停、停止、完成、出错时**立即**归零并推送一次事件，界面把非传输状态显示为 `—`，
  不会残留上一次的数字，也不会出现「已暂停还显示 8 MB/s」；
- 继续下载时清空采样窗口重新起算，之前暂停的那段时间不会把速度平均下来
  （否则恢复后要等一分钟数字才「爬」上来）；
- 长时间卡住（没有新数据）时窗口会自动把速度摊回 0，而不是一直挂着高位读数。

### 优雅退出

捕获 `SIGINT`、`SIGTERM`（Windows 上另含 `CTRL_CLOSE / LOGOFF / SHUTDOWN`）：
停止分发 → 在途分块立即中断 → 保存检查点 → 关闭连接并退出。

## 本机验证结果

本地模拟每连接 40KB/s 的服务器：

| 场景 | 结果 |
|---|---|
| 单连接基线 | 38.0 KB/s |
| 64 并发下载 32MB | 13.2s，峰值 7.8 MB/s（约 210 倍），SHA256 一致 |
| 50% 暂停后继续 | 检查点恢复，SHA256 一致 |
| 412 探测 | 直接给出 Cookie/Referer 排查提示，不误入整文件模式 |
| 429 限流 | 退避重试后自动恢复并完成，哈希一致 |
| 416 分支 | 独立判定 Range 越界，给出明确提示并进入错误终态 |
| 脏检查点 | 文件名 / 尺寸 / ETag 不符自动丢弃、清理残留文件后从头下载，哈希一致 |
| cURL 解析 | Chrome/Firefox 两种风格 26 项断言全过，伪头/压缩头正确丢弃；GUI 前端回填往返正常 |
| GUI 真实通道 | `list_tasks / parse_curl / add_download` 经 pywebview 序列化往返成功，任务下载完成且哈希一致，无 Future 序列化错误 |
| 直链鉴权实测 | 同一条夸克直链：裸请求 / 仅 UA / UA+Referer(5 种) / Origin / HTTP2 / HEAD / 无 Range 共 20+ 组合 **全部 412**；同一链接在 Edge 地址栏可直接下载 → 结论：缺的是会话 Cookie，不是头指纹 |
| 会话取 Cookie | 隐藏窗口 + `GetCookiesAsync`：HttpOnly 可读、深路径可读、`dl-*.drive.quark.cn` 跨域取不到本地 Cookie（域过滤生效）、`cookie_header` 拼接正确、非主线程动态建窗可用 |
| 重启复用登录态 | 无任何可见窗口时 `capture()` 自动建隐藏读取窗并读到 Cookie（本地站点断言 `windows_可见=0 隐藏=1`；真实 `pan.quark.cn`：未登录 → 3 个指纹 Cookie、判未登录；已登录 → 20 个 Cookie、`logged_in=true`；查询 URL 改到根域时只剩 11 个 `.quark.cn` 父域 Cookie → 逐条 URL 取并集才不会漏登录态） |
| 412 提示语 | `_auth_hint(412)` 三种配置（有 Cookie / 无 Cookie / 关闭 Referer）均正常输出，不再抛 `NameError` |
| 诊断 → 一键修正 | 本地模拟"带 Referer 就 412"：四组合判定 best=`cookie`，点修正后 4MB 下载完成、SHA256 一致 |
| Cookie 刷新节流 | 30 秒内只重取一次；取到同值不重试；回调抛异常不影响下载流程 |
| 界面冒烟 | `pywebviewready` 后 Vue 正常挂载（不再误报“请通过 main.py 启动应用”）、4 个站点下拉、粘贴直链自动识别为夸克、`diagnose_link` 经真实桥返回结论 |
| 关窗流程 | 关闭主窗后登录窗被自动收走，`webview.start()` 正常返回，进程退出 |
| 退出（三场景） | ① 主窗+登录窗+读取窗：关闭耗时 0.0s，进程 6→6；② 只剩隐藏读取窗：0.0s，6→6；③ 下载进行中关窗：任务转 `paused`、检查点落 8/12 块、6→6 |
| 托盘 | 点 × 后 `Visible=False` 且 `start()` 未返回（进程存活）、托盘图标已建；托盘「恢复」后窗口重新可见；托盘「退出」后 0.0s 返回、进程 6→6 |
| 托盘后台推送 | 窗口隐藏时 `evaluate_js` 正常（`1+1=2`）、`task_update` 推送后前端 `percent=42.5` 生效，恢复后数据仍在 |
| 单实例 | 父进程持有互斥体后，子进程 `_acquire_instance_mutex()` 返回 False（第二个实例被拦） |
| 传输栈探测 | 本地模拟"单看 UA/栈就 412"：4 传输栈状态正确（httpx/ChromeTLS/curl 412，curl_cffi 与 WebView2 206），深度结论正确 |
| curl_cffi 引擎 | `transport=curl_cffi` 跑完 6MB 分块下载，8 并发，SHA256 一致 |
| 自动兜底 | 首个探测强制 412 → 自动切 curl_cffi → 任务 completed，SHA256 一致，卡片提示已切换 |
| 速度显示 | 合成时钟单测：稳定 1MB/s 误差 <0.01%；每 5 秒突发 5MB（瞬时峰值 5MB/s）时显示 0.98~1.07MB/s，抖动 0.08MB/s；卡死 30/60 秒后渐进衰减 0.50→0.00MB/s；窗口跨度封顶 61s |
| 暂停 / 继续 / 完成 | 真下载（12 并发、48MB）：暂停瞬间 `speed=0` 并推送归零事件、暂停期间保持 0；继续后 0.8s 就有 14.96MB/s（暂停前 16.27，未被空档摊薄）；完成后归零、文件 SHA256 一致、检查点清理 |
| 停止 / 界面 | 下载中停止与「已暂停再停止」均立即归零；WebView2 实际渲染：下载中显示 `8.0 MB/s` / `00:06`，暂停·完成·已停止·错误四种状态速度与剩余时间均为 `—`，推送暂停事件后立刻变 `—`，恢复后显示 `3.5 MB/s` / `02:00` |
| 偏好记忆 | `app_settings.json` 写入后重新加载值一致；并发 9999 → 夹到 256、分块 9 → 5、空白目录 → 回落默认 `下载/`；未登录 GUI 外读取会话返回明确提示 |
| 链接分类 | CDN 直链 → `direct` 原样放行；`pan.quark.cn/s/…` / `pan.baidu.com/s/…` → `share` 走对应解析器 |
| 分享异常映射（离线 mock） | 31001 → 引导登录；23018 → 引导登录（文案说明「被当访客 / 票据过期」）；未带提取码 → 提取码输入框；已带错码 → 错码提示；分享已取消 / 已失效 → 不可用提示；mock 全链路文件列表 → 直链签发往返字段正确 |
| 百度直链（离线 mock） | `wxlist` 的 shorturl 含开头 1 / 文件夹 key 用路径 / 子目录 `dir=/路径` / `mispwd-9` 区分缺码与错码 / `sharedownload` 带 sign·timestamp·bdstoken·sekey / dlink UA=`pan.baidu.com` / 112 刷新签名、118 重取 seckey 各重试一次 |
| 目录下钻 / 批量（离线 mock） | 根目录翻页 100+20=120 项（含文件夹）；子目录 `list_dir` 返回 3 个文件；16 个文件批量签发按 15+1 两批完成；批量提交后 16 个任务全部创建；`list_share_dir` 返回正确 |
| 真实启动冒烟 | `py -3.13 main.py` 后台启动存活 13 秒，stdout/stderr 无异常堆栈；27 个前端调用的后端方法逐一核对存在 |

> 夸克 `detail` 列表层级与 `download` 响应字段名来自公开源码比对；`token / detail / download`
> 已在 2026-10-23 用真实分享链接 + 已登录会话跑通（见修复记录），真机解析若字段对不上，
> 只需按开发文档 4.7 调整 `app/parsers/quark.py` 的映射。

## 修复记录

- **2026-11-09（百度网盘分享直链）**：之前百度只能识别、列不出目录，直链也没对齐。
  真机排查后重写 `app/parsers/baidu.py`：
  - **列目录换接口**：老的 `GET /share/list` 对真实分享统一回 `errno=2`「链接出错了」；
    改用 `POST /share/wxlist`（`shorturl` 用**含开头 1** 的完整 token，`root=1|0`，
    `dir` 用绝对路径，`pwd` 直接交给接口校验）。真机验证：`/s/1tPzn…?pwd=eh9y`
    能列出根目录、进入子文件夹、拿到 3 个文件；
  - **修复文件夹下钻**：`ShareFile.key` 对文件夹改用**绝对路径**（原来用 `fs_id`，
    而 wxlist 的 `dir` 要的是路径，点进去必然 `mis_2 不存在此目录`）；文件仍用 `fs_id`；
  - **补齐直链签发**：`app/gettemplatevariable` 取 `bdstoken` + `loginstate`，
    `share/tplconfig` 取 5 分钟有效的 `sign/timestamp`，`api/sharedownload` 带
    `extra={"sekey": wxlist 下发的 seckey}` / `fid_list` / `primaryid` / `uk` 换 `dlink`；
    未登录时百度回 `9019 need verify`，现在抛 `LoginRequiredError` 自动弹登录窗；
  - **提取码**：`mispwd-9` 时按「链接里有没有带 pwd」区分 `passcode_required` 与
    `passcode_wrong`，不再额外调不可靠的 `/share/verify`；
  - **下载 UA**：`ResolvedFile` 新增 `user_agent` 字段，百度直链固定 `pan.baidu.com`
    （浏览器 UA 下 >50MB 会 403）；续传重签时 Manager 也会同步换 UA；
  - **dlink 302 预解析**：签发后先 `Range: 0-1` 跟一次 302 拿到最终 CDN 地址，
    避免 64 路并发各自跳一次；
  - 新增离线回归 `tests/test_baidu.py`（54 项断言，所有请求走 `httpx.MockTransport`）；
    **不含转存逻辑**，不会往用户网盘写东西。

- **2026-11-07（夸克分享大文件签不出直链 / 移除转存）**：粘贴夸克分享链接，文件列表能出，
  点「开始下载」却报「游客可下载大小受限」（`23018`）；而浏览器里用 LinkSwift 同一条分享
  能签出直链。真机 A/B 定位到**两个独立原因**：
  - **API 请求用了浏览器 UA**：同一组 Cookie、同一个 248MB 分享文件，`User-Agent` 用会话里的
    Edge UA → `23018`；换 LinkSwift 那套 `quark-cloud-drive/2.5.x` 的 PC 客户端 UA →
    `code=0` 直接返回直链。网页端 UA 自带访客级大小上限，跟账号是不是会员无关。
    修复：`_client()` 固定用 `API_UA`（PC 客户端 UA），会话里的真实 Edge UA 只留给 CDN 下载；
  - **`__puus` 票据过期**：列表接口只认长期票据 `__pus`，所以目录照列；但 CDN 要求短时效的
    `__puus`，过期后签出的直链下载会 412 / 403（`RequestDeniedByCallback`）。应用久未打开
    网盘时，WebView2 里读到的就是旧票据。修复：新增 `app/cookie_refresh.py`，取到 Cookie /
    签发直链前先打 `auth/pc/flush` 用 `__pus` 换一张新票据（失败就退回原 Cookie，不阻断下载）；
  - **移除「转存」功能**：以前把 `23018` 误当成「大文件必须转存」，会走 `sharepage/save`
    把文件存进你的网盘再签直链。现在确认**不需要转存**（浏览器里的 LinkSwift 也不转存），
    这套代码连同 `task` 轮询、容量提示一起删除。下载器不再往你的网盘里写任何东西，
    也不会再出现「转存失败：网盘空间不足」；
  - 实测（2026-11-07，真实账号 + 公开分享）：248MB 文件 `token → detail → download` 全部
    `code=0`，签出的直链 `Range` 请求 206；同一组 Cookie 用过期 `__puus` 时 412 / 403，
    刷新后恢复。新增离线回归测试 `tests/test_quark_fix.py`（23 项断言）。

  > 内置 WebView2 里的夸克登录 Cookie 到期后会被浏览器清掉（排查这台机器时
  > `__pus/__puus` 已过期被清），这种情况需要在内置登录窗**重新扫码一次**；
  > 登录后票据会自动续期，不会因为放几天没打开网盘就失效。

- **2026-10-23（分享链接报「游客可下载大小受限 / 无登录凭证」）**：直链正常，分享链接一贴就报
  「游客可下载大小受限，请登录夸克网盘后重试：download file size limit[…]」：
  - **结论：不是"大文件限制"，是那次请求以游客身份发出去了。** 报错文案里的 `[guest]` / 「游客」
    就是自白；根因是上面那条「已登录却提示未登录」里的取 Cookie bug —— Cookie 没读出来，
    分享签发就走成了匿名请求，于是被套上游客限制。取 Cookie 修好后，同一条大文件分享能正常签发
    （**12GB 的直链也能签**，LinkSwift 走同一套网页端接口可印证：直链大小本身从来不是限制）；
  - 复现/证伪方法（开发文档 11.6 有可复制的脚本）：用会话 Cookie 打
    `pan.quark.cn/account/info` 应返回昵称、`drive-pc.quark.cn/1/clouddrive/member` 应 `code=0`；
    返回 `31001 require login [guest]` 就说明没带上会话；
  - 顺手把提示改对：`23018` 从「请登录」改成「请求未带会话 / 站点限制」，
    分享面板会常驻显示失败原因（这条 2026-11-07 又改回 `LoginRequiredError`：
    现在把 `23018` 当作「没被认成已登录客户端」的证据，自动弹登录窗）；
  - 当时还加过一条**转存兜底**（`23018` → `sharepage/save` → 轮询 `task` → 用新 fid 重签），
    后来确认 23018 的真实原因是「浏览器 UA + 票据过期」，不是「必须转存」；
    该兜底已于 2026-11-07 整体删除（见上面那条）。

- **2026-10-23（已登录却提示未登录）**：明明扫码登录过，仍被要求「打开网盘登录 / 读取会话凭据」：
  - 根因一：`_read_cookies` 改错了对象。pywebview 6 里 `BrowserView.instances[uid]` 拿到的是
    **BrowserForm**（它的 `url` 建窗后再没人读），`GetCookiesAsync` 真正用的是
    **EdgeChrome**（`BrowserForm.browser`）上的 `url` —— 改外层等于没改，读到的永远是窗口
    当前页面的 Cookie；站点一旦被风控页/运营商页劫持，就一个登录 Cookie 也读不到；
  - 根因二：登录态被当成**硬闸门**。`logged_in` 只是「Cookie 名里有没有标志 Cookie」的启发式
    判断，读超时、父域 Cookie 缺失、站点改名都会误判成未登录，而 `resolve_link / list_share_dir /
    add_download` 又直接据此拦人，于是「已登录」被挡在门外；
  - `session.py`：两层后端都改查询 URL；一次读**多个候选 URL 取并集**（目标直链 / 站点首页 /
    根域），去重后再按目标 URL 过滤出真正要发的 Cookie；登录态改成看「未过滤」的标志 Cookie 名；
    读取超时 8s→12s；隐藏读取窗被重定向到站外时自动拉回站点首页；`state()` 少读一轮（原来同一个
    URL 读两次）；
  - `manager.py`：新增 `_needs_login()`，只有**一个 Cookie 都拿不到**才要求登录；有 Cookie 就先按
    现有会话干活，由服务端（31001 / 游客超限）做最终裁决；
  - `api.py`：提交预检与 `capture_credentials` 同样改成「没 Cookie 才弹登录窗」；
  - `sites.py`：夸克登录标志只保留 `__pus/__puus`，去掉 `__kp`（风控指纹 Cookie，未登录也会下发，
    会误判成已登录）；
  - 顺手修掉同一模块的另一个坑：`clear(site)` 原来调 `window.clear_cookies()`，底层是
    `DeleteAllCookies()`，**清一个网盘会把所有网盘的登录态一起清掉**（本次测试就被它清掉了
    夸克登录）；改成 CookieManager 按域名逐条 `DeleteCookie`，UI 线程里执行，调用方等信号量，
    拿不到后端就一条都不删（`clear_session` 返回值多了 `deleted`）；
  - 前端：登录引导面板打开期间轮询登录态，扫码完成后**自动重解析**，不用再手点；「开始下载」遇到
    `need_login` 时先复查会话再决定，避免已登录用户卡在引导面板；
  - 真机会话实测：用项目内已登录的 WebView2 配置跑 `session.capture()` → `logged_in=true`、
    读到 20 个站点 Cookie，CDN 直链过滤后仍带 11 个 `.quark.cn` 父域 Cookie。

- **2026-10-23（文件夹下钻 / 多选批量）**：解决「文件在文件夹里看不到、也没法选」：
  - 解析器基类新增 `list_dir(result, dir_fid, ctx)` 接口：按文件夹 fid 列目录，
    夸克 detail 自动翻页（每页 100，最多 10 页 = 1000 个，超出给提示），
    根目录与子目录共用同一套 `_fetch_dir` 逻辑；
  - Manager 新增 `list_share_dir()`；分享提交分支重构为批量：一次签发所有勾选文件
    （夸克每批 15 个），每个文件独立创建下载任务，返回 `{created:[快照], count}`；
  - 配置构造抽成 `_cfg_from_opts()` / `_start_cfg_task()`，直链与分享共用，
    显式透传 `auto_credentials`，会话 Cookie / UA / Referer 与刷新回调照常带入；
  - 前端新增面包屑导航（点级返回、目录缓存）、文件夹点击进入、复选框多选
    （跨目录累积）、全选当前目录、已选数量与总体积实时统计；空文件夹给出提示；
  - 文件夹不再灰色禁用；仅勾选 1 个文件时才回填文件名框，多选时清空避免混淆。

- **2026-10-23（分享解析 / 目录记忆 / 自动登录）**：输入框直链、分享链接双通道，未登录自动开窗：
  - 新增 `app/settings.py`：保存目录、并发数、分块大小持久化到项目根 `app_settings.json`
    （先写 `.tmp` 再 `os.replace` 原子替换，白名单字段 + 边界夹取：并发 1..256、分块 2..5MB；
    Cookie / 登录态一律不落盘）；启动时自动回填，浏览目录与提交成功后即时落盘；
  - 新增 `app/parsers/` 插件包（参考 LinkSwift 每网盘一模块）：`base.py` 定义五类异常与
    `ShareFile / ShareResult / ParseContext / ResolvedFile` 抽象契约，`registry.py` 负责
    「直链 / 已接入分享 / 未接入分享」分类，`quark.py` 实现夸克三步流程（`sharepage/token`
    换 stoken → `sharepage/detail` 列文件 → `file/download` 签发 CDN 直链，一批 15 个）；
  - `manager.py` 新增 `resolve_link()` 与任务提交的分享分支：复查登录态、重建上下文、
    签发直链后回落到原直链流程；会话 Cookie / 真实 UA / Referer 原样带进下载配置，
    Cookie 刷新回调绑定回分享域名；目录为空兜底到项目 `下载/`；
  - `api.py` 新增 `get_settings / save_settings / get_default_dir / resolve_link`；
    提交前先按站点查登录态，未登录**自动弹出内置登录窗**（不再要求先进「高级选项」），
    异常按类型翻译成 `need_login / passcode_required / passcode_wrong / error` 面板载荷；
  - 前端新增分享解析面板：未登录引导（可重开登录窗 / 重新解析）、提取码输入与错码提示、
    文件单选列表、不可用提示；粘贴链接 500ms 防抖自动解析；表单保留最近一次目录与参数；
  - 未接入的网盘分享链接（当时是百度等；百度已于 2026-11-09 接入）明确提示
    「接口已预留、暂未适配」，不再被当成直链误下。

- **2026-10-23（速度显示）**：速度数字跳得厉害（且暂停后残留旧值）：
  - `DownloadTask._monitor` 由「1 秒瞬时值 + EMA」改为**采样窗口 + 近 60 秒滑动平均**
    （`_roll_speed`：每 0.5s 采一次，只保留窗口内的采样点，保留一个越界锚点保证跨度 ≥60s）；
  - 去掉 EMA 自适应系数，窗口不满时按「自开始以来的平均」出数，一上来就有读数，
    跑满一分钟后自然变成 60 秒平均；
  - 新增 `_freeze_speed()`：暂停/停止请求的**那一刻**就归零并推事件，Worker 收尾期间不再残留旧数字；
  - `run()` 的 `finally` 统一归零（暂停/停止/出错/完成），并且在 `_done_event.set()` **之前**，
    保证 `pause_task/cancel_task` 返回的快照也是 0；
  - `reset_for_restart()` 清空采样窗口，暂停那段空档不会被算进平均；
  - 前端新增 `speedText/etaText`，非传输状态显示 `—`，标签改为「速度（近 1 分钟）」；
  - 实测 66 秒真机下载（10 并发、服务器突发起伏）：显示值 1.54~1.57 MB/s（抖动 0.03），
    同期每 1 秒原始增量抖动 1.56 MB/s —— 抖动被压到约 2%。

- **2026-10-23（TLS 指纹）**：Cookie 已注入却仍 412 时，定位到传输层：
  - 新增 `app/transport_probe.py`：同一请求头下对比 4 种非浏览器传输栈
    （httpx / httpx+ChromeTLS / curl.exe-Schannel / curl_cffi-Chrome）；
  - 新增 `app/transports.py`：`curl_cffi`（libcurl-impersonate）后端，接口对齐引擎用到的
    httpx 子集（`stream` / `status_code` / `headers` / `aiter_bytes` / `aread` / `aclose`）；
  - 引擎遇 412/403 时**自动**切换传输栈重试一次（`DownloadTask._fallback_transport`），
    并在卡片上提示「已自动切换为 Chrome TLS 指纹后端」；
  - 会话模块新增 `browser_probe`：WebView2 页面上下文同源 `fetch`（真 Chromium 网络栈 + 登录态），
    用于判定"浏览器能下、第三方客户端不行"；
  - `requirements.txt` 增加 `curl_cffi>=0.7`，`启动下载器.bat` 的依赖检查同步更新；
  - 实测：本地模拟 WAF（按 UA/栈拦截）时，深度诊断正确给出「浏览器栈能下、其它不行」；
    `transport=curl_cffi` 跑完整 6MB 分块下载 SHA256 一致；首个探测 412 时自动兜底切换并完成下载。
- **2026-10-23（托盘常驻）**：关闭窗口改为最小化到 Windows 系统托盘，下载在后台继续：
  - 新增 `app/tray.py`：`NotifyIcon` 直接走 pywebview 已加载的 pythonnet / WinForms，
    **不新增任何依赖**；创建/销毁/更新统一 marshal 到主窗 UI 线程，菜单回调只起短线程再调
    pywebview（避免 `Invoke` 重入）；
  - 用 `window.events.closing` 的**可取消**语义拦截关闭（返回 `False` → `FormClosing.Cancel`），
    再把主窗与登录窗一起 `hide()`；
  - 托盘悬停提示每 3 秒显示「N 个任务下载中 · X MB/s」；
  - 托盘不可用时自动退化为"关闭即退出"，异常不阻塞主流程；
  - 新增 `assets/tray.ico`：用标准库（无 Pillow）手写 ICO，4× 超采样抗锯齿，
    造型与界面 logo 一致；
  - 新增 GUI **单实例互斥体**，防止托盘常驻时重复启动抢同一个 WebView2 配置目录；
  - 实测隐藏状态下 `evaluate_js` 仍可推送进度，恢复窗口后 UI 数据完整。
- **2026-10-23（会话凭据）**：新增内置 WebView2 会话，彻底替代「手工 Copy as cURL」这一必做步骤：
  - 新增 `app/sites.py`（站点表）与 `app/session.py`（登录窗 + 按目标 URL 精确取 Cookie）；
  - `main.py` 改为 `private_mode=False` + `storage_path=会话数据/`，登录态跨重启保留；
  - 取 Cookie 时临时把后端 URL 指向目标直链，调用后按 RFC 6265 过滤
    （实测 WebView2 的 `GetCookiesAsync(uri)` 并不保证按 host 过滤，这一层必须自己做，否则会把无关站点 Cookie 发给 CDN）；
  - **重启后自动复用登录态**：Cookie 存在 WebView2 配置里，但必须有窗口才读得出来，
    因此新增「隐藏读取窗」（`SessionManager._reader`）——进程刚启动、没有可见登录窗时也能读到，
    再也不需要"每次重启先开一次登录窗"；
  - 修复 `_auth_hint` 从 `@staticmethod` 改为实例方法后误用 `self.cfg` 造成的
    `NameError: name 'self' is not defined`（412 提示语动态化时引入）；
  - **凭据注入改由 Manager 统一负责**（`credential_provider`）：新建任务、点「重试/继续」、
    下载途中 403/412 都会重新读取会话，解决「先建任务后登录」时重试仍 412 的问题；
  - 直链诊断升级为 **Cookie × Referer 四组合**探测，并给出可执行结论 + 卡片上的「按此修正并重试」
    （`DownloadConfig.send_default_referer` 让「不发送 Referer」成为可选项）；
  - 412 提示语动态化：直接写明本次「是否带了 Cookie / Referer 是什么」，不再笼统让人去填；
  - 新增后端 → 前端 Toast 通道（`manager.notify`），注入失败会明确提示去登录；
  - 使用会话的真实 Edge UA 时自动关闭 Sec-Ch-Ua / Sec-Fetch-*，避免 UA 与客户端提示自相矛盾；
  - 修复前端既有 bug：`mounted()` 早于 pywebview 注入 `window.pywebview.api`，
    导致首页误报「请通过 main.py 启动应用」并跳过 `loadAll()`；改为等 `pywebviewready`；
  - 关闭主窗时自动收起登录窗，避免进程被会话窗挂住。
- **2026-10-23**：修复 pywebview 6.x 不自动解包 `concurrent.futures.Future` 导致的
  `Object of type Future is not JSON serializable` 崩溃（api 层同步解包后返回快照）；
  文件夹对话框改用 `webview.FileDialog.FOLDER` 消除弃用告警；修正 cURL 解析器模块
  docstring 的无效转义 SyntaxWarning；补充脏检查点自愈（告警 + 清理残留）与 416 独立提示分支。

## 后续路线图

- [x] 夸克分享链接 → CDN 直链：插件式解析器，内置会话自动取登录态（已上线）
- [x] 分享子目录下钻、多文件勾选批量下载（已上线；单个目录最多列出 1000 个文件）
- [x] 其余网盘分享解析：UC / 百度 / 123 / 迅雷 / 蓝奏 五个解析模块已接入
      （UC 与夸克同构；123/蓝奏匿名可下；百度登录后可签直链；迅雷直链需登录）
- [x] 百度直链签发（`wxlist` 列目录 + `gettemplatevariable`/`tplconfig`/`sharedownload`
      签名；登录后可用，不转存）
- [ ] 迅雷直链签发对齐（登录后再签发）
- [ ] 文件夹整包一键下载（当前需进入文件夹后勾选文件）
- [ ] 全局任务索引，应用重启后自动恢复任务列表
- [x] 直链 / 分享 stoken 过期后一键重新解析续传（会话已在手，可自动重新签发）——
      分享任务已自动重签（`_reissue_share_url`）；纯直链任务没有重签凭据，仍需重新取一条链接，
      但**进度不会丢**（按文件名 + 大小 + ETag 认领原有检查点）
- [ ] 分块哈希校验（etag/CRC，可选）
- [ ] 多任务全局连接预算与任务队列
- [ ] 多镜像/多源合并下载

## 许可证

本项目以 [MIT License](LICENSE) 发布：可自由使用、修改、分发与商业使用，
只需保留版权声明与许可声明。

## 相关链接

- 仓库：<https://github.com/hehe0602/any-downloader>
- 问题反馈：[Issues](https://github.com/hehe0602/any-downloader/issues)
- 开发文档：[docs/开发文档.md](docs/开发文档.md)
- 交接说明：[docs/交接说明.md](docs/交接说明.md)

## 致谢

- [pywebview](https://pywebview.flowrl.com/)：轻量 WebView 桌面外壳
- [httpx](https://www.python-httpx.org/) / [curl_cffi](https://github.com/lexiforest/curl_cffi)：HTTP 客户端与 TLS 指纹
- [Vue 3](https://vuejs.org/)：前端框架
