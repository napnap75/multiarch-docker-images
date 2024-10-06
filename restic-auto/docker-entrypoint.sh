#!/bin/bash

set -e

# When used with S3 and docker secrets, get the credentials from files
if [[ "$AWS_ACCESS_KEY_ID" = /* && -f "$AWS_ACCESS_KEY_ID" ]] ; then
	AWS_ACCESS_KEY_ID=$(cat $AWS_ACCESS_KEY_ID)
fi
if [[ "$AWS_SECRET_ACCESS_KEY" = /* && -f "$AWS_SECRET_ACCESS_KEY" ]] ; then
	AWS_SECRET_ACCESS_KEY=$(cat $AWS_SECRET_ACCESS_KEY)
fi

# When used with SFTP set the SSH configuration file
if [[ "$RESTIC_REPOSITORY" = sftp:* ]] ; then
	# Copy the key and make it readable only by the current user to meet SSH security requirements
	cp $SFTP_KEY /tmp/foreign_host_key
	chmod 400 /tmp/foreign_host_key
	SFTP_KEY=/tmp/foreign_host_key

	# Initialize the SSH config file with the values provided in the environment
	mkdir -p /root/.ssh
	echo "Host $SFTP_HOST" > /root/.ssh/config
	if [[ "$SFTP_PORT" != "" ]] ; then echo "Port $SFTP_PORT" >> /root/.ssh/config ; fi
	echo "IdentityFile $SFTP_KEY" >> /root/.ssh/config
	echo "StrictHostKeyChecking no" >> /root/.ssh/config
fi

# Install the crontabs
if [ -d /crontabs ] ; then
	for f in /crontabs/* ; do
		crontab -u $(basename $f) $f
	done
else
	echo "# This crontab is generated by the entrypoint script, do not edit" > /tmp/crontab
	if [[ "$BACKUP_CRONTAB" ]] ; then
		echo -n "$BACKUP_CRONTAB" >> /tmp/crontab
	else
		echo -n "0 4 * * *" >> /tmp/crontab
	fi
	if [[ "$HC_PING_KEY" ]] ; then
		echo -n " runitor -slug ${HOSTNAME}-restic-backup --" >> /tmp/crontab
	fi
	echo -n " restic-auto >> /var/log/cron.log" >> /tmp/crontab
	if [[ "$POST_BACKUP_COMMAND" ]] ; then
		echo -n " && $POST_BACKUP_COMMAND" >> /tmp/crontab
	fi
	echo " " >> /tmp/crontab
	if [[ "$MAINTENANCE_CRONTAB" ]] ; then
		echo -n "$MAINTENANCE_CRONTAB" >> /tmp/crontab
	else
		echo -n "0 1 * * 0" >> /tmp/crontab
	fi
	if [[ "$HC_PING_KEY" ]] ; then
		echo -n " runitor -slug ${HOSTNAME}-restic-forget --" >> /tmp/crontab
	fi
	echo -n " restic forget --keep-daily 7 --keep-weekly 4 --keep-monthly 12 --keep-yearly 2 --prune >> /var/log/cron.log &&" >> /tmp/crontab
	if [[ "$HC_PING_KEY" ]] ; then
		echo -n " runitor -slug ${HOSTNAME}-restic-check --" >> /tmp/crontab
	fi
	echo -n " restic check >> /var/log/cron.log" >> /tmp/crontab
	if [[ "$POST_MAINTENANCE_COMMAND" ]] ; then
		echo -n " && $POST_MAINTENANCE_COMMAND" >> /tmp/crontab
	fi
	echo " " >> /tmp/crontab

	crontab -u root /tmp/crontab
fi

"$@"
