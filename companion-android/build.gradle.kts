// Versions are deliberately explicit rather than "latest": a floating version
// would make the first real failure harder to read than it already will be.
//
// WHY THESE THREE MOVE TOGETHER. Google Play refuses an upload that does not
// meet its target API rule - API 35 today, API 36 from 2026-08-31, with no
// exemption for internal testing tracks. Reaching compileSdk 36 needs AGP 8.11,
// which needs Gradle 8.13, which comes with Kotlin 2.x expectations. None of
// these can be bumped alone.
//
// Kotlin 2.0 MOVED THE COMPOSE COMPILER out of AGP's `composeOptions` and into
// its own Gradle plugin, so `kotlinCompilerExtensionVersion` is gone from
// app/build.gradle.kts and `org.jetbrains.kotlin.plugin.compose` appears here.
// Leaving the old setting in place is not an error that fails loudly - it is
// simply ignored, and the Compose compiler silently is not applied.
plugins {
    id("com.android.application") version "8.11.1" apply false
    id("org.jetbrains.kotlin.android") version "2.1.20" apply false
    id("org.jetbrains.kotlin.plugin.compose") version "2.1.20" apply false
}
