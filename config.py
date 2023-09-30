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

PublicNetTag="public"
StorageNetTag="storage"
DataNetTag="data"
ImageId="ocid1.image.oc1.iad.aaaaaaaamf35m2qg5krijvq4alf6qmvdqiroq4i5zdwqqdijmstn4ryes36q"
MGSHostPattern="mgs-server-"
MDSHostPattern="metdata-server-"
OSSHostPattern="storage-server-"
DeploymentConfig={}
OCIConfig=None

ServerKernelVerson="4.18.0-477.10.1.el8_lustre.x86_64"
ServerLustreVersion="lustre-2.15.3-1.el8.x86_64"

#STS = [ "storage-server-19", "storage-server-26", "storage-server-23", "storage-server-24", "storage-server-21", "storage-server-20", "storage-server-25", "storage-server-22", "storage-server-7", "storage-server-4", "storage-server-17", "storage-server-10", "storage-server-8", "storage-server-16", "storage-server-1", "storage-server-9", "storage-server-15", "storage-server-12" ]

CLUSTER = {
        "name": "xai-phx-1",
#        "nodes": [ "mgs-server-1", "metadata-server-1", "storage-server-1" ]
        "nodes": [ 
            { 
                "name": "mgs-server-1",
                "shape": "VM.Standard2.2",
                "nic": 0,
                "vnics": 2,
                "bvs": 1,
                "bvSize": 50
            }
        ]
        
}


def runCmd(cmd,output=True,timeout=None):
    print("cmd: " + cmd)
    def timerout(p):
        print("Error: timed out")
        timer.cancel()
        os.killpg(os.getpgid(p.pid), signal.SIGTERM)

    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    timer = threading.Timer(timeout, timerout, args=[p])
    timer.start()
    out=""
    for line in iter(p.stdout.readline,b''):
        line=line.decode("utf-8").strip()
        out+=line
        if output:
            print(line,flush=True)
    for line in iter(p.stderr.readline,b''):
        line=line.decode("utf-8").strip()
        out+=line
        if output:
            print(line, flush=True)
    p.stdout.close()
    r = p.wait()
    timer.cancel()
    return {
                "output": out,
                "status": r
            }

def runRemoteCmd(ip,cmd,output=False,timeout=None):
    return runCmd(f'ssh -T -o StrictHostKeyChecking=no {ip} "{cmd}"',output=output, timeout=timeout)

def initOCI():
    global OCIConfig
    OCIConfig = oci.config.from_file()

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
    else:
        return None

def getConfig():
    error=False
    basicConfig={}
    print("Getting VCN and subnet details")
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
        print(f"Public subnet not found in vcn id {vcn}")
        error=True
    if not storage:
        print(f"Storage subnet not found in vcn id {vcn}")
        error=True
    if not data:
        data = storage
        
    basicConfig["publicNet"]=public
    basicConfig["storageNet"]=storage
    basicConfig["dataNet"]=data

    DeploymentConfig["basicConfig"]=basicConfig
    print("Getting Instance details")
    instanceClient = oci.core.ComputeClient(OCIConfig)

    r=oci.pagination.list_call_get_all_results(
            instanceClient.list_instances,
            compartment_id=DeploymentConfig["basicConfig"]["compartmentId"], 
            availability_domain=DeploymentConfig["basicConfig"]["availabilityDomain"]
            ).data

    nodes=[]
    for i in r:

        if i.lifecycle_state in [ "TERMINATING", "TERMINATED" ]:
#            print("Skipping instance not marked in currect state " + name)
            continue

        name=i.display_name
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
#            print("Skipping instance not marked in any cluster " + name)
            continue

        if cluster not in DeploymentConfig["clusters"]:
            DeploymentConfig["clusters"][cluster]={}
            DeploymentConfig["clusters"][cluster]["nodes"]=[]

        details={}

        details["name"]=name
        t=getNodeType(details)
        if t == None:
