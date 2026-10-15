# -*- coding: utf-8 -*-
"""Textos da página inicial, editáveis pela tela — sem mexer em código.

POR QUE ISTO EXISTE (09/09/2026, pedido do dono)

Trocar uma frase da porta de entrada do projeto exigia editar template em
produção, e editar template em produção é o tipo de coisa que derruba o site
por uma vírgula. O dono precisa poder ajustar o texto que fala com a cidade
dele sem depender de ninguém e sem risco.

O QUE PODE E O QUE NÃO PODE

Só formatação: **negrito**, *itálico*, sublinhado e MAIÚSCULA. Cor, tamanho e
fonte não entram — quem escolhe isso é o site, e deixar cada trecho com uma
cor faria a página parecer remendada. Também não entra HTML solto: o que chega
da tela é limpo aqui dentro, tag por tag, e o que não estiver na lista vira
texto comum. Página pública que aceita HTML de fora é convite para injeção.

COMO O TEMPLATE USA

    {{ texto('home.frase', 'Mover o corpo para mover a mente') }}

O segundo argumento é o que aparece enquanto ninguém editou nada. Isso importa:
a página nunca fica em branco porque a tabela está vazia, e o texto de fábrica
continua versionado no Git, onde dá para ver o que mudou.
"""
import logging
import re
from datetime import datetime

from markupsafe import Markup, escape
from sqlalchemy import text as sa_text

from app import db

logger = logging.getLogger(__name__)

_PRONTO = False

# As únicas marcações aceitas. Sem atributos, sem estilo, sem link — o link
# muda o destino da página e é o caminho mais curto para uma armadilha.
_TAGS_PERMITIDAS = ('strong', 'em', 'u', 'br')

_RE_TAG = re.compile(r'</?([a-zA-Z0-9]+)[^>]*>')


def ensure_schema():
    """Cria a tabela na primeira chamada. Idempotente (convenção do projeto)."""
    global _PRONTO
    if _PRONTO:
        return
    try:
        with db.engine.begin() as conn:
            conn.execute(sa_text("""
                CREATE TABLE IF NOT EXISTS textos_site (
                    chave       VARCHAR(80) NOT NULL,
                    cidade      VARCHAR(100) NOT NULL DEFAULT '',
                    conteudo    TEXT NOT NULL DEFAULT '',
                    atualizado_por VARCHAR(120) DEFAULT '',
                    atualizado_em  DATETIME,
                    PRIMARY KEY (chave, cidade)
                )
            """))
        _PRONTO = True
    except Exception as exc:
        logger.warning('textos_site: não consegui preparar a tabela (%s)', exc)


def limpar(bruto):
    """Deixa só a formatação permitida. O resto vira texto comum.

    Não é "confiar no admin": é não deixar um erro de copiar e colar (um
    trecho de site, um script) virar problema numa página que qualquer pessoa
    abre. Escapa tudo primeiro e devolve as tags permitidas depois — nessa
    ordem, porque o contrário deixa passar o que estiver dentro do que sobrou.
    """
    if not bruto:
        return ''
    texto = escape(str(bruto).strip())          # tudo vira texto
    for tag in _TAGS_PERMITIDAS:                # e só estas voltam a ser tag
        texto = texto.replace('&lt;%s&gt;' % tag, '<%s>' % tag)
        texto = texto.replace('&lt;/%s&gt;' % tag, '</%s>' % tag)
        texto = texto.replace('&lt;%s/&gt;' % tag, '<%s>' % tag)
    return str(texto)


def obter(chave, padrao='', cidade=''):
    """O texto gravado, ou o padrão de fábrica quando ninguém editou.

    Nunca levanta erro: página pública com o texto de fábrica é melhor que
    página pública quebrada.
    """
    ensure_schema()
    try:
        linha = db.session.execute(sa_text(
            'SELECT conteudo FROM textos_site WHERE chave = :k AND cidade = :c'),
            {'k': chave, 'c': cidade or ''}).fetchone()
        if linha and (linha[0] or '').strip():
            return Markup(linha[0])
    except Exception as exc:
        logger.debug('textos_site: leitura de %r falhou (%s)', chave, exc)
    return Markup(escape(padrao)) if padrao else Markup('')


