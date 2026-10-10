# get_thesis —— 湖南大学研究生系统「论文评阅书」自动检查 + 微信推送

自动登录 <http://yjsxt.hnu.edu.cn/gmis/> 学生端，检查**论文评阅书**是"已有记录"还是"暂无数据"，
并在**发现评阅书时**通过 **DSH 微信插件**推送通知（"暂无数据"期间保持静默）。

日常运行 = **Python 常驻进程每 60 秒检查（0 token）+ DSH 定时任务每 30 分钟看护**，
见 [自动化运行](#自动化运行当前已生效)；下面是手动排查用的命令。

## 快速开始

```bash
python check_thesis.py                 # 手动检查一次（发现评阅书才推送）
python check_thesis.py --no-push       # 只检查，打印结果
python check_thesis.py --force-login   # 忽略缓存会话，强制重新登录
```

运行环境：推荐用 **conda 环境 `get_thesis`**（Python 3.11，已装
`requests` / `cryptography` / `Pillow` / `numpy`，够用；代码不依赖 scipy）：

```powershell
& 'C:\Users\gejiamin\anaconda3\envs\get_thesis\python.exe' check_thesis.py
```

实测输出：

```
[2026-10-10 17:14:47] 学生端首页 student/default/index -> 200 (14861 字节)
[2026-10-10 17:14:47] 菜单中定位到「论文评阅书上传」-> student/bygl/lwpyssc
[2026-10-10 17:14:47] 数据接口返回空（即页面显示「暂无数据」）
【湖南大学研究生系统】论文评阅书检查
学号：B22********（运行时会显示真实学号）
入口：论文评阅书上传
结果：📭 暂无数据（系统里还没有可查看/下载的论文评阅书）
```

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `check_thesis.py` | **主程序**：登录 → 定位评阅书入口 → 拉列表 → 组装结论 → 微信推送 |
| `watch_thesis.py` | **常驻检查主逻辑**：默认每 60 秒一轮，只在发现评阅书时推微信；`--once` 只跑一轮 |
| `ensure_watcher.ps1` | 自愈拉起脚本：进程不在才拉起（含"今日重启上限"保护） |
| `run_hidden.vbs` | 隐藏启动器：拉起时不开窗口（**纯 ASCII，勿加非 ASCII 字符**） |
| `watch_status.py` | 健康检查：心跳新鲜返回 0，过期/异常返回非 0（DSH 看门狗据此判断） |
| `hnu.py` | gmis 站点客户端：cookieless 会话管理、RSA 加密、验证码获取、登录、取页面 |
| `captcha_ocr.py` | 验证码识别引擎（固定槽位切片 + 平移不变模板匹配） |
| `templates.npz` | 0–9 数字模板（由标注样本对齐+多数投票生成，已去干扰线） |
| `wechat_push.py` | 复用 DSH 微信插件登录态，调用 iLink 机器人接口推送文本 |
| `build_templates.py` | 离线重建模板并报告准确率（含人工标注数据） |
| `make_montage.py` | 把 `caps/` 样本拼成大图，便于人工标注 |
| `caps/`、`captcha_now.gif`、`montage_0/1.png` | 40 张验证码样本与标注用拼图（仅用于重建模板） |
| `.hnu_session.json` | 会话缓存（cookies + 会话 URL），实现"已登录就直接复用" |
| `.thesis_state.json` | 上次检查结论与记录签名，用于判断"是否是新发现的评阅书" |
| `.watch_heartbeat.json` | 轮询心跳（pid/轮次/状态），看门狗与 `watch_status.py` 依据它判断存活 |
| `check_thesis.log`、`watchdog.log` | 运行日志、看门狗拉起日志 |

## 关键技术点

### 1. 登录接口（不是普通表单）

`/home/stulogin_do` 接收一个 JSON 字符串字段 `json`，其中：

* `Password` —— 必须用登录页内嵌的 **RSA 公钥**（JSEncrypt，PKCS#1 v1.5）加密后 base64；
* `VeriCode` —— 图形验证码，**服务端强制校验**（错误返回 `{"jg":"0","msg":"验证码错误"}`）。

另外该系统是 ASP.NET **cookieless 会话**：会话 ID 写在 URL 的 `(S(...))` 段里，
所以缓存会话时必须**连 URL 一起存**，只存 cookie 会失效。

### 2. 验证码识别（100% 实测准确率）

对 40+ 样本分析后发现验证码有很强的规律：

* 102×46 GIF，4 位数字，**位置固定**：第 k 位横向窗口 = `[25k-3, 25k+24)`；
* 数字本体只有 **±5px 左右抖动**，另有 1px 干扰线与浅色噪点；
* 颜色量化干净：深蓝描边 = `(B-R) ≥ 50 且 R+G+B < 560`，恰好排除所有背景噪点。

因此采用：**固定槽位切片 → 颜色掩码 → 对 10 个模板做平移不变的最小汉明距离匹配**。
模板由 25 张人工标注样本（`montage_0/1.png` 一次性标注 96 个字符）经"对齐 + 多数投票"生成，
投票过程天然抹掉了干扰线。

> 匹配时距离在**整幅画布**上统计并惩罚越界，否则会出现"把图形移出画布 → 重叠区变小 → 距离变小"的退化解。

**实测结果：**

* 24/24 标注样本识别正确（96/96 个字符），模板一致性 0.98+，最易混淆对（0/6、1/4）余量仍有 46+；
* 对真实服务器连续 **6 次全新登录：6/6 一次成功（100%）**；
* 注入一次错误验证码后，程序自动换一张重试成功（2 次内恢复）。

即使偶发识别错误也不影响可用性：`ensure_login()` 会**换新验证码重试最多 8 次**
（8 次全错的概率约 10⁻⁸），且只有非验证码类错误（如密码错误）才立即中止，避免账号被锁。

### 3. 「论文评阅书」怎么判断

学生端首页 `student/default/index` 本身是框架页，真正的内容来自 AJAX：

1. `POST student/default/MenuleftInfo` 取左侧菜单 → 按关键字「评阅」定位
   到**论文评阅书上传** → `student/bygl/lwpyssc`；
2. 打开该页，正则取出 EasyUI datagrid 的数据接口（`url: 'lwpyssc_list'`）；
3. `POST student/bygl/lwpyssc_list` 取记录：
   * **有记录** → 判定"有论文评阅书"，逐条列出"第几次送审 / 状态 / 姓名 / 学号"；
   * **返回空列表** → 判定"**暂无数据**"。

> "暂无数据"这个文案来自站点自带的 `easyui-lang-zh_CN.js`：
> `$('<div class="datagrid-empty">').html(opts.emptyMsg || '暂无数据')` ——
> 即空表格就显示"暂无数据"，与接口返回空列表完全对应。

### 4. 微信推送

直接复用 DSH 微信插件（`dsh-wechat`）自己保存的登录态，不需要额外配置：

* `~/.dsh-wechat/auth/token.json` → 机器人 `token` / `baseUrl`；
* `~/.dsh-wechat/state.json` → 默认收件人 `peerUserId`。

然后 `POST {baseUrl}/ilink/bot/sendmessage`，请求头带
`Authorization: Bearer <token>`、`AuthorizationType: ilink_bot_token`、随机 `X-WECHAT-UIN`，
报文体为 `{"msg": {...}, "base_info": {"channel_version": "1.0.2"}}`。
`wechat_push.py` 还处理了落单代理项（iLink 会以 `ret=-1` 拒收）、超长文本分条、网络失败指数退避重试。

## 自动化运行（当前已生效）

结构：**Python 常驻进程负责 1 分钟一轮的实际检查（0 token），DSH 定时任务只当看门狗**。
这样既满足"放进 DSH 自动化任务"，又把 token 消耗压到最低。

```
┌─ Python 常驻进程 watch_thesis.py --interval 60 ───────────────┐
│  每 60 秒：复用会话 → 取评阅书列表 → 只在发现评阅书时推微信      │
│  完全不消耗 token；无 DSH 唤醒；无窗口                        │
│  连续失败指数退避（最长 30 分钟）                              │
│  登录熔断：1 小时内真正登录 >8 次即停手，保护账号               │
│  每轮写心跳 .watch_heartbeat.json                             │
└──────────────────────────────────────────────────────────────┘
        ▲ 心跳过期才拉起（正常情况下什么都不做）
┌─ DSH 定时任务「论文评阅书监控-常驻进程看门狗」（every 1800s）──┐
│  id: schedule-545b613e-1ab7-4b90-b5bb-6402d8602b52           │
│  心跳新鲜 → 只回“正常”，不推微信、不重启                       │
│  心跳过期 → 用隐藏启动器拉起常驻进程 + 微信告警                  │
└──────────────────────────────────────────────────────────────┘
```

### 为什么这样最省 token（重要）

DSH 定时任务每触发一次**就是一轮完整 Agent 对话**，而且要把整个会话上下文重新过一遍
（本会话缓存已达 500+ KB，粗估 ≈15 万 tokens/轮），所以少唤醒一次就省一大笔：

| 方案 | 每天 DSH 唤醒 | Python 检查频率 |
| --- | --- | --- |
| 纯 DSH 任务、每 1 分钟 | **1440 轮** | — |
| 纯 DSH 任务、每 5 分钟 | **288 轮** | — |
| **当前：看门狗 + 常驻进程** | **48 轮**（看门狗 30 分钟） | **1 分钟，0 token** |

即：**检查频率从 5 分钟提到 1 分钟，DSH 唤醒反而从 288 降到 48 轮**；
若与"纯 DSH 每分钟"相比省 30 倍。看门狗间隔想再放宽可把 `every_seconds` 改成 3600（24 轮/天）。

### 管理

```
schedule_list                            # 查看看门狗任务与 everySeconds
watch_status.py                          # 健康检查：0=常驻进程健康，非 0=已停止
```

停止常驻监控（看门狗也不会再拉起）：

```powershell
New-Item 'C:\Users\gejiamin\Downloads\get_thesis\.watch_stop' -ItemType File -Force
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*watch_thesis.py*' } | Stop-Process -Force
```

重新启动：删掉 `.watch_stop`，然后跑一次隐藏启动器（或等看门狗自动拉起）：

```powershell
Remove-Item 'C:\Users\gejiamin\Downloads\get_thesis\.watch_stop' -Force -ErrorAction SilentlyContinue
wscript.exe //B "C:\Users\gejiamin\Downloads\get_thesis\run_hidden.vbs"
```

| 组件 | 文件 | 说明 |
| --- | --- | --- |
| 常驻检查进程 | `watch_thesis.py --interval 60` | 不消耗 token，循环检查 |
| 看门狗（DSH 任务） | 本会话 `schedule-545b613e-…` | 只做保活，正常时零动作 |
| 拉起脚本 | `ensure_watcher.ps1` | 进程不在才拉起，含重启上限保护 |
| 隐藏启动器 | `run_hidden.vbs` | 消除命令行弹窗的关键 |
| 健康检查 | `watch_status.py` | 心跳新鲜返回 0，过期/异常返回非 0 |

> ℹ️ 如果连看门狗这 48 轮也想省掉，可以完全脱离 DSH：把 Windows 计划任务
> `GetThesis_ReviewWatch` 启用（已注册好、当前 Disabled，同样走隐藏启动器、无弹窗），
> 并把 DSH 看门狗删掉 —— 代价是保活逻辑不再受 DSH 管理。

### 推送策略与手动命令

```bash
watch_thesis.py                     # 默认：只在发现评阅书时推送
watch_thesis.py --interval 60       # 常驻模式下的轮询间隔（秒），当前 60
watch_thesis.py --once              # 只跑一轮后退出（手动排查用）
watch_thesis.py --only-change       # 改成“任何结论变化都推送”
watch_thesis.py --always-push       # 改成“每轮都推送”（很吵）
check_thesis.py --no-push           # 只检查并打印，不推送
```

### 负载说明

* **登录次数极少**：会话缓存让 ASP.NET 滑动会话长期有效，通常 **≈1 次登录/天**；
  每轮只增加 3 个普通页面请求；
* 1 分钟一轮 = 约 1440 轮/天，但**这些都是 Python 直接发的 HTTP 请求，不消耗 token**；
  对学校系统而言仍是每分钟 3 个普通页面请求，压力有限；
* 若学校会话策略变化导致频繁重新登录，熔断器会在 1 小时内 8 次后停手，避免账号被锁；
* 想更保守就把常驻进程的 `--interval` 调到 300（5 分钟）。

> ⚠️ **踩过的两个坑（都已修复，改脚本时注意）**
>
> 1. **PowerShell 脚本必须带 UTF-8 BOM**。Windows PowerShell 5.1 会把无 BOM 的 `.ps1`
>    按 GBK 解码，中文注释被打乱后会破坏引号配对，脚本直接语法报错
>    （`The string is missing the terminator`）。`run_hidden.vbs` 则相反，**要保持纯 ASCII**
>    （wscript 按 ANSI 读 .vbs）。
> 2. **`schedule_update` 会把 `everySeconds` 重置成 60**。实测只改 `prompt`/`title`、
>    不传 `every_seconds`，间隔也从 1800 变成了 60（导致每隔一分钟就唤醒一次）。
>    **要改间隔请删除后重建，并重建后用 `schedule_list` 核对。**
> 3. 若用 Windows 计划任务方案：登录模式是 `Interactive only` 时直接执行
>    `powershell.exe` / `python.exe` 会每次都闪出命令行窗口，必须经
>    `wscript.exe //B` + VBS `sh.Run cmd, 0, False` 隐藏调起。

> ℹ️ 早先那套并行部署（`Documents\deepseek-harness\...\hnu_thesis`）已停用：
> 其计划任务 `HNU_ThesisReviewWatchdog` 与本目录的 `GetThesis_ReviewWatch` 目前都是
> **Disabled**（未删除，随时可恢复），相关监控进程均已停止。

## 注意事项

* **凭据**：学号/密码**不写进代码**，运行时从环境变量读取（`check_thesis.py` 顶部只保留变量名），
  因此本仓库可以安全公开。运行前需设置：

  ```powershell
  $env:HNU_USER_ID  = "你的学号"
  $env:HNU_PASSWORD = "你的密码"
  ```

  若本地已有有效会话缓存（`.hnu_session.json`），可先不设置，只有真正需要重新登录时才会要求凭据。
* **会话有效期**：`.hnu_session.json` 只在服务端会话未过期时有效；失效会自动重新登录，无需人工干预。
* **合规**：请仅用于查询本人信息，控制检查频率，避免对学校系统造成压力。