#            print("Skipping instance not current name pattern " + name)
            continue
        t=t["type"]
        idx=t["idx"]

        details["fqdn"]=name + "." + DeploymentConfig["basicConfig"]["storageNet"]["domain"]
        details["domain"]=DeploymentConfig["basicConfig"]["storageNet"]["domain"]
        details["dataDomain"]=DeploymentConfig["basicConfig"]["dataNet"]["domain"]
        details["vcnDomain"]=DeploymentConfig["basicConfig"]["domain"]
        details["idx"]=idx
        details["status"]=status
        details["shape"]=i.shape
        details["state"]=i.lifecycle_state
        details["id"]=i.id
        details["type"]=t

        instanceClient = oci.core.ComputeClient(OCIConfig)
        r=oci.pagination.list_call_get_all_results(
                instanceClient.list_vnic_attachments,
                compartment_id=basicConfig["compartmentId"],
                instance_id=i.id
                )

        cc=0
        for x in r.data:
            if x.lifecycle_state == "ATTACHED":
                if x.subnet_id == DeploymentConfig["basicConfig"]["dataNet"]["id"]:
                    details["name_a"]=x.display_name
                    details["fqdn_a"]=details["name_a"] + "." + DeploymentConfig["basicConfig"]["dataNet"]["domain"]
                cc+=1
        details["vnics"]=cc
        if "name_a" not in details:
            details["name_a"]=name
            details["fqdn_a"]=details["fqdn"]

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
                    cmds.append(f"yum iscsiadm -m node -o new -T {x.iqn} -p {x.ipv4}:{x.port}")
                    cmds.append(f"yum iscsiadm -m node -o update -T {x.iqn} -n node.startup -v automatic")
                    cmds.append(f"yum iscsiadm -m node -T {x.iqn} -p {x.ipv4}:{x.port} -l")

        details["volumes"]=cc
        details["cmds"]=cmds

        DeploymentConfig["clusters"][cluster]["nodes"].append(details)
    print(json.dumps(DeploymentConfig))
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
    r=instanceClient.attach_vnic(attach_vnic_details=attachDetails)
    if r and r.status/100 == 2:
        oci.wait_until(
            instanceClient,
            instanceClient.get_vnic_attachment(r.data.id),
            'lifecycle_state',
            'ATTACHED'
            )

        return True

    print("Attach vnic failed")
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

    r=instanceClient.attach_volume(attach_volume_details=attachDetails)
    if r and r.status/100 == 2:
        oci.wait_until(
            instanceClient,
            instanceClient.get_volume_attachment(r.data.id),
            'lifecycle_state',
            'ATTACHED'
            )
        return r.data

    print("Volume attach failed")
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
        max_wait_seconds=300
        )

    if r and r.status/100 == 2:
        return attachBV(displayName, instanceId, "iscsi", r.data.id)

    print("Create volume failed")
    return None

def getNodeStatus(n):
    instanceClient = oci.core.ComputeClient(OCIConfig)
    r=instanceClient.get_instance(n["id"]).data
    if "lustre-node-status" in r.freeform_tags:
        return r.freeform_tags["lustre-node-status"]
    else:
        return "Unknown"

def imageNode(n):
    if getNodeStatus(n) != "Created":
        print("Node " + n['name'] + " should not be imaged. Not in Created state")

def updateTag(instanceId, tags):
    instanceClient = oci.core.ComputeClient(OCIConfig)
    r=instanceClient.get_instance(instanceId).data

    for k in tags:
        r.freeform_tags[k]=tags[k]

    details=oci.core.models.UpdateInstanceDetails(freeform_tags=r.freeform_tags)
    instanceClient.update_instance(instance_id=instanceId, update_instance_details=details)

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
            "lustre-node-status": "Imaged"
            }
    updateTag(n["id"],tag)

def createInstance(clusterName, nodeType, shape, instanceName=None):

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
        if i.display_name == name:
            if i.lifecycle_state in [ "RUNNING", "STARTING" ]:
