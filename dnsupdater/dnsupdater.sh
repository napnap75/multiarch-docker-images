#!/bin/bash

# Get my IP
my_ip=$(curl -s https://api.ipify.org)
if [[ ! ("$my_ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$) ]] ; then
  echo "Got incorrect IP: $my_ip"
  exit 1
fi

# Get my registered IP
current_ip=
if [[ "$GANDI_API_KEY" != "" ]] ; then
  current_ip=$(curl -s -H"X-Api-Key: $GANDI_API_KEY" https://dns.api.gandi.net/api/v5/domains/$DNS_DOMAIN/records | jq -r '.[] | select(.rrset_name == "'$DNS_HOST'") | select(.rrset_type == "A") | .rrset_values[0]')
elif [[ "$OVH_USERNAME" != "" ]] ; then
  master_server=$(dig +short -t NS $DNS_DOMAIN | head -n 1)
  current_ip=$(dig +short @$master_server $DNS_HOST.$DNS_DOMAIN)
else
  echo "No DNS provider configured"
  exit 2
fi
if [[ ! ("$current_ip" =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$) ]] ; then
  echo "Incorrect registered IP: $current_ip"
  exit 3
fi

# If they match, do nothing
if [[ "$my_ip" == "$current_ip" ]]; then
  echo "$DNS_HOST.$DNS_DOMAIN record already up-to-date with IP $my_ip"
  exit 0
fi

# Update the DNS record
echo "Updating $DNS_HOST.$DNS_DOMAIN record with IP $my_ip"
if [[ "$GANDI_API_KEY" != "" ]] ; then
  current_record=$(curl -s -H"X-Api-Key: $GANDI_API_KEY" https://dns.api.gandi.net/api/v5/domains/$DNS_DOMAIN/records | jq -c '.[] | select(.rrset_name == "'$DNS_HOST'") | select(.rrset_type == "A")')
  current_ttl=$(echo $current_record | jq -r '.rrset_ttl')
  curl -s -X PUT -H "Content-Type: application/json" -H "X-Api-Key: $GANDI_API_KEY" -d '{"rrset_ttl": '$current_ttl', "rrset_values":["'$my_ip'"]}' https://dns.api.gandi.net/api/v5/domains/$DNS_DOMAIN/records/$DNS_HOST/A
  if [[ $? != 0 ]] ; then
    echo "Unable to update GANDI record"
    exit 4
  fi
elif [[ "$OVH_USERNAME" != "" ]] ; then
  curl -s --user "$OVH_USERNAME:$OVH_PASSWORD" "http://www.ovh.com/nic/update?system=dyndns&hostname=$DNS_HOST.$DNS_DOMAIN&myip=$my_ip"
  if [[ $? != 0 ]] ; then
    echo "Unable to update OVH record"
    exit 5
  fi
fi

# Send a notification to Slack
if [[ "$SLACK_URL" != "" ]] ; then
  curl -o /dev/null -s -m 10 --retry 5 -X POST -d "payload={\"username\": \"gandi\", \"icon_emoji\": \":dart:\", \"text\": \"New IP $my_ip for host $DNS_HOST.$DNS_DOMAIN\"}" $SLACK_URL
fi

# Send a notification to Gotify
if [[ "$GOTIFY_URL" != "" ]] ; then
   curl -o /dev/null -s -m 10 --retry 5 -X POST -H "accept: application/json" -H "Content-Type: application/json" -d "{\"priority\": 5, \"title\": \"New IP for host $DNS_HOST.$DNS_DOMAIN\", \"message\": \"New IP $my_ip for host $DNS_HOST.$DNS_DOMAIN, the old IP was $current_ip\"}" $GOTIFY_URL
fi
