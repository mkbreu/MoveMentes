# app/blueprints/ranking.py
# -*- coding: utf-8 -*-
"""
Ranking do MoveMentes.
- Texto para WhatsApp com/sem flags
- HTML com links para dashboards
- Link publico assinado + encurtador interno
- Snapshot mensal de patrocinador (nome + logo)
"""
from flask import (
    Blueprint, render_template, flash, redirect, url_for, jsonify,
    request, current_app, abort, Response, send_file,
)
from flask_login import login_required, current_user
from app.navegacao import voltar
from app import db, signing_keys
from app.models import (
    Activity, Config, MonthlyBackup, ShortLink,
    FlagAnticheat, Atleta, PatrocinadorMensal,
    Feedback, PrestacaoContas, ComprovantePrestacao,
)
from app.blueprints.auth import admin_required, viewer_required
from datetime import datetime, timedelta, date as _date
from sqlalchemy import extract, func, inspect, text
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from werkzeug.utils import secure_filename
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
import base64
import hashlib
import mimetypes
import html as html_mod
import json
import logging
import locale
import os
import re
import secrets

logger = logging.getLogger(__name__)

try:
    locale.setlocale(locale.LC_TIME, 'pt_BR.UTF-8')
except locale.Error:
    try:
        locale.setlocale(locale.LC_TIME, 'pt_BR')
    except locale.Error:
        pass

MESES_PT = {
    1: 'Janeiro', 2: 'Fevereiro', 3: 'Marco', 4: 'Abril',
    5: 'Maio', 6: 'Junho', 7: 'Julho', 8: 'Agosto',
    9: 'Setembro', 10: 'Outubro', 11: 'Novembro', 12: 'Dezembro'
}

EMOJI_NUMEROS = [
    '', '1\ufe0f\u20e3', '2\ufe0f\u20e3', '3\ufe0f\u20e3', '4\ufe0f\u20e3',
    '5\ufe0f\u20e3', '6\ufe0f\u20e3', '7\ufe0f\u20e3', '8\ufe0f\u20e3',
    '9\ufe0f\u20e3', '\U0001f51f',
]

# Links publicos sao permanentes por (cidade, ano, mes): o mesmo link do mes
# pode ser fixado no grupo sem expirar nem mudar a cada geracao de ranking.
_SHORT_LINK_TTL_DAYS = 3650
_SPONSOR_MAX_SIZE_BYTES = 10 * 1024 * 1024
_SCHEMA_READY = False
_RANKING_LINKS_SALT = 'ranking-public-links-v1'
_RANKING_LINKS_MAX_AGE_SECONDS = None  # None = token assinado sem expiracao
_SLUG_RE = re.compile(r'^[A-Za-z0-9]{4,32}$')
# O ENDERECO NAO MORA MAIS AQUI (P-102, 28/08/2026). Quem sabe e'
# `app/enderecos.py`, e dentro de um pedido ele devolve o dominio que a pessoa
# de fato usou -- e' o que faz o dominio velho e o novo funcionarem ao mesmo
# tempo, sem instante critico na virada. Continua sendo `BASE_URL` no resto do
# arquivo, agora como funcao, para nao mexer nos seis pontos que a usam.
def BASE_URL_ATUAL():
    from app.enderecos import base_url
    return base_url()


def get_month_name(month_num):
    return MESES_PT.get(month_num, str(month_num))


def pos_emoji(n):
    """Posicao no ranking: 1º, 2º, 15º...

    ERA emoji de numero (1️⃣2️⃣3️⃣). Trocado em 31/07/2026 porque no WhatsApp
    cada emoji desses ocupa a largura de dois caracteres e, da 10ª posicao em
    diante, viravam dois emojis por atleta — a linha estourava a largura da
    tela e quebrava no meio do nome. Com "15º" cabe um atleta por linha, que
    e como a lista deve ser lida.
    """
    try:
        return f'{int(n)}º'
    except (TypeError, ValueError):
        return str(n or '')


def _as_bool(value):
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _append_query_param(raw_url, key, value):
    parsed = urlparse(raw_url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    params[key] = str(value)
    return urlunparse(parsed._replace(query=urlencode(params)))


def _slugify_city(cidade):
    # Translitera acentos (ó->o, ç->c) para um endereco limpo: "Palmeirópolis"
    # vira "palmeiropolis", "Minaçu" vira "minacu". Usa unicodedata (stdlib):
    # zero dependencia e zero manutencao. Como o slug e sempre recalculado a
    # partir do nome (nunca fica gravado), trocar a regra mantem tudo coerente.
    import unicodedata
    texto = unicodedata.normalize('NFKD', (cidade or '').strip())
    texto = texto.encode('ascii', 'ignore').decode('ascii')
    base = re.sub(r'[^A-Za-z0-9]+', '-', texto).strip('-').lower()
    return base or 'cidade'


def _normalizar_nome_atleta(nome):
    return re.sub(r'\s+', ' ', (nome or '').strip()).lower()


# Tipo sanguineo que o pessoal mais experiente poe no fim do nome do Strava
# (Gabi, Alex...). Nao e sobrenome e nao pode ir para o ranking.
_TIPO_SANGUINEO_RE = re.compile(
    r'^(?:a|b|ab|o)\s*(?:\+|-|positivo|negativo|pos|neg)$', re.IGNORECASE)
# Emojis de tipo sanguineo (🅰️ 🅱️ 🆎 🅾️) e qualquer pedaco que so tenha
# simbolo/emoji — nome de gente tem letra.
_SEM_LETRA_RE = re.compile(r'^[^0-9A-Za-zÀ-ÿ]+$')


def _e_ruido_de_nome(parte):
    """A parte do nome e tipo sanguineo, emoji ou enfeite?"""
    limpo = parte.strip()
    if not limpo:
        return True
    if _TIPO_SANGUINEO_RE.match(limpo):
        return True
    if _SEM_LETRA_RE.match(limpo):
        return True
    # 'A+' colado, '🅰️', 'O-' — sobra 1-2 caracteres sem valor de sobrenome.
    sem_simbolo = re.sub(r'[^0-9A-Za-zÀ-ÿ]', '', limpo)
    if not sem_simbolo:
        return True
    if len(sem_simbolo) <= 2 and _TIPO_SANGUINEO_RE.match(sem_simbolo):
        return True
    return False


def _nome_curto_atleta(nome):
    """Nome que aparece no ranking: primeiro nome + sobrenome MAIS CURTO.

    POR QUE NAO E O ULTIMO SOBRENOME: quem usa Strava ha mais tempo costuma
    colocar o tipo sanguineo no fim do nome do perfil ("Gabi Ferreira Souza
    O+", "Maykon Breu 🅰️"). Pegando o ultimo, o ranking mostrava "Gabi O+".

    O sobrenome mais curto tambem deixa a linha limpa: no WhatsApp cada linha
    que estoura a largura da tela quebra no meio do nome, e o ranking vira
    uma bagunca.
    """
    limpo = re.sub(r'\s+', ' ', (nome or '').strip())
    if not limpo:
        return 'Desconhecido'
    # Tipo sanguineo escrito em DUAS palavras no fim ("Ana Beatriz O Positivo"):
    # so cai fora quando vem no fim e com o sinal/palavra junto, para nunca
    # comer um nome de verdade.
    limpo = re.sub(r'\s+(?:AB|A|B|O)\s*(?:\+|-|positivo|negativo|pos|neg)\s*$',
                   '', limpo, flags=re.IGNORECASE).strip()
    if not limpo:
        return 'Desconhecido'
    partes = [p for p in limpo.split(' ') if not _e_ruido_de_nome(p)]
    if not partes:
        return 'Desconhecido'
    # "@Alex" vira "Alex": o arroba vem do apelido no Strava e no ranking so
    # atrapalha (no WhatsApp da a impressao de que a pessoa foi marcada).
    partes[0] = partes[0].lstrip('@').strip() or partes[0]
    if len(partes) == 1:
        return partes[0]
    primeiro = partes[0]
    sobrenomes = partes[1:]
    # Preposicao nao e sobrenome ("de", "da", "dos"...), e sobrenome de UMA
    # letra tambem nao — quase sempre e inicial ou resto de tipo sanguineo.
    candidatos = [s for s in sobrenomes
                  if s.lower() not in ('de', 'da', 'do', 'das', 'dos', 'e')
                  and len(re.sub(r'[^0-9A-Za-zÀ-ÿ]', '', s)) > 1]
    if not candidatos:
        candidatos = sobrenomes
    # Empate de tamanho fica com o ULTIMO: em "Maria das Dores Silva",
    # "Dores" e "Silva" tem 5 letras, mas o nome de familia e o do fim.
    menor = min(reversed(candidatos), key=len)
    return f'{primeiro} {menor}'


def _nomes_escolhidos():
    """{strava_id: nome escolhido} -- o que o admin ou o proprio atleta pediu.

    UMA consulta para o ranking inteiro. Vazio quando ninguem escolheu nada,
    e ai tudo segue como antes.
    """
    try:
        from app.models import Atleta
        return {
            str(sid): (nome or '').strip()
            for sid, nome in db.session.query(
                Atleta.strava_id, Atleta.nome_exibicao).filter(
                    Atleta.nome_exibicao.isnot(None),
                    Atleta.nome_exibicao != '').all()
            if (nome or '').strip()
        }
    except Exception as exc:
        logger.debug('Sem nomes escolhidos: %s', exc)
        return {}


def _desambiguar_nomes(lst):
    """Duas pessoas, o mesmo nome na tela: da um sobrenome diferente a cada.

    POR QUE (regra do dono, 29/08/2026). Maria Silva existe as duzias, e no
    ranking do grupo duas linhas 'Maria Silva' com km diferentes nao dizem a
    ninguem quem e quem. `_nome_curto_atleta` escolhe o sobrenome mais
    curto -- otimo para a linha do WhatsApp nao quebrar, mas nada impede que
    duas pessoas caiam no mesmo.

    Aqui so mexe em QUEM COLIDIU, e so na exibicao: ninguem e fundido, e o
    strava_id continua sendo a identidade (mesmo criterio do
    `consolidado.py`, que conta nome repetido e nunca une).

    A ordem de busca e a que o dono pediu: o proximo sobrenome; se nao
    houver proximo, um ANTERIOR que nao seja o primeiro nome. Particula
    ('de', 'da', 'e') nunca entra.
    """
    from app.nomes import e_particula, partes_uteis

    porNome = {}
    for item in lst:
        porNome.setdefault(item['nome'].lower(), []).append(item)

    for repetidos in porNome.values():
        if len(repetidos) < 2:
            continue
        usados = set(x['nome'].lower() for x in lst)
        for item in repetidos[1:]:      # o primeiro fica como esta
            # NOME ESCOLHIDO POR GENTE NAO SE MEXE (29/08/2026). Se duas
            # pessoas escolheram o mesmo nome, quem resolve e o admin, nao
            # um palpite do codigo.
            if item.get('nome_escolhido'):
                continue
            partes = partes_uteis(item.get('nome_completo') or item['nome'])
            if len(partes) < 2:
                continue
            primeiro = partes[0]
            atual = item['nome'].split(' ')[-1].lower()
            # Candidatos: todo sobrenome util que ainda nao esta em uso.
            # `partes[1:]` ja exclui o primeiro nome; a ordem natural da
            # lista atende 'o proximo, senao um anterior'.
            for cand in partes[1:]:
                if e_particula(cand) or cand.lower() == atual:
                    continue
                novo = '%s %s' % (primeiro, cand)
                if novo.lower() in usados:
                    continue
                item['nome'] = novo
                usados.add(novo.lower())
                break
    return lst


def _format_km_total(valor):
    return f'{float(valor or 0):.1f}'


def _strava_id_valido(strava_id):
    sid = str(strava_id or '').strip()
    return bool(sid and sid != '0' and sid.isdigit())


def _dashboard_href(atleta):
    sid = str(atleta.get('strava_id') or '0')
    if _strava_id_valido(sid):
        return f'{BASE_URL_ATUAL()}/ath/{sid}'
    name_key = atleta.get('name_key') or _normalizar_nome_atleta(atleta.get('nome'))
    query = urlencode({'name_key': name_key})
    return f'{BASE_URL_ATUAL()}/ath/0?{query}'


# ============================================================================
# PATROCINADORES POR (CIDADE, ANO, MES)
#
# REGRA DE OURO: nada de um mes aparece em outro, e nada de uma cidade
# aparece em outra. Cada periodo tem a PROPRIA copia do patrocinador -- nome
# interno, nome de exibicao, logo, bordao, legenda e telefone. Trocar o
# patrocinador hoje nao mexe em nenhum mes ja publicado.
#
# NAO EXISTE fallback para "o valor corrente". Mes sem patrocinador cadastrado
# aparece vazio ("A definir"), nunca com o patrocinador de hoje. Era esse
# fallback que carimbava o patrocinador de julho no ranking de junho.
# ============================================================================

SEM_PATROCINADOR = 'A definir'


def _sponsor_principal(cidade, year, month):
    """O Patrocinador do Mes daquele periodo — ou None. Sem fallback."""
    return PatrocinadorMensal.query.filter_by(
        cidade=cidade, ano=year, mes=month, categoria='principal'
    ).order_by(PatrocinadorMensal.ordem.asc(), PatrocinadorMensal.id.asc()).first()


def _sponsor_nome_periodo(cidade, year, month):
    """Nome de EXIBICAO do patrocinador do periodo (o que o publico ve)."""
    sp = _sponsor_principal(cidade, year, month)
    return sp.exibicao if sp else SEM_PATROCINADOR


def _patrocinadores_adicionais(cidade, year, month):
    """Apoios do periodo (tudo que nao e o principal)."""
    return PatrocinadorMensal.query.filter(
        PatrocinadorMensal.cidade == cidade,
        PatrocinadorMensal.ano == year,
        PatrocinadorMensal.mes == month,
        db.or_(PatrocinadorMensal.categoria.is_(None),
               PatrocinadorMensal.categoria != 'principal'),
    ).order_by(PatrocinadorMensal.ordem.asc(), PatrocinadorMensal.id.asc()).all()


def _patrocinadores_periodo(cidade, year, month):
    """Todos os patrocinadores do periodo, principal primeiro."""
    principal = _sponsor_principal(cidade, year, month)
    itens = [principal] if principal else []
    return itens + _patrocinadores_adicionais(cidade, year, month)


# Marco zero do rodizio. Fixo de proposito: mudar isto reembaralha quem
# aparece, e nao ha motivo para reembaralhar.
_EPOCA_RODIZIO = _date(2026, 1, 1)


def numero_da_rodada(quando=None):
    """Qual rodada de publicacao e' esta. Duas por dia: manha (0) e tarde (1).

    E' o unico "relogio" do rodizio, e ele nao guarda nada -- por isso a
    escolha do apoio da vez e' a mesma toda vez que for perguntada.
    """
    quando = quando or datetime.now()
    dias = (quando.date() - _EPOCA_RODIZIO).days
    return dias * 2 + (0 if quando.hour < 12 else 1)


# QUANTAS VEZES POR SEMANA cada categoria de `peso_frequencia` (0-5, mesma
# coluna do banco de sempre) representa (07/09/2026, redivisao pedida pelo
# dono). O ranking sai 2x/dia, 7 dias/semana = 14 vagas por semana; nao e'
# linear de proposito -- quem oferece o melhor patrocinio ganha
# desproporcionalmente mais espaco. 0 continua fora do rodizio.
MAPA_VEZES_SEMANA = {0: 0, 1: 3, 2: 5, 3: 7, 4: 10, 5: 14}


def _vezes_semana(sponsor):
    """Quantas vagas de 14 este patrocinador reivindica por semana."""
    bruto = getattr(sponsor, 'peso_frequencia', None)
    try:
        peso = 3 if bruto is None else int(bruto)
    except (TypeError, ValueError):
        peso = 3
    peso = max(0, min(peso, 5))
    return MAPA_VEZES_SEMANA.get(peso, 7)


def _posicoes_uniformes(k):
    """`k` posicoes espalhadas o mais uniformemente possivel entre 0 e 13.

    Ex.: k=3 -> {0,5,9} (a cada ~2,5 dias); k=7 -> {0,2,4,6,8,10,12} (uma
    vez por dia, exato). E' o que garante "dias alternados e distantes"
    para quem tem a categoria mais baixa, em vez de uma media de longo prazo.
    """
    if k <= 0:
        return []
    k = min(k, 14)
    return sorted({round(i * 14 / k) % 14 for i in range(k)})


def _slot_livre_mais_proximo(grade, p):
    """Vaga livre mais perto de `p` na grade circular de 14, ou None se lotou."""
    for d in range(1, 14):
        for cand in (p - d, p + d):
            cand %= 14
            if grade[cand] is None:
                return cand
    return None


def _grade_semanal(cidade, year, month):
    """Cronograma fixo de 14 vagas (2 rodadas/dia x 7 dias) para o rodape.

    Cada apoio reivindica sua contagem semanal (`_vezes_semana`) espalhada
    pela grade; colisao pega a vaga livre mais proxima. O PRINCIPAL e'
    processado por ULTIMO e absorve toda vaga que sobrar -- nao ha nenhum
    "se ninguem escalado, usa o principal" em separado: e' o mesmo mecanismo
    de fila ponderada de sempre, so' que numa grade fixa de 14 em vez de
    infinita (rotacao infinita so' garante a MEDIA de longo prazo; a grade
    fixa garante a contagem semanal, que foi o que o dono pediu).
    """
    candidatos = _patrocinadores_periodo(cidade, year, month)
    candidatos = sorted(candidatos, key=lambda c: (
        1 if getattr(c, 'is_principal', False) else 0,
        -_vezes_semana(c),
        getattr(c, 'ordem', 0) or 0,
        c.id,
    ))
    grade = [None] * 14
    for c in candidatos:
        k = _vezes_semana(c)
        if k <= 0:
            continue
        for p in _posicoes_uniformes(k):
            alvo = p if grade[p] is None else _slot_livre_mais_proximo(grade, p)
            if alvo is not None:
                grade[alvo] = c

    # O PRINCIPAL ABSORVE TODA VAGA QUE SOBRAR (06/10/2026). Era o desenho
    # (wsgi.py de 07/09 e o texto da tela "Com que frequencia aparece"), mas o
    # codigo so dava a ele as vagas do PROPRIO peso: no padrao (peso 3 = 7x)
    # sobravam ate 7 vagas vazias por semana, e nessas rodadas o ranking saia
    # SEM logo -- foi o que aconteceu as 15h30 de 05 e 06/10. Peso 0 ("nao
    # aparecer por ora") continua de fora: nao ocupa nem a sobra.
    principal = next((c for c in candidatos if getattr(c, 'is_principal', False)), None)
    if principal is not None and _vezes_semana(principal) > 0:
        grade = [c if c is not None else principal for c in grade]
    return grade


def apoio_da_vez(cidade, year, month, quando=None):
    """Quem entra no BLOCO FINAL desta rodada, ou None se nao ha ninguem.

    O PRINCIPAL TAMBEM REVEZA AQUI (07/09/2026, pedido do dono). Ele ja tem
    o cabecalho garantido em toda rodada ("Patrocinador do Mes: X"), entao
    no rodape ele divide a vaga com os apoios -- e' processado por ultimo em
    `_grade_semanal`, entao absorve o que sobrar (normalmente a maior parte).
    Antes ele saia nos dois lugares sempre, e o apoio da vez virava um
    acompanhante.

    Tambem e' o UNICO lugar de onde sai a logo/imagem do patrocinador (ver
    `api._sponsors_payload`) -- texto e logo sempre saem do mesmo sorteio,
    nunca separados.

    Nao grava nada e nao depende de nada gravado: a grade e' recalculada do
    cadastro atual a cada chamada.
    """
    grade = _grade_semanal(cidade, year, month)
    if not any(grade):
        return None
    return grade[numero_da_rodada(quando) % 14]


def _bloco_patrocinadores_linhas(cidade, year, month, principal_nome=None, escape=None):
    """Bloco final (apos a contagem de medalhas) com TODOS os patrocinadores.

    Usa sempre o NOME DE EXIBICAO do periodo. Nunca altera o formato
    consolidado acima — apenas acrescenta ao final. Retorna [] quando o mes
    nao tem patrocinador nenhum.
    """
    esc = escape or (lambda valor: valor)
    linhas = []
    vistos = set()

    def _add(nome, bordao):
        nome = (nome or '').strip()
        if not nome or nome.lower() == SEM_PATROCINADOR.lower():
            return
        chave = nome.lower()
        if chave in vistos:
            return
        vistos.add(chave)
        texto = '• ' + esc(nome)
        bord = (bordao or '').strip()
        if bord:
            texto += ' — ' + esc(bord)
        linhas.append(texto)

    # UM POR RANKING, INCLUSIVE O PRINCIPAL (06 e 07/09/2026).
    # Ate 06/09 saiam TODOS os patrocinadores juntos, em toda rodada -- com
    # o cadastro crescendo isso vira lista que ninguem le, e tira do
    # apoiador justamente o que ele quer, que e' ser notado.
    # O principal continua no CABECALHO toda vez; aqui no rodape ele reveza
    # com os apoios, cada um na frequencia que merece (`peso_frequencia`).
    da_vez = apoio_da_vez(cidade, year, month)
    if da_vez:
        _add(da_vez.exibicao, da_vez.bordao)
    # principal_nome so entra se o periodo nao tiver registro proprio: e o
    # caso de instalacoes antigas, antes da migracao rodar.
    if not linhas:
        _add(principal_nome, None)

    if not linhas:
        return []
    return ['', '\U0001f91d *Nossos patrocinadores*'] + linhas


def _feedback_public_url(cidade):
    """URL publica permanente da pagina de feedback da cidade."""
    return f'{BASE_URL_ATUAL()}/ranking/feedback/{_slugify_city(cidade)}'


def _transparencia_public_url(cidade):
    """URL publica permanente da pagina de transparencia da cidade."""
    return f'{BASE_URL_ATUAL()}/ranking/transparencia/{_slugify_city(cidade)}'


def _conferencia_public_url(cidade):
    """URL publica da pagina onde o atleta confere os proprios links.

    Ela existe desde sempre e NAO era divulgada em lugar nenhum (achado
    pelo dono em 06/09/2026). E justamente a pagina que resolve sozinha a
    duvida mais comum do grupo -- "meu treino entrou?" -- sem ninguem
    precisar abrir o painel de admin.
    """
    return f'{BASE_URL_ATUAL()}/conferencia/{_slugify_city(cidade)}'


def _bloco_rodape_links(cidade, escape=None):
    """Rodape do texto do ranking: link de feedback + link de transparencia.

    Fica APOS o bloco de patrocinadores. Nunca altera o formato consolidado
    acima.
    """
    fb_url = _feedback_public_url(cidade)
    tr_url = _transparencia_public_url(cidade)
    cf_url = _conferencia_public_url(cidade)
    linhas = ['', '\U0001f4e3 *Sua opiniao importa!*']
    if escape:
        fb = escape(fb_url)
        tr = escape(tr_url)
        cf = escape(cf_url)
        linhas.append(
            f'Sugestoes, elogios ou pedido de revisao: <a href="{fb}">{fb}</a>'
        )
        linhas.append(
            f'\U0001f50e Seu treino nao entrou? confira aqui: <a href="{cf}">{cf}</a>'
        )
        linhas.append(
            f'\U0001f4b0 Transparencia (prestacao de contas): <a href="{tr}">{tr}</a>'
        )
    else:
        linhas.append('Sugestoes, elogios ou pedido de revisao:')
        linhas.append(fb_url)
        # A CONFERENCIA ENTRA AQUI (06/09/2026): e o link que responde
        # sozinho a pergunta mais repetida do grupo, "meu treino entrou?".
        linhas.append('\U0001f50e Seu treino nao entrou? confira aqui:')
        linhas.append(cf_url)
        linhas.append('\U0001f4b0 Transparencia (prestacao de contas):')
        linhas.append(tr_url)
    return linhas


def _nomes_patrocinadores(cidade, year, month, principal=None):
    """O nome do PATROCINADOR DO MES -- um so'.

    Ate 07/09/2026 esta funcao juntava TODOS os patrocinadores do periodo com
    ' | ', e o resultado ia para a linha "Patrocinador do Mes: *X*". Enquanto
    o mes teve um patrocinador so', ninguem viu; no dia em que entraram dois
    apoios, o cabecalho virou uma lista de tres nomes separados por barra.

    Patrocinador do Mes e' um so' -- e' o lugar de honra que o principal
    comprou. Os apoios aparecem no rodape, revezando (`apoio_da_vez`).
    """
    sp = _sponsor_principal(cidade, year, month)
    if sp and (sp.exibicao or '').strip():
        return sp.exibicao.strip()
    # Sem registro principal no periodo: instalacao antiga, antes da migracao.
    # Cai no primeiro do periodo, e so' entao no valor recebido de fora.
    for item in _patrocinadores_periodo(cidade, year, month):
        if (item.exibicao or '').strip():
            return item.exibicao.strip()
    if principal and str(principal).strip():
        return str(principal).strip()
    return SEM_PATROCINADOR


def _resolve_city_by_slug(city_slug):
    slug = (city_slug or '').strip().lower()
    if not slug:
        return None
    # CIDADE INATIVA NAO TEM PAGINA PUBLICA (19/09/2026, regra do dono — P-135).
    from app import consolidado as _cons
    for cidade in _cons.cidades_ativas():
        if _slugify_city(cidade) == slug:
            return cidade
    return None


def _ranking_links_serializer():
    # Lista de chaves (legada -> atual): assina com a atual e continua validando
    # os links permanentes gerados antes da troca de chave.
    return URLSafeTimedSerializer(
        signing_keys(),
        salt=_RANKING_LINKS_SALT,
    )


def _generate_ranking_links_token(cidade, year, month):
    payload = {
        'cidade': str(cidade),
        'ano': int(year),
        'mes': int(month),
    }
    return _ranking_links_serializer().dumps(payload)


def _project_root():
    return os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))