#                print("Found instance " + name)
                found=True
                r=i
                break
            elif i.lifecycle_state not in [ "TERMINATING", "TERMINATED" ]:
                print("Found instnace" + name + " in incorrect sate " + i.lifecycle_state )
                return None

    if not found:
        print("Creating new instance " + name)
        vnicDetails=oci.core.models.CreateVnicDetails(
                assign_private_dns_record=True,
                display_name=name,
                hostname_label=name,
                subnet_id=DeploymentConfig["basicConfig"]["storageNet"]["id"]
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
            print("Create instance failed")
            return None
    oci.wait_until(
        instanceClient,
        instanceClient.get_instance(r.id),
        'lifecycle_state',
        'RUNNING',
        max_wait_seconds=300
        )
    print("Set instance " + name + " to Created")
    updateTag(r.id, tags)
    return r

def getLustre(n):
    host=n["fqdn"]
    r=runRemoteCmd(host,"rpm -qa |grep ^lustre-2",timeout=10)
    if r["status"] == 0:
        return r["output"].strip()
    else:
        return "Unknown"

def getKernel(n):
    host=n["fqdn"]
    r=runRemoteCmd(host,"uname -a | awk '{print \$3}'",timeout=10)
    if r["status"] == 0:
        return r["output"].strip()
    else:
        return "Unknown"

def checkSsh(n,timeout=30):
    host=n["fqdn"]
    t=0
    while t <= 600:
        print("Checking ssh access to " + n["name"])
        r=runRemoteCmd(host,"echo test",timeout=timeout)
        if r["status"] == 0: 
            time.slee(30)
            print("ssh is available to node " + n["name"])
            return True
        else:
            time.sleep(5)
            t+=10
            continue
    if t >= 600 :
        return False

def iSCSIConfig(n):
    host=n["fqdn"]
    for c in n["cmds"]:
        r=runRemoteCmd(host,c)
        if r["status"] != 0:
            print(f'Running command {c} failed on host {n["name"]}')
            return False
    return True

def configureNode(n,template):

    if getNodeStatus(n) == "Created":
        print(f"Configuring node {n['name']} with additional VNIC and volumes")
        vols=template["bvs"]
        nicIndex=template["nic"]
        vnics=template["bnics"]

        if n["type"] == "MGS":
            nicName=f"{MGSHostPattern}vnic-{n['idx']}"
            bvName=f"{MGSHostPattern}{n['idx']}-MGT-"
        if n["type"] == "MDS":
            nicName=f"{MDSHostPattern}vnic-{n['idx']}"
            bvName=f"{MDSHostPattern}{n['idx']}-MDT-"
        if n["type"] == "OSS":
            nicName=f"{OSSHostPattern}vnic-{n['idx']}"
            bvName=f"{OSSHostPattern}{n['idx']}-OST-"

        if n["vnics"] < vnics:
            print("Creating vnic ...")
            attachVnic(n["id"], nicName, DeploymentConfig["basicConfig"]["dataNet"]["id"], nicIndex)
        if n["volumes"] < vols:
            for i in range(n["volumes"]+1,vols+1):
                name=f"{bvName}{i}" 
                print("Creating and attaching Block Volumes " + name)
                createAndAttachBV(name,
                DeploymentConfig["basicConfig"]["availabilityDomain"], 
                DeploymentConfig["basicConfig"]["compartmentId"], 
                template["bvSize"], n["id"])
        if checkSsh(n):
            setConfigured(n)

def reboot(n):
    print(f"Rebooting {n['fqdn']} ....")
    r=runCmd(f"sudo reboot")

def runScript(n,script):
    print(f"Running {script} on node {n['name']}")
    r=runCmd(f"cat {script} | ssh {n['fqdn']} 'cat > /tmp/{script}'")
    if r["status"] != 0 :
        print(f"Copying {script} to {n['fqdn']} failed")
        return False
    runRemoteCmd(n['fqdn'],f"chmod 755 /tmp/{script}.sh")
    if r["status"] != 0 :
        print(f"setting mode for {script} on {n['fqdn']} failed")
        return False
    runRemoteCmd(n['fqdn'],f"sudo /tmp/{script}",output=True)
    if r["status"] != 0 :
        print(f"{script} failed to image {n['fqdn']} failed")
        return False
    return True

def imageNode(n):
    print(f"Imaging node {n['name']} with lustre software")
    if not checkSsh(n):
        return

    if getNodeStatus(n) == "Imaged":
        print(f"Node {n['name']} is already imaged")
        return

    if getNodeStatus(n) == "Configured":
        if not runScript(n,"image_server.sh") :
            return
        setImaged(n)
        reboot(n)
    else:
        print(f"Node {n['fqdn']}  is not in Configured state")

def configureLustre(n):
    print(f"Configuring lustre on node {n['name']}")
    if not checkSsh(n):
        return
    v1=getKernel(n)
    v2=getLustre(n)
    print("Kernel version:" + v1)
    print("Lustre version:" + v2)
    if ( v1 != ServerKernelVerson ):
        print(f"Node {n['name']} is not running currect kernel version")
        return
    if ( v2 != ServerLustreVersion ):
        print(f"Node {n['name']} is not running currect lustre version")
        return

    if getNodeStatus(n) == "Ready":
        print(f"Node {n['name']} is already in ready state")
        return

    if getNodeStatus(n) == "Imaged":
        if not iSCSIConfig(n):
            print(f"Configuring iSCSI devices on node {n['name']} failed")
            return

        if not runScript(n,f"update_resolv_conf.sh {n['vcnDomain']} {n['domain']} {n['dataDomain']}"):
            print(f"Updating resolv.conf failed on node {n['name']}")
            return
            
        t=getNodeType(n)
        if t == None:
            print("Unknow node type {n['fqdn']}")
            return

        t=t["type"]
        if t == "MDS":
            script="install_metadata_2.sh "
        if t == "OSS":
            script="install_storage_2.sh "
        if t == "MGS":
            script="install_management_2.sh "

        if not runScript(n,script + n["fqdn_a"]):
            return

        setReady(n)
    else:
        print(f"Node {n['fqdn']}  is not in Imaged state")


#Main start here

initOCI()
   
for cn in CLUSTER["nodes"]:
    getConfig()
    if not createInstance(CLUSTER["name"], cn["shape"], cn["name"]):
        print(f"Create instance {cn['name']} failed")
    found=False
    getConfig()
    for n in DeploymentConfig["clusters"][CLUSTER["name"]]["nodes"]:
        if cn["name"] == n["name"]:
            configureNode(n,cn)
            imageNode(n)
            configureLustre(n)
            found=True
    if not found:
        print(f"Instance Node {cn['name']} was not found")



