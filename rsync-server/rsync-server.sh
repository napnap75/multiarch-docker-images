#!/bin/bash
set -e

cat > /etc/rsyncd.conf <<EOF
log file = /dev/stdout
timeout = 300
max connections = 10
port = 873
EOF

for i in {0..9} ; do
	testvar=RSYNC_VOLUME_$i
	if [ "${!testvar}" != "" ] ; then
		VOLUME=${!testvar}
		echo "Configuring $VOLUME"

		testvar=RSYNC_PATH_$i
		DIRECTORY=${!testvar:-/data/$VOLUME}
		testvar=RSYNC_USERNAME_$i
		USERNAME=${!testvar:-user}
		testvar=RSYNC_PASSWORD_$i
		PASSWORD=${!testvar:-pass}
		testvar=RSYNC_ALLOW_$i
		ALLOW=${!testvar:-10.0.0.0/8 192.168.0.0/16 172.16.0.0/12 127.0.0.1/32}
		testvar=RSYNC_UID_$i
		USERID=${!testvar:-root}
		testvar=RSYNC_GID_$i
		GROUPID=${!testvar:-root}
		testvar=RSYNC_READONLY_$i
		READONLY=${!testvar:-false}
		testvar=RSYNC_USECHROOT_$i
		USECHROOT=${!testvar:-no}

		echo "$USERNAME:$PASSWORD" >> /etc/rsyncd.secrets

		cat >> /etc/rsyncd.conf <<EOF
[${VOLUME}]
	uid = ${USERID}
	gid = ${GROUPID}
	hosts deny = *
	hosts allow = ${ALLOW}
	read only = ${READONLY}
	use chroot = ${USECHROOT}
	path = ${DIRECTORY}
	comment = ${VOLUME} directory
	auth users = ${USERNAME}
	secrets file = /etc/rsyncd.secrets
EOF

		mkdir -p $VOLUME
	fi
done

chmod 0400 /etc/rsyncd.secrets

exec /usr/bin/rsync --no-detach --daemon --config /etc/rsyncd.conf
