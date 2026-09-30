#!/usr/bin/env bash
# Live smoke test on a THROWAWAY Hermes profile. Manual, not part of `check`.
#
#   tests/smoke.sh TEST_CONFIG_YAML [TEST_ENV_FILE]
#
# TEST_CONFIG_YAML (and optionally TEST_ENV_FILE with the chat model's API key) must be
# files you prepared for testing. The script refuses anything under ~/.hermes and
# never reads or writes a real Hermes profile. It installs the plugin into a temp
# profile, turns built-in memory off there, and runs two one-shot chats: "remember
# X", then "what's X?". It makes real model calls. It leaves the temp profile behind
# for inspection (the hook trace is in its logs).
set -euo pipefail

usage="usage: smoke.sh TEST_CONFIG_YAML [TEST_ENV_FILE]"
config=$(realpath "${1:?$usage}")
envfile=""
if [ $# -ge 2 ]; then envfile=$(realpath "$2"); fi
real_home=$(realpath -m "$HOME/.hermes")
for f in "$config" $envfile; do
  case "$f" in
    "$real_home"/*) echo "refusing $f: it's inside your real Hermes profile ($real_home); use a test copy" >&2; exit 2 ;;
  esac
done

plugin=$(realpath "$(dirname "$0")/..")
tmp=$(mktemp -d -t memory-buckets-smoke.XXXXXX)
home=$tmp/home
mkdir -p "$home/plugins/memory-buckets"
# The plugin directory as Hermes loads it: the repo root minus tests, venv and VCS.
(cd "$plugin" && cp -r plugin.yaml __init__.py cli.py memory_buckets skills LICENSE "$home/plugins/memory-buckets/")
find "$home/plugins/memory-buckets" -name __pycache__ -prune -exec rm -rf {} +
cp "$config" "$home/config.yaml"
if [ -n "$envfile" ]; then cp "$envfile" "$home/.env"; fi

export HERMES_HOME=$home MEMORY_BUCKETS_TRACE=1
hermes config set memory.provider memory-buckets >/dev/null
hermes config set memory.memory_enabled false >/dev/null
hermes config set memory.user_profile_enabled false >/dev/null

echo "profile: $home"
hermes memory-buckets status || true

fact="my favourite tea is lapsang souchong"
hermes chat -q "Please remember this about me: $fact." --oneshot --format stream-json >"$tmp/run1.jsonl" 2>"$tmp/run1.err"
hermes chat -q "What's my favourite tea? Check your memory." --oneshot --format stream-json >"$tmp/run2.jsonl" 2>"$tmp/run2.err"

fail=0
check() { if eval "$2"; then echo "ok    $1"; else echo "FAIL  $1"; fail=1; fi; }
check "run 1 called a memory_* tool" "grep -q '\"memory_\\(write\\|append\\|str_replace\\)' '$tmp/run1.jsonl'"
check "the fact landed in the store" "grep -rqi lapsang '$home/memory-buckets/memories'"
check "run 2 read memory" "grep -q '\"memory_\\(read\\|search\\|list\\)' '$tmp/run2.jsonl' || grep -qi lapsang '$tmp/run2.jsonl'"
check "run 2 answered with the fact" "grep -qi lapsang '$tmp/run2.jsonl'"
echo
echo "hooks Hermes called (S7/S9/S10 evidence):"
grep -rhoE "memory-buckets hook [a-z_]+" "$home/logs" 2>/dev/null | sort | uniq -c || echo "  (no trace found in $home/logs)"
echo
echo "store files:"
find "$home/memory-buckets/memories" -name '*.md' 2>/dev/null | sed "s|$home/memory-buckets/memories/|  |"
echo "left in $tmp"
exit $fail
