#!/bin/python

import oci
import requests
import subprocess
import re
import shlex
import sys
import time
import threading
import signal
import json
import os
import logging

# Change this to INFO, DEBUG, ERROR, etc as desired. Change to DEBUG for debug
LOGLEVEL=logging.INFO

PublicNetTag="public"
StorageNetTag="storage"
DataNetTag="data"
ImageId="ocid1.image.oc1.iad.aaaaaaaamf35m2qg5krijvq4alf6qmvdqiroq4i5zdwqqdijmstn4ryes36q"
MGSHostPattern="mgs-server-"
MDSHostPattern="metadata-server-"
OSSHostPattern="storage-server-"
ClientHostPattern="client-"
DeploymentConfig={}
OCIConfig=None

KernelVersion="4.18.0-477"
LustreVersion="lustre-2.15.3"

DefaultOSS = {
        # Default template for OSS. To make it easy as there could be many OSS being deployed. 
                "shape": "VM.Standard2.4",
                "nic": 0,
                "vnics": 2,
                "volumes": 12,
                "bvSize": 1024
        }

CLUSTER = {
        "name": "lstr1",
        # The names of the server actually desides what class they are:
        # For example: msg-server-1 is the first MGS server.
        # storage-server-10 is the 10th OSS server.
        # Also, do not name unrelated servers in the lustre subnet with this naming convention.
        # This script will pick them try to do something with them (to be changed)
        "nodes": [ 
            { 
                "name": "mgs-server-1",  # These names are standard names and must follow these format. 
                                         # Only the last index can be chaned. 
                "shape": "VM.Standard2.2", 
                "nic": 0, # Physical index where the second(data) NIC to be created. 
                          # For VM this is always 0.
                "vnics": 2, # How many vnics
                "volumes": 1, # How many block volumes. 
                "bvSize": 50 # BV size in GB
            }
            ,
            { 
                "name": "metadata-server-1",
                "shape": "VM.Standard2.4",
                "nic": 0,
                "vnics": 2,
                "volumes": 1,
                "bvSize": 100 
            }
            ,
            { 
                "name": "storage-server-1"
                # Rest of the details gets added from DefaultOSS
            }
            ,
            { 
                "name": "storage-server-2"
                # Rest of the details gets added from DefaultOSS
            }
            ,
            { 
                "name": "client-1",
                "shape": "VM.Standard2.1",
                "nic": 0,
                "vnics": 1,
                "volumes": 0,
                "bvSize": 100 
            }
        ]
        
}

#for s in range(1,25):
#    dd= {
#            "name": "client-" + str(s),
#            "shape": "VM.Standard2.24",
#            "nic": 0,
#            "vnics": 1,
#            "volumes": 0,
#            "bvSize": 100
#            }
#
#    CLUSTER["nodes"].append( dd )


logger=None

def logInfo(message):
    logger.info(message)

def logWarn(message):
    logger.warning(message)

def logCritical(message):
    logger.critical(message)

def logDebug(message):
    logger.debug(message)

def logError(message):
    logger.error(message)

def runCmd(cmd,output=True,timeout=None):
    logDebug("cmd: " + cmd)
    def timerout(p):
        logDebug("Error: timed out")
        timer.cancel()
        os.kill(p.pid, signal.SIGKILL)

    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timer = threading.Timer(timeout, timerout, args=[p])
    timer.start()
    out=""
    error=""
    for line in iter(p.stdout.readline,b''):
        line=line.decode("utf-8").strip()
        out+=line
        if output:
            print(line,flush=True)
    for line in iter(p.stderr.readline,b''):
        line=line.decode("utf-8").strip()
        error+=line
        if output:
            print(line, flush=True)
    p.stdout.close()
    p.stderr.close()
    r = p.wait()
    timer.cancel()

    return {
                "output": out,
                "status": r,
                "error": error
            }

def runRemoteCmd(ip,cmd,output=False,timeout=None):
    return runCmd(f'ssh -T -o StrictHostKeyChecking=no {ip} "{cmd}"',output=output, timeout=timeout)

