# app/cityrename.py
# -*- coding: utf-8 -*-
"""Renomear uma cidade sem cirurgia manual no banco.

O nome da cidade e a chave de tenancy do sistema: ele esta repetido em todas
as tabelas de dados e no caminho das logos de patrocinador. Trocar esse nome
na unha significava um UPDATE por tabela e o risco de esquecer alguma.

Este modulo concentra a operacao:

- descobre SOZINHO toda tabela que tenha coluna "cidade" (o schema pode
  crescer que nada fica para tras);
- valida antes de escrever (origem existe, destino livre, nenhum
  processamento em andamento nas cidades envolvidas);
- escreve tudo em UMA transacao;
- move a pasta de logos so depois do commit.

Os links publicos NAO quebram: /r/<slug> resolve pelo slug e regenera o token
a partir da cidade gravada na linha, que e atualizada junto.

Existe um gemeo offline em scripts/renomear_cidades.py, para o caso de ser
preciso renomear com a aplicacao fora do ar. Os dois fazem a mesma coisa; este
aqui e o caminho normal, porque roda dentro do processo que ja e dono do banco.
"""
import logging
import shutil
from pathlib import Path

from sqlalchemy import inspect, text

from app import db

logger = logging.getLogger(__name__)

LIMITE_NOME = 100


def _raiz_projeto():
    return Path(__file__).resolve().parents[1]


def _slug(cidade):
    from app.blueprints.ranking import _slugify_city
    return _slugify_city(cidade)


def tabelas_com_cidade():
    """Toda tabela do banco que guarda o nome da cidade."""
    inspetor = inspect(db.engine)
    tabelas = []
    for tabela in inspetor.get_table_names():
        colunas = {coluna['name'] for coluna in inspetor.get_columns(tabela)}
        if 'cidade' in colunas:
            tabelas.append(tabela)
    return sorted(tabelas)


def contagem(cidade, tabelas=None):
    """Quantas linhas cada tabela tem para essa cidade (so as que tem alguma)."""
    total = {}
    for tabela in (tabelas or tabelas_com_cidade()):
        n = db.session.execute(
            text(f'SELECT COUNT(*) FROM "{tabela}" WHERE cidade = :cidade'),
            {'cidade': cidade},
        ).scalar() or 0
        if n:
            total[tabela] = n
    return total


def cidades_existentes():
    """Nomes de cidade em uso, vindos da config e de qualquer tabela de dados."""
    nomes = set()
    for tabela in tabelas_com_cidade():
        for (valor,) in db.session.execute(
            text(f'SELECT DISTINCT cidade FROM "{tabela}"')
        ):
            if valor:
                nomes.add(valor)
    return sorted(nomes)


def validar(origem, destino):
    """Lista de impedimentos. Vazia significa que pode renomear."""
    problemas = []
    origem = (origem or '').strip()
    destino = (destino or '').strip()

    if not origem:
        problemas.append('Informe a cidade que sera renomeada.')
    if not destino:
        problemas.append('Informe o novo nome da cidade.')
    if not problemas and origem == destino:
        problemas.append('O novo nome e igual ao atual.')
    if destino and len(destino) > LIMITE_NOME:
        problemas.append(f'O novo nome passa de {LIMITE_NOME} caracteres.')
    if problemas:
        return problemas

    existentes = cidades_existentes()
    if origem not in existentes:
        problemas.append(f'A cidade "{origem}" nao existe no sistema.')
    if destino in existentes:
        problemas.append(
            f'Ja existe uma cidade chamada "{destino}". Renomeie ou remova essa '
            f'primeira, senao os dados das duas ficariam misturados.'
        )

    # Um processamento em andamento continuaria gravando com o nome antigo.
    try:
        from app.blueprints.upload import _has_active_job_for_city
        for cidade in (origem, destino):
            if _has_active_job_for_city(cidade):
                problemas.append(
                    f'Ha um processamento em andamento em "{cidade}". '
                    f'Espere terminar antes de renomear.'
                )
    except Exception as exc:
        logger.debug('Nao foi possivel checar jobs ativos: %s', exc)

    return problemas


def _mover_logos(origem, destino):
    """Acompanha a troca no disco: uploads/sponsors/<slug>. Nunca levanta."""
    try:
        base = _raiz_projeto() / 'uploads' / 'sponsors'
        de = base / _slug(origem)
        para = base / _slug(destino)
        if not de.is_dir():
            return ''
        if para.exists():
            return (f'A pasta de logos "{para.name}" ja existia; as logos de '
                    f'"{origem}" continuam em "{de.name}".')
        shutil.move(str(de), str(para))
        logger.info('Logos de patrocinador movidas: %s -> %s', de.name, para.name)
        return ''
    except Exception as exc:
        logger.error('Falha ao mover a pasta de logos: %s', exc, exc_info=True)
        return (f'Os dados foram renomeados, mas a pasta de logos nao pode ser '
                f'movida ({exc}). Mova a mao em uploads/sponsors.')


def renomear(origem, destino, autor=None):
    """Renomeia a cidade em todas as tabelas, numa transacao unica.

    Devolve {'linhas': {tabela: n}, 'total': n, 'aviso': str}.
    Levanta ValueError se a validacao reprovar.
    """
    origem = (origem or '').strip()
    destino = (destino or '').strip()
    problemas = validar(origem, destino)
    if problemas:
        raise ValueError(' '.join(problemas))

    tabelas = tabelas_com_cidade()
    linhas = {}
    try:
        for tabela in tabelas:
            resultado = db.session.execute(
                text(f'UPDATE "{tabela}" SET cidade = :novo WHERE cidade = :velho'),
                {'novo': destino, 'velho': origem},
            )
            if resultado.rowcount:
                linhas[tabela] = resultado.rowcount
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.error('Renomeacao de cidade revertida: %s -> %s', origem, destino,
                     exc_info=True)
        raise

    total = sum(linhas.values())
    logger.warning('Cidade renomeada por %s: "%s" -> "%s" (%s linhas em %s tabelas)',
                   autor or 'desconhecido', origem, destino, total, len(linhas))
    aviso = _mover_logos(origem, destino)
    # O cofre (instance/secrets.json) guarda token de automacao, cookie do
    # Strava e saude da sessao por CIDADE. Como e um arquivo (nao tabela), o
    # UPDATE acima nao o alcanca -- mover o escopo aqui evita o token/cookie
    # ficarem "presos" no nome antigo (bug real apos o rename de 23/07).
    try:
        from app import secretstore
        secretstore.rename_scope(origem, destino)
    except Exception as exc:
        logger.error('Falha ao migrar segredos no rename de cidade: %s', exc)
    return {'linhas': linhas, 'total': total, 'aviso': aviso}
