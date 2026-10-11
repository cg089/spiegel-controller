#!/usr/bin/env python3
"""
RTSP-Server + minimaler ONVIF-Dienst (Profile S) für UniFi Protect.

Bestandteile:
  1. GStreamer RTSP-Server       -> rtsp://<IP>:8554/stream
  2. ONVIF Device/Media Service  -> http://<IP>:8080/onvif/device_service
  3. WS-Discovery (UDP 3702)     -> Protect findet die Kamera automatisch
  4. Snapshot                    -> http://<IP>:8080/snapshot.jpg

Nur Python-Standardbibliothek + python3-gi / GStreamer nötig.
Zugangsdaten und Gerätename kommen aus der Umgebung (.env, siehe example.env).
"""
import base64
import datetime
import hashlib
import hmac
import os
import socket
import struct
import sys
import threading
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote
from xml.sax.saxutils import escape

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
from gi.repository import Gst, GstRtspServer, GLib

# ===================== Konfiguration =====================
VIDEO_DEVICE = "/dev/video0"
WIDTH, HEIGHT, FPS = 1920, 1080, 30
BITRATE_KBIT = 2000          # für 1080p30 eher 4000-6000 empfehlenswert
GOP = 30                     # Keyframe-Abstand in Frames

RTSP_PORT = 8554
RTSP_PATH = "/stream"
ONVIF_PORT = 8080

# Zugangsdaten, die du in UniFi Protect bei der Übernahme einträgst (aus .env)
USERNAME = os.environ.get("ONVIF_USERNAME", "admin")
PASSWORD = os.environ.get("ONVIF_PASSWORD", "")
ONVIF_AUTH_REQUIRED = os.environ.get("ONVIF_AUTH_REQUIRED", "1") != "0"   # bei Adoptionsproblemen testweise 0
RTSP_AUTH = os.environ.get("RTSP_AUTH", "0") == "1"   # 1 = RTSP verlangt ebenfalls Benutzer/Passwort (Basic)

DEVICE_NAME = os.environ.get("ONVIF_DEVICE_NAME") or socket.gethostname()
MANUFACTURER = "Spiegel"
MODEL = "USB-MJPEG-Cam"
FIRMWARE = "1.0.0"
MAC_OVERRIDE = None          # z. B. "aa:bb:cc:dd:ee:ff", sonst automatisch
# =========================================================

NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
NS_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
NS_WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"

Gst.init(None)


# ---------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------
def primary_interface():
    """Erste 'echte' Netzwerkschnittstelle samt MAC ermitteln."""
    skip = ("lo", "docker", "veth", "br-", "virbr", "tun", "tap", "wg")
    candidates = []
    try:
        for name in sorted(os.listdir("/sys/class/net")):
            if name.startswith(skip):
                continue
            try:
                with open(f"/sys/class/net/{name}/address") as f:
                    mac = f.read().strip()
                with open(f"/sys/class/net/{name}/operstate") as f:
                    state = f.read().strip()
            except OSError:
                continue
            if mac and mac != "00:00:00:00:00:00":
                candidates.append((state != "up", name, mac))
    except OSError:
        pass
    if candidates:
        candidates.sort()
        return candidates[0][1], candidates[0][2]
    node = uuid.getnode()
    return "eth0", ":".join(f"{(node >> s) & 0xff:02x}" for s in range(40, -1, -8))


IFACE, MAC = primary_interface()
if MAC_OVERRIDE:
    MAC = MAC_OVERRIDE.lower()
SERIAL = MAC.replace(":", "").upper()
DEVICE_UUID = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"onvif-diy-{SERIAL}"))
HOSTNAME = socket.gethostname()
INSTANCE_ID = int(datetime.datetime.now().timestamp())

SCOPES = " ".join([
    "onvif://www.onvif.org/type/video_encoder",
    "onvif://www.onvif.org/type/NetworkVideoTransmitter",
    "onvif://www.onvif.org/Profile/Streaming",
    "onvif://www.onvif.org/Profile/S",
    f"onvif://www.onvif.org/name/{quote(DEVICE_NAME)}",
    f"onvif://www.onvif.org/hardware/{quote(MODEL)}",
    f"onvif://www.onvif.org/MAC/{MAC}",
])


