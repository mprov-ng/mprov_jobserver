# The job server as a portable Python tree, for node images (#309): a
# python-build-standalone CPython with the job server and its dependencies
# installed in it.  image-update unpacks it at /usr/libexec/mprov in each image
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
    mkdir -p /usr/libexec/mprov; \
    tar xzf /tmp/python.tar.gz -C /usr/libexec/mprov --strip-components=1; \
    rm /tmp/python.tar.gz
COPY dist/ /tmp/dist/
COPY container/requirements.txt /tmp/container-requirements.txt
COPY container/install_jobserver.sh /tmp/install_jobserver.sh
# python-build-standalone was built with clang, which its sysconfig names as
# the compiler for extensions; build pyparted and netifaces with EL8's gcc.
RUN CC=gcc CXX=g++ PIP="/usr/libexec/mprov/bin/python3 -m pip" sh /tmp/install_jobserver.sh
# Fail here, not on a node, if something the job server or the baked node
# scripts import didn't build (mprov_scripts#86).
RUN /usr/libexec/mprov/bin/python3 -c 'import mprov_jobserver.main, mprov_jobserver.script, parted, netifaces, pyroute2, sh, yaml, requests, jinja2, psutil'
# MPROV_BUNDLE_VERSION says which job server is in it; image-update only
# unpacks a bundle of its own version.
RUN set -e; \
    v=$(/usr/libexec/mprov/bin/python3 -c 'from importlib.metadata import version; print(version("mprov_jobserver"))'); \
    echo "$v" > /usr/libexec/mprov/MPROV_BUNDLE_VERSION; \
    mkdir /out; \
    tar czf "/out/mprov-jobserver-${v}-x86_64.tar.gz" -C /usr/libexec/mprov .

# Just the bundle tarball, for the release to attach for job servers
# installed with pip: `docker buildx build --target bundle-export
# --output type=local,dest=<dir> .` (ci_build.yml).
FROM scratch AS bundle-export
COPY --from=bundle /out/ /

# The job server image, on Rocky Linux 9 (#111).  Its dnf and rpm build
# the node images and sync the repos, so its OS version matters: a Rocky 8
# rpm can only read a Rocky 9 image's sqlite rpm database.
# Use the full URL to avoid confusion with podman builds.
FROM docker.io/rockylinux/rockylinux:9.8

# For every dnf command below: give up on a mirror that stalls for 15
# seconds or crawls below 50 kB/s and move on to the next one, instead of
# waiting on it, and retry dropped connections.
RUN printf '%s\n' 'timeout=15' 'minrate=50k' 'retries=10' >> /etc/dnf/dnf.conf

# dnf-plugins-core for config-manager here and reposync in repo-update
RUN dnf install -y epel-release dnf-plugins-core

# Use EPEL's main server instead of the mirror list, which sometimes hands
# out mirrors with bad certificates or stale metadata and fails the build.
# EL9's EPEL also ships Cisco's openh264 repo, which has no such server to
# point at and nothing we use: turn it off.
RUN dnf config-manager --set-disabled epel-cisco-openh264 && \
    sed -i -e 's|^metalink=|#metalink=|' \
        -e 's|^#baseurl=https://download.example/|baseurl=https://dl.fedoraproject.org/|' \
        /etc/yum.repos.d/epel*.repo

# CRB is EL9's name for EL8's PowerTools
RUN dnf config-manager --set-enabled crb

# the dnsmasq module builds iPXE from source at start-up (git, make, gcc,
# and mkisofs from genisoimage)
RUN dnf groupinstall -y "Development Tools"

# Update the system and install what the job modules run.  No Python
# packages: the job server runs on its own Python, below.
RUN dnf update -y && \
    dnf install -y \
    git \
    wget \
    iproute \
    dnsmasq \
    tcpdump \
    ipmitool \
    which \
    parted \
    genisoimage \
    procps \
    rsync \
    cpio \
    infiniband-diags

# guestfs-tools reads cloud disk images (qcow2, raw) as image bases (#279),
# in a small VM of its own with this kernel; image-update caches that VM's
# appliance in /var/lib/mprov/guestfs.  Without its weak dependencies:
# they'd add 2GB (linux-firmware, Java, GTK and more) the VM doesn't need.
RUN dnf install -y --setopt=install_weak_deps=False guestfs-tools

# The job server runs from the same bundle the node images get (#309,
# #111): one Python for the job server, the same in the container as on
# the nodes, and none of the image's own.  EL9's /usr/bin/python3 is the
# platform Python that dnf needs; nothing here uses it.  The tarball stays
# in /usr/share/mprov/ for image-update to unpack into images.
COPY --from=bundle /out/ /usr/share/mprov/
RUN set -e; \
    mkdir -p /usr/libexec/mprov; \
    tar xzf /usr/share/mprov/mprov-jobserver-*-x86_64.tar.gz -C /usr/libexec/mprov; \
    ln -s /usr/libexec/mprov/bin/mprov_jobserver /usr/local/bin/mprov_jobserver; \
    test "$(readlink -f /usr/local/bin/mprov_jobserver)" = /usr/libexec/mprov/bin/mprov_jobserver; \
    /usr/libexec/mprov/bin/python3 -c 'import mprov_jobserver.main'

# The config the container ships: its template and the plugin configs,
# kept pristine in /usr/share/mprov/config.  /etc/mprov is a volume shared
# with the mPCC (mprov-compose's mprov-conf), which Docker seeds only while
# it's empty, so it ships empty here and start_server.sh brings it up to
# date from this copy on every start (#334, mprov-ng/mprov-compose#21).
RUN set -e; \
    mkdir -p /etc/mprov /usr/share/mprov/config/plugins; \
    chmod 700 /etc/mprov; \
    cp /usr/libexec/mprov/lib/python3.*/site-packages/mprov_jobserver/plugins/*.yaml /usr/share/mprov/config/plugins/
COPY jobserver.yaml.j2 container/shipped-config.sha256 /usr/share/mprov/config/

COPY start_server.sh /
RUN chmod 755 /start_server.sh

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