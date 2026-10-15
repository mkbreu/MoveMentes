# -*- coding: utf-8 -*-
"""Quem publica no grupo, e o que sai em cada horario.

DUAS PERGUNTAS QUE A AUTOMACAO FAZ AO SISTEMA

1. "De qual WhatsApp eu mando agora?"
   Cada admin tem o proprio WhatsApp ligado. A cidade escolhe um PRINCIPAL e,
   opcionalmente, um SUPLENTE. Se nenhum dos dois estiver de pe, cai no
   WhatsApp do master supremo, que esta no grupo de todas as cidades.

   REGRA QUE NAO SE QUEBRA: sai UM, nunca dois. Duas pessoas publicando o
   mesmo ranking significa mensagem repetida no grupo — e ninguem reclama de
   mensagem que chegou duas vezes, so acha o sistema desleixado. Por isso
   quem escolhe e a aplicacao, e o orquestrador obedece; ele nao decide.

2. "O que eu mando neste horario?"
   Os horarios sairam do Agendador do Windows e vieram para ca. Cada horario
   diz o que sai nele: ranking, patrocinadores, parabens, avisos, promocoes.
   Assim a cidade manda o ranking 2x ao dia mas as promocoes so 1x.

O QUE ISTO NAO FAZ: nao envia nada. Só responde quem e o que. Quem entrega e
o orquestrador, que tem acesso ao WAHA.
"""
import logging
from datetime import datetime

from app import db, secretstore
from app.models import PostagemAgenda, Usuario, TIPO_SUPREMO

logger = logging.getLogger(__name__)

# Horarios que a instalacao usava antes desta tela existir. Viram a agenda
# inicial de toda cidade, para ninguem ficar sem publicar por falta de
# cadastro.
AGENDA_PADRAO = (
    # (horario, ranking, patrocinadores, parabens, avisos, promocoes)
    ('10:30', True, True, True, True, True),
    ('22:00', True, True, True, True, False),
)


def garantir_agenda(cidade):
    """Toda cidade nasce com os dois horarios de sempre. Idempotente."""
    if PostagemAgenda.query.filter_by(cidade=cidade).first():
        return False
    for horario, rk, pt, pb, av, pr in AGENDA_PADRAO:
        db.session.add(PostagemAgenda(
            cidade=cidade, horario=horario, ativo=True,
            envia_ranking=rk, envia_patrocinadores=pt, envia_parabens=pb,
            envia_avisos=av, envia_promocoes=pr, criado_em=datetime.now(),
        ))
    db.session.commit()
    logger.info('Agenda padrao criada para %s', cidade)
    return True


def agenda(cidade):
    garantir_agenda(cidade)
    return PostagemAgenda.query.filter_by(cidade=cidade).order_by(
        PostagemAgenda.horario.asc()).all()


def _minutos(hhmm):
    try:
        h, m = str(hhmm).split(':')
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return None


def horario_de_agora(cidade, agora=None, tolerancia_min=90):
    """Qual horario da agenda esta valendo agora.

    A tolerancia existe porque a maquina liga atrasada: se o horario era
    10:30 e ela so subiu 10:52, ainda e a rodada das 10:30. Passada a
    tolerancia, a rodada e considerada perdida — melhor nao publicar do que
    publicar o ranking da manha as 3 da tarde.
    """
    agora = agora or datetime.now()
    minuto_atual = agora.hour * 60 + agora.minute
    candidatos = []
    for item in agenda(cidade):
        if not item.ativo:
            continue
        alvo = _minutos(item.horario)
        if alvo is None:
            continue
        atraso = minuto_atual - alvo
        if 0 <= atraso <= tolerancia_min:
            candidatos.append((atraso, item))
    if not candidatos:
        return None
    candidatos.sort(key=lambda par: par[0])
    return candidatos[0][1]


def _sessao_do_usuario(usuario):
    """Nome da sessao WAHA daquele admin: <papel>-<cidade>.

    Ex.: `principal-palmeiropolis`, `suplente-minacu`.

    POR QUE PAPEL E NAO O NOME DA PESSOA
    O nome dizia quem era (`cidade-usuario`), mas quem opera precisa
    saber o que aquela sessao FAZ. Na madrugada de 02/08 se perdeu tempo com
    duas sessoes chamadas `palmeiropolis` e `suplente` em que a chamada
    "suplente" era a do numero principal — o nome mentia sobre o papel. Com
    `principal-<cidade>` isso nao acontece: ler o nome basta.

    Um WhatsApp por admin continua valendo — o admin de Palmeiropolis nunca
    esbarra na conversa do admin de Minacu, porque a cidade esta no nome.

    Quem nao tem ordem de envio (nao publica) cai para o id, para nunca
    colidir com o principal nem com o suplente.
    """
    from app import whatsapp as wa
    # A cidade aqui é a FORMA CURTA (P-64): Palmeirópolis é `pls`, herdado.
    # Usar o slug cru gerava `principal-palmeiropolis` enquanto a instância
    # real se chamava `principal-pls` — e o site nunca reencontrava a sessão.
    base = wa.cidade_curta(usuario.cidade)
    ordem = getattr(usuario, 'ordem_envio', 0) or 0
    if ordem == 1:
        papel = 'principal'
    elif ordem == 2:
        papel = 'suplente'
    elif ordem:
        papel = 'reserva%d' % ordem
    else:
        papel = 'u%d' % usuario.id
    return '%s-%s' % (papel, base)


