// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: Apache-2.0

// Where plugins and dependencies come from. With CODEARTIFACT_MAVEN_URL set (CI, see
// scripts/smithy-generate.sh), a pull-through cache of Maven Central is the ONLY repository:
// Gradle does not fall through to another repository on HTTP 429/5xx, only on 404, so a
// fallback listed after Maven Central would not survive its per-IP rate limits. Unset
// (local builds, forks) -> the Gradle Plugin Portal and Maven Central, as before.
pluginManagement {
    val proxyUrl = providers.environmentVariable("CODEARTIFACT_MAVEN_URL").orNull
    repositories {
        if (proxyUrl != null) {
            maven {
                url = uri(proxyUrl)
                credentials {
                    username = "aws"
                    password = providers.environmentVariable("CODEARTIFACT_AUTH_TOKEN").get()
                }
            }
        } else {
            gradlePluginPortal()
        }
    }
}

dependencyResolutionManagement {
    // Settings repositories win even if a plugin adds its own to the project.
    repositoriesMode.set(RepositoriesMode.PREFER_SETTINGS)
    val proxyUrl = providers.environmentVariable("CODEARTIFACT_MAVEN_URL").orNull
    repositories {
        if (proxyUrl != null) {
            maven {
                url = uri(proxyUrl)
                credentials {
                    username = "aws"
                    password = providers.environmentVariable("CODEARTIFACT_AUTH_TOKEN").get()
                }
            }
        } else {
            mavenCentral()
        }
    }
}

// Was the directory name by default; the projection paths (build/smithyprojections/models/...)
// depend on it, so pin it now that a settings file exists.
rootProject.name = "models"
