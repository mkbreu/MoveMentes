# -*- coding: utf-8 -*-
"""Quem postou o link e o dono do perfil — a agenda telefonica sai daqui.

POR QUE ESTE MODULO EXISTE
A deducao de telefone lia `activities.telefone_whatsapp`. Essa coluna esta
corrompida desde a ingestao, e a prova e direta: as 8 atividades do "Fulano 0+"
foram postadas no grupo por um unico numero (+55 DD XXXX-XXXX) e o banco gravou
SEIS numeros diferentes para elas (84799715, 84270601, 84439288, 84361365,
84083872, 84229961). Conferindo 356 atividades contra o TXT cru: 85 nao batem
com ninguem que postou. Os backups de 23/07 e 29/07 ja estavam assim — nasceu
errada, ninguem estragou depois.

A regra dos 5 ultimos links com voto majoritario (admin.py) sempre esteve
CERTA. Ela so estava votando sobre lixo. Por isso a agenda saiu embaralhada:
metade acertava, metade trocava as pessoas de lugar. O conserto nao e a regra,
e a fonte.

A FONTE CERTA E O TXT
No arquivo da conversa, a linha diz quem falou e o que ele mandou. Se o link
esta na linha do fulano, o fulano postou. Guardamos isso aqui, uma vez, e a
tela passa a ler daqui — 600 MB de TXT nao se releem a cada clique.

O QUE VALE E O QUE NAO VALE
- **Telefone** vale: e o remetente identificado por numero.
- **Nome salvo na agenda** ("Fulana Padaria") identifica a PESSOA, mas nao da o
  numero dela. Guardamos assim mesmo — serve para saber que o link teve dono, e
  a tela mostra que falta so o numero.
- **Id do grupo** (55DDXXXXXXXXX-XXXXXXXXXX) nao e ninguem. Ate 05/08 a automacao
  escrevia o id do grupo no lugar do participante em todo TXT que gerava; essas
  linhas nao provam nada e sao descartadas aqui.
- **LID** (`lid:166881138856128`) e pessoa, mas o numero so sai do WAHA
  (`check-exists`, regra 22-C). Guardamos para quando houver por onde resolver.

Regra 4-A do CLAUDE.md continua valendo: **isto NAO conta km.** Quem conta km e
o scraping da pagina do Strava. Aqui so se decide para qual telefone a medalha
e avisada.
"""
import logging
import os
import re
from datetime import datetime, timedelta

from app import db
from app import telefone as tel

logger = logging.getLogger(__name__)

# As mesmas regex do parser de upload (processing.py). Ficam repetidas de
# proposito: importar processing.py aqui traria o Strava, o throttle e o
# anticheat junto, so para ler uma linha de texto.
WA_LINE_RE = re.compile(
    r'^\[?(\d{1,2}/\d{1,2}/\d{4})[, ]+\d{1,2}:\d{2}(?::\d{2})?'
    r'(?:\]?\s*[-–]\s*|\]\s*)(.+?):\s*(.*)'
)
WA_PHONE_RE = re.compile(r'^\+?55[\s\-\(]?\d{2}[\s\-\)]?\s?\d{4,5}[\s\-]?\d{4}$')
STRAVA_DIRECT_RE = re.compile(
    r'https?://(?:www\.)?strava\.com/activities/\d+(?:/[\w-]+)*(?:\?[^\s<>"\]]+)?',
    re.IGNORECASE)
STRAVA_SHORT_RE = re.compile(r'https?://strava\.app\.link/\S+', re.IGNORECASE)
_SO_DIGITOS = re.compile(r'\D')

# Quantos envios recentes decidem. Cinco foi a regra que o dono definiu: um
# envio so deixa a deducao refem do dia em que o vizinho postou por voce.
JANELA = 5

FONE, NOME, LID = 'fone', 'nome', 'lid'


def ensure_schema():
    """Cria as tabelas da prova. Idempotente (SQLite nao tem ADD COLUMN IF NOT EXISTS)."""
    db.session.execute(db.text("""
        CREATE TABLE IF NOT EXISTS atividade_remetente (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cidade VARCHAR(100) NOT NULL,
            id_atividade VARCHAR(50) NOT NULL,
            id_usuario_strava VARCHAR(50) NOT NULL,
            tipo VARCHAR(10) NOT NULL,
            valor VARCHAR(120) NOT NULL,
            quando DATE,
            arquivo VARCHAR(255),
            criado_em DATETIME
        )
    """))
    db.session.execute(db.text(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_remetente_unico "
        "ON atividade_remetente (cidade, id_atividade, tipo, valor)"))
    db.session.execute(db.text(
        "CREATE INDEX IF NOT EXISTS idx_remetente_perfil "
        "ON atividade_remetente (cidade, id_usuario_strava, quando)"))
    db.session.execute(db.text("""
        CREATE TABLE IF NOT EXISTS txt_varrido (
            arquivo VARCHAR(255) PRIMARY KEY,
            tamanho INTEGER,
            achados INTEGER,
            varrido_em DATETIME
        )
    """))
    # E-mail do atleta. SQLite nao tem ADD COLUMN IF NOT EXISTS, entao pergunta
    # antes (convencao do projeto).
    colunas = {linha[1] for linha in db.session.execute(
        db.text('PRAGMA table_info(atletas)')).fetchall()}
    if 'email' not in colunas:
        db.session.execute(db.text('ALTER TABLE atletas ADD COLUMN email VARCHAR(150)'))
    # Quem digitou o numero na mao, e quando. E o que faz a deducao automatica
    # respeitar o trabalho de quem conferiu pessoa por pessoa.
    if 'telefone_confirmado_em' not in colunas:
        db.session.execute(db.text(
            'ALTER TABLE atletas ADD COLUMN telefone_confirmado_em DATETIME'))
    if 'telefone_confirmado_por' not in colunas:
        db.session.execute(db.text(
            'ALTER TABLE atletas ADD COLUMN telefone_confirmado_por VARCHAR(120)'))
    db.session.execute(db.text("""
        CREATE TABLE IF NOT EXISTS telefone_historico (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lote VARCHAR(40) NOT NULL,
            strava_id VARCHAR(50) NOT NULL,
            nome VARCHAR(100),
            telefone_antes VARCHAR(20),
            quando DATETIME,
            por VARCHAR(120)
        )
    """))
    db.session.execute(db.text(
        'CREATE INDEX IF NOT EXISTS idx_telefone_historico_lote '
        'ON telefone_historico (lote)'))
    db.session.commit()


