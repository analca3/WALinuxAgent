# Windows Azure Linux Agent
#
# Copyright 2014 Microsoft Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Requires Python 2.4+ and Openssl 1.0+

import os
import json
import re
import time
import xml.etree.ElementTree as ET
import walinuxagent.logger as logger
import walinuxagent.utils.restutil as restutil
from walinuxagent.utils.osutil import CurrOS, CurrOSInfo
import walinuxagent.utils.fileutil as fileutil
import walinuxagent.utils.shellutil as shellutil
from walinuxagent.utils.textutil import *
from walinuxagent.protocol.common import *

VersionInfoUri = "http://{0}/?comp=versions"
GoalStateUri = "http://{0}/machine/?comp=goalstate"
HealthReportUri="http://{0}/machine?comp=health"
RolePropUri="http://{0}/machine?comp=roleProperties"

WireServerAddrFile = "WireServer"
VersionInfoFile = "Versions.xml"
IncarnationFile = "Incarnation"
GoalStateFile = "GoalState.{0}.xml"
HostingEnvFile = "HostingEnvironmentConfig.xml"
SharedConfigFile = "SharedConfig.xml"
CertificatesFile = "Certificates.xml"
CertificatesJsonFile = "Certificates.json"
P7MFile="Certificates.p7m"
PEMFile="Certificates.pem"
ExtensionsFile = "ExtensionsConfig.{0}.xml"
ManifestFile="{0}.{1}.manifest"
TransportCertFile = "TransportCert.pem"
TransportPrivateFile = "TransportPrivate.pem"

ProtocolVersion = "2012-11-30"

HandlerStatusMapping = {
    'installed' : 'Installing',
    'enabled' : 'Ready',
    'uninstalled' : 'NotReady',
    'disabled' : 'NotReady'
}

