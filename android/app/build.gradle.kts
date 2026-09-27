plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}

// Release signing: set ROOMLOG_KEYSTORE (path), ROOMLOG_KEYSTORE_PASSWORD, ROOMLOG_KEY_ALIAS and
// ROOMLOG_KEY_PASSWORD to sign with a real key (the release workflow does this from secrets when
// they exist). Without them the release build is debug-signed so it stays installable by sideload.
val releaseKeystore: String? = System.getenv("ROOMLOG_KEYSTORE")?.takeIf { it.isNotBlank() }

android {
    namespace = "no.nyta.roomlog.spike"
    compileSdk = 35
    buildToolsVersion = "35.0.0"

    defaultConfig {
        applicationId = "no.nyta.roomlog.spike"
        minSdk = 29
        targetSdk = 35
        versionCode = System.getenv("ROOMLOG_VERSION_CODE")?.toIntOrNull() ?: 4
        versionName = System.getenv("ROOMLOG_VERSION")?.takeIf { it.isNotBlank() } ?: "0.2.0-p3"
    }

    signingConfigs {
        if (releaseKeystore != null) {
            create("release") {
                storeFile = file(releaseKeystore)
                storePassword = System.getenv("ROOMLOG_KEYSTORE_PASSWORD")
                keyAlias = System.getenv("ROOMLOG_KEY_ALIAS")
                keyPassword = System.getenv("ROOMLOG_KEY_PASSWORD")
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            signingConfig = if (releaseKeystore != null) signingConfigs.getByName("release")
                            else signingConfigs.getByName("debug")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        compose = true
    }
}

dependencies {
    implementation(project(":core"))

    implementation(platform("androidx.compose:compose-bom:2024.12.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
}