# --- LEGADO: logo em arquivo, uma por cidade -------------------------------
# Era assim que a logo principal era guardada: uploads/sponsors/<cidade>/logo.*
# — SEM mes nenhum no caminho. Trocar a logo reescrevia a de todos os meses
# anteriores. As funcoes abaixo sobrevivem SO para a migracao unica ler o
# arquivo antigo e transformar em blob do periodo. Nada novo grava aqui.

def _sponsor_dir_for_city(cidade):
    return os.path.join(_project_root(), 'uploads', 'sponsors', _slugify_city(cidade))


def _get_current_sponsor_file_info(cidade):
    folder = _sponsor_dir_for_city(cidade)
    if not os.path.isdir(folder):
        return None
    files = []
    for name in os.listdir(folder):
        if name.lower().startswith('logo.'):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                files.append(path)
    if not files:
        return None
    files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    selected = files[0]
    mime, _ = mimetypes.guess_type(selected)
    return {
        'path': selected,
        'filename': os.path.basename(selected),
        'mime': mime or 'application/octet-stream',
    }


def _load_current_sponsor_blob(cidade):
    info = _get_current_sponsor_file_info(cidade)
    if not info:
        return None, None, None
    try:
        with open(info['path'], 'rb') as f:
            return f.read(), info['mime'], info['filename']
    except OSError as exc:
        logger.warning('Falha ao ler logo do patrocinador em %s: %s', info['path'], exc)
        return None, None, None


_NOME_MIGRACAO_PATROCINADORES = 'patrocinadores_por_periodo'


def migrar_patrocinadores_para_periodo():
    """Migracao UNICA: patrocinador por CIDADE -> patrocinador por PERIODO.

    ⚠️ P-124 (28/08/2026): isto rodava a CADA BOOT, sincrono, e "idempotente"
    so' queria dizer "nao duplica" -- nao queria dizer "nao ressuscita". O
    passo 1 recria a partir do `monthly_backup` (congelado, nunca muda)
    QUALQUER linha que um admin tenha apagado de proposito pela tela: 8
    linhas erradas da Relojoaria 3R apagadas de manha voltaram com id novo
    duas horas depois, no boot seguinte. A fonte errada foi corrigida
    naquele dia, mas o COMPORTAMENTO -- rodar pra sempre -- continuava
    ressuscitando qualquer futura exclusao legitima.

    Por isso agora e' de verdade uma migracao de UMA VEZ SO: a marca fica em
    `migracoes_unicas`, e da segunda chamada em diante a funcao nao toca em
    nada. Se um dia o `MonthlyBackup` ganhar dado novo que precise entrar no
    catalogo, isso e' migracao nova, com nome novo -- nao reabrir esta.

    Tres passos, quando roda:

    1. Cada MonthlyBackup que ja tinha nome/logo congelados vira um
       PatrocinadorMensal 'principal' daquele mes. E o historico que existe.
    2. Config.patrocinador_mes + a logo em arquivo viram o principal do MES
       CORRENTE (so dele -- e o unico mes a que aquele valor realmente se
       referia).
    3. Cada nome distinto vira uma ficha no catalogo da cidade, para o admin
       reaproveitar sem redigitar.

    Devolve um resumo do que foi criado.
    """
    from app.models import PatrocinadorCatalogo

    resumo = {'principais_do_historico': 0, 'principal_mes_corrente': 0, 'catalogo': 0}

    ja_rodou = db.session.execute(text(
        'SELECT 1 FROM migracoes_unicas WHERE nome = :n'
    ), {'n': _NOME_MIGRACAO_PATROCINADORES}).first()
    if ja_rodou:
        return resumo

    agora = datetime.now()

    # ---- passo 1: historico congelado no MonthlyBackup ----
    criados = db.session.execute(text("""
        INSERT INTO patrocinadores_mensais
            (cidade, mes, ano, patrocinador_nome, categoria, ordem,
             patrocinador_logo_blob, patrocinador_logo_mime,
             patrocinador_logo_filename, criado_em)
        SELECT mb.cidade, mb.mes, mb.ano, TRIM(mb.patrocinador_nome_mes),
               'principal', 0,
               mb.patrocinador_logo_blob, mb.patrocinador_logo_mime,
               mb.patrocinador_logo_filename, :agora
        FROM monthly_backup mb
        WHERE mb.patrocinador_nome_mes IS NOT NULL
          AND TRIM(mb.patrocinador_nome_mes) <> ''
          AND LOWER(TRIM(mb.patrocinador_nome_mes)) <> 'a definir'
          AND NOT EXISTS (
              SELECT 1 FROM patrocinadores_mensais p
              WHERE p.cidade = mb.cidade AND p.ano = mb.ano AND p.mes = mb.mes
                AND p.categoria = 'principal')
    """), {'agora': agora})
    resumo['principais_do_historico'] = criados.rowcount or 0

    # ---- passo 2: o que estava "corrente" pertence ao mes corrente ----
    for config in Config.query.all():
        nome = (config.patrocinador_mes or '').strip()
        if not nome or nome.lower() == SEM_PATROCINADOR.lower():
            continue
        ja = PatrocinadorMensal.query.filter_by(
            cidade=config.cidade, ano=agora.year, mes=agora.month,
            categoria='principal').first()
        blob, mime, filename = _load_current_sponsor_blob(config.cidade)
        if ja:
            # Mes corrente ja tem principal: so completa a logo se faltar.
            if blob and not ja.patrocinador_logo_blob:
                ja.patrocinador_logo_blob = blob
                ja.patrocinador_logo_mime = mime
                ja.patrocinador_logo_filename = filename
            continue
        db.session.add(PatrocinadorMensal(
            cidade=config.cidade, ano=agora.year, mes=agora.month,
            patrocinador_nome=nome, categoria='principal', ordem=0,
            patrocinador_logo_blob=blob, patrocinador_logo_mime=mime,
            patrocinador_logo_filename=filename, criado_em=agora,
        ))
        resumo['principal_mes_corrente'] += 1

    db.session.commit()

    # ---- passo 3: catalogo a partir dos nomes que ja existem ----
    vistos = {
        (c.cidade, (c.nome_interno or '').strip().lower())
        for c in PatrocinadorCatalogo.query.all()
    }
    # A ficha do catalogo herda os dados do registro MAIS RECENTE daquele nome.
    for sp in PatrocinadorMensal.query.order_by(
            PatrocinadorMensal.ano.asc(), PatrocinadorMensal.mes.asc(),
            PatrocinadorMensal.id.asc()).all():
        nome = (sp.patrocinador_nome or '').strip()
        if not nome:
            continue
        chave = (sp.cidade, nome.lower())
        if chave in vistos:
            continue
        vistos.add(chave)
        db.session.add(PatrocinadorCatalogo(
            cidade=sp.cidade, nome_interno=nome,
            nome_exibicao=(sp.nome_exibicao or '').strip() or None,
            patrocinador_logo_blob=sp.patrocinador_logo_blob,
            patrocinador_logo_mime=sp.patrocinador_logo_mime,
            patrocinador_logo_filename=sp.patrocinador_logo_filename,
            telefone=sp.telefone, bordao=sp.bordao, legenda=sp.legenda,
            ativo=True, criado_em=agora,
        ))
        resumo['catalogo'] += 1

    db.session.execute(text(
        'INSERT INTO migracoes_unicas (nome, executada_em) VALUES (:n, :agora)'
    ), {'n': _NOME_MIGRACAO_PATROCINADORES, 'agora': agora})
    db.session.commit()

    if any(resumo.values()):
        logger.warning('Migracao de patrocinadores para periodo: %s', resumo)
    return resumo


def _ensure_ranking_schema():
    """
    Garante tabela short_links e novas colunas do snapshot mensal.
    Estratégia idempotente para ambientes sem migração automatizada.
    """
    with db.engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS short_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug VARCHAR(32) NOT NULL UNIQUE,
                cidade VARCHAR(100) NOT NULL,
                ano INTEGER NOT NULL,
                mes INTEGER NOT NULL,
                created_at DATETIME NOT NULL,
                expires_at DATETIME NOT NULL,
                active BOOLEAN NOT NULL DEFAULT 1
            )
        """))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_short_links_slug ON short_links(slug)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_short_links_exp ON short_links(expires_at)"))

        # Controle de parabens ja enviados: uma linha por atleta+modalidade+mes
        # que bateu a meta e ja foi parabenizado. Evita mandar parabens repetido
        # e permite a automacao (WAHA) marcar o que ja disparou.
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS parabens_enviados (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cidade VARCHAR(100) NOT NULL,
                ano INTEGER NOT NULL,
                mes INTEGER NOT NULL,
                chave_atleta VARCHAR(120) NOT NULL,
                modalidade VARCHAR(30) NOT NULL,
                enviado_em DATETIME NOT NULL,
                enviado_por VARCHAR(120),
                CONSTRAINT _parabens_uc UNIQUE (cidade, ano, mes, chave_atleta, modalidade)
            )
        """))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_parabens_periodo "
            "ON parabens_enviados(cidade, ano, mes)"
        ))

        insp = inspect(db.engine)
        table_names = set(insp.get_table_names())
        if 'monthly_backup' not in table_names:
            return

        def _run_ddl_idempotent(stmt):
            try:
                conn.execute(text(stmt))
            except Exception as exc:
                msg = str(exc).lower()
                if 'duplicate column name' in msg or 'already exists' in msg:
                    return
                raise

        _run_ddl_idempotent("ALTER TABLE monthly_backup ADD COLUMN patrocinador_nome_mes VARCHAR(200)")
        _run_ddl_idempotent("ALTER TABLE monthly_backup ADD COLUMN patrocinador_logo_blob BLOB")
        _run_ddl_idempotent("ALTER TABLE monthly_backup ADD COLUMN patrocinador_logo_mime VARCHAR(100)")
        _run_ddl_idempotent("ALTER TABLE monthly_backup ADD COLUMN patrocinador_logo_filename VARCHAR(255)")


def _ensure_schema_once():
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    try:
        _ensure_ranking_schema()
        _SCHEMA_READY = True
    except Exception as exc:
        logger.error('Erro ao garantir schema do ranking: %s', exc, exc_info=True)
        raise


def _active_short_link(cidade, year, month):
    # Sem filtro de validade: o slug do mes e reutilizado para sempre.
    return (
        ShortLink.query
        .filter_by(cidade=cidade, ano=year, mes=month, active=True)
        .order_by(ShortLink.created_at.desc())
        .first()
    )


def _new_short_slug(length=8):
    alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def _slug_deterministico(cidade, year, month):
    """Slug derivado de (cidade, ano, mes): sempre o mesmo, sem depender do banco.

    Os links curtos vao para o WhatsApp e vivem para sempre; a tabela
    short_links nao pode ser o unico lugar onde essa informacao existe. Como o
    slug e uma funcao pura do periodo, um link em circulacao volta a funcionar
    sozinho mesmo que a linha se perca (ver _reconstruir_short_link).
    """
    base = f'{cidade}|{int(year):04d}|{int(month):02d}'
    digest = hashlib.sha256(base.encode('utf-8')).digest()
    # Base32 usa apenas A-Z e 2-7: casa com _SLUG_RE e nao gera caractere ambiguo.
    return base64.b32encode(digest).decode('ascii').rstrip('=')[:10].lower()


def _periodos_candidatos():
    """(cidade, ano, mes) que podem ter link curto, para conferir contra um slug.

    Sao os meses com ranking salvo mais os ultimos 24 meses de cada cidade
    cadastrada -- algumas dezenas de combinacoes, conferidas por hash em
    microssegundos.
    """
    candidatos = set()
    try:
        for cidade, ano, mes in db.session.query(
            MonthlyBackup.cidade, MonthlyBackup.ano, MonthlyBackup.mes
        ).distinct():
            if cidade and ano and mes:
                candidatos.add((cidade, int(ano), int(mes)))
    except Exception as exc:
        logger.debug('Sem meses de MonthlyBackup para reconstruir slug: %s', exc)
    try:
        hoje = datetime.now()
        for (cidade,) in db.session.query(Config.cidade).all():
            if not cidade:
                continue
            ano, mes = hoje.year, hoje.month
            for _ in range(24):
                candidatos.add((cidade, ano, mes))
                mes -= 1
                if mes == 0:
                    mes = 12
                    ano -= 1
    except Exception as exc:
        logger.debug('Sem cidades para reconstruir slug: %s', exc)
    return candidatos


def _reconstruir_short_link(slug):
    """Recria a linha de um slug deterministico perdido (ex.: restauracao antiga).

    Devolve o ShortLink recriado ou None quando o slug nao corresponde a nenhum
    periodo conhecido (ai e link invalido mesmo).
    """
    alvo = (slug or '').strip().lower()
    if not alvo:
        return None
    for cidade, ano, mes in _periodos_candidatos():
        if _slug_deterministico(cidade, ano, mes) != alvo:
            continue
        try:
            registro = ShortLink(
                slug=alvo,
                cidade=cidade,
                ano=ano,
                mes=mes,
                created_at=datetime.utcnow(),
                expires_at=datetime.utcnow() + timedelta(days=_SHORT_LINK_TTL_DAYS),
                active=True,
            )
            db.session.add(registro)
            db.session.commit()
            logger.warning('Link curto %s reconstruido sozinho para %s %s/%s',
                           alvo, cidade, mes, ano)
            return registro
        except Exception as exc:
            db.session.rollback()
            # Outro worker pode ter recriado no mesmo instante: aproveitar o dele.
            logger.debug('Falha ao recriar link curto %s: %s', alvo, exc)
            return ShortLink.query.filter_by(slug=alvo, active=True).first()
    return None


