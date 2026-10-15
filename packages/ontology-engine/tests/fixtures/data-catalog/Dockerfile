# Build context: this directory
#   docker build packages/ontology-engine/tests/fixtures/data-catalog
# Pin to digest to prevent supply-chain attacks via tag mutation. The pin is for
# supply-chain integrity only, NOT patch currency — see the dnf upgrade below.
# amazonlinux:2023-minimal, the same base and digest as the ontology-engine image
# (not Debian python:3.12-slim, whose OS findings are mostly unfixed upstream).
FROM --platform=linux/amd64 public.ecr.aws/amazonlinux/amazonlinux:2023-minimal@sha256:66f0e66270106f31586a8dd6d0b1019a7e55b9a841ac3f04fb8b72fa735831a8

WORKDIR /app

# Apply AL2023 security updates on top of the pinned base. shadow-utils provides useradd.
# python3.12 comes from the AL2023 repos; `python`/`pip` are symlinked because
# the pip step below calls them unversioned. `--releasever=latest` is what makes
# the upgrade effective: AL2023 locks dnf to the repo snapshot the base image
# was cut from.
RUN dnf upgrade -y --releasever=latest && \
    dnf install -y --releasever=latest python3.12 python3.12-pip shadow-utils && \
    ln -sf /usr/bin/python3.12 /usr/local/bin/python && \
    ln -sf /usr/bin/python3.12 /usr/local/bin/python3 && \
    ln -sf /usr/bin/pip3.12 /usr/local/bin/pip && \
    dnf clean all && rm -rf /var/cache/dnf

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as non-root user for defense-in-depth, even in test fixtures.
RUN useradd --no-create-home --shell /bin/false appuser
USER appuser

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