def gravar_telefone(strava_id, numero, email=None, por='admin'):
    """Grava o telefone (e o e-mail) de UM atleta. (mensagem, categoria).

    UMA LINHA, UM COMMIT — e nao e preciosismo. Este banco devolve
    `disk I/O error` em gravacao grande: aconteceu em 16-17/06 (78 vezes), em
    29/07 na tentativa anterior de arrumar a agenda (52) e em 06/08 (23). A
    transacao grande falha inteira, e pior: envenena a conexao do worker, e
    dali em diante ate `SELECT` devolve erro — foi assim que a tela passou a
    dar 500 depois de "10 gravados" que nao gravaram nada.

    Se o numero ja for de outro atleta, ele MUDA DE DONO: o cadastro so aceita
    um dono por numero, e quem esta corrigindo na mao ja sabe de quem e. Travar
    obrigaria a apagar de um lugar e voltar no outro, duas idas para uma
    correcao so.
    """
    from app.models import Atleta

    atleta = Atleta.query.filter_by(strava_id=str(strava_id)).first()
    if not atleta:
        return 'Atleta não encontrado.', 'warning'

    numero_limpo = tel.normalizar(numero) if (numero or '').strip() else None
    if (numero or '').strip() and not numero_limpo:
        return ('Não entendi o telefone "%s". Escreva com DDD, sem o +55: '
                'DD 9XXXX-XXXX.' % numero, 'warning')

    aviso = ''
    try:
        if numero_limpo:
            anterior = Atleta.query.filter(
                Atleta.telefone_normalizado == numero_limpo,
                Atleta.strava_id != atleta.strava_id).first()
            if anterior:
                aviso = (' Esse número estava no cadastro de %s e saiu de lá.'
                         % (anterior.nome_atual or 'outro atleta'))
                anterior.telefone_normalizado = None
                db.session.commit()

        atleta.telefone_normalizado = numero_limpo
        # CARIMBO DE GENTE. A partir daqui a deducao automatica nao mexe mais
        # neste numero — ela pode discordar, e a tela mostra a discordancia,
        # mas quem conferiu pessoa por pessoa nao perde o trabalho no proximo
        # clique. Apagar tambem e decisao de gente: o carimbo fica.
        atleta.telefone_confirmado_em = datetime.utcnow()
        atleta.telefone_confirmado_por = por
        if email is not None:
            atleta.email = (email or '').strip() or None
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning('nao gravei o telefone de %s: %s', strava_id, exc)
        return ('Não consegui gravar: %s. Tente de novo — este banco às vezes '
                'recusa a escrita e a segunda tentativa passa.' % str(exc)[:120],
                'danger')

    return ('%s: %s.%s' % (atleta.nome_atual or 'Atleta',
                           tel.bonito(numero_limpo) if numero_limpo else 'telefone apagado',
                           aviso), 'success')


def agenda(cidade):
    """A agenda inteira da cidade: atletas e administradores, em uma lista só.

    Uma tabela, nao seis. O admin abre a tela para achar uma pessoa e corrigir
    o numero dela — todo o resto era explicacao no caminho.

    QUANDO A CONVERSA DISCORDA DO CADASTRO, a linha mostra o que a conversa diz
    e um botao para aceitar. Nao troca sozinho (isso desfaria a conferencia
    feita na mao) e nao esconde (o que a pessoa digitou tambem erra). Quem
    decide continua sendo gente, mas gente olhando a prova.
    """
    from app.models import Atleta, Activity, Usuario

    try:
        levantamento = decidir(cidade)
        # `confere` entra junto de proposito: o aviso de troca de numero vale
        # tambem para quem esta com o numero "certo" pela maioria — e
        # justamente ali que a troca recente se esconde.
        sugestoes = {i['strava_id']: i for i in
                     (levantamento['fortes'] + levantamento['fracas']
                      + levantamento['confere'])}
    except Exception as exc:
        logger.warning('não consegui levantar as sugestões: %s', exc)
        sugestoes = {}

    atletas = (Atleta.query
               .join(Activity, Activity.id_usuario_strava == Atleta.strava_id)
               .filter(Activity.cidade == cidade)
               .distinct().all())

    linhas = []
    for atleta in atletas:
        numero = tel.normalizar(atleta.telefone_normalizado)
        sugestao = sugestoes.get(atleta.strava_id)
        propoe = sugestao['telefone'] if sugestao else None
        linhas.append({
            'tipo': 'atleta',
            'chave': atleta.strava_id,
            'nome': atleta.nome_atual or 'Atleta',
            'telefone': numero,
            'telefone_bruto': atleta.telefone_normalizado,
            'bonito': tel.bonito(atleta.telefone_normalizado) if atleta.telefone_normalizado else '',
            'email': getattr(atleta, 'email', None),
            'torto': bool(atleta.telefone_normalizado) and atleta.telefone_normalizado != numero,
            'sugestao': propoe if propoe and propoe != numero else None,
            'sugestao_bonita': tel.bonito(propoe) if propoe and propoe != numero else '',
            'sugestao_envios': sugestao['envios'] if sugestao else 0,
            'troca': (sugestao or {}).get('troca_recente'),
            'troca_bonita': tel.bonito((sugestao or {}).get('troca_recente', {}).get('telefone'))
                            if (sugestao or {}).get('troca_recente') else '',
            'confirmado': getattr(atleta, 'telefone_confirmado_em', None),
        })

    for usuario in Usuario.query.filter_by(cidade=cidade).all():
        numero = tel.normalizar(getattr(usuario, 'telefone', None))
        linhas.append({
            'tipo': 'adm',
            'chave': 'u%d' % usuario.id,
            'nome': usuario.nome if hasattr(usuario, 'nome') else usuario.email,
            'telefone': numero,
            'telefone_bruto': getattr(usuario, 'telefone', None),
            'bonito': tel.bonito(getattr(usuario, 'telefone', None) or ''),
            'email': usuario.email,
            'torto': False,
            'sugestao': None, 'sugestao_bonita': '', 'sugestao_envios': 0,
            'confirmado': None,
        })

    # Quem NAO tem numero vai para o fim. Sem isso, as ~20 linhas vazias ficam
    # espalhadas no meio e quem esta procurando alguem passa por elas o tempo
    # todo. Administradores primeiro, depois quem tem numero, depois os vazios.
    return sorted(linhas, key=lambda l: (l['tipo'] != 'adm',
                                         not l['telefone'],
                                         (l['nome'] or '').lower()))