def initOCI():
    global logger
    global OCIConfig
    dateFormat="%Y-%m-%dT%H:%M:%S"
    fmt = logging.Formatter(
        fmt='%(asctime)s.%(msecs)03dZ - %(name)s - %(levelname)s - %(message)s',
        datefmt= dateFormat
    )
    OCIConfig = oci.config.from_file()
    logger = logging.getLogger('LBUILDER')
    logger.setLevel(LOGLEVEL)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    logInfo("Init complete")

def getNodeType(n):
    name=n["name"]
    if MGSHostPattern == name[:len(MGSHostPattern)]:
       idx=name[len(MGSHostPattern):]
       if not idx.isnumeric():
           return None
       return { "type": "MGS", "idx": int(idx) }
    if OSSHostPattern == name[:len(OSSHostPattern)]:
       idx=name[len(OSSHostPattern):]
       if not idx.isnumeric():
           return None
       return { "type": "OSS", "idx": int(idx) }
    if MDSHostPattern == name[:len(MDSHostPattern)]:
       idx=name[len(MDSHostPattern):]
       if not idx.isnumeric():
           return None
       return { "type": "MDS", "idx": int(idx) }
    if ClientHostPattern == name[:len(ClientHostPattern)]:
       idx=name[len(ClientHostPattern):]
       if not idx.isnumeric():
           return None
       return { "type": "CLIENT", "idx": int(idx) }
    else:
        return None

