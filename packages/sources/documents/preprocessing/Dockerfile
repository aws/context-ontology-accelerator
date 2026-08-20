# Pin to digest to prevent supply-chain attacks via tag mutation.
# public.ecr.aws/lambda/python:3.12 as of 2026-05-13
FROM --platform=linux/amd64 public.ecr.aws/lambda/python:3.12@sha256:5fda6fa581d6876d9382c51fe7ed3ce1ecd0569bc1672be217082cf5c35e1242

# System deps: poppler for PDF text extraction, file-libs for libmagic (file type detection)
RUN dnf install -y poppler-utils file-libs && dnf clean all

# Install shared common library (changes less often -> better layer caching)
COPY libs/common/ /tmp/common/
RUN pip install --no-cache-dir --root-user-action=ignore /tmp/common/ && rm -rf /tmp/common/

# CPU-only PyTorch — unstructured[pdf] pulls in torch via unstructured-inference
# (requires torch>=2.10.0). Without constraints, pip installs the full CUDA
# variant (~4 GB nvidia-* packages). Pre-install CPU torch, then constrain the
# requirements install so pip can't upgrade it.
COPY packages/sources/documents/preprocessing/constraints.txt /tmp/constraints.txt
RUN pip install --no-cache-dir --root-user-action=ignore \
    torch==2.11.0+cpu \
    --extra-index-url https://download.pytorch.org/whl/cpu

# Install remaining deps (constrained to keep CPU torch), then:
#  - Swap opencv-python → headless (unstructured pulls in opencv which needs libGL, absent in Lambda)
#  - Ensure spaCy is present (newer unstructured releases no longer pin it as a hard
#    dependency, so install it explicitly before downloading the model — otherwise
#    `spacy download` fails with "No module named spacy" under a constrained install)
#  - Pre-install spaCy model (unstructured loads it at runtime but Lambda fs is read-only)
COPY packages/sources/documents/preprocessing/requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir --root-user-action=ignore \
    -c /tmp/constraints.txt \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    -r ${LAMBDA_TASK_ROOT}/requirements.txt && \
    pip uninstall -y opencv-python 2>/dev/null; \
    pip install --no-cache-dir --root-user-action=ignore opencv-python-headless && \
    pip install --no-cache-dir --root-user-action=ignore spacy && \
    python -m spacy download en_core_web_sm

# Install the documents package so handler can be imported as a module
COPY smithy-generated/control-plane-python-server/ /tmp/server/
RUN pip install --no-cache-dir --root-user-action=ignore /tmp/server/ && rm -rf /tmp/server/
COPY packages/sources/ /tmp/sources/
RUN pip install --no-cache-dir --root-user-action=ignore /tmp/sources/ && rm -rf /tmp/sources/

# numba's @njit(cache=True) needs a writable cache dir (/tmp is writable on Lambda)
ENV NUMBA_CACHE_DIR=/tmp/numba_cache
# Suppress matplotlib/fontconfig warnings about read-only home dir
ENV MPLCONFIGDIR=/tmp/matplotlib FONTCONFIG_PATH=/tmp/fonts

USER 1000
CMD ["coa_sources.documents.preprocessing.handler.handler"]
