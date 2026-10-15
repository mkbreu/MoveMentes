# -*- coding: utf-8 -*-
"""Conferência automática — a rede que pega o link que a coleta perdeu.

POR QUE ISTO EXISTE (27/08/2026 — `ERROS.md` §4.30)
Em 26/08 o ranking das 15:30 saiu **só com as logos**. Um link postado às 06:25
não foi contado: o WAHA principal só entregou mensagens até 05:34, e o suplente
— que TINHA o link — não foi consultado.

A causa, MEDIDA em 27/08 e corrigida em relação ao que este cabeçalho dizia antes:
ao abrir, o WhatsApp Web sincroniza em lote o que chegou enquanto esteve fora, e
esse lote **às vezes entra e às vezes não**. Não é "nunca entra": a mensagem das
08:50 de 27/08 chegou com o container parado e apareceu na leitura depois que ele
subiu; já os 4 recados de 26/08 de manhã sumiram no principal e sobreviveram no
suplente. Como os containers ficam parados ~21h por dia, toda subida é uma aposta
sobre o intervalo inteiro — e aposta não é coleta. (`ERROS.md` §4.31.)

ENTÃO A SAÍDA NÃO É CONSERTAR A LEITURA — É NÃO DEPENDER SÓ DELA. Este módulo
compara o Strava de cada atleta participante com o que está no ranking e traz o
que faltou.

A TENSÃO COM A REGRA 4-A, RESOLVIDA (decisão do dono, 26/08)
A regra 4-A diz que o que vale é o LINK POSTADO no grupo, e ela continua
valendo. Isto aqui é **exceção de contingência**: enquanto a coleta perder link,
é pior deixar o atleta com km errado do que contar uma atividade que ele fez e
não postou. Duas consequências disso, que o código cumpre:

  * quem NÃO POSTOU NADA no mês não é conferido — não é participante daquele mês;
  * quem pediu para sair tem como sair, e a dispensa vale só naquele mês
    (`dispensar`). **Quem não quer participar, não participa** — isso é regra,
    não conveniência.

ONDE RODA: no site (DS215j), que fica de pé das 05:13 às 23:05 e já tem o cookie
do Strava e a fila de acesso. **Não depende do Dell nem do WAHA** — é
exatamente isso que a torna uma rede de segurança de verdade.
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy import text as sa_text

from app import db, secretstore

logger = logging.getLogger(__name__)

# TETO DE ATLETAS: NÃO EXISTE MAIS (regra do dono, 20/09/2026).
# Ele existia para uma cidade grande não segurar a fila do Strava por horas.
# O dono decidiu o contrário, e com razão: parar no atleta 61 é decidir que o
# km de alguém não vale, e ninguém fica sabendo. Demora o que demorar — a
# conferência roda no site, que não é o caminho da publicação.
MAX_ATLETAS_POR_RODADA = 0          # 0 = sem teto

# ===================== O FECHAMENTO DO MÊS (P-136) =====================
#
# Dias fixos, decididos pelo dono em 20/09/2026: **10, 20 e o último do mês**
# (28, 29, 30 ou 31).
#
# NOS DIAS 10 E 20 A CONFERÊNCIA SEGURA A MÃO, DE PROPÓSITO: lança no máximo
# 3 atividades por atleta, e só se **todas** as que faltam forem posteriores ao
# marco anterior. Se uma delas é mais velha que isso, não lança nenhuma daquele
# atleta — ele está deixando de postar, e o sistema lançando por ele ensina
# exatamente o contrário do que o grupo precisa (a regra 4-A diz que o que vale
# é o link postado). O que não entra vira aviso, com nome e quantidade.
#
# NO ÚLTIMO DIA LANÇA TUDO. Aí o mês vai fechar e a conta tem de estar certa:
# ninguém discute ranking no dia seguinte por causa de um link esquecido.
MARCOS_DO_MES = (10, 20)
MAX_LANCAMENTOS_NOS_MARCOS = 3
# 22h30: é a última hora em que o Synology está acordado (ele desliga 23h05).
# NÃO é recorte de atividade — o mês continua sendo o mês inteiro.
HORA_DO_ULTIMO_DIA = (22, 30)
JANELA_ULTIMO_DIA_MIN = 25

# SOBRE O TAMANHO DAS ATIVIDADES — DECISÃO DO DONO, 27/08/2026.
#
# Medido antes de ligar: das 16 atividades apontadas como "faltando" em três
# atletas, 13 tinham menos de 0,3 km — 0,00, 0,01, 0,02 km. São fragmentos que o
# Strava guarda quando o aplicativo liga e desliga. Uma atleta parecia ter
# "parado de postar" com 14 faltantes; as 14 eram fragmentos.
#
# Cheguei a propor um piso de 1 km. **O dono decidiu manter só o filtro que já
# existe** (`distancia <= 0`, em `_process_single_strava_url`): o que passar por
# ele entra, e quem não quiser participar é retirado pela dispensa. Fica
# registrado aqui porque é a explicação de por que um atleta pode ganhar muitas
# atividades curtas de uma vez — não é defeito, é como o Strava dele grava.

_SCHEMA_PRONTO = False


def ensure_schema():
    """Cria a tabela de dispensa. Idempotente, como manda a convenção §3."""
    global _SCHEMA_PRONTO
    if _SCHEMA_PRONTO:
        return
    try:
        db.session.execute(sa_text(
            """
            CREATE TABLE IF NOT EXISTS conferencia_dispensa (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade      VARCHAR(100) NOT NULL,
                strava_id   VARCHAR(50)  NOT NULL,
                ano         INTEGER      NOT NULL,
                mes         INTEGER      NOT NULL,
                marcado_por VARCHAR(120),
                marcado_em  DATETIME,
                UNIQUE (cidade, strava_id, ano, mes)
            )
            """
        ))
        db.session.commit()
        _SCHEMA_PRONTO = True
    except Exception as exc:
        db.session.rollback()
        logger.warning('conferencia_dispensa: nao consegui garantir a tabela: %s', exc)


# ---------------------------------------------------------------- dispensa

def dispensado(cidade, strava_id, ano, mes):
    """Este atleta pediu para ficar de fora NESTE mês?"""
    ensure_schema()
    try:
        achou = db.session.execute(sa_text(
            'SELECT 1 FROM conferencia_dispensa '
            'WHERE cidade = :c AND strava_id = :s AND ano = :a AND mes = :m'
        ), {'c': cidade, 's': str(strava_id), 'a': int(ano), 'm': int(mes)}).first()
        return achou is not None
    except Exception as exc:
        db.session.rollback()
        logger.warning('conferencia: falha ao ler dispensa de %s: %s', strava_id, exc)
        return False        # na duvida, confere (perder km e pior que conferir demais)


def dispensar(cidade, strava_id, ano, mes, por='', dispensar_=True):
    """Marca (ou desmarca) o atleta como fora da conferência DESTE mês."""
    ensure_schema()
    try:
        if dispensar_:
            db.session.execute(sa_text(
                'INSERT OR IGNORE INTO conferencia_dispensa '
                '(cidade, strava_id, ano, mes, marcado_por, marcado_em) '
                'VALUES (:c, :s, :a, :m, :p, :q)'
            ), {'c': cidade, 's': str(strava_id), 'a': int(ano), 'm': int(mes),
                'p': por or '', 'q': datetime.now()})
        else:
            db.session.execute(sa_text(
                'DELETE FROM conferencia_dispensa '
                'WHERE cidade = :c AND strava_id = :s AND ano = :a AND mes = :m'
            ), {'c': cidade, 's': str(strava_id), 'a': int(ano), 'm': int(mes)})
        db.session.commit()
        return True
    except Exception as exc:
        db.session.rollback()
        logger.error('conferencia: falha ao gravar dispensa de %s: %s', strava_id, exc)
        return False


# ---------------------------------------------------------------- candidatos

def _limites_do_mes(ano, mes):
    inicio = datetime(ano, mes, 1)
    fim = datetime(ano + 1, 1, 1) if mes == 12 else datetime(ano, mes + 1, 1)
    return inicio, fim


def atletas_candidatos(cidade, ano, mes):
    """Quem entra na conferência deste mês: [(strava_id, nome)].

    O CRITÉRIO É PARTICIPAÇÃO, NÃO CADASTRO: entra quem já tem atividade
    registrada no mês. Quem não postou nada não é conferido — a regra 4-A diz
    que participar é postar link, e trazer km de quem não postou nada seria
    inscrever alguém que não se inscreveu.
    """
    from app.models import Activity
    inicio, fim = _limites_do_mes(ano, mes)
    linhas = (
        db.session.query(Activity.id_usuario_strava, Activity.nome_usuario)
        .filter(Activity.cidade == cidade,
                Activity.data_hora >= inicio,
                Activity.data_hora < fim,
                Activity.status != 'removida')
        .distinct()
        .all()
    )
    vistos, saida = set(), []
    for sid, nome in linhas:
        sid = str(sid or '').strip()
        # `name:<slug>` é atleta sem id do Strava — não dá para consultar o
        # perfil dele, então não há o que conferir.
        if not sid or not sid.isdigit() or sid in vistos:
            continue
        vistos.add(sid)
        saida.append((sid, nome or 'Atleta'))
    return saida


# Folga sobre o marco. A atividade e' lida do grupo minutos depois de
# postada, e o ZIP leva um tempo para ser processado: sem a folga, link
# postado 3 minutos antes da leitura seria acusado de perdido.
FOLGA_APOS_LEITURA_MIN = 30


def ate_quando_o_grupo_foi_lido(cidade):
    """Data/hora da ultima leitura do grupo que o site processou.

    E' O MARCO QUE FALTAVA (29/08/2026). A conferencia compara o Strava com
    o BANCO, e nunca olha o WhatsApp. Sem saber ate quando o grupo ja foi
    lido, ela nao tem como distinguir:

        atividade que o WhatsApp PERDEU      -> e' falha, avisa
        atividade que AINDA NAO FOI LIDA     -> e' normal, cala

    Em 28/08 ela confundiu as duas e mandou e-mail de falha as 22:41 sobre
    links postados as 19h, que so entrariam no ciclo das 05:30.

    None = nunca houve upload da automacao; ai nao da para afirmar nada, e
    a conferencia volta a conferir tudo (comportamento antigo).
    """
    from app.models import ProcessedFile
    try:
        linha = (ProcessedFile.query
                 .filter(ProcessedFile.cidade == cidade,
                         ProcessedFile.arquivo_nome.like('automacao_%'))
                 .order_by(ProcessedFile.processado_em.desc())
                 .first())
        return getattr(linha, 'processado_em', None)
    except Exception as exc:
        logger.debug('conferencia: sem marco de leitura do grupo: %s', exc)
        return None


def _ids_no_ranking(cidade, strava_id, ano, mes):
    from app.models import Activity
    inicio, fim = _limites_do_mes(ano, mes)
    linhas = (
        db.session.query(Activity.id_atividade)
        .filter(Activity.cidade == cidade,
                Activity.id_usuario_strava == str(strava_id),
                Activity.data_hora >= inicio,
                Activity.data_hora < fim,
                Activity.status != 'removida')
        .all()
    )
    return {str(x[0]) for x in linhas}


# ---------------------------------------------------------------- conferência

def conferir_atleta(cidade, strava_id, nome, ano, mes, cookie, throttle,
                    limite=None, so_depois_de=None):
    """Confere UM atleta. Devolve o que achou e o que gravou.

    `limite` e `so_depois_de` sao a regra dos dias 10 e 20 (P-136): no maximo
    `limite` lancamentos, e nenhum se alguma das faltantes for anterior a
    `so_depois_de`. No ultimo dia do mes os dois vem vazios — lanca tudo.

    NÃO EXISTE LIMITE DE QUANTOS LINKS ENTRAM AQUI (pedido do dono, 26/08).
    A tela pública de conferência limita de propósito: sem limite, o atleta se
    vicia em lançar por lá e para de postar no grupo, o que fere a 4-A. Aqui é o
    contrário — é contingência, o sistema é que falhou, e deixar link de fora
    seria repetir justamente o erro que este módulo existe para consertar.
    """
    from app.blueprints.processing import fetch_athlete_month_summary
    from app.blueprints.upload import _process_single_strava_url

    resultado = {'strava_id': strava_id, 'nome': nome, 'faltavam': 0,
                 'gravadas': 0, 'erros': 0, 'linhas': [], 'motivo': '',
                 # Das gravadas, quantas o grupo JA deveria ter trazido.
                 # So estas sao falha do WhatsApp.
                 'perdidas_de_verdade': 0}

    try:
        # A MESMA REQUISICAO, agora com a data de cada atividade: e' dela que
        # sai a regra dos dias 10 e 20 (P-136).
        from app.blueprints.processing import fetch_athlete_month_entries
        entradas = fetch_athlete_month_entries(
            strava_id, ano, mes, cookie, throttle=throttle)
        ids_strava = [e['id_atividade'] for e in entradas]
        quando_de = {str(e['id_atividade']): e.get('data_hora') for e in entradas}
    except Exception as exc:
        resultado['motivo'] = 'nao consegui ler o Strava: %s' % exc
        logger.warning('conferencia: perfil %s nao pode ser lido: %s', strava_id, exc)
        return resultado

    # ATE QUANDO O GRUPO JA FOI LIDO. Ver a funcao para o incidente de
    # 28/08 que motivou isto (e-mail de falha as 22:41 sobre links das 19h).
    marco = ate_quando_o_grupo_foi_lido(cidade)
    if marco is not None:
        marco = marco - timedelta(minutes=FOLGA_APOS_LEITURA_MIN)
    ja_temos = _ids_no_ranking(cidade, strava_id, ano, mes)
    faltantes = [str(i) for i in ids_strava if str(i) not in ja_temos]
    resultado['faltavam'] = len(faltantes)
    if not faltantes:
        return resultado

    # ===== A MAO SEGURA DOS DIAS 10 E 20 (P-136) =====
    resultado['nao_lancadas'] = 0
    if so_depois_de is not None:
        velhas = [i for i in faltantes
                  if quando_de.get(i) is None or quando_de[i] < so_depois_de]
        if velhas:
            # UMA VELHA CONTAMINA A PASSADA INTEIRA DAQUELE ATLETA, por
            # decisao do dono: quem esqueceu link de antes do marco precisa
            # postar, nao ser socorrido.
            resultado['nao_lancadas'] = len(faltantes)
            resultado['motivo'] = (
                'faltam %d link(s), e %d é(são) anterior(es) ao dia %d — '
                'nada foi lançado; avise para postar'
                % (len(faltantes), len(velhas), so_depois_de.day))
            return resultado
    if limite is not None and len(faltantes) > limite:
        resultado['nao_lancadas'] = len(faltantes) - limite
        resultado['motivo'] = (
            'faltam %d link(s): lancei os %d mais recentes e o resto fica para '
            'ele postar' % (len(faltantes), limite))
        # Os mais RECENTES primeiro: sao os que o grupo ainda vai comentar.
        faltantes = sorted(
            faltantes, key=lambda i: quando_de.get(i) or datetime.min,
            reverse=True)[:limite]

    for aid in faltantes:
        url = 'https://www.strava.com/activities/%s' % aid
        try:
            # A GUARDA DO ATLETA — QUE ESTE COMENTÁRIO AFIRMAVA E NÃO EXISTIA.
            #
            # Até 27/08/2026 aqui estava escrito que `_process_single_strava_url`
            # "já confere que a atividade é DESTE atleta". **Ela não conferia.**
            # O feed do perfil traz treino em grupo (regra 4-D), e a atividade
            # era gravada sob o DONO REAL dela — então conferir a atleta X
            # trazia para o ranking gente que nem está no grupo.
            #
            # Achado ao vivo pelo dono: uma atleta, que saiu do grupo há muito
            # tempo, apareceu no ranking de agosto com 8,2 km. Ela veio no feed
            # de outra pessoa que estava sendo conferida.
            #
            # Comentário que promete guarda inexistente é pior que comentário
            # nenhum: ele faz a próxima leitura do código parar de procurar.
            item = _process_single_strava_url(
                url, cidade=cidade, admin_id=None,
                only_current_month=False, periodos={(ano, mes)},
                atleta_esperado=strava_id,
                # Assina como maquina, nao como admin (27/08/2026).
                origem='conferencia_automatica',
                # E PASSA PELA FILA DE ACESSO (27/08/2026). Este laco roda um
                # link atras do outro; sem o throttle era a regra 4-C furada
                # por dentro -- o ban do Strava nao pergunta por qual funcao a
                # requisicao passou. O `throttle` ja e' montado acima para a
                # leitura do perfil; e' o mesmo, de proposito: uma fila so.
                throttle=throttle,
            )
        except Exception as exc:
            resultado['erros'] += 1
            logger.warning('conferencia: %s nao pode ser lida: %s', url, exc)
            continue

        # O CONTRATO É `status`, NÃO uma chave booleana (conferido no código em
        # 27/08): os valores possíveis são 'salvo', 'duplicado', 'ignorado' e
        # 'erro'. Ler isso errado contaria zero para sempre, em silêncio.
        situacao = (item or {}).get('status')
        if situacao == 'salvo':
            resultado['gravadas'] += 1
            # ANTERIOR AO MARCO = O GRUPO JA FOI LIDO E ELA NAO VEIO.
            # Posterior = normal; estamos so adiantando o proximo ciclo.
            quando = (item or {}).get('data_hora')
            if marco is None or (quando and quando < marco):
                resultado['perdidas_de_verdade'] += 1
            # O km vem dentro da mensagem ("Fulano | 7.4km | ..."), não num
            # campo próprio. Somar por aí seria frágil; o total do mês é
            # recalculado pela consolidação de qualquer forma.
            resultado['linhas'].append(item.get('message') or url)
        elif situacao == 'erro':
            resultado['erros'] += 1
        elif situacao == 'ignorado':
            # IGNORADO NÃO É UMA COISA SÓ (29/08/2026, incidente dos 266 km).
            #
            # Até aqui um comentário dizia que 'ignorado' é "a guarda do
            # atleta fazendo o trabalho dela", e o código não contava nem
            # logava nada. São dois casos bem diferentes:
            #
            #   dono VÁLIDO e diferente  -> treino em grupo. Descarte certo
            #                               (regra 4-D), silêncio tolerado.
            #   dono INVÁLIDO ('0')      -> a LEITURA falhou. A atividade é
            #                               do atleta, e está sendo jogada
            #                               fora achando que é de terceiro.
            #
            # Foi assim que 16,471 km reais do Maykon sumiram em 28/08 sem
            # deixar uma linha de log. Km que some calado não volta nunca
            # (regra 4-I): o segundo caso agora é registrado e contado.
            motivo = (item or {}).get('motivo') or 'sem motivo'
            dono = str((item or {}).get('dono') or '').strip()
            leitura_falhou = motivo == 'outro_atleta' and (
                not dono or not dono.isdigit() or dono == '0')
            resultado.setdefault('ignoradas', {})
            chave = 'leitura_sem_dono' if leitura_falhou else motivo
            resultado['ignoradas'][chave] = \
                resultado['ignoradas'].get(chave, 0) + 1
            if leitura_falhou:
                resultado['erros'] += 1
                logger.warning(
                    'conferência: %s foi descartada como "de outro atleta", '
                    'mas a leitura não achou dono nenhum — é defeito de '
                    'leitura, não treino em grupo. O km pode ser do atleta '
                    'conferido.', url)
        # 'duplicado' segue sem barulho: a atividade já está no ranking.
    return resultado


DIAS_PADRAO = 4
DIAS_VALIDOS = (0, 1, 2, 3, 4)
# Marca de "confere na proxima rodada, sem esperar o prazo". Some ao ser
# atendida — e um pedido pontual, nao uma mudanca de frequencia.
CONFERENCIA_FORCAR = 'conferencia_forcar'


def frequencia(cidade):
    """De quantos em quantos dias esta cidade confere. 0 = desligada."""
    bruto = (secretstore.get(secretstore.CONFERENCIA_DIAS, scope=cidade) or '').strip()
    try:
        n = int(bruto)
    except (TypeError, ValueError):
        return DIAS_PADRAO
    return n if n in DIAS_VALIDOS else DIAS_PADRAO


def definir_frequencia(cidade, dias, por=''):
    dias = int(dias)
    if dias not in DIAS_VALIDOS:
        return False
    secretstore.set_value(secretstore.CONFERENCIA_DIAS, str(dias),
                          scope=cidade, updated_by=por or 'painel')
    return True


def forcar_proxima(cidade, por='', ligar=True):
    """Pede a conferência na PRÓXIMA rodada, sem esperar o prazo.

    POR QUE ISTO EXISTE (pedido do dono, 27/08/2026)
    O prazo de 4 dias serve para o dia a dia. Mas quando se descobre que a
    coleta falhou — o ranking saiu só com a logo, ou dá para ver no grupo que
    faltou link — esperar quatro dias é esperar demais: o atleta fica com km
    errado esse tempo todo. Esta marca faz a próxima rodada conferir e acabou.

    É UM PEDIDO, NÃO UMA MUDANÇA DE FREQUÊNCIA: some sozinha depois de
    atendida, e o ritmo escolhido continua valendo. Assim ninguém liga a
    conferência diária "só desta vez" e esquece ligada.
    """
    if ligar:
        return secretstore.set_value(CONFERENCIA_FORCAR, '1', scope=cidade,
                                     updated_by=por or 'painel')
    # APAGAR E' `delete`, NAO `set_value('')` (29/08/2026).
    #
    # `secretstore.set_value` recusa valor vazio de proposito (`if not
    # value: return False`) -- e' a guarda que impede um segredo de ser
    # zerado por engano. So que este codigo usava exatamente isso para
    # DESLIGAR a marca, e ela nunca saia: uma vez clicado "conferir na
    # proxima rodada", `_forcada` devolvia True para sempre e a conferencia
    # atravessava o prazo em toda chamada. Depois que ela foi para o vigia
    # (15 em 15 min), isso virou quatro varreduras por hora.
    #
    # "Some sozinha depois de atendida", diz a docstring acima. Agora some.
    secretstore.delete(CONFERENCIA_FORCAR, scope=cidade)
    return True


def _forcada(cidade):
    return (secretstore.get(CONFERENCIA_FORCAR, scope=cidade) or '').strip() == '1'


# DEPOIS DA PUBLICACAO, NAO ANTES (29/08/2026 — corrigido no mesmo dia).
#
# A primeira versao rodava 25 min ANTES, para o ranking ja sair completo.
# Errado: a leitura do grupo daquele ciclo chega JUNTO com a publicacao (o
# ZIP de 28/08 entrou as 15:30:37 e o ranking saiu as 15:30). Conferir
# antes disso e' olhar o banco justamente quando ele esta mais atrasado --
# a conferencia veria como "faltando" os links que estao a caminho.
#
# Depois da publicacao o banco esta no seu momento mais completo, e o que
# faltar e' perda de verdade. O que a conferencia trouxer entra no proximo
# ranking (05:30 ou 15:30), que e' o desenho que o dono descreveu.
DEPOIS_DA_PUBLICACAO_MIN = 40
# Largura da janela: o vigia passa de 15 em 15 min, entao ela precisa ser
# maior que isso, ou um dia a passada cai fora e a conferencia nao roda.
JANELA_MIN = 20


def minutos_ate_publicar(cidade, agora=None):
    """Quantos minutos faltam para a proxima publicacao desta cidade.

    None quando a cidade nao tem agenda -- e ai a conferencia volta a se
    guiar so pelo prazo em dias, como antes.
    """
    try:
        from app.publicador import agenda
        agora = agora or datetime.now()
        atual = agora.hour * 60 + agora.minute
        faltas = []
        for linha in agenda(cidade) or ():
            if not getattr(linha, 'ativo', True):
                continue
            try:
                h, m = str(linha.horario).split(':')
                alvo = int(h) * 60 + int(m)
            except (ValueError, AttributeError):
                continue
            falta = alvo - atual
            if falta < 0:
                falta += 24 * 60      # so amanha
            faltas.append(falta)
        return min(faltas) if faltas else None
    except Exception as exc:
        logger.debug('Sem agenda de publicacao para %s: %s', cidade, exc)
        return None


def minutos_desde_publicar(cidade, agora=None):
    """Quantos minutos se passaram desde a ULTIMA publicacao desta cidade."""
    falta = minutos_ate_publicar(cidade, agora)
    if falta is None:
        return None
    # O horario mais proximo para tras e' o mesmo ciclo visto do outro lado.
    try:
        from app.publicador import agenda
        agora = agora or datetime.now()
        atual = agora.hour * 60 + agora.minute
        passados = []
        for linha in agenda(cidade) or ():
            if not getattr(linha, 'ativo', True):
                continue
            try:
                h, m = str(linha.horario).split(':')
                alvo = int(h) * 60 + int(m)
            except (ValueError, AttributeError):
                continue
            desde = atual - alvo
            if desde < 0:
                desde += 24 * 60
            passados.append(desde)
        return min(passados) if passados else None
    except Exception as exc:
        logger.debug('Sem agenda de publicacao para %s: %s', cidade, exc)
        return None


def na_hora_certa(cidade, agora=None):
    """Estamos na janela de conferir esta cidade?

    A janela e' logo DEPOIS da publicacao — quando o banco ja recebeu a
    leitura do grupo daquele ciclo e o que faltar e' perda de verdade.
    Cidade sem agenda cadastrada passa direto: melhor conferir em hora
    imprevisivel do que nunca.
    """
    agora = agora or datetime.now()

    # ÚLTIMO DIA: 22h30, porque é a última hora em que o Synology está de pé
    # (P-136). Não depende de agenda de publicação nenhuma.
    if marco_de_hoje(agora) == 'ultimo':
        alvo = agora.hour * 60 + agora.minute
        marcado = HORA_DO_ULTIMO_DIA[0] * 60 + HORA_DO_ULTIMO_DIA[1]
        return marcado <= alvo <= marcado + JANELA_ULTIMO_DIA_MIN

    # DIAS 10 E 20: no ÚLTIMO ranking do dia (regra do dono, 20/09). Conferir
    # depois da rodada da manhã acusaria como "faltando" o link de quem acabou
    # de treinar e ainda nem postou — aviso falso, que ensina a ignorar aviso.
    desde = minutos_desde_ultima_publicacao_do_dia(cidade, agora)
    if desde is None:
        return True
    return DEPOIS_DA_PUBLICACAO_MIN <= desde <= (DEPOIS_DA_PUBLICACAO_MIN
                                                 + JANELA_MIN)


def minutos_desde_ultima_publicacao_do_dia(cidade, agora=None):
    """Minutos desde a ULTIMA publicacao DE HOJE (nao a mais proxima).

    `minutos_desde_publicar` responde "a publicacao mais recente", que de
    manha e' a de ontem a tarde. Aqui interessa outra coisa: ja passou o
    ultimo ranking do dia? So depois dele o banco esta no seu momento mais
    completo.
    """
    try:
        from app.publicador import agenda
        agora = agora or datetime.now()
        atual = agora.hour * 60 + agora.minute
        horarios = []
        for linha in agenda(cidade) or ():
            if not getattr(linha, 'ativo', True):
                continue
            try:
                h, m = str(linha.horario).split(':')
                horarios.append(int(h) * 60 + int(m))
            except (ValueError, AttributeError):
                continue
        if not horarios:
            return None
        ultimo = max(horarios)
        if atual < ultimo:
            return None      # o ranking do dia ainda nem saiu
        return atual - ultimo
    except Exception as exc:
        logger.debug('Sem agenda de publicacao para %s: %s', cidade, exc)
        return None


def ultimo_dia_do_mes(quando):
    """O dia 28, 29, 30 ou 31 daquele mês — o único que fecha a conta."""
    import calendar
    return calendar.monthrange(quando.year, quando.month)[1]


def marco_de_hoje(hoje=None):
    """Que marco é hoje: 10, 20, 'ultimo' — ou None, se hoje não é dia de conferir.

    ATRASO CONTA COMO O MARCO (a lição da P-78-b, que o desenho por calendário
    esquecia): se o site passou o dia 10 fora do ar, o dia 11 ainda roda o
    marco do dia 10. O que não pode é o marco simplesmente não acontecer.
    """
    hoje = hoje or datetime.now()
    ultimo = ultimo_dia_do_mes(hoje)
    if hoje.day >= ultimo:
        return 'ultimo'
    for marco in sorted(MARCOS_DO_MES, reverse=True):
        if hoje.day >= marco:
            # Só o marco MAIS RECENTE vale — no dia 21 o marco é o 20, e ele
            # já foi feito no dia 20 (quem impede a repetição é `vencida`).
            return marco
    return None


def marco_anterior(marco, hoje=None):
    """A partir de quando as atividades contam, naquele marco.

    Dia 10 → dia 1º (o mês inteiro até aqui). Dia 20 → dia 10. Último dia →
    None, que é "tudo".
    """
    hoje = hoje or datetime.now()
    if marco == 'ultimo':
        return None
    if marco == 20:
        return datetime(hoje.year, hoje.month, 10)
    return datetime(hoje.year, hoje.month, 1)


def vencida(cidade, hoje=None):
    """Está na hora de conferir esta cidade?

    O PRAZO TEM MEMÓRIA, de propósito. Disparar por calendário ("todo dia par")
    faz a checagem sumir quando a máquina não liga naquele dia — foi a lição da
    P-78-b, e ela vale igual aqui. Quem manda é **quantos dias se passaram desde
    a última vez**, não que dia é hoje.
    """
    # O PEDIDO EXPLICITO GANHA DE TUDO, ate de "desligada": quem clicou quer
    # conferir agora, e a alternativa seria um botao que nao faz nada.
    if _forcada(cidade):
        return True
    if frequencia(cidade) <= 0:      # o 0 continua sendo "desligada"
        return False
    hoje = hoje or datetime.now()
    marco = marco_de_hoje(hoje)
    if marco is None:
        return False
    # UMA VEZ POR MARCO, e nao uma vez por dia: a marca guarda a data em que
    # rodou; se ela e' de hoje, ja foi. Se e' de antes do marco (ou nao
    # existe), o marco ainda nao aconteceu e roda agora — inclusive atrasado.
    ultima = (secretstore.get(secretstore.CONFERENCIA_ULTIMA, scope=cidade) or '').strip()
    if not ultima:
        return True
    try:
        quando = datetime.strptime(ultima, '%Y-%m-%d').date()
    except ValueError:
        return True          # marca ilegível não pode virar "nunca mais rodar"
    dia_do_marco = (ultimo_dia_do_mes(hoje) if marco == 'ultimo' else marco)
    inicio_do_marco = hoje.date().replace(day=dia_do_marco)
    return quando < inicio_do_marco


def marcar_rodada(cidade, hoje=None):
    hoje = hoje or datetime.now()
    secretstore.set_value(secretstore.CONFERENCIA_ULTIMA, hoje.strftime('%Y-%m-%d'),
                          scope=cidade, updated_by='conferencia')
    # CONSOME O PEDIDO: atendido, ele some. Se ficasse, a conferencia passaria a
    # rodar todo dia sem ninguem ter escolhido isso.
    if _forcada(cidade):
        forcar_proxima(cidade, por='conferencia', ligar=False)


def rodar(cidade, ano=None, mes=None, progresso=None, marco=None):
    """Confere a cidade inteira. Devolve o resumo para tela, log e e-mail.

    `progresso(mensagem, pct)` é opcional — quem chamou decide se mostra.
    """
    from app.blueprints.processing import build_strava_throttle

    agora = datetime.now()
    ano = ano or agora.year
    mes = mes or agora.month

    def _av(msg, pct):
        if progresso:
            try:
                progresso(msg, pct)
            except Exception:
                pass

    resumo = {'cidade': cidade, 'ano': ano, 'mes': mes, 'quando': agora,
              'atletas': 0, 'dispensados': 0, 'gravadas': 0,
              'erros': 0, 'detalhes': [], 'motivo': ''}

    cookie = secretstore.get(secretstore.STRAVA_SESSION, scope=cidade)
    if not cookie:
        # SEM COOKIE NÃO SE INVENTA RESULTADO: dizer "nada faltando" aqui seria
        # a mentira mais cara do sistema (regra 4-G, a mesma ideia).
        resumo['motivo'] = ('A sessão do Strava não está configurada nesta cidade — '
                            'sem ela não dá para ler o perfil de ninguém.')
        logger.warning('conferencia (%s): sem cookie do Strava, nada a fazer', cidade)
        return resumo

    candidatos = atletas_candidatos(cidade, ano, mes)
    if MAX_ATLETAS_POR_RODADA and len(candidatos) > MAX_ATLETAS_POR_RODADA:
        logger.warning('conferencia (%s): %d atletas, conferindo os %d primeiros',
                       cidade, len(candidatos), MAX_ATLETAS_POR_RODADA)
        candidatos = candidatos[:MAX_ATLETAS_POR_RODADA]

    # A REGRA DO MARCO (P-136). Sem marco (pedido manual, "conferir agora"),
    # vale o que sempre valeu: lanca tudo.
    marco = marco if marco is not None else marco_de_hoje()
    limite = (None if marco in (None, 'ultimo') else MAX_LANCAMENTOS_NOS_MARCOS)
    so_depois_de = (marco_anterior(marco, datetime(ano, mes, 1))
                    if marco not in (None, 'ultimo') else None)
    resumo['marco'] = marco
    resumo['nao_lancadas'] = 0

    throttle = build_strava_throttle(mode='mes_atual', cidade=cidade)
    total = len(candidatos)
    logger.info('conferencia (%s %02d/%d): %d atleta(s) participando',
                cidade, mes, ano, total)

    for pos, (sid, nome) in enumerate(candidatos, start=1):
        if dispensado(cidade, sid, ano, mes):
            resumo['dispensados'] += 1
            continue
        resumo['atletas'] += 1
        _av('Conferindo %s (%d de %d)...' % (nome, pos, total),
            5 + int(90 * pos / max(total, 1)))
        r = conferir_atleta(cidade, sid, nome, ano, mes, cookie, throttle,
                            limite=limite, so_depois_de=so_depois_de)
        resumo['gravadas'] += r['gravadas']
        resumo['nao_lancadas'] += r.get('nao_lancadas', 0)
        resumo['erros'] += r['erros']
        resumo['perdidas_de_verdade'] = (resumo.get('perdidas_de_verdade', 0)
                                         + r.get('perdidas_de_verdade', 0))
        if r['gravadas'] or r['motivo']:
            resumo['detalhes'].append(r)

    if resumo['gravadas']:
        # O ranking precisa ser refeito para os km novos aparecerem.
        try:
            from app.blueprints.upload import _invalidar_consolidacao
            _invalidar_consolidacao(cidade, periodos={(ano, mes)})
        except Exception as exc:
            logger.warning('conferencia: falha ao atualizar o ranking: %s', exc)

    logger.warning(
        'conferencia (%s %02d/%d): %d atleta(s), %d atividade(s) recuperada(s), '
        '%d dispensado(s), %d erro(s)',
        cidade, mes, ano, resumo['atletas'], resumo['gravadas'],
        resumo['dispensados'], resumo['erros'])
    return resumo


# ---------------------------------------------------------------- o aviso

# A CHAVE DO ALERTA (regra 21-J: etapa nova entra no `alertas._ACOES` na
# mesma sessao). Nome novo porque o antigo -- 'Conferencia recuperou link'
# -- chegava ao dono como "Falha na automacao: conferencia recuperou link",
# e ele leu, com razao, como alarme falso: a conferencia tinha funcionado.
# O que falhou foi o WhatsApp, e e' isso que o assunto passa a dizer.
ETAPA_LINK_PERDIDO = 'WhatsApp perdeu link (a conferencia trouxe)'


def avisar(resumo):
    """E-mail quando a conferência traz atividade. Só quando traz.

    POR QUE ISTO É IMPORTANTE (pedido do dono, 26/08/2026)
    Encontrar link é **sinal de que a coleta pelo WhatsApp falhou** — e quem
    decide reparear é o dono. Sem este aviso, a rede de segurança faria o
    trabalho em silêncio e o defeito de baixo continuaria crescendo sem ninguém
    saber. O e-mail diz o que houve, o que isso significa e o que conferir.
    """
    if not resumo:
        return False
    # QUEM PRECISA POSTAR (20/09/2026, P-136): nos dias 10 e 20 a conferencia
    # segura o lancamento de proposito. Segurar sem avisar seria so' silencio:
    # o atleta continuaria sem saber que ha link dele faltando.
    a_avisar = [d for d in resumo.get('detalhes', [])
                if d.get('nao_lancadas')]
    if not resumo.get('gravadas') and not a_avisar:
        return False
    # SO AVISA QUANDO O GRUPO REALMENTE PERDEU (29/08/2026).
    #
    # Em 28/08 saiu e-mail de falha as 22:41 por causa de 6 links postados
    # as 19h. Nao havia falha: o ciclo de leitura e' 05:30 e 15:30, e eles
    # so entrariam de manha. A conferencia comparava o Strava com o banco e
    # nao tinha como saber ate quando o grupo ja tinha sido lido.
    #
    # Alarme falso e' pior que alarme nenhum: ensina o dono a ignorar o
    # proximo, que pode ser verdadeiro.
    perdidas = int(resumo.get('perdidas_de_verdade') or 0)
    if not perdidas and not a_avisar:
        logger.info('conferencia (%s): %d atividade(s) trazidas, todas mais '
                    'novas que a ultima leitura do grupo — adiantamento '
                    'normal, sem aviso.',
                    resumo.get('cidade'), resumo.get('gravadas'))
        return False
    try:
        from app import correio
    except Exception:
        return False

    cidade = resumo['cidade']
    linhas = []
    for d in resumo.get('detalhes', []):
        if d.get('gravadas'):
            linhas.append('  • %s: %d atividade(s)' % (d['nome'], d['gravadas']))
    if a_avisar:
        linhas.append('')
        linhas.append('PRECISAM POSTAR O LINK NO GRUPO (não lancei por eles):')
        for d in a_avisar:
            linhas.append('  • %s — %s' % (d['nome'], d.get('motivo') or
                                           '%d link(s)' % d['nao_lancadas']))
        linhas.append('')
        linhas.append('Nos dias 10 e 20 o sistema lança no máximo %d link(s) por '
                      'pessoa, e nenhum quando falta link mais antigo que o marco '
                      'anterior — para o hábito de postar no grupo não se perder. '
                      'No último dia do mês ele lança tudo.'
                      % MAX_LANCAMENTOS_NOS_MARCOS)

    corpo = (
        'A conferência trouxe %d atividade(s) de %s que estavam no Strava e não '
        'estavam no ranking, mesmo já tendo passado leitura do grupo depois '
        'delas.\n\n'
        '%s\n\n'
        'O QUE EU SEI, E O QUE EU NÃO SEI\n'
        'Sei que estas atividades estão no Strava, que a leitura do grupo já '
        'passou depois delas, e que mesmo assim não estavam no ranking. NÃO sei '
        'por quê — e são três causas possíveis, em ordem de probabilidade:\n\n'
        '  a) O atleta não postou o link no grupo. É o caso mais comum, e não é '
        'defeito de ninguém: o Strava é dele, o link é que faltou.\n'
        '  b) O WhatsApp parou de entregar mensagem. Foi assim em 26/08 — as '
        'mensagens chegaram até 05:34 e pararam, e o ranking das 15:30 saiu só '
        'com as logos.\n'
        '  c) Defeito de leitura nosso. Aconteceu em 28-29/08: uma atividade era '
        'lida, o dono não era identificado e ela era descartada como se fosse de '
        'outra pessoa (ERROS.md §4.37).\n\n'
        'COMO SABER QUAL É, sem mexer em nada ainda:\n'
        '1) Abra o grupo e procure o link no dia da atividade. **Se o link está '
        'lá, é (b) ou (c); se não está, é (a) e não há nada a fazer.**\n'
        '2) Só se o link estiver no grupo: veja o horário do último ranking. Se '
        'só chegou a logo dos patrocinadores, sem o ranking, aí sim o sinal '
        'aponta o WhatsApp, e vale refazer o pareamento pela tela de Sessão.\n'
        '3) Se o atleta pediu para não participar deste mês, tire-o na página de '
        'ajuste dele — a conferência passa a pular esse atleta.\n\n'
        'NÃO REPAREIE O WHATSAPP SÓ POR CAUSA DESTE E-MAIL: todo pareamento é '
        'uma chance de perder a sessão (regra 15-D). Confira o passo 1 antes.\n\n'
        'Nada foi perdido: o que a conferência trouxe já está no ranking.'
        % (perdidas, cidade,
           '\n'.join(linhas) if linhas else '  (sem detalhe por atleta)')
    )
    try:
        from app import alertas
        # O parâmetro é `erro` (conferido na assinatura em 27/08) — passar
        # `detalhe=` levantaria TypeError só na hora de avisar, que é
        # justamente a hora em que não pode falhar.
        # O NOME DO ALERTA E A METADE QUE FALTAVA (29/08/2026).
        # O assunto saia como "Falha na automacao -- Conferencia
        # recuperou link", e o dono leu como alarme falso: a
        # conferencia tinha FUNCIONADO. Ela funcionou mesmo -- mas o que
        # ela achou (6 links que o WhatsApp perdeu) E uma falha real, do
        # WAHA. O erro era chamar de falha o mensageiro. Agora o assunto
        # diz o que aconteceu e quantos links faltavam.
        alertas.registrar_falha(
            cidade=cidade,
            etapa=ETAPA_LINK_PERDIDO,
            erro=corpo,
        )
        return True
    except Exception as exc:
        logger.warning('conferencia: nao consegui avisar por e-mail: %s', exc)
        return False


def rodar_agendadas(hoje=None):
    """O que a rotina diária chama. Confere as cidades cujo prazo venceu."""
    from app.models import Config

    hoje = hoje or datetime.now()
    feitos = {}
    try:
        # CIDADE INATIVA NAO ACIONA NADA (19/09/2026, regra do dono — P-135).
        from app import consolidado as _cons
        cidades = list(_cons.cidades_ativas())
    except Exception as exc:
        logger.warning('conferencia: nao consegui listar cidades: %s', exc)
        return feitos

    for cidade in cidades:
        if not vencida(cidade, hoje):
            continue
        # DOIS PORTOES, NAO UM (29/08/2026). `vencida` diz se ja passou
        # tempo suficiente em DIAS; `na_hora_certa` diz se AGORA e' a hora
        # boa do dia -- pouco antes de o ranking sair, para o que faltar
        # ainda entrar a tempo. Sem o segundo portao a conferencia caia em
        # hora aleatoria (as 22:41 de 28/08, por acaso do ultimo reinicio) e
        # o aviso chegava de madrugada, sem nada a fazer com ele.
        #
        # O pedido explicito ("conferir na proxima rodada") atravessa: quem
        # clicou quer agora, e nao no horario do ranking de amanha.
        if not _forcada(cidade) and not na_hora_certa(cidade, hoje):
            continue
        try:
            marco = 'forcada' if _forcada(cidade) else marco_de_hoje(hoje)
            resumo = rodar(cidade, hoje.year, hoje.month,
                           marco=(None if marco == 'forcada' else marco))
            # A MARCA VAI MESMO QUANDO NÃO ACHA NADA: senão uma cidade sem
            # pendência seria conferida toda madrugada, gastando a fila do
            # Strava à toa.
            marcar_rodada(cidade, hoje)
            avisar(resumo)
            feitos[cidade] = resumo

            # ===== O RANKING FINAL DO ÚLTIMO DIA (P-136, revisto em 01/10/2026) =====
            # SEMPRE acorda o Dell e pede o ranking (decisão do dono, 01/10):
            # além do que esta conferência lançou, o Dell lê o GRUPO e pega os
            # links postados depois da rodada das 15h30 — que a conferência
            # por perfil não enxerga. Antes só pedia quando a conferência
            # gravava algo, e o ranking final dependia disso. Se nada mudou, a
            # regra 6 ("sem novidade não republica") segura o envio sozinha.
            # Ranking igual aos outros, sem cabeçalho de "fechamento"; no dia 1
            # o Dell confere de novo o mês anterior (P-146).
            if marco == 'ultimo':
                try:
                    from app import rodada
                    ok_pedido, aviso_pedido = rodada.pedir(
                        cidade, por='conferencia do último dia')
                    logger.warning(
                        'conferencia (%s): último dia trouxe %d atividade(s) — '
                        'pedi o ranking final (%s: %s)', cidade, resumo.get('gravadas') or 0,
                        'aceito' if ok_pedido else 'recusado', aviso_pedido)
                except Exception as exc:
                    logger.error('conferencia (%s): não consegui pedir o '
                                 'ranking do último dia: %s', cidade, exc)
        except Exception as exc:
            db.session.rollback()
            logger.error('conferencia (%s) falhou: %s', cidade, exc, exc_info=True)
    return feitos
