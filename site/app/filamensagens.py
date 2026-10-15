# -*- coding: utf-8 -*-
"""Fila de mensagens para o WhatsApp — o que sai quando a maquina ligar.

POR QUE ESTE MODULO EXISTE
O site fica ligado o tempo todo; o computador que fala com o WhatsApp passa o
dia desligado. Toda vez que alguem aperta "enviar" no site, a mensagem tinha
duas saidas ruins: falhar na cara da pessoa, ou o site fingir que enviou.

Aqui existe uma terceira: a mensagem entra na fila e sai assim que a maquina
subir. Quem aperta o botao ve "vai sair quando a automacao ligar" — que e a
verdade — e ninguem precisa lembrar de refazer.

O QUE PASSA POR AQUI
  aviso_atleta   - o texto de tom leve sobre uma revisao de anticheat
  anticheat_adm  - o alerta de suspeita para o admin da cidade decidir
  promocao_adm   - o pedido de autorizacao de promocao de patrocinador
  alerta         - falha de sistema para o responsavel da cidade
  livre          - qualquer texto que uma tela precise despachar

REGRAS
- Cada item pertence a uma CIDADE. O orquestrador so leva o que e da cidade
  do token que ele apresentou.
- Item com `dedupe` so entra uma vez enquanto estiver pendente: apertar o
  botao duas vezes nao manda a mensagem duas vezes.
- Nada aqui envia nada. Este modulo so guarda e entrega a lista.
"""
import json
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy import text as sa_text

from app import db
from app import telefone

logger = logging.getLogger(__name__)

PENDENTE = 'pendente'
ENVIADA = 'enviada'
FALHOU = 'falhou'
CANCELADA = 'cancelada'

MAX_TENTATIVAS = 3
_SCHEMA_PRONTO = False


