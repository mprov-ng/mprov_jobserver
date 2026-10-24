# Runs the test suite in the jobserver image, so the tests use the same
# Rocky build, Python and dnsmasq that ship.  Build from the repository
# root, after building the jobserver image:
#
#   docker build -f tests/Dockerfile --build-arg JOBSERVER_IMAGE=<image> -t mprov-jobserver-tests .
#   docker run --rm mprov-jobserver-tests
ARG JOBSERVER_IMAGE=ghcr.io/mprov-ng/mprov-jobserver:dev
FROM ${JOBSERVER_IMAGE}

# genders for nodeattr, which checks the genders files libgenders writes.
RUN dnf -y install genders && dnf -y clean all && \
    pip3 --no-cache-dir install pytest

COPY tests/ /tests/
# test_repack_image.py runs the repo-root script at ../repack_image relative
# to /tests, so ship it at the filesystem root like start_server.sh.
COPY repack_image /repack_image
WORKDIR /tests
ENTRYPOINT ["python3", "-m", "pytest", "-v", "-p", "no:cacheprovider", "/tests"]
