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


ENTRYPOINT ["/start_server.sh", "${MPROV_ARGS}"]