def classificar(remetente):
    """(tipo, valor) do remetente da linha, ou None quando nao identifica ninguem."""
    bruto = (remetente or '').strip()
    if not bruto:
        return None
    if bruto.lower().startswith('lid:'):
        return (LID, _SO_DIGITOS.sub('', bruto))
    digitos = _SO_DIGITOS.sub('', bruto)
    # Id de grupo: 55DDXXXXXXXXX-XXXXXXXXXX. Ate 05/08 a automacao escrevia isto
    # no lugar do participante — nao e pessoa, e nao pode virar telefone.
    if '-' in bruto and len(digitos) > 14:
        return None
    parece_numero = WA_PHONE_RE.match(bruto) or (
        len(digitos) in (12, 13) and not re.search(r'[A-Za-zÀ-ÿ]', bruto))
    if parece_numero:
        canonico = tel.normalizar(bruto)
        return (FONE, canonico) if canonico else None
    return (NOME, bruto[:120])


def _mapa_shortlinks():
    """{link curto: id da atividade} — o cache que o processamento ja alimenta."""
    try:
        linhas = db.session.execute(db.text(
            'SELECT short_url, activity_id FROM strava_link_cache')).fetchall()
    except Exception as exc:
        logger.warning('sem cache de shortlink: %s', exc)
        return {}
    return {str(c).strip(): str(a) for c, a in linhas if c and a}


def _mapa_atividades():
    """{id da atividade: [(cidade, perfil)]} — uma atividade existe em 1+ cidades."""
    mapa = {}
    linhas = db.session.execute(db.text(
        'SELECT id_atividade, cidade, id_usuario_strava FROM activities '
        'WHERE id_usuario_strava IS NOT NULL AND id_usuario_strava != ""')).fetchall()
    for aid, cidade, perfil in linhas:
        mapa.setdefault(str(aid), []).append((cidade, str(perfil)))
    return mapa


def _data_iso(texto):
    for formato in ('%d/%m/%Y', '%d/%m/%y'):
        try:
            return datetime.strptime(texto, formato).date().isoformat()
        except (ValueError, TypeError):
            continue
    return None


def varrer_arquivo(caminho, curtos, atividades):
    """Le um TXT e devolve as provas encontradas. Nao grava."""
    achados = []
    atual = None
    quando = None
    try:
        with open(caminho, 'r', encoding='utf-8', errors='replace') as arq:
            for linha in arq:
                linha = linha.strip()
                if not linha:
                    continue
                casou = WA_LINE_RE.match(linha)
                if casou:
                    quando = _data_iso(casou.group(1))
                    atual = classificar(casou.group(2))
                    corpo = casou.group(3)
                else:
                    # Continuacao de mensagem de varias linhas: o link vem na
                    # linha de baixo e o remetente continua sendo o mesmo.
                    corpo = linha
                if atual is None:
                    continue
                ids = []
                for achado in STRAVA_DIRECT_RE.finditer(corpo):
                    numero = re.search(r'/activities/(\d+)', achado.group(0))
                    if numero:
                        ids.append(numero.group(1))
                for achado in STRAVA_SHORT_RE.finditer(corpo):
                    curto = achado.group(0).strip().rstrip('.,;)]}>')
                    if curto in curtos:
                        ids.append(curtos[curto])
                for aid in ids:
                    for cidade, perfil in atividades.get(aid, ()):
                        achados.append((cidade, aid, perfil, atual[0], atual[1], quando))
    except Exception as exc:
        logger.warning('nao consegui ler %s: %s', caminho, exc)
    return achados


MESES_DE_LEITURA = 2

# Acima disto o arquivo e uma exportacao cumulativa do WhatsApp (o historico
# inteiro do grupo, ~5 MB). Abaixo, e um incremento: a automacao gera um por
# rodada e o upload manual tambem.
EXPORTACAO_CUMULATIVA = 1024 * 1024


def _data_do_arquivo(nome, caminho):
    """A data do TXT sai do nome (20260704_035556_...); na falta dele, do disco."""
    casou = re.match(r'^(\d{4})(\d{2})(\d{2})_', nome)
    if casou:
        try:
            return datetime(int(casou.group(1)), int(casou.group(2)),
                            int(casou.group(3)))
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(os.path.getmtime(caminho))
    except OSError:
        return datetime.min


AMOSTRA_BYTES = 300 * 1024


