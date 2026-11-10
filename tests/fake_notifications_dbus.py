"""An isolated real D-Bus endpoint; never renders a desktop banner."""
import json
from pathlib import Path
import sys
from gi.repository import Gio, GLib

ready, capture, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
xml = '''<node><interface name="org.freedesktop.Notifications">
<method name="GetCapabilities"><arg type="as" direction="out"/></method>
<method name="GetServerInformation"><arg type="s" direction="out"/><arg type="s" direction="out"/><arg type="s" direction="out"/><arg type="s" direction="out"/></method>
<method name="Notify"><arg type="s" direction="in"/><arg type="u" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="s" direction="in"/><arg type="as" direction="in"/><arg type="a{sv}" direction="in"/><arg type="i" direction="in"/><arg type="u" direction="out"/></method>
<method name="CloseNotification"><arg type="u" direction="in"/></method>
</interface></node>'''
connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
pending = []
def call(_connection, _sender, _path, _interface, method, parameters, invocation):
    if method == "GetCapabilities": invocation.return_value(GLib.Variant("(as)", (["body", "body-markup"],)))
    elif method == "GetServerInformation": invocation.return_value(GLib.Variant("(ssss)", ("Isolated fixture", "Paveway tests", "1", "1.2")))
    elif method == "Notify":
        data = parameters.unpack()
        with capture.open("a") as stream: stream.write(json.dumps({"app": data[0], "summary": data[3], "body": data[4], "expire_timeout": data[7]}) + "\n")
        if mode == "hang": pending.append(invocation)
        elif mode == "reject": invocation.return_dbus_error("org.freedesktop.Notifications.Error.Rejected", "Fixture rejection")
        else: invocation.return_value(GLib.Variant("(u)", (1,)))
    else: invocation.return_value(GLib.Variant("()", ()))
connection.register_object("/org/freedesktop/Notifications", Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0], call, None, None)
Gio.bus_own_name_on_connection(connection, "org.freedesktop.Notifications", Gio.BusNameOwnerFlags.NONE,
    lambda *_: ready.touch(), lambda *_: None)
GLib.MainLoop().run()
