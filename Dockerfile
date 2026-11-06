# Start with Rocky Linux 8.8 as the base image
# Use the full URL to avoid confusion with podman builds.
FROM docker.io/rockylinux/rockylinux:8.8

# For every dnf command below: give up on a mirror that stalls for 15
# seconds or crawls below 50 kB/s and move on to the next one, instead of
# waiting on it, and retry dropped connections.
RUN printf '%s\n' 'timeout=15' 'minrate=50k' 'retries=10' >> /etc/dnf/dnf.conf

# install epel repo
RUN dnf install -y epel-release

# Use EPEL's main server instead of the mirror list, which sometimes hands
# out mirrors with bad certificates or stale metadata and fails the build.
RUN sed -i -e 's|^metalink=|#metalink=|' \
        -e 's|^#baseurl=https://download.example/|baseurl=https://dl.fedoraproject.org/|' \
        /etc/yum.repos.d/epel*.repo

# enable powertools repo
RUN dnf config-manager --enable powertools

# we need dev tools
RUN dnf groupinstall -y "Development Tools"

# Update system and install necessary dependencies
RUN dnf update -y && \
    dnf install -y \   
    python38 \
    python38-devel \
    python38-pip \
    git \
    wget \
    iproute \
    dnsmasq \
    tcpdump \
    ipmitool \
    which \
    parted-devel \
    gcc \
    genisoimage \
    procps

# install the mprov_jobserver python module and some other useful mods
RUN dnf -y install python3.11-pip python3.11-devel
# Install the job server from the wheel in dist/.  CI always builds one from
# the checkout first (ci_build.yml, dockerimage-dev.yml); for a local build,
# run `python -m build --wheel` first.  With no wheel in dist/ (only its
# .placeholder), this installs the latest release from PyPI instead, which
# is NOT the checked-out code (#320).  With more than one wheel in dist/,
# the build fails rather than guess which one to install (#322).
COPY dist/ /tmp/dist/
# the exact dependency versions for this image (#166)
COPY container/requirements.txt /tmp/container-requirements.txt
# `set --` puts the glob's matches in $1..$N; with no match, $1 is the
# unexpanded pattern, which doesn't exist.
RUN set -e; \
    set -- /tmp/dist/*.whl; \
    if [ ! -e "$1" ]; then \
        echo "No wheel in dist/: installing the latest mprov_jobserver release from PyPI, NOT the checked-out code."; \
        pip3 --no-cache-dir install -c /tmp/container-requirements.txt mprov_jobserver pyyaml requests jinja2; \
    elif [ "$#" -gt 1 ]; then \
        echo "dist/ has $# wheels, expected one:" "$@" >&2; \
        echo "Remove the old ones (rm dist/*.whl), rebuild the wheel and build again." >&2; \
        exit 1; \
    else \
        pip3 uninstall -y mprov-jobserver; \
        pip3 install -c /tmp/container-requirements.txt "$1"; \
    fi

RUN mkdir -p /etc/mprov && \
    chmod 700 /etc/mprov/ && \
    chown root:root /etc/mprov && \
    cp /usr/local/lib/python3.*/site-packages/mprov_jobserver/plugins/ /etc/mprov/ -R

COPY start_server.sh /
RUN chmod 755 /start_server.sh

COPY jobserver.yaml.j2 /etc/mprov/

# # setup for a systemd container
# RUN (cd /lib/systemd/system/sysinit.target.wants/; for i in *; do [ $i == \
# systemd-tmpfiles-setup.service ] || rm -f $i; done); \
# rm -f /lib/systemd/system/multi-user.target.wants/*;\
# rm -f /etc/systemd/system/*.wants/*;\
# rm -f /lib/systemd/system/local-fs.target.wants/*; \
# rm -f /lib/systemd/system/sockets.target.wants/*udev*; \
# rm -f /lib/systemd/system/sockets.target.wants/*initctl*; \
# rm -f /lib/systemd/system/basic.target.wants/*;\
# rm -f /lib/systemd/system/anaconda.target.wants/*;

# COPY mprov_jobserver.service /etc/systemd/system
# # COPY systemd-system.conf /etc/systemd/system.conf
# RUN systemctl enable mprov_jobserver

# # for cgroup access to the host.
# VOLUME [ "/sys/fs/cgroup" ]

# make the lib dir and chown it to dnsmasq.
RUN mkdir -p /var/lib/dnsmasq
RUN chown dnsmasq:dnsmasq /var/lib/dnsmasq

# clean up yum/dnf
RUN dnf -y clean all
COPY wait-for-it.sh /
RUN chmod 755 /wait-for-it.sh

EXPOSE 69/udp
EXPOSE 67/udp
EXPOSE 53/udp
EXPOSE 8080/tcp

# CMD [ "/usr/sbin/init" ]
# Exec form, so start_server.sh is PID 1 and the job server it execs gets
# the container's signals (#294).  No argument: exec form does no shell
# expansion, so "${MPROV_ARGS}" was passed through as that literal string;
# start_server.sh reads MPROV_ARGS from the environment itself.
ENTRYPOINT ["/start_server.sh"]
#STOPSIGNAL SIGRTMIN+3
#CMD [ "/sbin/init" ] 
#["/bin/bash", "-c", "exec /sbin/init --log-target=journal"]