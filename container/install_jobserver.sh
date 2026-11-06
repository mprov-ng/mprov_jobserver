#!/bin/sh
# Install the job server with the pip command in $PIP (e.g. "pip3", or
# "/usr/libexec/mprov/bin/python3 -m pip"), pinned by the container's exact
# dependency versions (#166).  Used by both Dockerfile stages, so the image
# and the bundle it carries (#309) get the same job server.
#
# The wheel comes from /tmp/dist/.  CI always builds one from the checkout
# first; for a local build, run `python -m build --wheel` first.  With no
# wheel there (only dist/'s .placeholder), this installs the latest release
# from PyPI instead, which is NOT the checked-out code (#320).  With more
# than one, it fails rather than guess which one to install (#322).
set -e
: "${PIP:?set PIP to the pip command to use}"
constraints=/tmp/container-requirements.txt

# `set --` puts the glob's matches in $1..$N; with no match, $1 is the
# unexpanded pattern, which doesn't exist.
set -- /tmp/dist/*.whl
# $PIP is deliberately unquoted: it holds a whole command.
# shellcheck disable=SC2086
if [ ! -e "$1" ]; then
    echo "No wheel in dist/: installing the latest mprov_jobserver release from PyPI, NOT the checked-out code."
    $PIP --no-cache-dir install -c "$constraints" mprov_jobserver pyyaml requests jinja2
elif [ "$#" -gt 1 ]; then
    echo "dist/ has $# wheels, expected one:" "$@" >&2
    echo "Remove the old ones (rm dist/*.whl), rebuild the wheel and build again." >&2
    exit 1
else
    $PIP uninstall -y mprov-jobserver
    $PIP --no-cache-dir install -c "$constraints" "$1"
fi