def _get_or_create_short_slug(cidade, year, month):
    existing = _active_short_link(cidade, year, month)
    if existing:
        # Link ja distribuido: jamais trocar o slug de um periodo em circulacao.
        return existing.slug

    expires = datetime.utcnow() + timedelta(days=_SHORT_LINK_TTL_DAYS)
    # O slug deterministico vem primeiro; o aleatorio fica so como plano B para
    # o caso (praticamente impossivel) de o valor ja estar ocupado por outro
    # periodo. A linha no banco passa a ser um atalho, nao a fonte da verdade.
    candidatos = [_slug_deterministico(cidade, year, month)]
    candidatos += [_new_short_slug(8) for _ in range(20)]
    for slug in candidatos:
        ocupado = ShortLink.query.filter_by(slug=slug).first()
        if ocupado:
            if (ocupado.cidade, ocupado.ano, ocupado.mes) == (cidade, int(year), int(month)):
                return ocupado.slug
            continue
        record = ShortLink(
            slug=slug,
            cidade=cidade,
            ano=int(year),
            mes=int(month),
            created_at=datetime.utcnow(),
            expires_at=expires,
            active=True,
        )
        db.session.add(record)
        db.session.commit()
        return slug
    raise RuntimeError('Nao foi possivel gerar slug unico para link curto.')


def _build_public_ranking_url(cidade, year, month):
    slug = _get_or_create_short_slug(cidade, year, month)
    return url_for('ranking_short.redirect_short_link', slug=slug, _external=True)


def _list_backup_months(cidade):
    rows = (
        MonthlyBackup.query
        .with_entities(MonthlyBackup.ano, MonthlyBackup.mes)
        .filter_by(cidade=cidade)
        .distinct()
        .order_by(MonthlyBackup.ano.desc(), MonthlyBackup.mes.desc())
        .all()
    )
    return [(int(ano), int(mes)) for ano, mes in rows]


def _split_ranking_sections(ranking_data):
    corrida = ranking_data.get('corrida_caminhada', {}) if isinstance(ranking_data, dict) else {}
    ciclismo = ranking_data.get('ciclismo', {}) if isinstance(ranking_data, dict) else {}
    return [
        {
            'id': 'corrida_caminhada',
            'titulo': 'Corrida/Caminhada',
            'meta': corrida.get('meta'),
            'atingiram_meta': corrida.get('atingiram_meta', []),
            'nao_atingiram': corrida.get('nao_atingiram', []),
            'total_atletas': corrida.get('total_atletas', 0),
            'medalhistas': corrida.get('medalhistas', 0),
        },
        {
            'id': 'ciclismo',
            'titulo': 'Ciclismo',
            'meta': ciclismo.get('meta'),
            'atingiram_meta': ciclismo.get('atingiram_meta', []),
            'nao_atingiram': ciclismo.get('nao_atingiram', []),
            'total_atletas': ciclismo.get('total_atletas', 0),
            'medalhistas': ciclismo.get('medalhistas', 0),
        },
    ]


def _ranking_item_key(atleta):
    sid = str((atleta or {}).get('strava_id') or '0').strip()
    if _strava_id_valido(sid):
        return sid
    name_key = (atleta or {}).get('name_key') or _normalizar_nome_atleta((atleta or {}).get('nome'))
    return f'name:{name_key}'


def _activity_athlete_key(act, mapa_canonico=None):
    """Chave de agrupamento por atleta. `mapa_canonico` (opcional, vindo de
    `Atleta.mapa_strava_canonico()`) resolve atletas mesclados (07/09/2026)
    para o strava_id do cabeca do grupo ANTES de virar chave -- assim o
    ranking soma os dois strava_ids como uma posicao so, sem duplicidade."""
    sid = str(act.id_usuario_strava or '0').strip()
    if mapa_canonico:
        sid = mapa_canonico.get(sid, sid)
    if _strava_id_valido(sid):
        return sid
    return f'name:{_normalizar_nome_atleta(act.nome_usuario)}'


def _build_yearly_public_sections(cidade, year, selected_month=None,
                                  mes_ini=1, mes_fim=12):
    rejected_act_ids = {
        f.atividade_id
        for f in FlagAnticheat.query.filter_by(status='rejeitada').all()
    }
    # O RECORTE DE MESES (20/09/2026, P-137) é o que transforma esta mesma
    # soma em trimestre, semestre ou ano. Uma soma só para os quatro períodos:
    # duas somas seriam duas verdades sobre o mesmo km.
    activities = Activity.query.filter(
        Activity.cidade == cidade,
        extract('year', Activity.data_hora) == year,
        extract('month', Activity.data_hora) >= int(mes_ini),
        extract('month', Activity.data_hora) <= int(mes_fim),
    ).all()

    buckets = {
        'corrida_caminhada': {},
        'ciclismo': {},
    }

    mapa_canonico = Atleta.mapa_strava_canonico()

    for act in activities:
        if (act.id in rejected_act_ids
                or act.status == 'removida'
                or getattr(act, 'excluir_do_anual', False)):
            continue
        mod = (act.modalidade or '').lower()
        if mod not in buckets:
            continue

        key = _activity_athlete_key(act, mapa_canonico)
        sid = mapa_canonico.get(str(act.id_usuario_strava or '0').strip(),
                                 str(act.id_usuario_strava or '0').strip())
        rec = buckets[mod].setdefault(key, {
            'key': key,
            'nome': _nome_curto_atleta(act.nome_usuario),
            'strava_id': sid if _strava_id_valido(sid) else '0',
            'name_key': _normalizar_nome_atleta(act.nome_usuario),
            'km': 0.0,
            'km_mes': 0.0,
        })
        rec['nome'] = _nome_curto_atleta(act.nome_usuario)
        rec['km'] += act.distancia or 0
        if selected_month and act.data_hora and act.data_hora.month == selected_month:
            rec['km_mes'] += act.distancia or 0

    result = []
    lookup = {}
    labels = {
        'corrida_caminhada': 'Corrida/Caminhada',
        'ciclismo': 'Ciclismo',
    }
    for mod, data in buckets.items():
        atletas = list(data.values())
        atletas.sort(key=lambda item: item['km'], reverse=True)
        for idx, atleta in enumerate(atletas, start=1):
            atleta['rank'] = idx
            atleta['km'] = round(atleta['km'], 1)
            atleta['km_mes'] = round(atleta['km_mes'], 1)
        lookup[mod] = {item['key']: item for item in atletas}
        result.append({
            'id': mod,
            'titulo': labels[mod],
            'atletas': atletas,
            'total_atletas': len(atletas),
            'total_km': round(sum(item['km'] for item in atletas), 1),
        })
    return result, lookup


# ===================== RANKING POR PERÍODO (P-137) =====================
# Blocos FIXOS do calendário, como o dono definiu: trimestre é jan-mar,
# abr-jun, jul-set, out-dez; semestre é jan-jun e jul-dez. Não são "os últimos
# 3 meses" — em fevereiro o trimestre começa em janeiro, e em agosto o semestre
# começa em julho. A mesma regra que `app/consolidado.py` já usa na página
# pública desde 28/08/2026: uma regra só, nos dois lugares.
PERIODOS_RANKING = ('mes', 'trimestre', 'semestre', 'ano')
ROTULO_PERIODO = {'mes': 'Mensal', 'trimestre': 'Trimestral',
                  'semestre': 'Semestral', 'ano': 'Anual'}
_ORDINAL = {1: '1º', 2: '2º', 3: '3º', 4: '4º'}


def meses_do_periodo(periodo, quando=None):
    """(mes_inicio, mes_fim, nome) do bloco em que `quando` cai."""
    quando = quando or datetime.now()
    periodo = (periodo or 'ano').strip().lower()
    if periodo == 'mes':
        return quando.month, quando.month, 'de %02d/%d' % (quando.month, quando.year)
    if periodo == 'trimestre':
        bloco = (quando.month - 1) // 3
        return (bloco * 3 + 1, bloco * 3 + 3,
                'do %s TRIMESTRE de %d' % (_ORDINAL[bloco + 1], quando.year))
    if periodo == 'semestre':
        bloco = (quando.month - 1) // 6
        return (bloco * 6 + 1, bloco * 6 + 6,
                'do %s SEMESTRE de %d' % (_ORDINAL[bloco + 1], quando.year))
    return 1, 12, 'de %d' % quando.year


def build_periodo_ranking_data(cidade, periodo, quando=None):
    """Ranking de um período inteiro, na mesma estrutura do anual."""
    quando = quando or datetime.now()
    mes_ini, mes_fim, nome = meses_do_periodo(periodo, quando)
    sections, _ = _build_yearly_public_sections(
        cidade, quando.year, mes_ini=mes_ini, mes_fim=mes_fim)
    return {
        'ano': int(quando.year),
        'periodo': periodo,
        'nome': nome,
        'mes_ini': mes_ini,
        'mes_fim': mes_fim,
        'corrida_caminhada': next((s for s in sections if s['id'] == 'corrida_caminhada'), {}),
        'ciclismo': next((s for s in sections if s['id'] == 'ciclismo'), {}),
    }


def format_periodo_ranking_text(dados, patrocinadores='A definir',
                                dashboard_url=None):
    """O texto pronto para colar no grupo — é ele que o botão Copiar copia."""
    titulo = ROTULO_PERIODO.get(dados.get('periodo'), '').upper()
    lines = ['\U0001f3c6*RANKING %s DOS ATLETAS %s* \U0001f3c6'
             % (titulo, (dados.get('nome') or '').upper())]
    lines.append('Patrocinadores: *%s* \U0001f3c5\u2705' % patrocinadores)
    # O LINK VAI NO TOPO, UMA VEZ SO — igual ao ranking do mês (06/09/2026).
    if dashboard_url:
        lines += ['', 'Painel dos atletas: %s' % dashboard_url]
    for key, title in (('corrida_caminhada', 'Corrida/Caminhada'), ('ciclismo', 'Ciclismo')):
        sec = dados.get(key) or {}
        lines.extend(['', '*%s*' % title,
                      'Total atletas: %s' % sec.get('total_atletas', 0),
                      'KM total: %s Km' % _format_km_total(sec.get('total_km'))])
        for atleta in sec.get('atletas', []):
            lines.append('%s %.1fkm %s' % (pos_emoji(atleta.get('rank', 0)),
                                           atleta.get('km', 0),
                                           atleta.get('nome', 'Desconhecido')))
    return '\n'.join(lines)


def format_periodo_partes(dados, patrocinadores='A definir',
                          dashboard_url=None):
    """O ranking do período em 3 mensagens, como o do mês vai para o grupo.

    NÃO REAPROVEITA `format_ranking_partes`: aquela é do MÊS e exige
    `mes_nome`, `atingiram_meta` e a meta da modalidade — coisas que só o mês
    tem. Chamada com o ranking do período ela estoura com `KeyError`
    (medido em 20/09/2026), e o plano B mandava tudo numa mensagem só, sem o
    link. Aqui não há meta nem medalhista de propósito: o troféu do período é
    entregue por fora, e prometer medalha no texto seria mentira.

    Uma mensagem por modalidade + o fechamento, porque o ranking inteiro numa
    mensagem vira "Ler mais" no celular e quem está no fim da lista some.
    """
    titulo = ROTULO_PERIODO.get(dados.get('periodo'), '').upper()
    cabecalho = ['\U0001f3c6*RANKING %s DOS ATLETAS %s* \U0001f3c6'
                 % (titulo, (dados.get('nome') or '').upper()),
                 'Patrocinadores: *%s* \U0001f3c5\u2705' % patrocinadores]
    if dashboard_url:
        cabecalho += ['', 'Painel dos atletas: %s' % dashboard_url]

    def secao(chave, rotulo):
        sec = dados.get(chave) or {}
        linhas = ['*%s*' % rotulo,
                  'Total Atletas: %s' % sec.get('total_atletas', 0)]
        for atleta in sec.get('atletas', []):
            linhas.append('%s %.1fkm %s' % (pos_emoji(atleta.get('rank', 0)),
                                            atleta.get('km', 0),
                                            atleta.get('nome', 'Desconhecido')))
        linhas += ['', '*TOTAL DE KM %s:* %s Km'
                   % (rotulo.upper(), _format_km_total(sec.get('total_km')))]
        return linhas

    m1 = cabecalho + [''] + secao('corrida_caminhada',
                                  'Corrida/Caminhada \U0001f3c3')
    m2 = secao('ciclismo', 'Ciclismo \U0001f6b4')
    total = ((dados.get('corrida_caminhada') or {}).get('total_km') or 0) + \
            ((dados.get('ciclismo') or {}).get('total_km') or 0)
    m3 = ['*TOTAL GERAL DO PERÍODO:* %s Km' % _format_km_total(total),
          'Período: %02d/%d a %02d/%d' % (dados.get('mes_ini') or 1,
                                          dados.get('ano') or 0,
                                          dados.get('mes_fim') or 12,
                                          dados.get('ano') or 0)]
    if dashboard_url:
        m3 += ['', 'Painel dos atletas: %s' % dashboard_url]
    return ['\n'.join(m1), '\n'.join(m2), '\n'.join(m3)]


def build_yearly_ranking_data(cidade, year):
    """Estrutura anual derivada diretamente das atividades validas do Strava."""
    sections, _ = _build_yearly_public_sections(cidade, year)
    return {
        'ano': int(year),
        'corrida_caminhada': next((s for s in sections if s['id'] == 'corrida_caminhada'), {}),
        'ciclismo': next((s for s in sections if s['id'] == 'ciclismo'), {}),
    }


def format_yearly_ranking_text(yearly_data, patrocinadores='A definir'):
    lines = [f'\U0001f3c6*RANKING ANUAL DOS ATLETAS {yearly_data["ano"]}* \U0001f3c6']
    lines.append(f'Patrocinadores: *{patrocinadores}* \U0001f3c5\u2705')
    for key, title in (('corrida_caminhada', 'Corrida/Caminhada'), ('ciclismo', 'Ciclismo')):
        sec = yearly_data.get(key) or {}
        lines.extend(['', f'*{title}*', f"Total atletas: {sec.get('total_atletas', 0)}", f"KM total: {_format_km_total(sec.get('total_km'))} Km"])
        for atleta in sec.get('atletas', []):
            lines.append(f"{pos_emoji(atleta.get('rank', 0))} {atleta.get('km', 0):.1f}km {atleta.get('nome', 'Desconhecido')}")
    return '\n'.join(lines)


def _attach_yearly_info(sections, yearly_lookup):
    for sec in sections:
        by_key = yearly_lookup.get(sec.get('id'), {})
        for atleta in (sec.get('atingiram_meta') or []) + (sec.get('nao_atingiram') or []):
            info = by_key.get(_ranking_item_key(atleta)) or {}
            atleta['annual_rank'] = info.get('rank')
            atleta['annual_km'] = info.get('km', 0)
    return sections


def format_ranking_text(ranking_data, config, dashboard_url=None):
    """
    Texto plano para WhatsApp. Ranking sempre limpo (sem avisos/flags).
    """
    cc = ranking_data['corrida_caminhada']
    ci = ranking_data['ciclismo']
    total_km_geral = (cc.get('total_km') or 0) + (ci.get('total_km') or 0)

    lines = []
    lines.append(
        f"\U0001f3c6*RANKING DOS ATLETAS {ranking_data['mes_nome'].upper()}"
        f"/{ranking_data['ano']}* \U0001f3c6"
    )
    patrocinador = ranking_data.get('patrocinador') or SEM_PATROCINADOR
    lines.append(f"Patrocinador do Mês: *{patrocinador}* \U0001f3c5\u2705")
    if dashboard_url:
        lines.append('')
        lines.append(f"Painel dos atletas: {dashboard_url}")
    lines.append('')

    def secao_texto(titulo, com_meta, sem_meta, meta_km, total, medalhistas):
        s = [
            titulo,
            f'Total Atletas: {total}',
            f'Medalhistas: {medalhistas} \U0001f3c5',
        ]
        pos = 0
        for a in com_meta:
            pos += 1
            s.append(f"{pos_emoji(pos)} {a['km']:.1f}km {a['nome']} \U0001f3c5")
        s.append(f"*\U0001f3c5 META: {int(meta_km)} km*")
        for a in sem_meta:
            pos += 1
            s.append(f"{pos_emoji(pos)} {a['km']:.1f}km {a['nome']}")
        return s

    lines += secao_texto(
        f"*Corrida/Caminhada \U0001f3c3\u200d\u2640\ufe0f\U0001f3c3\u200d\u2642\ufe0f"
        f" [META: {int(config.meta_corrida_caminhada)} km]*",
        cc['atingiram_meta'], cc['nao_atingiram'],
        config.meta_corrida_caminhada, cc['total_atletas'], cc['medalhistas'],
    )
    lines.append('')

    lines += secao_texto(
        f"*Ciclismo \U0001f6b4\u200d\u2640\ufe0f\U0001f6b4\u200d\u2642\ufe0f"
        f" [META: {int(config.meta_ciclismo)} km]*",
        ci['atingiram_meta'], ci['nao_atingiram'],
        config.meta_ciclismo, ci['total_atletas'], ci['medalhistas'],
    )
    lines.append('')

    # Metricas no FIM: quem le no celular quer ver o proprio nome primeiro.
    total = cc['medalhistas'] + ci['medalhistas']
    lines.append(f"*TOTAL DE KM CORRIDA/CAMINHADA:* {_format_km_total(cc.get('total_km'))} Km")
    lines.append(f"*TOTAL DE KM PEDALADA:* {_format_km_total(ci.get('total_km'))} Km")
    lines.append(f"*TOTAL DE KM GERAL:* {_format_km_total(total_km_geral)} Km")
    lines.append(f"*TOTAL DE MEDALHAS CICLISTAS:* {ci['medalhistas']}")
    lines.append(f"*TOTAL DE MEDALHAS CORRIDA/CAMINHADA:* {cc['medalhistas']}")
    lines.append(f"*TOTAL DE MEDALHAS GERAL:* {total}")
    lines += _bloco_patrocinadores_linhas(
        config.cidade, ranking_data['ano'], ranking_data['mes'], patrocinador
    )
    lines += _bloco_rodape_links(config.cidade)
    lines += _bloco_disclaimer()
    return '\n'.join(lines)


# Texto curto que explica de onde vem o km. Fica no fim do ranking para que
# ninguem precise perguntar "como voces sabem quanto eu corri?".
DISCLAIMER_LINHAS = [
    '',
    'ℹ️ Os quilometros vem das atividades PUBLICAS do Strava que cada',
    'atleta compartilha aqui no grupo. Ao postar seu link voce concorda que o',
    'sistema leia daquela atividade os mesmos dados que o Strava ja mostra no',
    'seu perfil publico (distancia, tempo e ritmo). Nada alem disso e coletado,',
    'e nenhum dado e repassado a terceiros.',
]


def _bloco_disclaimer(escape=None):
    if escape:
        return [escape(l) if l else l for l in DISCLAIMER_LINHAS]
    return list(DISCLAIMER_LINHAS)