def _remetentes_do_comeco(caminho, limite=AMOSTRA_BYTES):
    """Quem este arquivo conhece PELO NUMERO — lendo so o comeco.

    Nao interessa a mensagem, interessa o estilo: quem exportou tem uns
    contatos salvos e outros nao, e isso vale para o arquivo inteiro. Ler o
    comeco basta para saber de qual aparelho ele veio, por 300 KB em vez de
    5 MB.
    """
    achados = set()
    try:
        with open(caminho, 'r', encoding='utf-8', errors='replace') as arq:
            lido = 0
            for linha in arq:
                lido += len(linha)
                if lido > limite:
                    break
                casou = WA_LINE_RE.match(linha.strip())
                if not casou:
                    continue
                ident = classificar(casou.group(2))
                if ident and ident[0] == FONE:
                    achados.add(ident[1])
    except OSError:
        pass
    return achados


def escolher_arquivos(pasta, meses=MESES_DE_LEITURA, incluir_base=True):
    """Quais TXT valem a leitura. Devolve [(nome, tamanho)], do mais novo primeiro.

    DOIS REGIMES, decididos pelo dono em 06/08:
    - **Cadastro inicial** (`incluir_base=True`): a exportacao cumulativa maior
      entra como base, porque e a que tem mais conversa e mais contato salvo
      com nome atualizado.
    - **Do cadastro em diante** (`incluir_base=False`): so os TXT dos ultimos
      dois meses, **os dois tipos** — o que a automacao constroi e o que e
      carregado na tela de upload. Nao importa qual: importa ser recente.

    NAO SE LEEM OS 132 ARQUIVOS, E O MOTIVO E QUE NAO ADIANTA (decisao do dono,
    06/08). A exportacao manual do WhatsApp e CUMULATIVA: o **maior arquivo** ja
    traz a conversa inteira desde o comeco do grupo, e e tambem o que tem mais
    contatos salvos com nome atualizado. Os arquivos dos **ultimos dois meses**
    completam o que veio depois dele — venham da automacao ou do upload manual,
    tanto faz.
    Os outros 120 sao copias cumulativas mais velhas do mesmo conteudo: custam
    5 minutos de leitura e 600 MB de I/O para nao acrescentar nada. Medido em
    06/08: varredura completa = 1.118.446 linhas lidas para 16.300 provas
    distintas.
    """
    try:
        nomes = [n for n in os.listdir(pasta) if n.lower().endswith('.txt')]
    except OSError:
        return []

    corte = datetime.now() - timedelta(days=31 * meses)
    grandes, pequenos = [], {}
    for nome in nomes:
        caminho = os.path.join(pasta, nome)
        try:
            tamanho = os.path.getsize(caminho)
        except OSError:
            continue
        quando = _data_do_arquivo(nome, caminho)
        if tamanho >= EXPORTACAO_CUMULATIVA:
            grandes.append((quando, nome, tamanho))
        elif quando >= corte:
            # Incremento: a automacao gera um por rodada e o upload manual
            # tambem. Sao KB — le-se todos, e e neles que esta o que veio
            # depois da ultima exportacao grande.
            pequenos[nome] = tamanho

    escolhidos = dict(pequenos)
    grandes.sort(reverse=True)          # do mais novo para o mais velho
    if grandes:
        recente = grandes[0]
        if recente[0] >= corte or incluir_base:
            escolhidos[recente[1]] = recente[2]
    if incluir_base and grandes:
        # O CRITERIO NAO E TAMANHO, E QUEM O ARQUIVO CONHECE PELO NUMERO.
        # A exportacao sai do celular de alguem, e quem ele tem salvo na agenda
        # aparece pelo NOME — desses o arquivo nao da telefone nenhum. Dois
        # celulares diferentes salvam pessoas diferentes, entao o que um esconde
        # o outro mostra: em 06/08, a exportacao de 28/07 conhecia 48 atletas
        # pelo numero e 52 so pelo nome, e a de 14/04 (outro aparelho) trazia o
        # numero de 26 deles — Fulana Exemplo e "Fulana🥰" numa e 55DD9XXXXXXXX na
        # outra. Escolher "a maior" pegava a errada: as duas maiores eram do
        # mesmo aparelho.
        # Entao le-se so o comeco de cada exportacao para ver QUEM ela conhece
        # pelo numero, e fica quem acrescenta gente. As outras 80 sao o mesmo
        # aparelho repetido: 414 MB para nao mudar uma linha do resultado.
        conhecidos = set()
        for _q, nome, _t in [g for g in grandes if g[1] in escolhidos]:
            conhecidos |= _remetentes_do_comeco(os.path.join(pasta, nome))
        for quando, nome, tamanho in grandes:
            if nome in escolhidos:
                continue
            novos = _remetentes_do_comeco(os.path.join(pasta, nome)) - conhecidos
            if novos:
                escolhidos[nome] = tamanho
                conhecidos |= novos
    return sorted(escolhidos.items(), key=lambda item: item[0], reverse=True)


