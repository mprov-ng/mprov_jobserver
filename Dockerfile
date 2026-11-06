# The job server as a portable Python tree, for node images (#309): a
# python-build-standalone CPython with the job server and its dependencies
# installed in it.  image-update unpacks it at /usr/lib/mprov in each image
# it builds, instead of pip-installing onto the image's own Python.
#
# Its own stage, on EL8 whatever the image below is based on (#111): the
# compiled extensions (pyparted, netifaces) built here need only EL8's
# glibc, so the bundle runs on EL8 and EL9 nodes.
FROM docker.io/rockylinux/rockylinux:8 AS bundle
# parted-devel is in PowerTools on EL8
RUN printf '%s\n' 'timeout=15' 'minrate=50k' 'retries=10' >> /etc/dnf/dnf.conf && \
    dnf -y --enablerepo=powertools install gcc make parted-devel pkgconf-pkg-config tar gzip && \
    dnf -y clean all
COPY container/python-bundle.lock /tmp/python-bundle.lock
# Unpacked at its final path: pip writes absolute shebangs into bin/.
RUN set -e; \
    . /tmp/python-bundle.lock; \
    curl -fsSL --retry 5 -o /tmp/python.tar.gz \
        "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/cpython-${PBS_PYTHON}+${PBS_RELEASE}-x86_64-unknown-linux-gnu-install_only.tar.gz"; \
    echo "${PBS_SHA256}  /tmp/python.tar.gz" | sha256sum -c -; \
    mkdir -p /usr/lib/mprov; \
    tar xzf /tmp/python.tar.gz -C /usr/lib/mprov --strip-components=1; \
    rm /tmp/python.tar.gz
COPY dist/ /tmp/dist/
COPY container/requirements.txt /tmp/container-requirements.txt
COPY container/install_jobserver.sh /tmp/install_jobserver.sh
# python-build-standalone was built with clang, which its sysconfig names as
# the compiler for extensions; build pyparted and netifaces with EL8's gcc.
RUN CC=gcc CXX=g++ PIP="/usr/lib/mprov/bin/python3 -m pip" sh /tmp/install_jobserver.sh
# Fail here, not on a node, if something the job server or the baked node
# scripts import didn't build (mprov_scripts#86).
RUN /usr/lib/mprov/bin/python3 -c 'import mprov_jobserver.main, mprov_jobserver.script, parted, netifaces, pyroute2, sh, yaml, requests, jinja2, psutil'
# MPROV_BUNDLE_VERSION says which job server is in it; image-update only
# unpacks a bundle of its own version.
RUN set -e; \
    v=$(/usr/lib/mprov/bin/python3 -c 'from importlib.metadata import version; print(version("mprov_jobserver"))'); \
    echo "$v" > /usr/lib/mprov/MPROV_BUNDLE_VERSION; \
    mkdir /out; \
    tar czf "/out/mprov-jobserver-${v}-x86_64.tar.gz" -C /usr/lib/mprov .

# Just the bundle tarball, for the release to attach for job servers
# installed with pip: `docker buildx build --target bundle-export
# --output type=local,dest=<dir> .` (ci_build.yml).
FROM scratch AS bundle-export
COPY --from=bundle /out/ /

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
# Install the job server from the wheel in dist/, with the exact dependency
# versions for this image (#166).  install_jobserver.sh says what happens
# with no wheel or several (#320, #322).
COPY dist/ /tmp/dist/
COPY container/requirements.txt /tmp/container-requirements.txt
COPY container/install_jobserver.sh /tmp/install_jobserver.sh
RUN PIP=pip3 sh /tmp/install_jobserver.sh

# The node-image bundle from the first stage (#309).
COPY --from=bundle /out/ /usr/share/mprov/

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