def local_ip_for(dest="8.8.8.8"):
    """Lokale IP, über die 'dest' erreicht wird."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((dest, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def urls(ip):
    return {
        "device": f"http://{ip}:{ONVIF_PORT}/onvif/device_service",
        "media": f"http://{ip}:{ONVIF_PORT}/onvif/media_service",
        "rtsp": f"rtsp://{ip}:{RTSP_PORT}{RTSP_PATH}",
        "snapshot": f"http://{ip}:{ONVIF_PORT}/snapshot.jpg",
    }


def envelope(body):
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{NS_SOAP}"'
        ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
        ' xmlns:trt="http://www.onvif.org/ver10/media/wsdl"'
        ' xmlns:tt="http://www.onvif.org/ver10/schema"'
        ' xmlns:ter="http://www.onvif.org/ver10/error">'
        f'<s:Body>{body}</s:Body></s:Envelope>'
    )


def fault(code, subcode, reason):
    return envelope(
        f'<s:Fault><s:Code><s:Value>s:{code}</s:Value>'
        f'<s:Subcode><s:Value>{subcode}</s:Value></s:Subcode></s:Code>'
        f'<s:Reason><s:Text xml:lang="en">{escape(reason)}</s:Text></s:Reason></s:Fault>'
    )


def check_ws_auth(root):
    """WS-Security UsernameToken (PasswordDigest oder PasswordText) prüfen."""
    ns = {"wsse": NS_WSSE, "wsu": NS_WSU}
    tok = root.find(".//wsse:UsernameToken", ns)
    if tok is None:
        return False
    user = tok.findtext("wsse:Username", "", ns)
    pw_el = tok.find("wsse:Password", ns)
    if user != USERNAME or pw_el is None:
        return False
    given = (pw_el.text or "").strip()
    ptype = pw_el.get("Type", "")
    if ptype.endswith("#PasswordText"):
        return hmac.compare_digest(given, PASSWORD)
    nonce = tok.findtext("wsse:Nonce", "", ns)
    created = tok.findtext("wsu:Created", "", ns)
    try:
        nonce_bytes = base64.b64decode(nonce)
    except Exception:
        return False
    digest = base64.b64encode(
        hashlib.sha1(nonce_bytes + created.encode() + PASSWORD.encode()).digest()
    ).decode()
    if hmac.compare_digest(digest, given):
        return True
    # kein Type-Attribut -> laut Spezifikation Klartext
    return not ptype and hmac.compare_digest(given, PASSWORD)


# ---------------------------------------------------------
# ONVIF-XML-Bausteine
# ---------------------------------------------------------
def xml_video_source_config(tag):
    return (
        f'<{tag} token="VideoSourceConfig_1">'
        '<tt:Name>VideoSourceConfig</tt:Name><tt:UseCount>1</tt:UseCount>'
        '<tt:SourceToken>VideoSource_1</tt:SourceToken>'
        f'<tt:Bounds x="0" y="0" width="{WIDTH}" height="{HEIGHT}"/>'
        f'</{tag}>'
    )


def xml_video_encoder_config(tag):
    return (
        f'<{tag} token="VideoEncoder_1">'
        '<tt:Name>VideoEncoder</tt:Name><tt:UseCount>1</tt:UseCount>'
        '<tt:Encoding>H264</tt:Encoding>'
        f'<tt:Resolution><tt:Width>{WIDTH}</tt:Width><tt:Height>{HEIGHT}</tt:Height></tt:Resolution>'
        '<tt:Quality>5</tt:Quality>'
        f'<tt:RateControl><tt:FrameRateLimit>{FPS}</tt:FrameRateLimit>'
        f'<tt:EncodingInterval>1</tt:EncodingInterval><tt:BitrateLimit>{BITRATE_KBIT}</tt:BitrateLimit></tt:RateControl>'
        f'<tt:H264><tt:GovLength>{GOP}</tt:GovLength><tt:H264Profile>Main</tt:H264Profile></tt:H264>'
        '<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address>'
        '<tt:Port>0</tt:Port><tt:TTL>1</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast>'
        '<tt:SessionTimeout>PT60S</tt:SessionTimeout>'
        f'</{tag}>'
    )


def xml_profile(tag):
    return (
        f'<{tag} token="Profile_1" fixed="true"><tt:Name>MainStream</tt:Name>'
        + xml_video_source_config("tt:VideoSourceConfiguration")
        + xml_video_encoder_config("tt:VideoEncoderConfiguration")
        + f'</{tag}>'
    )


def xml_media_uri(uri):
    return (
        f'<trt:MediaUri><tt:Uri>{escape(uri)}</tt:Uri>'
        '<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>'
        '<tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>'
        '<tt:Timeout>PT0S</tt:Timeout></trt:MediaUri>'
    )


DEVICE_SERVICE_CAPS = (
    '<tds:Capabilities>'
    '<tds:Network IPFilter="false" ZeroConfiguration="false" IPVersion6="false" DynDNS="false"/>'
    '<tds:Security UsernameToken="true" HttpDigest="false"/>'
    '<tds:System DiscoveryResolve="false" DiscoveryBye="true" RemoteDiscovery="false" '
    'SystemBackup="false" SystemLogging="false" FirmwareUpgrade="false"/>'
    '</tds:Capabilities>'
)
MEDIA_SERVICE_CAPS = (
    '<trt:Capabilities SnapshotUri="true" Rotation="false" VideoSourceMode="false" OSD="false">'
    '<trt:ProfileCapabilities MaximumNumberOfProfiles="1"/>'
    '<trt:StreamingCapabilities RTPMulticast="false" RTP_TCP="true" RTP_RTSP_TCP="true" NonAggregateControl="false"/>'
    '</trt:Capabilities>'
)


# ---------------------------------------------------------
# Device Service
# ---------------------------------------------------------
def d_get_system_date_and_time(req, ip):
    n = datetime.datetime.now(datetime.timezone.utc)
    return (
        '<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>'
        '<tt:DateTimeType>NTP</tt:DateTimeType><tt:DaylightSavings>false</tt:DaylightSavings>'
        '<tt:TimeZone><tt:TZ>UTC0</tt:TZ></tt:TimeZone>'
        f'<tt:UTCDateTime><tt:Time><tt:Hour>{n.hour}</tt:Hour><tt:Minute>{n.minute}</tt:Minute>'
        f'<tt:Second>{n.second}</tt:Second></tt:Time><tt:Date><tt:Year>{n.year}</tt:Year>'
        f'<tt:Month>{n.month}</tt:Month><tt:Day>{n.day}</tt:Day></tt:Date></tt:UTCDateTime>'
        '</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>'
    )


def d_get_device_information(req, ip):
    return (
        '<tds:GetDeviceInformationResponse>'
        f'<tds:Manufacturer>{escape(MANUFACTURER)}</tds:Manufacturer>'
        f'<tds:Model>{escape(MODEL)}</tds:Model>'
        f'<tds:FirmwareVersion>{escape(FIRMWARE)}</tds:FirmwareVersion>'
        f'<tds:SerialNumber>{SERIAL}</tds:SerialNumber>'
        f'<tds:HardwareId>{escape(MODEL)}</tds:HardwareId>'
        '</tds:GetDeviceInformationResponse>'
    )


def d_get_capabilities(req, ip):
    u = urls(ip)
    return (
        '<tds:GetCapabilitiesResponse><tds:Capabilities>'
        f'<tt:Device><tt:XAddr>{u["device"]}</tt:XAddr>'
        '<tt:Network><tt:IPFilter>false</tt:IPFilter><tt:ZeroConfiguration>false</tt:ZeroConfiguration>'
        '<tt:IPVersion6>false</tt:IPVersion6><tt:DynDNS>false</tt:DynDNS></tt:Network>'
        '<tt:System><tt:DiscoveryResolve>false</tt:DiscoveryResolve><tt:DiscoveryBye>true</tt:DiscoveryBye>'
        '<tt:RemoteDiscovery>false</tt:RemoteDiscovery><tt:SystemBackup>false</tt:SystemBackup>'
        '<tt:SystemLogging>false</tt:SystemLogging><tt:FirmwareUpgrade>false</tt:FirmwareUpgrade>'
        '<tt:SupportedVersions><tt:Major>2</tt:Major><tt:Minor>5</tt:Minor></tt:SupportedVersions></tt:System>'
        '</tt:Device>'
        f'<tt:Media><tt:XAddr>{u["media"]}</tt:XAddr>'
        '<tt:StreamingCapabilities><tt:RTPMulticast>false</tt:RTPMulticast>'
        '<tt:RTP_TCP>true</tt:RTP_TCP><tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP></tt:StreamingCapabilities>'
        '</tt:Media>'
        '</tds:Capabilities></tds:GetCapabilitiesResponse>'
    )


def d_get_services(req, ip):
    u = urls(ip)
    include = any(
        el.tag.endswith("IncludeCapability") and (el.text or "").strip().lower() == "true"
        for el in req.iter()
    )

    def svc(ns, xaddr, caps):
        c = f'<tds:Capabilities>{caps}</tds:Capabilities>' if include else ""
        return (f'<tds:Service><tds:Namespace>{ns}</tds:Namespace><tds:XAddr>{xaddr}</tds:XAddr>{c}'
                '<tds:Version><tt:Major>2</tt:Major><tt:Minor>5</tt:Minor></tds:Version></tds:Service>')

    return (
        '<tds:GetServicesResponse>'
        + svc("http://www.onvif.org/ver10/device/wsdl", u["device"], DEVICE_SERVICE_CAPS)
        + svc("http://www.onvif.org/ver10/media/wsdl", u["media"], MEDIA_SERVICE_CAPS)
        + '</tds:GetServicesResponse>'
    )


def d_get_service_capabilities(req, ip):
    return f'<tds:GetServiceCapabilitiesResponse>{DEVICE_SERVICE_CAPS}</tds:GetServiceCapabilitiesResponse>'


def d_get_scopes(req, ip):
    items = "".join(
        f'<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>{escape(s)}</tt:ScopeItem></tds:Scopes>'
        for s in SCOPES.split()
    )
    return f'<tds:GetScopesResponse>{items}</tds:GetScopesResponse>'


def d_get_hostname(req, ip):
    return (
        '<tds:GetHostnameResponse><tds:HostnameInformation>'
        f'<tt:FromDHCP>false</tt:FromDHCP><tt:Name>{escape(HOSTNAME)}</tt:Name>'
        '</tds:HostnameInformation></tds:GetHostnameResponse>'
    )


def d_get_network_interfaces(req, ip):
    return (
        f'<tds:GetNetworkInterfacesResponse><tds:NetworkInterfaces token="{IFACE}">'
        '<tt:Enabled>true</tt:Enabled>'
        f'<tt:Info><tt:Name>{IFACE}</tt:Name><tt:HwAddress>{MAC}</tt:HwAddress><tt:MTU>1500</tt:MTU></tt:Info>'
        '<tt:IPv4><tt:Enabled>true</tt:Enabled><tt:Config>'
        f'<tt:FromDHCP><tt:Address>{ip}</tt:Address><tt:PrefixLength>24</tt:PrefixLength></tt:FromDHCP>'
        '<tt:DHCP>true</tt:DHCP></tt:Config></tt:IPv4>'
        '</tds:NetworkInterfaces></tds:GetNetworkInterfacesResponse>'
    )


def d_get_wsdl_url(req, ip):
    return '<tds:GetWsdlUrlResponse><tds:WsdlUrl>http://www.onvif.org/</tds:WsdlUrl></tds:GetWsdlUrlResponse>'


DEVICE_HANDLERS = {
    "GetSystemDateAndTime": d_get_system_date_and_time,
    "GetDeviceInformation": d_get_device_information,
    "GetCapabilities": d_get_capabilities,
    "GetServices": d_get_services,
    "GetServiceCapabilities": d_get_service_capabilities,
    "GetScopes": d_get_scopes,
    "GetHostname": d_get_hostname,
    "GetNetworkInterfaces": d_get_network_interfaces,
    "GetWsdlUrl": d_get_wsdl_url,
}


# ---------------------------------------------------------
# Media Service
# ---------------------------------------------------------
MEDIA_HANDLERS = {
    "GetServiceCapabilities": lambda r, ip:
        f'<trt:GetServiceCapabilitiesResponse>{MEDIA_SERVICE_CAPS}</trt:GetServiceCapabilitiesResponse>',
    "GetProfiles": lambda r, ip:
        f'<trt:GetProfilesResponse>{xml_profile("trt:Profiles")}</trt:GetProfilesResponse>',
    "GetProfile": lambda r, ip:
        f'<trt:GetProfileResponse>{xml_profile("trt:Profile")}</trt:GetProfileResponse>',
    "GetVideoSources": lambda r, ip: (
        '<trt:GetVideoSourcesResponse><trt:VideoSources token="VideoSource_1">'
        f'<tt:Framerate>{FPS}</tt:Framerate>'
        f'<tt:Resolution><tt:Width>{WIDTH}</tt:Width><tt:Height>{HEIGHT}</tt:Height></tt:Resolution>'
        '</trt:VideoSources></trt:GetVideoSourcesResponse>'),
    "GetVideoSourceConfigurations": lambda r, ip:
        '<trt:GetVideoSourceConfigurationsResponse>'
        + xml_video_source_config("trt:Configurations")
        + '</trt:GetVideoSourceConfigurationsResponse>',
    "GetVideoSourceConfiguration": lambda r, ip:
        '<trt:GetVideoSourceConfigurationResponse>'
        + xml_video_source_config("trt:Configuration")
        + '</trt:GetVideoSourceConfigurationResponse>',
    "GetVideoEncoderConfigurations": lambda r, ip:
        '<trt:GetVideoEncoderConfigurationsResponse>'
        + xml_video_encoder_config("trt:Configurations")
        + '</trt:GetVideoEncoderConfigurationsResponse>',
    "GetVideoEncoderConfiguration": lambda r, ip:
        '<trt:GetVideoEncoderConfigurationResponse>'
        + xml_video_encoder_config("trt:Configuration")
        + '</trt:GetVideoEncoderConfigurationResponse>',
    "GetVideoEncoderConfigurationOptions": lambda r, ip: (
        '<trt:GetVideoEncoderConfigurationOptionsResponse><trt:Options>'
        '<tt:QualityRange><tt:Min>1</tt:Min><tt:Max>10</tt:Max></tt:QualityRange>'
        f'<tt:H264><tt:ResolutionsAvailable><tt:Width>{WIDTH}</tt:Width><tt:Height>{HEIGHT}</tt:Height></tt:ResolutionsAvailable>'
        '<tt:GovLengthRange><tt:Min>1</tt:Min><tt:Max>300</tt:Max></tt:GovLengthRange>'
        f'<tt:FrameRateRange><tt:Min>1</tt:Min><tt:Max>{FPS}</tt:Max></tt:FrameRateRange>'
        '<tt:EncodingIntervalRange><tt:Min>1</tt:Min><tt:Max>1</tt:Max></tt:EncodingIntervalRange>'
        '<tt:H264ProfilesSupported>Main</tt:H264ProfilesSupported></tt:H264>'
        '</trt:Options></trt:GetVideoEncoderConfigurationOptionsResponse>'),
    "GetStreamUri": lambda r, ip:
        f'<trt:GetStreamUriResponse>{xml_media_uri(urls(ip)["rtsp"])}</trt:GetStreamUriResponse>',
    "GetSnapshotUri": lambda r, ip:
        f'<trt:GetSnapshotUriResponse>{xml_media_uri(urls(ip)["snapshot"])}</trt:GetSnapshotUriResponse>',
    # Kein Audio vorhanden -> leere Listen
    "GetAudioSources": lambda r, ip: '<trt:GetAudioSourcesResponse/>',
    "GetAudioSourceConfigurations": lambda r, ip: '<trt:GetAudioSourceConfigurationsResponse/>',
    "GetAudioEncoderConfigurations": lambda r, ip: '<trt:GetAudioEncoderConfigurationsResponse/>',
    # Änderungswünsche werden akzeptiert, aber ignoriert (feste Pipeline)
    "SetVideoEncoderConfiguration": lambda r, ip: '<trt:SetVideoEncoderConfigurationResponse/>',
    "SetSynchronizationPoint": lambda r, ip: '<trt:SetSynchronizationPointResponse/>',
}

# Aufrufe, die laut ONVIF ohne Anmeldung erlaubt sind (+ GetCapabilities aus Kulanz)
NO_AUTH = {"GetSystemDateAndTime", "GetServices", "GetServiceCapabilities",
           "GetWsdlUrl", "GetEndpointReference", "GetCapabilities"}


# ---------------------------------------------------------
# HTTP-Server (SOAP + Snapshot)
# ---------------------------------------------------------
latest_snapshot = None   # bytes, wird von der GStreamer-Pipeline befüllt


class OnvifHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # eigene, knappe Logs unten

    def _send(self, status, body, ctype="application/soap+xml; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.split("?")[0] == "/snapshot.jpg":
            snap = latest_snapshot
            if snap:
                self._send(200, snap, "image/jpeg")
            else:
                self._send(503, b"no snapshot yet (stream not running)", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        ip = self.connection.getsockname()[0]
        path = self.path.split("?")[0]

        try:
            root = ET.fromstring(raw)
            body = next(el for el in root if el.tag.endswith("Body"))
            req = next(iter(body))
            action = req.tag.split("}")[-1]
        except Exception:
            self._send(400, fault("Sender", "ter:WellFormed", "Malformed SOAP request"))
            return

        if path.endswith("media_service"):
            handlers = MEDIA_HANDLERS
        else:
            handlers = DEVICE_HANDLERS

        if ONVIF_AUTH_REQUIRED and action not in NO_AUTH and not check_ws_auth(root):
            print(f"[ONVIF] {self.client_address[0]} {action} -> NotAuthorized")
            self._send(400, fault("Sender", "ter:NotAuthorized", "Sender not authorized"))
            return

        handler = handlers.get(action)
        if handler is None:
            print(f"[ONVIF] {self.client_address[0]} {action} -> nicht unterstützt")
            self._send(500, fault("Receiver", "ter:ActionNotSupported", f"{action} not supported"))
            return

        print(f"[ONVIF] {self.client_address[0]} {action}")
        self._send(200, envelope(handler(req, ip)))


# ---------------------------------------------------------
# WS-Discovery
# ---------------------------------------------------------
WSD_GROUP, WSD_PORT = "239.255.255.250", 3702
_msg_counter = 0


def wsd_message(action, to, body, relates_to=None):
    global _msg_counter
    _msg_counter += 1
    rel = f"<a:RelatesTo>{escape(relates_to)}</a:RelatesTo>" if relates_to else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{NS_SOAP}"'
        ' xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"'
        ' xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"'
        ' xmlns:dn="http://www.onvif.org/ver10/network/wsdl"'
        ' xmlns:tds="http://www.onvif.org/ver10/device/wsdl">'
        f'<s:Header><a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>{rel}'
        f'<a:To>{to}</a:To>'
        f'<a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/{action}</a:Action>'
        f'<d:AppSequence InstanceId="{INSTANCE_ID}" MessageNumber="{_msg_counter}"/>'
        f'</s:Header><s:Body>{body}</s:Body></s:Envelope>'
    )


def wsd_endpoint(ip):
    return (
        f'<a:EndpointReference><a:Address>urn:uuid:{DEVICE_UUID}</a:Address></a:EndpointReference>'
        '<d:Types>dn:NetworkVideoTransmitter tds:Device</d:Types>'
        f'<d:Scopes>{escape(SCOPES)}</d:Scopes>'
        f'<d:XAddrs>{urls(ip)["device"]}</d:XAddrs>'
        '<d:MetadataVersion>1</d:MetadataVersion>'
    )


def discovery_loop():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.bind(("", WSD_PORT))
    mreq = struct.pack("4s4s", socket.inet_aton(WSD_GROUP), socket.inet_aton("0.0.0.0"))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)

    # Hello beim Start ankündigen
    hello = wsd_message("Hello", "urn:schemas-xmlsoap-org:ws:2005:04:discovery",
                        f"<d:Hello>{wsd_endpoint(local_ip_for())}</d:Hello>")
    sock.sendto(hello.encode(), (WSD_GROUP, WSD_PORT))

    while True:
        data, addr = sock.recvfrom(65535)
        try:
            root = ET.fromstring(data)
        except ET.ParseError:
            continue
        if not any(el.tag.endswith("}Probe") for el in root.iter()):
            continue
        types = " ".join(el.text or "" for el in root.iter() if el.tag.endswith("}Types"))
        if types.strip() and "NetworkVideoTransmitter" not in types and "Device" not in types:
            continue
        msg_id = next((el.text for el in root.iter() if el.tag.endswith("}MessageID")), "")
        ip = local_ip_for(addr[0])
        resp = wsd_message(
            "ProbeMatches",
            "http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous",
            f"<d:ProbeMatches><d:ProbeMatch>{wsd_endpoint(ip)}</d:ProbeMatch></d:ProbeMatches>",
            relates_to=msg_id,
        )
        sock.sendto(resp.encode(), addr)
        print(f"[WS-Discovery] Probe von {addr[0]} beantwortet")


# ---------------------------------------------------------
# RTSP-Server (GStreamer)
# ---------------------------------------------------------
class RTSPFactory(GstRtspServer.RTSPMediaFactory):
    def __init__(self):
        super().__init__()
        # MJPEG 1920x1080@30 -> H.264 (VAAPI); Nebenzweig mit 1 fps als JPEG-Snapshot
        self.set_launch((
            f"v4l2src device={VIDEO_DEVICE} do-timestamp=true ! "
            f"image/jpeg,width={WIDTH},height={HEIGHT},framerate={FPS}/1 ! "
            "jpegdec ! tee name=t "
            "t. ! queue ! videoconvert ! video/x-raw,format=NV12 ! "
            f"vaapih264enc bitrate={BITRATE_KBIT} keyframe-period={GOP} ! "
            "video/x-h264,profile=main ! "
            "h264parse config-interval=1 ! "
            "rtph264pay name=pay0 pt=96 config-interval=1 "
            "t. ! queue leaky=downstream max-size-buffers=2 ! "
            "videorate drop-only=true ! video/x-raw,framerate=1/1 ! "
            "videoconvert ! jpegenc quality=85 ! "
            "appsink name=snap emit-signals=true max-buffers=1 drop=true sync=false async=false"
        ))
        self.set_shared(True)


def on_snapshot_sample(sink):
    global latest_snapshot
    sample = sink.emit("pull-sample")
    if sample:
        buf = sample.get_buffer()
        ok, info = buf.map(Gst.MapFlags.READ)
        if ok:
            latest_snapshot = bytes(info.data)
            buf.unmap(info)
    return Gst.FlowReturn.OK


def on_media_configure(factory, media):
    snap = media.get_element().get_by_name("snap")
    if snap:
        snap.connect("new-sample", on_snapshot_sample)


def main():
    server = GstRtspServer.RTSPServer()
    server.set_service(str(RTSP_PORT))
    factory = RTSPFactory()
    factory.connect("media-configure", on_media_configure)

    if RTSP_AUTH:
        if not PASSWORD:
            sys.exit("❌ RTSP_AUTH=1, aber ONVIF_PASSWORD ist nicht gesetzt")
        auth = GstRtspServer.RTSPAuth()
        token = GstRtspServer.RTSPToken()
        token.set_string("media.factory.role", "user")
        auth.add_basic(GstRtspServer.RTSPAuth.make_basic(USERNAME, PASSWORD), token)
        server.set_auth(auth)
        perms = GstRtspServer.RTSPPermissions()
        perms.add_permission_for_role("user", "media.factory.access", True)
        perms.add_permission_for_role("user", "media.factory.construct", True)
        factory.set_permissions(perms)

    server.get_mount_points().add_factory(RTSP_PATH, factory)
    server.attach(None)

    u = urls(local_ip_for())
    print(f"✅ RTSP:     {u['rtsp']}")

    # Ohne Passwort kein ONVIF mit Anmeldung -> nur RTSP anbieten
    httpd = None
    if PASSWORD or not ONVIF_AUTH_REQUIRED:
        httpd = ThreadingHTTPServer(("", ONVIF_PORT), OnvifHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        threading.Thread(target=discovery_loop, daemon=True).start()
        print(f"✅ ONVIF:    {u['device']}")
        print(f"✅ Snapshot: {u['snapshot']}")
        print(f"   Gerät:    {DEVICE_NAME}  MAC {MAC}  UUID {DEVICE_UUID}")
    else:
        print("⚠️  ONVIF deaktiviert: ONVIF_PASSWORD ist nicht gesetzt (.env)")

    try:
        GLib.MainLoop().run()
    except KeyboardInterrupt:
        if httpd:
            httpd.shutdown()


if __name__ == "__main__":
    main()