def varrer(pasta, limite_arquivos=None, refazer=False, meses=MESES_DE_LEITURA,
           incluir_base=True):
    """Varre os TXT que valem a leitura. Devolve o resumo.

    RESUMIVEL DE PROPOSITO: o site roda num DS215j de 512 MB. Cada chamada faz
    um pedaco e anota o que ja fez; a tela chama de novo ate acabar.
    """
    ensure_schema()
    curtos = _mapa_shortlinks()
    atividades = _mapa_atividades()

    ja_feitos = {}
    if not refazer:
        for nome, tamanho in db.session.execute(db.text(
                'SELECT arquivo, tamanho FROM txt_varrido')).fetchall():
            ja_feitos[nome] = tamanho

    # Do mais novo para o mais velho: o que decide e o envio recente, entao a
    # prova que importa aparece na primeira passada.
    candidatos = [nome for nome, _t in escolher_arquivos(pasta, meses, incluir_base)]
    if not candidatos:
        return {'erro': 'não achei TXT em %s' % pasta}

    feitos, gravados, restantes = 0, 0, 0
    for nome in candidatos:
        caminho = os.path.join(pasta, nome)
        try:
            tamanho = os.path.getsize(caminho)
        except OSError:
            continue
        if ja_feitos.get(nome) == tamanho:
            continue
        if limite_arquivos is not None and feitos >= limite_arquivos:
            restantes += 1
            continue

        achados = varrer_arquivo(caminho, curtos, atividades)
        # De 500 em 500. Uma exportacao grande da ~8 mil linhas, e transacao
        # desse tamanho e o que faz este banco devolver `disk I/O error` (visto
        # em 16-17/06, 29/07 e 06/08). Em lotes, uma falha custa 500 registros
        # que a proxima passada regrava — nao o arquivo inteiro nem a conexao.
        pendentes = 0
        for cidade, aid, perfil, tipo, valor, quando in achados:
            try:
                db.session.execute(db.text("""
                    INSERT OR IGNORE INTO atividade_remetente
                      (cidade, id_atividade, id_usuario_strava, tipo, valor,
                       quando, arquivo, criado_em)
                    VALUES (:c, :a, :p, :t, :v, :q, :f, :agora)
                """), {'c': cidade, 'a': aid, 'p': perfil, 't': tipo,
                       'v': valor, 'q': quando, 'f': nome,
                       'agora': datetime.utcnow()})
                gravados += 1
                pendentes += 1
                if pendentes >= 500:
                    db.session.commit()
                    pendentes = 0
            except Exception as exc:
                db.session.rollback()
                pendentes = 0
                logger.debug('prova recusada (%s/%s): %s', aid, valor, exc)
        db.session.execute(db.text("""
            INSERT OR REPLACE INTO txt_varrido (arquivo, tamanho, achados, varrido_em)
            VALUES (:a, :t, :n, :agora)
        """), {'a': nome, 't': tamanho, 'n': len(achados), 'agora': datetime.utcnow()})
        db.session.commit()
        feitos += 1

    return {'arquivos_varridos': feitos, 'provas_gravadas': gravados,
            'arquivos_restantes': restantes,
            'total_de_arquivos': len(candidatos)}


def registrar_envio(cidade, id_atividade, id_usuario_strava, remetente, quando=None):
    """Grava a prova de UM link, na hora do processamento do TXT.

    E o que mantem a agenda em dia sem varrer nada: a partir daqui, toda
    atividade nova ja entra com o dono do envio identificado.
    """
    ident = classificar(remetente)
    if not ident or not id_atividade or not id_usuario_strava:
        return False
    try:
        ensure_schema()
        db.session.execute(db.text("""
            INSERT OR IGNORE INTO atividade_remetente
              (cidade, id_atividade, id_usuario_strava, tipo, valor, quando,
               arquivo, criado_em)
            VALUES (:c, :a, :p, :t, :v, :q, :f, :agora)
        """), {'c': cidade, 'a': str(id_atividade), 'p': str(id_usuario_strava),
               't': ident[0], 'v': ident[1],
               'q': quando.isoformat() if hasattr(quando, 'isoformat') else quando,
               'f': 'processamento', 'agora': datetime.utcnow()})
        return True
    except Exception as exc:
        logger.debug('nao registrei o remetente de %s: %s', id_atividade, exc)
        return False


def _envios(cidade, desde=None):
    """{perfil: [(quando, tipo, valor, id_atividade)]} do mais recente para o mais velho.

    `desde` (data ISO) corta a prova velha. Depois do cadastro inicial e assim
    que a agenda se mantem: vale o que os TXT dos ultimos dois meses provam,
    nao o que alguem postou ha um ano.
    """
    ensure_schema()
    linhas = db.session.execute(db.text("""
        SELECT id_usuario_strava, quando, tipo, valor, id_atividade
        FROM atividade_remetente
        WHERE cidade = :c AND (:desde IS NULL OR quando >= :desde)
        ORDER BY quando DESC, id_atividade DESC
    """), {'c': cidade, 'desde': desde}).fetchall()
    por_perfil = {}
    for perfil, quando, tipo, valor, aid in linhas:
        por_perfil.setdefault(str(perfil), []).append(
            (quando or '', tipo, valor, str(aid)))
    return por_perfil


def _votar(envios, janela=JANELA):
    """(vencedor, contagem, quantos, empate, troca_recente).

    Maioria entre os `janela` envios de TELEFONE mais recentes. Empate devolve o
    mais recente — e marca empate, porque empate nao se aplica sozinho.

    `troca_recente` = {'telefone', 'quando', 'dias'} quando o link MAIS NOVO
    veio de um numero diferente do vencedor. Isso e o sinal de troca de numero,
    e a maioria sozinha nao pega: quem troca de chip continua com a maioria
    historica no numero velho por meses.
    Caso que criou a regra (06/08): um atleta postou 15 links de
    55DD9XXXXXXXX ate 10/06 e passou a postar de outro numero em 21/06. A
    maioria dizia o velho; o dono sabia que era o novo, e estava certo.
    Nao se aplica sozinho — vira sugestao na linha, porque um vizinho postando
    por voce hoje produz exatamente a mesma leitura.
    """
    fones = [e for e in envios if e[1] == FONE and e[2]]
    if not fones:
        return None, {}, 0, False, None
    recentes = fones[:janela]
    contagem = {}
    for _q, _t, valor, _a in recentes:
        contagem[valor] = contagem.get(valor, 0) + 1
    ordenado = sorted(contagem.items(), key=lambda kv: -kv[1])
    empate = len(ordenado) > 1 and ordenado[0][1] == ordenado[1][1]
    vencedor = recentes[0][2] if empate else ordenado[0][0]

    troca = None
    mais_novo = fones[0]
    if mais_novo[2] != vencedor:
        ultimo_do_vencedor = next((e[0] for e in fones if e[2] == vencedor), '')
        troca = {'telefone': mais_novo[2], 'quando': mais_novo[0],
                 'desde_o_vencedor': ultimo_do_vencedor}
    return vencedor, contagem, len(fones), empate, troca