class ProtocolV1(Protocol):

    @staticmethod
    def Detect():
        endpoint = CurrOS.GetWireServerEndpoint()
        if endpoint is None:
            raise Exception("Wire server endpoint not found.")
        protocol = ProtocolV1(endpoint)
        protocol.refreshCache()
        return protocol
      
    @staticmethod
    def Init():
        endpoint = CurrOS.GetWireServerEndpoint()
        if endpoint is None:
            raise Exception("Wire server endpoint not found.")
        protocol = ProtocolV1(endpoint)
        return protocol

    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.libDir = CurrOS.GetLibDir()
        self.incarnation = None
        self.hostingEnv = None
        self.certificates = None
        self.extensions = None
 
    @logger.LogError("check protocol version")
    def checkProtocolVersion(self):
        versionInfoXml = restutil.HttpGet(VersionInfoUri.format(self.endpoint))
        self.versionInfo = VersionInfo(versionInfoXml)
        fileutil.SetFileContents(VersionInfoFile, versionInfoXml)

        negotiated = None;
        if ProtocolVersion == self.versionInfo.getPreferred():
            negotiated = self.versionInfo.getPreferred()
        for version in self.versionInfo.getSupported():
            if ProtocolVersion == version:
                negotiated = version
                break
        if negotiated:
            logger.Info("Negotiated wire protocol version:{0}", ProtocolVersion)
        else:
            logger.Warn("Agent supported wire protocol version: {0} was not "
                        "advised by Fabric.", ProtocolVersion)
            raise Exception("Wire protocol version not supported")
    
    def updateGoalState(self):
        goalStateXml = restutil.HttpGet(GoalStateUri.format(self.endpoint),
                                        headers={
                                            "x-ms-agent-name":"WALinuxAgent",
                                            "x-ms-version":ProtocolVersion
                                        })
        if goalStateXml is None:
            raise Exception("Failed update goalstate")
        self.goalState = GoalState(goalStateXml)
        self.incarnation = self.goalState.getIncarnation()

        goalStateFile = GoalStateFile.format(self.incarnation)
        fileutil.SetFileContents(goalStateFile, goalStateXml)
        fileutil.SetFileContents(IncarnationFile, str(self.incarnation))

    def updateHostingEnv(self):
        hostingEnvXml = restutil.HttpGet(self.goalState.getHostingEnvUri(),
                                         headers={
                                            "x-ms-agent-name":"WALinuxAgent",
                                            "x-ms-version":ProtocolVersion
                                         })
        if hostingEnvXml is None:
            raise Exception("Failed to update hosting environment config")
        self.hostingEnv = HostingEnv(hostingEnvXml)
        fileutil.SetFileContents(HostingEnvFile, hostingEnvXml)

    def updateSharedConfig(self):
        sharedConfigXml = restutil.HttpGet(self.goalState.getSharedConfigUri(),
                                           headers={
                                                "x-ms-agent-name":"WALinuxAgent",
                                                "x-ms-version":ProtocolVersion
                                           })
        self.sharedConfig = SharedConfig(sharedConfigXml)
        fileutil.SetFileContents(SharedConfigFile, sharedConfigXml)

    def updateCertificates(self):
        certificatesXml = restutil.HttpGet(self.goalState.getCertificatesUri(),
                                           headers={
                                                "x-ms-agent-name":"WALinuxAgent",
                                                "x-ms-version":ProtocolVersion
                                           })
        if certificatesXml is None:
            raise Exception("Failed to update certificates")
        fileutil.SetFileContents(CertificatesFile, certificatesXml)
        self.certificates = Certificates()
        certificatesJson = self.certificates.decrypt(certificatesXml)
        fileutil.SetFileContents(CertificatesJsonFile, certificatesJson)
    
    def updateExtensionConfig(self):
        extentionsXml = restutil.HttpGet(self.goalState.getExtensionsUri(),
                                         headers={
                                            "x-ms-agent-name":"WALinuxAgent",
                                            "x-ms-version":ProtocolVersion,
                                            "x-ms-cipher-name": "DES_EDE3_CBC",
                                            "x-ms-guest-agent-public-x509-cert":self.getTransportCert()
                                         })
        if extentionsXml is None:
            raise Exception("Failed to update extensions config")
        self.extensions = ExtensionsConfig(extentionsXml)
        extensionsFile = ExtensionsFile.format(self.incarnation)
        fileutil.SetFileContents(extensionsFile, extentionsXml)

        for ext in self.extensions.getExtensions():
            manifestUri = self.extensions.getManifestUri(ext.getName())
            for uri in manifestUri:
                try:
                    manifestXml = restutil.HttpGet(uri)
                    manifestXml = RemoveBom(manifestXml)
                    manifestFile = ManifestFile.format(ext.getName(), 
                                                       self.incarnation)
                    fileutil.SetFileContents(manifestFile, manifestXml)
                    ExtensionManifest(manifestXml).update(ext)
                    break
                except Exception, e:
                    #Download manifest failed, will retry with failover location
                    logger.Warn("Download manifest for {0} failed: uri={1}",
                                ext.getName(),
                                uri)

    def getTransportCert(self):
        cert = ""
        for line in fileutil.GetFileContents(TransportCertFile).split('\n'):
            if "CERTIFICATE" not in line:
                cert += line.rstrip()
        return cert

    def refreshCache(self):
        """
        In protocol v1(wire server protocol), agent will periodically call wire
        server to get the configuration and save it in the disk. So that other 
        application like extension could read the data from the disk.
        """
        self.checkProtocolVersion()
        CurrOS.GenerateTransportCert()
        self.updateGoalState()
        self.updateHostingEnv()
        self.updateSharedConfig()
        self.updateCertificates()
        self.updateExtensionConfig()
        
        
    def getIncarnation(self):
        if self.incarnation is None:
            if os.path.isfile(IncarnationFile):
                incarnationStr = fileutil.GetFileContents(IncarnationFile)
                self.incarnation = int(incarnationStr)
            else:
                self.incarnation = 0
        return self.incarnation

    def getVmInfo(self):
        if self.hostingEnv is None:
            hostingEnvXml = fileutil.GetFileContents(HostingEnvFile)
            self.hostingEnv = HostingEnv(hostingEnvXml)
        vmInfo = {
            "subscriptionId":None,
            "vmName":self.hostingEnv.getVmName()
        }
        return VmInfo(vmInfo)

    def getCerts(self):
        if self.certificates is None:
            certificatesJson = fileutil.GetFileContents(CertificatesJsonFile)
            self.certificates = Certificates(certificatesJson)
        return self.certificates.getCerts()

    def getExtensions(self):
        return self._getExtensionConfig().getExtensions()

    #TODO Move this method into class ExtensionConfig
    def _getExtensionConfig(self):
        if self.extensions is None:
            extensionsFile = ExtensionsFile.format(self.getIncarnation())
            extentionsXml = fileutil.GetFileContents(extensionsFile)
            self.extensions = ExtensionsConfig(extentionsXml)
            for ext in self.extensions.getExtensions():
                manifestFile = ManifestFile.format(ext.getName(), 
                                                   self.incarnation)
                manifestXml = fileutil.GetFileContents(manifestFile)
                ExtensionManifest(manifestXml).update(ext)
        return self.extensions

    def reportProvisionStatus(self, status=None, subStatus="", 
                              description="", thumbprint=None):
        if status is not None:
            healthReport = self._buildHealthReport(status, 
                                                   subStatus, 
                                                   description)
            healthReportUri = HealthReportUri.format(self.endpoint)
            ret = restutil.HttpPost(healthReportUri, healthReport)

        if thumbprint is not None:
            roleProp = self._buildRoleProperties(thumbprint)
            rolePropUri = RolePropUri.format(self.endpoint)
            ret = restutil.HttpPost(rolePropUri, roleProp)

    def _buildRoleProperties(self, thumbprint):
        return (u"<?xml version=\"1.0\" encoding=\"utf-8\"?>"
                "<RoleProperties>"
                "<Container>"
                "<ContainerId>{0}</ContainerId>"
                "<RoleInstances>"
                "<RoleInstance>"
                "<Id>{1}</Id>"
                "<Properties>"
                "<Property name=\"CertificateThumbprint\" value=\"{2}\" />"
                "</Properties>"
                "</RoleInstance>"
                "</RoleInstances>"
                "</Container>"
                "</RoleProperties>"
                "").format(self.goalState.getContainerId(),
                           self.goalState.getRoleInstanceId(),
                           thumbprint)

    def _buildHealthReport(self, status, subStatus, description):
        return (u"<?xml version=\"1.0\" encoding=\"utf-8\"?>"
                "<Health"
                    "xmlns:xsi=\"http://www.w3.org/2001/XMLSchema-instance\""
                " xmlns:xsd=\"http://www.w3.org/2001/XMLSchema\">"
                "<GoalStateIncarnation>{0}</GoalStateIncarnation>"
                "<Container>"
                "<ContainerId>{1}</ContainerId>"
                "<RoleInstanceList>"
                "<Role>"
                "<InstanceId>{2}</InstanceId>"
                "<Health>"
                "<State>{3}</State>"
                "<Details>"
                "<SubStatus>{4}</SubStatus>"
                "<Description>{5}</Description>"
                "</Details>"
                "</Health>"
                "</Role>"
                "</RoleInstanceList>"
                "</Container>"
                "</Health>"
                "").format(self.goalState.getIncarnation(),
                           self.goalState.getContainerId(),
                           self.goalState.getRoleInstanceId(),
                           status, 
                           subStatus, 
                           description)

    def reportAgentStatus(self, version, status, message):
        tstamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        formattedMessage = {
            'lang' : 'en-Us',
            'message' : message
        }
        guestAgentStatus = {
            'version' : version,
            'status' : status,
            'formattedMessage' : formattedMessage
        }
        handlerAggregateStatus = self.getExtensionStatus()
        aggregateStatus = {
            'guestAgentStatus': guestAgentStatus,
            'handlerAggregateStatus' : handlerAggregateStatus
        }
        report = {
            'version' : version,
            'timestampUTC' : tstamp,
            'aggregateStatus' : aggregateStatus
        }
        data = json.dumps(report)
        headers = {
             "x-ms-blob-type" : "BlockBlob", 
             "x-ms-date" : time.strftime("%Y-%M-%dT%H:%M:%SZ", time.gmtime()) ,
             "Content-Length": str(len(data))
        }
        restutil.HttpPut(self._getExtensionConfig().getStatusUploadBlob(),
                         data,
                         headers, 2)

    def getExtensionStatus(self): 
        aggregatedStatusList = []
        for ext in self.getExtensions():
            status = None
            statusFile = os.path.join(self.libDir,  ext.getStatusFile())
            if os.path.isfile(statusFile):
                try:
                    statusJson = fileutil.GetFileContents(statusFile)
                    status = json.loads(statusJson)[0]
                except Exception, e:
                    logger.Error("Failed to parse extension status file: {0}", e)
            
            handlerStatus = "NotReady"
            handlerCode = None
            handlerMessage = None
            handlerFormattedMessage = None
            handlerStatusFile = os.path.join(self.libDir, 
                                             ext.getHandlerStateFile())
            if os.path.isfile(handlerStatusFile):
                handlerStatus = fileutil.GetFileContents(handlerStatusFile)
                handlerStatus = handlerStatus.lower()
                handlerStatus = HandlerStatusMapping[handlerStatus]
            
            heartbeat = None
            heartbeatFile = os.path.join(self.libDir, ext.getHeartbeatFile())
            if os.path.isfile(heartbeatFile):
                if not self.isResponsive(heartbeatFile):
                    handlerStatus = 'Unresponsive'
                else:
                    try:
                        heartbeatJson = fileutil.GetFileContents(heartbeatFile)
                        heartbeat = json.loads()[0]['heartbeat']
                        handlerStatus = heartbeat['status']
                        handlerCode = heartbeat['code']
                        handlerMessage = heartbeat['message']
                        handlerFormattedMessage = heartbeat['formattedMessage']
                    except Exception, e:
                        logger.Error(("Failed to parse extension "
                                      "heart beat file: {0}"), e)
            aggregatedStatus = {
                'handlerVersion' : ext.getVersion(),
                'handlerName' : ext.getName(),
                'status' : handlerStatus,
                'code' : handlerCode,
                'message' : handlerMessage,
                'formattedMessage' : handlerFormattedMessage,
                'runtimeSettingsStatus' : {
                    'settingsStatus' : status
                }
            }
            aggregatedStatusList.append(aggregatedStatus)
        return aggregatedStatusList

    def isResponsive(self, heartbeatFile):
        lastUpdate=int(time.time()-os.stat(heartbeatFile).st_mtime)
        return  lastUpdate > 600    # not updated for more than 10 min

    def reportExtensionStatus(self, status):
        """
        In wire protocol, extensions status is reported together with 
        agent status in a fixed period. Leave this method empty.
        """
        pass

    def reportEvent(self):
        #TODO port diagnostic code here
        pass

