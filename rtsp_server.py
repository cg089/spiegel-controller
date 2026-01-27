#!/usr/bin/env python3
import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstRtspServer', '1.0')
from gi.repository import Gst, GstRtspServer, GLib

Gst.init(None)

class RTSPFactory(GstRtspServer.RTSPMediaFactory):
    def __init__(self):
        super().__init__()
        # exakt angepasst auf deine Kamera: MJPEG 1920x1080@30fps
        self.set_launch((
            "v4l2src device=/dev/video0 ! "
            "image/jpeg,width=1920,height=1080,framerate=30/1 ! "
            "jpegdec ! videoconvert ! video/x-raw,format=NV12 ! "
            "vaapih264enc bitrate=2000 keyframe-period=30 ! "
            "h264parse config-interval=1 ! "
            "rtph264pay name=pay0 pt=96"
        ))
        self.set_shared(True)

server = GstRtspServer.RTSPServer()
factory = RTSPFactory()
server.get_mount_points().add_factory("/stream", factory)
server.attach(None)

print("✅ RTSP läuft unter: rtsp://<IP>:8554/stream")
GLib.MainLoop().run()
