#!/bin/sh
rm -rf /var/run /run/dbus
mkdir -p /var/run/dbus /run/dbus
dbus-uuidgen --ensure
dbus-daemon --system
avahi-daemon --daemonize --no-chroot
su-exec shairport-sync /usr/local/bin/shairport-sync $@