def candidatos_envio(cidade, tipo='ranking'):
    """Quem pode publicar, na ordem. O primeiro de pe e quem manda.

    tipo: 'ranking' ou 'avisos' (logos, parabens e promocoes seguem avisos).
    Devolve lista de dicionarios; o orquestrador tenta na ordem e para no
    primeiro que responder.
    """
    campo = Usuario.envia_ranking if tipo == 'ranking' else Usuario.envia_avisos
    escolhidos = Usuario.query.filter(
        Usuario.cidade == cidade,
        Usuario.ativo.is_(True),
        campo.is_(True),
    ).order_by(Usuario.ordem_envio.asc(), Usuario.id.asc()).all()

    saida = []
    for pos, usuario in enumerate(escolhidos, start=1):
        saida.append({
            'papel': 'principal' if pos == 1 else ('suplente' if pos == 2 else 'reserva'),
            'usuario_id': usuario.id,
            'nome': usuario.nome,
            'telefone': usuario.telefone,
            'sessao': _sessao_do_usuario(usuario),
            'ordem': usuario.ordem_envio or pos,
        })

    # Ultimo recurso: o master supremo, que esta no grupo de todas as cidades.
    # So entra se ninguem da cidade estiver configurado ou de pe — e vem
    # marcado como 'socorro' para o orquestrador avisar que precisou dele.
    supremo = Usuario.query.filter_by(tipo=TIPO_SUPREMO, ativo=True).order_by(
        Usuario.id.asc()).first()
    if supremo and supremo.telefone and not any(
            c['usuario_id'] == supremo.id for c in saida):
        saida.append({
            'papel': 'socorro',
            'usuario_id': supremo.id,
            'nome': supremo.nome,
            'telefone': supremo.telefone,
            'sessao': _sessao_do_usuario(supremo),
            'ordem': 99,
        })
    return saida


def responsavel_alertas(cidade):
    """Para quem vai o aviso quando algo falha NESTA cidade.

    Autonomia de cidade: o problema de Minacu chega ao admin de Minacu, nao
    ao dono da instalacao. So o que exige o dono (servidor fora do ar, banco
    corrompido) sobe para ele.
    """
    from app import whatsapp as wa
    numero = wa.numero_responsavel(cidade)
    if numero:
        return numero
    dono_da_cidade = Usuario.query.filter(
        Usuario.cidade == cidade, Usuario.ativo.is_(True),
        Usuario.telefone.isnot(None),
    ).order_by(Usuario.ordem_envio.asc(), Usuario.id.asc()).first()
    if dono_da_cidade:
        return dono_da_cidade.telefone
    supremo = Usuario.query.filter_by(tipo=TIPO_SUPREMO, ativo=True).first()
    return supremo.telefone if supremo else None


def conflitos(cidade):
    """Problemas de configuracao que fazem a cidade publicar errado.

    Devolve uma lista de frases em portugues claro para a tela mostrar. E de
    proposito que isto seja uma FUNCAO e nao uma validacao no salvar: o admin
    precisa poder salvar pela metade e voltar depois, mas nunca sem ver o
    aviso.
    """
    problemas = []
    for tipo, rotulo in (('ranking', 'o ranking'), ('avisos', 'os avisos e logos')):
        lista = [c for c in candidatos_envio(cidade, tipo) if c['papel'] != 'socorro']
        if not lista:
            problemas.append(
                f'Ninguém está marcado para enviar {rotulo}. Enquanto ficar assim, '
                'o sistema vai usar o WhatsApp do master supremo.')
            continue
        ordens = [c['ordem'] for c in lista]
        if len(set(ordens)) != len(ordens):
            problemas.append(
                f'Duas pessoas estão na mesma posição para enviar {rotulo}. '
                'O sistema envia por uma só, mas defina quem é o principal.')
        sem_telefone = [c['nome'] for c in lista if not c['telefone']]
        if sem_telefone:
            problemas.append(
                'Sem telefone cadastrado: ' + ', '.join(sem_telefone) +
                '. Sem número, o WhatsApp dessa pessoa não pode ser usado.')
        if len(lista) == 1:
            problemas.append(
                f'Só há uma pessoa para enviar {rotulo}. Se o WhatsApp dela cair, '
                'a cidade fica muda até alguém perceber — marque um suplente.')
    if not PostagemAgenda.query.filter_by(cidade=cidade, ativo=True).first():
        problemas.append('Nenhum horário de publicação está ativo.')
    return problemas
