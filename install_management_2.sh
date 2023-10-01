#!/bin/bash

set -x

FAILED=0
##############
function configure_vnics {

# Configure second vNIC
scriptsource="https://raw.githubusercontent.com/oracle/terraform-examples/master/examples/oci/connect_vcns_using_multiple_vnics/scripts/secondary_vnic_all_configure.sh"
vnicscript=/root/secondary_vnic_all_configure.sh
curl -s $scriptsource > $vnicscript
chmod +x $vnicscript
cat > /etc/systemd/system/secondnic.service << EOF
[Unit]
Description=Script to configure a secondary vNIC

[Service]
Type=oneshot
ExecStart=$vnicscript -c
ExecStop=$vnicscript -d
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target

EOF

systemctl enable secondnic.service
systemctl start secondnic.service

retry=0
while !( systemctl restart secondnic.service )
do
   # give the infrastructure another 10 seconds to provide the metadata for the second vnic
   echo waiting for second NIC to come online
   sleep 10
   retry=`expr $retry + 1`
   if [ "$retry" -ge "11" ]
   then
           FAILED=1
   fi
done

}

function enable_lnet_at_boot_time {
  # Update lnet service to start with correct config and enable at boot time
  lnet_service_config="/usr/lib/systemd/system/lnet.service"
  cp $lnet_service_config $lnet_service_config.backup
  sed -i 's/^ExecStart=\/usr\/sbin\/lnetctl net add --net.*//g' $lnet_service_config
  search_string="ExecStart=/usr/sbin/lnetctl import /etc/lnet.conf"
  nic_add="ExecStart=/usr/sbin/lnetctl net add --net tcp1 --if $interface  –peer-timeout 180 –peer-credits 128 –credits 1024"

  sed -i "s|$search_string|#$search_string\n$nic_add|g" $lnet_service_config
  # To comment ConditionPathExists clause
  sed -i "s|ConditionPathExists=!/proc/sys/lnet/|#ConditionPathExists=!/proc/sys/lnet/|g" $lnet_service_config

  systemctl status lnet
  systemctl enable lnet

}


# function
function disk_mount {

  if [ $disk_type = "nvme" ]; then
    fsname=lfsnvme
  else
    fsname=lfsbv
  fi
  mount_point="/mnt/mgs${num}_mgt${index}_${disk_type}"

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

  mgs_pri_nid=$mgs_ip@tcp1 ;  echo $mgs_pri_nid
  mkfs.lustre --fsname=$fsname --mgs $mount_device

  lctl network up
  lctl list_nids
  mkdir -p $mount_point
  mount -t lustre $mount_device $mount_point || FAILED=1

  ## Update fstab
  echo "$mount_device               $mount_point           lustre  defaults,_netdev        0 0" >> /etc/fstab
}

##############
# Start of script execution
#############

mgs_fqdn_hostname_nic1=$1
uname -a

getenforce
modprobe lnet
lnetctl lnet configure
lctl list_nids

# Secondary VNIC details
privateIp=`curl -s http://169.254.169.254/opc/v1/vnics/ | jq '.[1].privateIp ' | sed 's/"//g' ` ;
[[ -n "$privateIp" ]] && configure_vnics
[[ -z "$privateIp" ]] && privateIp=`curl -s http://169.254.169.254/opc/v1/vnics/ | jq '.[0].privateIp ' | sed 's/"//g' ` ;
interface=`ip addr |egrep "inet $privateIp|BROADCAST" | grep -B 1 "inet $privateIp" | grep BROADCAST | cut -f2 -d:`

# Configure lnet network
lnetctl net add --net tcp1 --if $interface  –peer-timeout 180 –peer-credits 128 –credits 1024 || FAILED=1


num=`hostname | gawk -F"." '{ print $1 }' | gawk -F"-"  'NF>1&&$0=$(NF)'`
hostname
echo $num

disk_type=""
drive_variables=""
drive_letter=""
dcount=0
index=-1
total_disk_count=`ls /dev/ | grep nvme | grep n1 | wc -l`
for disk in `ls /dev/ | grep nvme | grep n1`; do
  echo -e "\nProcessing /dev/$disk"
  disk_type="nvme"
  pvcreate -y  /dev/$disk
  mount_device="/dev/$disk"
  index=$((((((num-1))*total_disk_count))+(dcount)))
  echo $index
  dcount=$((dcount+1))
  disk_mount 
done;

echo "$dcount $disk_type disk found"

disk_type=""
drive_variables=""
drive_letter=""
dcount=0
index=-1
total_disk_count=`cat /proc/partitions | grep -ivw 'sda' | grep -ivw 'sda[1-3]' | grep -iv nvme  | sed 1,2d | gawk '{print $4}' | grep "^sd" | wc -l`
for disk in `cat /proc/partitions | grep -ivw 'sda' | grep -ivw 'sda[1-3]' | grep -iv nvme  | sed 1,2d | gawk '{print $4}' | grep "^sd" `; do
  echo -e "\nProcessing /dev/$disk"
  disk_type="bv"
  pvcreate -y  /dev/$disk
  mount_device="/dev/$disk"

  index=$((((((num-1))*total_disk_count))+(dcount)))
  echo $index
  drive_letter=`echo $disk | sed 's/sd//'`
  drive_variables="${drive_variables}${drive_letter}"
  dcount=$((dcount+1))
  disk_mount
done;

echo "$dcount $disk_type disk found"

service lustre status
lctl list_nids
nids=`lctl list_nids` | grep tcp1
echo $nids

df -h

# function call
enable_lnet_at_boot_time

echo "setup complete"
if [ "$FAILED" -eq "1" ]
then
    exit 1
fi

exit 0;
