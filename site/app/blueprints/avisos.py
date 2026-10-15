# -*- coding: utf-8 -*-
"""Avisos programados enviados no WhatsApp pela automacao.

POR QUE ESTE MODULO EXISTE
O grupo recebe o ranking automaticamente, mas havia recados que continuavam
manuais: "venha buscar sua medalha", "mudou a regra", "tem evento sabado".
Aqui esses recados viram agendamentos com codigo, publico definido e horario,
e a automacao (send_ranking.py) apenas consome o que estiver vencido.

OS QUATRO PUBLICOS
  todos       - todo atleta com telefone
  ranking     - quem aparece no ranking do mes corrente
  ativos      - quem postou link nos ultimos N dias (padrao 45)
  manual      - atletas escolhidos um a um

MEDALHA NAO SE AGENDA AQUI (13/08/2026). A cobranca de medalha mora na
/admin/central: ela se dispara quando o admin decide e pode repetir, porque
medalha nao tem data fixa — tem "chegou no balcao". Ver PUBLICOS_APOSENTADOS.

REGRA DE OURO: nada e enviado sem telefone confirmado, e ninguem recebe o
mesmo aviso duas vezes no mesmo dia (tabela avisos_envios).
"""
import json
import re
import secrets as _secrets
from datetime import datetime, timedelta

from flask import (Blueprint, abort, current_app, flash, jsonify, redirect,
                   render_template, request, url_for)
from flask_login import current_user, login_required
from sqlalchemy import text as sa_text

from app import db
from app.navegacao import voltar
from app.models import Activity, Atleta, Config

avisos_bp = Blueprint('avisos', __name__, url_prefix='/avisos')

DIAS_ATIVO_PADRAO = 45
# O PUBLICO 'medalhistas' SAIU DAQUI EM 13/08/2026 (decisao do dono).
#
# Existiam DUAS mensagens de medalha, cada uma com seus marcadores:
#   - aqui, um aviso programado com {estabelecimento} {endereco} {mapa} {horario}
#   - na /admin/central, a "Cobranca de medalha" com {local} {link} {patrocinador}
# Duas mensagens para a mesma coisa divergem sozinhas — foi a causa raiz de
# 02-03/08 em outra roupa. Fica a da Central: e ela que se dispara quando o
# admin decide, e pode repetir; o aviso programado nunca serviu bem a isso,
# porque medalha nao tem data fixa, tem "chegou no balcao".
#
# A CHAVE CONTINUA RECONHECIDA no resto do modulo de proposito: aviso ja
# cadastrado com esse publico segue funcionando e pode ser apagado pelo admin.
# Some so da lista de OPCOES NOVAS. Tirar do dicionario faria a tela quebrar
# ao renderizar um aviso antigo, que e trocar uma duplicidade por um erro.
PUBLICOS = {
    'todos': 'Todos os atletas com telefone',
    'ranking': 'Somente quem esta no ranking do mes',
    'ativos': f'Somente quem postou link nos ultimos {DIAS_ATIVO_PADRAO} dias',
    'manual': 'Atletas escolhidos manualmente',
}
# Publicos que ainda funcionam, mas nao se oferecem mais para avisos novos.
PUBLICOS_APOSENTADOS = {
    'medalhistas': 'Somente quem bateu a meta de um mes escolhido '
                   '(aposentado — use a Cobrança de medalha na Central)',
}
PUBLICOS_TODOS = dict(PUBLICOS, **PUBLICOS_APOSENTADOS)
FREQUENCIAS = {
    'uma_vez': 'Uma vez so',
    'diario': 'Todo dia',
    'semanal': 'Uma vez por semana',
    'com_ranking': 'Junto com o ranking',
}
_SCHEMA_PRONTO = False


# ===================== SCHEMA =====================