class VersionInfo():
    def __init__(self, xmlText):
        """
        Query endpoint server for wire protocol version.
        Fail if our desired protocol version is not seen.
        """
        self.parse(xmlText)
   
    def parse(self, xmlText):
        xmlDoc = ET.fromstring(xmlText.strip())
        self.preferred = FindFirstNode(xmlDoc, ".//Preferred/Version").text
        logger.Info("Fabric preferred wire protocol version:{0}", self.preferred)

        self.supported = []
        nodes = FindAllNodes(xmlDoc, ".//Supported/Version")
        for node in nodes:
            version = node.text
            logger.Verbose("Fabric supported wire protocol version:{0}", version)
            self.supported.append(version)

    def getPreferred(self):
        return self.preferred

    def getSupported(self):
        return self.supported

class GoalState():
    
    def __init__(self, xmlText):
        self.parse(xmlText)

    def reinitialize(self):
        self.incarnation = None
        self.expectedState = None
        self.hostingEnvUri = None
        self.sharedConfigUri = None
        self.certificatesUri = None
        self.extensionsUri = None
        self.roleInstanceId = None
        self.containerId = None
        self.loadBalancerProbePort = None

    def getIncarnation(self):
        return self.incarnation
    
    def getExpectedState(self):
        return self.expectedState

    def getHostingEnvUri(self):
        return self.hostingEnvUri

    def getSharedConfigUri(self):
        return self.sharedConfigUri
    
    def getCertificatesUri(self):
        return self.certificatesUri

    def getExtensionsUri(self):
        return self.extensionsUri

    def getRoleInstanceId(self):
        return self.roleInstanceId

    def getContainerId(self):
        return self.containerId

    def getLoadBalancerProbePort(self):
        return self.loadBalancerProbePort

    def parse(self, xmlText):
        """
        Request configuration data from endpoint server.
        """
        self.reinitialize()
        logger.Verbose(xmlText)
        self.xmlText = xmlText
        xmlDoc = ET.fromstring(xmlText.strip())
        self.incarnation = (FindFirstNode(xmlDoc, ".//Incarnation")).text
        self.expectedState = (FindFirstNode(xmlDoc, ".//ExpectedState")).text
        self.hostingEnvUri = (FindFirstNode(xmlDoc, 
                                            ".//HostingEnvironmentConfig")).text
        self.sharedConfigUri = (FindFirstNode(xmlDoc, ".//SharedConfig")).text
        self.certificatesUri = (FindFirstNode(xmlDoc, ".//Certificates")).text
        self.extensionsUri = (FindFirstNode(xmlDoc, ".//ExtensionsConfig")).text
        self.roleInstanceId = (FindFirstNode(xmlDoc, 
                                             ".//RoleInstance/InstanceId")).text
        self.containerId = (FindFirstNode(xmlDoc, 
                                             ".//Container/ContainerId")).text
        self.loadBalancerProbePort = (FindFirstNode(xmlDoc, 
                                                    ".//LBProbePorts/Port")).text
        return self
        

