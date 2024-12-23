# Start with Rocky Linux 8.8 as the base image
# Use the full URL to avoid confusion with podman builds.
FROM docker.io/rockylinux/rockylinux:8.8

# install epel repo
RUN dnf install -y epel-release

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
    gcc

# install the mprov_jobserver python module and some other useful mods
RUN dnf -y install python3.11-pip python3.11-devel; pip3 --no-cache-dir install mprov_jobserver pyyaml requests jinja2

RUN mkdir -p /etc/mprov && \
    chmod 700 /etc/mprov/ && \
    chown root:root /etc/mprov && \
    cp /usr/local/lib/python3.*/site-packages/mprov_jobserver/plugins/ /etc/mprov/ -R

COPY start_server.sh /
RUN chmod 755 /start_server.sh

COPY jobserver.yaml.j2 /etc/mprov/
COPY mprov_jobserver.conf /etc/systemd/system/mprov_jobserver.service.d/mprov_jobserver.conf

# setup for a systemd container
RUN (cd /lib/systemd/system/sysinit.target.wants/; for i in *; do [ $i == \
systemd-tmpfiles-setup.service ] || rm -f $i; done); \
rm -f /lib/systemd/system/multi-user.target.wants/*;\
rm -f /etc/systemd/system/*.wants/*;\
rm -f /lib/systemd/system/local-fs.target.wants/*; \
rm -f /lib/systemd/system/sockets.target.wants/*udev*; \
rm -f /lib/systemd/system/sockets.target.wants/*initctl*; \
rm -f /lib/systemd/system/basic.target.wants/*;\
rm -f /lib/systemd/system/anaconda.target.wants/*;

COPY mprov_jobserver.service /etc/systemd/system
# COPY systemd-system.conf /etc/systemd/system.conf
RUN systemctl enable mprov_jobserver


# for cgroup access to the host.
VOLUME [ "/sys/fs/cgroup" ]

# clean up yum/dnf
RUN dnf -y clean all

# CMD [ "/usr/sbin/init" ]
#ENTRYPOINT ["/start_server.sh", "${MPROV_ARGS}"]
STOPSIGNAL SIGRTMIN+3
ENTRYPOINT [ "/sbin/init" ] 
#["/bin/bash", "-c", "exec /sbin/init --log-target=journal"]