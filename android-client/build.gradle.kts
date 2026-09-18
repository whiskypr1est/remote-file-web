// 根构建脚本：只声明插件版本，不在这里应用。
// AGP 8.13.2 与 F:\安卓测试\AndroidApp 保持一致 —— 那个版本已经在这台机器上
// 验证过能构建成功，换版本等于重新踩一遍镜像与 build-tools 的坑。
plugins {
    id("com.android.application") version "8.13.2" apply false
}
