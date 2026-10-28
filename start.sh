#!/bin/sh
# A virtual screen and window manager, shared as a web page, then the agent on it.
export DISPLAY=:99
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99  # left behind by a restart
Xvfb :99 -screen 0 1400x1000x24 -nolisten tcp &
sleep 1
openbox &
x11vnc -display :99 -forever -shared -nopw -quiet -localhost -rfbport 5900 &
websockify --web /usr/share/novnc 6080 127.0.0.1:5900 &
exec uarb-mail