def format_ranking_partes(ranking_data, config, dashboard_url=None):
    """Ranking quebrado em 3 mensagens, para o WhatsApp da automacao.

    O ranking inteiro numa mensagem so vira um paredao no celular: o WhatsApp
    corta com "Ler mais" e quem esta no fim da lista nem aparece. Dividido em
    tres, cada modalidade chega completa e visivel.

      1) cabecalho + corrida/caminhada + total da modalidade
      2) ciclismo + total da modalidade
      3) patrocinadores + totais gerais + medalhas + links + disclaimer

    Devolve uma lista de strings, na ordem de envio.
    """
    cc = ranking_data['corrida_caminhada']
    ci = ranking_data['ciclismo']
    total_km_geral = (cc.get('total_km') or 0) + (ci.get('total_km') or 0)
    patrocinador = ranking_data.get('patrocinador') or SEM_PATROCINADOR

    def secao(titulo, com_meta, sem_meta, meta_km, total, medalhistas):
        s = [titulo, f'Total Atletas: {total}', f'Medalhistas: {medalhistas} \U0001f3c5']
        pos = 0
        for a in com_meta:
            pos += 1
            s.append(f"{pos_emoji(pos)} {a['km']:.1f}km {a['nome']} \U0001f3c5")
        s.append(f"*\U0001f3c5 META: {int(meta_km)} km*")
        for a in sem_meta:
            pos += 1
            s.append(f"{pos_emoji(pos)} {a['km']:.1f}km {a['nome']}")
        return s

    # ----- 1: corrida/caminhada -----
    m1 = [
        f"\U0001f3c6*RANKING DOS ATLETAS {ranking_data['mes_nome'].upper()}"
        f"/{ranking_data['ano']}* \U0001f3c6",
        f"Patrocinador do Mês: *{patrocinador}* \U0001f3c5✅",
    ]
    if dashboard_url:
        m1 += ['', f'Painel dos atletas: {dashboard_url}']
    m1.append('')
    m1 += secao(
        f"*Corrida/Caminhada \U0001f3c3‍♀️\U0001f3c3‍♂️"
        f" [META: {int(config.meta_corrida_caminhada)} km]*",
        cc['atingiram_meta'], cc['nao_atingiram'],
        config.meta_corrida_caminhada, cc['total_atletas'], cc['medalhistas'],
    )
    # O PAINEL APARECE UMA VEZ SO, LA EM CIMA (06/09/2026). Ele estava
    # sendo inserido aqui TAMBEM, e o mesmo bloco saia com o link repetido.
    # A caixa de texto do site mostra uma vez, no inicio -- a mensagem do
    # grupo tem de ser a mesma coisa.
    m1 += ['', f"*TOTAL DE KM CORRIDA/CAMINHADA:* {_format_km_total(cc.get('total_km'))} Km"]

    # ----- 2: ciclismo -----
    m2 = secao(
        f"*Ciclismo \U0001f6b4‍♀️\U0001f6b4‍♂️"
        f" [META: {int(config.meta_ciclismo)} km]*",
        ci['atingiram_meta'], ci['nao_atingiram'],
        config.meta_ciclismo, ci['total_atletas'], ci['medalhistas'],
    )
    m2 += ['', f"*TOTAL DE KM PEDALADA:* {_format_km_total(ci.get('total_km'))} Km"]

    # ----- 3: fechamento -----
    medalhas = cc['medalhistas'] + ci['medalhistas']
    m3 = []
    bloco_patro = _bloco_patrocinadores_linhas(
        config.cidade, ranking_data['ano'], ranking_data['mes'], patrocinador
    )
    if bloco_patro:
        # o helper comeca com linha vazia; no inicio da mensagem ela sobra
        m3 += [l for i, l in enumerate(bloco_patro) if not (i == 0 and l == '')]
        m3.append('')
    m3 += [
        f"*TOTAL DE KM GERAL:* {_format_km_total(total_km_geral)} Km",
        f"*TOTAL DE MEDALHAS CICLISTAS:* {ci['medalhistas']}",
        f"*TOTAL DE MEDALHAS CORRIDA/CAMINHADA:* {cc['medalhistas']}",
        f"*TOTAL DE MEDALHAS GERAL:* {medalhas}",
    ]
    m3 += _bloco_rodape_links(config.cidade)
    m3 += _bloco_disclaimer()

    return ['\n'.join(m1), '\n'.join(m2), '\n'.join(m3)]


def format_ranking_html(ranking_data, config, dashboard_url=None):
    cc = ranking_data['corrida_caminhada']
    ci = ranking_data['ciclismo']
    total_km_geral = (cc.get('total_km') or 0) + (ci.get('total_km') or 0)

    lines = []
    lines.append(
        f"\U0001f3c6*RANKING DOS ATLETAS {html_mod.escape(ranking_data['mes_nome'].upper())}"
        f"/{ranking_data['ano']}* \U0001f3c6"
    )
    lines.append(
        f"Patrocinador do Mês: "
        f"*{html_mod.escape(ranking_data.get('patrocinador') or SEM_PATROCINADOR)}* \U0001f3c5\u2705"
    )
    if dashboard_url:
        safe_dashboard_url = html_mod.escape(dashboard_url, quote=True)
        lines.append('')
        lines.append(f'Painel dos atletas: <a href="{safe_dashboard_url}">{safe_dashboard_url}</a>')
    lines.append('')

    def linha_html(a, pos, com_medalha):
        nome_safe = html_mod.escape(a['nome'])
        href = _dashboard_href(a)
        link_km = (
            f'<a href="{href}" class="km-link" '
            f'title="Ver dashboard de {nome_safe}">{a["km"]:.1f}km</a>'
        )
        link_nome = f'<span class="atleta-nome">{nome_safe}</span>'
        medalha = ' \U0001f3c5' if com_medalha else ''
        return f"{pos_emoji(pos)} {link_km} {link_nome}{medalha}"

    def secao_html(titulo, com_meta, sem_meta, meta_km, total, medalhistas):
        s = [
            html_mod.escape(titulo),
            f'Total Atletas: {total}',
            f'Medalhistas: {medalhistas} \U0001f3c5',
        ]
        pos = 0
        for a in com_meta:
            pos += 1
            s.append(linha_html(a, pos, com_medalha=True))
        s.append(f"*\U0001f3c5 META: {int(meta_km)} km*")
        for a in sem_meta:
            pos += 1
            s.append(linha_html(a, pos, com_medalha=False))
        return s

    lines += secao_html(
        f"*Corrida/Caminhada \U0001f3c3\u200d\u2640\ufe0f\U0001f3c3\u200d\u2642\ufe0f"
        f" [META: {int(config.meta_corrida_caminhada)} km]*",
        cc['atingiram_meta'], cc['nao_atingiram'],
        config.meta_corrida_caminhada, cc['total_atletas'], cc['medalhistas'],
    )
    lines.append('')

    lines += secao_html(
        f"*Ciclismo \U0001f6b4\u200d\u2640\ufe0f\U0001f6b4\u200d\u2642\ufe0f"
        f" [META: {int(config.meta_ciclismo)} km]*",
        ci['atingiram_meta'], ci['nao_atingiram'],
        config.meta_ciclismo, ci['total_atletas'], ci['medalhistas'],
    )
    lines.append('')

    # Metricas no FIM, igual ao texto plano.
    total = cc['medalhistas'] + ci['medalhistas']
    lines.append(f"*TOTAL DE KM CORRIDA/CAMINHADA:* {_format_km_total(cc.get('total_km'))} Km")
    lines.append(f"*TOTAL DE KM PEDALADA:* {_format_km_total(ci.get('total_km'))} Km")
    lines.append(f"*TOTAL DE KM GERAL:* {_format_km_total(total_km_geral)} Km")
    lines.append(f"*TOTAL DE MEDALHAS CICLISTAS:* {ci['medalhistas']}")
    lines.append(f"*TOTAL DE MEDALHAS CORRIDA/CAMINHADA:* {cc['medalhistas']}")
    lines.append(f"*TOTAL DE MEDALHAS GERAL:* {total}")
    principal_html = ranking_data.get('patrocinador') or SEM_PATROCINADOR
    lines += _bloco_patrocinadores_linhas(
        config.cidade, ranking_data['ano'], ranking_data['mes'],
        principal_html, escape=html_mod.escape,
    )
    lines += _bloco_rodape_links(config.cidade, escape=html_mod.escape)
    lines += _bloco_disclaimer(escape=html_mod.escape)
    return '\n'.join(lines)


def process_ranking(activities, config, month, year, patrocinador_nome=None):
    corrida_tmp = {}
    ciclismo_tmp = {}

    rejected_act_ids = {
        f.atividade_id
        for f in FlagAnticheat.query.filter_by(status='rejeitada').all()
    }

    _escolhidos = _nomes_escolhidos()
    # Atleta mesclado (07/09/2026): resolve para o strava_id do cabeca do
    # grupo ANTES de virar chave do balde -- assim o ranking publicado soma
    # os dois strava_ids como uma posicao so, sem duplicidade. O perdedor
    # ja fica `arquivado=True` (mesclagem), entao sem isto o km dele
    # simplesmente sumiria da lista em vez de somar no cabeca.
    mapa_canonico = Atleta.mapa_strava_canonico()
    for act in activities:
        mod = (act.modalidade or '').lower()
        if mod == 'corrida_caminhada':
            target = corrida_tmp
        elif mod == 'ciclismo':
            target = ciclismo_tmp
        else:
            continue
        sid_bruto = str(act.id_usuario_strava or '0').strip()
        sid = mapa_canonico.get(sid_bruto, sid_bruto)
        name_key = _normalizar_nome_atleta(act.nome_usuario)
        # O NOME ESCOLHIDO MANDA (29/08/2026). Quando alguem definiu como
        # esta pessoa deve aparecer, e esse nome que vai para a tela e para
        # o grupo -- sem passar pelo `_nome_curto_atleta`, que existe para
        # ADIVINHAR um nome curto, e aqui nao ha o que adivinhar.
        display_name = (_escolhidos.get(sid)
                        or _nome_curto_atleta(act.nome_usuario))
        uid = sid if _strava_id_valido(sid) else f'name:{name_key}'
        if uid not in target:
            target[uid] = {
                'nome': display_name,
                'km': 0.0,
                'strava_id': sid if _strava_id_valido(sid) else '0',
                'name_key': name_key,
                # O nome INTEIRO, para desempatar homonimo la embaixo:
                # 'Maria Souza' de duas pessoas so se separa com o resto
                # do nome, que o nome curto ja jogou fora.
                'nome_completo': act.nome_usuario or '',
                # Escolhido por gente? entao a desambiguacao nao mexe.
                'nome_escolhido': bool(_escolhidos.get(sid)),
            }
        if act.id not in rejected_act_ids and act.status != 'removida':
            target[uid]['km'] += (act.distancia or 0)
        target[uid]['nome'] = display_name

    # Perfil ARQUIVADO (duplicata/sem uso -- Fase 3 do plano das melhorias,
    # 28/08/2026) some da LISTA do ranking que vai pro grupo, mas o km dele
    # continua no total_km -- por isso o total soma o dict INTEIRO, ANTES do
    # arquivado sair da lista exibida.
    # ⚠️ NAO usar `consolidado.ocultos()` aqui -- ela tambem inclui quem so'
    # pediu para nao aparecer no SITE (`oculto_no_publico`), e essas pessoas
    # continuam aparecendo pelo nome no ranking do proprio grupo (regra do
    # termo, secao 5): so' saem das paginas publicas, nao do WhatsApp.
    from app import consolidado as _cons
    _arquivados = _cons.arquivados()

    def total_km(d):
        return round(sum(v['km'] for v in d.values()), 1)

    def to_list(d):
        lst = [
            {
                'nome': v['nome'],
                'km': round(v['km'], 1),
                'strava_id': v['strava_id'],
                'name_key': v.get('name_key') or _normalizar_nome_atleta(v['nome']),
                'nome_completo': v.get('nome_completo') or v['nome'],
                'nome_escolhido': v.get('nome_escolhido', False),
            }
            for v in d.values()
            # QUEM FICOU COM ZERO NAO ENTRA NA LISTA (P-132, 07/09/2026).
            # O balde do atleta e criado antes do teste de rejeicao, entao
            # quem teve TODAS as atividades removidas ou rejeitadas sobrava
            # no fim do ranking com '0.0km' -- parecia que a pessoa treinou
            # zero, quando na verdade ela nao pontuou naquele mes. O total
            # do projeto nao muda: `total_km` soma o dicionario inteiro
            # antes deste corte, e zero nao altera soma.
            if v['km'] > 0 and v['strava_id'] not in _arquivados
        ]
        # Homonimo se separa ANTES de ordenar: o nome ja sai pronto para a
        # tela e para a mensagem do grupo.
        _desambiguar_nomes(lst)
        lst.sort(key=lambda x: x['km'], reverse=True)
        return lst

    corrida_total_km = total_km(corrida_tmp)
    ciclismo_total_km = total_km(ciclismo_tmp)
    corrida_list = to_list(corrida_tmp)
    ciclismo_list = to_list(ciclismo_tmp)

    corrida_meta = [a for a in corrida_list if a['km'] >= config.meta_corrida_caminhada]
    corrida_sem = [a for a in corrida_list if a['km'] < config.meta_corrida_caminhada]
    ciclismo_meta = [a for a in ciclismo_list if a['km'] >= config.meta_ciclismo]
    ciclismo_sem = [a for a in ciclismo_list if a['km'] < config.meta_ciclismo]

    return {
        'mes': month, 'ano': year, 'mes_nome': get_month_name(month),
        'corrida_caminhada': {
            'total_atletas': len(corrida_list), 'medalhistas': len(corrida_meta),
            'atingiram_meta': corrida_meta, 'nao_atingiram': corrida_sem,
            'meta': config.meta_corrida_caminhada,
            'total_km': corrida_total_km,
        },
        'ciclismo': {
            'total_atletas': len(ciclismo_list), 'medalhistas': len(ciclismo_meta),
            'atingiram_meta': ciclismo_meta, 'nao_atingiram': ciclismo_sem,
            'meta': config.meta_ciclismo,
            'total_km': ciclismo_total_km,
        },
        'patrocinador': patrocinador_nome or SEM_PATROCINADOR,
        'total_medalhas': len(corrida_meta) + len(ciclismo_meta),
    }


def save_monthly_backup(
    cidade,
    month,
    year,
    ranking_data,
    ranking_text,
    user_id,
    patrocinador_nome_mes,
    patrocinador_logo_blob,
    patrocinador_logo_mime,
    patrocinador_logo_filename,
    overwrite_sponsor_snapshot=False,
):
    total_atletas = (
        ranking_data['corrida_caminhada']['total_atletas'] +
        ranking_data['ciclismo']['total_atletas']
    )
    existing = MonthlyBackup.query.filter_by(cidade=cidade, mes=month, ano=year).first()
    novo_json = json.dumps(ranking_data, ensure_ascii=False)
    if existing:
        # Evita gravacao (e fsync) em toda visualizacao quando nada mudou.
        atualiza_nome = overwrite_sponsor_snapshot or not existing.patrocinador_nome_mes
        atualiza_logo = overwrite_sponsor_snapshot or not existing.patrocinador_logo_blob
        conteudo_igual = (
            existing.json_ranking == novo_json and
            (existing.texto_whatsapp or '') == (ranking_text or '')
        )
        sponsor_igual = (
            (not atualiza_nome or existing.patrocinador_nome_mes == patrocinador_nome_mes) and
            (not atualiza_logo or (
                existing.patrocinador_logo_blob == patrocinador_logo_blob and
                existing.patrocinador_logo_mime == patrocinador_logo_mime and
                existing.patrocinador_logo_filename == patrocinador_logo_filename
            ))
        )
        if conteudo_igual and sponsor_igual:
            logger.debug("Backup inalterado, gravacao pulada: %s - %s/%s", cidade, month, year)
            return
        existing.json_ranking = novo_json
        existing.texto_whatsapp = ranking_text
        existing.total_atletas = total_atletas
        existing.total_medalhas = ranking_data['total_medalhas']
        existing.timestamp_geracao = datetime.now()
        existing.gerado_por = user_id
        if atualiza_nome:
            existing.patrocinador_nome_mes = patrocinador_nome_mes
        if atualiza_logo:
            existing.patrocinador_logo_blob = patrocinador_logo_blob
            existing.patrocinador_logo_mime = patrocinador_logo_mime
            existing.patrocinador_logo_filename = patrocinador_logo_filename
    else:
        db.session.add(MonthlyBackup(
            cidade=cidade, mes=month, ano=year,
            json_ranking=json.dumps(ranking_data, ensure_ascii=False),
            texto_whatsapp=ranking_text,
            total_atletas=total_atletas,
            total_medalhas=ranking_data['total_medalhas'],
            gerado_por=user_id,
            patrocinador_nome_mes=patrocinador_nome_mes,
            patrocinador_logo_blob=patrocinador_logo_blob,
            patrocinador_logo_mime=patrocinador_logo_mime,
            patrocinador_logo_filename=patrocinador_logo_filename,
        ))
    # Dois dispositivos abrindo o ranking ao mesmo tempo tentam gravar o mesmo
    # snapshot. Com retentativa, o segundo espera e passa; sem ela, virava 500
    # na cara de quem estava usando.
    erro = _commit_com_retentativa('snapshot do ranking')
    if erro is not None:
        logger.warning('Snapshot de %s %s/%s nao gravado agora: %s',
                       cidade, month, year, erro)
        return
    logger.info("Backup salvo: %s - %s/%s", cidade, month, year)


def rebuild_monthly_backup(cidade, month, year, user_id=None):
    """Rebuild the persisted ranking snapshot from the activity source of truth."""
    config = Config.query.filter_by(cidade=cidade).first()
    if not config:
        raise RuntimeError(f'Configuracao nao encontrada para {cidade}')

    existing = MonthlyBackup.query.filter_by(
        cidade=cidade, ano=year, mes=month
    ).first()
    now = datetime.now()
    is_current_month = year == now.year and month == now.month
    # Patrocinador vem SEMPRE do registro daquele periodo. Sem registro, fica
    # vazio -- jamais o patrocinador de hoje (era esse fallback que carimbava
    # o patrocinador de julho no ranking de junho).
    principal = _sponsor_principal(cidade, year, month)
    sponsor_name = principal.exibicao if principal else SEM_PATROCINADOR
    generated_by = user_id if user_id is not None else (
        existing.gerado_por if existing else None
    )

    activities = Activity.query.filter(
        Activity.cidade == cidade,
        extract('month', Activity.data_hora) == month,
        extract('year', Activity.data_hora) == year,
    ).all()
    ranking_data = process_ranking(
        activities, config, month, year,
        patrocinador_nome=_nomes_patrocinadores(cidade, year, month),
    )

    slug = _get_or_create_short_slug(cidade, year, month)
    dashboard_url = _append_query_param(
        f'{BASE_URL_ATUAL()}/r/{slug}', 'include_sponsor', '0'
    )
    ranking_text = format_ranking_text(
        ranking_data, config, dashboard_url=dashboard_url
    )

    # A logo tambem vem do periodo. O snapshot no MonthlyBackup e apenas um
    # espelho do registro do mes: quem manda e o PatrocinadorMensal.
    logo_blob = principal.patrocinador_logo_blob if principal else None
    logo_mime = principal.patrocinador_logo_mime if principal else None
    logo_filename = principal.patrocinador_logo_filename if principal else None

    save_monthly_backup(
        cidade=cidade,
        month=month,
        year=year,
        ranking_data=ranking_data,
        ranking_text=ranking_text,
        user_id=generated_by,
        patrocinador_nome_mes=sponsor_name,
        patrocinador_logo_blob=logo_blob,
        patrocinador_logo_mime=logo_mime,
        patrocinador_logo_filename=logo_filename,
        overwrite_sponsor_snapshot=True,
    )
    return ranking_data


def rebuild_current_month_backups():
    """Refresh existing current-month snapshots after a service restart."""
    now = datetime.now()
    cities = [
        row[0] for row in db.session.query(MonthlyBackup.cidade)
        .filter_by(ano=now.year, mes=now.month)
        .distinct()
        .all()
    ]
    for cidade in cities:
        try:
            rebuild_monthly_backup(cidade, now.month, now.year)
            logger.info('Ranking mensal reconstruido no startup: %s', cidade)
        except Exception as exc:
            db.session.rollback()
            logger.error(
                'Falha ao reconstruir ranking mensal de %s: %s',
                cidade, exc, exc_info=True,
            )



def _nomes_patrocinadores_do_ano(cidade, year):
    """Nomes de exibicao de todos os patrocinadores do ANO, sem repetir.

    Cada mes tem os seus; o texto anual agradece a todos. Como o nome de
    exibicao pode variar de mes para mes, entram as duas versoes se de fato
    forem diferentes -- o publico ve o que foi combinado em cada mes.
    """
    nomes = []
    for sp in PatrocinadorMensal.query.filter_by(cidade=cidade, ano=year).order_by(
            PatrocinadorMensal.mes.asc(), PatrocinadorMensal.ordem.asc()).all():
        nome = sp.exibicao
        if nome and nome not in nomes:
            nomes.append(nome)
    return ' | '.join(nomes) or SEM_PATROCINADOR


