#!/usr/bin/env bash
# Lend the primary's GPU and take it back, one command each way (run on the primary as root).
#
#   server/scripts/gpu_lend.sh status            who answers, who has the mic, VRAM, display build
#   server/scripts/gpu_lend.sh lend [--music]    Antigua moves to the backup (--music: Music Assistant too)
#   server/scripts/gpu_lend.sh reclaim           everything comes back; prints how the lend performed
#
# docs/deployment.md "Lending the primary's GPU" explains each piece. It's the
# normal failover, started by hand. Ollama stays up on the primary for other
# work. Hosts come from deploy.env, as for deploy_fallback.sh.
#
# Optional pieces, used when present on the primary:
#   pineda-web + pineda-standby-sync   the display server and its standby sync; the kiosk fails over
#   server/music/.env on both boxes    Music Assistant via server/music/docker-compose.yml
set -euo pipefail

REPO=$(cd "$(dirname "$0")/../.." && pwd)
PY="$REPO/venv/bin/python3"
CFG="$REPO/server/config/server.yaml"
FLAG=/etc/antigua/gpu_lent
UNITS=(antigua-tts antigua-whisper antigua-model-warmup antigua-server)
GUARDED=("${UNITS[@]}" kitchen-bridge pineda-web pineda-standby-sync)
NEED_MIB=${LEND_NEED_MIB:-5600}       # VRAM the primary's stack takes (4B + whisper large-v3-turbo)
# shellcheck source=deploy.env.example
. "$REPO/server/scripts/deploy.env"
HOST=${FALLBACK_SSH:?set FALLBACK_SSH in deploy.env}
DEST=${FALLBACK_DIR:?set FALLBACK_DIR in deploy.env}
WG=${FALLBACK_WG_IP:?set FALLBACK_WG_IP in deploy.env}

say() { printf '\n== %s\n' "$*"; }
die() { echo "!! $*" >&2; exit 1; }
has_unit() { systemctl cat "$1.service" >/dev/null 2>&1; }
yget() { "$PY" -c "import yaml; d=yaml.safe_load(open('$CFG')); print((d.get('$1') or {}).get('$2') or '')"; }
fb_health() { ssh "$HOST" "curl -s -m 3 localhost:9394/health" 2>/dev/null; }
fb_active() { fb_health | grep -q '"active": *true'; }

vram() {   # "used total" in MiB, any vendor
    local d
    for d in /sys/class/drm/card*/device; do
        [ -r "$d/mem_info_vram_total" ] && { echo $(( $(cat "$d/mem_info_vram_used") >> 20 )) $(( $(cat "$d/mem_info_vram_total") >> 20 )); return; }
    done
    command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader,nounits | tr -d ',' | head -1 && return
    echo "0 0"
}

bundle() { curl -s -m 3 "http://$1:8090/" | grep -o 'assets/index-[A-Za-z0-9_-]*\.js' || echo "-"; }

set_music_url() {   # <url>, in place, keeping the comment
    sed -i -E "/^music:/,/^\S/ s|^(  url: )\S+(.*)$|\1$1\2|" "$CFG"
    grep -A3 '^music:' "$CFG" | grep -m1 '  url:'
}

dropins() {   # install|remove the ConditionPathExists guard on every lend-managed unit
    local u d
    for u in "${GUARDED[@]}"; do
        has_unit "$u" || continue
        d=/etc/systemd/system/$u.service.d
        if [ "$1" = install ]; then
            mkdir -p "$d"
            printf '%s\n' "# Installed by server/scripts/gpu_lend.sh while the GPU is lent; removed on reclaim." \
                "[Unit]" "ConditionPathExists=!$FLAG" > "$d/40-gpu-lent.conf"
        else
            rm -f "$d/40-gpu-lent.conf"; rmdir "$d" 2>/dev/null || true
        fi
    done
    systemctl daemon-reload
}

status() {
    local used total
    read -r used total < <(vram)
    if [ -e "$FLAG" ]; then echo "GPU      LENT (flag set since $(date -r "$FLAG" '+%F %R'))"; else echo "GPU      with Antigua"; fi
    echo "VRAM     ${used}/${total} MiB used"
    curl -s localhost:11434/api/ps | python3 -c "import json,sys; [print('  model ', m['name'], m['size_vram']>>20, 'MiB') for m in json.load(sys.stdin)['models']]" 2>/dev/null || true
    if curl -sf -m 2 localhost:9393/health >/dev/null; then echo "answers  primary"
    elif fb_active; then echo "answers  backup (standing in)"; else echo "answers  NOBODY — check both boxes"; fi
    echo "mic      $(systemctl is-active kitchen-bridge 2>/dev/null | sed 's/^active$/primary/;s/^inactive$/backup/')"
    echo "music    $(yget music url)"
    if has_unit pineda-web; then
        echo "display  primary $(systemctl is-active pineda-web) $(bundle 127.0.0.1) | backup $(bundle "$WG")"
    fi
}

move_music() {   # to-backup | to-primary
    [ -f "$REPO/server/music/.env" ] || { echo "   (no server/music/.env here; skipping music)"; return; }
    local here there
    here=$(sed -n 's/^MA_DATA_DIR=//p' "$REPO/server/music/.env")
    there=$(ssh "$HOST" "sed -n 's/^MA_DATA_DIR=//p' $DEST/server/music/.env" 2>/dev/null) \
        || die "no $DEST/server/music/.env on the backup (MA_DATA_DIR=...)"
    if [ "$1" = to-backup ]; then
        say "Music Assistant -> backup"
        docker compose -f "$REPO/server/music/docker-compose.yml" stop
        rsync -aH --delete "$here/" "$HOST:$there/"
        rsync -az "$REPO/server/music/docker-compose.yml" "$HOST:$DEST/server/music/"
        ssh "$HOST" "cd $DEST/server/music && docker compose up -d"
        set_music_url "http://$WG:8095"
    else
        say "Music Assistant -> primary"
        ssh "$HOST" "cd $DEST/server/music && docker compose down"
        rsync -aH --delete "$HOST:$there/" "$here/"
        docker compose -f "$REPO/server/music/docker-compose.yml" up -d
        set_music_url "http://${PRIMARY_WG_IP:-10.77.0.1}:8095"
    fi
}

