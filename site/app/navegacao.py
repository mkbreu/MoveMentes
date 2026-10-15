"""Depois de um clique, a tela volta para ONDE a pessoa estava.

POR QUE EXISTE (01/10/2026, pedido do dono)
Os botões da Central de mensagens e das Revisões do anticheat respondiam com
`redirect(url_for('admin.reviews'))` — o endereço "pelado". A tela recarregava
na aba padrão, sem o mês, sem a busca, no topo da página, e para voltar ao
item seguinte era preciso refazer todo o caminho. Medido no ensaio de 01/10:
7 botões perdiam o lugar; o "Enviar aviso" da Central ainda levava para OUTRA
tela (Medalhistas). Regra do dono: daqui para frente, toda ação volta para a
mesma tela (CLAUDE.md, regra 27).

Duas metades:
- AQUI, no servidor: o endereço de onde veio o clique (Referer), com a aba,
  o mês e a busca que estão nele. Só se for do próprio site — endereço de
  fora nunca é seguido.
- NO NAVEGADOR (`base.html`): a aba aberta e a altura da página, que não
  viajam no endereço, são lembradas antes do envio e restauradas na volta.
"""
from urllib.parse import urlparse

from flask import redirect, request, url_for


def endereco_de_volta():
    """O caminho de onde veio o clique (com a consulta), ou None."""
    origem = request.referrer or ''
    if not origem:
        return None
    alvo = urlparse(origem)
    # Só volta para o próprio site: o host tem de ser o deste pedido. Sem
    # isto, um Referer forjado viraria redirecionamento para fora.
    if alvo.netloc and alvo.netloc != request.host:
        return None
    if alvo.scheme and alvo.scheme not in ('http', 'https'):
        return None
    caminho = alvo.path or ''
    if not caminho.startswith('/') or caminho.startswith('//'):
        return None
    return caminho + (('?' + alvo.query) if alvo.query else '')


def voltar(endpoint_padrao, **valores):
    """Redireciona para onde a pessoa estava; sem origem conhecida, para o padrão."""
    return redirect(endereco_de_volta() or url_for(endpoint_padrao, **valores))
