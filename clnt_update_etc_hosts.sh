update_etc_hosts () {
  for host_fqdn in `cat /tmp/${nodeType}nodehosts` ; do
    host=${host_fqdn%%.*}
    host_ip=`nslookup $host_fqdn | grep "Address: " | gawk '{print $2}'`
    # update /etc/hosts file on all nodes with ip, fqdn and hostname of all nodes
    echo "$host_ip ${host_fqdn} ${host}" >> /etc/hosts
  done ;
}


find_cluster_nodes () {
  # Make a list of nodes in the cluster
  echo "Doing nslookup for $nodeType nodes"
  ct=1
  if [ $nodeCount -gt 0 ]; then
    while [ $ct -le $nodeCount ]; do
      nslk=`nslookup $nodeHostnamePrefix${ct}.$domainName`
      ns_ck=`echo -e $?`
      if [ $ns_ck = 0 ]; then
        hname=`nslookup $nodeHostnamePrefix${ct}.$domainName | grep Name | gawk '{print $2}'`
        echo "$hname" >> /tmp/${nodeType}nodehosts;
        echo "$hname" >> /tmp/allnodehosts;
        ct=$((ct+1));
      else
        # sleep 10 seconds and check again - infinite loop
        echo "Sleeping for 10 secs and will check again for nslookup $nodeHostnamePrefix${ct}.$domainName"
        sleep 10
      fi
    done;
    echo "Found `cat /tmp/${nodeType}nodehosts | wc -l` $nodeType nodes";
    echo `cat /tmp/${nodeType}nodehosts`;
  else
    echo "no $nodeType nodes configured"
  fi
}

nodeType="management"
domainName=$1
nodeHostnamePrefix=mgs-server-vnic-
nodeCount=1
find_cluster_nodes
update_etc_hosts

nodeType="meta"
domainName=$1
nodeHostnamePrefix=metadata-server-vnic-
nodeCount=1
find_cluster_nodes
update_etc_hosts

nodeType="storage"
domainName=$1
nodeHostnamePrefix=storage-server-vnic-
nodeCount=2
find_cluster_nodes
update_etc_hosts


