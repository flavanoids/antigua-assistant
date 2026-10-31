#!/usr/bin/env bash
# Swap the voice LLM on the primary, or the backup's, in one step (run on the primary).
#
#   server/scripts/use_model.sh <ollama-model>              # the primary's ollama.model
#   server/scripts/use_model.sh --fallback <ollama-model>   # the backup's fallback.model
#   server/scripts/use_model.sh --check                     # what each host runs, and whether it fits
#
# Any small instruct model Ollama serves works: Antigua's skills don't depend
# on it, and the LLM only writes the reply.
#
# Primary steps:
#   1. pull the model and load it with ollama.num_ctx
#   2. refuse (and reload the old model) unless it sits 100% in VRAM
#   3. set ollama.model in server.yaml, re-render the warmup unit, restart antigua-server
#   4. print a short latency bench (bench_latency.py)
#
# --fallback does this on the backup instead. It builds a pinned copy called
# antigua-fallback-llm (Modelfile: num_gpu 99, fallback.num_ctx), so every
# request keeps it whole on a small GPU. It checks the fit, unloads the model
# again (the backup stays TTS-only until it takes over), sets fallback.model
# and runs deploy_fallback.sh.
#
# A model with a vision projector loads it too. On a 4 GB card, point at a
# text-only GGUF instead (`ollama show --modelfile <model>` gives the blob's
# FROM path); that is how antigua-4b-text was made.
set -euo pipefail

