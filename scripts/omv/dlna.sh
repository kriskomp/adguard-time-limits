#!/usr/bin/env bash
# /opt/sh/dlna.sh - włącza/wyłącza MiniDLNA przez API OMV (omv-rpc), spójnie z panelem OMV.
# Użycie (jako root): dlna.sh status | start | stop
# Przez SSH (klucz z command="/opt/sh/dlna.sh",restrict) polecenie przychodzi w SSH_ORIGINAL_COMMAND.
# Wynik (stdout): "enabled=true|false active=active|inactive"
# Kody wyjścia: 0 ok, 2 błąd API OMV, 3 stan po zmianie nie zgadza się z oczekiwanym, 64 zły argument.
set -u
OMV_USER="${OMV_USER:-admin}"
SERVICE="${SERVICE:-minidlna}"
WAIT_S="${WAIT_S:-90}"
RPC=(omv-rpc -u "$OMV_USER")

exec 9>/var/lock/dlna.lock
flock -w 120 9 || { echo "ERROR: nie udało się zająć blokady" >&2; exit 2; }

svc_state() { if systemctl is-active --quiet "$SERVICE"; then echo active; else echo inactive; fi; }
rpc_failed() { printf '%s' "$1" | grep -q '"error":{'; }
json_enable() { python3 -c 'import sys,json; print("true" if json.load(sys.stdin).get("enable") else "false")'; }

get_settings() {
  local s
  s=$("${RPC[@]}" MiniDlna getSettings 2>&1) || true
  if rpc_failed "$s" || ! printf '%s' "$s" | python3 -c 'import sys,json; json.load(sys.stdin)' 2>/dev/null; then
    echo "ERROR getSettings: ${s:0:300}" >&2; exit 2
  fi
  printf '%s' "$s"
}

status() { echo "enabled=$(get_settings | json_enable) active=$(svc_state)"; }

apply_changes() {
  local out
  out=$("${RPC[@]}" Config applyChanges '{"modules":["minidlna"],"force":true}' 2>&1) || true
  if rpc_failed "$out"; then echo "ERROR applyChanges: ${out:0:300}" >&2; exit 2; fi
}

set_enable() {
  local want="$1" settings cur new out want_svc waited=0
  settings=$(get_settings)
  cur=$(printf '%s' "$settings" | json_enable)
  [ "$want" = true ] && want_svc=active || want_svc=inactive

  if [ "$cur" = "$want" ] && [ "$(svc_state)" = "$want_svc" ]; then
    status; exit 0                       # już zgodne - nic nie robimy (bez zbędnego wdrożenia)
  fi

  if [ "$cur" != "$want" ]; then         # zmiana ustawienia "Włączone" w OMV (pozostałe pola bez zmian)
    new=$(printf '%s' "$settings" | python3 -c 'import sys,json; d=json.load(sys.stdin); d["enable"]=(sys.argv[1]=="true"); print(json.dumps(d))' "$want")
    out=$("${RPC[@]}" MiniDlna setSettings "$new" 2>&1) || true
    if rpc_failed "$out"; then echo "ERROR setSettings: ${out:0:300}" >&2; exit 2; fi
  fi
  apply_changes                          # tylko moduł minidlna; wdraża też, gdy usługa rozjechała się z ustawieniem

  while [ "$(svc_state)" != "$want_svc" ] && [ "$waited" -lt "$WAIT_S" ]; do sleep 2; waited=$((waited + 2)); done
  status
  [ "$(svc_state)" = "$want_svc" ] || exit 3
}

# polecenie: argument albo (przy forced command w authorized_keys) to, co klient podał w ssh
CMD="${1:-${SSH_ORIGINAL_COMMAND:-}}"

case "$CMD" in
  status) status ;;
  start)  set_enable true ;;
  stop)   set_enable false ;;
  *) echo "Użycie: $0 status|start|stop" >&2; exit 64 ;;
esac
