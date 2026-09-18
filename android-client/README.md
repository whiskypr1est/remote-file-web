# 远程桌面 · 安卓平板办公客户端

把 `F:\远程仿真桌面` 那个「远程文件管理」虚拟桌面装进安卓平板，配合**蓝牙鼠标键盘**
当办公桌面用。

```
平板（App） ──http://<服务器IP>:8000──▶  Windows 上的本服务
```

---

## 一、它是什么：一个 WebView 壳，不是原生应用

App 本身**不实现任何业务**。它只做一件事：用 WebView 加载那台服务器上的网页，
并补上网页在浏览器之外拿不到的那几样能力。

**为什么不做原生重写**：网页那套界面（窗口、任务栏、右键菜单、双击进入、
`Ctrl+C/V`）本来就是照「鼠标 + 键盘」写的，而办公场景正好是平板 + 蓝牙鼠标键盘 ——
**它已经是对的形式**，不需要重做 UI。原生重写等于再写一个前端，而且要在两个
地方各维护一份，与「不重复实现一遍」的原则冲突。

**对网页工程的影响：0 行。** 所有补丁都在这个目录里。

---

## 二、壳必须自己实现的东西（不写就会静默失败）

这几条是 WebView 与浏览器的差别所在，也是这个目录存在的全部理由：

| 能力 | 不做的后果 | 在哪实现 |
|---|---|---|
| `onShowFileChooser` | 网页里所有 `<input type=file>`（照片上传、「选择照片…」、资源管理器上传）**点了毫无反应** | `MainActivity` |
| `DownloadListener` | 网页里的「下载 / 打包下载」**点了毫无反应** | `MainActivity` |
| 下载要带会话 Cookie | ★ DownloadManager 跑在另一个进程、**拿不到 WebView 的 Cookie**，不带就 401 | `handleDownload()` |
| 中文文件名 | 服务端的 `Content-Disposition` 对中文走的是 `filename*=UTF-8''…`，只认 `filename="…"` 会拿到兜底英文名 | `fileNameFromDisposition()` |
| 明文 HTTP 许可 | Android 9+ 默认禁止明文流量，不开 **http:// 局域网地址根本连不上** | `AndroidManifest.xml` |
| 服务器地址配置 | 首次打开不知道连哪 | `showServerDialog()` |
| 返回键 | 一按就退出 App | `handleBack()` |

另外两个体验上的决定：

- **全屏**（隐掉状态栏）：桌面多出一条。时间不会丢 —— 网页任务栏托盘里本来就有
  实时时钟与日期。
- **返回键**：先回退网页历史 → 再关掉桌面上**最前面**的那个窗口 → 都没有了才退出。
  关窗口那段是**壳这边注入的一小段 JS**（读 winbox 的行内 `z-index` 找最前面的窗口），
  同样没有落到网页代码里。

---

## 三、环境要求

| 项 | 要求 |
|---|---|
| 安卓版本 | **7.0+**（minSdk 24） |
| Android System WebView | **≥ 88**（前端用了 CSS `aspect-ratio`；WebView 由 Play 商店单独更新，与安卓版本无关） |
| 网络 | 平板与服务器**在同一个局域网** |
| 外设 | 蓝牙鼠标 + 键盘（推荐；触摸也能用，但桌面隐喻本来是为指针设计的） |

> ⚠️ **不要把服务器地址填成公网地址。** 本服务在局域网里走的是**明文 HTTP**、
> 口令是明文传的；而且 `config.json` 里 `terminal.enabled` 默认为 true ——
> 那等于**一台 Windows 机器的真 shell**。要外网访问请走 VPN 或反代 + HTTPS。

---

## 四、构建

用的是 `F:\安卓测试` 里那套**已验证过的离线自包含工具链**
（JDK 17 + Gradle 8.13 + Android SDK 36 + 已填充的依赖缓存）。

```powershell
cd F:\远程仿真桌面\android-client
$env:JAVA_HOME        = 'C:\Java\jdk-17.0.19+10'
$env:GRADLE_USER_HOME = 'F:\安卓测试\gradle-home'
$env:ANDROID_HOME     = 'F:\安卓测试\android-sdk'

& 'F:\安卓测试\tools\gradle-8.13\bin\gradle.bat' assembleDebug --no-daemon
```

产物：

```
android-client\app\build\outputs\apk\debug\app-debug.apk
```

### 两个别踩的坑（都是 `F:\安卓测试\README.md` 里记过的）

1. **`buildToolsVersion = "36.0.0"` 不要删。** AGP 8.13.2 默认想要 35.0.0，
   会去 `dl.google.com` 下载 —— 而本机访问 Google 源不稳定，构建会**长时间卡在**
   `Preparing "Install Android SDK Build-Tools 35"` 无响应。
