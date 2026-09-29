#!/usr/bin/env bash
# Publica la web del asistente en internet con un túnel gratuito de Cloudflare.
# El modelo sigue corriendo en este Mac (oMLX); el Mac debe estar encendido.
# Uso:  ./publicar.sh      (Ctrl+C para dejar de publicar)
set -uo pipefail
cd "$(dirname "$0")"

bold=$'\033[1m'; green=$'\033[32m'; red=$'\033[31m'; reset=$'\033[0m'
fail() { echo "${red}✗ $*${reset}"; exit 1; }

[ -f .env ] || fail "Falta el archivo .env. Créalo con:  cp .env.example .env  y pon tu OMLX_API_KEY."
PORT=$(grep -E '^PORT=' .env | cut -d= -f2); PORT=${PORT:-5174}
OMLX_URL=$(grep -E '^OMLX_URL=' .env | cut -d= -f2); OMLX_URL=${OMLX_URL:-http://127.0.0.1:8000}

command -v cloudflared >/dev/null || fail "Falta cloudflared. Instálalo con:  brew install cloudflared"
curl -sf -m 5 "$OMLX_URL/health" >/dev/null || fail "oMLX no responde en $OMLX_URL. Arráncalo primero."
if lsof -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  fail "El puerto $PORT ya está en uso (¿otro servidor de la web abierto?). Ciérralo y vuelve a intentarlo."
fi

LOG=$(mktemp -t cloudflared)
cleanup() {
  trap - EXIT INT TERM
  echo; echo "Deteniendo…"
  kill "${SERVER_PID:-}" "${TUNNEL_PID:-}" "${AWAKE_PID:-}" 2>/dev/null
  rm -f "$LOG"
  echo "La web ya no está publicada."
}
trap cleanup EXIT INT TERM

python3 server.py & SERVER_PID=$!
sleep 1
kill -0 "$SERVER_PID" 2>/dev/null || fail "No se pudo arrancar server.py."

caffeinate -i -w $$ & AWAKE_PID=$!   # evita que el Mac se duerma mientras se publica

echo "Abriendo túnel con Cloudflare…"
cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" >"$LOG" 2>&1 & TUNNEL_PID=$!

URL=""
for _ in $(seq 1 45); do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1)
  [ -n "$URL" ] && break
  kill -0 "$TUNNEL_PID" 2>/dev/null || break
  sleep 1
done
[ -n "$URL" ] || { cat "$LOG"; fail "No se pudo abrir el túnel (ver mensajes de arriba)."; }

echo
echo "${bold}${green}✓ Web publicada en:${reset}  ${bold}$URL${reset}"
echo "  Local:              http://localhost:$PORT"
echo "  Comparte la URL pública. Cambia cada vez que vuelves a ejecutar este script."
echo "  Mantén este Mac encendido y oMLX en marcha. Ctrl+C para dejar de publicar."
echo
echo "Consultas recibidas:"

wait "$SERVER_PID" "$TUNNEL_PID"
