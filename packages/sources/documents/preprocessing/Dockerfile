# Pin to digest to prevent supply-chain attacks via tag mutation. The pin is for
# supply-chain integrity only, NOT patch currency — see the dnf upgrade below.
# public.ecr.aws/lambda/python:3.12 (digest kept current by Renovate), resolved
# from the tag on 2026-10-29.
FROM --platform=linux/amd64 public.ecr.aws/lambda/python:3.12@sha256:3c249526505c4bf83ab74be5d4749b7705798b03effcd34c205c6a77d149c77e

# Apply AL2023 security updates, then install system deps: poppler for PDF text
# extraction, file-libs for libmagic (file type detection). The Lambda base locks
# dnf to the repo snapshot it was built from, so a plain `dnf upgrade` is a no-op;
# `--releasever=latest` points it at the current AL2023 repos instead.
#
# Also delete the base image's Runtime Interface Emulator. /lambda-entrypoint.sh
# runs /usr/local/bin/aws-lambda-rie only when AWS_LAMBDA_RUNTIME_API is unset,
# i.e. when the image is started outside Lambda; in Lambda it execs
# /var/runtime/bootstrap directly, so the emulator is dead weight in the deployed
# function. Its statically linked Go stdlib and go-chi carry Critical/High CVEs,
# including some no published base digest fixes yet. To run the image locally,
# mount an emulator from the aws-lambda-runtime-interface-emulator releases at
# that path.
#
# The dnf step brings in the system python3.9 (crypto-policies-scripts needs
# /usr/bin/python3), and with it python3-setuptools 59.6.0, a weak dependency of
# python3 and python3-pip that nothing requires and the Lambda runtime (python3.12
# under /var/lang) never imports. It carries CVE-2026-59890, so it is removed in
# this same layer. python3-setuptools-wheel 59.6.0 stays (python3-libs requires it;
# it is the wheel ensurepip installs from), so a scanner may still report its
# /usr/share/python3-wheels/setuptools-59.6.0-py3-none-any.whl. The build fails if the
# RPM is still installed, or if the removal took poppler-utils or file-libs with it.
RUN dnf upgrade -y --releasever=latest && \
    dnf install -y --releasever=latest poppler-utils file-libs && \
    dnf remove -y python3-setuptools && \
    ! rpm -q python3-setuptools && rpm -q poppler-utils file-libs && \
    dnf clean all && rm -rf /var/cache/dnf && \
    rm -f /usr/local/bin/aws-lambda-rie

# Install shared common library (changes less often -> better layer caching)
COPY libs/common/ /tmp/common/
RUN pip install --no-cache-dir --root-user-action=ignore /tmp/common/ && rm -rf /tmp/common/

# CPU-only PyTorch — unstructured[pdf] pulls in torch via unstructured-inference
# (requires torch>=2.10.0). Without constraints, pip installs the full CUDA
# variant (~4 GB nvidia-* packages). Pre-install CPU torch, then constrain the
# requirements install so pip can't upgrade it. torch is also what brings in
# setuptools (it declares setuptools>=77.0.3), so both installs use the
# constraints, whose setuptools floor (CVE-2026-59890) applies where it lands.
COPY packages/sources/documents/preprocessing/constraints.txt /tmp/constraints.txt
RUN pip install --no-cache-dir --root-user-action=ignore \
    -c /tmp/constraints.txt \
    torch \
    --extra-index-url https://download.pytorch.org/whl/cpu

