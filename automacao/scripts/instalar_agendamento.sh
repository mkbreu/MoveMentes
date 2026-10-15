#!/usr/bin/env bash
# Instala:
#   - parada limpa dos containers no desligamento (protege a sessao do WhatsApp)
#   - ranking 2x/dia (05:30 e 15:30)
#   - checagem de saude de 6 em 6 horas
#
# Rode uma vez:  sudo ~/movimente-automacao/scripts/instalar_agendamento.sh
#
# Os timers so disparam com a distro do WSL ligada. Como esta maquina liga so
# para publicar, o normal e o Windows subir o WSL no logon (atalho
# manter_wsl_vivo.vbs em shell:startup) ou voce disparar pelo .bat do Desktop.

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo 'Rode com sudo.' >&2
  exit 1
fi

ORIGEM="$(cd "$(dirname "${BASH_SOURCE[0]}")/systemd" && pwd)"

install -m 0644 "$ORIGEM"/movimente-graceful.service /etc/systemd/system/
install -m 0644 "$ORIGEM"/movimente-ranking.service  /etc/systemd/system/
install -m 0644 "$ORIGEM"/movimente-ranking.timer    /etc/systemd/system/
install -m 0644 "$ORIGEM"/movimente-monitor.service  /etc/systemd/system/
install -m 0644 "$ORIGEM"/movimente-monitor.timer    /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now movimente-graceful.service
systemctl enable --now movimente-ranking.timer
systemctl enable --now movimente-monitor.timer

echo
echo 'Instalado. Proximos disparos:'
systemctl list-timers 'movimente-*' --no-pager
echo
echo 'Parada limpa:     systemctl status movimente-graceful.service'
echo 'Para acompanhar:  journalctl -u movimente-ranking.service -f'
echo 'Para desligar:    sudo systemctl disable --now movimente-ranking.timer'
