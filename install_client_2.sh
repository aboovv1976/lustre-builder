#/bin/bash
set -x

FAILED=0
function enable_lnet_at_boot_time {
  # Update lnet service to start with correct config and enable at boot time
  lnet_service_config="/usr/lib/systemd/system/lnet.service"
  cp $lnet_service_config $lnet_service_config.backup
  sed -i 's/^ExecStart=\/usr\/sbin\/lnetctl net add --net.*//g' $lnet_service_config
  search_string="ExecStart=/usr/sbin/lnetctl import /etc/lnet.conf"
  nic_add="ExecStart=/usr/sbin/lnetctl net add --net tcp1 --if $interface  –peer-timeout 180 –peer-credits 128 –credits 1024"

  sed -i "s|$search_string|#$search_string\n$nic_add|g" $lnet_service_config
  # To comment ConditionPathExists clause
  sed -i "s|ConditionPathExists=\!/proc/sys/lnet/|#ConditionPathExists=\!/proc/sys/lnet/|g" $lnet_service_config

  systemctl status lnet
  systemctl enable lnet
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

##############
# Start of script execution
#############

disable_selinux
disable_firewall

mgs_fqdn_hostname_nic1=$1
fs_type=Persistent
uname -a

# ensure the change before reboot is effective (should be unlimited)
ulimit -l
uname -a
getenforce
modprobe lnet
lnetctl lnet configure
lnetctl net show

# On Client nodes, use the 1st VNIC only.
privateIp=`curl -s http://169.254.169.254/opc/v1/vnics/ | jq '.[0].privateIp ' | sed 's/"//g' ` ; echo $privateIp
interface=`ip addr |egrep "inet $privateIp|BROADCAST" | grep -B 1 "inet $privateIp" | grep BROADCAST | cut -f2 -d: | cut -f1 -d'@'`

lnetctl net add --net tcp1 --if $interface  –peer-timeout 180 –peer-credits 128 –credits 1024 | FAILED=1

lnetctl net show --net tcp > tcp.yaml
lnetctl  import --del tcp.yaml
lctl list_nids

  # Add logic to ensure the below is not empty
    cmd=`nslookup ${mgs_fqdn_hostname_nic1} | grep -qi "Name:"`
    while [ $? -ne 0 ];
    do
      echo "Waiting for nslookup..."
      sleep 10s
      cmd=`nslookup ${mgs_fqdn_hostname_nic1} | grep -qi "Name:"`
    done



mgs_ip=`nslookup ${mgs_fqdn_hostname_nic1} | grep "Address: " | gawk '{ print $2 }'` ; echo $mgs_ip
if [ -z $mgs_ip ]; then
  exit 1;
fi


  if [ -n "$2" ]; then
    fsname=$2
  else
    fsname=lfs-oci
  fi

function mount_lustrefs() {
#    echo "sleep - 100s"
#    sleep 100s
    mounted_fs=`df -k | grep "^${mgs_ip}@tcp1:/$fsname "`
    if [ -z "$mounted_fs" ]
    then
	    echo
        echo "mount -t lustre ${mgs_ip}@tcp1:/$fsname $mount_point"
        mount -t lustre ${mgs_ip}@tcp1:/$fsname $mount_point || FAILED=1
    fi
}


mount_point=/mnt/fs
mkdir -p $mount_point
mount_lustrefs

## Update fstab
fstab_fs=`grep "^${mgs_ip}@tcp1:/$fsname " /etc/fstab`
if [ -z "$fstab_fs" ]
then
    cp /etc/fstab /etc/fstab.backup
    echo "${mgs_ip}@tcp1:/$fsname  $mount_point lustre defaults,_netdev,x-systemd.automount,x-systemd.requires=lnet.service 0 0" >> /etc/fstab
#    sudo chown -R opc:opc $mount_point
fi

df -h


# function call
enable_lnet_at_boot_time


if [ "$FAILED" -eq "1" ]
then
    exit 1
fi

echo "complete"

