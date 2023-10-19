#!/bin/bash

while true ; do
  # Get my current IP
  my_ip=$(curl -s https://api.ipify.org)

  # Get my currently registered IP
  current_ip=
  if [[ "$GANDI_API_KEY" != "" ]] ; then
    current_ip=$(curl -s -H"X-Api-Key: $GANDI_API_KEY" https://dns.api.gandi.net/api/v5/domains/$DNS_DOMAIN/records | jq -r '.[] | select(.rrset_name == "'$DNS_HOST'") | select(.rrset_type == "A") | .rrset_values[0]')
  elif [[ "$OVH_USERNAME" != "" ]] ; then
    master_server=$(dig +short -t NS $DNS_DOMAIN | head -n 1)
    current_ip=$(dig +short @$master_server $DNS_HOST.$DNS_DOMAIN)
  fi

  # Check if both IP addresses are correct
  if [[ "$my_ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ && "$current_ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] ; then
    # If they do not match, change it (and keep the TTL and TYPE)
    if [[ "$my_ip" != "$current_ip" ]]; then
      result=1
      echo "Updating $DNS_HOST.$DNS_DOMAIN record with IP $my_ip"
      if [[ "$GANDI_API_KEY" != "" ]] ; then
        current_record=$(curl -s -H"X-Api-Key: $GANDI_API_KEY" https://dns.api.gandi.net/api/v5/domains/$DNS_DOMAIN/records | jq -c '.[] | select(.rrset_name == "'$DNS_HOST'") | select(.rrset_type == "A")')
        current_ttl=$(echo $current_record | jq -r '.rrset_ttl')
        curl -s -X PUT -H "Content-Type: application/json" -H "X-Api-Key: $GANDI_API_KEY" -d '{"rrset_ttl": '$current_ttl', "rrset_values":["'$my_ip'"]}' https://dns.api.gandi.net/api/v5/domains/$DNS_DOMAIN/records/$DNS_HOST/A
	result=$?
      elif [[ "$OVH_USERNAME" != "" ]] ; then
        curl -s --user "$OVH_USERNAME:$OVH_PASSWORD" "http://www.ovh.com/nic/update?system=dyndns&hostname=$DNS_HOST.$DNS_DOMAIN&myip=$my_ip"
	result=$?
      fi

      # If the update was OK
      if [[ $result == 0 ]] ; then
        # Send a notification to Slack
        if [[ "$SLACK_URL" != "" ]] ; then
          curl -o /dev/null -s -m 10 --retry 5 -X POST -d "payload={\"username\": \"gandi\", \"icon_emoji\": \":dart:\", \"text\": \"New IP $my_ip for host $DNS_HOST.$DNS_DOMAIN\"}" $SLACK_URL
        fi

        # Send a notification to Gotify
        if [[ "$GOTIFY_URL" != "" ]] ; then
          curl -o /dev/null -s -m 10 --retry 5 -X POST -H "accept: application/json" -H "Content-Type: application/json" -d "{\"priority\": 5, \"title\": \"New IP for host $DNS_HOST.$DNS_DOMAIN\", \"message\": \"New IP $my_ip for host $DNS_HOST.$DNS_DOMAIN, the old IP was $current_ip\"}" $GOTIFY_URL
        fi

        # Send a notification to Healthchecks
        if [[ "$HEALTHCHECKS_URL" != "" ]] ; then
          curl -o /dev/null -s -m 10 --retry 5 $HEALTHCHECKS_URL
        fi
      fi
    else
      # Send a notification to Healthchecks
      if [[ "$HEALTHCHECKS_URL" != "" ]] ; then
        curl -o /dev/null -s -m 10 --retry 5 $HEALTHCHECKS_URL
      fi
    fi
  fi

  # Wait 5 minutes
  sleep 300
done