lend() {
    [ -e "$FLAG" ] && die "already lent (rm $FLAG only via: $0 reclaim)"
    say "Preflight"
    fb_health | grep -q '"status": *"ok"' || die "the backup's fallback isn't answering on :9394"
    local fbm; fbm=$(yget fallback model); fbm=${fbm:-antigua-4b-text}
    ssh "$HOST" "ollama list | awk '{print \$1}' | grep -qx '$fbm\(:latest\)\?'" || die "backup has no $fbm (server/scripts/use_model.sh --fallback <model>)"
    echo "   backup healthy, has $fbm"
    [ "${1:-}" = --music ] && move_music to-backup

    say "Primary steps aside"
    touch "$FLAG"; dropins install
    systemctl disable "${UNITS[@]}" 2>/dev/null
    systemctl stop antigua-server antigua-whisper antigua-tts
    curl -s localhost:11434/api/generate -d "{\"model\":\"$(yget ollama model)\",\"keep_alive\":0}" >/dev/null || true
    systemctl disable --now kitchen-bridge 2>/dev/null || true
    if has_unit pineda-web; then systemctl stop pineda-standby-sync.timer pineda-web; fi
    say "Backup takes the mic"
    "$REPO/server/scripts/deploy_fallback.sh" >/dev/null
    for _ in $(seq 30); do fb_active && break; sleep 2; done
    fb_active || die "the backup hasn't taken over after 60 s; check its fallback.log"
    date +%s > /etc/antigua/gpu_lent.since
    echo "   backup is answering. VRAM now: $(vram | awk '{print $1"/"$2}') MiB"
    status
}

reclaim() {
    [ -e "$FLAG" ] || die "not lent"
    say "Preflight"
    local used total
    read -r used total < <(vram)
    [ $(( total - used )) -ge "$NEED_MIB" ] || {
        echo "!! only $(( total - used )) MiB VRAM free; Antigua needs ~$NEED_MIB. Loaded in Ollama:" >&2
        curl -s localhost:11434/api/ps | python3 -c "import json,sys; [print('   ', m['name'], m['size_vram']>>20, 'MiB') for m in json.load(sys.stdin)['models']]" >&2
        die "free it first (ollama stop <model>, stop other GPU servers)"; }
    echo "   $(( total - used )) MiB free"
    rsync -a "$HOST:$DEST/data/household_memory.yaml" "$REPO/data/" 2>/dev/null && echo "   household memory copied back"
    [[ "$(yget music url)" == *"$WG"* ]] && move_music to-primary

    say "Antigua comes home"
    rm -f "$FLAG"; dropins remove
    systemctl enable --now "${UNITS[@]}" 2>/dev/null
    for _ in $(seq 60); do curl -sf -m 2 localhost:9393/health >/dev/null && break; sleep 2; done
    curl -sf -m 2 localhost:9393/health >/dev/null || die "antigua-server isn't healthy; journalctl -u antigua-server"
    for _ in $(seq 30); do fb_active || break; sleep 2; done
    fb_active && die "the backup hasn't stood down; check its fallback.log"
    "$REPO/server/scripts/deploy_fallback.sh" >/dev/null     # the backup's bridge back to standby
    systemctl enable --now kitchen-bridge 2>/dev/null

    if has_unit pineda-web; then
        say "Display"
        systemctl enable pineda-web 2>/dev/null
        systemctl start pineda-standby-sync.timer pineda-standby-sync; systemctl start pineda-web
        local a b; a=$(bundle 127.0.0.1); b=$(bundle "$WG")
        [ "$a" = "$b" ] && echo "   same build on both: $a" \
            || echo "!! builds differ (primary $a, backup $b): a display deploy during the lend went to the backup only. Install it here."
    fi

    say "Check"
    curl -s localhost:11434/api/ps | python3 -c "import json,sys; [print('  ', m['name'], m['size_vram']>>20, 'of', m['size']>>20, 'MiB in VRAM') for m in json.load(sys.stdin)['models']]"
    curl -s -m 60 localhost:9393/pipeline_text -H 'Content-Type: application/json' -d '{"text":"what time is it","quiet":true}' \
        | python3 -c "import json,sys; print('   probe:', json.load(sys.stdin).get('response'))"

    say "How the lend performed (spoken turns on the backup)"
    local since="0000"
    [ -f /etc/antigua/gpu_lent.since ] && since=$(date -d "@$(cat /etc/antigua/gpu_lent.since)" +%F)
    ssh "$HOST" "cd $DEST/logs && zcat -f \$(ls -tr fallback.log*)" 2>/dev/null | grep "TURN route=" | grep "stt=" \
        | awk -v s="$since" '$1 >= s' | "$PY" "$REPO/server/scripts/turn_stats.py" - || echo "   (no spoken turns logged)"
    echo "   Primary, for comparison: $PY server/scripts/turn_stats.py logs/server.log"
    rm -f /etc/antigua/gpu_lent.since
}

[ "$(id -u)" = 0 ] || [ "${1:-status}" = status ] || die "run as root"
case "${1:-status}" in
    status) status ;;
    lend) lend "${2:-}" ;;
    reclaim) reclaim ;;
    *) sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
