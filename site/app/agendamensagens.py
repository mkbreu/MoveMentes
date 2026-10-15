# -*- coding: utf-8 -*-
"""Quem enfileira as mensagens do mes SEM ninguem apertar botao.

O QUE FALTAVA
As mensagens do ciclo (agradecimento, encerramento, abertura, medalha) eram
montadas e mostradas na tela, mas so entravam na fila quando o admin clicava
"enviar agora". Ou seja: a trilha "acontece sozinho" nao acontecia sozinha.
Este modulo e a peca que faltava.

COMO FUNCIONA
Uma vez por rodada (a rotina diaria do site chama, e a automacao tambem pode
chamar pela API), para cada cidade:

  1. pergunta ao app/mensagens.py o que faz sentido HOJE;
  2. filtra pelo CALENDARIO abaixo — nem tudo que faz sentido ver na tela
     deve sair sozinho no mesmo dia;
  3. enfileira o que passou, com chave de dedupe.

A chave de dedupe e a MESMA que o botao "enviar agora" usa (`ciclo:<chave>`).
Por isso nao existe risco de sair duas vezes: se o admin adiantou no dia 31,
a rotina do dia 1º ve que ja saiu e nao repete.

O QUE NAO SAI SOZINHO DAQUI
Anti-cheat e promocao de patrocinador. Sao a trilha "esperando voce" — sao
assuntos sensiveis e continuam dependendo de o admin apertar o botao, mesmo
depois de tres avisos no mes.
"""
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# Em que dias do mes cada tipo pode sair sozinho.
#
# A janela vai ate o dia 5 de proposito: se a maquina do WhatsApp passou o dia
# 1º desligada, a mensagem ainda sai no dia 2 ou 3 em vez de se perder. O
# dedupe garante que a janela larga nao vira repeticao.
CALENDARIO = {
    'encerramento':  (1, 5),    # o grupo sabe primeiro que o mes fechou
    'agradecimento': (1, 5),    # privado de cada patrocinador
    'abertura':      (1, 5),    # metas e patrocinador do mes novo
    # MEDALHA COMECA NO DIA 8, nao no 2 (decidido pelo dono em 07/08/2026).
    # Antes do dia 8 o patrocinador raramente tem as medalhas na mao e o local
    # ainda esta sendo combinado — avisar cedo faz a pessoa ir buscar e voltar
    # sem nada, que e pior do que avisar depois.
    # E ela NUNCA sai sozinha: ver _pedir_autorizacao_medalha.
    'medalha':       (8, 10),
    'patrocinio':    (1, 31),   # anuncio de patrocinador, assim que houver
}

# Ordem em que entram na fila. O grupo ve o mes fechar, depois o mes abrir, e
# so entao comeca a chegar mensagem no privado — a ordem inversa confunde.
ORDEM = ('encerramento', 'agradecimento', 'abertura', 'patrocinio', 'medalha')


def _pode_hoje(tipo, dia):
    faixa = CALENDARIO.get(tipo)
    if not faixa:
        return False
    return faixa[0] <= dia <= faixa[1]


def planejar(cidade, hoje=None):
    """O que DEVERIA sair hoje nesta cidade, sem enfileirar nada.

    Existe separado de `rodar()` para a tela poder mostrar "isto sai sozinho
    hoje" e para dar para testar sem mandar mensagem para ninguem.
    """
    from app.mensagens import mensagens_do_ciclo

    hoje = hoje or datetime.now()
    try:
        itens = mensagens_do_ciclo(cidade, hoje=hoje)
    except Exception as exc:
        logger.error('Agenda de mensagens: falha ao montar o ciclo de %s: %s',
                     cidade, exc, exc_info=True)
        return []

    # `ja_feito` fica de fora do automatico (P-51). A tela passou a MOSTRAR o
    # que ja foi feito em vez de esconder — mas mostrar e enviar sao coisas
    # diferentes. Sem este filtro, o patrocinador ja anunciado voltaria a ser
    # anunciado sozinho: o "ja anunciado" e registrado fora da fila, entao o
    # anti-repeticao da fila nao pegaria esse caso.
    escolhidos = [i for i in itens
                  if _pode_hoje(i['tipo'], hoje.day) and not i.get('ja_feito')]
    escolhidos.sort(key=lambda i: (ORDEM.index(i['tipo'])
                                   if i['tipo'] in ORDEM else 99))

    # A MEDALHA NUNCA SAI SOZINHA (P-23, decidido pelo dono em 07/08/2026).
    #
    # Ela e a unica mensagem que MANDA a pessoa sair de casa. Se o dia chegar
    # com tudo pronto, o aviso sai e a galera vai buscar; se nao estiver, quem
    # foi buscar volta sem nada — e isso o sistema nao tem como desfazer.
    # Por isso a maquina nao decide: ela PERGUNTA ao admin e espera o clique.
    #
    # Marcar em vez de sumir da lista: a tela mostra "isto sai sozinho hoje", e
    # item que desaparece nao ensina nada a ninguem. Assim ela mostra a medalha
    # com o motivo, e o admin ve exatamente o que falta.
    for item in escolhidos:
        if item['tipo'] != 'medalha':
            continue
        item['automatico'] = False
        try:
            from app.mensagens import local_definido
            tem_local = local_definido(cidade)
        except Exception as exc:
            logger.warning('Agenda de mensagens: nao consegui conferir o local '
                           'de %s (%s).', cidade, exc)
            tem_local = False
        item['tem_local'] = tem_local
        item['motivo_bloqueio'] = (
            'Está tudo pronto — falta você confirmar o envio.' if tem_local else
            'O local de retirada ainda não foi definido. Defina o local e '
            'confirme o envio.')

    return escolhidos


