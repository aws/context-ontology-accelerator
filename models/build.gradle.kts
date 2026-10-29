// The smithy-jar plugin asks for smithy-model/build/cli as the range [1.0, 2.0[. A range
// makes Gradle fetch maven-metadata.xml from Maven Central to list versions, on every run
// without a warm cache, and CI failed whenever Maven Central rate-limited it (HTTP 429).
// Forcing the version we compile against skips that lookup and stops the build tooling
// drifting to whatever 1.x is newest.
buildscript {
    configurations.classpath {
        resolutionStrategy.force(
            "software.amazon.smithy:smithy-model:1.69.0",
            "software.amazon.smithy:smithy-build:1.69.0",
            "software.amazon.smithy:smithy-cli:1.69.0",
        )
    }
}

plugins {
    java
    id("software.amazon.smithy.gradle.smithy-jar") version "1.3.0"
}

// Repositories are declared in settings.gradle.kts (Maven Central, or the CI pull-through cache).

dependencies {
    // Smithy core
    implementation("software.amazon.smithy:smithy-model:1.69.0")
    implementation("software.amazon.smithy:smithy-aws-traits:1.69.0")

    // OpenAPI projection (for API Gateway integration + docs)
    implementation("software.amazon.smithy:smithy-openapi:1.69.0")
    implementation("software.amazon.smithy:smithy-aws-apigateway-openapi:1.69.0")

    // Native TypeScript client codegen (published on Maven Central)
    smithyBuild("software.amazon.smithy.typescript:smithy-aws-typescript-codegen:0.49.1")
}
