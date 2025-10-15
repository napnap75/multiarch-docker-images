#!/bin/sh

# Make sure a volume is properly mounted
if [ ! -d "/config" ] ; then
	echo "####################################################################"
	echo "### Please start this container with a volume mounted to /config ###"
	echo "####################################################################"
	exit
fi

# First use : init the /config directory
if [ ! -f "/config/ssh_host_ed25519_key" ] ; then
	ssh-keygen -t ed25519 -f /config/ssh_host_ed25519_key -N "" < /dev/null
fi
if [ ! -f "/config/passwd" ] ; then
	echo -n "Enter username:"
	read
	NEW_USER=$REPLY
	adduser -u 666 $NEW_USER
	echo $NEW_USER > /config/username
	grep -E "root|sshd|$NEW_USER" /etc/passwd > /config/passwd
	grep -E "root|sshd|$NEW_USER" /etc/shadow > /config/shadow
	grep -E "root|sshd|$NEW_USER" /etc/group > /config/group
fi
if [ ! -f "/config/sshd_config" ] ; then
	echo "ChrootDirectory /chroot" > /config/sshd_config
fi

# Use the config provided
cp -f /config/ssh_host_ed25519_key* /etc/ssh/
cp -f /config/sshd_config /etc/ssh/
cp -f /config/passwd /etc/passwd
cp -f /config/shadow /etc/shadow
cp -f /config/group /etc/group

# Prepare the chrooted env
if [ ! -d "/chroot" ] ; then
	mkdir /chroot
	mkdir /chroot/dev
	mknod -m 666 /chroot/dev/null c 1 3
	mknod -m 666 /chroot/dev/zero c 1 5
	mknod -m 666 /chroot/dev/tty c 5 0
	mkdir /chroot/bin
	cp /bin/sh /chroot/bin/
	mkdir /chroot/lib
	cp /lib/*.so.* /chroot/lib/
	mkdir /chroot/usr
	mkdir /chroot/usr/bin
	cp /usr/bin/ssh /chroot/usr/bin/
	mkdir /chroot/etc
	cp /etc/passwd /chroot/etc/
	mkdir /chroot/home
	mkdir /chroot/home/$(cat /config/username)
fi

"$@"