def getConfig():
    error=False
    basicConfig={}
    logInfo("Getting VCN and subnet details")
    headers = {"Authorization": "Bearer Oracle"}
    r = requests.get("http://169.254.169.254/opc/v2/instance/", headers=headers).json()
    instanceId=r["id"]
    basicConfig["compartmentId"]=r["compartmentId"]
    basicConfig["availabilityDomain"]=r["availabilityDomain"]
    basicConfig["region"]=r["region"]
    basicConfig["key"]=r["metadata"]["ssh_authorized_keys"]
    basicConfig["imageId"]=r["image"]

    headers = {"Authorization": "Bearer Oracle"}
    r = requests.get("http://169.254.169.254/opc/v2/vnics/", headers=headers).json()
    vnic=r[0]["vnicId"]

    vnicClient = oci.core.VirtualNetworkClient(OCIConfig)
    r=vnicClient.get_vnic(vnic_id=vnic).data
    subnet=r.subnet_id
    r=vnicClient.get_subnet(subnet_id=subnet).data
    vcn=r.vcn_id

    basicConfig["vcn"]=vcn
    r=vnicClient.get_vcn(vcn).data
    basicConfig["domain"]=r.dns_label + ".oraclevcn.com"

    r=oci.pagination.list_call_get_all_results(
            vnicClient.list_subnets,
            compartment_id=basicConfig["compartmentId"], 
            vcn_id=vcn
            ).data

    public={}
    data={}
    storage={}
    DeploymentConfig["clusters"]={}
    for s in r:
        for k in s.freeform_tags:
            vv=s.freeform_tags[k].strip()
            k=k.strip()
            if k == "lustre-net" and vv == PublicNetTag:
                public["id"]=s.id
                public["domain"]=s.subnet_domain_name
            if k == "lustre-net" and vv == StorageNetTag:
                storage["id"]=s.id
                storage["domain"]=s.subnet_domain_name
            if k == "lustre-net" and vv == DataNetTag:
                data["id"]=s.id
                data["domain"]=s.subnet_domain_name
            if k == "lustre-cluster-name" :
                DeploymentConfig["clusters"][vv]={}
                DeploymentConfig["clusters"][vv]["nodes"]=[]

    if not public:
        logCritical(f"Public subnet not found in vcn id {vcn}")
        error=True
    if not storage:
        logCritical(f"Storage subnet not found in vcn id {vcn}")
        error=True
    if not data:
        data = storage
        
    basicConfig["publicNet"]=public
    basicConfig["storageNet"]=storage
    basicConfig["dataNet"]=data

    DeploymentConfig["basicConfig"]=basicConfig
    logInfo("Getting Instance details")
    instanceClient = oci.core.ComputeClient(OCIConfig)

    r=oci.pagination.list_call_get_all_results(
            instanceClient.list_instances,
            compartment_id=DeploymentConfig["basicConfig"]["compartmentId"], 
            availability_domain=DeploymentConfig["basicConfig"]["availabilityDomain"]
            ).data

    nodes=[]
    for i in r:

        name=i.display_name

        if i.lifecycle_state in [ "TERMINATING", "TERMINATED" ]:
            logDebug("Skipping instance not marked in currect state " + name)
            continue

        status=None
        cluster=None
        for k in i.freeform_tags:
            vv=i.freeform_tags[k].strip()
            k=k.strip()
            if k == "lustre-node-status":
                status=vv
            if k == "lustre-cluster-name":
                cluster=vv

        if status == None or cluster == None:
            logDebug("Skipping instance not marked in any cluster " + name)
            continue

        if cluster not in DeploymentConfig["clusters"]:
            DeploymentConfig["clusters"][cluster]={}
            DeploymentConfig["clusters"][cluster]["nodes"]=[]

        details={}

        details["name"]=name
        t=getNodeType(details)
        if t == None:
            logDebug("Skipping instance doesn't have consistent name pattern " + name)
            continue
        idx=t["idx"]
        t=t["type"]

        details["fqdn"]=name + "." + DeploymentConfig["basicConfig"]["storageNet"]["domain"]
        details["domain"]=DeploymentConfig["basicConfig"]["storageNet"]["domain"]
        if t == "CLIENT":
            details["fqdn"]=name + "." + DeploymentConfig["basicConfig"]["dataNet"]["domain"]
            details["domain"]=DeploymentConfig["basicConfig"]["dataNet"]["domain"]
        details["dataDomain"]=DeploymentConfig["basicConfig"]["dataNet"]["domain"]
        details["vcnDomain"]=DeploymentConfig["basicConfig"]["domain"]
        details["idx"]=idx
        details["status"]=status
        details["shape"]=i.shape
        details["state"]=i.lifecycle_state
        details["id"]=i.id
        details["type"]=t
        details["cluster"]=cluster

        instanceClient = oci.core.ComputeClient(OCIConfig)
        r=oci.pagination.list_call_get_all_results(
                instanceClient.list_vnic_attachments,
                compartment_id=basicConfig["compartmentId"],
                instance_id=i.id
                )

        cc=0
        found=False
        for x in r.data:
            if x.subnet_id in [ DeploymentConfig["basicConfig"]["storageNet"]["id"], DeploymentConfig["basicConfig"]["dataNet"]["id"] ]:
                found=True
            if x.lifecycle_state == "ATTACHED":
                if x.subnet_id == DeploymentConfig["basicConfig"]["dataNet"]["id"]:
                    if x.display_name:
                        details["name_a"]=x.display_name
                    else:
                        details["name_a"]=name
                    details["fqdn_a"]=details["name_a"] + "." + DeploymentConfig["basicConfig"]["dataNet"]["domain"]
                cc+=1
        if not found:
            logDebug(f"Skipping instance {i.display_name} not in subnet")
            continue

        details["vnics"]=cc
        if "name_a" not in details:
            details["name_a"]=name
            details["fqdn_a"]=details["fqdn"]
        
        if t == "MGS":
            DeploymentConfig["clusters"][cluster]["mgs"] = details["fqdn_a"]

        r=oci.pagination.list_call_get_all_results(
                instanceClient.list_volume_attachments,
                compartment_id=basicConfig["compartmentId"],
                instance_id=i.id 
                )
        cc=0
        cmds=[]
        for x in r.data:
            if x.attachment_type == "iscsi":
                if x.lifecycle_state == "ATTACHED":
                    cc+=1
                    cmds.append(f"sudo iscsiadm -m node -o new -T {x.iqn} -p {x.ipv4}:{x.port}")
                    cmds.append(f"sudo iscsiadm -m node -o update -T {x.iqn} -n node.startup -v automatic")
                    cmds.append(f"sudo iscsiadm -m node -T {x.iqn} -p {x.ipv4}:{x.port} -l")

        details["volumes"]=cc
        details["cmds"]=cmds

        DeploymentConfig["clusters"][cluster]["nodes"].append(details)
    logDebug(json.dumps(DeploymentConfig,indent=4))
    return error

