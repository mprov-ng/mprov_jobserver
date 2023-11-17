#!/bin/bash

# check your python 3 version.
which python3 > /dev/null
if [ "$?" != "0" ]
then    
    echo "Error: Either python 3 is not installed or no alternatives setup for python3 binary." >&2
    exit 1
fi

pyvers=`python3 --version| awk '{print $2}' `
if [ "$pyvers" != "3.8" ]
then
        pyvershi=`echo -e "3.8\n$pyvers" |  sort -Vr | head -n1 `
        if [ "$pyvershi" == "3.8" ]
        then
                # we don't have python 3.8
                echo "Error: You don't seem to have at least python 3.8.  Check your etc-alternaives and set them accordingly!" >&2
                exit 1
        fi
fi

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
dnf -y install python3 python3-pip git wget iproute dnsmasq tcpdump ipmitool which $extra_pkgs
dnf -y --enablerepo=powertools install parted-devel

extra_pip=""
pip3 --no-cache-dir install mprov_jobserver pyyaml requests jinja2 $extra_pip


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
mkdir -p /etc/mprov/
cp /usr/local/lib/python3.*/site-packages/mprov_jobserver/plugins/ /etc/mprov/ -R
mprov_jobserver -r || true


