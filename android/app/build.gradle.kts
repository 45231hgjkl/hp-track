plugins {
    id("com.android.application")
    id("com.chaquo.python")
}

// 版本号唯一来源：仓库根目录 VERSION
val appVersion: String = rootProject.file("../VERSION").readText().trim()

android {
    namespace = "dev.hptrack"
    compileSdk = 36

    defaultConfig {
        applicationId = "dev.hptrack"
        minSdk = 24
        targetSdk = 36
        versionCode = appVersion.split(".").let { (maj, min, patch) ->
            maj.toInt() * 10_000 + min.toInt() * 100 + patch.toInt()
        }
        versionName = appVersion
        ndk {
            // Chaquopy 17.2 的 Python 3.12 只提供 arm64-v8a / x86_64。
            // 只留 arm64-v8a：真机全是这个，x86_64 只在模拟器上用，外加 13 MB。
            abiFilters += listOf("arm64-v8a")
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            // 默认用 debug 签名，方便直接安装；要上架/发布再换 keystore（见 docs/ADAPTATION-PLAN.md）
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    packaging {
        // Chaquopy 的 .so 默认不压缩（Android 10+ 直读），但那是 26 MB 的代价。
        // 压缩后体积能砍一半，代价是首次安装多做一次解包。
        jniLibs {
            useLegacyPackaging = true
        }
    }
}

chaquopy {
    defaultConfig {
        version = "3.12"
        // 纯标准库实现（pure_aes），不需要任何 pip 包
        pip {
        }
    }
}

/**
 * 把仓库 src/ 下的 4 个运行时文件同步进 APK 工程，避免第二份副本漂移。
 *   ecust.py / ecust_web.py / pure_aes.py → src/main/python（Chaquo 打进 app.imy）
 *   index.html → assets/index.html（服务首次启动时复制到私有目录）
 */
val repoRoot = rootProject.projectDir.parentFile

val syncRuntime by tasks.registering(Copy::class) {
    from(File(repoRoot, "src")) {
        include("ecust.py", "ecust_web.py", "pure_aes.py")
    }
    into(layout.projectDirectory.dir("src/main/python"))
}

val syncIndexHtml by tasks.registering(Copy::class) {
    from(File(repoRoot, "src")) {
        include("index.html")
    }
    into(layout.projectDirectory.dir("src/main/assets"))
}

tasks.named("preBuild") {
    dependsOn(syncRuntime, syncIndexHtml)
}

// Gradle 9 的隐式依赖校验：Chaquo 的 merge*PythonSources 会读 src/main/python，
// 必须显式声明它依赖 syncRuntime，否则报 "uses this output without declaring dependency"。
tasks.configureEach {
    when {
        name.contains("PythonSources") -> dependsOn(syncRuntime)
        name.contains("Assets")        -> dependsOn(syncIndexHtml)
    }
}