def attachVnic(instanceId, displayName,subnetId, nicIndex):

    vnicDetails=oci.core.models.CreateVnicDetails(
            assign_private_dns_record=True,
            display_name=displayName,
            hostname_label=displayName,
            subnet_id=subnetId
            )
    attachDetails=oci.core.models.AttachVnicDetails(
            create_vnic_details=vnicDetails,
            display_name=displayName,
            instance_id=instanceId,
            nic_index=nicIndex
            )

    instanceClient = oci.core.ComputeClient(OCIConfig)
    cc=0
    status=409
    while cc < 5 and status == 409:
        r=instanceClient.attach_vnic(attach_vnic_details=attachDetails)
        if r and r.status/100 == 2:
            oci.wait_until(
                instanceClient,
                instanceClient.get_vnic_attachment(r.data.id),
                'lifecycle_state',
                'ATTACHED'
                )
            return True
        status=r.status
        cc+=1


    logCritical("Attach vnic failed")
    return None

def attachBV(displayName, instanceId, attType, volumeId):
    #attType="iscsi" or "paravirtualized"
    instanceClient = oci.core.ComputeClient(OCIConfig)
    attachDetails = oci.core.models.AttachVolumeDetails(
            display_name=displayName,
            instance_id=instanceId,
            is_shareable=True,
            type=attType,
            volume_id=volumeId
            )

    cc=0
    status=409
    while cc < 5 and status == 409:
        r=instanceClient.attach_volume(attach_volume_details=attachDetails)
        if r and r.status/100 == 2:
            oci.wait_until(
                instanceClient,
                instanceClient.get_volume_attachment(r.data.id),
                'lifecycle_state',
                'ATTACHED'
                )
            return r.data
        status=r.status
        cc+=1

    logCritical("Volume attach failed")
    return False

def createAndAttachBV(displayName,ad, compartmentId, size, instanceId):

    bvClient = oci.core.BlockstorageClient(OCIConfig)
    volumeDetails = oci.core.models.CreateVolumeDetails(
            availability_domain=ad,
            compartment_id=compartmentId,
            display_name=displayName,
            size_in_gbs=size,
            vpus_per_gb=20
            )

    r=bvClient.create_volume(create_volume_details=volumeDetails)

    oci.wait_until(
        bvClient,
        bvClient.get_volume(r.data.id),
        'lifecycle_state',
        'AVAILABLE',
        max_wait_seconds=600
        )

    if r and r.status/100 == 2:
        return attachBV(displayName, instanceId, "iscsi", r.data.id)

    logCritical("Create volume failed")
    return None

def getNodeStatus(n):
    instanceClient = oci.core.ComputeClient(OCIConfig)
    r=instanceClient.get_instance(n["id"]).data
    if "lustre-node-status" in r.freeform_tags:
        return r.freeform_tags["lustre-node-status"]
    else:
        return "Unknown"

def updateTag(instanceId, tags):
    instanceClient = oci.core.ComputeClient(OCIConfig)
    r=instanceClient.get_instance(instanceId).data

    for k in tags:
        r.freeform_tags[k]=tags[k]

    details=oci.core.models.UpdateInstanceDetails(freeform_tags=r.freeform_tags)
    cc=0
    status=409
    while cc < 5 and status == 409:
        r=instanceClient.update_instance(instance_id=instanceId, update_instance_details=details)
        if r and r.status/100 == 2:
            return True
        status=r.status
        cc+=1


