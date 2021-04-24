import argparse
import configparser
import time
import re
import ast

from slacker import Slacker

# Parse the arguments
parser = argparse.ArgumentParser(description='Utilityy to delete Slack message.')
parser.add_argument('file', help='The configuration file')
parser.add_argument('--verbose', '-v', action='count', help='Be verbose')
parser.add_argument('--dry-run', '-n', action='count', help='Do nothing (for test purpose)')
args = parser.parse_args()
if args.verbose != None :
	print("Using args:", args)

# Parse the configuration file
if args.verbose != None :
	print("Parsing configuration file:", args.file)
config = configparser.ConfigParser()
config.read(args.file)

# Open the connection to Slack
if args.verbose != None :
	print("Opening Slack connection")
slack = Slacker(config['DEFAULT']['SlackTocken'])

# Iterate over all sections in the config file
for sectionName in config.sections():
	section = config[sectionName]
	eraserType = section.get('Type')

	searchRequest = section.get('Request', "")
	searchFrom = section.get('From')
	if searchFrom != None and " " not in searchFrom:
		searchRequest += " from:" + searchFrom
	searchIn = section.get('In')
	if searchIn != None:
		searchRequest += " in:" + searchIn
	searchSort = section.get('Sort', "timestamp")
	searchOrder = section.get('Order', "desc")
	searchCount = section.get('Count', 100)
	searchRE = section.get('RegExp')
	regexp = None
	if searchRE != None:
		regexp = re.compile(searchRE)

	if eraserType == 'duplicates':
		if args.verbose != None :
			print("Deleting duplicates messages with request: \"", searchRequest, "\", sort:", searchSort, ", order:", searchOrder, "and count:", searchCount)
			if searchRE != None:
				print("And using regular expression:", searchRE)

		searchResponse = slack.search.messages(searchRequest, searchSort, searchOrder, None, searchCount)
		messages = searchResponse.body['messages']['matches']
		seenMessages = []
		for message in messages:
			text = message['text']
			if text == "":
				try:
					text = message['attachments'][0]['fallback']
				except KeyError as ex:
					try:
						text = message['attachments'][0]['text']
					except KeyError as ex:
						if args.verbose != None and args.verbose > 1:
							print("Message skipped because empty")
						continue
			if searchFrom != None and searchFrom != message['username']:
				if args.verbose != None and args.verbose > 1:
					print(text, "--> Skipped (From '", message['username'], "' != '", searchFrom, "')")
				continue
			if searchIn != None and searchIn != message['channel']['name']:
				if args.verbose != None and args.verbose > 1:
					print(text, "--> Skipped (In '", message['channel']['name'], "' != '", searchIn, "')")
				continue

			key = text
			if regexp != None:
				match = regexp.match(text)
				if match:
					try:
						key = match.group(1)
					except IndexError as ex:
						key = match.group(0)
					if args.verbose != None and args.verbose > 1:
						print(text, "using key:", key)
				else:
					if args.verbose != None and args.verbose > 1:
						print(key, "--> Skipped (RegExp)")
					continue

			if key in seenMessages:
				if args.verbose != None and args.verbose > 1:
					print(key, "--> Already seen")
				if args.dry_run == None:
					if args.verbose != None:
						print("Deleting message timestamp", message['ts'], "in channel", message['channel']['name'])
					slack.chat.delete(message['channel']['id'], message['ts'])
					time.sleep(60/50)
				else:
					if args.verbose != None:
						print("Should have deleted message timestamp", message['ts'], "in channel", message['channel']['name'])
			else:
				if args.verbose != None and args.verbose > 1:
					print(key, "--> Not seen")
				seenMessages.append(key)
	elif eraserType == 'alerts':
		closedStatus = ast.literal_eval(section.get('ClosedStatus', "['OK', 'Success']"))
		openStatus = ast.literal_eval(section.get('OpenStatus', "['CRITICAL', 'Failure']"))
		if args.verbose != None :
			print("Deleting alerts messages with request: \"", searchRequest, "\", sort:", searchSort, ", order:", searchOrder, "and count:", searchCount)
			print("And using regular expression:", searchRE, "with opening status:", openStatus, "and closing status:", closedStatus)

		searchResponse = slack.search.messages(searchRequest, searchSort, searchOrder, None, searchCount)
		messages = searchResponse.body['messages']['matches']
		closedAlerts = []
		openedAlerts = []
		for message in messages:
			text = message['text']
			if text is None or text == "":
				try:
					text = message['attachments'][0]['fallback']
				except KeyError as ex:
					try:
						text = message['attachments'][0]['text']
					except KeyError as ex:
						if args.verbose != None and args.verbose > 1:
							print("Message skipped because empty")
						continue
			if searchFrom != None and searchFrom != message['username']:
				if args.verbose != None and args.verbose > 1:
					print(text, "--> Skipped (From '", message['username'], "' != '", searchFrom, "')")
				continue
			if searchIn != None and searchIn != message['channel']['name']:
				if args.verbose != None and args.verbose > 1:
					print(text, "--> Skipped (In '", message['channel']['name'], "' != '", searchIn, "')")
				continue

			status = None
			match = regexp.match(text)
			if match:
				if match.group(1) in closedStatus:
					closedAlerts.append(match.group(2))
					if args.verbose != None and args.verbose > 1:
						print(text, "--> Alert '", match.group(2), "' closed")
				elif match.group(1) in openStatus:
					if match.group(2) in closedAlerts:
						if args.verbose != None and args.verbose > 1:
							print(text, "--> Alert '", match.group(2), "' already closed")
					elif match.group(2) in openedAlerts:
						if args.verbose != None and args.verbose > 1:
							print(text, "--> Alert '", match.group(2), "' not closed but already seen")
					else:
						if args.verbose != None and args.verbose > 1:
							print(text, "--> Alert '", match.group(2), "' not closed")

						openedAlerts.append(match.group(2))
						continue
				elif match.group(2) in closedStatus:
					closedAlerts.append(match.group(1))
					if args.verbose != None and args.verbose > 1:
						print(text, "--> Alert '", match.group(1), "' closed")
				elif match.group(2) in openStatus:
					if match.group(1) in closedAlerts:
						if args.verbose != None and args.verbose > 1:
							print(text, "--> Alert '", match.group(1), "' already closed")
					elif match.group(1) in openedAlerts:
						if args.verbose != None and args.verbose > 1:
							print(text, "--> Alert '", match.group(1), "' not closed but already seen")
					else:
						if args.verbose != None and args.verbose > 1:
							print(text, "--> Alert '", match.group(1), "' not closed")

						openedAlerts.append(match.group(1))
						continue
				else:
					if args.verbose != None and args.verbose > 1:
						print(text, "--> Skipped (Status not found)")
					continue
			else:
				if args.verbose != None and args.verbose > 1:
					print(text, "--> Skipped (Does not match regular expression)")
				continue

			if args.dry_run == None:
				if args.verbose != None:
					print("Deleting message timestamp", message['ts'], "in channel", message['channel']['name'])
				slack.chat.delete(message['channel']['id'], message['ts'])
				time.sleep(60/50)
			else:
				if args.verbose != None:
					print("Should have deleted message timestamp", message['ts'], "in channel", message['channel']['name'])
	elif eraserType == 'older':
		olderThan = int(ast.literal_eval(section.get('OlderThan')))
		if args.verbose != None :
			print("Deleting messages older than ", olderThan, " days with request: \"", searchRequest, "\" and count:", searchCount)

		searchRequest = searchRequest + " before:" + time.strftime("%Y-%m-%d", time.gmtime(time.time()-olderThan*24*60*60))
		searchResponse = slack.search.messages(searchRequest, "timestamp", "desc", None, searchCount)
		messages = searchResponse.body['messages']['matches']
		for message in messages:
			if args.dry_run == None:
				if args.verbose != None:
					print("Deleting message timestamp", message['ts'], "in channel", message['channel']['name'])
				slack.chat.delete(message['channel']['id'], message['ts'])
				time.sleep(60/50)
			else:
				if args.verbose != None:
					print("Should have deleted message timestamp", message['ts'], "in channel", message['channel']['name'])
	else:
		print("Unknown type ", eraserType, " in section", sectionName)