def decidir(cidade, janela=JANELA, forca_minima=5, meses=None):
    """O levantamento inteiro, sem gravar nada.

    `meses=2` restringe a prova aos ultimos dois meses — o regime de
    manutencao, depois que o cadastro inicial estiver feito.

    Devolve {'fortes', 'fracas', 'sem_prova', 'de_outro', 'perdeu_a_disputa',
             'resumo'}.

    A REGRA DE DESEMPATE ENTRE PERFIS, que o dono definiu em 06/08:
    o telefone e UNICO no cadastro (uma pessoa tem varios perfis no Strava —
    um atleta tem quatro). Entao, quando o mesmo telefone e provado para dois
    perfis, fica com o perfil para quem ele postou por ultimo; os outros perfis
    ficam SEM telefone. Melhor perfil vazio que numero repetido.
    """
    from app.models import Atleta

    desde = None
    if meses:
        desde = (datetime.now() - timedelta(days=31 * meses)).date().isoformat()
    por_perfil = _envios(cidade, desde)
    atletas = {a.strava_id: a for a in Atleta.query.all()}

    # 1) o que cada perfil prova
    intencao = {}
    for perfil, envios in por_perfil.items():
        vencedor, contagem, quantos, empate, troca = _votar(envios, janela)
        nomes = sorted({e[2] for e in envios if e[1] == NOME})
        intencao[perfil] = {
            'telefone': vencedor, 'contagem': contagem, 'envios': quantos,
            'empate': empate, 'nomes': nomes, 'troca_recente': troca,
            'ultimo': next((e[0] for e in envios if e[1] == FONE), None),
        }

    # 2) o mesmo telefone provado para dois perfis: quem ficou por ultimo leva
    disputa = {}
    for perfil, dados in intencao.items():
        if dados['telefone']:
            disputa.setdefault(dados['telefone'], []).append(perfil)
    perdeu = {}
    for numero, perfis in disputa.items():
        if len(perfis) < 2:
            continue
        campeao = max(perfis, key=lambda p: (
            intencao[p]['ultimo'] or '', intencao[p]['envios']))
        for perfil in perfis:
            if perfil != campeao:
                perdeu[perfil] = campeao

    fortes, fracas, sem_prova, perdeu_a_disputa, confere = [], [], [], [], []
    orfaos = []
    for perfil, dados in intencao.items():
        atleta = atletas.get(perfil)
        if not atleta:
            # Postou link e nao tem cadastro de atleta. Antes sumia da tela sem
            # explicacao — o admin nao tinha como saber que existia.
            if dados['telefone']:
                orfaos.append({'strava_id': perfil, 'nome': 'sem cadastro',
                               'telefone': dados['telefone'], 'atual': None,
                               'quando': dados['ultimo'], 'envios': dados['envios'],
                               'votos': 0, 'analisados': 0, 'empate': False,
                               'concorrentes': 1, 'cadastrado': False})
            continue
        atual = tel.normalizar(atleta.telefone_normalizado)
        item = {
            'strava_id': perfil, 'nome': atleta.nome_atual or 'Atleta',
            'atual': atleta.telefone_normalizado, 'atual_canonico': atual,
            'telefone': dados['telefone'], 'envios': dados['envios'],
            'contagem': dados['contagem'], 'empate': dados['empate'],
            'nomes': dados['nomes'], 'bonito': tel.bonito(dados['telefone']),
            'quando': dados['ultimo'],
            'confirmado': getattr(atleta, 'telefone_confirmado_em', None),
            'confirmado_por': getattr(atleta, 'telefone_confirmado_por', None),
            'troca_recente': dados['troca_recente'],
            # Quantos perfis reivindicam este mesmo numero. A tela mostra para
            # o admin entender por que um deles vai ficar sem.
            'concorrentes': len(disputa.get(dados['telefone'], []) or [1]),
            'votos': max(dados['contagem'].values()) if dados['contagem'] else 0,
            'analisados': min(dados['envios'], janela),
            'cadastrado': True,
        }
        if perfil in perdeu:
            item['dono'] = atletas[perdeu[perfil]].nome_atual if perdeu[perfil] in atletas else '?'
            perdeu_a_disputa.append(item)
            continue
        if not dados['telefone']:
            sem_prova.append(item)
            continue
        if atual == dados['telefone']:
            # Confere. Se o que esta gravado nao for exatamente o canonico
            # (tem "+", falta o nono digito), entra como conserto de formato —
            # nao muda o numero de ninguem, so escreve direito.
            item['so_formato'] = atleta.telefone_normalizado != dados['telefone']
            confere.append(item)
            continue
        maior = max(dados['contagem'].values()) if dados['contagem'] else 0
        somados = sum(dados['contagem'].values()) or 1
        if dados['envios'] >= forca_minima and not dados['empate'] \
                and maior / float(somados) >= 0.6:
            fortes.append(item)
        else:
            fracas.append(item)

    # 3) cadastro que carrega numero de outra pessoa, com prova
    donos = {}
    for perfil, dados in intencao.items():
        if dados['telefone'] and dados['envios'] >= forca_minima and perfil not in perdeu:
            donos.setdefault(dados['telefone'], []).append(perfil)
    de_outro = []
    for atleta in atletas.values():
        atual = tel.normalizar(atleta.telefone_normalizado)
        if not atual:
            continue
        proprio = intencao.get(atleta.strava_id, {}).get('telefone')
        if proprio == atual:
            continue
        reais = [p for p in donos.get(atual, []) if p != atleta.strava_id]
        if reais:
            de_outro.append({
                'strava_id': atleta.strava_id, 'nome': atleta.nome_atual or 'Atleta',
                'atual': atleta.telefone_normalizado,
                'dono': ', '.join((atletas[p].nome_atual or p) for p in reais),
            })

    return {
        'fortes': sorted(fortes, key=lambda i: -i['envios']),
        'fracas': sorted(fracas, key=lambda i: -i['envios']),
        'confere': sorted(confere, key=lambda i: (i['nome'] or '').lower()),
        'sem_prova': sorted(sem_prova, key=lambda i: (i['nome'] or '').lower()),
        'de_outro': sorted(de_outro, key=lambda i: (i['nome'] or '').lower()),
        'perdeu_a_disputa': sorted(perdeu_a_disputa, key=lambda i: (i['nome'] or '').lower()),
        'orfaos': sorted(orfaos, key=lambda i: -i['envios']),
    }


