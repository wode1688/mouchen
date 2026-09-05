plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
    id("com.google.devtools.ksp")
}

android {
    namespace = "com.mouchen.app"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.mouchen.app"
        minSdk = 28
        targetSdk = 36
        versionCode = 11
        versionName = "0.1.0-alpha11"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        vectorDrawables.useSupportLibrary = true
        manifestPlaceholders["usesCleartextTraffic"] = "false"
        buildConfigField("String", "DEFAULT_BACKEND_URL", "\"https://example.invalid\"")
        buildConfigField("String", "PRIVATE_DNS_PINNED_HOST", "\"\"")
        buildConfigField("String", "PRIVATE_DNS_PINNED_IPV4", "\"\"")
    }

    flavorDimensions += "distribution"
    productFlavors {
        create("privateAlpha") {
            dimension = "distribution"
            applicationIdSuffix = ".alpha"
            versionNameSuffix = "-private"
            buildConfigField("boolean", "ALLOW_SENSITIVE_CAPTURE", "true")
            buildConfigField("boolean", "ALLOW_EXPERIMENTAL_SERVICES", "true")
            // Optional private-DNS pinning is intentionally blank in the public source.
            // Configure it locally only when your own deployment requires it.
            buildConfigField("String", "PRIVATE_DNS_PINNED_HOST", "\"\"")
            buildConfigField("String", "PRIVATE_DNS_PINNED_IPV4", "\"\"")
            manifestPlaceholders["usesCleartextTraffic"] = "false"
            resValue("string", "app_name", "My AI Twin Private Alpha")
        }
        create("playRelease") {
            dimension = "distribution"
            applicationIdSuffix = ".play"
            versionNameSuffix = "-play"
            buildConfigField("boolean", "ALLOW_SENSITIVE_CAPTURE", "false")
            buildConfigField("boolean", "ALLOW_EXPERIMENTAL_SERVICES", "false")
            resValue("string", "app_name", "My AI Twin")
        }
    }

    buildTypes {
        debug {
            applicationIdSuffix = ".debug"
        }
        release {
            isMinifyEnabled = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions.jvmTarget = "17"
    packaging.resources.excludes += setOf(
        "/META-INF/{AL2.0,LGPL2.1}",
        "/META-INF/LICENSE.md",
        "/META-INF/NOTICE.md",
    )
}

dependencies {
    val composeBom = platform("androidx.compose:compose-bom:2025.05.01")
    implementation(composeBom)
    androidTestImplementation(composeBom)

    implementation("androidx.core:core-ktx:1.16.0")
    implementation("androidx.activity:activity-compose:1.10.1")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.9.0")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.9.0")
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.ui:ui-tooling-preview")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.work:work-runtime-ktx:2.10.1")
    implementation("androidx.room:room-runtime:2.7.1")
    implementation("androidx.room:room-ktx:2.7.1")
    ksp("androidx.room:room-compiler:2.7.1")
    implementation("androidx.sqlite:sqlite:2.5.1")
    implementation("net.zetetic:sqlcipher-android:4.6.1")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation("com.sun.mail:android-mail:1.6.7")
    implementation("com.sun.mail:android-activation:1.6.7")
    // Bundled models keep private-alpha screen OCR fully on-device and available offline.
    add("privateAlphaImplementation", "com.google.mlkit:text-recognition:16.0.1")
    add("privateAlphaImplementation", "com.google.mlkit:text-recognition-chinese:16.0.1")

    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
    androidTestImplementation("androidx.test.espresso:espresso-core:3.6.1")
    androidTestImplementation("androidx.compose.ui:ui-test-junit4")
    debugImplementation("androidx.compose.ui:ui-tooling")
    debugImplementation("androidx.compose.ui:ui-test-manifest")
}