def gravar(chave, conteudo, por='', cidade=''):
    """Guarda o texto já limpo. Devolve True quando gravou."""
    ensure_schema()
    limpo = limpar(conteudo)
    try:
        db.session.execute(sa_text(
            'INSERT INTO textos_site (chave, cidade, conteudo, atualizado_por, '
            'atualizado_em) VALUES (:k, :c, :v, :p, :e) '
            'ON CONFLICT(chave, cidade) DO UPDATE SET conteudo = :v, '
            'atualizado_por = :p, atualizado_em = :e'),
            {'k': chave, 'c': cidade or '', 'v': limpo, 'p': (por or '')[:120],
             'e': datetime.now()})
        db.session.commit()
        return True
    except Exception as exc:
        db.session.rollback()
        logger.error('textos_site: não consegui gravar %r (%s)', chave, exc)
        return False


def apagar(chave, cidade=''):
    """Volta ao texto de fábrica (apaga o personalizado)."""
    ensure_schema()
    try:
        db.session.execute(sa_text(
            'DELETE FROM textos_site WHERE chave = :k AND cidade = :c'),
            {'k': chave, 'c': cidade or ''})
        db.session.commit()
        return True
    except Exception:
        db.session.rollback()
        return False


# ---------------------------------------------------------------------------
# O CATÁLOGO — é daqui que a tela de edição se monta
# ---------------------------------------------------------------------------
# Trecho novo na home entra AQUI, com o texto de fábrica. Sem isto a tela não
# saberia o que oferecer para editar, e cada um inventaria uma chave.
CATALOGO = (
    ('home.frase', 'A frase de efeito, logo abaixo do nome',
     'Mover o corpo para mover a mente.'),
    ('home.subtitulo', 'O parágrafo de apresentação, abaixo da frase',
     'Moradores da cidade postam seus treinos num grupo de WhatsApp. '
     'O sistema soma os quilômetros e publica o ranking do mês.'),
    ('home.chamada_participar', 'A chamada do bloco "participe"',
     'Aberto a qualquer pessoa da cidade, sem custo'),
    ('home.rodape', 'O texto do rodapé da página inicial', ''),
    # 19/09/2026 (item 74, pedido do dono): os títulos das seções e os textos
    # de explicação também se editam pela tela. Número, nome e tabela NÃO
    # entram aqui — eles vêm do banco e mudar na mão seria mentir na porta de
    # entrada (regra 22).
    ('home.apresentacao', 'O parágrafo de apresentação, no topo',
     'Atividade física e educação nutricional para o bem-estar da comunidade. '
     'Gratuito, aberto a qualquer pessoa da sua respectiva cidade.'),
    ('home.titulo_tabela', 'Título da tabela de quilômetros',
     'Quilômetros do projeto, ano a ano'),
    ('home.titulo_historia', 'Título do bloco da história',
     'Como isto começou'),
    ('home.titulo_construiu', 'Título do bloco de quem construiu',
     'Quem construiu isto (Palmeirópolis)'),
    ('home.titulo_frente', 'Título do bloco de quem está à frente',
     'Quem está à frente hoje'),
    ('home.titulo_faz_acontecer', 'Título do bloco de quem faz acontecer',
     'Quem faz acontecer'),
    ('home.titulo_cidades', 'Título do bloco das cidades',
     'Onde o projeto acontece'),
    ('home.titulo_por_pessoa', 'Título da tabela de quilômetros por pessoa',
     'Quilômetros por pessoa'),
    ('home.titulo_apoie', 'Título do bloco "apoie o projeto"',
     'Apoie o projeto'),
    ('home.titulo_patrocinadores', 'Título do bloco dos patrocinadores',
     'Quem apoia o projeto'),
)
