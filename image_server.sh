#!/bin/bash

set -x
set -e

function install_lustre {

lustre_version=$1


cat > /etc/yum.repos.d/lustre.repo << EOF
[lustre-server]
name=CentOS-lustre-server
baseurl=https://downloads.whamcloud.com/public/lustre/lustre-${lustre_version}/el8.8/server/
gpgcheck=0

[e2fsprogs-wc]
name=CentOS-lustre-e2fsprogs-wc
baseurl=https://downloads.whamcloud.com/public/e2fsprogs/latest/el8/
gpgcheck=0

[lustre-client]
name=CentOS-lustre-client
baseurl=https://downloads.whamcloud.com/public/lustre/lustre-${lustre_version}/el8.8/client/
gpgcheck=0
EOF

# Install without ZFS and just with LDISKFS

yum --nogpgcheck --disablerepo=* --enablerepo=e2fsprogs-wc install -y \
e2fsprogs

yum --nogpgcheck --disablerepo=base,extras,updates -y \
--enablerepo=lustre-server install -y \
kernel \
kernel-devel \
kernel-headers \
kernel-tools \
kernel-tools-libs

yum --nogpgcheck --enablerepo=lustre-server install -y \
kmod-lustre \
kmod-lustre-osd-ldiskfs \
lustre-osd-ldiskfs-mount \
lustre

yum install -y resource-agents

yum --nogpgcheck --enablerepo=lustre-server install -y \
lustre-resource-agents

dnf -y install oraclelinux-developer-release-el8
dnf install python36-oci-cli

}

function switch_kernel {

    grubby --set-default  `grubby --info=ALL | grep ^kernel |grep  lustre.x86_64 | cut -f2 -d\"` || echo "Unable to find or set the kernel to lusture kernel"

}

function disable_selinux {
    setenforce 0
    sed -i "s/SELINUX=enforcing/SELINUX=disabled/g" /etc/sysconfig/selinux
    sed -i "s/SELINUX=enforcing/SELINUX=disabled/g" /etc/selinux/config
}

function set_params {
    echo "options ksocklnd nscheds=10 sock_timeout=100 credits=2560 peer_credits=63 enable_irq_affinity=0"  >  /etc/modprobe.d/ksocklnd.conf
}


function disable_firewall {

    systemctl stop firewalld
    systemctl disable firewalld
}

##########
## Start #
##########

disable_selinux
disable_firewall

install_lustre "2.15.3"

set_params

switch_kernel

touch /tmp/complete
echo "complete.  reboot now"

##########
## End #
##########
