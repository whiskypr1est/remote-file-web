# 远程文件管理（Windows 桌面风格 Web 应用）

在浏览器里像操作本机 Windows 一样浏览和管理服务器文件。

后端 **Python + FastAPI + uvicorn**，前端 **原生 HTML/CSS/JavaScript + winbox.js 窗口系统**，
不依赖 Node.js、不需要任何前端构建步骤，window 端所有第三方库（winbox.js / pdf.js / xterm.js / CodeMirror）都已内置到
`static/vendor/`，**局域网内即使完全不通外网也能正常使用**。

---

## 目录

- [一、功能特性](#一功能特性)
- [二、目录结构](#二目录结构)
- [三、环境要求](#三环境要求)
- [四、快速开始（Windows）](#四快速开始windows)
- [五、配置详解 config.json](#五配置详解-configjson)
- [六、修改登录账号密码](#六修改登录账号密码)
- [七、注册为 Windows 服务（NSSM 开机自启）](#七注册为-windows-服务nssm-开机自启)
- [八、放行防火墙端口](#八放行防火墙端口)
- [九、Office 文档预览（可选安装 LibreOffice）](#九office-文档预览可选安装-libreoffice)
- [十、安全设计说明](#十安全设计说明)
- [十一、接口一览](#十一接口一览)
- [十二、常见问题 FAQ](#十二常见问题-faq)
- [十三、已知限制](#十三已知限制)

---

## 一、功能特性

### 桌面与窗口
- 仿 Windows 10 桌面：全屏蓝色渐变壁纸（可自定义）、桌面图标、底部任务栏
- 任务栏：开始按钮（含「文件资源管理器 / 各根目录 / 关于 / 更换壁纸 / 注销」）、已打开窗口按钮（点击切换或最小化）、系统托盘（服务器地址 + 实时时钟 + 日期）
- 窗口基于 winbox.js：拖动标题栏移动、四边四角缩放、最小化、最大化/还原、关闭、多窗口层叠、点击置顶
- 双击标题栏最大化/还原、双击图片预览切换适应窗口/原始大小
- 窗口最大化会精确停在任务栏上方（不会盖住任务栏）
- 支持地址栏深链接：`#explorer/根标识/路径`、`#preview/根标识/路径`，可收藏或分享给同事
- **虚拟桌面快捷方式**：在资源管理器里右键文件夹/文件 →「发送到桌面快捷方式」，
  桌面就会出现带小箭头角标的图标。快捷方式**只存在于这个 Web 虚拟桌面**，
  不会往 Windows 真实桌面写任何东西；数据由服务端持久化
  （`desktop_shortcuts.json`），**重启服务、换浏览器、清缓存都不会丢**。
  桌面右键快捷方式可「打开 / 打开所在位置 / 重命名 / 删除快捷方式」，
  目标被移走或 U 盘拔出时会自动标灰划掉并给出提示
- **全盘访问**：默认自动挂载本机所有磁盘（C:/D:/E:…），「此电脑」里直接就能看到，
  不再局限于单个共享目录；插上 U 盘重启服务即自动出现

### 文件资源管理器
- **地址栏**：面包屑导航 + 可点击切换为输入框，支持直接粘贴绝对路径回车跳转
- **工具栏**：后退、前进、向上、刷新
- **视图切换**：图标视图（含图片缩略图）/ 详细列表视图（名称、修改日期、类型、大小，点表头排序）
- **多选**：单击、Ctrl+单击、Shift+范围选择、Ctrl+A 全选、Esc 取消
- **双击**文件夹进入，双击文件按类型打开预览窗口
- **「此电脑」视图**：展示所有可访问根目录及磁盘容量条
- 文件图标按扩展名区分：文件夹、图片、文档、演示文稿、PDF、压缩包、视频、音频、表格、代码、其他
- 右侧按钮区 + 右键菜单 + 键盘快捷键（F2 重命名、Delete 删除、F5 刷新、Backspace 向上、Enter 打开）
- 右键菜单对文本类文件还有**「编辑」**：在带语法高亮的编辑器窗口里直接改服务端的文件
  （保存用 `Ctrl+S`；只读根目录下该项置灰）。详见[编辑文本文件](#编辑文本文件)

### 文件操作
- 新建文件夹、重命名（就地编辑，自动选中主文件名不含扩展名）、删除（**默认进系统回收站，可恢复**）
- **复制 / 剪切 / 粘贴**：`Ctrl+C` / `Ctrl+X` / `Ctrl+V`，或右键菜单；支持跨盘（C: → D:）复制与移动
- **拖动**：把文件/文件夹拖到文件夹图标上＝移动进去，按住 `Ctrl` 拖动＝复制；
  支持在多个资源管理器窗口之间拖动；被剪切的项目显示为半透明，粘贴后恢复
- 粘贴遇到同名文件**自动改名**（`报告.docx` → `报告 (1).docx`），**永不覆盖**已有文件
- 上传：支持文件选择与**拖拽上传**，单个文件最大 2GB，实时进度条，重名自动改名
- 下载：单文件直接下载；多选自动在服务器端打包成 ZIP 后下载
- 删除前弹确认框，删除失败会如实告知原因
- **压缩 / 解压**：右键「压缩…」可打包成 `zip` / `tar` / `tar.gz` / `7z` / `rar`；
  右键压缩包可「解压到当前文件夹」或「解压到 <包名> 文件夹」

> **不能**把文件从浏览器里拖到真实的 Windows 桌面 —— 这是浏览器的安全限制，
> 拖动只在「这个 Web 虚拟桌面内部」有效。
>
> 复制/移动是**同步**执行的（与「打包下载」一致）：选中几十 GB 时请求会等待较久，
> 期间界面会显示忙碌状态。

#### 压缩与解压

| 格式 | 创建 | 解压 | 依赖 |
| --- | --- | --- | --- |
| `.zip` | ✅ | ✅ | 无（标准库） |
| `.tar` / `.tar.gz` / `.tgz` / `.tar.bz2` / `.tbz2` / `.tar.xz` / `.txz` | ✅ | ✅ | 无（标准库） |
| `.gz` / `.bz2` / `.xz`（**单个文件的压缩流**，如 `app.log.gz`） | — | ✅ | 无（标准库） |
| `.7z` | ✅ | ✅ | `py7zr`（纯 Python，已随 requirements 安装） |
| `.rar` | ✅ | ✅ | WinRAR 的 `Rar.exe` / `UnRAR.exe`，自动探测安装目录 |

> 后缀决定真正的压缩方式，这一条有回归测试盯着（看的是**文件头魔数**，不是文件名）。
> `.tgz` / `.tbz2` / `.txz` 这些别名也都在覆盖范围内 —— 它们并不以 `.gz`/`.bz2`/`.xz` 结尾，
> 只按后缀结尾判断会把它们漏掉、产出「没压缩的 tar 却叫压缩包」，实测踩过这个坑。
>
> 裸 `.gz`/`.bz2`/`.xz` 有两种可能：包着 tar（`.tar.gz`）或只压了一个文件（`app.log.gz`）。
> 会先按 tar 试，打不开就退到单文件流，解成 `app.log`。

- 压缩包**生成在服务器上**（不是下载），默认放在源文件旁边；重名会自动改名，
  不会覆盖已有的压缩包。
- 解压目标若已存在同名项则**整体拒绝**并列出冲突的名字 —— 解压是批量写入，
  一旦覆盖几乎没有恢复手段，所以宁可让你换个目录重来。
- 中文文件名兼容：Windows 自带「发送到 → 压缩(zipped)文件夹」生成的是
  GBK 名字且未置 UTF-8 标志位，解压时会自动还原（仅在确实解出中日韩字符时才改写，
  不会把西欧语言的合法文件名弄坏）。
- 解压有防「解压炸弹」上限，见配置 `archive` 段；RAR 相关能力需要 WinRAR，
  未安装时会明确提示而不是静默失败。
- **加密压缩包（带密码）暂不支持**：会明确告诉你「需要密码才能解压」，
  而不是抛一个看不懂的服务器内部错误。创建压缩包也不支持设密码。
- 打包时**不会跟进符号链接与目录联接**（junction）：Windows 上
  `os.path.islink()` 认不出目录联接，一旦跟进就会把联接指向的外部内容一起装进包里。
  被跳过的项会在结果里如实列出。创建 RAR 时另加了 `-ol`，因为 RAR 有自己的遍历逻辑。

### 预览
| 类型 | 预览方式 |
|---|---|
| 图片 jpg/png/gif/webp/bmp/… | 窗口内直接显示，放大、缩小、适应窗口、原始大小、旋转、滚轮缩放、拖动平移 |
| PDF | 内置 pdf.js（含中文 CMaps），支持翻页、页码跳转、缩放、适应宽度，大文档按需懒加载渲染。**若 pdf.js 在 8 秒内未就绪会自动切换到浏览器内置 PDF 阅读器**（同样支持翻页缩放），确保一定能看到内容 |
| 文本 txt/md/log/json/csv/xml/代码… | 等宽字体展示，自动识别 UTF-8 / GB18030(GBK) / Big5 等编码，超长截断保护 |
| doc/docx、xls/xlsx、ppt/pptx | 装 LibreOffice 时转 PDF 预览；**没装时自动降级**：docx/xlsx/pptx 用内置解析器提取文字表格生成网页版预览 |
| 视频/音频 | HTML5 播放器在线播放，后端支持 HTTP Range，**进度条可随意拖动** |
| 压缩包及其他 | 显示文件信息 + 一键下载（不做在线解压） |

### 编辑文本文件

文本预览的工具栏上有**「编辑」**按钮，资源管理器里右键文件也有**「编辑」**，
都会另开一个带语法高亮的编辑器窗口（CodeMirror 5，本地内置）。

**会高亮的扩展名**（其余文本按纯文本打开，一样能改）：

| 语言 | 扩展名 |
|---|---|
| Python | `.py` |
| Shell | `.sh` `.bash` |
| 批处理 | `.bat` `.cmd` |
| PowerShell | `.ps1` |
| JavaScript | `.js` `.mjs` |
| JSON | `.json` |
| HTML / XML | `.html` `.htm` `.xml` `.svg` |
| CSS | `.css` |
| Markdown | `.md` |
| YAML | `.yml` `.yaml` |
| SQL | `.sql` |
| ini / properties | `.ini` `.cfg` `.conf` `.properties` |
| 纯文本 | `.txt` `.log` `.csv` |

- **`Ctrl+S` 保存**，也可以用工具栏的「保存」按钮；标题栏出现 `●` 表示有未保存的改动。
- **「还原」**重新从磁盘读一遍，放弃当前改动（会先确认）。
- **带未保存改动关窗口会先问一句**，选「取消」窗口就留着。
- 编码（UTF-8 / GB18030 / Big5…）、BOM、换行符（CRLF / LF / CR）都会**原样保留**：
  读的时候服务端告诉你原来是什么，保存时按原样写回去，不会把 CRLF 文件悄悄变成 LF。

#### ⚠️ 两条必须知道的安全行为

1. **文件太大时不能在这里改。**
   预览和编辑器都只读文件的前 `preview.text_max_kb`（默认 2048KB）。
   超过这个上限的文件，编辑器会用一层遮罩**锁成只读**并写明原因 ——
   因为缓冲区里只有前半段，一旦保存就等于把后半段删掉，而界面上看不出来。
   要改这种文件请走：**下载 → 本地编辑 → 上传覆盖**。
2. **文件在别处被改过时，保存会被拒绝。**
   打开文件时会记住当时的修改时间和大小；保存时服务端会比对磁盘现状，
   不一致就直接返回 `409` 并**一个字节都不写**，然后给你一个**「重新载入」**。
   这样就不会覆盖掉别人的修改（或者你自己另一个窗口里的修改）。
   重新载入会丢弃你当前的改动，所以那一步由你按下去，程序不会自作主张。
   顺带一提：保存成功后会立刻刷新这份凭据，所以**连续保存第二次**不会误报冲突。

### 命令行（CMD）窗口
- 任务栏「开始」菜单 →「命令提示符」，或桌面右键 →「新建命令提示符窗口」
- **是真正的终端**：后端用 ConPTY（pywinpty）给 shell 分配伪控制台，前端用 xterm.js 渲染，因此：
  - 裸 `python` 能正常进入交互式解释器（出现 `>>>`），`node`、`git`、`ipconfig` 等同理
  - **Tab 补全**、方向键翻历史、**`Ctrl+C` 真正中断正在运行的程序**
  - 窗口缩放会同步给远端（换行位置不会错行），彩色输出正常
  - **支持中文输入法打字**（xterm.js 内置能力），中文路径也可直接敲
- 工具栏：新建 / 清屏 / **中断** / **断开** / **结束会话**（后两个是**不同**的动作，别混）
  - **断开** = 走开：会话在服务端继续跑，输出继续攒，之后还能接回来（窗口里会出现「重新连接」）
  - **结束会话** = 真的关掉：发 `{"type":"close"}`，杀掉整棵进程树，**之后接不回来**。
    所以它需要**二次确认**（不可逆的操作不该挨着随手点的按钮）
  - `Ctrl+C` 是**原生中断**（和真机一样），优先用它
  - 「中断」按钮是**逃生口**：杀掉卡住的子进程但**保留 shell**，
    当前目录还在，适合程序赖着不走时救场
- 工作目录在命令之间保持；默认最多 4 个并发会话，空闲 30 分钟自动关闭
- **关掉浏览器不等于结束会话**：断开连接只是「离开」，`cmd.exe` 继续跑、
  输出继续进缓冲；重新打开页面会用同一个会话 id 接回去，先补上离开期间错过的输出。
  真正结束会话只有三种情况：空闲超时、显式关闭、服务退出。
  这样「浏览器崩了 / 手滑关了标签页」不会把正在跑的任务一起带走。
  - 会话可分离之后 `max_sessions` 更要紧：分离的会话**仍然占名额**（它确实还占着一个 shell），
    所以撞到「会话数已达上限」时，请把不再需要的命令行窗口关掉，或等空闲回收
  - 重连时会话已被回收的话会明确提示「会话已结束」，而不是静默卡住

### 会话持久（关掉页面再打开还是之前的界面）

- 打开的**资源管理器 / 预览**窗口、它们的位置与尺寸、最大化状态、
  每个资源管理器窗口所在的目录与视图模式、以及前进后退历史，
  都会保存到服务端，重新打开页面时原样恢复
- 状态存在 `user_state_path`（默认项目根目录下的 `user_state.json`），
  与 `desktop_shortcuts.json` 同级，方便一起备份
- 保存是**整体覆盖**（前端每次手握完整布局），并做了防抖，拖动窗口不会刷爆服务端；
  单次上限 256KB
- 恢复是**容错**的：某个窗口记的目录已经被删除或盘符没挂上，只会跳过那一个，
  不会导致整个桌面打不开；损坏的状态文件会被当作「没有状态」处理
- 想恢复默认布局，删掉 `user_state.json` 再刷新即可（不影响快捷方式）
- 若目标机器没装 `pywinpty`，会自动**退回管道模式**并在窗口里提示：
  功能仍可用，但上面那些交互能力（交互式程序 / Tab 补全 / Ctrl+C 中断 / 缩放）都没有

> ### ⚠️ 安全警告（请务必阅读）
> 这个功能**等于把服务器的命令行交给任何能登录本服务的人**。
> 以 Windows 服务方式（NSSM）运行时进程身份是 **SYSTEM**，即最高权限；
> 而本服务默认走明文 HTTP，局域网内密码是明文传输的。
> 不需要这个功能时，请把 `config.json` 里的 `terminal.enabled` 改成 `false`（默认是 `true`）。
>
> 另外，真终端下**全屏交互程序（如 `vim`）也可能被跑起来**，
> 这比原来的管道模式能做的事情更多，请务必按上面的建议评估是否开启。

### 认证与安全
详见[第十节](#十安全设计说明)。

---

## 二、目录结构

```
remote-file-web/
├── app.py                      # 程序入口（加载配置、组装应用、启动 uvicorn）
├── config.example.json         # ★ 配置示例（含全部字段与说明，值都是占位符）
├── requirements.txt            # Python 依赖
├── README.md                   # 本文档
├── .gitignore                  # 忽略清单（密钥、缓存、个人数据、虚拟环境）
├── start.bat                   # 一键启动（Windows 双击即可）
├── install_service.bat         # 用 NSSM 注册为 Windows 服务
├── uninstall_service.bat       # 卸载 Windows 服务
├── nssm.exe                    # NSSM 2.24（注册服务用，已随项目提供）
│
├── fileweb/                    # 后端包
│   ├── __init__.py             # 版本号与应用名
│   ├── config.py               # 配置读写、默认值、首次运行初始化
│   ├── security.py             # 口令哈希、会话令牌、路径穿越防护、受保护路径、文件名清洗
│   ├── drives.py               # 磁盘枚举（自动挂载 C:/D:/E:… 为可访问根目录）
│   ├── shortcuts.py            # 虚拟桌面快捷方式的持久化存储
│   ├── userstate.py            # 界面状态（窗口布局等）的服务端持久化
│   ├── deps.py                 # 共享状态、认证/CSRF 依赖、客户端 IP 判定
│   ├── http_utils.py           # Range 请求处理、Content-Disposition、MIME 降级
│   ├── thumbs.py               # 缩略图生成与磁盘缓存（按最久未访问清理）
│   ├── office.py               # Office 预览：LibreOffice 转 PDF + 纯 Python OOXML 解析
│   ├── fsops.py                # 文件系统操作（列举、新建、重命名、删除、打包）
│   ├── archive.py              # 压缩与解压（zip/tar/7z/rar + 逐条目安全校验）
│   ├── terminal.py             # 命令行会话管理（ConPTY/管道 + 编码/限额/分离重连）
│   └── routers/
│       ├── auth.py             # 登录 / 注销 / 登录状态
│       ├── system.py           # 系统信息、LibreOffice 重探测、壁纸上传
│       ├── fs.py               # 列目录、增删改、复制/移动、上传、打包下载、压缩/解压
│       ├── content.py          # 原始文件输出、缩略图、文本预览与保存、Office 预览
│       ├── desktop.py          # 虚拟桌面快捷方式与界面状态的增删改查
│       └── terminal.py         # 命令行窗口：会话创建 + WebSocket 双向流
│
├── static/                     # 前端资源（纯静态，无需构建）
│   ├── index.html              # 桌面主页面
│   ├── login.html              # Windows 风格锁屏登录页
│   ├── favicon.svg
│   ├── css/
│   │   ├── desktop.css         # 桌面、任务栏、开始菜单、窗口外观、对话框
│   │   ├── explorer.css        # 文件资源管理器
│   │   ├── preview.css         # 各类预览窗口
│   │   ├── editor.css          # 文本编辑器窗口（CodeMirror 适配）
│   │   ├── terminal.css        # 命令行窗口
│   │   └── login.css           # 登录页
│   ├── js/
│   │   ├── main.js             # 启动入口（登录校验 → 系统信息 → 建桌面）
│   │   ├── api.js              # API 客户端（CSRF、401 处理、上传进度）
│   │   ├── ui.js               # 对话框、右键菜单、Toast、格式化工具
│   │   ├── icons.js            # 全部内联 SVG 图标
│   │   ├── wins.js             # 窗口管理器（建立在 winbox.js 之上）
│   │   ├── desktop.js          # 桌面外壳、任务栏、开始菜单、壁纸
│   │   ├── explorer.js         # 文件资源管理器窗口
│   │   ├── preview.js          # 各类型预览窗口
│   │   ├── editor.js           # 文本编辑器窗口（CodeMirror 5 语法高亮）
│   │   ├── terminal.js         # 命令行（CMD）窗口
│   │   ├── sessionstate.js     # ★ 桌面布局持久化（保存/还原窗口，终端按 sid 接回）
│   │   └── login.js            # 登录页逻辑
│   └── vendor/                 # 内置第三方库（离线可用，约 4.5MB）
│       ├── winbox.min.js / winbox.min.css
│       ├── VERSIONS.json
│       ├── pdf/                # pdf.js、worker、cmaps、standard_fonts
│       ├── xterm/              # xterm.js、xterm.css、addon-fit.js（终端渲染 + IME）
│       └── codemirror/         # CodeMirror 5 + 各语言 mode（编辑器语法高亮）
│
├── tools/
│   ├── fetch_vendor.py         # 重新下载前端离线资源（构建期用一次）
│   └── gen_password.py         # 生成 / 修改登录口令
│
└── tests/                      # 回归测试（共 192 条，见「开发与测试」）
    ├── __init__.py
    ├── _harness.py             # 测试脚手架：起真实服务子进程 + 带 CSRF 的客户端
    ├── test_fileweb.py         # 路径安全、IP 判定、缩略图、Office、删除报错
    ├── test_features.py        # 复制/移动、命令行会话与限额
    ├── test_archive.py         # 压缩/解压：穿越、炸弹、重名、符号链接、GBK 名
    ├── test_text_edit.py       # 文本编辑：编码/BOM/换行保留、并发冲突 409
    ├── test_terminal_reattach.py  # 终端分离与会话重连
    ├── test_userstate.py       # 界面状态持久化
    ├── test_desktop_state.py   # 桌面快捷方式
    ├── test_config_safety.py   # 配置安全（口令、可信代理等）
    └── test_frontend_wiring.py # 前端跨模块调用名的静态闸门

─── 以下均为「运行时自动生成」，已在 .gitignore 中排除，不会进仓库 ───
├── config.json                 # 实际配置（★ 含口令哈希与 session_secret，切勿提交）
├── FIRST_RUN_PASSWORD.txt      # 首次生成的明文口令（★ 切勿提交，登录后建议删除）
├── user_state.json             # 界面状态（窗口布局、所在目录、视图模式）
├── desktop_shortcuts.json      # 虚拟桌面快捷方式数据
├── static/wallpapers/          # 用户在界面上传的自定义壁纸
├── thumb_cache/                # 缩略图缓存
├── office_cache/               # Office 转 PDF 缓存
├── temp_zip/                   # 批量打包临时文件（自动清理）
├── logs/                       # 注册为服务后的日志（NSSM 生成）
└── .venv/                      # 虚拟环境（实测约 60MB）
```

> **`config.json` 不在仓库里**是这个项目的有意设计：它包含 `session_secret`
> （拿到即可伪造会话 Cookie）与口令哈希，属于**部署数据**而非源码。
> 首次启动会自动生成它并打印随机口令；需要完整字段说明请看 `config.example.json`。

---

## 三、环境要求

| 项目 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11 / Server 2016+（代码同时兼容 Linux/macOS） |
| Python | **3.9 及以上**（开发验证于 3.13.14） |
| 浏览器 | Chrome / Edge（现代版本） |
| Node.js | **不需要** |
| 外网 | **不需要**（前端库已内置；仅首次 `pip install` 需要联网） |

> 命令行窗口的「真终端」能力依赖 **pywinpty**（仅 Windows 需要）。
> 交付用的 `.venv` 里已经装好；自己重建环境时 `pip install -r requirements.txt` 会自动装上。
> 没装的机器照样能启动，只是命令行会退回管道模式。

---

## 四、快速开始（Windows）

### 1. 安装依赖

在项目目录下打开命令行（PowerShell 或 CMD）：

```bat
:: 建议使用虚拟环境，避免污染系统 Python
python -m venv .venv
.venv\Scripts\activate

pip install -r requirements.txt
```

> **提示**：`start.bat` 会优先使用项目目录内的 `.venv`。
> 如果是别人**整个目录打包交付**给你（可能已预置 `.venv`，实测约 60MB），
> 且其 Python 版本与你的兼容，可以**跳过这一步**直接启动。
>
> 但**从本仓库 clone 下来的目录不含 `.venv`**（已在 `.gitignore` 中排除），必须自己建。
> 见下方命令，或删掉已有的 `.venv` 后重建。

如果 pip 下载慢，可换国内镜像：

```bat
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 2. 启动服务

双击 `start.bat`，或在命令行执行：

```bat
python app.py
```

首次启动会自动生成 `config.json` 和随机初始口令，控制台会打印：

```
====================================================================
  远程文件管理  v1.0.0
====================================================================
  本机访问 : http://127.0.0.1:8000
  局域网   : http://192.168.1.100:8000
  监听地址 : 0.0.0.0:8000

  登录账号 : admin
  ...
```

> 本仓库**不预置** `config.json`：首次启动会自动生成它，并把随机口令写入
> `FIRST_RUN_PASSWORD.txt`（见[第六节](#六修改登录账号密码)）。
> 想先看清所有可配置项，可以直接参考 `config.example.json`，它的值都是占位符。

### 3. 打开浏览器

- 本机：`http://127.0.0.1:8000`
- 局域网其他电脑：`http://服务器IP:8000`（IP 见启动横幅，或任务栏右下角托盘）

### 命令行参数

```bat
python app.py --port 9000            :: 临时换端口
python app.py --host 127.0.0.1       :: 只允许本机访问
python app.py --config other.json    :: 指定配置文件
python app.py --log-level debug      :: 输出访问日志，便于排查
python app.py --reload               :: 开发用，改代码自动重载
```

也可以直接用 uvicorn 启动：

```bat
uvicorn app:app --host 0.0.0.0 --port 8000
```

---

## 五、配置详解 config.json

完整字段说明（也可以参考 `config.example.json`）：

```jsonc
{
  "server": {
    "host": "0.0.0.0",        // 0.0.0.0 = 监听所有网卡，局域网可访问；127.0.0.1 = 仅本机
    "port": 8000,             // 服务端口
    "title": "远程文件管理"    // 页面标题与「关于」里显示的名称
  },

  "roots": [                  // 允许访问的根目录，可以配置多个（可留空，见下方自动挂载）
    {
      "id": "share",          // 前端使用的稳定标识（改名不影响已有书签）
      "name": "共享目录",      // 桌面图标 / 开始菜单里显示的名字
      "path": "D:\\Share",    // 实际目录；不存在时启动会自动创建
      "readonly": false       // true = 该目录只读，禁止新建/上传/改名/删除
    }
  ],

  // ★ 自动把本机所有磁盘（C:/D:/E: …）挂载为可访问根目录。
  // 打开后「此电脑」里能直接看到所有盘符；以后插入 U 盘/移动硬盘，
  // 重启服务会自动出现，不用再改配置。
  "mount_all_drives": true,
  // 是否也挂载网络驱动器（网络盘无响应时可能让界面卡顿，默认关闭）
  "mount_network_drives": false,
  // 是否挂载可移动磁盘（U 盘、移动硬盘）
  "mount_removable_drives": true,

  // ★ 受保护路径：允许浏览，但禁止新建/重命名/删除/上传。
  // 开放整盘访问后这道保险很有用 —— 误删这些目录可能让 Windows 直接起不来。
  // 想完全放开就改成空数组 []。
  "protected_paths": [
    "C:\\Windows",
    "C:\\Program Files",
    "C:\\Program Files (x86)",
    "C:\\ProgramData",
    "C:\\$Recycle.Bin",
    "C:\\System Volume Information",
    "C:\\Recovery",
    "C:\\PerfLogs"
  ],

  "auth": {
    "username": "admin",
    "password_hash": "pbkdf2_sha256$260000$...",  // 推荐：只存哈希
    "password": "",                                // 兼容项：明文（不推荐，启动会告警）
    "session_secret": "自动生成的随机串",           // 会话签名密钥，不要外泄
    "session_hours": 12,                           // 登录状态保持时长
    "max_login_fails": 5,                          // 连续失败几次后锁定
    "lockout_seconds": 300,                        // 锁定时长（秒）
    // ★ 可信反向代理。X-Forwarded-For 是客户端可随意伪造的头，
    // 只有直连对端 IP 命中这个列表时才采信它来判定客户端 IP
    // （登录失败锁定按该 IP 计数）。默认空数组 = 完全不信任该头。
    // 前面挂了 Nginx/Caddy 时填代理所在机器：["10.0.0.5"] 或 ["10.0.0.0/8"]
    "trusted_proxies": []
  },

  "ui": {
    "wallpaper": "",          // 空 = 内置蓝色渐变；也可填 /static/wallpapers/xxx.jpg
    "default_view": "list"    // 新建窗口默认视图：icons | list（本项目默认 list）
  },

  "upload": {
    "max_file_size_mb": 2048, // 单文件上限（2GB）
    "blocked_extensions": [   // ★ 禁止上传的可执行文件扩展名，按需增删
      ".exe", ".com", ".scr", ".pif", ".cpl", ".msi", ".msp",
      ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".vbs", ".vbe",
      ".wsf", ".wsh", ".hta", ".reg", ".inf", ".msc", ".sct",
      ".dll", ".sys", ".drv", ".ocx", ".gadget", ".job", ".lnk",
      ".appx", ".msix", ".appxbundle"
    ]
  },

  "delete": {
    "use_recycle_bin": true   // true = 删除到系统回收站（可恢复）；false = 永久删除
  },

  // ★ 虚拟桌面里的「命令提示符」窗口
  // 等于把服务器命令行交给任何能登录本服务的人；以服务方式运行时身份是 SYSTEM。
  // 不需要就改成 enabled: false（改完重启服务生效）。
  "terminal": {
    "enabled": true,            // 是否启用命令行窗口（默认开）
    "shell": "cmd.exe",         // 也可换成 powershell.exe
    "max_sessions": 4,          // 同时允许的会话数上限
    "idle_timeout_seconds": 1800, // 空闲多久自动关闭（0 = 不限制）
    "max_output_kb": 512,       // 单会话在浏览器侧保留的输出上限
    "start_dir": ""             // 启动目录，留空 = 第一个可访问根目录
  },

  "thumbs": {
    "enabled": true,          // 是否生成图片缩略图
    "cache_dir": "./thumb_cache",
    "size": 100,              // 缩略图边长（像素）
    "max_cache_mb": 512       // 缓存体积上限，超出按最旧优先清理
  },

  "office": {
    "enabled": true,          // 是否启用 Office 预览
    "soffice_path": "",       // 留空自动探测 LibreOffice；也可写死 soffice.exe 完整路径
    "timeout_seconds": 120,   // 单次转换超时
    "cache_dir": "./office_cache",
    "max_cache_mb": 1024      // 转换结果缓存体积上限（MB），超出按最旧优先清理
  },

  "user_state_path": "user_state.json",   // 界面状态（窗口布局）存哪儿，相对路径相对本文件

  "archive": {
    "max_entries": 20000,     // 单个压缩包最多多少个条目，超出拒绝解压
    "max_total_mb": 8192,     // 解压后总体积上限（MB）
    "max_single_mb": 4096,    // 解压后单个文件上限（MB）
    "rar_path": "",           // Rar.exe 完整路径，留空自动探测 WinRAR 安装目录
    "unrar_path": ""          // UnRAR.exe 完整路径，同上
  },

  "preview": {
    "text_max_kb": 2048       // 文本预览最多读取多少 KB，防止打开超大日志卡死
  },

  "log": {
    "level": "info"           // debug | info | warning | error
  }
}
```

**修改配置后需要重启服务生效**（壁纸例外，界面上改完立即生效）。

### 换端口 / 换根目录

- **换端口**：改 `server.port`，或临时 `python app.py --port 9000`
- **换根目录**：改 `roots[].path`。指向一个不存在的目录时，服务启动会自动创建
- **加目录**：往 `roots` 数组里再加一项即可，桌面上会自动多出对应图标

---

## 六、修改登录账号密码

项目提供了专门的工具 `tools/gen_password.py`：

```bat
:: 交互式修改（推荐，输入时不回显）
python tools\gen_password.py --set

:: 随机生成一个新密码并写入配置，同时在控制台显示
python tools\gen_password.py --set --random

:: 直接指定密码
python tools\gen_password.py --set --password "MyNewPass123!"

:: 顺便改用户名
python tools\gen_password.py --set --username boss --random

:: 只生成一个随机密码和它的哈希（不修改任何文件）
python tools\gen_password.py

:: 只把某个密码转成哈希（用于手工编辑 config.json）
python tools\gen_password.py --hash "MyPass123!"
```

**首次启动会自动生成随机口令**，用户名默认 `admin`。口令同时出现在两处：

1. 启动时控制台横幅（醒目打印一次）；
2. 项目根目录的 `FIRST_RUN_PASSWORD.txt`。

> 密码在 `config.json` 中只以 PBKDF2-HMAC-SHA256 哈希形式保存，**没有任何明文**。
> 明文只存在于 `FIRST_RUN_PASSWORD.txt`，登录确认无误后请删除该文件并修改密码。
>
> ⚠️ **本仓库刻意不包含 `config.json` 与 `FIRST_RUN_PASSWORD.txt`**（已在 `.gitignore` 中排除）。
> 前者含 `session_secret`（拿到它就能伪造会话 Cookie），后者是明文口令 ——
> 这两个文件属于**部署数据**，任何情况下都不要提交到仓库。

---

## 七、注册为 Windows 服务（NSSM 开机自启）

[NSSM](https://nssm.cc/download) 可以把任意程序注册成 Windows 服务，实现**开机自启、
崩溃自动重启、无需登录桌面**。

### 步骤

1. **NSSM 已随项目提供**：`nssm.exe`（v2.24）已经放在本项目目录里，**无需另外下载**。
   - 脚本会先在系统 PATH 中查找 nssm，找不到才使用项目目录内的这一份；
   - 若需要更新版本，可从 https://nssm.cc/download 下载，解压后把 `win64\nssm.exe`
     覆盖到本项目目录（与 `app.py` 同级）即可。

2. **以管理员身份**右键运行 `install_service.bat`。

脚本会自动完成：
- 注册服务 `RemoteFileWeb`
- 设置工作目录为项目目录
- 日志输出到 `logs\service_out.log` / `logs\service_err.log`（超过 10MB 自动轮转）
- 崩溃自动重启（延迟 5 秒）
- 停止时先发 Ctrl+C（让服务有机会清理临时文件），5 秒后再强杀
- 设置开机自动启动并立即启动服务

3. **常用管理命令**：

```bat
sc query RemoteFileWeb        :: 查看状态
net start RemoteFileWeb       :: 启动
net stop  RemoteFileWeb       :: 停止
```

4. **卸载服务**：以管理员身份运行 `uninstall_service.bat`

### 手动注册（参考）

```bat
nssm install RemoteFileWeb "C:\路径\项目\.venv\Scripts\python.exe" "app.py"
nssm set RemoteFileWeb AppDirectory "C:\路径\项目"
nssm set RemoteFileWeb AppStdout "C:\路径\项目\logs\service_out.log"
nssm set RemoteFileWeb AppStderr "C:\路径\项目\logs\service_err.log"
nssm set RemoteFileWeb Start SERVICE_AUTO_START
nssm start RemoteFileWeb
```

> **注意**：服务方式运行时，`soffice` 探测与「回收站删除」都以 SYSTEM 账号身份执行，
> 与你在桌面登录的账号看到的回收站可能不是同一个。若发现回收站删除异常，
> 可把 `delete.use_recycle_bin` 改为 `false` 使用永久删除。

---

## 八、放行防火墙端口

Windows 防火墙默认会拦截外部访问。**以管理员身份**执行：

```bat
netsh advfirewall firewall add rule name="FileWeb 8000" dir=in action=allow protocol=TCP localport=8000
```

换端口时把 `8000` 改成实际端口。删除规则：

```bat
netsh advfirewall firewall delete rule name="FileWeb 8000"
```

> 首次以交互方式启动时，Windows 也可能弹出「是否允许应用通过防火墙」，
> 勾选**专用网络**并允许即可。

---

## 九、Office 文档预览（可选安装 LibreOffice）

`doc / docx / xls / xlsx / ppt / pptx` 的预览有两条路线，服务会自动选择：

1. **首选：LibreOffice 无头模式转 PDF**（版式最接近原文）
   - 下载安装：https://www.libreoffice.org/download/download-libreoffice/
   - 安装后**无需重启服务**，在预览窗口点「重新检测 LibreOffice」即可，或重启服务。
   - 也可以在 `config.json` 的 `office.soffice_path` 里写死 `soffice.exe` 的完整路径。

2. **降级：纯 Python 解析 OOXML**（没装 LibreOffice 时）
   - `docx / xlsx / pptx` 本质是 zip + XML，服务会直接解析并生成网页版预览，**文字和表格都能看**，
     但版式与原文有差异（预览页顶部会有黄色提示说明这一点）。
   - 老的二进制格式 `.doc / .xls / .ppt` 无法用这种方式解析，会给出「请安装 LibreOffice」的友好提示，
     不会报错也不会崩溃。

启动横幅会明确显示当前检测结果（下例是已装 LibreOffice 的机器）：

```
  依赖检测：
    LibreOffice : 已找到 -> C:\Program Files\LibreOffice\program\soffice.exe
    缩略图缓存  : D:\Share\thumb_cache
    转换缓存    : D:\Share\office_cache
    删除方式    : 移至回收站
```

未安装时则显示：

```
  依赖检测：
    LibreOffice : 未安装
                  doc/xls/ppt 老格式将无法预览；
                  docx/xlsx/pptx 会用内置解析器降级显示内容。
                  安装地址：https://www.libreoffice.org/download/
```

---

## 十、安全设计说明

| 风险 | 防护措施 |
|---|---|
| **路径穿越**（`../../Windows`） | 双重校验：① 字符串层直接拒绝任何 `..` 段、绝对路径、UNC 路径；② `realpath` 解析符号链接/目录联接后再做「根目录前缀」比对。注意：开启 `mount_all_drives` 后根目录覆盖整个磁盘，此时绝对路径会先被规范化，落回某个盘内属正常放行；规范化后若跑出所有根目录（例如 UNC 路径）仍会被 403 拒绝 |
| **全盘开放后的误操作** | `protected_paths` 列表中的目录（`C:\Windows`、`C:\Program Files`、`C:\ProgramData` 等）仍然可以**浏览和下载**，但禁止新建/重命名/删除/上传，避免误删系统文件导致 Windows 无法启动。需要完全放开时把该列表清空即可 |
| **明文口令** | 配置只存 PBKDF2-HMAC-SHA256 哈希（26 万次迭代 + 随机盐），比较用恒定时间函数 `hmac.compare_digest` |
| **未登录访问** | 中间件统一拦截：除 `/api/auth/login`、`/api/auth/status` 外，所有 `/api/*` 未登录一律 **401** |
| **会话安全** | HttpOnly + SameSite=Lax 签名 Cookie，密钥持久化在配置中；前端脚本读不到 Cookie |
| **CSRF** | 所有改状态请求（POST/PUT/PATCH/DELETE）必须带 `X-CSRF-Token`（由会话令牌派生），并校验 Origin 同源 |
| **暴力破解** | 同一 IP 连续失败达阈值（默认 5 次）锁定 5 分钟。**客户端 IP 取 TCP 对端地址，默认不采信 `X-Forwarded-For`** —— 该头可被随意伪造，早先无条件采信它时，换一个伪造值就能让失败计数归零、完全绕过锁定（实测已复现）；uvicorn 的 `proxy_headers` 也按 `auth.trusted_proxies` 联动，避免它在中间件之前就把来源地址改写掉。只有显式配置了可信代理才会采信该头 |
| **存储型 XSS** | 上传的 `.html/.svg/.xml` 等**内联输出时强制降级为 `text/plain`**；所有响应带 `X-Content-Type-Options: nosniff`；HTML 页面带严格 CSP（`script-src 'self'`） |
| **上传可执行文件** | 按扩展名黑名单拦截 `.exe/.bat/.ps1/.msi/...`，且会同时检查完整后缀链（`evil.bat.exe` 也拦得住） |
| **文件名攻击** | 清洗非法字符、拦截 Windows 保留设备名（`CON/NUL/COM1`…）、去除尾部点与空格、只取最后一段防止夹带路径 |
| **解压炸弹 / 超大文件** | 缩略图**在解码前**做显式像素预检（上限 8000 万像素），并对 JPEG 先用 `draft()` 按 1/2、1/4、1/8 降采样再解码；文本预览限制读取体积；Office 解析限制单个 XML 部件大小（64MB）与表格行列数 |
| **误删磁盘根目录** | 拒绝把「根目录本身」作为删除/打包/重命名的目标。开启 `mount_all_drives` 后每个盘符都是一个可访问根目录，而根目录既不在 `protected_paths` 里也不是只读，此前 `paths: [""]` 这类请求可以通过全部校验 |
| **命令行窗口（高危）** | 默认开启，但 WebSocket 与普通接口走**同一套认证**，并额外校验 `Origin` 同源；会话 ID 随机且与当前会话令牌绑定，无法枚举或跨会话复用。它**等价于把服务器 shell 交给任何能登录本服务的人**（服务方式下是 SYSTEM 权限），不需要时请关闭 `terminal.enabled` |
| **缓存无限增长** | 缩略图与 Office 转换缓存都有体积上限（`thumbs.max_cache_mb` / `office.max_cache_mb`），超出后按最旧优先清理；转换超时会**连同子进程一起结束**，遗留的 `office-*` 临时目录按年龄回收 |
| **接口暴露** | 自动文档（`/docs`、`/openapi.json`）已关闭 |
| **分离会话的寿命变长** | 「断开不再结束会话」意味着一个 shell 可能比一次登录活得更久，所以会话与「创建它时的登录令牌摘要」强绑定：连 WebSocket 必须提供**同一个**令牌才能接管，即使 sid 因某种原因泄露也**无法被他人接管**（否则 sid 跨身份可用就等于把别人的 shell 变成公共资源）。未知 sid 在**握手阶段**就被拒，不给探测有效 sid 的机会 |
| **解压写入任意文件（Zip Slip）** | 压缩包内的条目名是**外部可控**的，所以解压没有使用 `extractall`，而是逐条目校验：拒绝绝对路径、盘符（`C:`）、UNC、任何 `..` 段，再用 `realpath` 二次确认没跑出目标目录；符号链接/硬链接/设备文件条目一律**跳过**（否则等于给出一个「写任意链接」的跳板）。7z/RAR 先解到目标目录下的暂存目录，再按校验过的名字搬过去 |
| **解压炸弹** | 限制条目数、解压后总体积与单文件体积（`archive` 段），超限直接报错中止而不是截断；体积在**落盘之前**就要先算一遍，避免解压到一半才发现空间不够 |
| **解压覆盖已有文件** | 目标存在同名顶层项时**整体拒绝**（`409`）并列出冲突名称，不做覆盖、不做半途而废 |

### 部署建议

- 本服务默认使用 **HTTP**，局域网内**密码是明文传输**的。若对安全性要求高：
  - 只在内网/可信局域网使用；或
  - 在前面加一层 Nginx/Caddy 做 HTTPS（自签证书即可）
- 不要把端口直接映射到公网
- 根目录建议使用专用目录（默认 `D:\Share`），不要直接共享整个系统盘
- 定期修改密码；不再需要时删除 `FIRST_RUN_PASSWORD.txt`

---

## 十一、接口一览

所有接口都在 `/api` 下，除标注外均需登录。

### 认证
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/login` | 登录（公开），返回会话 Cookie 与 CSRF 令牌 |
| POST | `/api/auth/logout` | 注销 |
| GET | `/api/auth/status` | 查询登录状态（公开，带 `#login` 的登录页会用它自动跳转） |

### 系统
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/system/info` | 桌面初始化元信息（根目录、功能开关、版本号、服务器 IP） |
| POST | `/api/system/soffice/rescan` | 重新探测 LibreOffice |
| POST | `/api/system/wallpaper` | 上传自定义壁纸（原始请求体 + `?filename=`） |
| POST | `/api/system/wallpaper/reset` | 恢复默认壁纸 |

### 虚拟桌面（快捷方式）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/desktop/shortcuts` | 列出桌面快捷方式（含目标是否仍存在） |
| POST | `/api/desktop/shortcuts` | 新建快捷方式（右键「发送到桌面快捷方式」） |
| POST | `/api/desktop/shortcuts/rename` | 重命名快捷方式（只改显示名） |
| POST | `/api/desktop/shortcuts/delete` | 删除快捷方式（**不会删除真实文件**） |
| GET | `/api/desktop/state` | 读取上次保存的界面状态（没有时返回 `{}`，**不会 404**） |
| PUT | `/api/desktop/state` | 保存界面状态（`{"state": {...}}`，**整体覆盖**，上限 256KB） |

> 快捷方式持久化在项目目录的 `desktop_shortcuts.json`，**只属于 Web 虚拟桌面**：
> 不会往 Windows 真实桌面写任何东西，重启服务、换浏览器、清缓存都不会丢。
> 同一个「根标识 + 路径」重复创建不会产生重复项。

### 命令行（CMD）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/terminal/session` | 创建命令行会话（需 CSRF 令牌）。请求体可带 `cols`/`rows`；返回 `id`、`backend`（`conpty`/`pipe`）、`cols`、`rows` |
| WS | `/api/terminal/ws?sid=…` | 双向流。客户端发 `input`（原始按键流）、`resize`（`cols`/`rows`，真 TTY 下生效）、`interrupt`（`force:true` = 杀子进程保 shell）、`close`；服务端回 `output`（原始 VT 流）、`exit`、`error`、`interrupted` |

> WebSocket **同样需要登录**：安全中间件对 `websocket` scope 也校验会话 Cookie 与
> `Origin` 同源。这一点很关键 —— 浏览器发起 WS 握手时会自动带上 Cookie，
> 若像早先那样「非 http 一律放行」，就等于把 shell 暴露给局域网内任何人。
> 会话 ID 为随机串并与当前会话令牌绑定，无法枚举或跨会话复用。

### 文件
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/fs/roots` | 根目录列表（含容量） |
| GET | `/api/fs/list` | 列目录（`root`、`path`、`sort`、`order`、`show_hidden`） |
| POST | `/api/fs/mkdir` | 新建文件夹 |
| POST | `/api/fs/rename` | 重命名 |
| POST | `/api/fs/delete` | 删除（默认回收站，`permanent:true` 强制永久删除） |
| POST | `/api/fs/copy` | 复制（`root` / `paths` / `target_root` / `target_path`，重名自动改名） |
| POST | `/api/fs/move` | 移动（参数同上；跨盘时自动走「复制 + 删除」） |
| POST | `/api/fs/upload` | 上传（**原始请求体** + `?root=&path=&filename=`，流式落盘） |
| POST | `/api/fs/zip` | 打包，返回一次性下载令牌 |
| GET | `/api/fs/zip/download` | 用令牌下载 zip |
| POST | `/api/fs/compress` | 压缩成 zip/tar/7z/rar 并**存到服务端目录**（不是下载；重名自动改名） |
| POST | `/api/fs/extract` | 解压 zip/tar/7z/rar（目标有同名项时返回 `409` 并整体拒绝） |

### 内容与预览
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/fs/raw` | 输出文件原始内容（**支持 Range**；`download=1` 作为附件下载） |
| GET | `/api/fs/thumb` | 图片缩略图（JPEG，100×100，服务端缓存） |
| GET | `/api/fs/text` | 文本预览（自动识别编码，超长截断） |
| POST | `/api/fs/text` | **保存文本**（全文 + 编码/BOM/换行符 + 并发凭据；详见下） |
| GET | `/api/fs/office` | Office 预览（返回 `pdf` / `html` / `unsupported` 三种模式） |
| GET | `/api/fs/office/pdf` | 输出转换后的 PDF（供 pdf.js 使用） |

> `POST /api/fs/text` 的请求体是**全文**，不是补丁：
> `{root, path, text, encoding, bom, newline, base_mtime, base_size}`。
> `text` 必须是 **LF 归一化**的，服务端按 `encoding` / `bom` / `newline` 还原原始字节形态。
> `base_mtime` / `base_size` 是打开时从 `GET` 拿到的那两个值（并发凭据）：
> 磁盘现状与之不符时返回 **`409` 且不写任何内容**，客户端必须重新载入而不是重试。
> 其余失败码：`400` 非文本/编码不支持/文件过大不宜在线编辑、`403` 只读根目录、
> `404` 文件已不存在、`413` 提交内容超出上限。

> 上传接口刻意没有使用 `multipart/form-data`：Starlette 解析 multipart 时会先把整个文件
> 落到系统临时目录再让我们拷贝一次，上传 2GB 要多占 2GB 的 C 盘空间和多一倍磁盘 IO。
> 改成原始请求体后可以边收边写目标文件，零额外空间占用。

---

## 十二、常见问题 FAQ

**Q1：局域网其他电脑打不开？**
1. 确认服务监听 `0.0.0.0`（`config.json` 的 `server.host`）
2. 放行防火墙端口（见[第八节](#八放行防火墙端口)）
3. 确认用的是服务器**局域网 IP**，不是 `127.0.0.1`
4. 在服务器本机先试 `http://127.0.0.1:8000`，能开说明服务正常，问题在网络/防火墙

**Q2：端口被占用启动失败？**
换端口：`python app.py --port 9000`，并同步放行新端口的防火墙。
查看占用：`netstat -ano | findstr :8000`

**Q3：视频拖动进度条没反应？**
后端已实现 HTTP Range（返回 206）。若仍不能拖动，通常是**该视频编码浏览器不支持**
（如 MKV 容器、H.265）；可以下载后用本地播放器打开。

**Q4：doc/xls/ppt 提示无法预览？**
本机没装 LibreOffice。老格式只有 LibreOffice 能转换，装上即可（见[第九节](#九office-文档预览可选安装-libreoffice)）。
`docx/xlsx/pptx` 不受影响，会走内置解析器降级预览。

**Q5：删除报错 / 没进回收站？**
目标盘不支持回收站（网络驱动器、非 NTFS 分区），或服务以 SYSTEM 身份运行时回收站不可用。
把 `config.json` 里 `delete.use_recycle_bin` 改成 `false` 使用永久删除。

**Q6：中文文件名乱码？**
服务全程使用 UTF-8 并做 URL 编码，中文名下载时用 RFC 5987 的 `filename*` 传递。
若仍有问题，请确认不是第三方代理/下载工具改写了响应头。

**Q7：上传大文件失败？**
1. 确认不超过 `upload.max_file_size_mb`
2. 确认目标磁盘剩余空间充足（服务会在开始前用 `Content-Length` 预检）
3. 若前面套了 Nginx，需要放开 `client_max_body_size`

**Q8：如何完全离线部署？**
`static/vendor/` 已经内置了 winbox.js、pdf.js、xterm.js 与 CodeMirror（含各语言 mode），
把整个项目目录拷到目标机器，
用 `pip download -r requirements.txt -d wheels` 在有网机器下载依赖后离线 `pip install` 即可。

**Q9：登录页一直显示锁屏？**
这是刻意的 Windows 行为：先显示大时钟，点击任意位置或按任意键才出现密码框。
在地址栏 URL 后加 `#login` 可直达密码输入框（方便收藏）。

**Q10：忘记密码了？**
```
python tools\gen_password.py --set --random
```
然后重启服务。

**Q10b：在命令行里敲 `python` 会不会卡住？**

不会。早先终端用的是**管道**而不是真终端，裸 `python` 检测到 stdin 不是终端，
会改成「把 stdin 当脚本读」，于是你后面敲的每一行都被它吞掉、提示符再也不回来，
看起来就像卡死。现在后端用 **ConPTY（pywinpty）** 提供了真正的 TTY，
裸 `python` 会正常进入交互式解释器。

如果某台机器上没装 `pywinpty`（会自动退回管道模式），那就别用裸 `python`，
改用 `python -c "..."` 或 `python 脚本.py` —— 这两个不读 stdin，不会卡。

真卡住了怎么办：按 `Ctrl+C`（原生中断）；或用工具栏的**「中断」**按钮，
它会杀掉卡住的子进程但保留 shell（当前目录还在）。

**Q11：命令提示符窗口能关掉吗？**

能。把 `config.json` 里的 `terminal.enabled` 改成 `false` 再重启服务，
开始菜单与桌面右键里的入口就会消失，`POST /api/terminal/session` 也会返回 403。

**Q12：复制/移动几十 GB 时界面像卡住了？**

这是预期行为：复制/移动和「打包下载」一样是同步执行的，请求会一直等到做完。
期间界面会显示忙碌状态，请不要重复点击。后续如需改成后台任务+实时进度，
需要新增任务队列与进度查询接口。

**Q13：前面挂了 Nginx，登录失败锁定好像不太对？**

需要在 `config.json` 的 `auth.trusted_proxies` 里填上代理所在机器的 IP
（例如 `["127.0.0.1"]` 或 `["10.0.0.0/8"]`）。默认是空数组，含义是
**完全不采信 `X-Forwarded-For`**：这时如果所有请求都来自代理，
锁定就会按代理那一个 IP 计数，可能把所有人一起锁掉。
反过来说，没有可信代理时千万**不要**随便填这个字段 ——
`X-Forwarded-For` 是客户端可以随意伪造的，早先无条件采信它时，
攻击者换个伪造值就能让失败计数归零、完全绕过锁定。

---

## 十三、已知限制

- **未实现**：文件搜索、批量重命名、**回收站查看与还原**（删除虽会进系统回收站，但 Web 界面里看不到已删除项，需到 Windows 回收站手动还原）、多用户与权限分级、操作审计日志、HTTPS（建议由反向代理承担）
  - （「在线解压」此前列在这里，现在已经实现了，见「压缩与解压」一节）
- **复制/移动是同步执行的**：选中体积非常大（数十 GB）时请求会等待较久，与「打包下载」是同一个问题
- **拖动不能拖到真实桌面**：只能在 Web 虚拟桌面内部拖动（浏览器安全限制，无法绕过）
- 命令行窗口现在是**真正的终端**（ConPTY + xterm.js）：裸 `python`、Tab 补全、`Ctrl+C` 中断、中文输入法都可用。若目标机器没装 `pywinpty`，会自动退回管道模式，届时这些交互能力都会缺失（窗口内会明确提示）
- 全屏类程序（如 `vim`）在 ConPTY 下通常可用，但**没有逐一验证**；`shell` 配成 PowerShell 的分支也未实测
- **`root` 目录既不能删除也不能整个打包**（`paths: [""]` 会被 403 拒绝），这是有意为之
- Office 降级预览只提取文字与表格，**不还原版式、图片和图表**；`.doc/.xls/.ppt` 老格式必须装 LibreOffice
- 拖拽上传依赖浏览器能力，**不支持拖拽整个文件夹**（会忽略目录）
- 缩略图仅支持浏览器/Pillow 能解码的图片格式（不含 SVG、HEIC 视 Pillow 支持情况）
- 打包下载为同步生成，选中体积非常大时（数十 GB）请求会等待较久
- 会话为无状态签名令牌，**修改密码不会让已登录的浏览器立即掉线**，需等会话超时

关于**压缩与解压**：
- **不支持加密（带密码）压缩包**：会明确提示「需要密码才能解压」，但确实解不开；创建时也不能设密码
- **RAR 依赖 WinRAR**：没装 WinRAR 时创建/解压 RAR 会返回 501 并说明原因，其余格式不受影响（7z 走纯 Python 的 `py7zr`，不需要外部程序）
- 压缩与解压都是**同步**执行的，大包会让请求等待较久（与「打包下载」同一个问题）
- 打包时**不跟进符号链接与目录联接**，被跳过的项会在结果里列出；解压时压缩包内的链接条目同样被跳过
- 解压**不会覆盖**已有同名项（整体拒绝并列出冲突名），这是刻意的

关于**会话持久**：
- 分离的会话**仍然占用** `terminal.max_sessions` 名额（它确实还占着一个 shell）。上限满时，一个「没有客户端连着且已闲置 `evict_grace_seconds`」的旧会话会被新会话顶掉，所以不会把自己锁死；但长期堆积仍会占用系统资源
- 分离期间输出超过 `terminal.max_output_kb` 的部分会被丢弃（重连时前端会提示「有输出被截断」）
- 窗口布局恢复是**尽力而为**：目录已被删除、盘符未挂载、状态文件损坏等情况会退化为「跳过该窗口」或「用默认布局」，不会因此打不开桌面
- `user_state.json` 是**单用户**的（与快捷方式一样，没有多用户隔离）；如果多人共用同一份配置，会互相覆盖布局

关于**文本编辑**：
- **混合换行的文件会被统一**：编辑器只记录并沿用「文件中**第一个**换行符的风格」（确定、可复现），
  所以一个既有 CRLF 又有 LF 的文件保存后，会全部变成首行那种风格。不是数据丢失，但 diff 会变大，
  对 `.bat` 这类对 CRLF 敏感的文件尤其明显
- **过大的文件不能就地编辑**：预览只读取开头 `preview.text_max_kb`（默认 2MB）那一段，
  为避免「保存时把后面的内容整段删掉」，超出这个大小的文件会**明确拒绝编辑**，
  请走「下载 → 本地编辑 → 上传」
- **不会覆盖别人的修改**：保存时会带上你打开那一刻的文件时间与大小，若磁盘上已被改动，
  保存会被**拒绝并提示重新载入**，而不是静默覆盖
- 编辑器窗口纳入会话持久化，但**只记录路径与位置，不还原未保存的内容** ——
  重新打开页面时是重新从磁盘载入，未保存的编辑会丢失（这是刻意的：把旧缓冲写回磁盘更危险）
- 支持语法高亮的扩展名有限；未覆盖到的扩展名以纯文本打开（功能仍可用，只是没有高亮）

---

## 开发与测试

```bat
:: 前端资源缺失时重新下载（构建期用一次，不影响离线部署）
python tools\fetch_vendor.py

:: 启动（开发模式，改后端代码自动重载）
python app.py --reload --log-level debug

:: 跑回归测试（只用标准库，不需要额外安装 pytest / httpx）
.venv\Scripts\python.exe -m unittest discover -s tests -t . -v
```

前端为纯原生 ES Module，浏览器直接加载，**无需任何构建步骤**。
后端共 6 个路由模块（认证 / 系统 / 文件 / 内容 / 虚拟桌面 / 命令行），可按需扩展。

测试覆盖的都是「曾经真实出过问题」的点：路径穿越防护（含能骗过字符串层检查的
`....//` 写法）、`X-Forwarded-For` 伪造绕过登录锁定、根目录误删、缩略图的像素上限
与缓存上限、Office 转换抛异常时必须仍能降级预览等。改动这些地方前请先跑一遍。

> 测试会自己起一个 `python app.py` 子进程（随机端口 + 临时配置），只暴露一个临时
> 目录，不会碰到真实磁盘，跑完自动清理。之所以要用真实入口而不是在进程内构造
> `uvicorn.Config`，是因为 `proxy_headers` 这类参数的默认值本身就是被测试对象。

---

## 技术栈

| 层 | 选型 |
|---|---|
| Web 框架 | FastAPI 0.141 + Starlette + Pydantic v2 |
| ASGI 服务器 | uvicorn |
| 图片处理 | Pillow |
| 回收站 | send2trash |
| 窗口系统 | winbox.js 0.2.82（本地内置） |
| PDF 渲染 | pdf.js 6.3.289（本地内置，含中文 CMaps） |
| 终端 | pywinpty 3.0.5（ConPTY，提供真 TTY）+ xterm.js 6.0.0（本地内置，含 IME 中文输入） |
| 文本编辑 | CodeMirror 5.65.21（本地内置，编辑器窗口的语法高亮） |
| 前端 | 原生 HTML / CSS / ES Module，零依赖构建 |
