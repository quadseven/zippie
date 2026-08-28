import java.io.File

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    // Kotlin 2.0 moved the Compose compiler out of AGP into this plugin. Without
    // it Compose is not compiled at all - and the old `composeOptions` setting it
    // replaces is IGNORED rather than rejected, so forgetting this fails as a
    // pile of unresolved @Composable errors rather than as a missing plugin.
    id("org.jetbrains.kotlin.plugin.compose")
}

// -----------------------------------------------------------------------------
// RELEASE SIGNING AND VERSION NUMBERING COME FROM THE ENVIRONMENT.
//
// There is no keystore, password or alias in this repository and there must
// never be one. An Android signing key is a permanent identity: lose it and an
// installed app can never be updated again, leak it and anyone can publish an
// app the phone will happily install over this one. CI stages the keystore in a
// temp directory OUTSIDE the checkout and passes it in through these variables.
// companion-android/README.md, "Release builds and signing", has the ceremony.
//
//   ZIPPIE_KEYSTORE_PATH      absolute path to the keystore, outside this repo
//   ZIPPIE_KEYSTORE_PASSWORD  store password
//   ZIPPIE_KEY_ALIAS          key alias inside the store
//   ZIPPIE_KEY_PASSWORD       key password
//   ZIPPIE_VERSION_CODE       integer, must not go backwards (CI: commit count)
//   ZIPPIE_VERSION_LABEL      appended to versionName, e.g. 341-0ce0f7f-TESTKEY
//
// With NONE of them set every path that existed before still works untouched:
// `testDebugUnitTest` and `assembleDebug` never read a signing config. Only
// release PACKAGING requires them, and it fails loudly rather than emitting an
// unsigned APK - see the task guard at the bottom of this file.
// -----------------------------------------------------------------------------

/** The part of the version name that is a human decision, not a build fact. */
val zippieVersionBase = "0.1.0"

fun requiredEnv(name: String): String {
    val value = System.getenv(name)
    if (value.isNullOrBlank()) {
        throw GradleException(
            "$name must be set when ZIPPIE_KEYSTORE_PATH is set. " +
                "See companion-android/README.md, 'Release builds and signing'."
        )
    }
    return value
}

// versionCode is the ONE number Android uses to decide whether an install is an
// upgrade. A build with a code lower than the installed one is refused
// (INSTALL_FAILED_VERSION_DOWNGRADE), and a build with the SAME code is also
// refused unless it is byte-identical - so a hardcoded 1 on every build, which
// is what this file carried until there was a release pipeline, means no build
// can ever be installed over another. CI derives it from the commit count of
// the ref being built, which only moves forward along main.
val zippieVersionCode: Int = System.getenv("ZIPPIE_VERSION_CODE").let { raw ->
    if (raw.isNullOrBlank()) {
        // Local builds. Deliberately the lowest possible code so that a hand
        // build can never sit above a CI build and block it.
        1
    } else {
        val parsed = raw.toIntOrNull()
            ?: throw GradleException("ZIPPIE_VERSION_CODE is '$raw', which is not an integer")
        if (parsed < 1 || parsed > 2_100_000_000) {
            throw GradleException("ZIPPIE_VERSION_CODE is $parsed, outside Android's 1..2100000000")
        }
        parsed
    }
}

// The label is what makes an APK self-identifying on the phone: Settings > Apps
// shows this string, so a build signed with a throwaway CI key says TESTKEY
// there rather than looking exactly like a real one.
val zippieVersionLabel: String = System.getenv("ZIPPIE_VERSION_LABEL").orEmpty().also {
    if (it.isNotEmpty() && !Regex("^[A-Za-z0-9._-]+$").matches(it)) {
        throw GradleException("ZIPPIE_VERSION_LABEL is '$it'; allowed: letters, digits, dot, underscore, dash")
    }
}

val zippieVersionName: String =
    if (zippieVersionLabel.isEmpty()) zippieVersionBase else "$zippieVersionBase-$zippieVersionLabel"

