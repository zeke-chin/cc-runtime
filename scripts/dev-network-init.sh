#!/bin/sh
set -eu

# This runs inside dev's network namespace, not proxy's.
# Prevent Docker's embedded DNS from becoming an alternate resolver.
iptables -C OUTPUT -d 127.0.0.11/32 -j REJECT 2>/dev/null ||
    iptables -I OUTPUT 1 -d 127.0.0.11/32 -j REJECT

# Configure the default route after blocking the alternate DNS path.
ip route replace default via 172.30.0.2
echo 'dev default route and DNS interception configured.'