def ensure_schema():
    """Cria as tabelas de avisos na primeira utilizacao."""
    global _SCHEMA_PRONTO
    if _SCHEMA_PRONTO:
        return
    with db.engine.begin() as conn:
        conn.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS avisos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                codigo VARCHAR(12) NOT NULL UNIQUE,
                cidade VARCHAR(100) NOT NULL,
                titulo VARCHAR(160) NOT NULL,
                mensagem TEXT NOT NULL,
                imagem_blob BLOB,
                imagem_mime VARCHAR(60),
                imagem_nome VARCHAR(200),
                publico VARCHAR(20) NOT NULL DEFAULT 'todos',
                publico_params TEXT,
                frequencia VARCHAR(20) NOT NULL DEFAULT 'uma_vez',
                horario VARCHAR(5) NOT NULL DEFAULT '10:30',
                horario_2 VARCHAR(5),
                dia_semana INTEGER,
                data_inicio DATE,
                data_fim DATE,
                so_mes_vigente INTEGER NOT NULL DEFAULT 0,
                ativo INTEGER NOT NULL DEFAULT 1,
                previa_enviada_em DATETIME,
                criado_em DATETIME NOT NULL,
                criado_por VARCHAR(120)
            )
        """))
        conn.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS avisos_envios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                aviso_id INTEGER NOT NULL,
                telefone VARCHAR(24) NOT NULL,
                chave_atleta VARCHAR(120),
                enviado_em DATETIME NOT NULL,
                dia VARCHAR(10) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'ok',
                detalhe VARCHAR(300),
                CONSTRAINT _aviso_dia_uc UNIQUE (aviso_id, telefone, dia)
            )
        """))
        conn.execute(sa_text(
            "CREATE INDEX IF NOT EXISTS idx_avisos_envios_aviso "
            "ON avisos_envios(aviso_id, dia)"))
        # Quem ja retirou a medalha do mes: some da lista de cobranca.
        conn.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS medalhas_retiradas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                ano INTEGER NOT NULL,
                mes INTEGER NOT NULL,
                chave_atleta VARCHAR(120) NOT NULL,
                retirado_em DATETIME NOT NULL,
                marcado_por VARCHAR(120),
                CONSTRAINT _medalha_uc UNIQUE (cidade, ano, mes, chave_atleta)
            )
        """))
        # Onde a medalha e retirada, POR MES (P-54, 12/08).
        #
        # ANTES ERA UMA LINHA POR CIDADE, PARA SEMPRE — e isso contradizia a
        # propria P-23, que ja dizia "o local muda todo mes". Com a chave em
        # `cidade` o local de agosto sobrescrevia o de julho, e a mensagem de um
        # mes fechado passava a apontar para o estabelecimento do mes seguinte.
        #
        # O ENDERECO NAO MORA MAIS AQUI: ele pertence ao CADASTRO DO
        # PATROCINADOR (`patrocinador_catalogo.endereco`), porque e dele o
        # balcao. Aqui fica so a decisao do mes: QUEM recebe as medalhas deste
        # mes. Assim, corrigir o endereco uma vez conserta todos os meses.
        conn.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS local_retirada (
                cidade VARCHAR(100) PRIMARY KEY,
                estabelecimento VARCHAR(160),
                endereco VARCHAR(300),
                maps_url VARCHAR(500),
                horario_funcionamento VARCHAR(160),
                atualizado_em DATETIME
            )
        """))
        # Migracao para a chave (cidade, ano, mes). Idempotente: so acontece
        # enquanto a coluna `ano` nao existir.
        colunas = {c[1] for c in conn.execute(
            sa_text('PRAGMA table_info(local_retirada)')).fetchall()}
        if 'ano' not in colunas:
            conn.execute(sa_text('ALTER TABLE local_retirada '
                                 'RENAME TO local_retirada_por_cidade'))
            conn.execute(sa_text("""
                CREATE TABLE local_retirada (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cidade VARCHAR(100) NOT NULL,
                    ano INTEGER NOT NULL,
                    mes INTEGER NOT NULL,
                    catalogo_id INTEGER,
                    estabelecimento VARCHAR(160),
                    endereco VARCHAR(300),
                    maps_url VARCHAR(500),
                    horario_funcionamento VARCHAR(160),
                    atualizado_em DATETIME,
                    CONSTRAINT _local_mes_uc UNIQUE (cidade, ano, mes)
                )
            """))
            # O que existia era "o local de agora". Sem mes, ele so pode ser
            # atribuido ao mes corrente — e fica ali como ponto de partida,
            # nunca como valor herdado pelos outros meses (regra 10).
            hoje = datetime.now()
            conn.execute(sa_text("""
                INSERT INTO local_retirada
                    (cidade, ano, mes, estabelecimento, endereco, maps_url,
                     horario_funcionamento, atualizado_em)
                SELECT cidade, :a, :m, estabelecimento, endereco, maps_url,
                       horario_funcionamento, atualizado_em
                FROM local_retirada_por_cidade
                WHERE estabelecimento IS NOT NULL AND TRIM(estabelecimento) <> ''
            """), {'a': hoje.year, 'm': hoje.month})
            current_app.logger.warning(
                'local_retirada migrada para (cidade, ano, mes); '
                'a tabela antiga virou local_retirada_por_cidade')

        # O endereco pertence ao patrocinador, nao a cidade (P-54).
        cat = {c[1] for c in conn.execute(
            sa_text('PRAGMA table_info(patrocinador_catalogo)')).fetchall()}
        if cat and 'endereco' not in cat:
            conn.execute(sa_text('ALTER TABLE patrocinador_catalogo '
                                 'ADD COLUMN endereco VARCHAR(300)'))
        if cat and 'maps_url' not in cat:
            conn.execute(sa_text('ALTER TABLE patrocinador_catalogo '
                                 'ADD COLUMN maps_url VARCHAR(500)'))
        # Medalhistas congelados no fim de cada mes (o ranking vivo muda se
        # alguem postar link atrasado; para cobrar medalha precisamos da foto
        # do mes fechado).
        conn.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS medalhistas_mes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                ano INTEGER NOT NULL,
                mes INTEGER NOT NULL,
                chave_atleta VARCHAR(120) NOT NULL,
                nome VARCHAR(160),
                modalidade VARCHAR(30) NOT NULL,
                km REAL,
                congelado_em DATETIME NOT NULL,
                CONSTRAINT _medalhista_uc UNIQUE (cidade, ano, mes, chave_atleta, modalidade)
            )
        """))
    _SCHEMA_PRONTO = True


# ===================== TELEFONE =====================

def normalizar_telefone(bruto):
    """Qualquer jeito que o admin digitar vira 55DDD9XXXXXXXX.

    Aceita '(DD) 9XXXX-XXXX', 'DD9XXXXXXXX', '+55 DD 9XXXX-XXXX'. Devolve None
    quando nao da para confiar — melhor nao enviar do que enviar para estranho.
    """
    numero = re.sub(r'\D', '', str(bruto or ''))
    if not numero:
        return None
    if numero.startswith('0'):
        numero = numero.lstrip('0')
    if not numero.startswith('55'):
        numero = '55' + numero
    # 55 + DDD(2) + numero(8 ou 9)
    if len(numero) == 12:
        # fixo ou celular antigo sem o 9: insere o 9 dos celulares
        ddd, resto = numero[2:4], numero[4:]
        if resto[0] in '6789':
            numero = f'55{ddd}9{resto}'
    if len(numero) != 13:
        return None
    return numero


def telefone_bonito(telefone):
    numero = re.sub(r'\D', '', str(telefone or ''))
    if len(numero) == 13 and numero.startswith('55'):
        return f'({numero[2:4]}) {numero[4:9]}-{numero[9:]}'
    return telefone or ''


# ===================== PUBLICO =====================

def _atletas_da_cidade(cidade):
    """Atletas que aparecem em atividades da cidade, com telefone e ultimo link."""
    linhas = db.session.execute(sa_text("""
        SELECT DISTINCT a.strava_id, a.nome_atual, a.telefone_normalizado, a.arquivado
        FROM atletas a
        WHERE EXISTS (SELECT 1 FROM activities ac
                      WHERE ac.id_usuario_strava = a.strava_id AND ac.cidade = :c)
    """), {'c': cidade}).fetchall()
    resultado = []
    for strava_id, nome, telefone, arquivado in linhas:
        if arquivado:
            continue
        resultado.append({
            'chave': str(strava_id),
            'nome': nome or 'Atleta',
            'telefone': normalizar_telefone(telefone),
            'telefone_bruto': telefone,
        })
    return resultado


def _ultimo_link_por_atleta(cidade):
    """{strava_id: data do ultimo link} — base do filtro de ativos."""
    linhas = db.session.execute(sa_text("""
        SELECT id_usuario_strava, MAX(data_hora) FROM activities
        WHERE cidade = :c AND id_usuario_strava IS NOT NULL AND id_usuario_strava != ''
        GROUP BY id_usuario_strava
    """), {'c': cidade}).fetchall()
    saida = {}
    for sid, quando in linhas:
        if not quando:
            continue
        if isinstance(quando, str):
            try:
                quando = datetime.fromisoformat(quando)
            except ValueError:
                continue
        saida[str(sid)] = quando
    return saida


def refazer_medalhistas(cidade, ano, mes):
    """Joga fora a foto congelada e tira outra com os km de AGORA.

    POR QUE EXISTE UM LUGAR SO PARA ISTO
    A lista de medalhistas do mes e uma foto (`medalhistas_mes`), tirada na
    primeira vez que alguem abre o mes. Se os km mudarem depois — foi o caso da
    uma atleta em 06/08, que passou de 277 para 413 km depois da importacao por
    perfil e so entao bateu a meta —, a foto continua velha e a pessoa some da
    tela de etiquetas mesmo aparecendo no ranking.

    Refazer aqui, num lugar so, e o que faz o botao "atualizar" valer para
    TODAS as telas de uma vez: etiquetas, medalhistas e cobranca leem a mesma
    tabela. Antes isto estava escrito dentro da tela de etiquetas e so
    consertava ali.

    Continua sendo A PEDIDO, nunca automatico: a foto existe justamente para o
    mes fechado nao mudar sozinho (regra 9). Quem manda refazer e o admin, que
    sabe o que corrigiu.

    Devolve quantos medalhistas ficaram.
    """
    ensure_schema()
    db.session.execute(sa_text(
        "DELETE FROM medalhistas_mes WHERE cidade=:c AND ano=:a AND mes=:m"),
        {'c': cidade, 'a': ano, 'm': mes})
    db.session.commit()
    return congelar_medalhistas(cidade, ano, mes)


def medalhistas_do_mes(cidade, ano, mes, so_pendentes=False):
    """Quem bateu a meta no mes. Usa a foto congelada; se nao houver, calcula.

    so_pendentes=True remove quem ja foi marcado como "retirou a medalha".
    """
    ensure_schema()

    def _foto():
        return db.session.execute(sa_text("""
            SELECT chave_atleta, nome, modalidade, km, congelado_em FROM medalhistas_mes
            WHERE cidade = :c AND ano = :a AND mes = :m
        """), {'c': cidade, 'a': ano, 'm': mes}).fetchall()

    if not _mes_terminou(ano, mes):
        # Mes em andamento: sempre ao vivo, nunca da foto (ver congelar_medalhistas).
        linhas = [(c, n, mo, k) for c, n, mo, k in _calcular_medalhistas(cidade, ano, mes)]
    else:
        foto = _foto()
        # FOTO TIRADA COM O MES AINDA ABERTO NAO E FOTO OFICIAL (01/10/2026):
        # foi o que deixou setembro com 1 medalhista. Refaz uma vez, com os km
        # de agora, e deixa rastro no log. Foto tirada depois do fim do mes
        # continua intocavel (regra 23).
        fim = _fim_do_mes(ano, mes)
        def _antes_do_fim(valor):
            try:
                return datetime.fromisoformat(str(valor)) < fim
            except (TypeError, ValueError):
                return False
        if foto and any(_antes_do_fim(f[4]) for f in foto):
            current_app.logger.warning(
                'Medalhistas de %s %s/%s: a foto foi tirada com o mes aberto '
                '(%d pessoa(s)) — refazendo com os km de agora.',
                cidade, mes, ano, len(foto))
            refazer_medalhistas(cidade, ano, mes)
            foto = _foto()
        elif not foto:
            congelar_medalhistas(cidade, ano, mes)
            foto = _foto()
        linhas = [(c, n, mo, k) for c, n, mo, k, _em in foto]

    # Quem retirou se consulta SEMPRE, mesmo sem filtrar (P-51): quando a
    # pessoa continua na lista, a tela precisa poder dizer "já retirou" em vez
    # de a linha simplesmente sumir. Sumir dá a impressão de que o sistema
    # comeu alguém — e obriga o admin a decorar quem já foi.
    retirados = {r[0] for r in db.session.execute(sa_text(
        "SELECT chave_atleta FROM medalhas_retiradas "
        "WHERE cidade = :c AND ano = :a AND mes = :m"),
        {'c': cidade, 'a': ano, 'm': mes}).fetchall()}

    saida = {}
    for chave, nome, modalidade, km in linhas:
        if so_pendentes and chave in retirados:
            continue
        item = saida.setdefault(str(chave), {
            'chave': str(chave), 'nome': nome or 'Atleta',
            'modalidades': [], 'km': 0.0,
            'retirou': chave in retirados,
        })
        item['modalidades'].append(modalidade)
        item['km'] = max(item['km'], km or 0)
    return list(saida.values())


def _fim_do_mes(ano, mes):
    """Primeiro instante do mes seguinte: dali em diante o mes acabou."""
    return datetime(ano + 1, 1, 1) if mes == 12 else datetime(ano, mes + 1, 1)


def _mes_terminou(ano, mes, agora=None):
    return (agora or datetime.now()) >= _fim_do_mes(ano, mes)


def _calcular_medalhistas(cidade, ano, mes):
    """Quem bateu a meta, calculado AGORA a partir das atividades. Nao grava.

    Devolve [(chave, nome, modalidade, km), ...].
    """
    from sqlalchemy import extract
    from app.blueprints.ranking import process_ranking, _chave_atleta_ranking

    config = Config.query.filter_by(cidade=cidade).first()
    if not config:
        return []
    atividades = Activity.query.filter(
        Activity.cidade == cidade,
        extract('month', Activity.data_hora) == mes,
        extract('year', Activity.data_hora) == ano,
    ).all()
    if not atividades:
        return []
    dados = process_ranking(atividades, config, mes, ano)
    saida = []
    for secao in ('corrida_caminhada', 'ciclismo'):
        for atleta in (dados.get(secao) or {}).get('atingiram_meta', []) or []:
            saida.append((_chave_atleta_ranking(atleta), atleta.get('nome'),
                          secao, atleta.get('km')))
    return saida


def congelar_medalhistas(cidade, ano, mes):
    """Grava a foto dos medalhistas do mes. Idempotente.

    MES EM ANDAMENTO NUNCA VIRA FOTO (01/10/2026). Ate aqui, a primeira tela
    que abrisse o mes congelava a lista — inclusive o mes CORRENTE. Em
    16/09/2026 15:48 alguem passou por setembro na tela de medalhistas e a
    foto ficou com 1 pessoa (a unica que tinha batido a meta ate ali); no dia
    1/10 setembro tinha muitos medalhistas e etiquetas, medalhas e avisos
    mostravam so ela. Com o mes aberto a lista e calculada ao vivo e nada se
    grava; a foto so nasce depois que o mes termina. Devolve quantos
    medalhistas ha (gravados ou, no mes aberto, calculados).
    """
    ensure_schema()
    lista = _calcular_medalhistas(cidade, ano, mes)
    if not _mes_terminou(ano, mes):
        current_app.logger.info(
            'Medalhistas de %s %s/%s: mes em andamento, lista ao vivo (%d) — '
            'nada foi congelado.', cidade, mes, ano, len(lista))
        return len(lista)

    agora = datetime.now()
    gravados = 0
    for chave, nome, rotulo, km in lista:
            atleta = {'nome': nome, 'km': km}
            try:
                db.session.execute(sa_text("""
                    INSERT OR REPLACE INTO medalhistas_mes
                    (cidade, ano, mes, chave_atleta, nome, modalidade, km, congelado_em)
                    VALUES (:c, :a, :m, :k, :n, :mod, :km, :em)
                """), {'c': cidade, 'a': ano, 'm': mes, 'k': chave,
                       'n': atleta.get('nome'), 'mod': rotulo,
                       'km': atleta.get('km'), 'em': agora})
                gravados += 1
            except Exception as exc:
                current_app.logger.warning('Falha ao congelar medalhista %s: %s', chave, exc)
    db.session.commit()
    return gravados


def destinatarios(cidade, publico, params=None):
    """Lista de destinatarios de um aviso. Cada item traz chave, nome e telefone.

    Quem nao tem telefone volta com telefone=None de proposito: a tela mostra
    esses casos para o admin completar, em vez de sumir com a pessoa.
    """
    ensure_schema()
    params = params or {}
    todos = _atletas_da_cidade(cidade)
    indice = {a['chave']: a for a in todos}

    if publico == 'todos':
        return todos

    if publico == 'ativos':
        dias = int(params.get('dias') or DIAS_ATIVO_PADRAO)
        corte = datetime.now() - timedelta(days=dias)
        ultimos = _ultimo_link_por_atleta(cidade)
        return [a for a in todos
                if ultimos.get(a['chave']) and ultimos[a['chave']] >= corte]

    if publico == 'ranking':
        from sqlalchemy import extract
        from app.blueprints.ranking import process_ranking, _chave_atleta_ranking
        agora = datetime.now()
        ano = int(params.get('ano') or agora.year)
        mes = int(params.get('mes') or agora.month)
        config = Config.query.filter_by(cidade=cidade).first()
        if not config:
            return []
        atividades = Activity.query.filter(
            Activity.cidade == cidade,
            extract('month', Activity.data_hora) == mes,
            extract('year', Activity.data_hora) == ano,
        ).all()
        dados = process_ranking(atividades, config, mes, ano)
        chaves = set()
        for secao in ('corrida_caminhada', 'ciclismo'):
            bloco = dados.get(secao) or {}
            for grupo in ('atingiram_meta', 'nao_atingiram'):
                for atleta in bloco.get(grupo, []) or []:
                    chaves.add(_chave_atleta_ranking(atleta))
        return [indice[c] for c in chaves if c in indice]

    if publico == 'medalhistas':
        agora = datetime.now()
        ano = int(params.get('ano') or agora.year)
        mes = int(params.get('mes') or agora.month)
        pendentes = params.get('so_pendentes', True)
        lista = medalhistas_do_mes(cidade, ano, mes, so_pendentes=pendentes)
        saida = []
        for item in lista:
            base = indice.get(item['chave'], {
                'chave': item['chave'], 'nome': item['nome'],
                'telefone': None, 'telefone_bruto': None,
            })
            base = dict(base)
            base['modalidades'] = item['modalidades']
            saida.append(base)
        return saida

    if publico == 'manual':
        escolhidos = params.get('chaves') or []
        return [indice[c] for c in escolhidos if c in indice]

    return []


# ===================== TEXTO =====================

def _local_retirada(cidade, ano=None, mes=None):
    """O local de retirada DAQUELE mês. Nunca herda de outro (regra 10).

    `ano`/`mes` omitidos = mês corrente, que é o que a tela do dia a dia quer.
    Pedir um mês fechado devolve o local **daquele** mês, e é isso que faz a
    mensagem de julho continuar apontando para o balcão de julho depois que
    agosto trocou de patrocinador.

    O ENDEREÇO SAI DO PATROCINADOR quando há um vinculado (P-54): é dele o
    balcão, então corrigir uma vez conserta todos os meses. O que estiver
    preenchido na própria linha do mês prevalece — serve para o caso em que a
    entrega, naquele mês, é em outro lugar que não o do patrocinador.
    """
    ensure_schema()
    if not ano or not mes:
        agora = datetime.now()
        ano, mes = ano or agora.year, mes or agora.month
    linha = db.session.execute(sa_text(
        "SELECT l.estabelecimento, l.endereco, l.maps_url, "
        "       l.horario_funcionamento, c.nome_exibicao, c.nome_interno, "
        "       c.endereco, c.maps_url "
        "FROM local_retirada l "
        "LEFT JOIN patrocinador_catalogo c ON c.id = l.catalogo_id "
        "WHERE l.cidade = :c AND l.ano = :a AND l.mes = :m"),
        {'c': cidade, 'a': int(ano), 'm': int(mes)}).fetchone()
    if linha:
        return {
            'estabelecimento': linha[0] or linha[4] or linha[5] or '',
            'endereco': linha[1] or linha[6] or '',
            'maps_url': linha[2] or linha[7] or '',
            'horario': linha[3] or '',
            # `proprio` = este mês tem endereço cadastrado só dele (entrega
            # excepcional). Falso = veio da ficha do patrocinador. A tela usa
            # para o admin saber se está mexendo no mês ou herdando.
            'proprio': bool((linha[1] or '').strip()),
        }

    # SEM LINHA DO MÊS: cai para o PATROCINADOR DAQUELE MÊS (12/08).
    #
    # Isto NÃO é o fallback proibido pela regra 10 — não herda de outro mês.
    # É derivar do patrocinador **daquele período**: quem banca as medalhas de
    # julho é quem tem o balcão em julho, e o endereço dele já está cadastrado.
    #
    # POR QUE PRECISOU: a cobrança de julho saiu com "o local ainda está sendo
    # definido" mesmo com o patrocinador certo na mensagem (Relojoaria 3R),
    # porque ninguém tinha criado a linha de julho. Obrigar um cadastro por mês
    # para repetir um endereço que não muda é trabalho que o admin não deveria
    # ter — e é ele que fica sem resposta quando o atleta pergunta "onde?".
    try:
        from app.blueprints.ranking import _patrocinadores_periodo
        itens = _patrocinadores_periodo(cidade, int(ano), int(mes)) or []
    except Exception as exc:
        current_app.logger.debug('local pelo patrocinador (%s %s/%s): %s',
                                 cidade, mes, ano, exc)
        return {}
    # O patrocinador do MÊS guarda só `catalogo_id` — o endereço mora na ficha
    # do catálogo, que é onde ele não se repete.
    # Usa as PROPRIEDADES do modelo, que já sabem achar a ficha por id **ou
    # pelo nome** — `catalogo_id` está vazio em todos os registros existentes,
    # e exigir o vínculo fazia esta busca nunca encontrar nada.
    for sp in itens:
        endereco = (getattr(sp, 'endereco', '') or '').strip()
        if not endereco:
            continue
        return {
            'estabelecimento': (sp.exibicao or '').strip(),
            'endereco': endereco,
            'maps_url': (getattr(sp, 'maps_url', '') or '').strip(),
            'horario': '',
            'proprio': False,
        }
    return {}


def montar_mensagem(aviso, destinatario, cidade):
    """Troca os marcadores do texto pelos dados de quem vai receber.

    Marcadores: {nome}, {estabelecimento}, {endereco}, {mapa}, {horario}, {mes}
    """
    texto = aviso.get('mensagem') or ''
    params = aviso.get('publico_params') or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError:
            params = {}
    meses = ['', 'janeiro', 'fevereiro', 'marco', 'abril', 'maio', 'junho',
             'julho', 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro']
    agora = datetime.now()
    mes_num = int(params.get('mes') or agora.month)
    ano_num = int(params.get('ano') or agora.year)

    # O LOCAL É O DO MÊS DO AVISO, não o de hoje (13/08). O aviso escolhe o
    # público por mês ("quem bateu a meta de julho") e depois pedia o local sem
    # dizer o mês — então a mensagem de julho vinha com o balcão de agosto, ou
    # em branco quando agosto ainda não tinha nenhum. Era isso que deixava
    # "Retire em **" e "Como chegar:" vazios na prévia.
    local = _local_retirada(cidade, ano_num, mes_num)
    trocas = {
        '{nome}': (destinatario.get('nome') or '').split(' ')[0],
        '{nome_completo}': destinatario.get('nome') or '',
        '{estabelecimento}': local.get('estabelecimento', ''),
        '{endereco}': local.get('endereco', ''),
        '{mapa}': local.get('maps_url', ''),
        '{horario}': local.get('horario', ''),
        '{mes}': meses[mes_num] if 1 <= mes_num <= 12 else '',
    }
    for marcador, valor in trocas.items():
        texto = texto.replace(marcador, valor)

    # MARCADOR SEM VALOR NÃO PODE DEIXAR CICATRIZ (13/08). Com o local vazio a
    # prévia mostrava `Retire em **` (negrito sem nada dentro) e `Como chegar:`
    # sozinho — pior que a informação faltando, porque anuncia a falta para o
    # atleta. Some o asterisco órfão e some a linha que virou só rótulo.
    texto = re.sub(r'\*\s*\*', '', texto)
    texto = re.sub(r'(?m)^[^\n:]{1,40}:[ \t]*$\n?', '', texto)
    texto = re.sub(r'\n{3,}', '\n\n', texto)
    return texto.strip()


MODELO_MEDALHA = """Oi {nome}! 🏅

