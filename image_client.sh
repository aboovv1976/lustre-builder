set -x


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

# Only client should be installed
yum  install  lustre-client  -y
if [ $? -ne 0 ]; then
  echo "yum install of lustre binaries failed"
  exit 1
fi


}

function disable_selinux {
    setenforce 0
    sed -i "s/SELINUX=enforcing/SELINUX=disabled/g" /etc/sysconfig/selinux
    sed -i "s/SELINUX=enforcing/SELINUX=disabled/g" /etc/selinux/config
}

function disable_firewall {

    systemctl stop firewalld
    systemctl disable firewalld

}

function set_params {
    echo "options ksocklnd nscheds=10 sock_timeout=100 credits=2560 peer_credits=63 enable_irq_affinity=0"  >  /etc/modprobe.d/ksocklnd.conf

echo "*          hard   memlock           unlimited
*          soft    memlock           unlimited
" >> /etc/security/limits.conf

}


function switch_kernel {

    grubby --set-default  `grubby --info=ALL | grep ^kernel | grep -v rescue |grep -v vmlinuz-5.15  | head -1 | cut -f2 -d\"` || echo "Unable to find or set the kernel to lusture kernel"

}

##########
## Start #
##########

disable_selinux
disable_firewall

# use this, since this is minimum version to support clients with UEK kernel
##install_lustre "2.13.0"
# Use this for RHCK
install_lustre "2.15.3"

set_params


switch_kernel

touch /tmp/complete
echo "complete.  rebooting now"
# Reboot happens in Ansible code, since it is capable of waiting for the node to come back and continue with rest of the commands.


##########
## End #
##########