def setImaged(n):
    instanceClient = oci.core.ComputeClient(OCIConfig)
    tag={
            "lustre-node-status": "Imaged"
            }
    updateTag(n["id"],tag)

def setReady(n):
    instanceClient = oci.core.ComputeClient(OCIConfig)
    tag={
            "lustre-node-status": "Ready"
            }
    updateTag(n["id"],tag)

def setConfigured(n):
    instanceClient = oci.core.ComputeClient(OCIConfig)
    tag={
            "lustre-node-status": "Configured"
            }
    updateTag(n["id"],tag)

def createInstance(clusterName, shape, instanceName=None):

    tags = {
            "lustre-node-status": "Created",
            "lustre-cluster-name": clusterName
    }
    name=instanceName
    instanceClient = oci.core.ComputeClient(OCIConfig)
    r=oci.pagination.list_call_get_all_results(
        instanceClient.list_instances,
        compartment_id=DeploymentConfig["basicConfig"]["compartmentId"],
        availability_domain=DeploymentConfig["basicConfig"]["availabilityDomain"]
        ).data

    found=False
    for i in r:
        vr=oci.pagination.list_call_get_all_results(
                instanceClient.list_vnic_attachments,
                compartment_id=DeploymentConfig["basicConfig"]["compartmentId"],
                instance_id=i.id
                )
        inSubnet=False
        for x in vr.data:
            if x.subnet_id in [ DeploymentConfig["basicConfig"]["storageNet"]["id"] , DeploymentConfig["basicConfig"]["dataNet"]["id"] ] :
                inSubnet=True
                break

        if not inSubnet:
            logDebug(f"Skipping instance {i.display_name} not in subnet")
            continue

        if i.display_name == name:
            if i.lifecycle_state in [ "RUNNING", "STARTING" ]:
                logDebug("Found instance " + name)
                found=True
                r=i
                break
            elif i.lifecycle_state not in [ "TERMINATING", "TERMINATED" ]:
                logDebug("Found instnace" + name + " in incorrect sate " + i.lifecycle_state )
                return None

    if not found:
        logInfo("Creating new instance " + name)
        sid=DeploymentConfig["basicConfig"]["storageNet"]["id"]
        t=getNodeType( { "name": name })
        if t and t["type"] == "CLIENT":
            sid=DeploymentConfig["basicConfig"]["dataNet"]["id"]
        vnicDetails=oci.core.models.CreateVnicDetails(
                assign_private_dns_record=True,
                display_name=name,
                hostname_label=name,
                subnet_id=sid
                )

        sourceDetails=oci.core.models.InstanceSourceViaImageDetails(
                source_type="image",
                boot_volume_size_in_gbs=50,
                image_id=DeploymentConfig["basicConfig"]["imageId"]
                )

        instanceMetaData = {
            'ssh_authorized_keys': DeploymentConfig["basicConfig"]["key"]
        }

        instanceDetails=oci.core.models.LaunchInstanceDetails(
            availability_domain=DeploymentConfig["basicConfig"]["availabilityDomain"],
            compartment_id=DeploymentConfig["basicConfig"]["compartmentId"],
            display_name=name,
            create_vnic_details=vnicDetails,
            freeform_tags = tags,
            shape=shape,
            metadata=instanceMetaData,
            source_details=sourceDetails
            )

        r=instanceClient.launch_instance(launch_instance_details=instanceDetails)
        if r and r.status/100 == 2:
            r=r.data
        else:
            logCritical("Create instance failed")
            return None
    oci.wait_until(
        instanceClient,
        instanceClient.get_instance(r.id),
        'lifecycle_state',
        'RUNNING',
        max_wait_seconds=600
        )

    if getNodeStatus( { "id": r.id } ) == "Unknown":
        logDebug("Set instance " + name + " to Created")
        updateTag(r.id, tags)

    getConfig()
    return r