def _get_sponsor_context(cidade, year, month):
    """Nome e logo do patrocinador DAQUELE periodo. Sem fallback."""
    principal = _sponsor_principal(cidade, year, month)
    if not principal:
        return {'name': SEM_PATROCINADOR, 'logo_url': None}
    logo_url = None
    if principal.patrocinador_logo_blob:
        logo_url = url_for(
            'ranking.public_sponsor_logo',
            city_slug=_slugify_city(cidade),
            year=year,
            month=month,
            _external=True,
        )
    return {'name': principal.exibicao, 'logo_url': logo_url}


def _public_page_context(cidade, year, month):
    """Contexto comum das paginas publicas (partial padrao):
    agradecimento, logo principal, logos adicionais, QR e links de rodape.
    """
    config = Config.query.filter_by(cidade=cidade).first()
    ctx = _get_sponsor_context(cidade, year, month)
    adicionais = [
        {
            'nome': sp.exibicao,
            'logo_url': url_for('ranking.public_extra_sponsor_logo', sponsor_id=sp.id),
        }
        for sp in _patrocinadores_adicionais(cidade, year, month)
        if sp.patrocinador_logo_blob
    ]
    return {
        'cidade': cidade,
        'sponsor_name': ctx['name'],
        'sponsor_logo_url': ctx['logo_url'],
        'sponsors_extra': adicionais,
        'agradecimento_nomes': _nomes_patrocinadores(cidade, year, month),
        'chave_pix': (config.chave_pix if config else '') or '',
        'qr_pix': (config.qr_pix if config else '') or '',
        'feedback_url': _feedback_public_url(cidade),
        'transparencia_url': _transparencia_public_url(cidade),
    }


def _chave_atleta_ranking(atleta):
    """Chave estavel de um atleta no ranking (strava_id valido ou name:<slug>)."""
    sid = str(atleta.get('strava_id') or '0').strip()
    if _strava_id_valido(sid):
        return sid
    return f"name:{atleta.get('name_key') or _normalizar_nome_atleta(atleta.get('nome'))}"


def _datas_meta_batida(activities, config, month, year):
    """Data em que cada atleta CRUZOU a meta, por modalidade.

    Retorna {(modalidade, chave_atleta): date}. A soma acumulada segue a ordem
    cronologica das atividades validas (nao removidas / nao rejeitadas); a data
    e a da atividade que fez o total alcancar a meta. Serve para ordenar os
    parabens e mostrar 'bateu no dia X'.
    """
    rejected_act_ids = {
        f.atividade_id
        for f in FlagAnticheat.query.filter_by(status='rejeitada').all()
    }
    metas = {
        'corrida_caminhada': config.meta_corrida_caminhada,
        'ciclismo': config.meta_ciclismo,
    }
    ordenadas = sorted(
        (a for a in activities if a.data_hora),
        key=lambda a: a.data_hora,
    )
    acumulado = {}
    cruzou = {}
    for a in ordenadas:
        mod = (a.modalidade or '').lower()
        if mod not in metas:
            continue
        if a.id in rejected_act_ids or a.status == 'removida':
            continue
        chave = mod, _chave_atleta_ranking({
            'strava_id': a.id_usuario_strava,
            'nome': a.nome_usuario,
            'name_key': _normalizar_nome_atleta(a.nome_usuario),
        })
        antes = acumulado.get(chave, 0.0)
        depois = antes + (a.distancia or 0)
        acumulado[chave] = depois
        if chave not in cruzou and depois >= metas[mod] and metas[mod] > 0:
            cruzou[chave] = a.data_hora.date()
    return cruzou


def _parabens_enviados_map(cidade, year, month):
    """{(modalidade, chave_atleta): enviado_em} do periodo."""
    try:
        rows = db.session.execute(
            text("SELECT modalidade, chave_atleta, enviado_em FROM parabens_enviados "
                 "WHERE cidade = :c AND ano = :a AND mes = :m"),
            {'c': cidade, 'a': year, 'm': month},
        ).fetchall()
    except Exception:
        return {}
    return {(r[0], r[1]): r[2] for r in rows}


def _build_goal_messages(ranking_data, sponsor_card_url=None, cidade=None,
                         year=None, month=None, datas_meta=None):
    if not isinstance(ranking_data, dict):
        return []

    enviados = _parabens_enviados_map(cidade, year, month) if cidade else {}
    datas_meta = datas_meta or {}

    items = []
    sections = (
        ('corrida_caminhada', 'Caminhada/Corrida'),
        ('ciclismo', 'Ciclismo'),
    )
    for section_id, modalidade_label in sections:
        section = ranking_data.get(section_id, {}) or {}
        for atleta in section.get('atingiram_meta', []) or []:
            nome = atleta.get('nome') or 'Atleta'
            chave = _chave_atleta_ranking(atleta)
            message = f'Parabéns por bater a meta de {modalidade_label}, {nome}!! 👏👏👏'
            if sponsor_card_url:
                message = f'{message}\n{sponsor_card_url}'
            data_meta = datas_meta.get((section_id, chave))
            enviado_em = enviados.get((section_id, chave))
            items.append({
                'id': f"{section_id}-{chave}",
                'chave_atleta': chave,
                'section_id': section_id,
                'nome': nome,
                'modalidade': modalidade_label,
                'message': message,
                'data_meta': data_meta,
                'data_meta_texto': data_meta.strftime('%d/%m') if data_meta else '',
                'enviado': enviado_em is not None,
            })
    # Ordena pela data em que bateu a meta (quem bateu primeiro aparece primeiro);
    # sem data conhecida vai para o fim.
    items.sort(key=lambda it: (it['data_meta'] is None, it['data_meta'] or datetime.max.date()))
    return items


ranking_bp = Blueprint('ranking', __name__, url_prefix='/ranking')
ranking_short_bp = Blueprint('ranking_short', __name__)


@ranking_short_bp.route('/r/<slug>')
def redirect_short_link(slug):
    _ensure_schema_once()
    if not _SLUG_RE.match(slug or ''):
        return render_template(
            'ranking/public_links.html',
            token_valid=False,
            error_message='Link inválido. Solicite um novo link do ranking.',
        ), 400

    link = ShortLink.query.filter_by(slug=slug, active=True).first()
    if not link:
        # Slug deterministico cuja linha se perdeu (banco restaurado de um
        # backup anterior a geracao do link): recalcula o periodo e recria o
        # registro no primeiro acesso, sem manutencao e sem erro para quem clicou.
        link = _reconstruir_short_link(slug)
    if not link:
        return render_template(
            'ranking/public_links.html',
            token_valid=False,
            error_message='Link não encontrado. Solicite um link atualizado no ranking.',
        ), 410

    token = _generate_ranking_links_token(link.cidade, link.ano, link.mes)
    include_sponsor = '1' if _as_bool(request.args.get('include_sponsor')) else '0'
    # O RANKING DE PERIODO USA O MESMO ENCURTADOR (P-137-c, 25/09/2026): o
    # `?periodo=` leva direto a pagina publica daquele periodo, sem senha.
    periodo = (request.args.get('periodo') or '').strip().lower()
    if periodo in ('trimestre', 'semestre', 'ano'):
        return redirect(url_for(
            'ranking.public_yearly_links',
            token=token,
            year=link.ano,
            mes=link.mes,
            periodo=periodo,
            include_sponsor=include_sponsor,
            _external=True,
        ))
    target = url_for(
        'ranking.public_athletes_links',
        token=token,
        include_sponsor=include_sponsor,
        _external=True,
    )
    return redirect(target)


@ranking_bp.route('/')
@ranking_bp.route('/current')
@login_required
@viewer_required
def current():
    _ensure_schema_once()
    now = datetime.now()
    return view_month(now.year, now.month)


@ranking_bp.route('/periodo/<periodo>')
@login_required
@viewer_required
def periodo(periodo):
    """Ranking do trimestre, do semestre ou do ano — com as abas e o Copiar.

    O botão Copiar leva SEMPRE o texto da aba aberta (regra do dono, 20/09):
    quem está olhando o trimestre quer copiar o trimestre.
    """
    _ensure_schema_once()
    if periodo not in PERIODOS_RANKING:
        abort(404)
    if periodo == 'mes':
        return redirect(url_for('ranking.current'))
    cidade = current_user.cidade_ativa
    agora = datetime.now()
    dados = build_periodo_ranking_data(cidade, periodo, agora)
    patrocinadores = _nomes_patrocinadores_do_ano(cidade, agora.year)
    try:
        painel = link_publico_do_periodo(cidade, periodo, agora)
    except Exception as exc:
        logger.warning('ranking do periodo: sem link publico (%s)', exc)
        painel = None
    return render_template(
        'ranking/periodo.html',
        dados=dados,
        rotulo=ROTULO_PERIODO[periodo],
        periodo=periodo,
        premiado=periodo_premiado(cidade),
        frequencia=_frequencia_da_tela(cidade, periodo),
        ranking_text=format_periodo_ranking_text(
            dados, patrocinadores, dashboard_url=painel),
        sponsor_name=patrocinadores,
    )


@ranking_bp.route('/periodo')
@login_required
@viewer_required
def periodo_inicio():
    """Porta de entrada do ranking por período, para o card do painel.

    O catálogo (`indice._abrivel`) só faz card de rota SEM argumento, e
    `/periodo/<periodo>` tem um — por isso o card "Ranking por período" nunca
    apareceu (achado em 25/09/2026). Abre no período premiado da cidade, ou no
    trimestre quando não há troféu.
    """
    premiado = periodo_premiado(current_user.cidade_ativa)
    return redirect(url_for('ranking.periodo', periodo=premiado or 'trimestre'))


def link_publico_do_periodo(cidade, periodo, quando=None):
    """O link do ranking de um período que vai ao grupo — PÚBLICO, sem senha.

    Era `/ranking/periodo/<p>` (20/09), tela que exige login: o atleta que
    clicava no ranking do trimestre caía na tela de senha (achado em 25/09).
    Agora é o mesmo encurtador do mês (`/r/<slug>`) com `?periodo=`, e o
    encurtador leva à página pública daquele período.
    """
    quando = quando or datetime.now()
    slug = _get_or_create_short_slug(cidade, quando.year, quando.month)
    url = _append_query_param('%s/r/%s' % (BASE_URL_ATUAL(), slug), 'periodo', periodo)
    return _append_query_param(url, 'include_sponsor', '0')


@ranking_bp.route('/periodo/premiado', methods=['POST'])
@login_required
@admin_required
def definir_periodo_premiado():
    """Qual período ganha troféu NESTA cidade — ou nenhum.

    Nenhum marcado quer dizer "não houve patrocinador para o troféu", e é isso
    que impede o envio daquele ranking ao grupo (regra do dono). A escolha é de
    cada cidade e não alcança as outras (regra 1).
    """
    from app import secretstore
    escolhido = (request.form.get('periodo') or '').strip().lower()
    if escolhido not in ('', 'trimestre', 'semestre', 'ano'):
        flash('Período inválido.', 'error')
        return voltar('ranking.periodo', periodo='trimestre')
    cidade = current_user.cidade_ativa
    # "NENHUM" SE APAGA, NAO SE GRAVA VAZIO (medido em 20/09/2026): o
    # `set_value` recusa valor vazio e devolve False em silencio — marcar
    # "nenhum" nao desmarcava nada, e a tela dizia que sim.
    if escolhido:
        secretstore.set_value(secretstore.PERIODO_PREMIADO, escolhido, scope=cidade,
                              updated_by=current_user.email)
    else:
        secretstore.delete(secretstore.PERIODO_PREMIADO, scope=cidade)
    if escolhido:
        # MARCAR O TROFEU JA LIGA A FREQUENCIA MINIMA (regra do dono): ter
        # trofeu quer dizer ter patrocinador, e o ranking precisa aparecer no
        # grupo. Se a cidade ja tinha escolhido uma frequencia, ela fica.
        from app import rankingperiodo
        if escolhido in ('trimestre', 'semestre'):
            atual = rankingperiodo.frequencia(cidade, escolhido)
            rankingperiodo.definir_frequencia(
                cidade, escolhido,
                atual or rankingperiodo.FREQ_PADRAO[escolhido],
                por=current_user.email)
        flash('Troféu %s marcado para %s. O ranking do período passa a ser '
              'enviado ao grupo.' % (ROTULO_PERIODO[escolhido].lower(), cidade),
              'success')
    else:
        flash('Nenhum troféu marcado: o ranking de período não vai ao grupo.',
              'info')
    return voltar('ranking.periodo', periodo=escolhido or 'trimestre')


def _frequencia_da_tela(cidade, periodo):
    """A frequência escolhida naquele período, para a tela marcar a opção."""
    try:
        from app import rankingperiodo
        return rankingperiodo.frequencia(cidade, periodo)
    except Exception:
        return ''


def periodo_premiado(cidade):
    """O período com troféu nesta cidade, ou '' quando não há."""
    from app import secretstore
    valor = (secretstore.get(secretstore.PERIODO_PREMIADO, scope=cidade) or '').strip().lower()
    return valor if valor in ('trimestre', 'semestre', 'ano') else ''


# O TEXTO DA FREQUÊNCIA, para gente ler (P-137-c, 25/09/2026). As chaves são
# as de `rankingperiodo`; aqui mora só o que aparece na tela.
ROTULO_FREQ = {
    'trimestre': {'1': '1× por semana (sábado)',
                  '2': '2× por semana (quarta e sábado)',
                  '3': '3× por semana (segunda, quarta e sábado)',
                  'diario': 'todo dia'},
    'semestre': {'4': '4× por mês (dias 7, 14, 21 e 28)',
                 '6': '6× por mês (dias 5, 10, 15, 20, 25 e 28)',
                 '8': '8× por mês (dias 4, 8, 12, 16, 20, 24, 28 e o último)'},
}


def _resumo_trofeu(cidade):
    """O troféu da cidade e quando ele vai ao grupo — a linha sob as abas.

    Nunca derruba a tela do ranking: sem conseguir ler, a linha não aparece.
    """
    try:
        premiado = periodo_premiado(cidade)
        freq = (_frequencia_da_tela(cidade, premiado)
                if premiado in ('trimestre', 'semestre') else '')
        return {'premiado': premiado,
                'frequencia': ROTULO_FREQ.get(premiado, {}).get(freq, '')}
    except Exception as exc:
        logger.warning('resumo do trofeu (%s): %s', cidade, exc)
        return None


_MES_CURTO = {1: 'jan', 2: 'fev', 3: 'mar', 4: 'abr', 5: 'mai', 6: 'jun',
              7: 'jul', 8: 'ago', 9: 'set', 10: 'out', 11: 'nov', 12: 'dez'}


def _nome_do_bloco(periodo, ano, mes_ini):
    """'3º trimestre de 2026 (jul–set)' — o nome do bloco como gente lê."""
    if periodo not in ('trimestre', 'semestre'):
        return str(ano)
    tamanho = 3 if periodo == 'trimestre' else 6
    numero = (mes_ini - 1) // tamanho + 1
    return '%s %s de %d (%s–%s)' % (_ORDINAL[numero], periodo, ano,
                                    _MES_CURTO[mes_ini],
                                    _MES_CURTO[mes_ini + tamanho - 1])