2. **`android.overridePathCheck=true` 不要删。** 本工程路径含中文
   （`F:\远程仿真桌面\…`），AGP 默认会因此直接拒绝构建。
   万一 aapt2 仍报错，退路是 `subst X: "F:\远程仿真桌面"` 用盘符映射出纯 ASCII 路径。

---

## 五、跑一遍纯逻辑用例（不需要平板）

```powershell
cd F:\远程仿真桌面\android-client
$out = "$env:TEMP\rd-check"
& 'C:\Java\jdk-17.0.19+10\bin\javac.exe' -encoding UTF-8 -d $out `
    'app\src\main\java\com\fileweb\desktop\ServerAddress.java' `
    'app\src\main\java\com\fileweb\desktop\DownloadName.java' `
    'tools\CheckPureLogic.java'
& 'C:\Java\jdk-17.0.19+10\bin\java.exe' -Dfile.encoding=UTF-8 -cp $out CheckPureLogic
```

共 30 条，覆盖两段**出错也不吭声**的逻辑：

- **服务器地址规范化**（`ServerAddress`）：只填 `192.168.1.100` 也要能连上；
  非法输入（端口写成 `abc`、只有 `http://`）要**拒绝**而不是猜 ——
  猜错的症状是「连不上服务器」，人会先去怀疑网络和服务，很难查到是解析的锅。
- **下载文件名**（`DownloadName`）：中文名走 RFC 5987 的
  `filename*=UTF-8''…`（本服务就是这么发的），落盘名不能带路径分隔符。

★ 这两段刻意**不引用任何 `android.*`**，所以能在电脑上用 JDK 直接跑 ——
测的是**与 App 里同一份实现**，不是在测试里重写一遍（网页工程里
`phototime.js` 是同一个理由）。

---

## 六、安装

**方式一：数据线 + adb**

```powershell
& 'F:\安卓测试\android-sdk\platform-tools\adb.exe' install -r `
  'F:\远程仿真桌面\android-client\app\build\outputs\apk\debug\app-debug.apk'
```

**方式二：拷贝 APK 到平板**，用文件管理器点击安装（需允许「安装未知来源应用」）。

> 换了签名（比如以后换成自己的 release keystore）**必须先卸载旧版**再装。
> 当前 release 与 debug 都复用 `F:\安卓测试\tools\debug.keystore`，仅供内部测试。

---

## 七、使用

1. 首次打开：填服务器地址。**只填 IP 就行**（例如 `192.168.1.100`），
   App 会自动补成 `http://192.168.1.100:8000/`。
2. 之后每次打开直接用上次的地址。
3. **长按「返回」键**可以随时改地址（连不上时也会自动弹出来）。
4. 下载的文件进平板的公共「下载」目录（Android 10+ 无需任何权限）。

---

## 八、已知限制

- **不做触摸手势适配**（双指缩放、长按当右键之类）。定位就是「平板 + 鼠标键盘」，
  触摸路径沿用系统默认行为；真要补，是另一个量级的活儿。
- **不保存离线内容**：所有数据都在服务器上，断网就用不了（这正是「远程桌面」的语义）。
- **登录状态**由服务端会话决定（默认 12 小时），过期后需要重新登录一次。
- 竖屏被锁成横屏（`sensorLandscape`）—— 桌面隐喻在竖屏下会挤成一团。
  想跟随重力感应，删掉清单里那一行即可。

---

## 九、目录

```
android-client/
├── settings.gradle.kts / build.gradle.kts / gradle.properties
├── local.properties            # 本机 SDK 路径（已在 .gitignore 排除）
├── tools/make_icon.py          # 生成启动图标（改配色/形状时重跑）
└── app/
    ├── build.gradle.kts        # compileSdk 36 / minSdk 24 / **零第三方依赖**
    └── src/main/
        ├── AndroidManifest.xml
        ├── java/com/fileweb/desktop/MainActivity.java   # 壳的全部逻辑
        └── res/                # 地址对话框布局、字符串、主题、五档图标
```

**为什么一个第三方依赖都不引**（连 appcompat 都不）：这个壳只用
`android.webkit.* / android.app.* / android.widget.*`，都是系统自带的。

1. 构建**完全不碰 Maven 解析** —— 本机 `dl.google.com` 不通，少一层依赖少一处卡点；
2. APK 只有几十 KB，而带 appcompat + material 的模板是 6MB，局域网分发省事；
3. 不会出现 AndroidX 版本冲突。

代价是设置界面用系统控件手搭（没有 Material 主题）。想要更漂亮的界面时，
在 `app/build.gradle.kts` 里按需加依赖即可 —— 镜像已经配好了。
