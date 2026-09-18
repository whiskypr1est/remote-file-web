// ============================================================
//  app 模块构建脚本
// ============================================================
plugins {
    id("com.android.application")
}

android {
    namespace = "com.fileweb.desktop"
    compileSdk = 36

    // ★ 显式锁定 build-tools 版本，别删这一行。
    //   不指定的话 AGP 8.13 会去要 35.0.0 并从 dl.google.com 自动下载，
    //   而本机访问 Google 源不稳定 —— 构建会长时间卡在
    //   「Preparing "Install Android SDK Build-Tools 35 v.35.0.0"」无响应。
    //   锁到本机已装好的 36.0.0 后完全不再触发下载。
    buildToolsVersion = "36.0.0"

    defaultConfig {
        applicationId = "com.fileweb.desktop"
        // 与既有工程一致：Android 7.0+，覆盖在用设备
        minSdk = 24
        targetSdk = 36
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        release {
            // 与 F:\安卓测试\AndroidApp 同样的做法：先用 debug 签名便于直接安装。
            // ★ 正式对外分发前要换成自己的 release keystore（否则换签名必须先卸载旧版）。
            signingConfig = signingConfigs.getByName("debug")
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    packaging {
        resources.excludes += setOf("META-INF/*.kotlin_module")
    }
}

// ============================================================
//  ★ 刻意**一个第三方依赖都不引**（连 appcompat / material 都不用）
//
//  这个壳只用到 android.webkit.* / android.app.* / android.widget.*，
//  全部是系统自带的，不需要从 Maven 取任何东西。三个好处：
//
//    1. **构建完全不碰 Maven 解析** —— 本机 dl.google.com 不通，
//       少一层依赖就少一处可能卡住的地方（AGP 本身仍从本地缓存取）；
//    2. APK 只有几十 KB（既有模板带 appcompat+material 是 6MB），
//       局域网分发给多台平板时省事得多；
//    3. 不会出现 AndroidX 版本冲突。
//
//  代价：界面用系统控件手搭（见 res/layout/dialog_server.xml），没有 Material 主题。
//  哪天想要更漂亮的设置界面，直接在这里加 appcompat 即可 —— 镜像已经配好。
// ============================================================
dependencies {
}