class HostingEnv(object):
    """
    parse Hosting enviromnet config and store in
    HostingEnvironmentConfig.xml
    """
    def __init__(self, xmlText):
        self.parse(xmlText)

    def reinitialize(self):
        """
        Reset Members.
        """
        self.vmName = None
        self.xmlText = None

    def getVmName(self):
        return self.vmName

    def parse(self, xmlText):
        """
        parse and create HostingEnvironmentConfig.xml.
        """
        self.reinitialize()
        self.xmlText = xmlText
        xmlDoc = ET.fromstring(xmlText.strip())
        self.vmName = FindFirstNode(xmlDoc, ".//Incarnation").attrib["instance"]
        return self

class SharedConfig(object):
    """
    parse role endpoint server and goal state config.
    """
    def __init__(self, xmlText):
        self.parse(xmlText)

    def reinitialize(self):
        """
        Reset members.
        """
        pass

    def parse(self, xmlText):
        """
        parse and write configuration to file SharedConfig.xml.
        """
        self.reinitialize()
        #Not used currently
        return self

class Certificates(object):

    """
    Object containing certificates of host and provisioned user.
    """
    def __init__(self, jsonText=None):
        self.libDir = CurrOS.GetLibDir()
        self.opensslCmd = CurrOS.GetOpensslCmd()
        if jsonText is not None:
            self.parse(jsonText)

    def reinitialize(self):
        """
        Reset members.
        """
        self.certs = []

    def parse(self, jsonText):
        self.reinitialize()
        certs = json.loads(jsonText)
        for cert in certs:
            self.certs.append(CertInfo(cert))

    def decrypt(self, xmlText):
        """
        Parse multiple certificates into seperate files.
        """
        self.reinitialize()
        xmlDoc = ET.fromstring(xmlText.strip())
        dataNode = FindFirstNode(xmlDoc, ".//Data")
        if dataNode is None:
            return 

        p7m = ("MIME-Version:1.0\n"
               "Content-Disposition: attachment; filename=\"{0}\"\n"
               "Content-Type: application/x-pkcs7-mime; name=\"{1}\"\n"
               "Content-Transfer-Encoding: base64\n"
               "\n"
               "{2}").format(P7MFile, P7MFile, dataNode.text)
        
        fileutil.SetFileContents(os.path.join(self.libDir, P7MFile), p7m)
        #decrypt certificates
        cmd = ("{0} cms -decrypt -in {1} -inkey {2} -recip {3}"
               "| {4} pkcs12 -nodes -password pass: -out {5}"
               "").format(self.opensslCmd, P7MFile, TransportPrivateFile, 
                               TransportCertFile, self.opensslCmd, PEMFile)
        shellutil.Run(cmd)
       
        #The parsing process use public key to match prv and crt.
        #TODO: Is there any way better to do so?
        buf = []
        beginCrt = False
        beginPrv = False
        prvs = {}
        thumbprints = {}
        index = 0
        certs = []
        with open(PEMFile) as pem:
            for line in pem.readlines():
                buf.append(line)
                if re.match(r'[-]+BEGIN.*KEY[-]+', line):
                    beginPrv = True
                elif re.match(r'[-]+BEGIN.*CERTIFICATE[-]+', line):
                    beginCrt = True
                elif re.match(r'[-]+END.*KEY[-]+', line):
                    tmpFile = self.writeToTempFile(index, 'prv', buf)
                    pub = CurrOS.GetPubKeyFromPrv(tmpFile)
                    prvs[pub] = tmpFile
                    buf = []
                    index += 1
                    beginPrv = False
                elif re.match(r'[-]+END.*CERTIFICATE[-]+', line):
                    tmpFile = self.writeToTempFile(index, 'crt', buf)
                    pub = CurrOS.GetPubKeyFromCrt(tmpFile)
                    thumbprint = CurrOS.GetThumbprintFromCrt(tmpFile)
                    thumbprints[pub] = thumbprint
                    #Rename crt with thumbprint as the file name 
                    crt = "{0}.crt".format(thumbprint)
                    certs.append({
                        "name":None,
                        "crt":crt,
                        "prv":None,
                        "thumbprint":thumbprint
                    })
                    os.rename(tmpFile, os.path.join(self.libDir, crt))
                    buf = []
                    index += 1
                    beginCrt = False

        #Rename prv key with thumbprint as the file name
        for pubkey in prvs:
            thumbprint = thumbprints[pubkey]
            if thumbprint:
                tmpFile = prvs[pubkey]
                prv = "{0}.prv".format(thumbprint)
                os.rename(tmpFile, os.path.join(self.libDir, prv))
                cert = filter(lambda x : x["thumbprint"] == thumbprint, 
                              certs)[0]
                cert["prv"] = prv

        for cert in certs:
            self.certs.append(CertInfo(cert))
        return json.dumps(certs)

    def getCerts(self):
        return self.certs

    def writeToTempFile(self, index, suffix, buf):
        fileName = os.path.join(self.libDir, "{0}.{1}".format(index, suffix))
        with open(fileName, 'w') as tmp:
            tmp.writelines(buf)
        return fileName