def ensure_schema():
    global _SCHEMA_PRONTO
    if _SCHEMA_PRONTO:
        return
    with db.engine.begin() as conn:
        conn.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS fila_mensagens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                tipo VARCHAR(30) NOT NULL,
                destino VARCHAR(40) NOT NULL,
                texto TEXT NOT NULL,
                imagem_blob BLOB,
                imagem_mime VARCHAR(60),
                imagem_nome VARCHAR(200),
                mencionar VARCHAR(40),
                dedupe VARCHAR(160),
                situacao VARCHAR(20) NOT NULL DEFAULT 'pendente',
                tentativas INTEGER NOT NULL DEFAULT 0,
                criado_em DATETIME NOT NULL,
                criado_por VARCHAR(120),
                enviado_em DATETIME,
                detalhe VARCHAR(300)
            )
        """))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_fila_situacao "
            "ON fila_mensagens (situacao, cidade, criado_em)"))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_fila_dedupe "
            "ON fila_mensagens (dedupe, situacao)"))
    _SCHEMA_PRONTO = True


def token_imagem(mensagem_id):
    """Token do link da imagem da fila. Assinado, amarrado a UMA mensagem.

    POR QUE EXISTE (14/08/2026 — a causa da fila travada a noite inteira)
    O WAHA nao busca a imagem com a nossa chave: nos damos uma URL a ele e ele
    baixa sozinho, sem cabecalho nenhum. Como `/api/*` exige o token da
    automacao no cabecalho, ele recebia **HTTP 401 com 41 bytes de JSON**,
    entregava esse JSON ao WhatsApp como se fosse foto, e o whatsapp-web.js
    morria dentro do navegador — HTTP 500 na nossa cara, sem dizer por que.

    Resultado: toda cobranca de medalha (que leva a logo do patrocinador)
    falhava 3 vezes e morria. Em 14/08 uma ficou parada desde a madrugada,
    alertando de 15 em 15 minutos, com a maquina ligada e a sessao boa.

    Quem nao pode mandar cabecalho tem que levar a credencial na URL. Mesmo
    desenho do QR de pareamento (`whatsapp.token_qr`): assinado com a
    SECRET_KEY, curto, e valido so para aquela mensagem.
    """
    import hashlib
    from flask import current_app
    chave = (current_app.config.get('SECRET_KEY') or '')
    if isinstance(chave, str):
        chave = chave.encode('utf-8')
    return hashlib.sha256(b'fila-img:%d:' % int(mensagem_id)
                          + chave).hexdigest()[:24]


def enfileirar(cidade, tipo, destino, texto, imagem=None, mencionar=None,
               dedupe=None, criado_por=None, forcar=False):
    """Coloca uma mensagem na fila. Devolve (id, ja_existia).

    `destino` e o telefone (so digitos) ou a palavra 'grupo'.
    `imagem` e a tupla (blob, mime, nome) quando houver anexo.
    `mencionar` e o telefone a marcar com @ dentro do texto.
    `dedupe` evita repetir: a mesma chave nao entra de novo enquanto a
    anterior estiver pendente OU ja tiver sido enviada.
    `forcar=True` ignora a trava — e o "mandar de novo" consciente do admin.
    """
    ensure_schema()
    if not destino or not (texto or '').strip():
        return None, False

    # O destino entra SEMPRE cru (55DD9XXXXXXXX) ou a palavra 'grupo'. Ja
    # entrou "(DD) 9XXXX-XXXX" aqui — o WhatsApp recusa e a mensagem ficava
    # eternamente na fila, tentando e falhando sem ninguem entender por que.
    if str(destino).strip().lower() != 'grupo':
        limpo = telefone.normalizar(destino)
        if not limpo:
            logger.warning('Fila: destino invalido "%s" (%s/%s) — nao enfileirado',
                           destino, cidade, tipo)
            return None, False
        destino = limpo
    else:
        destino = 'grupo'

    # PODE SER UMA PESSOA OU MUITAS (15/08/2026). O aviso de medalhas no grupo
    # marca os 14 medalhistas de uma vez; antes so cabia um telefone aqui.
    # Guardado separado por virgula — a coluna e VARCHAR, e no SQLite o tamanho
    # declarado nao limita nada.
    if mencionar:
        if isinstance(mencionar, (list, tuple, set)):
            numeros = [telefone.normalizar(m) for m in mencionar]
            mencionar = ','.join([n for n in numeros if n]) or None
        else:
            mencionar = telefone.normalizar(mencionar) or None

    if dedupe and not forcar:
        # Bloqueia tanto o que esta ESPERANDO quanto o que JA SAIU. So o
        # pendente nao bastava: assim que a mensagem era entregue a trava
        # sumia e a rotina automatica mandava tudo de novo no dia seguinte.
        ja = db.session.execute(sa_text(
            "SELECT id FROM fila_mensagens WHERE dedupe = :d "
            "AND situacao IN (:p, :e) ORDER BY id DESC LIMIT 1"
        ), {'d': dedupe, 'p': PENDENTE, 'e': ENVIADA}).fetchone()
        if ja:
            return ja[0], True

    blob = mime = nome = None
    if imagem:
        blob, mime, nome = imagem

    res = db.session.execute(sa_text("""
        INSERT INTO fila_mensagens
            (cidade, tipo, destino, texto, imagem_blob, imagem_mime, imagem_nome,
             mencionar, dedupe, situacao, tentativas, criado_em, criado_por)
        VALUES (:c, :t, :d, :tx, :ib, :im, :inome, :men, :dedupe, :sit, 0, :em, :por)
    """), {'c': cidade, 't': tipo, 'd': str(destino), 'tx': texto,
           'ib': blob, 'im': mime, 'inome': nome, 'men': mencionar,
           'dedupe': dedupe, 'sit': PENDENTE, 'em': datetime.now(),
           'por': criado_por})
    db.session.commit()
    logger.info('Mensagem na fila: %s / %s / destino %s', cidade, tipo, destino)
    return res.lastrowid, False


def entregar_agora(cidade, limite=10, apenas_id=None):
    """Entrega a fila JÁ, se a máquina da automação estiver de pé.

    POR QUE (item 10, 14/08/2026)
    A drenagem mora no Dell e roda de 15 em 15 minutos. Quando o admin aperta
    "enviar" com a máquina ligada, esperar até 15 minutos por nada é o que faz
    ele achar que o sistema travou — foi o caso medido: pedido às 20:22,
    chegada às 20:36.

    O site já tem tudo para entregar sozinho: endereço e chave do WAHA no
    cofre. Aqui ele tenta; se não der, a mensagem continua na fila e o Dell
    entrega no ciclo, exatamente como antes. **Nunca piora o caso ruim.**

    Devolve (entregues, motivo). Nunca levanta exceção.
    """
    import requests
    from app import maquina, secretstore

    ensure_schema()
    # NAO PERGUNTAR "A MAQUINA ESTA BOA?" ANTES DE TENTAR (15/08/2026).
    #
    # Isto exigia `estado == LIGADA`, que e uma segunda opiniao sobre a mesma
    # coisa — e ela erra: em 15/08 o site marcou a maquina como "quebrada"
    # durante um reinicio de container e recusou entregar uma mensagem que o
    # WhatsApp teria aceitado. Quem responde se da para entregar e o proprio
    # WAHA, na tentativa. Falhando, a mensagem continua pendente exatamente
    # como estava (regra 16-P: atalho, nunca dono).
    #
    # `DESLIGADA` continua saindo cedo: e o caso normal (a maquina dorme), e
    # tentar contra uma porta muda so gasta o tempo de espera do socket.
    try:
        estado = maquina.estado(cidade)
    except Exception as exc:
        return 0, 'não consegui ver a máquina (%s)' % exc
    if estado.get('situacao') == maquina.DESLIGADA:
        return 0, 'a máquina está dormindo — a fila sai no próximo ciclo'
    if estado.get('situacao') == maquina.SEM_ENDERECO:
        return 0, 'sem endereço do WhatsApp no cofre'

    base = (secretstore.get(secretstore.WAHA_URL, scope=cidade) or '').rstrip('/')
    chave = secretstore.get(secretstore.WAHA_API_KEY, scope=cidade) or ''
    if not base:
        return 0, 'sem endereço do WhatsApp no cofre'
    cab = {'X-Api-Key': chave} if chave else {}

    # A SESSÃO SE ACHA PELO ESTADO, nunca pelo nome (regra de ouro). E usamos a
    # caixa EXATA que o servidor devolveu — o caminho HTTP distingue.
    sessao = None
    try:
        r = requests.get('%s/api/sessions' % base, headers=cab, timeout=10)
        for item in (r.json() or []):
            if (item or {}).get('status') != 'WORKING':
                continue
            nome = item.get('name')
            d = requests.get('%s/api/sessions/%s' % (base, nome),
                             headers=cab, timeout=10)
            if d.status_code == 200 and ((d.json() or {}).get('engine') or {}
                                         ).get('state') == 'CONNECTED':
                sessao = nome
                break
    except Exception as exc:
        return 0, 'não consegui falar com o WhatsApp (%s)' % str(exc)[:60]
    if not sessao:
        return 0, 'nenhuma sessão pronta'

    # MODO TESTE MANDA AQUI TAMBÉM (P-27), E FALHA FECHADA (14/08/2026).
    #
    # Três respostas, não duas — as mesmas de `GET /api/config/envio`: 'sim'
    # (ensaio), 'nao' (destino real) e QUALQUER OUTRA COISA = nunca
    # configurado. Nunca configurado NÃO é "pode mandar para o número real":
    # é "o site não sabe", e quem não sabe não entrega direto — deixa a fila
    # para a pipeline, que ainda tem o `DESTINO_ENSAIO` do `.env` como rede.
    #
    # Antes desta guarda, cofre vazio significava envio ao número real do
    # atleta a partir de um botão do painel, furando o ensaio inteiro por
    # ausência de registro. Não existe despublicar (regra 16-C).
    teste = (secretstore.get(secretstore.MODO_TESTE, scope=cidade) or '').strip().lower()
    destino_teste = re.sub(r'\D', '', secretstore.get(
        secretstore.MODO_TESTE_DESTINO, scope=cidade) or '')
    if teste not in ('sim', 'nao'):
        return 0, ('o modo de envio ainda não foi definido no painel — a fila '
                   'sai pelo ciclo da automação, em ensaio')
    if teste == 'sim' and not destino_teste:
        return 0, 'modo teste ligado sem número — não entrego nada'

    entregues = 0
    fila = pendentes(cidade=cidade, limite=limite)
    if apenas_id is not None:
        # UMA linha, escolhida na tela. O admin apertou "Enviar agora" naquela
        # mensagem — mandar as vizinhas junto seria fazer mais do que ele pediu.
        fila = [m for m in fila if m['id'] == apenas_id]
        if not fila:
            return 0, 'essa mensagem não está mais esperando'
    grupo_teste = (secretstore.get(secretstore.MODO_TESTE_GRUPO, scope=cidade) or '').strip()
    for msg in fila:
        # EM ENSAIO, GRUPO VAI PARA O GRUPO DE TESTE (15/08/2026). Mandar uma
        # mensagem de grupo para um número não testa o que interessa: a menção
        # com @ não marca ninguém e o texto se perde. Sem grupo de teste
        # cadastrado, cai no número — o comportamento antigo.
        e_grupo = str(msg['destino']).strip().lower() == 'grupo'
        if teste == 'sim':
            alvo = (grupo_teste if (e_grupo and grupo_teste) else destino_teste)
        else:
            alvo = msg['destino']
        # FALHAR AQUI NÃO GASTA TENTATIVA. Quem conta as 3 tentativas é o ciclo
        # do Dell, que é o caminho oficial; esta entrega é um atalho por cima
        # dele. Se o atalho não der, a mensagem tem de continuar exatamente
        # como estava — pendente, esperando o ciclo. Marcar falha aqui mataria
        # em 3 cliques uma mensagem que a pipeline entregaria sem problema.
        #
        # O que muda é que o motivo fica REGISTRADO na linha (`anotar`), em vez
        # de sumir num `continue` mudo como antes.
        chat = _chat_id(base, cab, sessao, alvo, cidade=cidade)
        if not chat:
            anotar(msg['id'],
                   ('ainda não sei o id do grupo desta cidade — a máquina '
                    'informa isso na próxima rodada dela'
                    if str(alvo).strip().lower() == 'grupo' else
                    'o número %s não respondeu como WhatsApp na entrega '
                    'imediata' % alvo), cidade=cidade)
            logger.warning('Entrega imediata: %s sem chatId para %s',
                           msg['id'], alvo)
            continue
        ok, detalhe = _despachar(base, cab, sessao, chat, msg, cidade)
        if ok:
            marcar(msg['id'], ENVIADA, detalhe, cidade=cidade)
            entregues += 1
        else:
            anotar(msg['id'], detalhe, cidade=cidade)
    return entregues, ('%d entregue(s) na hora' % entregues if entregues
                       else 'nada entregue agora')


def _despachar(base, cab, sessao, chat, msg, cidade):
    """Manda UMA mensagem pelo WAHA. Devolve (ok, detalhe).

    A IMAGEM NUNCA DERRUBA O TEXTO (14/08/2026). A cobrança de medalha leva a
    logo do patrocinador (P-67); se a foto falhar, o aviso ainda tem de sair —
    sem ele a pessoa não sabe que tem medalha para buscar. Perder a arte é
    aceitável; perder o aviso não é.
    """
    import requests
    from flask import url_for
    cabj = dict(cab, **{'Content-Type': 'application/json'})

    if msg.get('tem_imagem'):
        try:
            url = url_for('api.fila_imagem', mensagem_id=msg['id'],
                          t=token_imagem(msg['id']), _external=True)
            resp = requests.post(
                '%s/api/sendImage' % base, headers=cabj,
                json={'session': sessao, 'chatId': chat,
                      'caption': msg['texto'],
                      'file': {'url': url, 'mimetype': 'image/png',
                               'filename': 'patrocinador.png'}},
                timeout=60)
            if resp.status_code in (200, 201):
                return True, 'entregue na hora pelo site, com a logo'
            logger.warning('Entrega imediata: imagem de %s falhou (HTTP %s) — '
                           'mandando só o texto', msg['id'], resp.status_code)
        except Exception as exc:
            logger.warning('Entrega imediata: imagem de %s falhou (%s) — '
                           'mandando só o texto', msg['id'], str(exc)[:120])

    try:
        corpo = {'session': sessao, 'chatId': chat, 'text': msg['texto']}
        # AS MARCACOES (@) VAO JUNTO. O WhatsApp so desenha a marcacao quando o
        # texto tem o "@numero" literal E a mensagem leva a lista de marcados —
        # sem os dois, a pessoa nao recebe notificacao nenhuma.
        marcados = _mentions(base, cab, sessao, msg.get('mencionar'))
        if marcados:
            corpo['mentions'] = marcados
        resp = requests.post('%s/api/sendText' % base, headers=cabj,
                             json=corpo, timeout=30)
        if resp.status_code in (200, 201):
            return True, ('entregue na hora pelo site (sem a logo)'
                          if msg.get('tem_imagem')
                          else 'entregue na hora pelo site')
        return False, 'o WhatsApp respondeu HTTP %s' % resp.status_code
    except Exception as exc:
        logger.warning('Entrega imediata falhou (%s): %s', msg['id'], exc)
        return False, str(exc)[:200]


def _mentions(base, cab, sessao, mencionar):
    """Os JIDs de quem será marcado com @. Lista vazia quando não há ninguém.

    O telefone guardado é canônico (`55DDD9…`), mas o WhatsApp hoje identifica
    por `@lid` — por isso cada um passa pelo `check-exists` (regra 22-C), que é
    a única forma de saber o destino certo. Quem não existir fica de fora: o
    nome dele continua no texto, só não vira marcação.
    """
    if not mencionar:
        return []
    numeros = [n.strip() for n in str(mencionar).split(',') if n.strip()]
    saida = []
    for numero in numeros:
        jid = _chat_id(base, cab, sessao, numero)
        if jid and jid not in saida:
            saida.append(jid)
    return saida


def _chat_id(base, cab, sessao, telefone, cidade=None):
    """O chatId real do número. NUNCA montar `@c.us` na mão (regra 22-C).

    O contato do suplente hoje é `<lid-exemplo>@lid`; pedir por
    `55DDD9XXXXXXXX@c.us` devolve HTTP 500 `No LID for user`.

    "GRUPO" TAMBÉM É UM DESTINO (15/08/2026). Antes esta função devolvia None
    para ele — "grupo se resolve na pipeline" — e o resultado era que **nenhuma
    mensagem de grupo saía pelo site**: o admin apertava "Salvar e enviar", a
    linha entrava na fila e ficava esperando o ciclo da máquina, até 15 minutos.
    Agora o id vem do cofre, contado pela própria máquina.
    """
    import requests
    from app import secretstore

    if str(telefone).strip().lower() in ('grupo', 'group'):
        grupo = (secretstore.get(secretstore.WHATSAPP_GRUPO, scope=cidade) or '').strip()
        # Sem o id não se inventa: mandar para o lugar errado é pior que
        # esperar o ciclo, e o ciclo continua sabendo resolver isto.
        return grupo or None

    numero = re.sub(r'\D', '', str(telefone or ''))
    if not numero:
        return None
    try:
        r = requests.get('%s/api/contacts/check-exists' % base,
                         params={'phone': numero, 'session': sessao},
                         headers=cab, timeout=15)
        if r.status_code == 200:
            dados = r.json() or {}
            if dados.get('numberExists'):
                return dados.get('chatId') or None
    except Exception:
        pass
    return None


def pendentes(cidade=None, limite=40):
    """O que o orquestrador leva agora. Sem o blob (vai por URL separada)."""
    ensure_schema()
    sql = ("SELECT id, cidade, tipo, destino, texto, mencionar, tentativas, "
           "criado_em, (imagem_blob IS NOT NULL) "
           "FROM fila_mensagens WHERE situacao = :s AND tentativas < :max")
    params = {'s': PENDENTE, 'max': MAX_TENTATIVAS, 'l': limite}
    if cidade:
        sql += " AND cidade = :c"
        params['c'] = cidade
    sql += " ORDER BY criado_em ASC LIMIT :l"
    linhas = db.session.execute(sa_text(sql), params).fetchall()
    return [{
        'id': r[0], 'cidade': r[1], 'tipo': r[2], 'destino': r[3],
        'texto': r[4], 'mencionar': r[5], 'tentativas': r[6],
        'criado_em': str(r[7]), 'tem_imagem': bool(r[8]),
    } for r in linhas]


def falhadas(cidade, limite=40):
    """As que desistiram depois do teto de tentativas, com o motivo.

    POR QUE PRECISOU (14/08/2026)
    O alerta do vigia mandava "abra a Central e veja quais sao" — e la nao
    havia nada: a tela so listava PENDENTES. A mensagem morta ficava invisivel,
    sem jeito de reenviar nem de dispensar, e o alerta se repetia para sempre
    sobre algo que ninguem conseguia resolver pela tela. Alerta que manda a
    pessoa a um lugar vazio ensina a ignorar alerta.
    """
    ensure_schema()
    linhas = db.session.execute(sa_text(
        "SELECT id, tipo, destino, texto, tentativas, criado_em, detalhe, "
        "(imagem_blob IS NOT NULL) FROM fila_mensagens "
        "WHERE cidade = :c AND situacao = :s ORDER BY id DESC LIMIT :l"
    ), {'c': cidade, 's': FALHOU, 'l': limite}).fetchall()
    return [{
        'id': r[0], 'tipo': r[1], 'destino': r[2], 'texto': r[3],
        'tentativas': r[4], 'criado_em': str(r[5]), 'detalhe': r[6],
        'tem_imagem': bool(r[7]),
    } for r in linhas]


def retomar(mensagem_id, cidade=None):
    """Zera as tentativas e devolve a mensagem para a fila. True se voltou.

    Serve para o caso em que a causa da falha JA FOI CORRIGIDA — o normal
    depois de um conserto. Sem isto, a unica saida para uma mensagem morta era
    montar tudo de novo do zero.
    """
    ensure_schema()
    sql = ("UPDATE fila_mensagens SET situacao = :p, tentativas = 0, "
           "detalhe = 'recolocada na fila pelo admin' "
           "WHERE id = :i AND situacao = :f")
    params = {'p': PENDENTE, 'i': mensagem_id, 'f': FALHOU}
    if cidade:
        sql += " AND cidade = :c"
        params['c'] = cidade
    res = db.session.execute(sa_text(sql), params)
    db.session.commit()
    return bool(res.rowcount)


def imagem_de(mensagem_id, cidade=None):
    ensure_schema()
    sql = "SELECT imagem_blob, imagem_mime FROM fila_mensagens WHERE id = :i"
    params = {'i': mensagem_id}
    if cidade:
        sql += " AND cidade = :c"
        params['c'] = cidade
    linha = db.session.execute(sa_text(sql), params).fetchone()
    if not linha or not linha[0]:
        return None, None
    return linha[0], (linha[1] or 'image/png')


def marcar(mensagem_id, situacao, detalhe=None, cidade=None):
    """O orquestrador conta o que aconteceu.

    Falha NAO some da fila: incrementa a tentativa e volta para pendente ate
    o teto. Assim uma queda de rede no meio nao engole a mensagem.
    """
    ensure_schema()
    if situacao not in (ENVIADA, FALHOU, CANCELADA):
        return False
    agora = datetime.now()
    if situacao == FALHOU:
        sql = ("UPDATE fila_mensagens SET tentativas = tentativas + 1, "
               "detalhe = :det, situacao = CASE WHEN tentativas + 1 >= :max "
               "THEN :falhou ELSE :pendente END WHERE id = :i")
        params = {'det': detalhe, 'max': MAX_TENTATIVAS, 'falhou': FALHOU,
                  'pendente': PENDENTE, 'i': mensagem_id}
    else:
        sql = ("UPDATE fila_mensagens SET situacao = :sit, enviado_em = :em, "
               "detalhe = :det WHERE id = :i")
        params = {'sit': situacao, 'em': agora, 'det': detalhe, 'i': mensagem_id}
    if cidade:
        sql += " AND cidade = :c"
        params['c'] = cidade
    db.session.execute(sa_text(sql), params)
    db.session.commit()
    return True


def anotar(mensagem_id, detalhe, cidade=None):
    """Registra POR QUE não deu, sem mexer em situação nem em tentativas.

    Existe para o atalho da entrega imediata: ele pode falhar sem que a
    mensagem esteja perdida — o ciclo do Dell ainda vai tentar. O que não pode
    é a falha não deixar rastro: foi um `continue` mudo aqui que fez uma
    cobrança de medalha ficar parada uma noite inteira sem ninguém conseguir
    descobrir o motivo.
    """
    ensure_schema()
    sql = "UPDATE fila_mensagens SET detalhe = :det WHERE id = :i"
    params = {'det': (detalhe or '')[:300], 'i': mensagem_id}
    if cidade:
        sql += " AND cidade = :c"
        params['c'] = cidade
    db.session.execute(sa_text(sql), params)
    db.session.commit()
    return True


def resumo(cidade):
    """Para a tela: quantas esperando, quantas falharam, a mais antiga."""
    ensure_schema()
    linha = db.session.execute(sa_text("""
        SELECT
          SUM(CASE WHEN situacao = :p THEN 1 ELSE 0 END),
          SUM(CASE WHEN situacao = :f THEN 1 ELSE 0 END),
          MIN(CASE WHEN situacao = :p THEN criado_em END)
        FROM fila_mensagens WHERE cidade = :c
    """), {'p': PENDENTE, 'f': FALHOU, 'c': cidade}).fetchone()
    return {
        'pendentes': linha[0] or 0,
        'falhadas': linha[1] or 0,
        'mais_antiga': linha[2],
    }


def contagem_por_dedupe(cidade, prefixo=None):
    """Quantas vezes cada chave JÁ SAIU. {dedupe: nº de mensagens entregues à fila}

    Só conta o que chegou a `enviada`: o que falhou ou está esperando não é
    "vez que a pessoa recebeu". Cada reenvio forçado cria uma linha nova com o
    mesmo dedupe, então contar linhas é contar envios.

    POR QUE (13/08/2026, pedido do dono): com duas telas mandando a mesma
    cobrança, era fácil avisar a mesma pessoa duas vezes sem perceber. Um
    número ao lado do nome responde "quantas ela já levou?" sem ninguém
    precisar decorar.
    """
    ensure_schema()
    sql = ("SELECT dedupe, COUNT(*) FROM fila_mensagens "
           "WHERE cidade = :c AND dedupe IS NOT NULL AND situacao = 'enviada'")
    params = {'c': cidade}
    if prefixo:
        sql += " AND dedupe LIKE :p"
        params['p'] = prefixo + '%'
    sql += " GROUP BY dedupe"
    return {r[0]: int(r[1] or 0)
            for r in db.session.execute(sa_text(sql), params).fetchall()}


def situacao_por_dedupe(cidade, prefixo=None):
    """O que ja foi despachado, indexado pela chave de dedupe.

    A tela da Central precisa disto para responder a pergunta mais basica de
    todas: "eu ja mandei isso?". Sem ela o botao "enviar agora" ficava aceso
    para sempre e ninguem sabia se a mensagem tinha saido, estava na fila ou
    tinha falhado.

    Devolve {dedupe: {'situacao', 'quando', 'tentativas', 'id', 'detalhe'}},
    ficando com o registro MAIS RECENTE de cada chave.
    """
    ensure_schema()
    sql = ("SELECT dedupe, situacao, criado_em, enviado_em, tentativas, id, detalhe "
           "FROM fila_mensagens WHERE cidade = :c AND dedupe IS NOT NULL")
    params = {'c': cidade}
    if prefixo:
        sql += " AND dedupe LIKE :p"
        params['p'] = prefixo + '%'
    sql += " ORDER BY id ASC"
    saida = {}
    for r in db.session.execute(sa_text(sql), params).fetchall():
        saida[r[0]] = {
            'situacao': r[1],
            'quando': r[3] or r[2],
            'tentativas': r[4] or 0,
            'id': r[5],
            'detalhe': r[6],
        }
    return saida


def aliviar_imagens_entregues(dias=7):
    """Apaga a IMAGEM de mensagens ja entregues, guardando a linha.

    MEDIDO EM 26/08/2026: `fila_mensagens.imagem_blob` somava 28,28 MB em 53
    linhas -- METADE do banco inteiro (55 MB). A imagem de uma mensagem que ja
    chegou nao serve mais para nada; o registro de que ela foi enviada, para
    quem e quando, serve. Entao some a imagem, nao a linha.

    So mexe em `enviada`: `cancelada` e `falhou` ainda podem ser reenviadas
    pelo botao "Enviar de novo" (`retomar`), e reenviar sem a imagem mandaria
    o texto pelado -- exatamente o que a regra 11-A proibe.

    O arquivo do banco NAO encolhe sozinho: o espaco liberado volta para
    reuso do SQLite. Encolher exigiria VACUUM, que tranca o banco inteiro e
    num NAS lento e' remedio pior que a doenca (regra 24).
    """
    ensure_schema()
    corte = datetime.now() - timedelta(days=dias)
    res = db.session.execute(sa_text(
        "UPDATE fila_mensagens SET imagem_blob = NULL "
        "WHERE situacao = :e AND enviado_em < :corte AND imagem_blob IS NOT NULL"
    ), {'e': ENVIADA, 'corte': corte})
    db.session.commit()
    if res.rowcount:
        logger.info('Fila de mensagens: imagem liberada de %s mensagem(ns) ja entregue(s)',
                    res.rowcount)
    return res.rowcount


def limpar_antigas(dias=60):
    """Some com o que ja foi entregue ha muito tempo (o blob pesa)."""
    ensure_schema()
    corte = datetime.now() - timedelta(days=dias)
    res = db.session.execute(sa_text(
        "DELETE FROM fila_mensagens WHERE situacao IN (:e, :c) AND enviado_em < :corte"
    ), {'e': ENVIADA, 'c': CANCELADA, 'corte': corte})
    db.session.commit()
    if res.rowcount:
        logger.info('Fila de mensagens: %s item(ns) antigo(s) removido(s)', res.rowcount)
    return res.rowcount
