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
if [ -e /etc/redhat-release ]
then
        ver=`cat /etc/redhat-release | awk '{print $4}'`
        echo -e "9\n${ver}" | sort -C -V
        if [ "$?" != "0" ]
        then
		dnf config-manager --enable powertools
        else
  		dnf config-manager --enable crb
        fi
fi
# make sure epel is installed before we try to do anything else.
dnf -y install epel-release
dnf -y install python3 python3-pip git wget iproute dnsmasq tcpdump ipmitool which $extra_pkgs parted-devel

extra_pip=""
pip3 --no-cache-dir install mprov_jobserver pyyaml requests jinja2 $extra_pip

# the files below ship in the package we just installed, wherever pip3 put
# it (python3.8 on Rocky 8, python3.9 on Rocky 9, lib or lib64)
pkgLocation=`pip3 show mprov_jobserver 2>/dev/null | awk '/^Location:/{print $2}'`
pkgDir="$pkgLocation/mprov_jobserver"
if [ -z "$pkgLocation" ] || [ ! -d "$pkgDir/plugins" ]
then
        echo "Error: unable to find the installed mprov_jobserver package (looked in '$pkgDir')." >&2
        exit 1
fi


if [ "$BUILD_DOCKER" != "1" ]
then
        # if we aren't building a docker image, install the service file
        # that matches the version just installed: from the package, or,
        # for a release from before it shipped there, from that release's
        # tag.  It was fetched from the tip of main on GitHub (#165).
        unit=/usr/lib/systemd/system/mprov_jobserver.service
        if [ -e "$pkgDir/systemd/mprov_jobserver.service" ]
        then
                install -m 644 "$pkgDir/systemd/mprov_jobserver.service" $unit
        else
                pkgVersion=`pip3 show mprov_jobserver 2>/dev/null | awk '/^Version:/{print $2}'`
                wget -O $unit "https://raw.githubusercontent.com/mprov-ng/mprov_jobserver/v${pkgVersion}/mprov_jobserver.service" && chmod 644 $unit
        fi
        if [ "$?" != "0" ] || [ ! -s $unit ]
        then
                echo "Error: unable to install the mprov_jobserver service file." >&2
                # wget -O leaves an empty file behind on a 404
                rm -f $unit
                exit 1
        fi
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
if ! cp "$pkgDir/plugins/" /etc/mprov/ -R
then
        echo "Error: unable to copy the plugin configs from $pkgDir/plugins." >&2
        exit 1
fi
mprov_jobserver -r || true