val zippieKeystoreFile: File? =
    System.getenv("ZIPPIE_KEYSTORE_PATH")?.takeIf { it.isNotBlank() }?.let { path ->
        val resolved = File(path).canonicalFile
        if (!resolved.isFile) {
            throw GradleException("ZIPPIE_KEYSTORE_PATH points at $resolved, which is not a file")
        }
        // REFUSE A KEYSTORE INSIDE THE CHECKOUT, rather than trusting .gitignore
        // to catch it. A .gitignore entry is one `git add -f` or one renamed
        // file away from being wrong, and a signing key committed once is
        // committed forever. Compared by path component (kotlin.io startsWith),
        // not string prefix, so a sibling directory named like the repo with a
        // suffix is not mistaken for being inside it.
        if (resolved.startsWith(rootDir.canonicalFile)) {
            throw GradleException(
                "the keystore at $resolved is inside the checkout at ${rootDir.canonicalFile}. " +
                    "Keep signing keys outside the repository."
            )
        }
        resolved
    }

android {
    namespace = "app.zippie.companion"
    compileSdk = 36

    defaultConfig {
        applicationId = "app.zippie.companion"
        minSdk = 29          // VpnService.Builder.setMetered and per-network
                             // binding both want Q; below that the cellular
                             // pinning this design rests on gets unreliable.
        // API 36 because Google Play requires it for new apps and updates from
        // 2026-08-31, with NO exemption for internal testing tracks. See #140.
        targetSdk = 36
        versionCode = zippieVersionCode
        versionName = zippieVersionName
    }

    signingConfigs {
        if (zippieKeystoreFile != null) {
            create("release") {
                storeFile = zippieKeystoreFile
                storePassword = requiredEnv("ZIPPIE_KEYSTORE_PASSWORD")
                keyAlias = requiredEnv("ZIPPIE_KEY_ALIAS")
                keyPassword = requiredEnv("ZIPPIE_KEY_PASSWORD")
                // The v1/v2/v3 scheme choice is deliberately NOT set here.
                // apksigner already picks by minSdk - at 29 it signs v2/v3 and
                // skips the v1 JAR signature, which is why the debug APK has no
                // v1 signature either. Pinning it here would be a second,
                // divergable source of truth for the same decision.
            }
        }
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            if (zippieKeystoreFile != null) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    buildFeatures { compose = true }
}

// AN UNSIGNED RELEASE APK IS NOT A FAILURE TO AGP - IT IS AN OUTPUT.
// With no signing config, `assembleRelease` succeeds and writes
// app-release-unsigned.apk, which no phone will install. That is a green build
// producing a thing that cannot be used, so make it a red build instead. The
// hook is on the packaging tasks rather than at configuration time on purpose:
// a configuration-time throw would break `testDebugUnitTest` too, and the unit
// test job must keep working with no keystore anywhere near it.
tasks.matching { it.name == "packageRelease" || it.name == "bundleRelease" }.configureEach {
    doFirst {
        if (zippieKeystoreFile == null) {
            throw GradleException(
                "release packaging needs a signing key: set ZIPPIE_KEYSTORE_PATH, " +
                    "ZIPPIE_KEYSTORE_PASSWORD, ZIPPIE_KEY_ALIAS and ZIPPIE_KEY_PASSWORD. " +
                    "companion-android/ci/build-signed-apk.sh does this for CI; " +
                    "companion-android/README.md has the ceremony for a real key."
            )
        }
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.activity:activity-compose:1.9.1")
    implementation(platform("androidx.compose:compose-bom:2025.06.00"))
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.foundation:foundation")
    // DECLARED, not inherited. animateFloatAsState arrives transitively through
    // foundation and material3 today, so the traffic bar's one authored
    // animation compiles without this line - right up until a Compose release
    // demotes that edge to `implementation` and the build breaks somewhere that
    // says nothing about animation. A direct use gets a direct dependency.
    implementation("androidx.compose.animation:animation-core")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    // collectAsStateWithLifecycle: the status poll must stop when the screen is
    // not visible. A phone mounted on a dashboard would otherwise poll the
    // router forever from a backgrounded activity.
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.4")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.4")

    // The gomobile-built datapath. Absent until #2246 produces it, which is
    // why client mode is a skeleton and the relay path is not.
    // implementation(files("libs/zippie.aar"))

    testImplementation("junit:junit:4.13.2")
    // org.json ships INSIDE android.jar as method stubs that throw, so a unit
    // test of the decoder would fail with "Stub!" rather than with an
    // assertion. This puts a real implementation on the unit-test classpath;
    // it is not shipped in the APK.
    testImplementation("org.json:json:20240303")
}