Voce bateu a meta de {mes} e sua medalha esta esperando por voce.

Retire em *{estabelecimento}*
{endereco}
{horario}

Como chegar: {mapa}

Parabens pela dedicacao e continue se movimentando!
_Mensagem automatica do grupo Movimente-se._"""


# ===================== AGENDAMENTO =====================

def _hoje_str():
    return datetime.now().strftime('%Y-%m-%d')


def aviso_vencido(aviso, agora=None):
    """Este aviso deve ser enviado agora? Devolve (sim, motivo)."""
    agora = agora or datetime.now()
    if not aviso.get('ativo'):
        return False, 'desativado'

    hoje = agora.date()
    if aviso.get('data_inicio'):
        inicio = aviso['data_inicio']
        if isinstance(inicio, str):
            inicio = datetime.fromisoformat(inicio).date()
        if hoje < inicio:
            return False, 'ainda nao comecou'
    if aviso.get('data_fim'):
        fim = aviso['data_fim']
        if isinstance(fim, str):
            fim = datetime.fromisoformat(fim).date()
        if hoje > fim:
            return False, 'ja terminou'

    if aviso.get('so_mes_vigente'):
        params = aviso.get('publico_params') or {}
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except ValueError:
                params = {}
        mes_ref = int(params.get('mes') or agora.month)
        ano_ref = int(params.get('ano') or agora.year)
        if (mes_ref, ano_ref) != (agora.month, agora.year):
            return False, 'restrito ao mes vigente'

    frequencia = aviso.get('frequencia') or 'uma_vez'
    if frequencia == 'com_ranking':
        return True, 'junto com o ranking'
    if frequencia == 'semanal':
        dia = aviso.get('dia_semana')
        if dia is not None and int(dia) != agora.weekday():
            return False, 'nao e o dia da semana'

    # Horario: so dispara depois da hora marcada (a automacao roda 2x ao dia).
    horarios = [h for h in (aviso.get('horario'), aviso.get('horario_2')) if h]
    if horarios:
        passou = False
        for h in horarios:
            try:
                hh, mm = [int(p) for p in str(h).split(':')[:2]]
            except (ValueError, TypeError):
                continue
            if (agora.hour, agora.minute) >= (hh, mm):
                passou = True
                break
        if not passou:
            return False, 'ainda nao deu o horario'

    return True, ''


def ja_enviado_hoje(aviso_id, telefone, dia=None):
    linha = db.session.execute(sa_text(
        "SELECT 1 FROM avisos_envios WHERE aviso_id = :a AND telefone = :t AND dia = :d"),
        {'a': aviso_id, 't': telefone, 'd': dia or _hoje_str()}).fetchone()
    return bool(linha)


def registrar_envio(aviso_id, telefone, chave, status='ok', detalhe=None):
    ensure_schema()
    try:
        db.session.execute(sa_text("""
            INSERT OR REPLACE INTO avisos_envios
            (aviso_id, telefone, chave_atleta, enviado_em, dia, status, detalhe)
            VALUES (:a, :t, :k, :em, :d, :s, :det)
        """), {'a': aviso_id, 't': telefone, 'k': chave, 'em': datetime.now(),
               'd': _hoje_str(), 's': status, 'det': (detalhe or '')[:300]})
        db.session.commit()
        return True
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning('Falha ao registrar envio de aviso: %s', exc)
        return False


def gerar_codigo():
    """Codigo curto e legivel, para o admin citar o aviso ao falar com alguem."""
    alfabeto = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'   # sem 0/O/1/I
    for _ in range(30):
        codigo = 'AV-' + ''.join(_secrets.choice(alfabeto) for _ in range(5))
        existe = db.session.execute(sa_text(
            "SELECT 1 FROM avisos WHERE codigo = :c"), {'c': codigo}).fetchone()
        if not existe:
            return codigo
    return 'AV-' + _secrets.token_hex(3).upper()


def carregar_aviso(aviso_id, cidade=None):
    ensure_schema()
    linha = db.session.execute(sa_text("""
        SELECT id, codigo, cidade, titulo, mensagem, imagem_mime, imagem_nome,
               publico, publico_params, frequencia, horario, horario_2,
               dia_semana, data_inicio, data_fim, so_mes_vigente, ativo,
               previa_enviada_em, criado_em, criado_por
        FROM avisos WHERE id = :i
    """), {'i': aviso_id}).fetchone()
    if not linha:
        return None
    aviso = {
        'id': linha[0], 'codigo': linha[1], 'cidade': linha[2], 'titulo': linha[3],
        'mensagem': linha[4], 'imagem_mime': linha[5], 'imagem_nome': linha[6],
        'publico': linha[7], 'publico_params': linha[8], 'frequencia': linha[9],
        'horario': linha[10], 'horario_2': linha[11], 'dia_semana': linha[12],
        'data_inicio': linha[13], 'data_fim': linha[14],
        'so_mes_vigente': bool(linha[15]), 'ativo': bool(linha[16]),
        'previa_enviada_em': linha[17], 'criado_em': linha[18], 'criado_por': linha[19],
    }
    if cidade and aviso['cidade'] != cidade:
        return None
    if aviso['publico_params']:
        try:
            aviso['publico_params'] = json.loads(aviso['publico_params'])
        except ValueError:
            aviso['publico_params'] = {}
    else:
        aviso['publico_params'] = {}
    return aviso


def listar_avisos(cidade):
    ensure_schema()
    linhas = db.session.execute(sa_text("""
        SELECT id, codigo, titulo, publico, frequencia, horario, ativo,
               data_inicio, data_fim, imagem_mime, criado_em
        FROM avisos WHERE cidade = :c ORDER BY ativo DESC, id DESC
    """), {'c': cidade}).fetchall()
    saida = []
    for l in linhas:
        enviados = db.session.execute(sa_text(
            "SELECT COUNT(*) FROM avisos_envios WHERE aviso_id = :a AND status = 'ok'"),
            {'a': l[0]}).scalar() or 0
        saida.append({
            'id': l[0], 'codigo': l[1], 'titulo': l[2], 'publico': l[3],
            'publico_label': PUBLICOS_TODOS.get(l[3], l[3]), 'frequencia': l[4],
            'frequencia_label': FREQUENCIAS.get(l[4], l[4]), 'horario': l[5],
            'ativo': bool(l[6]), 'data_inicio': l[7], 'data_fim': l[8],
            'tem_imagem': bool(l[9]), 'criado_em': l[10], 'enviados': enviados,
        })
    return saida


# ===================== TELAS =====================

def _admin_required():
    if not current_user.is_authenticated or not current_user.pode_administrar:
        abort(403)


@avisos_bp.route('/')
@login_required
def index():
    _admin_required()
    cidade = current_user.cidade_ativa
    ensure_schema()
    agora = datetime.now()
    sem_telefone = [a for a in _atletas_da_cidade(cidade) if not a['telefone']]

    # Mês do LOCAL DE RETIRADA (P-54). Sem ele o admin cadastrava às cegas e o
    # endereço caía sempre no mês corrente.
    from app.blueprints.ranking import MESES_PT
    ano_local = request.args.get('ano', type=int) or agora.year
    mes_local = request.args.get('mes', type=int) or agora.month
    if not 1 <= mes_local <= 12:
        mes_local = agora.month

    return render_template(
        'avisos/index.html',
        avisos=listar_avisos(cidade), publicos=PUBLICOS, frequencias=FREQUENCIAS,
        local=_local_retirada(cidade, ano_local, mes_local),
        sem_telefone=sem_telefone,
        cidade=cidade,
        ano_atual=agora.year, mes_atual=agora.month,
        ano_local=ano_local, mes_local=mes_local, meses=MESES_PT,
        anos_local=list(range(agora.year + 1, agora.year - 5, -1)),
        telefone_bonito=telefone_bonito,
    )


@avisos_bp.route('/salvar', methods=['POST'])
@login_required
def salvar():
    _admin_required()
    ensure_schema()
    cidade = current_user.cidade_ativa
    aviso_id = request.form.get('aviso_id', type=int)

    titulo = (request.form.get('titulo') or '').strip()
    mensagem = (request.form.get('mensagem') or '').strip()
    if not titulo or not mensagem:
        flash('Titulo e mensagem sao obrigatorios.', 'warning')
        return voltar('avisos.index')

    publico = (request.form.get('publico') or 'todos').strip()
    if publico not in PUBLICOS_TODOS:   # aposentado ainda edita
        publico = 'todos'
    frequencia = (request.form.get('frequencia') or 'uma_vez').strip()
    if frequencia not in FREQUENCIAS:
        frequencia = 'uma_vez'

    params = {}
    if publico in ('medalhistas', 'ranking'):
        params['ano'] = request.form.get('ref_ano', type=int) or datetime.now().year
        params['mes'] = request.form.get('ref_mes', type=int) or datetime.now().month
    if publico == 'medalhistas':
        params['so_pendentes'] = bool(request.form.get('so_pendentes'))
    if publico == 'ativos':
        params['dias'] = request.form.get('dias', type=int) or DIAS_ATIVO_PADRAO
    if publico == 'manual':
        params['chaves'] = request.form.getlist('chaves')

    dados = {
        'titulo': titulo[:160], 'mensagem': mensagem,
        'publico': publico, 'publico_params': json.dumps(params, ensure_ascii=False),
        'frequencia': frequencia,
        'horario': (request.form.get('horario') or '10:30')[:5],
        'horario_2': (request.form.get('horario_2') or '').strip()[:5] or None,
        'dia_semana': request.form.get('dia_semana', type=int),
        'data_inicio': (request.form.get('data_inicio') or '').strip() or None,
        'data_fim': (request.form.get('data_fim') or '').strip() or None,
        'so_mes_vigente': 1 if request.form.get('so_mes_vigente') else 0,
        'ativo': 1 if request.form.get('ativo') else 0,
    }

    imagem = request.files.get('imagem')
    tem_imagem = bool(imagem and imagem.filename
                      and (imagem.mimetype or '').startswith('image/'))
    conteudo = imagem.read() if tem_imagem else None
    if conteudo and len(conteudo) > 10 * 1024 * 1024:
        flash('Imagem maior que 10 MB.', 'warning')
        return voltar('avisos.index')

    try:
        if aviso_id:
            atual = carregar_aviso(aviso_id, cidade)
            if not atual:
                abort(404)
            sets = ', '.join(f'{k} = :{k}' for k in dados)
            p = dict(dados); p['i'] = aviso_id
            db.session.execute(sa_text(f'UPDATE avisos SET {sets} WHERE id = :i'), p)
            if conteudo:
                db.session.execute(sa_text(
                    'UPDATE avisos SET imagem_blob = :b, imagem_mime = :m, '
                    'imagem_nome = :n WHERE id = :i'),
                    {'b': conteudo, 'm': imagem.mimetype,
                     'n': (imagem.filename or '')[:200], 'i': aviso_id})
            codigo = atual['codigo']
        else:
            codigo = gerar_codigo()
            p = dict(dados)
            p.update({'codigo': codigo, 'cidade': cidade, 'criado_em': datetime.now(),
                      'criado_por': current_user.email,
                      'imagem_blob': conteudo,
                      'imagem_mime': imagem.mimetype if conteudo else None,
                      'imagem_nome': (imagem.filename or '')[:200] if conteudo else None})
            colunas = ', '.join(p.keys())
            valores = ', '.join(f':{k}' for k in p)
            db.session.execute(sa_text(
                f'INSERT INTO avisos ({colunas}) VALUES ({valores})'), p)
        db.session.commit()
        flash(f'Aviso {codigo} salvo.', 'success')
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error('Falha ao salvar aviso: %s', exc, exc_info=True)
        flash(f'Nao consegui salvar: {exc}', 'danger')
    return voltar('avisos.index')


@avisos_bp.route('/<int:aviso_id>/alternar', methods=['POST'])
@login_required
def alternar(aviso_id):
    _admin_required()
    aviso = carregar_aviso(aviso_id, current_user.cidade_ativa)
    if not aviso:
        abort(404)
    novo = 0 if aviso['ativo'] else 1
    db.session.execute(sa_text('UPDATE avisos SET ativo = :a WHERE id = :i'),
                       {'a': novo, 'i': aviso_id})
    db.session.commit()
    flash(f"Aviso {aviso['codigo']} {'ativado' if novo else 'pausado'}.", 'info')
    return voltar('avisos.index')


@avisos_bp.route('/<int:aviso_id>/apagar', methods=['POST'])
@login_required
def apagar(aviso_id):
    _admin_required()
    aviso = carregar_aviso(aviso_id, current_user.cidade_ativa)
    if not aviso:
        abort(404)
    db.session.execute(sa_text('DELETE FROM avisos_envios WHERE aviso_id = :i'), {'i': aviso_id})
    db.session.execute(sa_text('DELETE FROM avisos WHERE id = :i'), {'i': aviso_id})
    db.session.commit()
    flash(f"Aviso {aviso['codigo']} apagado.", 'info')
    return voltar('avisos.index')


@avisos_bp.route('/<int:aviso_id>/previa')
@login_required
def previa(aviso_id):
    """Mostra exatamente o que vai ser enviado e para quem."""
    _admin_required()
    cidade = current_user.cidade_ativa
    aviso = carregar_aviso(aviso_id, cidade)
    if not aviso:
        abort(404)
    pessoas = destinatarios(cidade, aviso['publico'], aviso['publico_params'])
    com_telefone = [p for p in pessoas if p['telefone']]
    sem_telefone = [p for p in pessoas if not p['telefone']]
    exemplo = montar_mensagem(aviso, com_telefone[0] if com_telefone else
                              {'nome': 'Fulano de Tal'}, cidade)
    return render_template(
        'avisos/previa.html', aviso=aviso, exemplo=exemplo,
        com_telefone=com_telefone, sem_telefone=sem_telefone,
        publicos=PUBLICOS, telefone_bonito=telefone_bonito, cidade=cidade,
    )


@avisos_bp.route('/<int:aviso_id>/imagem')
@login_required
def imagem(aviso_id):
    _admin_required()
    from flask import Response
    linha = db.session.execute(sa_text(
        'SELECT imagem_blob, imagem_mime FROM avisos WHERE id = :i AND cidade = :c'),
        {'i': aviso_id, 'c': current_user.cidade_ativa}).fetchone()
    if not linha or not linha[0]:
        abort(404)
    return Response(linha[0], mimetype=linha[1] or 'image/png')


@avisos_bp.route('/telefone', methods=['POST'])
@login_required
def gravar_telefone():
    """Completa o telefone de um atleta direto da tela de avisos."""
    _admin_required()
    chave = (request.form.get('chave') or '').strip()
    bruto = request.form.get('telefone') or ''
    numero = normalizar_telefone(bruto)
    if not chave or not numero:
        flash('Numero invalido. Use DDD + numero, por exemplo (DD) 9XXXX-XXXX.', 'warning')
        return redirect(request.referrer or url_for('avisos.index'))
    dono = Atleta.query.filter_by(telefone_normalizado='+' + numero).first() \
        or Atleta.query.filter_by(telefone_normalizado=numero).first()
    if dono and str(dono.strava_id) != chave:
        flash(f'Esse numero ja e do atleta {dono.nome_atual}.', 'warning')
        return redirect(request.referrer or url_for('avisos.index'))
    atleta = Atleta.query.filter_by(strava_id=chave).first()
    if not atleta:
        abort(404)
    atleta.telefone_normalizado = '+' + numero
    db.session.commit()
    flash(f'Telefone de {atleta.nome_atual} salvo: {telefone_bonito(numero)}', 'success')
    return redirect(request.referrer or url_for('avisos.index'))


@avisos_bp.route('/local', methods=['POST'])
@login_required
def salvar_local():
    """Onde a medalha e retirada (entra nos marcadores da mensagem)."""
    _admin_required()
    ensure_schema()
    cidade = current_user.cidade_ativa
    agora = datetime.now()

    # O MÊS VEM DA TELA (P-54, 13/08). Antes esta rota gravava uma linha por
    # cidade — e depois da migração para (cidade, ano, mes) ela passaria a
    # falhar, porque `ano`/`mes` são NOT NULL. Sem o seletor, o admin também
    # não tinha como saber para qual mês estava cadastrando: foi assim que o
    # endereço de julho acabou salvo em agosto e a cobrança saiu sem local.
    ano = request.form.get('ano', type=int) or agora.year
    mes = request.form.get('mes', type=int) or agora.month
    if not 1 <= mes <= 12:
        mes = agora.month

    dados = {
        'c': cidade, 'a': ano, 'mes': mes,
        'e': (request.form.get('estabelecimento') or '').strip()[:160],
        'end': (request.form.get('endereco') or '').strip()[:300],
        'm': (request.form.get('maps_url') or '').strip()[:500],
        'h': (request.form.get('horario_funcionamento') or '').strip()[:160],
        'em': agora,
    }
    # INSERT OR REPLACE respeita o UNIQUE (cidade, ano, mes): salvar de novo o
    # mesmo mês corrige, não duplica.
    db.session.execute(sa_text("""
        INSERT OR REPLACE INTO local_retirada
        (cidade, ano, mes, estabelecimento, endereco, maps_url,
         horario_funcionamento, atualizado_em)
        VALUES (:c, :a, :mes, :e, :end, :m, :h, :em)
    """), dados)
    db.session.commit()
    from app.blueprints.ranking import MESES_PT
    flash('Local de %s/%s salvo. Ele vale só para esse mês — os outros seguem '
          'com o endereço da ficha do patrocinador.'
          % (MESES_PT.get(mes, mes), ano), 'success')
    return voltar('avisos.index', ano=ano, mes=mes)


@avisos_bp.route('/medalha/<int:ano>/<int:mes>/<chave>', methods=['POST'])
@login_required
def marcar_medalha(ano, mes, chave):
    """Marca/desmarca que o atleta ja retirou a medalha do mes."""
    _admin_required()
    ensure_schema()
    cidade = current_user.cidade_ativa
    ja = db.session.execute(sa_text(
        "SELECT 1 FROM medalhas_retiradas WHERE cidade=:c AND ano=:a AND mes=:m "
        "AND chave_atleta=:k"), {'c': cidade, 'a': ano, 'm': mes, 'k': chave}).fetchone()
    if ja:
        db.session.execute(sa_text(
            "DELETE FROM medalhas_retiradas WHERE cidade=:c AND ano=:a AND mes=:m "
            "AND chave_atleta=:k"), {'c': cidade, 'a': ano, 'm': mes, 'k': chave})
        flash('Marcado como NAO retirada.', 'info')
    else:
        db.session.execute(sa_text("""
            INSERT OR REPLACE INTO medalhas_retiradas
            (cidade, ano, mes, chave_atleta, retirado_em, marcado_por)
            VALUES (:c, :a, :m, :k, :em, :por)
        """), {'c': cidade, 'a': ano, 'm': mes, 'k': chave,
               'em': datetime.now(), 'por': current_user.email})
        flash('Marcado como retirada — nao recebe mais cobranca.', 'success')
    db.session.commit()
    return redirect(request.referrer or url_for('avisos.index'))