def rodar(cidade, hoje=None, por='rotina automatica'):
    """Enfileira o que e do dia. Devolve (enfileiradas, repetidas, sem_destino)."""
    from app import filamensagens

    hoje = hoje or datetime.now()
    novas = repetidas = sem_destino = 0

    bloqueadas = 0
    for item in planejar(cidade, hoje=hoje):
        # Planejado sim, automatico nao (P-23): fica esperando o clique do
        # admin. Registrar o motivo aqui e o que evita o "sumiu e ninguem sabe
        # por que" — a rodada seguinte reavalia sozinha.
        if item.get('automatico') is False:
            bloqueadas += 1
            logger.info('Agenda de mensagens: %s NAO sai sozinha (%s) — %s',
                        item['chave'], cidade,
                        item.get('motivo_bloqueio') or 'sem motivo registrado')
            continue
        destino = item.get('telefone')
        if not destino and item.get('destino') == 'grupo':
            destino = 'grupo'
        if not destino:
            # Patrocinador ou atleta sem telefone. Nao e erro do sistema: e
            # cadastro faltando, e a tela ja mostra de quem.
            sem_destino += 1
            continue
        try:
            _id, ja_existia = filamensagens.enfileirar(
                cidade, item['tipo'], destino, item['texto'],
                dedupe='ciclo:' + item['chave'], criado_por=por)
        except Exception as exc:
            logger.error('Agenda de mensagens: nao consegui enfileirar %s: %s',
                         item['chave'], exc)
            continue
        if _id is None:
            sem_destino += 1
        elif ja_existia:
            repetidas += 1
        else:
            novas += 1
            logger.info('Agenda de mensagens: %s enfileirada (%s)',
                        item['chave'], cidade)

    if bloqueadas:
        # Assinatura inalterada de proposito: ha chamador esperando tres
        # valores. O que importa e nao passar em branco — a linha no log conta
        # o que ficou esperando gente.
        logger.info('Agenda de mensagens: %d mensagem(ns) de %s esperando o '
                    'admin (nao saem sozinhas).', bloqueadas, cidade)
        _pedir_autorizacao_medalha(cidade, hoje, por)

    return novas, repetidas, sem_destino


def _pedir_autorizacao_medalha(cidade, hoje, por):
    """Pergunta ao admin, no WhatsApp dele, se pode mandar as medalhas.

    REGRA 21-E: mensagem que pede acao leva o LINK que faz a acao. Escrever
    "abra o painel, va na central, ache o mes" obriga a pessoa a sair do
    WhatsApp, abrir o navegador, digitar o endereco e achar o menu. O link e um
    toque — e quem esta com o celular na mao nao vai fazer os quatro passos.

    UMA VEZ POR MES, nao uma por dia: o `dedupe` carrega o periodo. A janela da
    medalha e de tres dias (8 a 10) e receber a mesma pergunta tres vezes
    ensina a ignorar a pergunta.

    Nunca levanta excecao: isto e um lembrete, e derrubar a agenda inteira por
    causa dele seria trocar um problema pequeno por um grande.
    """
    from app import filamensagens, whatsapp
    from app.blueprints.ranking import MESES_PT
    try:
        from app.mensagens import mes_da_medalha, medalhistas_pendentes, local_definido
        from app.alertas import _base_url

        destino = whatsapp.numero_responsavel(cidade)
        if not destino:
            logger.warning('Medalhas de %s esperando autorizacao, mas a cidade '
                           'nao tem numero de responsavel cadastrado.', cidade)
            return

        ano, mes = mes_da_medalha(hoje)
        quantos = len(medalhistas_pendentes(cidade, ano, mes, True) or [])
        if not quantos:
            return

        link = '%s/admin/central' % _base_url().rstrip('/')
        nome_mes = MESES_PT.get(mes, str(mes))
        if local_definido(cidade):
            texto = (
                '*Medalhas de %s* — %d atleta(s) para avisar.\n\n'
                'Está tudo pronto: o local de retirada já está cadastrado.\n'
                '*Posso mandar o aviso?* Só sai quando você confirmar:\n%s'
                % (nome_mes, quantos, link))
        else:
            texto = (
                '*Medalhas de %s* — %d atleta(s) esperando.\n\n'
                'Falta o *local de retirada*. Sem ele o atleta recebe o aviso e '
                'não sabe onde buscar.\n'
                'Cadastre o local e confirme o envio aqui:\n%s'
                % (nome_mes, quantos, link))

        filamensagens.enfileirar(
            cidade, 'medalha-autorizacao', destino, texto,
            dedupe='medalha-autorizacao:%04d-%02d' % (ano, mes),
            criado_por=por)
        logger.info('Agenda de mensagens: pedi autorizacao das medalhas de '
                    '%02d/%04d a %s (%s).', mes, ano, destino, cidade)
    except Exception as exc:
        logger.error('Agenda de mensagens: nao consegui pedir autorizacao das '
                     'medalhas de %s: %s', cidade, exc, exc_info=True)


