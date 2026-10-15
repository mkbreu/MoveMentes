# -*- coding: utf-8 -*-
"""Acordar o computador da automacao pela rede (Wake-on-LAN).

POR QUE ISTO EXISTE
O servidor do site fica ligado o tempo todo, mas o computador que roda o
WhatsApp passa o dia desligado. Quando o admin de uma cidade precisa vincular
o proprio numero, ele dependia de alguem ir la e apertar o botao.

Com este modulo ele aperta o botao daqui. O servidor manda pela rede um
"pacote magico" — a placa de rede do computador desligado escuta esse pacote e
liga a maquina. Nao e magica nem invasao: e um recurso da propria placa, que
precisa estar habilitado na BIOS e no Windows.

O QUE ISTO NAO FAZ
Nao garante que ligou. Nao existe resposta: o pacote e disparado e pronto.
Quem confirma e a propria automacao aparecendo depois. Por isso a tela diz
"pedi para ligar", nunca "ligado".

LIMITE HONESTO: so funciona dentro da MESMA REDE do servidor. Se o
computador estiver em outra casa, nao ha o que fazer daqui.
"""
import logging
import re
import socket

from app import secretstore

logger = logging.getLogger(__name__)

PORTAS = (9, 7)  # 9 e a porta padrao do Wake-on-LAN; 7 e a alternativa antiga


def normalizar_mac(bruto):
    """'00:1A:2B:3C:4D:5E', '00-1a-2b-3c-4d-5e' ou '001a2b3c4d5e' -> 12 hex."""
    limpo = re.sub(r'[^0-9A-Fa-f]', '', str(bruto or ''))
    return limpo.lower() if len(limpo) == 12 else None


def normalizar_macs(bruto):
    """Uma OU MAIS placas, separadas por vírgula/ponto-e-vírgula/quebra de
    linha (08/09/2026): a máquina pode dormir suspensa (S3) com a placa de
    rede cabeada OU a Wi-Fi ativa, dependendo se há cabo conectado -- o
    pacote mágico precisa alcançar QUALQUER uma das duas, sem saber de
    antemão qual estava ligada na hora de dormir. Devolve lista sem
    duplicatas, na ordem em que apareceram; item invalido é descartado (não
    derruba os demais).
    """
    partes = re.split(r'[,;\n]+', str(bruto or ''))
    vistos = []
    for parte in partes:
        mac = normalizar_mac(parte)
        if mac and mac not in vistos:
            vistos.append(mac)
    return vistos


def mac_bonito(mac):
    m = normalizar_mac(mac)
    if not m:
        return ''
    return ':'.join(m[i:i + 2] for i in range(0, 12, 2)).upper()


def configurado(cidade=None):
    """A cidade tem endereco de maquina cadastrado?"""
    return bool(dados(cidade)[0])


def dados(cidade=None):
    """(lista de MACs normalizados, endereco de difusao). Lista vazia = nada
    cadastrado. Uma cidade que so tem uma placa continua com lista de 1."""
    mac = secretstore.get(secretstore.WOL_MAC, scope=cidade)
    difusao = (secretstore.get(secretstore.WOL_BROADCAST, scope=cidade)
               or '255.255.255.255')
    return normalizar_macs(mac), difusao


def _pacote_magico(mac):
    """6 bytes 0xFF seguidos do MAC repetido 16 vezes — o formato do padrao."""
    return b'\xff' * 6 + bytes.fromhex(mac) * 16


def ligar(cidade=None):
    """Manda o pacote para CADA placa cadastrada. Devolve (ok, mensagem em
    portugues claro).

    MAIS DE UMA PLACA (08/09/2026): a maquina pode ter dormido suspensa com o
    cabo conectado (acorda pela Ethernet) ou sem cabo (acorda pela Wi-Fi, sem
    garantia) -- o pedido de ligar nao sabe qual das duas era, entao manda
    para todas as cadastradas. Basta UMA chegar.
    """
    macs, difusao = dados(cidade)
    if not macs:
        return False, ('Nenhum computador cadastrado para ligar. Peça ao '
                       'responsável para cadastrar o endereço da máquina.')
    enviados = 0
    macs_ok = []
    erro = None
    for mac in macs:
        pacote = _pacote_magico(mac)
        for porta in PORTAS:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    s.settimeout(5)
                    s.sendto(pacote, (difusao, porta))
                enviados += 1
                if mac not in macs_ok:
                    macs_ok.append(mac)
            except OSError as exc:
                erro = exc
                logger.warning('Wake-on-LAN falhou em %s:%s (mac %s) — %s',
                               difusao, porta, mac_bonito(mac), exc)
    if not enviados:
        return False, (f'Não consegui enviar o sinal pela rede ({erro}). '
                       'O servidor pode não estar na mesma rede da máquina.')
    logger.warning('Wake-on-LAN enviado para %s (%s) — cidade %s',
                   ', '.join(mac_bonito(m) for m in macs_ok), difusao, cidade)
    return True, ('Pedi para o computador ligar. Ele costuma levar de 1 a 3 '
                  'minutos para ficar pronto — atualize esta página daqui a pouco. '
                  'Se não ligar, a placa de rede pode estar com o recurso '
                  'desativado na BIOS.')
