## Modify resolv.conf to ensure DNS lookups work from one private subnet to another subnet
mv /etc/resolv.conf /etc/resolv.conf.backup
echo "search $2 $3 $1" > /etc/resolv.conf
echo "nameserver 169.254.169.254" >> /etc/resolv.conf

# The below is to ensure any custom change to hosts and resolv.conf will not be overwritten with data from metaservice, but dhclient will still overwrite resolv.conf.
# This file /etc/oci-hostname.conf was updated using Ansible code already to add PRESERVE_HOSTINFO=2

# The below is to ensure above changes will not be overwritten by dhclient
chattr +i /etc/resolv.conf