# JANELA EM QUE A VIRADA DO MES PODE ENTRAR NA FILA (P-69, 13/08/2026).
#
# A rotina diaria roda de 6 em 6 horas A PARTIR DO BOOT — se o site subiu as
# 03h, ela cai as 03h todo dia. A mensagem de encerramento e a de abertura sao
# as unicas que falam com o grupo inteiro sobre o mes: enfileirar de madrugada
# significa que a primeira coisa que 80 pessoas veem ao acordar foi escrita
# quando ninguem podia conferir.
#
# Fora da janela ela NAO enfileira e tenta de novo na proxima passada — nada se
# perde, so espera o dia comecar.
HORA_MINIMA = 8
HORA_MAXIMA = 21


def rodar_todas(hoje=None, forcar_janela=False):
    """Passa por todas as cidades. E o que a rotina diaria chama.

    Devolve o resumo por cidade. Quem falhar entra em `_falhas` e vira UM
    alerta com a lista — antes, cada falha morria no log e o mes virava sem
    ninguem saber que a mensagem nao tinha entrado na fila.
    """
    from app.models import Config

    hoje = hoje or datetime.now()
    resumo = {}

    if not forcar_janela and not (HORA_MINIMA <= hoje.hour < HORA_MAXIMA):
        logger.info('Agenda de mensagens: fora da janela (%sh-%sh) — sao %sh. '
                    'Nada enfileirado; tento na proxima passada.',
                    HORA_MINIMA, HORA_MAXIMA, hoje.hour)
        return resumo

    try:
        # CIDADE INATIVA NAO ACIONA NADA (19/09/2026, regra do dono — P-135).
        # Era toda linha de `Config`; agora so as ativas, mesmo criterio do
        # vigia (`consolidado.cidades_ativas`).
        from app import consolidado as _cons
        cidades = list(_cons.cidades_ativas())
    except Exception as exc:
        logger.error('Agenda de mensagens: nao consegui listar as cidades: %s', exc)
        return resumo

    falhas = []
    for cidade in cidades:
        try:
            novas, repetidas, sem_destino = rodar(cidade, hoje=hoje)
        except Exception as exc:
            logger.error('Agenda de mensagens: falha em %s: %s', cidade, exc,
                         exc_info=True)
            falhas.append('%s: %s' % (cidade, str(exc)[:120]))
            continue
        resumo[cidade] = {'novas': novas, 'repetidas': repetidas,
                          'sem_destino': sem_destino}
        if sem_destino:
            falhas.append('%s: %s mensagem(ns) sem destino (falta telefone)'
                          % (cidade, sem_destino))
        if novas:
            logger.warning('Agenda de mensagens: %s mensagem(ns) nova(s) na fila de %s',
                           novas, cidade)

    # UM alerta com a LISTA, nunca um por falha (P-69). Quem recebe precisa
    # saber o tamanho do estrago numa olhada.
    if falhas:
        _avisar_falhas_da_virada(falhas, hoje)
    return resumo


def _avisar_falhas_da_virada(falhas, quando):
    """Um aviso só, com a lista do que não entrou na fila. Nunca levanta erro."""
    from app import alertas
    from app.models import Config
    try:
        cidade = next((c.cidade for c in Config.query.all() if c.cidade), None)
        if not cidade:
            return
        alertas.registrar_falha(
            cidade, 'Virada do mes',
            'Estas mensagens do ciclo NAO entraram na fila em %s:\n- %s\n\n'
            'O QUE FAZER:\n'
            '1) Abra a Central de Mensagens e veja quais faltam.\n'
            '2) O que estiver sem telefone se resolve na tela de Telefones.\n'
            '3) O resto sai no botao "enviar agora" da propria Central.'
            % (quando.strftime('%d/%m/%Y %H:%M'), '\n- '.join(falhas[:12])),
            origem='agenda-do-ciclo')
    except Exception as exc:
        logger.warning('Nao consegui avisar as falhas da virada: %s', exc)
