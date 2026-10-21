#!/bin/bash
# Offline drill: cut this box off from the internet (LAN, wg0 and localhost
# stay up), wait for antigua-server's netstatus to notice, ask a set of
# typed questions through /pipeline_text, then restore the internet.
#
#   sudo server/scripts/offline_drill.sh            # default: 6 minute cap
#   sudo server/scripts/offline_drill.sh --restore  # remove the block now
#
# The block is one nftables table, antigua_drill. A transient systemd timer
# deletes it after CAP_MIN minutes even if this script is killed, so a drill
# can't leave the house offline. Only this machine loses the internet; the
# rest of the network is untouched.
set -u
CAP_MIN=${CAP_MIN:-6}
API=${API:-http://127.0.0.1:9393/pipeline_text}
# This box's LAN subnet (the default route's interface), unless LAN is set.
LAN=${LAN:-$(ip -4 -o addr show dev "$(ip route show default | awk '{print $5; exit}')" |
             awk '{print $4; exit}' | python3 -c "import sys,ipaddress;print(ipaddress.ip_interface(sys.stdin.read().strip()).network)")}
REPO=$(cd "$(dirname "$0")/../.." && pwd)

restore() {
    nft delete table inet antigua_drill 2>/dev/null && echo "internet restored"
    systemctl stop antigua-drill-restore.timer 2>/dev/null
}
[ "${1:-}" = "--restore" ] && { restore; exit 0; }
[ "$(id -u)" = 0 ] || { echo "run as root"; exit 1; }

nft -f - <<EOF
table inet antigua_drill {
    chain out {
        type filter hook output priority -10; policy accept;
        oif "lo" accept
        oifname "wg0" accept
        ip daddr { $LAN, 10.0.0.0/8, 172.16.0.0/12 } accept
        ip daddr 224.0.0.0/4 accept
        ip6 daddr { fe80::/10, ff00::/8 } accept
        meta l4proto { tcp, udp, icmp, ipv6-icmp } drop
    }
}
EOF
systemd-run --quiet --unit antigua-drill-restore --on-active="${CAP_MIN}m" \
    /usr/sbin/nft delete table inet antigua_drill
trap restore EXIT
echo "internet blocked (auto-restore in ${CAP_MIN} min); waiting for netstatus..."

ask() {
    local t0 ms reply
    t0=$(date +%s%N)
    reply=$(curl -s -m 30 "$API" -d "{\"text\":\"$1\"}" |
            python3 -c "import sys,json;print(json.load(sys.stdin).get('response',''))" 2>/dev/null)
    ms=$(( ($(date +%s%N) - t0) / 1000000 ))
    printf '%6dms  %-40s → %s\n' "$ms" "$1" "$reply"
}

for _ in $(seq 1 12); do
    sleep 10
    r=$(curl -s -m 10 "$API" -d '{"text":"are we online"}' |
        python3 -c "import sys,json;print(json.load(sys.stdin).get('response',''))" 2>/dev/null)
    case "$r" in No,*) echo "netstatus sees it: $r"; break ;; esac
done

for q in "is the internet down" "why is the internet down" "what time is it" \
         "set a timer for 1 minute" "cancel the timer" "what's the weather" \
         "what's the news" "how's traffic to work" "what's bitcoin at" \
         "who was Frida Kahlo" "what's 15 percent of 80" "tell me a joke" \
         "how's the server doing" "is everything running" "play some jazz"; do
    ask "$q"
done
echo "Doorbell: walk past it now if you want to see whether Eufy events arrive offline (60 s)."
sleep 60
grep -E "Security: front_door (motion|person|ring)" "$REPO/logs/server.log" | tail -3
