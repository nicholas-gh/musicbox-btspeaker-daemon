#!/usr/bin/python
"""
A script to run on a raspberry pi, looking out for a bluetooth speaker
to appear. When we manage to connect to it, start mopidy playing -
also notify Hue.
"""

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from phue import Bridge
import argparse
import daemon
import daemon.pidlockfile
import gobject
import logging
import logging.handlers
import mpd
import os
import requests
import sys

parser = argparse.ArgumentParser(description='Watch for the speaker box...')
parser.add_argument('--stay', action='store_true', help='do not detach')
parser.add_argument('--btaddr', help='Bluetooth address of your speaker - already pared', required=True)
parser.add_argument('--playlist', help='Mopidy playlist name to start', required=True)
parser.add_argument('--hue', help='Hue bridge IP')
parser.add_argument('--light', help='Hue light name or number')
parser.add_argument('--iftttkey', help='IFTTT Maker Key')
parser.add_argument('--iftttevent_boot', help='IFTTT Maker Event for Boot')
parser.add_argument('--iftttevent_play', help='IFTTT Maker Event for Playing')
parser.add_argument('--iftttevent_stop', help='IFTTT Maker Event for Stopping')
parser.add_argument('--pidfile', help='PID filename', default='/var/run/watch_for_speaker.pid')
args = parser.parse_args()

# It took a while to get the bluez/dbus functionality working - 
# so for reference, I used
#bluez 4.99-2
#bluez-alsa:armhf 4.99-2
#bluez-gstreamer 4.99-2
#bluez-tools 0.1.38+git662e-3
#dbus 1.6.8-1+deb7u6
#dbus-x11 1.6.8-1+deb7u6
#libdbus-1-3:armhf 1.6.8-1+deb7u6
#libdbus-glib-1-2:armhf 0.100.2-1
#python-dbus 1.1.1-1
#python-dbus-dev 1.1.1-1
# on http://www.pimusicbox.com/ 0.6

# and then to make mopidy cope with the speaker going missing when you switch it off,
# I used this output, rather than alsasink:
# output = sbcenc ! a2dpsink device=AA:AA:AA:AA:AA:AA async-handling=true

# I'd love to get the battery level, then most IFTTT if it's low; but looks like
# I would need to patch bluez for it :-/

# bluetoothd[4480]: audio/headset.c:handle_event() Received AT+IPHONEACCEV=2,1,8,2,0
# bluetoothd[4480]: audio/headset.c:apple_command() Got Apple command: AT+IPHONEACCEV=2,1,8,2,0
# https://developer.apple.com/hardwaredrivers/BluetoothDesignGuidelines.pdf 

class Watcher(object):
    def __init__(self, 
                 bus,
                 btaddr, 
                 playlist_name,
                 hue_ip, hue_lamp, 
                 ifttt_maker_key, ifttt_maker_events,
                 logger):
        self.bus = bus
        self.btaddr = btaddr
        self.logger = logger
        self.playlist_name = playlist_name
        self.ifttt_maker_key = ifttt_maker_key
        self.ifttt_maker_events = ifttt_maker_events
        if hue_ip:
            self.hue = Bridge(hue_ip)
            self.light = hue_lamp
        else:
            self.hue = None

        adapterPath = dbus.Interface(self.bus.get_object('org.bluez', '/'), 
                                     'org.bluez.Manager').DefaultAdapter()
        self.adapter = dbus.Interface(self.bus.get_object('org.bluez', adapterPath),
                                      'org.bluez.Adapter')
        self.logger.info("Starting up, using bluetooth adapter %s for speakers %s", 
                         adapterPath, self.btaddr)

        headset = self.bus.get_object("org.bluez", self.adapter.FindDevice(self.btaddr))
        headset.connect_to_signal("Connected", self.connected, interface_keyword='iface')
        headset.connect_to_signal("Disconnected", self.disconnected, interface_keyword='iface')

        self._speculative_event_id = gobject.timeout_add(500, self.speculative_connect)

        self._ifttt('boot')

    def _ifttt(self, event):
        if self.ifttt_maker_key and self.ifttt_maker_events[event]:
            try:
                requests.get("https://maker.ifttt.com/trigger/%s/with/key/%s" % (self.ifttt_maker_events[event],
                                                                                 self.ifttt_maker_key))
            except (requests.exceptions.ConnectTimeout,
                    requests.exceptions.ReadTimeout) as e:
                self.logger.exception("Failed to post to IFTTT")

            
    def speculative_connect(self):
        self.logger.debug("Trying speculative audio.Connect()")
        audio = dbus.Interface(self.bus.get_object("org.bluez", self.adapter.FindDevice(self.btaddr)), 
                               "org.bluez.Audio")
        try:
            audio.Connect()
        except dbus.exceptions.DBusException, e:
            e_name = e.get_dbus_name()
            if e_name == "org.bluez.Error.AlreadyConnected":
                self.logger.debug("Was already connected")
            elif e_name == "org.bluez.Error.InProgress":
                self.logger.debug("InProgress?")
            elif e_name == "org.bluez.Error.Failed":
                self.logger.debug("Failed to connect (not switched on?)")
            else:
                raise
        return True

    def connected(self, iface=None):
        if "org.bluez.AudioSink" == iface:
            gobject.source_remove(self._speculative_event_id)
            self._speculative_event_id = None
            self.logger.info("Speaker connected - playing sound")
            self.hue.set_light(self.light, dict(bri=254, 
                                                sat=120, 
                                                hue=100,
                                                on=True, 
                                                transitiontime=5))
            c = mpd.MPDClient()
            c.connect("localhost", 6600)
            c.clear()
            c.load(self.playlist_name)
            c.shuffle()
            c.play()
            c.disconnect()
            self._ifttt('play')

    def disconnected(self, iface=None):
        if "org.bluez.AudioSink" == iface:
            self.logger.info("Speaker went AWOL, stopping sound")
            self.hue.set_light(self.light, 'on', False)
            c = mpd.MPDClient()
            c.connect("localhost", 6600)
            c.stop()
            c.disconnect()
            if self._speculative_event_id is None:
                self._speculative_event_id = gobject.timeout_add(500, self.speculative_connect)
            self._ifttt('stop')

def main():
    logger = logging.getLogger('watch_for_speaker')
    logger.setLevel(logging.DEBUG)
    if args.stay:
        handler = logging.StreamHandler(sys.stdout)
    else:
        handler = logging.handlers.SysLogHandler(address = '/dev/log')
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    dbus_loop = DBusGMainLoop()
    bus = dbus.SystemBus(mainloop=dbus_loop)
    
    watcher = Watcher(bus, 
                      args.btaddr,
                      args.playlist,
                      args.hue, args.light,
                      args.iftttkey, dict(boot = args.iftttevent_boot, 
                                          play = args.iftttevent_play,  
                                          stop = args.iftttevent_stop),
                      logger)
    
    loop = gobject.MainLoop()
    loop.run()

if args.stay:
    main()
else:
    with daemon.DaemonContext(pidfile = daemon.pidlockfile.TimeoutPIDLockFile(args.pidfile,
                                                                            acquire_timeout = 15)):
        main()
