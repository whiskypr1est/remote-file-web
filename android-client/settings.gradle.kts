// ============================================================
//  项目设置 —— 与 F:\安卓测试\AndroidApp 用同一套国内镜像
//  本机 **无法访问 dl.google.com**，所以 Maven 必须走阿里云：
//    * androidx / AGP 只在阿里云 google 仓库里有，
//      Maven Central 上没有 —— 所以它必须排在第一位。
// ============================================================

pluginManagement {
    repositories {
        maven {
            name = "AliyunGoogle"
            url = uri("https://maven.aliyun.com/repository/google")
        }
        maven {
            name = "AliyunGradlePlugin"
            url = uri("https://maven.aliyun.com/repository/gradle-plugin")
        }
        maven {
            name = "AliyunPublic"
            url = uri("https://maven.aliyun.com/repository/public")
        }
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        maven {
            name = "AliyunGoogle"
            url = uri("https://maven.aliyun.com/repository/google")
        }
        maven {
            name = "AliyunPublic"
            url = uri("https://maven.aliyun.com/repository/public")
        }
        mavenCentral()
    }
}

rootProject.name = "RemoteDesktop"
include(":app")
