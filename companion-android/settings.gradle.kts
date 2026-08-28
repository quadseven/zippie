pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}
dependencyResolutionManagement {
    repositories {
        google()
        mavenCentral()
        // gomobile output lands here; see README.
        flatDir { dirs("app/libs") }
    }
}

rootProject.name = "ZippieCompanion"
include(":app")