# Install remaining deps (constrained to keep CPU torch), then:
#  - Swap opencv-python → headless (unstructured pulls in opencv which needs libGL, absent in Lambda)
#  - Pin spaCy (unstructured depends on it) and pre-install its model (unstructured
#    loads it at runtime but Lambda fs is read-only)
#  - Only the opencv-python uninstall may fail; any other failure stops the build
#  - Lift filelock past unstructured's `filelock<4` cap to 4.2.1+ (CVE-2026-112940).
#    Every unstructured release still declares the cap, but its only use is
#    `FileLock(path, timeout=-1)` in unstructured/nlp/tokenize.py, an API 4.x keeps;
#    the CVE itself is in ReadWriteLock, which nothing here uses. `--no-deps` stops
#    pip from re-resolving against the cap, and doing it in this layer keeps the
#    3.x copy out of the image. `<5` makes a new major a deliberate bump. The check
#    builds a FileLock with the same arguments unstructured passes (`timeout=-1`)
#    and acquires/releases it, so a 4.x API change fails the build instead of the
#    first tokenize call at runtime.
#  - Delete spaCy's bundled test suite after the model download: its
#    spacy/tests/package/requirements.txt pins black==22.3.0, which scanners report as an
#    installed package (CVE-2026-31900, CVE-2026-32274) although black is never installed.
#    Nothing at runtime imports spacy.tests, and doing it in this layer keeps the file out
#    of every layer. The delete fails the build if the directory is missing or survives.
COPY packages/sources/documents/preprocessing/requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir --root-user-action=ignore \
    -c /tmp/constraints.txt \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    -r ${LAMBDA_TASK_ROOT}/requirements.txt && \
    { pip uninstall -y opencv-python 2>/dev/null || true; } && \
    pip install --no-cache-dir --root-user-action=ignore opencv-python-headless && \
    pip install --no-cache-dir --root-user-action=ignore spacy==3.8.7 && \
    python -m spacy download en_core_web_sm && \
    python -c "import os, shutil, spacy; d = os.path.join(os.path.dirname(spacy.__file__), 'tests'); shutil.rmtree(d); assert not os.path.exists(d)" && \
    pip install --no-cache-dir --root-user-action=ignore --no-deps "filelock>=4.2.1,<5" && \
    python -c "import unstructured.nlp.tokenize, filelock; lock = filelock.FileLock('/tmp/filelock-check.lock', timeout=-1); lock.acquire(); assert lock.is_locked; lock.release(); assert not lock.is_locked; print('filelock', filelock.__version__, 'OK')" && \
    rm -f /tmp/filelock-check.lock

# Install the documents package so handler can be imported as a module
COPY smithy-generated/control-plane-python-server/ /tmp/server/
RUN pip install --no-cache-dir --root-user-action=ignore /tmp/server/ && rm -rf /tmp/server/
COPY packages/sources/ /tmp/sources/
# Install the documents package, then upgrade pip to clear its own advisories.
# setuptools >= 83 (the constraints' floor) has no pkg_resources, which setuptools 82
# removed, so the build then imports the unstructured partitioners the handler loads
# lazily: a dependency that imports pkg_resources at module level fails the build
# instead of the first invocation. The later installs here don't use the
# constraints, so the build also checks the setuptools floor still holds.
# msgpack is not installed here.
RUN pip install --no-cache-dir --root-user-action=ignore /tmp/sources/ && rm -rf /tmp/sources/ && \
    pip install --no-cache-dir --root-user-action=ignore --upgrade pip && \
    python -c "import unstructured.partition.auto, unstructured.partition.pdf, unstructured.partition.docx" && \
    python -c "import setuptools; assert int(setuptools.__version__.split('.')[0]) >= 83, setuptools.__version__"

# numba's @njit(cache=True) needs a writable cache dir (/tmp is writable on Lambda)
ENV NUMBA_CACHE_DIR=/tmp/numba_cache
# Suppress matplotlib/fontconfig warnings about read-only home dir
ENV MPLCONFIGDIR=/tmp/matplotlib FONTCONFIG_PATH=/tmp/fonts

USER 1000

# Fail the build if the code this image runs imports anything the image cannot (scripts/image_import_check.py).
# llama_index is optional: coa_common.embeddings imports it lazily, for kg-build and serve only.
COPY scripts/image_import_check.py /tmp/
RUN python /tmp/image_import_check.py coa_sources.documents.preprocessing.handler --optional llama_index
CMD ["coa_sources.documents.preprocessing.handler.handler"]
