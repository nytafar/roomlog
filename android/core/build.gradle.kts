import org.gradle.api.tasks.testing.logging.TestExceptionFormat
import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    id("org.jetbrains.kotlin.jvm")
}

java {
    sourceCompatibility = JavaVersion.VERSION_17
    targetCompatibility = JavaVersion.VERSION_17
}

kotlin {
    compilerOptions {
        jvmTarget.set(JvmTarget.JVM_17)
    }
}

dependencies {
    testImplementation(kotlin("test-junit5"))
    testRuntimeOnly("org.junit.platform:junit-platform-launcher")
}

tasks.test {
    useJUnitPlatform()
    // Tests read contract/examples from the repo root and shell out to ffmpeg.
    systemProperty("roomlog.repoRoot", rootProject.projectDir.parentFile.absolutePath)
    testLogging {
        events("failed", "skipped")
        exceptionFormat = TestExceptionFormat.FULL
    }
}