def getLustre(n):
    host=n["fqdn"]
    rpm="lustre-2"
    if n["type"] == "CLIENT" :
        rpm="lustre-client-2"
    r=runRemoteCmd(host,f"rpm -qa |grep ^{rpm}")
    if r["status"] == 0:
        return r["output"].strip()
    else:
        return "Unknown"

def getKernel(n):
    host=n["fqdn"]
    r=runRemoteCmd(host,"uname -a | awk '{print \$3}'")
    if r["status"] == 0:
        return r["output"].strip()
    else:
        return "Unknown"

def checkSsh(n,timeout=10):
    host=n["fqdn"]
    start=time.time()
    while time.time() - start < 600:
        logInfo("Checking ssh access to " + n["name"])
        r=runRemoteCmd(host,"echo test",timeout=timeout)
        if r["status"] == 0: 
            return True
        else:
            time.sleep(10)
            continue
    return False

def iSCSIConfig(n):
    host=n["fqdn"]
    logInfo("Setting up iSCSI access to block volumes on " + host)
    for c in n["cmds"]:
        r=runRemoteCmd(host,c)
        if r["status"] != 0:
            logError(f'Running command {c} failed on host {n["name"]}')
            return False
    return True

def configureNode(n,template):

    if getNodeStatus(n) != "Created":
        logError("Node " + n['name'] + " already configured")
        return True

    logInfo(f"Configuring node {n['name']} with additional {template['vnics']-n['vnics']} VNIC and {template['volumes']-n['volumes']} volumes")
    vols=template["volumes"]
    nicIndex=template["nic"]
    vnics=template["vnics"]

    if n["type"] == "MGS":
        nicName=f"{MGSHostPattern}vnic-{n['idx']}"
        bvName=f"{MGSHostPattern}{n['idx']}-MGT-"
    if n["type"] == "MDS":
        nicName=f"{MDSHostPattern}vnic-{n['idx']}"
        bvName=f"{MDSHostPattern}{n['idx']}-MDT-"
    if n["type"] == "OSS":
        nicName=f"{OSSHostPattern}vnic-{n['idx']}"
        bvName=f"{OSSHostPattern}{n['idx']}-OST-"
    if n["type"] == "CLIENT":
        nicName=f"{ClientHostPattern}vnic-{n['idx']}"
        bvName=f"{ClientHostPattern}{n['idx']}-CLT-"

    if n["vnics"] < vnics:
        logInfo("Creating and attaching vnic ...")
        attachVnic(n["id"], nicName, DeploymentConfig["basicConfig"]["dataNet"]["id"], nicIndex)
    if n["volumes"] < vols:
        for i in range(n["volumes"]+1,vols+1):
            name=f"{bvName}{i}" 
            logInfo("Creating and attaching Block Volumes " + name)
            createAndAttachBV(name,
            DeploymentConfig["basicConfig"]["availabilityDomain"], 
            DeploymentConfig["basicConfig"]["compartmentId"], 
            template["bvSize"], n["id"])
    setConfigured(n)
    getConfig()
    return True

def rebootNode(n):
    logInfo(f"Rebooting {n['fqdn']} ....")
    r=runRemoteCmd(n['fqdn'], "sudo reboot")
    time.sleep(15)
    return True

def runScript(n,script,params=None):
    logInfo(f"Running {script} on node {n['name']}")
    r=runCmd(f"cat {script} | ssh -T -o StrictHostKeyChecking=no {n['fqdn']} 'cat > /tmp/{script}'")
    if r["status"] != 0 :
        logCritical(f"Copying {script} to {n['fqdn']} failed")
        return False
    r=runRemoteCmd(n['fqdn'],f"chmod 755 /tmp/{script}")
    if r["status"] != 0 :
        logCritical(f"setting mode for {script} on {n['fqdn']} failed")
        return False
    if params:
        script = script + " " + params
    r=runRemoteCmd(n['fqdn'],f"sudo /tmp/{script}",output=True)
    if r["status"] != 0 :
        logCritical(f"{script} failed on {n['fqdn']}")
        return False
    return True