REPO=$(cd "$(dirname "$0")/../.." && pwd)
CFG=${ANTIGUA_CFG:-$REPO/server/config/server.yaml}
PY="$REPO/venv/bin/python3"
OLLAMA=${OLLAMA_HOST_URL:-http://localhost:11434}
PINNED=antigua-fallback-llm

yget() { "$PY" -c "import yaml,sys; d=yaml.safe_load(open('$CFG')); print((d.get('$1') or {}).get('$2') or '$3')"; }

# Set <section>.<key> in server.yaml in place, keeping its comments.
yset() {
    "$PY" - "$CFG" "$1" "$2" "$3" <<'EOF'
import re, sys
path, sect, key, val = sys.argv[1:]
lines = open(path).read().split("\n")
inside = False
for i, line in enumerate(lines):
    if re.match(r"^\S", line):
        inside = line.startswith(f"{sect}:")
    elif inside and re.match(rf"^  {key}:", line):
        m = re.match(rf"^(  {key}:\s*)(\S+)(\s*#.*)?$", line)
        lines[i] = m.group(1) + val + ("  " + m.group(3).strip() if m.group(3) else "")
        break
else:
    sys.exit(f"{sect}.{key} not found in {path}")
open(path, "w").write("\n".join(lines))
EOF
}

# On host $1 ("" = here): load model $2 with num_ctx $3 and print "vram/size" in MiB.
fit() {
    local run=() py=python3
    [ -n "$1" ] && run=(ssh "$1")
    "${run[@]}" curl -s -m 300 "$OLLAMA/api/generate" \
        -d "{\"model\":\"$2\",\"prompt\":\"\",\"keep_alive\":-1,\"options\":{\"num_ctx\":$3}}" >/dev/null
    "${run[@]}" curl -s "$OLLAMA/api/ps" | $py -c "
import json,sys
for m in json.load(sys.stdin)['models']:
    if m['name'].split(':latest')[0] == '$2'.split(':latest')[0]:
        print(m['size_vram']//2**20, m['size']//2**20); break
else: print(0, 0)"
}

unload() {
    local run=(); [ -n "$1" ] && run=(ssh "$1")
    "${run[@]}" curl -s "$OLLAMA/api/generate" -d "{\"model\":\"$2\",\"keep_alive\":0}" >/dev/null || true
}

check() {
    local m ctx v s
    m=$(yget ollama model "?"); ctx=$(yget ollama num_ctx 8192)
    read -r v s < <(curl -s "$OLLAMA/api/ps" | python3 -c "
import json,sys
for x in json.load(sys.stdin)['models']:
    if x['name'] == '$m': print(x['size_vram']//2**20, x['size']//2**20); break
else: print(0, 0)")
    echo "primary  ollama.model   $m  (num_ctx $ctx)  loaded ${v}/${s} MiB in VRAM"
    echo "backup   fallback.model $(yget fallback model antigua-4b-text)  (num_ctx $(yget fallback num_ctx 8192); loads only while standing in)"
}

primary() {
    local new=$1 old ctx v s
    old=$(yget ollama model ""); ctx=$(yget ollama num_ctx 8192)
    [ "$new" = "$old" ] && { echo "already on $new"; exit 0; }
    echo "== pull $new"; ollama pull "$new"
    echo "== unload $old, load $new (num_ctx $ctx)"
    unload "" "$old"
    read -r v s < <(fit "" "$new" "$ctx")
    if [ "$s" = 0 ] || [ "$v" -lt "$s" ]; then
        echo "!! $new is only ${v}/${s} MiB in VRAM: part runs on the CPU, several times slower." >&2
        echo "!! Keeping $old. Try a smaller quant, or lower ollama.num_ctx." >&2
        unload "" "$new"; fit "" "$old" "$ctx" >/dev/null; exit 1
    fi
    echo "   fits: ${v} MiB, all on the GPU"
    cp "$CFG" "$CFG.bak-$(date +%Y%m%d-%H%M)-model"
    yset ollama model "$new"
    "$PY" "$REPO/server/scripts/render_unit.py" "$REPO/server/antigua-model-warmup.service" \
        | sudo tee /etc/systemd/system/antigua-model-warmup.service >/dev/null
    sudo systemctl daemon-reload
    sudo systemctl restart antigua-server
    for _ in $(seq 30); do curl -sf -m 2 localhost:9393/health >/dev/null && break; sleep 2; done
    curl -s -m 60 localhost:9393/pipeline_text -H 'Content-Type: application/json' \
        -d '{"text":"tell me a fun fact about the moon","quiet":true}' \
        | python3 -c "import json,sys; print('   probe:', json.load(sys.stdin).get('response'))"
    echo "== bench"; "$PY" "$REPO/server/scripts/bench_latency.py" --rounds 3 --gap 5
    sudo systemctl restart antigua-model-warmup   # re-prime the voice prompt the bench evicted
    echo "Switched $old -> $new. Undo: $0 $old"
}

fallback() {
    local new=$1 ctx v s
    # shellcheck source=deploy.env.example
    . "$REPO/server/scripts/deploy.env"
    local host=${FALLBACK_SSH:?set FALLBACK_SSH in deploy.env}
    ctx=$(yget fallback num_ctx 8192)
    if ssh "$host" "curl -sf -m 2 localhost:9394/health" | grep -q '"active": *true'; then
        echo "the backup is standing in right now; swap its model once the primary is back" >&2; exit 1
    fi
    echo "== pull $new on $host, build $PINNED"
    ssh "$host" "ollama pull '$new' >/dev/null && printf 'FROM %s\nPARAMETER num_gpu 99\nPARAMETER num_ctx %s\n' '$new' '$ctx' > /tmp/$PINNED.Modelfile && ollama create $PINNED -f /tmp/$PINNED.Modelfile >/dev/null"
    read -r v s < <(fit "$host" "$PINNED" "$ctx")
    unload "$host" "$PINNED"
    if [ "$s" = 0 ] || [ "$v" -lt "$s" ]; then
        echo "!! $new is only ${v}/${s} MiB in the backup's VRAM; fallback.model unchanged." >&2; exit 1
    fi
    echo "   fits: ${v} MiB, all on the backup's GPU"
    cp "$CFG" "$CFG.bak-$(date +%Y%m%d-%H%M)-model"
    yset fallback model "$PINNED"
    "$REPO/server/scripts/deploy_fallback.sh" >/dev/null
    echo "fallback.model -> $PINNED (from $new); deploy_fallback.sh done."
    echo "Bench it on the backup: server/scripts/bench_latency.py --fallback (from its checkout)"
}

case "${1:-}" in
    --check) check ;;
    --fallback) [ -n "${2:-}" ] || { echo "usage: $0 --fallback <model>" >&2; exit 2; }; fallback "$2" ;;
    ""|-h|--help) sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) primary "$1" ;;
esac
