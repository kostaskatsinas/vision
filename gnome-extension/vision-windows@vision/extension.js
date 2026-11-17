// Vision window helper - a GNOME Shell extension.
//
// WHY IT EXISTS
//   On GNOME Wayland no outside program may reach another app's window:
//   wmctrl and xdotool only see XWayland windows, and ":ACTIVE:" there means
//   "the last X11 window that had focus", which is usually a different app
//   from the one in front. That is how "minimize Spotify" ended up changing
//   some other window. Only the Shell itself can act on a window by identity,
//   so this extension does the acting and Vision just names the window.
//
// D-BUS (session bus, name org.gnome.Shell)
//   object    /org/gnome/Shell/Extensions/VisionWindows
//   interface org.gnome.Shell.Extensions.VisionWindows
//     List()            -> s   JSON array, one object per normal window
//     Act(t id, s what) -> b   what = minimize | maximize | unmaximize |
//                                     restore | activate
//     Version            u     bumped when List or Act change shape
//
// It does nothing until asked and holds no state between calls.

import Gio from 'gi://Gio';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const VERSION = 1;
const OBJECT_PATH = '/org/gnome/Shell/Extensions/VisionWindows';
const IFACE = `
<node>
  <interface name="org.gnome.Shell.Extensions.VisionWindows">
    <method name="List">
      <arg type="s" direction="out" name="windows"/>
    </method>
    <method name="Act">
      <arg type="t" direction="in" name="id"/>
      <arg type="s" direction="in" name="what"/>
      <arg type="b" direction="out" name="ok"/>
    </method>
    <property name="Version" type="u" access="read"/>
  </interface>
</node>`;

// Mutter 49 replaced get_maximized()/maximize(flags) with is_maximized() and
// a maximize() that takes no flags. Both shapes are handled here.
function isMaximized(w) {
    if (typeof w.is_maximized === 'function')
        return w.is_maximized();
    return w.get_maximized() === Meta.MaximizeFlags.BOTH;
}

function maximize(w) {
    if (typeof w.get_maximized === 'function' && Meta.MaximizeFlags)
        w.maximize(Meta.MaximizeFlags.BOTH);
    else
        w.maximize();
}

function unmaximize(w) {
    if (typeof w.get_maximized === 'function' && Meta.MaximizeFlags)
        w.unmaximize(Meta.MaximizeFlags.BOTH);
    else
        w.unmaximize();
}

function normalWindows() {
    return global.get_window_actors()
        .map(a => a.meta_window)
        .filter(w => w && w.get_window_type() === Meta.WindowType.NORMAL &&
                     !w.is_skip_taskbar());
}

function describe(w) {
    const tracker = Shell.WindowTracker.get_default();
    const app = tracker.get_window_app(w);
    return {
        id: Number(w.get_id()),
        pid: w.get_pid(),
        title: w.get_title() || '',
        wm_class: w.get_wm_class() || '',
        wm_class_instance: w.get_wm_class_instance() || '',
        gtk_app_id: w.get_gtk_application_id() || '',
        sandboxed_app_id: w.get_sandboxed_app_id() || '',
        app_id: app ? (app.get_id() || '') : '',
        app_name: app ? (app.get_name() || '') : '',
        minimized: Boolean(w.minimized),
        maximized: Boolean(isMaximized(w)),
        focused: Boolean(w.has_focus()),
        monitor: w.get_monitor(),
        user_time: w.get_user_time(),
    };
}

class VisionWindowsService {
    get Version() {
        return VERSION;
    }

    List() {
        const out = [];
        for (const w of normalWindows()) {
            try {
                out.push(describe(w));
            } catch (e) {
                // One odd window must not hide all the others.
            }
        }
        return JSON.stringify(out);
    }

    Act(id, what) {
        const w = normalWindows().find(x => Number(x.get_id()) === Number(id));
        if (!w)
            return false;
        const now = global.get_current_time();
        switch (what) {
        case 'minimize':
            if (!w.can_minimize())
                return false;
            w.minimize();
            return true;
        case 'maximize':
            if (w.minimized)
                w.unminimize();
            if (!w.can_maximize())
                return false;
            maximize(w);
            Main.activateWindow(w, now);
            return true;
        case 'unmaximize':
            if (w.minimized)
                w.unminimize();
            if (isMaximized(w))
                unmaximize(w);
            Main.activateWindow(w, now);
            return true;
        case 'restore':
            // Back from the dock if minimized; otherwise back from maximized.
            if (w.minimized)
                w.unminimize();
            else if (isMaximized(w))
                unmaximize(w);
            Main.activateWindow(w, now);
            return true;
        case 'activate':
            Main.activateWindow(w, now);
            return true;
        default:
            return false;
        }
    }
}

export default class VisionWindowsExtension extends Extension {
    enable() {
        this._service = new VisionWindowsService();
        this._dbus = Gio.DBusExportedObject.wrapJSObject(IFACE, this._service);
        this._dbus.export(Gio.DBus.session, OBJECT_PATH);
    }

    disable() {
        if (this._dbus) {
            this._dbus.flush();
            this._dbus.unexport();
        }
        this._dbus = null;
        this._service = null;
    }
}
