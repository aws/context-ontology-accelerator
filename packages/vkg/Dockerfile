# VKG Translation Service - Ontop 5.x, translation-only mode
# Uses Amazon Corretto 17 on AL2023 (public ECR, no Docker Hub dependency, actively patched)
# Digest re-resolved from the 17-al2023 tag on 2026-09-01 to clear AL2023 CVEs
# Inspector flagged against the prior build; the `yum update --security` below
# runs against current AL2023 repos once this layer rebuilds. Renovate keeps
# this digest current (dockerfile manager + pinDigests, held to the 17 line).
FROM public.ecr.aws/amazoncorretto/amazoncorretto:17-al2023@sha256:90617ef1a52a0b0db24613311b69cc75b4e71dcb7349ea1f67651220ba90091b

ARG ONTOP_VERSION=5.5.0
ARG ONTOP_SHA256=430dff312e68e8ad41d26e6113160ca2365c28c4fe911926193000a33299458f
ARG H2_VERSION=2.2.224
ARG H2_SHA256=b9d8f19358ada82a4f6eb5b174c6cfe320a375b5a9cb5a4fe456d623e6e55497

# Install dependencies and Ontop CLI
COPY requirements.txt /tmp/requirements.txt
RUN yum install -y python3 python3-pip unzip shadow-utils findutils && \
  yum update -y --security && \
  pip3 install --no-cache-dir -r /tmp/requirements.txt && \
  curl -fsSL "https://github.com/ontop/ontop/releases/download/ontop-${ONTOP_VERSION}/ontop-cli-${ONTOP_VERSION}.zip" \
  -o /tmp/ontop.zip && \
  echo "${ONTOP_SHA256}  /tmp/ontop.zip" | sha256sum -c - && \
  unzip /tmp/ontop.zip -d /opt/ontop && \
  rm /tmp/ontop.zip && \
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