def _guardar_como_estava(por):
    """Guarda os telefones de hoje numa tabela, para dar para voltar atras.

    POR QUE NAO SE COPIA MAIS O BANCO INTEIRO AQUI
    Copiar 25 MB para mudar 30 telefones e desproporcional — e caro. Este banco
    devolve `disk I/O error` sob escrita pesada, e DUAS das quatro rajadas
    registradas comecam no minuto exato em que uma copia dessas foi criada:
    29/07 14:06 (`pre_telefones_20260729_140603.db`) e 06/08 17:00
    (`pre_agenda_20260806_170058.db`). Nas duas o codigo era o mesmo:
    `sqlite3.backup()` do banco vivo, de dentro da aplicacao, enquanto o site
    escrevia. Guardar so a coluna que vai mudar custa alguns KB e desfaz do
    mesmo jeito.
    """
    db.session.execute(db.text("""
        CREATE TABLE IF NOT EXISTS telefone_historico (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lote VARCHAR(40) NOT NULL,
            strava_id VARCHAR(50) NOT NULL,
            nome VARCHAR(100),
            telefone_antes VARCHAR(20),
            quando DATETIME,
            por VARCHAR(120)
        )
    """))
    db.session.execute(db.text(
        'CREATE INDEX IF NOT EXISTS idx_telefone_historico_lote '
        'ON telefone_historico (lote)'))
    lote = datetime.now().strftime('%Y%m%d_%H%M%S')
    agora = datetime.utcnow()
    linhas = db.session.execute(db.text(
        'SELECT strava_id, nome_atual, telefone_normalizado FROM atletas')).fetchall()
    for strava_id, nome, fone in linhas:
        db.session.execute(db.text("""
            INSERT INTO telefone_historico
              (lote, strava_id, nome, telefone_antes, quando, por)
            VALUES (:l, :s, :n, :f, :q, :p)
        """), {'l': lote, 's': strava_id, 'n': nome, 'f': fone,
               'q': agora, 'p': por})
    db.session.commit()
    return lote, None


def aplicar(cidade, strava_ids=None, so_fortes=True, trocar_existentes=False,
            por='agenda'):
    """Grava. Devolve (relatorio, erro).

    A REGRA QUE MANDA AQUI: **automatico nao desfaz o que gente fez.**
    Por padrao (`trocar_existentes=False`) este botao so ACRESCENTA — preenche
    quem esta sem numero e conserta formato (que nao muda digito nenhum). Trocar
    ou apagar numero que ja existe fica para a tela, uma linha por vez.

    O motivo e concreto: em 06/08 o dono conferiu os 98 atletas um por um e
    corrigiu na mao. Se o botao continuasse "corrigindo pela prova", o clique
    seguinte devolveria os numeros errados — porque a prova sai das mesmas
    conversas que ja estavam la. Deducao automatica vale para o que ninguem
    olhou; onde alguem olhou, ela vira sugestao, e a tela mostra a divergencia.

    ORDEM QUE IMPORTA: primeiro se APAGA, depois se grava. O telefone e unico no
    cadastro; gravar antes de liberar faz a linha certa esbarrar na restricao e
    ficar de fora — foi assim que a varredura anterior quebrou no meio.
    """
    from app.models import Atleta

    try:
        caminho, erro = _guardar_como_estava(por)
    except Exception as exc:
        db.session.rollback()
        return None, ('Não mexi em nada: não consegui guardar como está hoje '
                      '(%s).' % str(exc)[:120])
    if erro:
        return None, 'Não mexi em nada: %s' % erro

    levantamento = decidir(cidade)
    escolhidos = set(strava_ids or [])
    fila = list(levantamento['fortes']) if so_fortes else \
        list(levantamento['fortes']) + list(levantamento['fracas'])
    preservados = 0
    if escolhidos:
        # Linha escolhida a dedo na tela: o admin viu a prova e mandou aplicar.
        fila = [i for i in (levantamento['fortes'] + levantamento['fracas'])
                if i['strava_id'] in escolhidos]
    elif not trocar_existentes:
        # So quem esta SEM numero. Quem ja tem fica intocado, tenha sido
        # digitado na mao ou deduzido antes.
        preservados = sum(1 for i in fila if i['atual'])
        fila = [i for i in fila if not i['atual']]

    relatorio = {'lote': caminho, 'gravados': 0, 'formato': 0,
                 'apagados_de_outro': 0, 'apagados_perdedores': 0,
                 'preservados': preservados, 'recusados': []}

    def _grava(strava_id, numero, item=None):
        """UMA linha, UM commit. Devolve True se gravou.

        Nao e preciosismo: este banco devolve `disk I/O error` em transacao
        grande (16-17/06, 78 vezes; 29/07 na tentativa anterior de arrumar a
        agenda, 52; 06/08, 23). Em 06/08 as 17:01 o log disse "10 gravados" e
        o banco ficou identico — a transacao inteira caiu no fim, e a conexao
        do worker ficou envenenada: dali em diante ate SELECT devolvia erro, e
        a tela passou a dar 500. Commit por linha troca "tudo ou nada" por
        "quase tudo, e a lista do que faltou".
        """
        try:
            atleta = Atleta.query.filter_by(strava_id=strava_id).first()
            if not atleta:
                return False
            atleta.telefone_normalizado = numero
            db.session.commit()
            return True
        except Exception as exc:
            db.session.rollback()
            relatorio['recusados'].append({
                'nome': (item or {}).get('nome', strava_id),
                'de': (item or {}).get('atual'), 'para': numero,
                'motivo': str(exc)[:120]})
            return False

    # ORDEM QUE IMPORTA: primeiro se APAGA, depois se grava. O telefone e unico
    # no cadastro; gravar antes de liberar faz a linha certa esbarrar na
    # restricao e ficar de fora — foi assim que a varredura anterior quebrou.

    # 1) libera os numeros que estao no cadastro errado — SO no modo agressivo.
    #    Apagar numero que alguem digitou e desfazer trabalho de gente; no modo
    #    normal isso vira aviso na tela, nao gravacao.
    if trocar_existentes:
        for item in levantamento['de_outro']:
            if _grava(item['strava_id'], None, item):
                relatorio['apagados_de_outro'] += 1
        for item in levantamento['perdeu_a_disputa']:
            if item['atual'] and _grava(item['strava_id'], None, item):
                relatorio['apagados_perdedores'] += 1

    # 2) conserta o formato de quem ja esta certo (nao muda numero de ninguem)
    for item in levantamento['confere']:
        if item.get('so_formato') and _grava(item['strava_id'], item['telefone'], item):
            relatorio['formato'] += 1

    # 3) grava o que a prova mandou
    for item in fila:
        if _grava(item['strava_id'], item['telefone'], item):
            relatorio['gravados'] += 1

    logger.info('Agenda telefonica por %s: %d gravados, %d de formato, '
                '%d liberados (numero de outro), %d liberados (perfil repetido), '
                '%d recusados. Como estava antes: lote %s.', por, relatorio['gravados'],
                relatorio['formato'], relatorio['apagados_de_outro'],
                relatorio['apagados_perdedores'], len(relatorio['recusados']),
                caminho)
    return relatorio, None


