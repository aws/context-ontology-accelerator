# VKG Translation Service - Ontop 5.x, translation-only mode
# Uses Amazon Corretto 17 on AL2023 (public ECR, no Docker Hub dependency, actively patched)
# Digest re-resolved from the 17-al2023 tag on 2026-09-01 to clear AL2023 CVEs
# Inspector flagged against the prior build. The `yum update --security` below
# needs `--releasever=latest`: AL2023 locks yum to the base image's repo snapshot,
# so without it the update only sees fixes already in the pinned base. Renovate keeps
# this digest current (dockerfile manager + pinDigests, held to the 17 line).
FROM public.ecr.aws/amazoncorretto/amazoncorretto:17-al2023@sha256:f55661af9731100217437403877b460d5eddbe0359c08b4ecb5cea7e59e39e31

ARG ONTOP_VERSION=5.5.0
ARG ONTOP_SHA256=430dff312e68e8ad41d26e6113160ca2365c28c4fe911926193000a33299458f
ARG H2_VERSION=2.2.224
ARG H2_SHA256=b9d8f19358ada82a4f6eb5b174c6cfe320a375b5a9cb5a4fe456d623e6e55497
# Maven repository the Ontop JAR overrides are fetched from (sha256-pinned either way).
ARG MAVEN_BASE=https://repo1.maven.org/maven2

# Install dependencies and Ontop CLI.
# translate-server.py and the aws CLI run on python3.12 from the AL2023 repos, not
# the system python3 (3.9, past end of life). `python3`/`pip3` are linked to 3.12
# in /usr/local/bin, which is ahead of /usr/bin on PATH; /usr/bin/python3 itself
# must stay 3.9 because dnf/yum run on it (`#!/usr/bin/python3`). The version
# check fails the build if `python3` ever stops resolving to 3.12.
#
# ontop-lib-overrides.sh swaps the Ontop CLI's bundled Tomcat/Spring/Thymeleaf/
# Jackson/Guava JARs for the patched versions pinned in ontop-lib-overrides.txt.
# It runs in this same layer as the unzip so the replaced JARs never reach a
# lower layer that scanners would still attribute to the image.
COPY requirements.txt /tmp/requirements.txt
COPY ontop-lib-overrides.sh ontop-lib-overrides.txt /tmp/
RUN yum install -y --releasever=latest python3.12 python3.12-pip unzip shadow-utils findutils && \
  yum update -y --security --releasever=latest && \
  ln -sf /usr/bin/python3.12 /usr/local/bin/python3 && \
  ln -sf /usr/bin/pip3.12 /usr/local/bin/pip3 && \
  python3 --version | grep -q '^Python 3\.12' && \
  pip3 install --no-cache-dir -r /tmp/requirements.txt && \
  curl -fsSL "https://github.com/ontop/ontop/releases/download/ontop-${ONTOP_VERSION}/ontop-cli-${ONTOP_VERSION}.zip" \
  -o /tmp/ontop.zip && \
  echo "${ONTOP_SHA256}  /tmp/ontop.zip" | sha256sum -c - && \
  unzip /tmp/ontop.zip -d /opt/ontop && \
  rm /tmp/ontop.zip && \
  MAVEN_BASE="${MAVEN_BASE}" sh /tmp/ontop-lib-overrides.sh /tmp/ontop-lib-overrides.txt /opt/ontop/lib && \
  rm /tmp/ontop-lib-overrides.sh /tmp/ontop-lib-overrides.txt && \
  chmod +x /opt/ontop/ontop && \
  mkdir -p /opt/ontop/jdbc && \
  curl -fsSL "https://repo1.maven.org/maven2/com/h2database/h2/${H2_VERSION}/h2-${H2_VERSION}.jar" \
  -o /opt/ontop/jdbc/h2-${H2_VERSION}.jar && \
  echo "${H2_SHA256}  /opt/ontop/jdbc/h2-${H2_VERSION}.jar" | sha256sum -c - && \
  yum clean all && rm -rf /var/cache/yum

ENV PATH="/opt/ontop:${PATH}"

# Create non-root user for runtime
RUN useradd -r -s /sbin/nologin ontop && \
  mkdir -p /app/ontology && \
  chown -R ontop:ontop /app

COPY --chown=ontop:ontop entrypoint.sh /app/entrypoint.sh
COPY --chown=ontop:ontop translate-server.py /app/translate-server.py
RUN chmod +x /app/entrypoint.sh && sed -i 's/\r$//' /app/entrypoint.sh

USER ontop
WORKDIR /app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=180s --retries=5 \
  CMD curl -sf http://localhost:8080/health || exit 1

ENTRYPOINT ["/app/entrypoint.sh"]
