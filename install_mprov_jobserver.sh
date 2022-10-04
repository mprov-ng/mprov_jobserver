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
dnf -y --enablerepo=powertools install parted-devel

extra_pip=""

pip3 --no-cache-dir install mprov_jobserver $extra_pip


if [ "$BUILD_DOCKER" != "1" ]
then
        # if we aren't building a docker image, grab the service file
        wget -O /usr/lib/systemd/system/mprov_jobserver.service https://raw.githubusercontent.com/mprov-ng/mprov_jobserver/main/mprov_jobserver.service
        systemctl daemon-reload
        systemctl enable mprov_jobserver
        systemctl restart mprov_jobserver
	echo "Please remmeber!  You need to open ports in your firewall for your image server to be able to serve images!  By default, this is port 8080."
	echo "If this machine is running dnsmasq, you will also want to setup the DNS, DHCP, and TFTP stuff in your firewawll as well, if you are running one."
	echo
	echo
	echo -e "\tExample Commands:"
	echo -e "\tfirewall-cmd --zone=public --add-port=8080/tcp --permanent"
	echo -e "\tfirewall-cmd --add-service=dhcp --permanent"
	echo -e "\tfirewall-cmd --add-service=dns --permanent"
	echo -e "\tfirewall-cmd --add-service=tftp --permanent"
        echo -e "\tfirewall-cmd --reload"
        echo -e "If you are using IPv6 you will want something like:"
        echo -e "\tfirewall-cmd --add-service=dhcpv6 --permanent && firewall-cmd --reload"
fi
mkdir /etc/mprov/
mprov_jobserver -r || true