def _blocos_publicos(cidade, periodo, ano, mes_ref):
    """Os trimestres (ou semestres) com ranking, do mais novo ao mais velho.

    Sai dos meses que já têm ranking guardado — a mesma lista que o seletor
    de mês da página pública usa —, mais o bloco que está aberto.
    """
    tamanho = 3 if periodo == 'trimestre' else 6
    inicio = lambda mes: ((mes - 1) // tamanho) * tamanho + 1  # noqa: E731
    aberto = (ano, inicio(mes_ref))
    blocos = {aberto}
    blocos.update((a, inicio(m)) for a, m in _list_backup_months(cidade))
    return [{'value': '%d-%d' % (a, ini),
             'label': _nome_do_bloco(periodo, a, ini),
             'selected': (a, ini) == aberto}
            for a, ini in sorted(blocos, reverse=True)]


def _abas_publicas(token, ano, mes, include_sponsor, ativa):
    """As quatro abas da página pública do link do grupo (P-137-c, 25/09/2026).

    O mês de referência viaja entre as abas: quem está em setembro e toca em
    Trimestral vê o trimestre de setembro (jul–set), e não outro.
    """
    inc = '1' if include_sponsor else '0'
    abas = [('mes', 'Mensal', 'fa-calendar-day',
             url_for('ranking.public_athletes_links', token=token, year=ano,
                     month=mes, include_sponsor=inc))]
    for chave, icone in (('trimestre', 'fa-calendar-week'),
                         ('semestre', 'fa-calendar-days'),
                         ('ano', 'fa-calendar')):
        abas.append((chave, ROTULO_PERIODO[chave], icone,
                     url_for('ranking.public_yearly_links', token=token,
                             year=ano, mes=mes, periodo=chave,
                             include_sponsor=inc)))
    return [{'chave': c, 'rotulo': r, 'icone': i, 'url': u, 'ativa': c == ativa}
            for c, r, i, u in abas]


@ranking_bp.route('/periodo/frequencia', methods=['POST'])
@login_required
@admin_required
def definir_frequencia_periodo():
    """Com que frequência o ranking daquele período sai no grupo.

    Trimestral conta por SEMANA (1, 2, 3 vezes ou todo dia); semestral conta
    por MÊS (4, 6 ou 8 vezes), porque semestre é período longo e ranking
    semanal cansaria o grupo sem acrescentar nada. Sempre no último ranking do
    dia — nunca em horário escrito no código.
    """
    from app import rankingperiodo
    periodo = (request.form.get('periodo') or '').strip().lower()
    valor = (request.form.get('frequencia') or '').strip().lower()
    cidade = current_user.cidade_ativa
    if rankingperiodo.definir_frequencia(cidade, periodo, valor,
                                         por=current_user.email):
        flash('Frequência do ranking %s salva para %s.'
              % (ROTULO_PERIODO.get(periodo, periodo).lower(), cidade), 'success')
    else:
        flash('Frequência inválida para esse período.', 'error')
    return voltar('ranking.periodo',
                            periodo=periodo if periodo in PERIODOS_RANKING
                            else 'trimestre')


@ranking_bp.route('/annual/<int:year>')
@login_required
@viewer_required
def annual(year):
    _ensure_schema_once()
    cidade = current_user.cidade_ativa
    if year < 2009 or year > datetime.now().year + 1:
        abort(404)
    data = build_yearly_ranking_data(cidade, year)
    config = Config.query.filter_by(cidade=cidade).first()
    # Anual: junta os nomes de exibicao de todos os meses do ano, sem repetir.
    sponsor_name = _nomes_patrocinadores_do_ano(cidade, year)
    return render_template(
        'ranking/annual.html', yearly_data=data,
        ranking_text=format_yearly_ranking_text(data, sponsor_name),
        sponsor_name=sponsor_name, selected_year=year,
    )


def _ler_imagem_enviada(campo='sponsor_logo'):
    """Le a imagem do formulario. Devolve (blob, mime, nome) ou (None, erro, None)."""
    image = request.files.get(campo)
    if not image or not image.filename:
        return None, None, None
    if not (image.mimetype or '').startswith('image/'):
        return None, 'Arquivo invalido: envie uma imagem.', None
    content = image.read()
    if not content:
        return None, 'A imagem chegou vazia.', None
    if len(content) > _SPONSOR_MAX_SIZE_BYTES:
        return None, 'Imagem maior que 10 MB.', None
    return content, image.mimetype, secure_filename(image.filename)


@ranking_bp.route('/sponsor-logo/upload', methods=['POST'])
@login_required
@admin_required
def upload_sponsor_logo():
    """Troca a logo do Patrocinador do Mes DO PERIODO escolhido.

    Antes esta rota gravava um arquivo por cidade (uploads/sponsors/<cidade>/
    logo.*), sem mes no caminho -- trocar a logo reescrevia a de todos os
    meses anteriores. Agora a logo e um blob do periodo.
    """
    _ensure_schema_once()
    cidade = current_user.cidade_ativa
    now = datetime.now()
    year = request.form.get('year', type=int) or now.year
    month = request.form.get('month', type=int) or now.month

    blob, mime, filename = _ler_imagem_enviada()
    if blob is None:
        flash(mime or 'Nenhuma imagem selecionada.', 'warning')
        return redirect(request.referrer or url_for('ranking.patrocinadores', year=year, month=month))

    principal = _sponsor_principal(cidade, year, month)
    if not principal:
        flash('Cadastre primeiro o Patrocinador do Mês deste período; depois envie a logo.', 'warning')
        return redirect(url_for('ranking.patrocinadores', year=year, month=month))

    principal.patrocinador_logo_blob = blob
    principal.patrocinador_logo_mime = mime
    principal.patrocinador_logo_filename = filename
    db.session.commit()
    _rebuild_silencioso(cidade, month, year)
    flash(f'Logo atualizada apenas para {get_month_name(month)}/{year}.', 'success')
    logger.info('Logo do patrocinador de %s atualizada em %s/%s', cidade, month, year)
    return redirect(request.referrer or url_for('ranking.patrocinadores', year=year, month=month))


@ranking_bp.route('/sponsor-logo/remove', methods=['POST'])
@login_required
@admin_required
def remove_sponsor_logo():
    _ensure_schema_once()
    cidade = current_user.cidade_ativa
    now = datetime.now()
    year = request.form.get('year', type=int) or now.year
    month = request.form.get('month', type=int) or now.month

    principal = _sponsor_principal(cidade, year, month)
    if principal:
        principal.patrocinador_logo_blob = None
        principal.patrocinador_logo_mime = None
        principal.patrocinador_logo_filename = None
    backup = MonthlyBackup.query.filter_by(cidade=cidade, ano=year, mes=month).first()
    if backup:
        backup.patrocinador_logo_blob = None
        backup.patrocinador_logo_mime = None
        backup.patrocinador_logo_filename = None
    db.session.commit()
    flash(f'Logo removida apenas de {get_month_name(month)}/{year}.', 'success')
    logger.info('Logo do patrocinador removida para %s em %s/%s', cidade, month, year)
    return redirect(request.referrer or url_for('ranking.patrocinadores', year=year, month=month))


@ranking_bp.route('/sponsors/<int:sponsor_id>/remove', methods=['POST'])
@login_required
@admin_required
def remove_monthly_sponsor(sponsor_id):
    _ensure_schema_once()
    sponsor = PatrocinadorMensal.query.get_or_404(sponsor_id)
    if sponsor.cidade != current_user.cidade_ativa and not current_user.is_supremo:
        abort(403)
    cidade, month, year = sponsor.cidade, sponsor.mes, sponsor.ano
    nome = sponsor.exibicao
    db.session.delete(sponsor)
    db.session.commit()
    _rebuild_silencioso(cidade, month, year)
    flash(f'"{nome}" removido de {get_month_name(month)}/{year}. '
          'Os outros meses não foram tocados.', 'success')
    return redirect(request.referrer or url_for('ranking.patrocinadores', year=year, month=month))


def _rebuild_silencioso(cidade, month, year):
    """Reconstroi o ranking do periodo sem derrubar a tela se algo falhar."""
    try:
        rebuild_monthly_backup(cidade, month, year, user_id=current_user.id)
    except Exception as exc:
        logger.warning('Falha ao reconstruir ranking de %s %s/%s: %s',
                       cidade, month, year, exc)


def _legenda_completa(sp):
    """Monta a legenda pronta para copiar/enviar (WAHA) de um patrocinador."""
    partes = []
    nome = sp.exibicao
    if nome:
        if (sp.categoria or 'adicional') == 'principal':
            partes.append('Patrocinador do mes: ' + nome)
        else:
            partes.append(nome)
    if (sp.bordao or '').strip():
        partes.append(sp.bordao.strip())
    if (sp.legenda or '').strip():
        partes.append(sp.legenda.strip())
    if (sp.telefone or '').strip():
        # Sempre no mesmo formato, nao "do jeito que foi digitado": o
        # principal saia 'DD9XXXXXXXX' e o apoio '(DD) 9XXXX-XXXX' na mesma
        # tela, so porque foram cadastrados por pessoas diferentes.
        from app import telefone as _tel
        partes.append('Contato: ' + _tel.bonito(sp.telefone))
    return '\n'.join(partes)


def _commit_com_retentativa(o_que='registro'):
    """Commit resiliente ao SQLite do NAS (lock/IO transitorio).

    Devolve None em caso de sucesso ou a excecao final. Sem isto a tela
    devolvia 500 e o admin perdia o que digitou.
    """
    erro = None
    for tentativa in range(3):
        try:
            db.session.commit()
            return None
        except Exception as exc:
            erro = exc
            db.session.rollback()
            logger.warning('Commit de %s falhou (tentativa %s): %s',
                           o_que, tentativa + 1, exc)
            import time as _t
            _t.sleep(0.6 * (tentativa + 1))
    return erro


@ranking_bp.route('/patrocinadores')
@login_required
@admin_required
def patrocinadores():
    """Gestao de patrocinadores por (cidade, ano, mes).

    Cada mes tem a propria lista. Trocar qualquer coisa aqui vale SO para o
    mes escolhido -- os meses ja publicados ficam como sairam.
    """
    _ensure_schema_once()
    from app.models import PatrocinadorCatalogo
    cidade = current_user.cidade_ativa
    now = datetime.now()
    year = request.args.get('year', type=int) or now.year
    month = request.args.get('month', type=int) or now.month

    principal = _sponsor_principal(cidade, year, month)
    itens = [
        {'sp': sp, 'legenda_completa': _legenda_completa(sp),
         'logo_url': (url_for('ranking.public_extra_sponsor_logo', sponsor_id=sp.id)
                      if sp.patrocinador_logo_blob else None)}
        for sp in _patrocinadores_adicionais(cidade, year, month)
    ]
    anos = list(range(now.year + 1, now.year - 6, -1))

    principal_logo_url = None
    if principal and principal.patrocinador_logo_blob:
        principal_logo_url = url_for(
            'ranking.public_extra_sponsor_logo', sponsor_id=principal.id)

    catalogo = PatrocinadorCatalogo.query.filter_by(cidade=cidade).order_by(
        PatrocinadorCatalogo.nome_interno.asc()).all()
    # Meses que ja tem patrocinador cadastrado — ajuda o admin a ver de relance
    # quais periodos estao vazios.
    periodos = db.session.query(
        PatrocinadorMensal.ano, PatrocinadorMensal.mes, func.count(PatrocinadorMensal.id)
    ).filter_by(cidade=cidade).group_by(
        PatrocinadorMensal.ano, PatrocinadorMensal.mes
    ).order_by(PatrocinadorMensal.ano.desc(), PatrocinadorMensal.mes.desc()).all()

    return render_template('ranking/patrocinadores.html',
                           itens=itens, cidade=cidade, year=year, month=month,
                           meses=MESES_PT, anos=anos,
                           principal=principal,
                           principal_logo_url=principal_logo_url,
                           # A legenda do PRINCIPAL faltava na tela: o admin nao
                           # conseguia conferir o texto que a automacao manda
                           # junto com a logo dele (o apoio ja tinha).
                           principal_legenda=(_legenda_completa(principal)
                                              if principal else ''),
                           catalogo=catalogo, periodos=periodos,
                           is_current_month=(year == now.year and month == now.month))


@ranking_bp.route('/patrocinadores/salvar', methods=['POST'])
@login_required
@admin_required
def patrocinador_salvar():
    """Cria ou atualiza um patrocinador DE UM PERIODO.

    Tudo que e salvo aqui pertence a (cidade, ano, mes) escolhidos. O mesmo
    patrocinador em outro mes e outro registro, com os proprios textos.
    """
    _ensure_schema_once()
    from app.models import PatrocinadorCatalogo
    cidade = current_user.cidade_ativa
    now = datetime.now()
    year = request.form.get('year', type=int) or now.year
    month = request.form.get('month', type=int) or now.month
    voltar = url_for('ranking.patrocinadores', year=year, month=month)

    sponsor_id = request.form.get('sponsor_id', type=int)
    nome = (request.form.get('sponsor_name') or '').strip()
    nome_exibicao = (request.form.get('nome_exibicao') or '').strip() or None
    categoria = (request.form.get('categoria') or 'adicional').strip()
    if categoria not in ('principal', 'adicional'):
        categoria = 'adicional'
    telefone = (request.form.get('telefone') or '').strip() or None
    bordao = (request.form.get('bordao') or '').strip() or None
    # A FREQUENCIA COM QUE ESTE APOIO APARECE (06/09, redividido em
    # 07/09/2026). 0 a 5; fora disso vira o padrao 3. Cada valor e' uma
    # contagem de vezes por semana (`ranking.MAPA_VEZES_SEMANA`: 1=3x,
    # 2=5x, 3=7x, 4=10x, 5=14x), distribuida pela grade de 14 vagas
    # semanais (`ranking._grade_semanal`). 0 fica de fora sem apagar a ficha.
    try:
        peso_frequencia = int(request.form.get('peso_frequencia', 3))
    except (TypeError, ValueError):
        peso_frequencia = 3
    peso_frequencia = max(0, min(peso_frequencia, 5))
    # Endereço e mapa do BALCÃO deste patrocinador (P-54). Ficam na ficha do
    # catálogo, não no mês: o endereço dele não muda de um mês para o outro.
    endereco = (request.form.get('endereco') or '').strip() or None
    maps_url = (request.form.get('maps_url') or '').strip() or None
    legenda = (request.form.get('legenda') or '').strip() or None
    o_que_banca = (request.form.get('o_que_banca') or '').strip() or None
    banca_medalhas = (request.form.get('banca_medalhas') or '') == 'sim'
    patrocinio_continuo = (request.form.get('patrocinio_continuo') or '') == 'sim'
    salvar_no_catalogo = (request.form.get('salvar_catalogo') or '') == 'sim'

    if not nome:
        flash('Informe o nome do patrocinador.', 'warning')
        return redirect(voltar)

    blob, mime, filename = _ler_imagem_enviada()
    if blob is None and mime:  # mime carrega a mensagem de erro
        flash(mime, 'warning')
        return redirect(voltar)

    novo = not sponsor_id
    if sponsor_id:
        sp = PatrocinadorMensal.query.get_or_404(sponsor_id)
        if sp.cidade != cidade and not current_user.is_supremo:
            abort(403)
    else:
        # Ainda NAO entra na sessao: qualquer consulta abaixo dispararia
        # autoflush e o banco recusaria a linha pela metade.
        sp = PatrocinadorMensal(cidade=cidade, ano=year, mes=month,
                                criado_por=current_user.id)

    # So existe UM principal por periodo: promover um rebaixa o anterior.
    if categoria == 'principal':
        anterior = _sponsor_principal(cidade, year, month)
        if anterior is not None and anterior is not sp:
            anterior.categoria = 'adicional'
            anterior.ordem = _proxima_ordem(cidade, year, month)
        sp.ordem = 0
    elif sp.ordem is None or sp.ordem == 0:
        sp.ordem = _proxima_ordem(cidade, year, month)

    sp.patrocinador_nome = nome
    sp.nome_exibicao = nome_exibicao
    sp.categoria = categoria
    sp.telefone = telefone
    sp.bordao = bordao
    # A frequencia vale para ESTE mes; a ficha guarda o padrao (abaixo).
    sp.peso_frequencia = peso_frequencia
    sp.legenda = legenda
    sp.o_que_banca = o_que_banca
    sp.banca_medalhas = banca_medalhas
    sp.patrocinio_continuo = patrocinio_continuo

    if blob is not None:
        sp.patrocinador_logo_blob = blob
        sp.patrocinador_logo_mime = mime
        sp.patrocinador_logo_filename = filename

    if novo:
        db.session.add(sp)

    if salvar_no_catalogo:
        ficha = PatrocinadorCatalogo.query.filter_by(
            cidade=cidade, nome_interno=nome).first()
        if not ficha:
            ficha = PatrocinadorCatalogo(cidade=cidade, nome_interno=nome,
                                         criado_por=current_user.id)
            db.session.add(ficha)
        ficha.nome_exibicao = nome_exibicao
        ficha.telefone = telefone
        ficha.bordao = bordao
        # O padrao da ficha, copiado quando ela entrar num mes novo.
        ficha.peso_frequencia = peso_frequencia
        # Só sobrescreve quando veio preenchido: salvar o mês com o campo em
        # branco não pode apagar o endereço que já estava na ficha.
        if endereco:
            ficha.endereco = endereco
        if maps_url:
            ficha.maps_url = maps_url
        ficha.legenda = legenda
        ficha.o_que_banca = o_que_banca
        ficha.banca_medalhas = banca_medalhas
        ficha.patrocinio_continuo = patrocinio_continuo
        ficha.ativo = True
        if blob is not None:
            ficha.patrocinador_logo_blob = blob
            ficha.patrocinador_logo_mime = mime
            ficha.patrocinador_logo_filename = filename

    # O ENDEREÇO SALVA MESMO SEM MARCAR "salvar no catálogo" (13/08).
    #
    # Ele pertence ao patrocinador, não ao mês — mas estava preso dentro do
    # bloco acima, que só roda com a caixa marcada. Resultado: o dono digitou
    # endereço e link, clicou em salvar, e nada persistiu; ao voltar em
    # Medalhistas o aviso continuava dizendo que não havia endereço.
    if endereco or maps_url:
        # A FICHA SE ACHA PELO NOME quando não há `catalogo_id` — e não há em
        # nenhum registro existente (medido em 14/08). Era por isso que o
        # endereço não salvava: a condição antiga exigia um vínculo que nunca
        # foi gravado. Cria a ficha se não existir, e **grava o vínculo**, para
        # o próximo salvamento já ir pelo caminho curto.
        _ficha = None
        if getattr(sp, 'catalogo_id', None):
            _ficha = PatrocinadorCatalogo.query.get(sp.catalogo_id)
        if _ficha is None:
            _ficha = PatrocinadorCatalogo.query.filter_by(
                cidade=cidade, nome_interno=nome).first()
        if _ficha is None:
            _ficha = PatrocinadorCatalogo(cidade=cidade, nome_interno=nome,
                                          criado_por=current_user.id)
            db.session.add(_ficha)
            db.session.flush()
        if endereco:
            _ficha.endereco = endereco
        if maps_url:
            _ficha.maps_url = maps_url
        if _ficha.id and not getattr(sp, 'catalogo_id', None):
            sp.catalogo_id = _ficha.id

    erro = _commit_com_retentativa('patrocinador')
    if erro is not None:
        flash('Não consegui salvar agora (o banco estava ocupado). '
              'Tente novamente em alguns segundos.', 'danger')
        return redirect(voltar)

    _rebuild_silencioso(cidade, month, year)
    flash(f'Patrocinador salvo em {get_month_name(month)}/{year}.', 'success')
    # AVISO, NAO BLOQUEIO (07/09/2026): se a soma das frequencias dos apoios
    # passar das 14 vagas da semana, quem tem frequencia mais alta continua
    # saindo (e' processado primeiro em `_grade_semanal`); quem tem
    # frequencia mais baixa perde repeticoes. O admin so' precisa saber.
    soma_semana = sum(_vezes_semana(sp) for sp in _patrocinadores_adicionais(cidade, year, month))
    if soma_semana > 14:
        flash(f'Atenção: os apoios deste mês somam {soma_semana}x por semana, mais '
              f'que as 14 vagas do rodízio (2 rankings/dia x 7 dias). Quem tem '
              f'frequência mais alta continua saindo; quem tem frequência mais '
              f'baixa vai perder repetições.', 'warning')
    return redirect(voltar)


def _proxima_ordem(cidade, year, month):
    """Proxima posicao livre entre os adicionais (0 e reservado ao principal)."""
    atual = db.session.query(func.max(PatrocinadorMensal.ordem)).filter_by(
        cidade=cidade, ano=year, mes=month).scalar() or 0
    return max(atual, 0) + 1


@ranking_bp.route('/patrocinadores/do-catalogo', methods=['POST'])
@login_required
@admin_required
def patrocinador_do_catalogo():
    """Traz uma ficha do catalogo para o mes escolhido — copiando tudo.

    Copia, nunca referencia: se a ficha mudar depois, este mes fica como
    esta. E assim que o mesmo patrocinador aparece em varios meses sem que um
    contamine o outro.
    """
    _ensure_schema_once()
    from app.models import PatrocinadorCatalogo
    cidade = current_user.cidade_ativa
    now = datetime.now()
    year = request.form.get('year', type=int) or now.year
    month = request.form.get('month', type=int) or now.month
    voltar = url_for('ranking.patrocinadores', year=year, month=month)

    ficha = PatrocinadorCatalogo.query.get_or_404(
        request.form.get('catalogo_id', type=int) or 0)
    if ficha.cidade != cidade and not current_user.is_supremo:
        abort(403)
    categoria = (request.form.get('categoria') or 'adicional').strip()
    if categoria not in ('principal', 'adicional'):
        categoria = 'adicional'

    ja = PatrocinadorMensal.query.filter_by(
        cidade=cidade, ano=year, mes=month, patrocinador_nome=ficha.nome_interno).first()
    if ja:
        flash(f'"{ficha.nome_interno}" já está em {get_month_name(month)}/{year}.', 'info')
        return redirect(voltar)

    if categoria == 'principal':
        anterior = _sponsor_principal(cidade, year, month)
        if anterior is not None:
            anterior.categoria = 'adicional'
            anterior.ordem = _proxima_ordem(cidade, year, month)
        ordem = 0
    else:
        ordem = _proxima_ordem(cidade, year, month)

    db.session.add(PatrocinadorMensal(
        cidade=cidade, ano=year, mes=month,
        patrocinador_nome=ficha.nome_interno,
        nome_exibicao=ficha.nome_exibicao,
        catalogo_id=ficha.id,
        patrocinador_logo_blob=ficha.patrocinador_logo_blob,
        patrocinador_logo_mime=ficha.patrocinador_logo_mime,
        patrocinador_logo_filename=ficha.patrocinador_logo_filename,
        telefone=ficha.telefone, bordao=ficha.bordao, legenda=ficha.legenda,
        o_que_banca=ficha.o_que_banca, banca_medalhas=ficha.banca_medalhas,
        patrocinio_continuo=ficha.patrocinio_continuo,
        categoria=categoria, ordem=ordem, criado_por=current_user.id,
    ))
    erro = _commit_com_retentativa('patrocinador do catalogo')
    if erro is not None:
        flash('Não consegui salvar agora. Tente novamente em alguns segundos.', 'danger')
        return redirect(voltar)
    _rebuild_silencioso(cidade, month, year)
    flash(f'"{ficha.exibicao}" adicionado a {get_month_name(month)}/{year}.', 'success')
    return redirect(voltar)


@ranking_bp.route('/patrocinadores/copiar-mes', methods=['POST'])
@login_required
@admin_required
def patrocinadores_copiar_mes():
    """Copia todos os patrocinadores de um mes para outro.

    Atalho para quando o time de patrocinadores se repete. Continua sendo
    COPIA: os dois meses seguem independentes depois disso.
    """
    _ensure_schema_once()
    cidade = current_user.cidade_ativa
    now = datetime.now()
    year = request.form.get('year', type=int) or now.year
    month = request.form.get('month', type=int) or now.month
    voltar = url_for('ranking.patrocinadores', year=year, month=month)

    origem = (request.form.get('origem') or '').strip()
    try:
        ano_o, mes_o = [int(p) for p in origem.split('-', 1)]
    except (ValueError, AttributeError):
        flash('Escolha o mês de origem.', 'warning')
        return redirect(voltar)
    if (ano_o, mes_o) == (year, month):
        flash('A origem e o destino são o mesmo mês.', 'warning')
        return redirect(voltar)

    existentes = {
        (sp.patrocinador_nome or '').strip().lower()
        for sp in _patrocinadores_periodo(cidade, year, month)
    }
    tem_principal = _sponsor_principal(cidade, year, month) is not None
    copiados = 0
    for sp in _patrocinadores_periodo(cidade, ano_o, mes_o):
        nome = (sp.patrocinador_nome or '').strip()
        if not nome or nome.lower() in existentes:
            continue
        principal = sp.is_principal and not tem_principal
        if principal:
            tem_principal = True
        db.session.add(PatrocinadorMensal(
            cidade=cidade, ano=year, mes=month,
            patrocinador_nome=nome, nome_exibicao=sp.nome_exibicao,
            catalogo_id=sp.catalogo_id,
            patrocinador_logo_blob=sp.patrocinador_logo_blob,
            patrocinador_logo_mime=sp.patrocinador_logo_mime,
            patrocinador_logo_filename=sp.patrocinador_logo_filename,
            telefone=sp.telefone, bordao=sp.bordao, legenda=sp.legenda,
            o_que_banca=sp.o_que_banca, banca_medalhas=sp.banca_medalhas,
            patrocinio_continuo=sp.patrocinio_continuo,
            categoria='principal' if principal else 'adicional',
            ordem=0 if principal else _proxima_ordem(cidade, year, month),
            criado_por=current_user.id,
        ))
        db.session.flush()
        existentes.add(nome.lower())
        copiados += 1

    erro = _commit_com_retentativa('copia de patrocinadores')
    if erro is not None:
        flash('Não consegui copiar agora. Tente novamente em alguns segundos.', 'danger')
        return redirect(voltar)
    _rebuild_silencioso(cidade, month, year)
    if copiados:
        flash(f'{copiados} patrocinador(es) copiado(s) de '
              f'{get_month_name(mes_o)}/{ano_o} para {get_month_name(month)}/{year}.', 'success')
    else:
        flash('Nada a copiar: esses patrocinadores já estavam no mês.', 'info')
    return redirect(voltar)


@ranking_bp.route('/patrocinadores/catalogo/<int:ficha_id>/remover', methods=['POST'])
@login_required
@admin_required
def catalogo_remover(ficha_id):
    """Tira a ficha do catalogo. Os meses ja cadastrados NAO sao tocados."""
    _ensure_schema_once()
    from app.models import PatrocinadorCatalogo
    ficha = PatrocinadorCatalogo.query.get_or_404(ficha_id)
    if ficha.cidade != current_user.cidade_ativa and not current_user.is_supremo:
        abort(403)
    nome = ficha.nome_interno
    db.session.delete(ficha)
    db.session.commit()
    flash(f'"{nome}" saiu do catálogo. Os meses em que ele aparece continuam iguais.', 'success')
    return redirect(request.referrer or url_for('ranking.patrocinadores'))


@ranking_bp.route('/catalogo-logo/<int:ficha_id>')
@login_required
@viewer_required
def catalogo_logo(ficha_id):
    """Logo guardada na ficha do catalogo (previa da tela de gestao)."""
    from app.models import PatrocinadorCatalogo
    ficha = PatrocinadorCatalogo.query.get_or_404(ficha_id)
    if ficha.cidade != current_user.cidade_ativa and not current_user.is_supremo:
        abort(403)
    if not ficha.patrocinador_logo_blob:
        abort(404)
    return Response(ficha.patrocinador_logo_blob,
                    mimetype=ficha.patrocinador_logo_mime or 'application/octet-stream')


@ranking_bp.route('/current-sponsor-logo')
@login_required
@viewer_required
def current_sponsor_logo():
    """Logo do patrocinador do MES CORRENTE da cidade em que o usuario esta."""
    _ensure_schema_once()
    agora = datetime.now()
    principal = _sponsor_principal(current_user.cidade_ativa, agora.year, agora.month)
    blob = principal.patrocinador_logo_blob if principal else None
    mime = principal.patrocinador_logo_mime if principal else None
    if not blob:
        abort(404)
    return Response(blob, mimetype=mime or 'application/octet-stream')


@ranking_bp.route('/public-sponsor-extra/<int:sponsor_id>')
def public_extra_sponsor_logo(sponsor_id):
    """Logo de um patrocinador, pelo id da linha do mes.

    ⚠️ ESTAS IMAGENS SAO GRANDES (28/08/2026): medidas em producao, 896 KB,
    1,2 MB e 1,8 MB -- e a pagina inicial mostra todas numa tabela de
    miniaturas de 92 pixels. Sem Pillow no servidor (conferido: nao esta
    instalado) nao da para reduzir aqui, entao o que se pode fazer e' garantir
    que o navegador baixe UMA VEZ:

    - `ETag` pelo conteudo: recarregar a pagina devolve 304, sem corpo;
    - `Cache-Control` de 30 dias -- logo de patrocinador de mes fechado nao
      muda, e se mudar o ETag muda junto;
    - o `loading="lazy"` no template evita baixar o que ninguem rolou ate.

    O conserto de verdade e' a logo NASCER pequena no upload (P-119).
    """
    sponsor = PatrocinadorMensal.query.get_or_404(sponsor_id)
    if not sponsor.patrocinador_logo_blob:
        abort(404)
    dados = sponsor.patrocinador_logo_blob
    etiqueta = '"%s"' % hashlib.md5(dados).hexdigest()
    if request.headers.get('If-None-Match') == etiqueta:
        return Response(status=304)
    resposta = Response(
        dados, mimetype=sponsor.patrocinador_logo_mime or 'application/octet-stream')
    resposta.headers['ETag'] = etiqueta
    resposta.headers['Cache-Control'] = 'public, max-age=2592000'
    return resposta


@ranking_bp.route('/public-sponsor-logo/<city_slug>/<int:year>/<int:month>')
def public_sponsor_logo(city_slug, year, month):
    """Logo do Patrocinador do Mes DAQUELE periodo.

    Le do registro do periodo; o snapshot do MonthlyBackup fica de reserva
    para links antigos gerados antes da migracao.
    """
    _ensure_schema_once()
    cidade = _resolve_city_by_slug(city_slug)
    if not cidade:
        abort(404)

    principal = _sponsor_principal(cidade, year, month)
    if principal and principal.patrocinador_logo_blob:
        return Response(
            principal.patrocinador_logo_blob,
            mimetype=principal.patrocinador_logo_mime or 'application/octet-stream')

    backup = MonthlyBackup.query.filter_by(cidade=cidade, ano=year, mes=month).first()
    if not backup or not backup.patrocinador_logo_blob:
        abort(404)

    mime = backup.patrocinador_logo_mime or 'application/octet-stream'
    return Response(backup.patrocinador_logo_blob, mimetype=mime)


@ranking_bp.route('/sponsor-card/<city_slug>/<int:year>/<int:month>')
def sponsor_card(city_slug, year, month):
    _ensure_schema_once()
    cidade = _resolve_city_by_slug(city_slug)
    if not cidade:
        abort(404)

    principal = _sponsor_principal(cidade, year, month)
    tem_logo = bool(principal and principal.patrocinador_logo_blob)
    if not tem_logo:
        backup = MonthlyBackup.query.filter_by(cidade=cidade, ano=year, mes=month).first()
        tem_logo = bool(backup and backup.patrocinador_logo_blob)
    if not tem_logo:
        abort(404)

    name = html_mod.escape(
        (principal.exibicao if principal else None) or 'Patrocinador do Mês', quote=True)
    image_url = url_for(
        'ranking.public_sponsor_logo',
        city_slug=_slugify_city(cidade),
        year=year,
        month=month,
        _external=True,
    )
    body = f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{name}</title>
  <meta property="og:title" content="{name}">
  <meta property="og:description" content="Patrocinador do Mês - MoveMentes">
  <meta property="og:type" content="website">
  <meta property="og:image" content="{image_url}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{name}">
  <meta name="twitter:description" content="Patrocinador do Mês - MoveMentes">
  <meta name="twitter:image" content="{image_url}">
</head>
<body style="margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center;background:#f8f9fa;">
  <img src="{image_url}" alt="{name}" style="max-width:100%;max-height:100vh;object-fit:contain;">
</body>
</html>"""
    return Response(body, mimetype='text/html; charset=utf-8')


@ranking_bp.route('/public-links/<token>')
def public_athletes_links(token):
    _ensure_schema_once()
    include_sponsor = _as_bool(request.args.get('include_sponsor'))
    try:
        data = _ranking_links_serializer().loads(token, max_age=_RANKING_LINKS_MAX_AGE_SECONDS)
    except SignatureExpired:
        return render_template(
            'ranking/public_links.html',
            token_valid=False,
            error_message='Link expirado. Solicite um link atualizado no ranking.',
        ), 410
    except BadSignature:
        return render_template(
            'ranking/public_links.html',
            token_valid=False,
            error_message='Link inválido. Solicite um novo link do ranking.',
        ), 400

    cidade = str(data.get('cidade', '')).strip()
    default_year = int(data.get('ano', 0) or 0)
    default_month = int(data.get('mes', 0) or 0)

    if not cidade or default_year <= 0 or not (1 <= default_month <= 12):
        return render_template(
            'ranking/public_links.html',
            token_valid=False,
            error_message='Link inválido. Solicite um novo link do ranking.',
        ), 400

    selected_year = request.args.get('year', type=int) or default_year
    selected_month = request.args.get('month', type=int) or default_month
    period = (request.args.get('period') or '').strip()
    if '-' in period:
        try:
            p_year, p_month = period.split('-', 1)
            selected_year = int(p_year)
            selected_month = int(p_month)
        except ValueError:
            pass
    if not (1 <= selected_month <= 12):
        selected_month = default_month

    available_pairs = _list_backup_months(cidade)
    if not available_pairs:
        return render_template(
            'ranking/public_links.html',
            token_valid=True,
            cidade=cidade,
            error_message='Nenhum ranking público disponível para esta cidade.',
            token=token,
            month_options=[],
            selected_year=selected_year,
            selected_month=selected_month,
            include_sponsor=include_sponsor,
        ), 404

    valid_pairs = set(available_pairs)
    if (selected_year, selected_month) not in valid_pairs:
        selected_year, selected_month = default_year, default_month
    if (selected_year, selected_month) not in valid_pairs:
        selected_year, selected_month = available_pairs[0]

    backup = MonthlyBackup.query.filter_by(
        cidade=cidade,
        ano=selected_year,
        mes=selected_month,
    ).first()
    if not backup:
        return render_template(
            'ranking/public_links.html',
            token_valid=True,
            cidade=cidade,
            error_message='Ranking não encontrado para o período solicitado.',
            token=token,
            month_options=[],
            selected_year=selected_year,
            selected_month=selected_month,
            include_sponsor=include_sponsor,
        ), 404

    try:
        ranking_data = backup.get_ranking_data()
    except Exception:
        ranking_data = {}

    month_options = [
        {
            'value': f'{year}-{month}',
            'label': f'{get_month_name(month)}/{year}',
            'selected': (year == selected_year and month == selected_month),
        }
        for (year, month) in available_pairs
    ]

    sponsor_ctx = _get_sponsor_context(cidade, selected_year, selected_month)
    yearly_sections, yearly_lookup = _build_yearly_public_sections(
        cidade, selected_year, selected_month
    )
    sections = _attach_yearly_info(_split_ranking_sections(ranking_data), yearly_lookup)

    return render_template(
        'ranking/public_links.html',
        token_valid=True,
        cidade=cidade,
        token=token,
        ranking_data=ranking_data,
        sections=sections,
        yearly_sections=yearly_sections,
        public_yearly_url=url_for(
            'ranking.public_yearly_links',
            token=token,
            year=selected_year,
            include_sponsor='1' if include_sponsor else '0',
        ),
        month_options=month_options,
        selected_year=selected_year,
        selected_month=selected_month,
        month_name=get_month_name(selected_month),
        include_sponsor=include_sponsor,
        sponsor_name=sponsor_ctx['name'],
        sponsor_logo_url=sponsor_ctx['logo_url'],
        pub=_public_page_context(cidade, selected_year, selected_month),
        atletas_publicos_url=url_for('athlete.lista_publica', city_slug=_slugify_city(cidade)),
        abas=_abas_publicas(token, selected_year, selected_month,
                            include_sponsor, 'mes'),
    )


@ranking_bp.route('/public-yearly/<token>')
def public_yearly_links(token):
    _ensure_schema_once()
    include_sponsor = _as_bool(request.args.get('include_sponsor'))
    try:
        data = _ranking_links_serializer().loads(token, max_age=_RANKING_LINKS_MAX_AGE_SECONDS)
    except SignatureExpired:
        return render_template(
            'ranking/public_yearly.html',
            token_valid=False,
            error_message='Link expirado. Solicite um link atualizado no ranking.',
        ), 410
    except BadSignature:
        return render_template(
            'ranking/public_yearly.html',
            token_valid=False,
            error_message='Link inválido. Solicite um novo link do ranking.',
        ), 400

    cidade = str(data.get('cidade', '')).strip()
    default_year = int(data.get('ano', 0) or 0)
    default_month = int(data.get('mes', 0) or 0)
    selected_year = request.args.get('year', type=int) or default_year

    if not cidade or selected_year <= 0:
        return render_template(
            'ranking/public_yearly.html',
            token_valid=False,
            error_message='Link inválido. Solicite um novo link do ranking.',
        ), 400

    # TRIMESTRE E SEMESTRE NA MESMA PAGINA (P-137-c, 25/09/2026). A pagina do
    # ano ganhou `?periodo=`: a soma e a mesma do ano, so com o recorte de meses
    # do bloco (`meses_do_periodo`) — a mesma conta da tela do admin e do
    # ranking que vai ao grupo. Sem `?periodo=`, e o anual de sempre.
    periodo = (request.args.get('periodo') or 'ano').strip().lower()
    if periodo not in ('trimestre', 'semestre', 'ano'):
        periodo = 'ano'
    mes_ref = request.args.get('mes', type=int) or 0
    bloco = (request.args.get('bloco') or '').strip()
    if '-' in bloco:
        try:
            b_ano, b_mes = bloco.split('-', 1)
            if int(b_ano) > 0 and 1 <= int(b_mes) <= 12:
                selected_year, mes_ref = int(b_ano), int(b_mes)
        except ValueError:
            pass
    if not (1 <= mes_ref <= 12):
        # O bloco do link (o mes em que ele foi enviado); noutro ano, o bloco
        # de hoje se for o ano corrente, senao o ultimo do ano.
        hoje = datetime.now()
        if selected_year == default_year and 1 <= default_month <= 12:
            mes_ref = default_month
        elif selected_year == hoje.year:
            mes_ref = hoje.month
        else:
            mes_ref = 12
    mes_ini, mes_fim, _nome = meses_do_periodo(
        periodo, datetime(selected_year, mes_ref, 1))

    sponsor_ctx = _get_sponsor_context(cidade, selected_year, default_month)
    yearly_sections, _ = _build_yearly_public_sections(
        cidade, selected_year, default_month, mes_ini=mes_ini, mes_fim=mes_fim)
    month_options = [
        {
            'value': str(year),
            'label': str(year),
            'selected': year == selected_year,
        }
        for year in sorted({ano for ano, _mes in _list_backup_months(cidade)}, reverse=True)
    ]
    if not month_options:
        month_options = [{'value': str(selected_year), 'label': str(selected_year), 'selected': True}]

    return render_template(
        'ranking/public_yearly.html',
        token_valid=True,
        cidade=cidade,
        token=token,
        selected_year=selected_year,
        selected_month=default_month,
        sections=yearly_sections,
        month_options=month_options,
        include_sponsor=include_sponsor,
        sponsor_name=sponsor_ctx['name'],
        sponsor_logo_url=sponsor_ctx['logo_url'],
        monthly_url=url_for(
            'ranking.public_athletes_links',
            token=token,
            year=selected_year,
            month=default_month,
            include_sponsor='1' if include_sponsor else '0',
        ),
        pub=_public_page_context(cidade, selected_year, default_month),
        periodo=periodo,
        rotulo_periodo=ROTULO_PERIODO[periodo],
        nome_bloco=_nome_do_bloco(periodo, selected_year, mes_ini),
        bloco_options=(_blocos_publicos(cidade, periodo, selected_year, mes_ref)
                       if periodo in ('trimestre', 'semestre') else []),
        abas=_abas_publicas(token, selected_year, mes_ref, include_sponsor,
                            periodo),
    )


def _marca_fora_do_recorde_impl():
    """Tira UMA atividade das tabelas públicas de velocidade (19/09/2026, item 76).

    POR QUE ESTE BOTÃO EXISTE (pedido do dono, com caso medido)
    A tabela de recordes da página inicial mostra a melhor marca de cada
    pessoa, e o GPS às vezes inventa: um trecho em linha reta, sem rota, vira
    velocidade impossível (o caso levantado: 28/10/2024, 2,57 km a 16,9 km/h,
    com o mapa mostrando o salto). Quem confere abrindo a atividade no Strava
    precisava de um jeito de tirar aquela marca dali — sem mexer nos km, que
    continuam contando (regra 14).

    O CAMINHO É O QUE JÁ EXISTE: `velocidade_confiavel = False` é exatamente o
    que as duas tabelas (`consolidado.recordistas` e `mais_velozes`) filtram, e
    é o mesmo campo que o anticheat usa. A justificativa fica numa flag
    `FlagAnticheat` já resolvida — assim o motivo e o autor ficam guardados no
    mesmo lugar em que o resto das decisões de anticheat mora.

    FRONTEIRA DE CIDADE (regra 1): só atividade da `cidade_ativa` de quem clica.
    """
    from app import consolidado as _cons
    from app import logcanon
    destino = request.referrer or url_for('index')
    # A validação do formulário é por rota neste projeto (não é global).
    try:
        from flask_wtf.csrf import validate_csrf
        validate_csrf(request.form.get('csrf_token') or '')
    except Exception:
        flash('A página expirou. Atualize e tente de novo.', 'warning')
        return redirect(destino)
    id_atividade = (request.form.get('id_atividade') or '').strip()
    motivo = (request.form.get('motivo') or '').strip()
    if not id_atividade:
        flash('Não sei qual atividade tirar da lista.', 'warning')
        return redirect(destino)
    if len(motivo) < 5:
        flash('Escreva o motivo (o que você viu na atividade).', 'warning')
        return redirect(destino)

    ativ = (Activity.query
            .filter(Activity.id_atividade == id_atividade,
                    Activity.cidade == current_user.cidade_ativa)
            .order_by(Activity.id.desc()).first())
    if not ativ:
        flash('Essa atividade não é da cidade em que você está trabalhando.', 'danger')
        return redirect(destino)
    if ativ.velocidade_confiavel is False:
        flash('Essa marca já estava fora das tabelas de velocidade.', 'info')
        return redirect(destino)

    ativ.velocidade_confiavel = False
    atleta = Atleta.query.filter_by(strava_id=str(ativ.id_usuario_strava)).first()
    if atleta:
        db.session.add(FlagAnticheat(
            atleta_id=atleta.id, atividade_id=ativ.id,
            tipo_flag='velocidade_fora_do_recorde', nivel='medio',
            status='aprovada', criada_em=datetime.now(),
            descricao='Marca tirada das tabelas públicas de velocidade por %s.'
                      % current_user.email,
            observacao_admin=motivo[:2000],
            resolvida_em=datetime.now(), resolvida_por_id=current_user.id))
    db.session.commit()
    try:
        logcanon.escrever('SITE', 'anticheat',
                          'atividade %s (%s, %s km/h) fora das tabelas de '
                          'velocidade por %s: %s'
                          % (id_atividade, ativ.nome_usuario,
                             ativ.velocidade_media, current_user.email, motivo[:120]))
    except Exception:
        pass
    # A página inicial guarda uma foto do dia: sem refazer, a marca continuaria
    # na tela até a foto vencer.
    try:
        _cons.refazer_foto_do_dia()
    except Exception as exc:
        logger.warning('Nao consegui refazer a foto do dia: %s', exc)
    flash('Marca de %s tirada das tabelas de velocidade. Os km dela continuam '
          'contando no ranking.' % ativ.nome_usuario, 'success')
    return redirect(destino)


@ranking_bp.route('/marca-fora-do-recorde', methods=['POST'])
@login_required
@admin_required
def marca_fora_do_recorde():
    return _marca_fora_do_recorde_impl()


@ranking_bp.route('/feedback/<city_slug>', methods=['GET', 'POST'])
def feedback_public(city_slug):
    """Pagina publica de feedback (sem login), linkada no fim do ranking."""
    _ensure_schema_once()
    cidade = _resolve_city_by_slug(city_slug)
    if not cidade:
        abort(404)

    tipos_validos = ('revisao_ranking', 'sugestao', 'reclamacao')
    if request.method == 'POST':
        tipo = (request.form.get('tipo') or '').strip()
        mensagem = (request.form.get('mensagem') or '').strip()
        nome = (request.form.get('nome_atleta') or '').strip()
        contato = (request.form.get('contato') or '').strip()

        if tipo not in tipos_validos:
            flash('Selecione o tipo de mensagem.', 'warning')
        elif not mensagem:
            flash('Escreva sua mensagem antes de enviar.', 'warning')
        elif tipo == 'revisao_ranking' and not nome:
            flash('Para pedir revisao do ranking, informe o nome do atleta.', 'warning')
        else:
            fb = Feedback(
                cidade=cidade,
                tipo=tipo,
                nome_atleta=nome or None,
                mensagem=mensagem[:4000],
                contato=contato[:120] or None,
                status='nova',
            )
            db.session.add(fb)
            db.session.commit()
            flash('Mensagem enviada. Obrigado pela sua contribuicao!', 'success')
            return redirect(url_for('ranking.feedback_public', city_slug=city_slug))

    return render_template(
        'ranking/feedback_public.html',
        cidade=cidade,
        city_slug=_slugify_city(cidade),
    )


# ---------------------------------------------------------------------------
# P-93 -- A PAGINA PUBLICA CONSOLIDADA
# ---------------------------------------------------------------------------
# A prova cabal do projeto: quanto ja se andou, desde quando, com quantas
# pessoas. Publica, sem login e sem token -- endereco estavel, para caber num
# oficio, num e-mail a patrocinador e na primeira linha de uma proposta.
#
# A conta mora em `app/consolidado.py` (unico lugar do sistema que soma
# ATRAVESSANDO cidade, de proposito e documentado la).


@ranking_bp.route('/consolidado')
@ranking_bp.route('/consolidado/<city_slug>')
def consolidado_publico(city_slug=None):
    """Numeros do projeto inteiro, ou de uma cidade, com filtro de periodo."""
    from app import consolidado as cons

    cidade = None
    if city_slug:
        cidade = _resolve_city_by_slug(city_slug)
        if not cidade:
            abort(404)

    periodo = request.args.get('periodo', 'tudo')
    dados = cons.numeros(
        periodo=periodo,
        de=request.args.get('de'),
        ate=request.args.get('ate'),
        cidade=cidade,
    )
    com_dado, em_implantacao = cons.cidades_com_dado()

    return render_template(
        'ranking/consolidado_publico.html',
        d=dados,
        cidade=cidade,
        city_slug=city_slug,
        cidades_com_dado=com_dado,
        cidades_em_implantacao=em_implantacao,
        slug=_slugify_city,
        rotulos=cons.ROTULO_PERIODO,
    )


@ranking_bp.route('/transparencia/<city_slug>')
def transparencia_public(city_slug):
    """Pagina publica de transparencia (prestacao de contas), mes a mes."""
    _ensure_schema_once()
    cidade = _resolve_city_by_slug(city_slug)
    if not cidade:
        abort(404)

    now = datetime.now()
    registros = (
        PrestacaoContas.query.filter_by(cidade=cidade)
        .order_by(PrestacaoContas.ano.desc(), PrestacaoContas.mes.desc())
        .all()
    )
    # Nunca exibir meses futuros.
    registros = [r for r in registros if (r.ano, r.mes) <= (now.year, now.month)]

    from app import caixa as caixa_mod
    acumulado = caixa_mod.acumulado_prestacao(cidade)

    meses = []
    for r in registros:
        corrido = acumulado.get((r.ano, r.mes), {})
        meses.append({
            'ano': r.ano,
            'mes': r.mes,
            'mes_nome': get_month_name(r.mes),
            'saldo_anterior': corrido.get('anterior', 0.0),
            'saldo_consolidado': corrido.get('ate', r.saldo),
            'valor_patrocinado': r.valor_patrocinado or 0,
            'custo_medalhas': r.custo_medalhas or 0,
            'custo_arte': r.custo_arte or 0,
            'custo_energia': r.custo_energia or 0,
            'outros_custos': r.outros_custos or 0,
            'total_custos': r.total_custos,
            'saldo': r.saldo,
            'observacao': r.observacao,
            'comprovantes': [
                {'id': c.id, 'nome': c.arquivo_nome} for c in r.comprovantes
            ],
        })

    # O consolidado de HOJE = o do mes mais recente ja publicado (meses futuros
    # nao entram, como na lista acima).
    consolidado_hoje = meses[0]['saldo_consolidado'] if meses else 0.0

    return render_template(
        'ranking/transparencia_public.html',
        cidade=cidade,
        meses=meses,
        consolidado_hoje=consolidado_hoje,
        city_slug=city_slug,
    )


@ranking_bp.route('/comprovante/<token>')
def comprovante_publico(token):
    """Abre o comprovante de uma linha do livro-caixa para quem tem o link.

    É o endereço que vai dentro da planilha e do PDF enviados por fora. Sem login,
    mas o token é aleatório (192 bits): só abre quem recebeu. Linha apagada ou
    link revogado = 404.
    """
    from app import caixa as caixa_mod
    linha = caixa_mod.por_token(token)
    resposta = caixa_mod.enviar_comprovante(linha) if linha else None
    if resposta is None:
        abort(404)
    return resposta


@ranking_bp.route('/transparencia/comprovante/<int:cid>')
def transparencia_comprovante(cid):
    """Serve um comprovante publicamente (exceto de meses futuros)."""
    _ensure_schema_once()
    comp = ComprovantePrestacao.query.get_or_404(cid)
    registro = comp.prestacao
    now = datetime.now()
    if not registro or (registro.ano, registro.mes) > (now.year, now.month):
        abort(404)
    if not comp.arquivo_path or not os.path.exists(comp.arquivo_path):
        abort(404)
    return send_file(
        comp.arquivo_path, download_name=comp.arquivo_nome, as_attachment=False
    )


@ranking_bp.route('/<int:year>/<int:month>')
@login_required
@viewer_required
def view_month(year, month):
    _ensure_schema_once()
    cidade = current_user.cidade_ativa
    config = Config.query.filter_by(cidade=cidade).first()
    if not config:
        flash('Configuracoes para sua cidade nao encontradas.', 'warning')
        return redirect(url_for('config.view_config'))

    backup_before = MonthlyBackup.query.filter_by(cidade=cidade, ano=year, mes=month).first()
    now = datetime.now()
    is_current_month = (year == now.year and month == now.month)
    principal_do_mes = _sponsor_principal(cidade, year, month)
    sponsor_name_for_month = (
        principal_do_mes.exibicao if principal_do_mes else SEM_PATROCINADOR
    )

    activities = Activity.query.filter(
        Activity.cidade == cidade,
        extract('month', Activity.data_hora) == month,
        extract('year', Activity.data_hora) == year,
    ).all()

    ranking_data = process_ranking(
        activities, config, month, year,
        patrocinador_nome=_nomes_patrocinadores(cidade, year, month),
    )
    ranking_share_url = _build_public_ranking_url(cidade, year, month)
    dashboard_url = _append_query_param(ranking_share_url, 'include_sponsor', '0')
    # Ranking sempre limpo (sem avisos/flags): uma unica saida de texto.
    ranking_text = format_ranking_text(
        ranking_data, config, dashboard_url=dashboard_url,
    )
    ranking_html = format_ranking_html(
        ranking_data, config, dashboard_url=dashboard_url,
    )

    logo_blob = principal_do_mes.patrocinador_logo_blob if principal_do_mes else None
    logo_mime = principal_do_mes.patrocinador_logo_mime if principal_do_mes else None
    logo_filename = principal_do_mes.patrocinador_logo_filename if principal_do_mes else None

    save_monthly_backup(
        cidade=cidade,
        month=month,
        year=year,
        ranking_data=ranking_data,
        ranking_text=ranking_text,
        user_id=current_user.id,
        patrocinador_nome_mes=sponsor_name_for_month,
        patrocinador_logo_blob=logo_blob,
        patrocinador_logo_mime=logo_mime,
        patrocinador_logo_filename=logo_filename,
        overwrite_sponsor_snapshot=True,
    )

    backup = MonthlyBackup.query.filter_by(cidade=cidade, ano=year, mes=month).first()
    sponsor_logo_url = None
    if backup and backup.patrocinador_logo_blob:
        sponsor_logo_url = url_for(
            'ranking.public_sponsor_logo',
            city_slug=_slugify_city(cidade),
            year=year,
            month=month,
        )
    sponsor_name = sponsor_name_for_month
    sponsors_extra = [
        {'sp': sp, 'legenda_completa': _legenda_completa(sp)}
        for sp in _patrocinadores_adicionais(cidade, year, month)
    ]
    sponsor_card_url = None
    if sponsor_logo_url:
        sponsor_card_url = url_for(
            'ranking.sponsor_card',
            city_slug=_slugify_city(cidade),
            year=year,
            month=month,
            _external=True,
        )
    datas_meta = _datas_meta_batida(activities, config, month, year)
    goal_messages = _build_goal_messages(
        ranking_data, sponsor_card_url=sponsor_card_url,
        cidade=cidade, year=year, month=month, datas_meta=datas_meta,
    )

    available_months = []
    for i in range(12):
        t = datetime.now() - timedelta(days=30 * i)
        available_months.append({
            'value': f"{t.year}-{t.month}",
            'label': f"{get_month_name(t.month)}/{t.year}",
            'selected': 'selected' if (t.month == month and t.year == year) else '',
        })
    available_months.reverse()

    can_manage_sponsor = current_user.pode_administrar
    trofeu = _resumo_trofeu(cidade)

    return render_template(
        'ranking/ranking.html',
        ranking_data=ranking_data,
        ranking_text=ranking_text,
        ranking_html=ranking_html,
        ranking_share_url=ranking_share_url,
        config=config,
        month=month, year=year,
        month_name=get_month_name(month),
        available_months=available_months,
        cidade=cidade,
        selected_year=year,
        selected_month=month,
        can_manage_sponsor=can_manage_sponsor,
        sponsor_name=sponsor_name,
        sponsor_logo_url=sponsor_logo_url,
        # Telefone e legenda do PRINCIPAL: faltavam nesta tela, entao o admin
        # nao via o texto que sai junto com a logo dele (o apoio ja mostrava).
        sponsor_telefone=(principal_do_mes.telefone if principal_do_mes else None),
        sponsor_legenda=(_legenda_completa(principal_do_mes)
                         if principal_do_mes else ''),
        sponsors_extra=sponsors_extra,
        goal_messages=goal_messages,
        # Conferência automática: de quantos em quantos dias (27/08/2026).
        conf_dias=_conferencia_dias(cidade),
        conf_forcada=_conferencia_forcada(cidade),
        conf_pode_mudar=current_user.pode_administrar,
        # As abas Mensal/Trimestral/Semestral/Anual (P-137-c, 25/09/2026).
        periodo='mes',
        trofeu=trofeu,
        premiado=(trofeu or {}).get('premiado', ''),
    )


def _conferencia_dias(cidade):
    """Nunca deixa a conferência derrubar a tela do ranking."""
    try:
        from app import conferenciaauto
        return conferenciaauto.frequencia(cidade)
    except Exception:
        return 4


def _conferencia_forcada(cidade):
    try:
        from app import conferenciaauto
        return conferenciaauto._forcada(cidade)
    except Exception:
        return False


@ranking_bp.route('/conferencia/proxima', methods=['POST'])
@login_required
@admin_required
def forcar_conferencia():
    """Pede a conferência na PRÓXIMA rodada, sem esperar o prazo.

    POR QUE SEPARADO DA FREQUÊNCIA (pedido do dono, 27/08/2026): quando se
    descobre que a coleta falhou, esperar quatro dias é esperar demais — mas
    ligar a conferência diária "só desta vez" é o caminho para esquecê-la
    ligada. Isto é um pedido pontual: some sozinho depois de atendido.
    """
    from app import conferenciaauto
    cidade = current_user.cidade_ativa
    ligar = (request.form.get('ligar') or '1') == '1'
    conferenciaauto.forcar_proxima(cidade, por=current_user.email, ligar=ligar)
    if ligar:
        flash('Certo. A conferência vai rodar na próxima vez que o ranking rodar '
              '(de manhã) e trazer o que faltou — depois disso o ritmo normal '
              'volta a valer.', 'success')
    else:
        flash('Pedido cancelado: a conferência volta a seguir só o ritmo normal.',
              'info')
    logger.warning('Conferencia de %s %s para a proxima rodada por %s', cidade,
                   'PEDIDA' if ligar else 'desmarcada', current_user.email)
    return redirect(request.referrer or url_for('ranking.current'))


@ranking_bp.route('/conferencia/frequencia', methods=['POST'])
@login_required
@admin_required
def definir_conferencia():
    """De quantos em quantos dias a conferência automática roda nesta cidade.

    POR QUE FICA AQUI, na tela do ranking (pedido do dono, 26/08/2026): é o
    ranking que fica errado quando a coleta perde link, então é aqui que se
    decide o quanto a rede aperta.
    """
    from app import conferenciaauto
    cidade = current_user.cidade_ativa
    try:
        dias = int((request.form.get('dias') or '').strip())
    except (TypeError, ValueError):
        dias = -1
    if not conferenciaauto.definir_frequencia(cidade, dias, por=current_user.email):
        flash('Escolha inválida para a conferência automática.', 'danger')
        return redirect(request.referrer or url_for('ranking.current'))

    if dias == 0:
        flash('Conferência automática DESLIGADA. O ranking passa a contar só o que '
              'for postado no grupo — se a coleta falhar, o link não entra.', 'warning')
    elif dias == 1:
        flash('Conferência automática ligada, TODO DIA. Use enquanto a coleta '
              'estiver falhando: os km sobem sozinhos e o atleta pode estranhar.',
              'success')
    else:
        flash('Conferência automática ligada, a cada %d dias.' % dias, 'success')
    logger.warning('Conferencia de %s ajustada para %d dia(s) por %s',
                   cidade, dias, current_user.email)
    return redirect(request.referrer or url_for('ranking.current'))


@ranking_bp.route('/parabens/marcar/<int:year>/<int:month>', methods=['POST'])
@login_required
@admin_required
def marcar_parabens(year, month):
    """Marca/desmarca um parabens como enviado (checkbox na tela do ranking)."""
    _ensure_schema_once()
    cidade = current_user.cidade_ativa
    data = request.get_json(silent=True) or {}
    chave = (data.get('chave_atleta') or '').strip()
    modalidade = (data.get('modalidade') or '').strip()
    enviado = bool(data.get('enviado'))
    if not chave or modalidade not in ('corrida_caminhada', 'ciclismo'):
        return jsonify(ok=False, error='Parametros invalidos.'), 400
    try:
        if enviado:
            db.session.execute(
                text("INSERT OR REPLACE INTO parabens_enviados "
                     "(cidade, ano, mes, chave_atleta, modalidade, enviado_em, enviado_por) "
                     "VALUES (:c, :a, :m, :k, :mod, :em, :por)"),
                {'c': cidade, 'a': year, 'm': month, 'k': chave, 'mod': modalidade,
                 'em': datetime.now(), 'por': current_user.email},
            )
        else:
            db.session.execute(
                text("DELETE FROM parabens_enviados WHERE cidade=:c AND ano=:a "
                     "AND mes=:m AND chave_atleta=:k AND modalidade=:mod"),
                {'c': cidade, 'a': year, 'm': month, 'k': chave, 'mod': modalidade},
            )
        db.session.commit()
        return jsonify(ok=True, enviado=enviado)
    except Exception as exc:
        db.session.rollback()
        logger.error('Erro ao marcar parabens: %s', exc, exc_info=True)
        return jsonify(ok=False, error='Falha ao gravar.'), 500


@ranking_bp.route('/marcar-copiado', methods=['POST'])
@login_required
def marcar_copiado():
    """O dono copiou o ranking para colar no grupo — isso conta como publicação.

    POR QUE ISTO EXISTE (09/09/2026)

    Até aqui só contava o que saía pela automação. Quem publicava à mão (copiar
    aqui, colar no WhatsApp) não deixava rastro nenhum, e o sistema seguia
    achando que o ranking não tinha saído. Consequência medida em 09/09: o
    ranking foi colado no grupo à 01:37, e às 09:53 o aviso do Windows acordou
    o dono dizendo que "o ranking das 05:30 não saiu".

    DUAS CAMADAS, DE PROPÓSITO. Copiar não é colar: o dono pode desistir no
    meio. Então aqui se registra a INTENÇÃO (`copiado_em`), que já é suficiente
    para calar o alarme falso; quem confirma de verdade é a rodada seguinte,
    que procura o cabeçalho do ranking na conversa do grupo. Marcar como
    publicado direto seria trocar um alarme falso por um silêncio falso — e
    silêncio é o pior desfecho (regra 10).
    """
    from app import secretstore
    from datetime import datetime as _dt

    cidade = current_user.cidade_ativa
    try:
        ano = int(request.form.get('ano') or 0)
        mes = int(request.form.get('mes') or 0)
    except (TypeError, ValueError):
        ano = mes = 0

    dados = {
        'copiado_em': _dt.now().isoformat(timespec='seconds'),
        'por': (getattr(current_user, 'email', '') or 'admin')[:120],
        'periodo': '%04d-%02d' % (ano, mes) if ano and mes else '',
        'confirmado_no_grupo': None,
    }
    secretstore.set_value('ranking_copiado', json.dumps(dados),
                          scope=cidade, updated_by=dados['por'])
    logger.info('Ranking copiado para publicação manual em %s por %s (%s)',
                cidade, dados['por'], dados['periodo'] or 'período atual')
    return jsonify(ok=True)


@ranking_bp.route('/get_ranking_text_json/<int:year>/<int:month>')
@login_required
@admin_required
def get_ranking_text_json(year, month):
    _ensure_schema_once()
    backup = MonthlyBackup.query.filter_by(
        cidade=current_user.cidade_ativa, mes=month, ano=year
    ).first()
    if backup and backup.texto_whatsapp:
        return jsonify(ranking_text=backup.texto_whatsapp)
    return jsonify(ranking_text='Nenhum ranking disponivel.')


@ranking_bp.route('/download_ranking/<int:year>/<int:month>')
@login_required
@admin_required
def download_ranking(year, month):
    _ensure_schema_once()
    backup = MonthlyBackup.query.filter_by(
        cidade=current_user.cidade_ativa, mes=month, ano=year
    ).first()
    if not backup:
        flash('Ranking nao encontrado.', 'error')
        return redirect(url_for('ranking.current'))

    return Response(
        backup.texto_whatsapp or '',
        mimetype='text/plain',
        headers={
            'Content-Disposition': (
                f'attachment;filename=ranking_{current_user.cidade_ativa}_'
                f'{get_month_name(month).lower()}_{year}.txt'
            )
        },
    )