class ExtensionsConfig(object):
    """
    parse ExtensionsConfig, downloading and unpacking them to /var/lib/waagent.
    Install if <enabled>true</enabled>, remove if it is set to false.
    """

    def __init__(self, xmlText):
        self.parse(xmlText)

    def reinitialize(self):
        """
        Reset members.
        """
        self.extensions = []
        self.manifestUris = {}
        self.statusUploadBlob = None
    
    def getExtensions(self):
        return self.extensions

    def getManifestUri(self, name):
        return self.manifestUris[name]

    def getStatusUploadBlob(self):
        return self.statusUploadBlob
    
    def parse(self, xmlText):
        """
        Write configuration to file ExtensionsConfig.xml.
        """
        self.reinitialize()
        logger.Verbose("Extensions Config: {0}", xmlText)
        xmlDoc = ET.fromstring(xmlText.strip())
        extensions = FindAllNodes(xmlDoc, ".//Plugins/Plugin")      
        settings = FindAllNodes(xmlDoc, ".//PluginSettings/Plugin")

        for extension in extensions:
            ext = {}
            properties = {}
            runtimeSettings = {}
            handlerSettings = {}

            name = extension.attrib["name"]
            version = extension.attrib["version"]
            location = extension.attrib["location"]
            failoverLocation = extension.attrib["failoverlocation"]
            autoUpgrade = extension.attrib["autoUpgrade"]
            upgradePolicy = "auto" if autoUpgrade == "true" else None
            state = extension.attrib["state"]
            setting = filter(lambda x: x.attrib["name"] == name 
                             and x.attrib["version"] == version,
                             settings)
            runtimeSettingsNode = FindFirstNode(settings[0], ("RuntimeSettings"))
            seqNo = runtimeSettingsNode.attrib["seqNo"]
            runtimeSettingsStr = runtimeSettingsNode.text
            runtimeSettingsDataList = json.loads(runtimeSettingsStr)
            runtimeSettingsData = runtimeSettingsDataList["runtimeSettings"][0]
            handlerSettingsData = runtimeSettingsData["handlerSettings"]
            publicSettings = handlerSettingsData["publicSettings"]
            privateSettings = handlerSettingsData["protectedSettings"]
            thumbprint = handlerSettingsData["protectedSettingsCertThumbprint"]

            ext["name"] = name
            properties["version"] = version
            properties["versionUris"] = []
            properties["upgrade-policy"] = upgradePolicy
            properties["state"] = state
            handlerSettings["sequenceNumber"] = seqNo
            handlerSettings["publicSettings"] = publicSettings
            handlerSettings["privateSettings"] = privateSettings
            handlerSettings["certificateThumbprint"] = thumbprint

            runtimeSettings["handlerSettings"] = handlerSettings
            properties["runtimeSettings"] = runtimeSettings
            ext["properties"] = properties
            self.extensions.append(ExtensionInfo(ext))
            self.manifestUris[name] = (location, failoverLocation)
        self.statusUploadBlob = (FindFirstNode(xmlDoc,"StatusUploadBlob")).text
        return self

class ExtensionManifest(object):
    def __init__(self, xmlText):
        self.xmlText = xmlText
        self.versionUris = []
        self.parse(xmlText)

    def parse(self, xmlText):
        logger.Verbose("Extension manifest:{0}", xmlText)
        xmlDoc = ET.fromstring(xmlText.strip())
        packages = FindAllNodes(xmlDoc, ".//Plugins/Plugin")
        for package in packages:
            version = FindFirstNode(package, "Version").text
            uris = filter(lambda x : x.text, FindAllNodes(package, "Uri"))
            self.versionUris.append({
                "version":version,
                "uris":uris
            })

    def update(self, ext):
        ext.data["properties"]["versionUris"] = self.versionUris

