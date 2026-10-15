# -*- coding: utf-8 -*-
"""Nome de atleta: o que e' nome de gente e o que e' enfeite.

POR QUE ESTE ARQUIVO EXISTE (29/08/2026, incidente dos 266 km).
A regra de "isto nao e' nome" (tipo sanguineo, emoji, enfeite) morava so' no
`ranking.py` e so' rodava NA HORA DE EXIBIR. Quem GRAVA o nome
(`processing._compact_athlete_name`) nao conhecia a regra: pegava o primeiro e
o ULTIMO pedaco do nome, e quando o ultimo pedaco era um emoji ele expulsava o
sobrenome de verdade. "Maykon Abreu 🅰️➕" virava "Maykon 🅰️➕" -- e era esse
nome que ia para o banco, para sempre.

Agora a regra e' uma so' e os dois lados leem daqui.
"""
import re

# Tipo sanguineo que o pessoal mais experiente poe no fim do nome do Strava
# (Gabi, Alex...). Nao e' sobrenome e nao pode ir para o ranking.
TIPO_SANGUINEO_RE = re.compile(
    r'^(?:a|b|ab|o)\s*(?:\+|-|positivo|negativo|pos|neg)$', re.IGNORECASE)
# Emojis de tipo sanguineo (🅰️ 🅱️ 🆎 🅾️) e qualquer pedaco que so tenha
# simbolo/emoji -- nome de gente tem letra.
SEM_LETRA_RE = re.compile(r'^[^0-9A-Za-zÀ-ÿ]+$')

# Particulas que ligam nome mas nunca servem de sobrenome sozinhas:
# "Maria de Souza" mostra "Maria Souza", nunca "Maria de". Regra do dono,
# 29/08/2026. "Sa" e "Le" NAO entram aqui de proposito -- sao sobrenome real,
# e por isso o corte por tamanho e' de UMA letra, nao de duas.
PARTICULAS = ('de', 'da', 'do', 'das', 'dos', 'e', 'di', 'du', 'del', 'dos')


def _so_letras(parte):
    return re.sub(r'[^0-9A-Za-zÀ-ÿ]', '', (parte or ''))


def e_ruido_de_nome(parte):
    """A parte do nome e' tipo sanguineo, emoji ou enfeite?"""
    limpo = (parte or '').strip()
    if not limpo:
        return True
    if TIPO_SANGUINEO_RE.match(limpo):
        return True
    if SEM_LETRA_RE.match(limpo):
        return True
    # 'A+' colado, '🅰️', 'O-' -- sobra 1-2 caracteres sem valor de sobrenome.
    sem_simbolo = _so_letras(limpo)
    if not sem_simbolo:
        return True
    if len(sem_simbolo) <= 2 and TIPO_SANGUINEO_RE.match(sem_simbolo):
        return True
    return False


def e_particula(parte):
    """'de', 'da', 'e'... -- liga nome, mas nao serve de sobrenome sozinha."""
    limpo = _so_letras(parte).lower()
    if not limpo:
        return True
    if limpo in PARTICULAS:
        return True
    # Inicial solta ("Maria S. Oliveira"): nao identifica ninguem no ranking.
    return len(limpo) <= 1


def limpar_bordas(parte):
    """Tira enfeite grudado no comeco e no fim, sem tocar no miolo.

    "Abreu\U0001F985" -> "Abreu"; "@Alex" -> "Alex". Mas "D'Avila" e
    "Silva-Souza" ficam inteiros: o corte e so nas pontas.
    """
    return re.sub(r'^[^0-9A-Za-z\u00c0-\u00ff]+|[^0-9A-Za-z\u00c0-\u00ff]+$',
                  '', parte or '')


def partes_uteis(nome):
    """Os pedacos do nome que sao NOME: sem emoji, sem tipo sanguineo, sem '@'."""
    limpo = re.sub(r'\s+', ' ', (nome or '').strip())
    if not limpo:
        return []
    partes = []
    for p in limpo.split(' '):
        if e_ruido_de_nome(p):
            continue
        # O emoji GRUDADO no sobrenome tambem sai (29/08/2026): o Strava
        # devolve last_name="Abreu\U0001F985 \U0001F1E7\U0001F1F7 | A+", e sem
        # isto o ranking mostrava "Abreu\U0001F985".
        p = limpar_bordas(p)
        if p:
            partes.append(p)
    return partes


def nome_para_gravar(*pedacos):
    """Primeiro + ultimo nome, ja SEM o enfeite -- e' o que vai para o banco.

    Substitui o antigo `_compact_athlete_name`, que escolhia o ultimo pedaco
    bruto e por isso gravava o emoji no lugar do sobrenome.
    """
    bruto = ' '.join(str(p or '').strip() for p in pedacos if str(p or '').strip())
    limpo = re.sub(r'\s+', ' ', bruto).strip()
    if not limpo or limpo == 'Desconhecido':
        return 'Desconhecido'
    partes = partes_uteis(limpo)
    if not partes:
        # So havia enfeite: melhor guardar o que veio do que perder a pessoa.
        tokens = limpo.split(' ')
        return tokens[0] if len(tokens) == 1 else '%s %s' % (tokens[0], tokens[-1])
    if len(partes) == 1:
        return partes[0]
    # O ultimo pedaco UTIL -- nunca o emoji que estava depois dele.
    sobrenomes = [p for p in partes[1:] if not e_particula(p)] or partes[1:]
    return '%s %s' % (partes[0], sobrenomes[-1])
