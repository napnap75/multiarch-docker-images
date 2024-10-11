#!/bin/bash

/usr/bin/runitor -every 5m -slug ${HOSTNAME}-dnsupdater -- /usr/bin/dnsupdater.sh