def copias():
    """As cópias guardadas: [(lote, quantos, quando, por)], da mais nova primeiro."""
    ensure_schema()
    linhas = db.session.execute(db.text("""
        SELECT lote, COUNT(*) AS quantos, MAX(quando) AS quando, MAX(por) AS por
        FROM telefone_historico GROUP BY lote ORDER BY lote DESC LIMIT 20
    """)).fetchall()
    return [{'lote': l, 'quantos': q, 'quando': w, 'por': p} for l, q, w, p in linhas]


def salvar_copia(por='admin'):
    """Guarda a agenda como está agora. (lote, erro)."""
    try:
        return _guardar_como_estava(por)
    except Exception as exc:
        db.session.rollback()
        return None, str(exc)[:150]


def restaurar(lote, por='admin'):
    """Devolve os telefones ao que eram naquele lote. (relatorio, erro).

    Guarda uma copia do estado atual ANTES de restaurar — desfazer tambem
    precisa de desfazer. Grava uma linha por vez, e libera antes de ocupar: o
    telefone e unico, entao restaurar dois atletas que trocaram de numero entre
    si esbarraria na restricao se fosse na ordem ingenua.
    """
    from app.models import Atleta

    ensure_schema()
    alvo = db.session.execute(db.text(
        'SELECT strava_id, nome, telefone_antes FROM telefone_historico '
        'WHERE lote = :l'), {'l': lote}).fetchall()
    if not alvo:
        return None, 'Não achei essa cópia (%s).' % lote

    salvar_copia('antes de voltar para %s, por %s' % (lote, por))

    relatorio = {'lote': lote, 'voltaram': 0, 'iguais': 0, 'recusados': []}
    atuais = {a.strava_id: a for a in Atleta.query.all()}

    # 1) libera o que vai mudar
    for strava_id, _nome, antes in alvo:
        atleta = atuais.get(str(strava_id))
        if atleta and atleta.telefone_normalizado != antes:
            try:
                atleta.telefone_normalizado = None
                db.session.commit()
            except Exception:
                db.session.rollback()
    # 2) devolve
    for strava_id, nome, antes in alvo:
        atleta = atuais.get(str(strava_id))
        if not atleta:
            continue
        if atleta.telefone_normalizado == antes:
            relatorio['iguais'] += 1
            continue
        try:
            atleta.telefone_normalizado = antes
            db.session.commit()
            relatorio['voltaram'] += 1
        except Exception as exc:
            db.session.rollback()
            relatorio['recusados'].append({'nome': nome, 'motivo': str(exc)[:120]})
    logger.info('Agenda restaurada para o lote %s por %s: %d voltaram, %d recusados.',
                lote, por, relatorio['voltaram'], len(relatorio['recusados']))
    return relatorio, None


def exportar_contatos(cidade):
    """[(nome, telefone com +55)] — o arquivo que vira agenda no celular.

    GUARDADO SEM O "+", EXPORTADO COM ELE. Dentro do sistema o numero e
    55DDD9XXXXXXXX: e a forma que o WhatsApp e o WAHA aceitam, e a que a fila de
    mensagens usa. Na hora de virar contato no celular, o formato internacional
    (+55 DD 9XXXX-XXXX) e o que faz o aparelho reconhecer o pais — sem ele, o
    contato importado em outro DDI nao disca.
    """
    from app.models import Atleta, Activity

    linhas = (Atleta.query
              .join(Activity, Activity.id_usuario_strava == Atleta.strava_id)
              .filter(Activity.cidade == cidade,
                      Atleta.telefone_normalizado.isnot(None),
                      Atleta.telefone_normalizado != '')
              .distinct().all())
    contatos = []
    for atleta in linhas:
        numero = tel.normalizar(atleta.telefone_normalizado)
        if not numero:
            continue
        contatos.append((atleta.nome_atual or 'Atleta', '+' + numero,
                         tel.bonito(numero), atleta.strava_id))
    return sorted(contatos, key=lambda c: (c[0] or '').lower())
