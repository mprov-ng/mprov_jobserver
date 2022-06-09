#!/bin/bash


# time for some arg parsing.
BUILD_DOCKER=0

while [[ $# -gt 0 ]]
do
        case $1 in
                -d)
                        BUILD_DOCKER=1
                        shift
                        ;;
                *)
                        echo "Error: Unknown arg $1"
                        exit 1
                        ;;
        esac
done
  

extra_pkgs=""

# make sure epel is installed before we try to do anything else.
dnf -y install epel-release
dnf -y install python38 python38-pip python38-pyyaml python38-requests python38-jinja2.noarch git wget iproute dnsmasq ipxe-bootimgs tcpdump ipmitool which $extra_pkgs

extra_pip=""

pip3 install mprov_jobserver $extra_pip


if [ "$BUILD_DOCKER" != "1" ]
then
        # if we aren't building a docker image, grab the service file
        wget -O /usr/lib/systemd/system/mprov_jobserver.service https://raw.githubusercontent.com/mprov-ng/mprov_jobserver/main/mprov_jobserver.service
        systemctl daemon-reload
        systemctl enable mprov_jobserver
        systemctl restart mprov_jobserver
        firewall-cmd --zone=public --add-service=http
        firewall-cmd --zone=public --add-service=http --permanent
fi
mkdir /etc/mprov/
mprov_jobserver -r || true