def imageNode(n):
    logInfo(f"Imaging node {n['name']} with lustre software")
    if getNodeStatus(n) != "Configured":
        logError(f"Node {n['name']} already imaged")
        return True

    if not checkSsh(n):
        return False

    script="image_server.sh"
    t=getNodeType(n)
    if t and t["type"] == "CLIENT":
        script="image_client.sh"
    if not runScript(n,script) :
        return False
    setImaged(n)
    rebootNode(n)
    return True

def configureLustre(n):
    logInfo(f"Configuring lustre on node {n['name']}")
    if getNodeStatus(n) != "Imaged":
        logError("Node " + n['name'] + " already configured for lustre")
        return True

    if not checkSsh(n):
        return False

    v1=getKernel(n)
    v2=getLustre(n)
    logInfo(f"{n['name']}: Kernel version:" + v1)
    logInfo(f"{n['name']}: Lustre version:" + v2)
    if ( v1[:len(KernelVersion)] != KernelVersion ):
        logCritical(f"Node {n['name']} is not running currect kernel version")
        return False
#    if ( v2[:len(LustreVersion)] != LustreVersion ):
#        logCritical(f"Node {n['name']} is not running currect lustre version")
#        return False

    if not iSCSIConfig(n):
        logCritical(f"Configuring iSCSI devices on node {n['name']} failed")
        return False

    if not runScript(n,f"update_resolv_conf.sh" , f"{n['vcnDomain']} {n['domain']} {n['dataDomain']}"):
        logCritical(f"Updating resolv.conf failed on node {n['name']}")
        return False
            
    t=getNodeType(n)
    if t == None:
        logCritical("Unknow node type {n['fqdn']}")
        return False

    t=t["type"]
    if t == "MDS":
        script="install_metadata_2.sh "
    if t == "OSS":
        script="install_storage_2.sh "
    if t == "MGS":
        script="install_management_2.sh "
    if t == "CLIENT":
        script="install_client_2.sh "

    if "mgs" not in DeploymentConfig["clusters"][n["cluster"]] :
        logCritical("Unable to find MGS for cluster " + DeploymentConfig["clusters"][n["cluster"]])
        return False
    if not runScript(n,script,f"{DeploymentConfig['clusters'][n['cluster']]['mgs']} {n['cluster']}"):
        return False

    setReady(n)
    return True


#Main start here
initOCI()
runCmd("mv ~/.ssh/known_hosts ~/.ssh/known_hosts.old 2>/dev/null")
   
for cn in CLUSTER["nodes"]:
    st=time.time()
    logInfo(f"Node {cn['name']} start")
    for k in DefaultOSS:
        if k not in cn:
            cn[k]=DefaultOSS[k]

    getConfig()

    finished=False
    if CLUSTER["name"] in DeploymentConfig["clusters"] :
        for n in DeploymentConfig["clusters"][CLUSTER["name"]]["nodes"]:
            if cn["name"] == n["name"]:
                if n["status"] == "Ready":
                    finished=True
                    logInfo(f"Node {n['name']} is already in Ready state")
                    break

    if finished:
        continue

    if not createInstance(CLUSTER["name"], cn["shape"], cn["name"]):
        logCritical(f"Create instance {cn['name']} failed")
        continue

    found=False
    failed=False
    for n in DeploymentConfig["clusters"][CLUSTER["name"]]["nodes"]:
        if cn["name"] == n["name"]:
            found=True
            if not configureNode(n,cn):
                failed=True
            break

    if failed:
        continue

    if not found:
        logError("Instnace " + cn["name"] + " not found in any running instances")
        continue

    failed=False
    for n in DeploymentConfig["clusters"][CLUSTER["name"]]["nodes"]:
        if cn["name"] == n["name"]:
            if not imageNode(n):
                failed=True
                break
            if not configureLustre(n):
                failed=True
                break
            logInfo(f"Node {cn['name']} finished, seconds={time.time()-st}")
            break

    if failed:
        continue
    print(DeploymentConfig)
