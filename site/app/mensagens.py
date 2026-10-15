# -*- coding: utf-8 -*-
"""As mensagens do ciclo mensal, montadas a partir dos textos editaveis.

O TEXTO fica em app/textos.py (padrao de fabrica + versao da cidade).
AQUI ficam os DADOS: quantas pessoas, quantos km, qual patrocinador, qual
link. Separado de proposito — o operador reescreve o texto sem tocar em
codigo, e o codigo troca os numeros sem estragar o texto.

Nada aqui envia nada. Quem entrega e a fila (app/filamensagens.py) e o
orquestrador.
"""
import logging
from datetime import datetime

from app import db
from app import telefone
from app import textos

logger = logging.getLogger(__name__)


def _mes_anterior(ano, mes):
    return (ano - 1, 12) if mes == 1 else (ano, mes - 1)


def _proximo_mes(ano, mes):
    return (ano + 1, 1) if mes == 12 else (ano, mes + 1)


def _numero_bonito(valor):
    """1234.5 -> '1.234,5' (jeito que se le em portugues)."""
    texto = f'{float(valor or 0):,.1f}'
    return texto.replace(',', '#').replace('.', ',').replace('#', '.')


def _linha_apoios(patrocinadores):
    """Bloco 'Apoio: *Fulano* — banca a arte', uma linha por apoiador."""
    linhas = []
    for sp in patrocinadores:
        if sp.is_principal:
            continue
        texto = f'Apoio: *{sp.exibicao}*'
        if (sp.o_que_banca or '').strip():
            texto += f' — {sp.o_que_banca.strip()}'
        linhas.append(texto)
    return '\n'.join(linhas)


def _nome_com_banca(sp):
    """'*Relojoaria 3R* — Te premiara com as medalhas'.

    O negrito e montado AQUI, nao no template: assim o que vem depois do
    travessao fica fora do negrito, que e como se le melhor no WhatsApp.
    """
    if not sp:
        return ''
    nome = f'*{sp.exibicao}*'
    if (sp.o_que_banca or '').strip():
        return f'{nome} — {sp.o_que_banca.strip()}'
    return nome


def _dados_do_periodo(cidade, ano, mes):
    """Numeros daquele mes, para as etiquetas do texto."""
    from app.models import MonthlyBackup
    backup = MonthlyBackup.query.filter_by(cidade=cidade, ano=ano, mes=mes).first()
    dados = backup.get_ranking_data() if backup else {}
    km_total = 0.0
    corrida = ciclismo = 0
    for secao, contador in (('corrida_caminhada', 'corrida'), ('ciclismo', 'ciclismo')):
        bloco = dados.get(secao) or {}
        km_total += float(bloco.get('total_km') or 0)
        if contador == 'corrida':
            corrida = int(bloco.get('total_atletas') or 0)
        else:
            ciclismo = int(bloco.get('total_atletas') or 0)
    return {
        'atletas': (backup.total_atletas if backup else 0) or 0,
        'medalhas': (backup.total_medalhas if backup else 0) or 0,
        'km': _numero_bonito(km_total),
        'corrida': corrida,
        'ciclismo': ciclismo,
    }


def agradecimento_patrocinadores(cidade, ano, mes):
    """Uma mensagem POR PATROCINADOR, para o privado de cada um.

    Com mais de um patrocinador, muda so o nome — cada um recebe o
    agradecimento como se fosse escrito para ele, que e o que ele merece
    depois de bancar o mes.
    """
    from app.blueprints.ranking import (
        MESES_PT, _patrocinadores_periodo, _build_public_ranking_url,
    )
    patrocinadores = _patrocinadores_periodo(cidade, ano, mes)
    if not patrocinadores:
        return []

    numeros = _dados_do_periodo(cidade, ano, mes)
    try:
        link = _build_public_ranking_url(cidade, ano, mes) or ''
    except Exception as exc:
        logger.debug('Sem link publico no agradecimento: %s', exc)
        link = ''

    saida = []
    for sp in patrocinadores:
        dados = dict(numeros)
        # Quem banca todo mes nao se despede: o fecho muda de "contamos com
        # voce no ano que vem" para "ate o mes que vem".
        continuo = bool(getattr(sp, 'patrocinio_continuo', False))
        dados.update({
            'mes': MESES_PT.get(mes, str(mes)),
            'patrocinador': sp.exibicao,
            'fecho': textos.FECHO_CONTINUO if continuo else textos.FECHO_UNICO,
            'link': link,
        })
        saida.append({
            'patrocinador_id': sp.id,
            'nome': sp.exibicao,
            'telefone': telefone.normalizar(sp.telefone),
            'continuo': continuo,
            'texto': textos.montar(cidade, textos.AGRADECIMENTO, dados),
        })
    return saida


def encerramento_do_mes(cidade, ano, mes):
    """Aviso no grupo de que o mês fechou, com os números e onde retirar."""
    from app.blueprints.ranking import MESES_PT, _build_public_ranking_url

    numeros = _dados_do_periodo(cidade, ano, mes)
    if not numeros['atletas']:
        # Mes sem ranking fechado ainda: nao ha o que anunciar.
        return None
    try:
        link = _build_public_ranking_url(cidade, ano, mes) or ''
    except Exception as exc:
        logger.debug('Sem link publico no encerramento: %s', exc)
        link = ''
    dados = dict(numeros)
    dados.update({
        'mes': MESES_PT.get(mes, str(mes)),
        'local': _local_em_texto(cidade, ano, mes) or 'combine com um dos admins',
        'link': link,
    })
    return {'chave': textos.FECHAMENTO_MES,
            'texto': textos.montar(cidade, textos.FECHAMENTO_MES, dados)}


def bloqueio_medalhas_no_grupo(cidade, ano, mes):
    """O que impede o aviso das medalhas de sair no grupo. None = pode sair.

    POR QUE ISTO É PÚBLICO E DEVOLVE O MOTIVO (14/08/2026)
    A recusa existia e estava certa, mas morria numa linha de log: quem
    apertava "Salvar e enviar" via um aviso azul dizendo que "não havia
    mensagem para enviar agora" e ficava sem saber o que faltava. Mensagem que
    pede ação leva o link que faz a ação (regra 21-E) — então o motivo sai
    daqui já com o botão certo, e quem chama só mostra.
    """
    if not _lista_de_medalhistas(cidade, ano, mes)[0]:
        if medalhistas_pendentes(cidade, ano, mes, so_pendentes=False):
            # Tem medalhista, mas todos já retiraram: não há quem avisar.
            return {
                'falta': 'lista',
                'texto': 'Todos os medalhistas deste mês já retiraram a '
                         'medalha — não há quem avisar.',
                'botao': 'Abrir os medalhistas',
                'rota': '/admin/medalhistas',
            }
        return {
            'falta': 'lista',
            'texto': 'Ninguém aparece como medalhista neste mês, então não há '
                     'lista para anunciar. Se você sabe que tem gente, atualize '
                     'a lista com os km de agora.',
            'botao': 'Abrir os medalhistas',
            'rota': '/admin/medalhistas',
        }
    if not local_definido(cidade, ano, mes):
        return {
            'falta': 'local',
            'texto': 'Falta dizer ONDE retirar a medalha. Sem isso a mensagem '
                     'manda o grupo inteiro procurar um balcão que ninguém sabe '
                     'qual é — e a medalha é a única mensagem que faz a pessoa '
                     'sair de casa.',
            'botao': 'Cadastrar o local de retirada',
            'rota': '/ranking/patrocinadores',
        }
    return None


def medalhas_no_grupo(cidade, ano, mes):
    """Aviso no grupo de que as medalhas chegaram (P-70).

    NAO SAI SEM A LISTA nem sem o local. As duas coisas são o motivo da
    mensagem existir: sem lista, o "Medalhistas:" fica pendurado; sem local,
    manda 80 pessoas procurarem um balcão que ninguém sabe qual é — e a medalha
    é a única mensagem que faz a pessoa SAIR DE CASA (P-23).

    Devolve None quando falta alguma das duas; o motivo, em português e com o
    link que resolve, sai do `bloqueio_medalhas_no_grupo`.
    """
    from app.blueprints.ranking import MESES_PT

    # A lista sai uma vez só no caminho feliz; o motivo detalhado só é montado
    # quando de fato não dá para enviar.
    lista, marcados = _lista_de_medalhistas(cidade, ano, mes)
    if not lista or not local_definido(cidade, ano, mes):
        bloqueio = bloqueio_medalhas_no_grupo(cidade, ano, mes) or {}
        logger.info('Medalhas no grupo (%s %s/%s): não sai — falta %s.',
                    cidade, mes, ano, bloqueio.get('falta', '?'))
        return None

    dados = {
        'mes': MESES_PT.get(mes, str(mes)),
        'local': _local_em_texto(cidade, ano, mes),
        'link': _maps_em_texto(cidade, ano, mes),
        'patrocinador': _patrocinador_em_texto(cidade, ano, mes),
        'lista': lista,
    }
    return {'chave': textos.MEDALHAS_NO_GRUPO,
            'texto': textos.montar(cidade, textos.MEDALHAS_NO_GRUPO, dados),
            # Quem sera marcado com @ — o texto ja traz o "@numero" literal; a
            # lista diz ao WhatsApp a quem cada um se refere. Sem os dois
            # juntos a pessoa nao recebe notificacao nenhuma.
            'mencionar': marcados}


def abertura_do_mes(cidade, ano, mes):
    """Mensagem do dia 1º. Muda conforme haja ou nao patrocinador."""
    from app.models import Config
    from app.blueprints.ranking import MESES_PT, _patrocinadores_periodo

    config = Config.query.filter_by(cidade=cidade).first()
    if not config:
        return None
    patrocinadores = _patrocinadores_periodo(cidade, ano, mes)
    principal = next((p for p in patrocinadores if p.is_principal), None)

    dados = {
        'mes': MESES_PT.get(mes, str(mes)),
        'meta_corrida': config.meta_corrida_caminhada,
        'meta_ciclismo': config.meta_ciclismo,
        'patrocinador': _nome_com_banca(principal) if principal else '',
        'apoios': _linha_apoios(patrocinadores),
    }
    chave = textos.ABERTURA_COM if principal else textos.ABERTURA_SEM
    return {'chave': chave, 'texto': textos.montar(cidade, chave, dados)}


def anuncio_de_patrocinador(cidade, ano, mes, agora=None):
    """Aviso no grupo de que o mês ganhou patrocinador.

    O tom muda conforme o momento: cadastrado ANTES do mês começar é
    "já tem patrocinador, pode separar o tênis"; DEPOIS que o mês começou é
    "achamos o patrocinador". Verbo no futuro num caso, no presente no outro.
    """
    from app.blueprints.ranking import MESES_PT, _patrocinadores_periodo

    agora = agora or datetime.now()
    patrocinadores = _patrocinadores_periodo(cidade, ano, mes)
    principal = next((p for p in patrocinadores if p.is_principal), None)
    if not principal:
        return None

    comeco_do_mes = datetime(ano, mes, 1)
    antes = agora < comeco_do_mes
    chave = textos.PATROCINIO_ANTES if antes else textos.PATROCINIO_DEPOIS
    banca = (principal.o_que_banca or '').strip()
    apoios = _linha_apoios(patrocinadores)
    dados = {
        'mes': MESES_PT.get(mes, str(mes)),
        # No anuncio o nome vai sozinho e o que ele banca vem na linha de
        # baixo — inline a frase quebrava ("*Fulano* — banca as medalhas vai
        # bancar o mes de Agosto").
        'patrocinador': principal.exibicao,
        'banca': ('— ' + banca + '\n') if banca else '',
        'apoios': (apoios + '\n') if apoios else '',
    }
    return {
        'chave': chave,
        'antes_do_mes': antes,
        'patrocinador_id': principal.id,
        'ja_anunciado': bool(principal.anunciado_em),
        'texto': textos.montar(cidade, chave, dados),
    }


def _local_em_texto(cidade, ano=None, mes=None):
    """O local de retirada como frase, nao como dicionario.

    O modulo de avisos devolve os campos separados; o texto precisa deles
    numa linha so. Sem isto o {local} da mensagem saia como um dicionario
    Python no meio do WhatsApp.
    """
    from app.blueprints.avisos import _local_retirada
    local = _local_retirada(cidade, ano, mes) or {}
    # UMA LINHA SO, porque a mensagem ja rotula ("Endereço: {local}").
    # O MAPA saiu daqui e tem linha propria (`_maps_em_texto`): URL solta no
    # meio do endereco vira parede de texto e o WhatsApp mostra a previa no
    # lugar. O HORARIO tambem saiu — virou frase fixa no template ("estará
    # disponível em horário comercial"), decisao do dono em 12/08.
    partes = []
    if local.get('estabelecimento'):
        partes.append('*' + local['estabelecimento'].strip() + '*')
    if (local.get('endereco') or '').strip():
        partes.append(local['endereco'].strip())
    partes = [' — '.join(partes)] if partes else []
    if not partes:
        # SEM LOCAL, A FRASE DIZ ISSO — nao fica em branco (P-23).
        # A mensagem automatica nem chega aqui: a agenda segura a medalha
        # enquanto o local nao existir. Este texto e para quando o admin manda
        # NA MAO mesmo assim, que e um direito dele — o mes vira e a pessoa
        # merece saber que ganhou, mesmo sem o local decidido.
        # Melhor prometer o aviso do que deixar um buraco no meio do WhatsApp:
        # buraco faz o atleta perguntar "onde?" e o admin nao ter resposta.
        return '_O local de retirada ainda está sendo definido — assim que ' \
               'estiver fechado, a gente avisa por aqui._'
    return '\n'.join(partes)


def _maps_em_texto(cidade, ano=None, mes=None):
    """O link do Google Maps do local de retirada, ou frase de falta.

    Linha propria na mensagem (12/08): endereco e mapa respondem perguntas
    diferentes — "onde fica" e "como chego". Juntos numa linha so, a URL
    engole o endereco e o WhatsApp mostra a previa no lugar do texto.
    """
    from app.blueprints.avisos import _local_retirada
    return ((_local_retirada(cidade, ano, mes) or {}).get('maps_url') or '').strip()


def _patrocinador_em_texto(cidade, ano, mes):
    """'*Fulano* — banca as medalhas' do mes, ou vazio.

    Quem paga a medalha aparece NA mensagem de quem vai busca-la: e o unico
    momento em que o patrocinador encontra o atleta um a um. Sai do periodo
    (regra 10) — nunca do mes corrente.
    """
    try:
        from app.blueprints.ranking import _patrocinadores_periodo
        itens = _patrocinadores_periodo(cidade, ano, mes) or []
        return _nome_com_banca(itens[0]) if itens else ''
    except Exception as exc:
        logger.warning('Nao consegui apurar o patrocinador de %s %s/%s: %s',
                       cidade, mes, ano, exc)
        return ''


def _lista_de_medalhistas(cidade, ano, mes):
    """Quem bateu a meta, por modalidade, pronto para o grupo (P-70).

    SAI DA FOTO CONGELADA (`medalhistas_mes`, regra 23), nunca do ranking vivo:
    o ranking muda se alguem postar link atrasado, e a lista anunciada no grupo
    nao pode mudar depois de anunciada.

    CATEGORIZADA porque a meta e por modalidade (regra 12): juntar corrida e
    ciclismo numa lista so faria parecer que todo mundo bateu a mesma coisa.

    MARCA CADA UM COM @ (15/08/2026, pedido do dono), igual ao parabéns de meta
    batida: o WhatsApp só desenha a marcação quando o texto traz o `@numero`
    literal E a mensagem leva a lista de marcados junto. Por isso esta função
    devolve **(texto, telefones)** — quem envia precisa dos dois.

    Devolve ('', []) quando nao ha ninguem — e ai quem chama decide, porque
    anunciar "Medalhistas:" sem nome embaixo e pior que nao anunciar.
    """
    # QUEM JÁ RETIROU NÃO ENTRA (01/10/2026, pedido do dono): o aviso é para
    # quem ainda precisa ir buscar — marcar quem já pegou só gera notificação
    # à toa e faz a pessoa achar que esqueceram de dar baixa.
    try:
        pessoas = medalhistas_pendentes(cidade, ano, mes, so_pendentes=True)
    except Exception as exc:
        logger.warning('Nao consegui montar a lista de medalhistas de %s %s/%s: %s',
                       cidade, mes, ano, exc)
        return '', []
    if not pessoas:
        return '', []

    # A MODALIDADE VEM NO PLURAL, E ISSO QUEBRAVA A CATEGORIZACAO (15/08/2026).
    # `medalhistas_do_mes` devolve `modalidades` (uma LISTA — a pessoa pode ter
    # batido a meta nas duas), e aqui se perguntava por `modalidade`. Como
    # ninguem tinha esse campo, todo mundo caia no bloco "sem modalidade" e a
    # mensagem saia com uma lista corrida, sem os titulos — foi assim que ela
    # chegou ao grupo em 15/08.
    def _modalidades(p):
        if p.get('modalidades'):
            return [str(m) for m in p['modalidades'] if m]
        return [str(p['modalidade'])] if p.get('modalidade') else []

    telefones = _telefones_dos_medalhistas(
        [p.get('chave') or p.get('chave_atleta') for p in pessoas])

    def _linha(p):
        nome = (p.get('nome') or '').strip()
        fone = telefones.get(str(p.get('chave') or p.get('chave_atleta') or ''))
        # Sem telefone a pessoa continua na lista, so nao marcada: sumir com
        # ela seria pior que nao notificar (regra 4-I).
        return ('• @%s %s' % (fone, nome)) if fone else ('• ' + nome)

    rotulos = [('corrida_caminhada', '🏃 *Corrida e caminhada*'),
               ('ciclismo', '🚴 *Ciclismo*')]
    blocos, marcados, ja_listados = [], [], set()
    for chave, titulo in rotulos:
        doentro = sorted((p for p in pessoas if chave in _modalidades(p)),
                         key=lambda p: (p.get('nome') or '').strip().lower())
        doentro = [p for p in doentro if (p.get('nome') or '').strip()]
        if not doentro:
            continue
        blocos.append(titulo + '\n' + '\n'.join(_linha(p) for p in doentro))
        for p in doentro:
            ja_listados.add(str(p.get('chave') or p.get('chave_atleta')))

    # Quem ficou sem modalidade reconhecida nao pode sumir (regra 4-I: nada
    # some em silencio) — entra num bloco proprio, sem rotulo inventado.
    conhecidas = {c for c, _ in rotulos}
    soltos = sorted((p for p in pessoas
                     if not (set(_modalidades(p)) & conhecidas)
                     and (p.get('nome') or '').strip()),
                    key=lambda p: (p.get('nome') or '').strip().lower())
    if soltos:
        blocos.append('\n'.join(_linha(p) for p in soltos))

    for p in pessoas:
        fone = telefones.get(str(p.get('chave') or p.get('chave_atleta') or ''))
        if fone and fone not in marcados:
            marcados.append(fone)
    return '\n\n'.join(blocos), marcados


def local_definido(cidade, ano=None, mes=None):
    """Da para dizer ao atleta ONDE retirar a medalha? (P-23)

    O que decide e o ESTABELECIMENTO. Endereco, horario e mapa ajudam, mas quem
    responde "onde?" e o nome do lugar — sem ele a mensagem nao tem serventia.

    Fica aqui, junto de quem monta o texto, para nao virar duas regras: a
    agenda pergunta a este modulo em vez de olhar a tabela por conta propria.
    """
    from app.blueprints.avisos import _local_retirada
    local = _local_retirada(cidade, ano, mes) or {}
    return bool((local.get('estabelecimento') or '').strip())


def _telefones_dos_medalhistas(chaves):
    """{chave_atleta: telefone} para quem tem numero cadastrado."""
    from app.models import Atleta
    numeros = {}
    strava_ids = [c for c in chaves if c and not str(c).startswith('name:')]
    if not strava_ids:
        return numeros
    for atleta in Atleta.query.filter(Atleta.strava_id.in_(strava_ids)).all():
        numero = telefone.normalizar(atleta.telefone_normalizado)
        if numero:
            numeros[str(atleta.strava_id)] = numero
    return numeros


def mes_da_medalha(hoje=None):
    """(ano, mes) da medalha que esta em jogo — sempre o MES FECHADO anterior.

    Medalha e de mes que ja acabou: em agosto se entrega a de julho. Nao ha
    caso em que a medalha do mes corrente esteja valendo, porque a meta so se
    sabe batida quando o mes termina.
    """
    hoje = hoje or datetime.now()
    return (hoje.year - 1, 12) if hoje.month == 1 else (hoje.year, hoje.month - 1)


def medalhistas_sem_telefone(cidade, ano, mes):
    """Quem bateu a meta e NAO tem numero. A medalha nao chega nessa pessoa.

    POR QUE ISTO EXISTE (P-25)
    Em 01/08 sairam 10 avisos de medalha de 12 medalhistas. Os dois que
    faltaram nao tinham telefone cadastrado — e ninguem soube: a rodada
    terminou dizendo que tinha enviado, e estava certa, ela enviou tudo que
    dava. O buraco nao estava no envio, estava no cadastro, e nada apontava
    para la.

    Devolve nome e chave, nao so a contagem: "faltam 2" manda o admin procurar;
    "faltam a Maria e o Noe" ele resolve na hora.
    """
    pessoas = medalhistas_pendentes(cidade, ano, mes, True)
    numeros = _telefones_dos_medalhistas([p['chave'] for p in pessoas])
    return [{'chave': str(p['chave']), 'nome': p.get('nome') or 'Atleta'}
            for p in pessoas if not numeros.get(str(p['chave']))]


def cobranca_de_medalha(cidade, ano, mes, so_pendentes=True):
    """Uma mensagem por medalhista que ainda não retirou.

    Vai para o PRIVADO de cada um — por isso carrega o telefone. Quem não
    tem número cadastrado aparece na lista assim mesmo, com telefone None,
    para a tela poder mostrar "falta o telefone desta pessoa".
    """
    from app.blueprints.ranking import MESES_PT

    local = _local_em_texto(cidade, ano, mes)
    # Apurados UMA vez para a lista inteira: sao iguais para todo mundo do mes,
    # e consultar por pessoa multiplicaria a mesma pergunta por 40.
    mapa = _maps_em_texto(cidade, ano, mes)
    patrocinador = _patrocinador_em_texto(cidade, ano, mes)
    pessoas = medalhistas_pendentes(cidade, ano, mes, so_pendentes)
    numeros = _telefones_dos_medalhistas([p['chave'] for p in pessoas])
    saida = []
    for pessoa in pessoas:
        dados = {
            'nome': (pessoa.get('nome') or 'atleta').split(' ')[0],
            'mes': MESES_PT.get(mes, str(mes)),
            'local': local,
            'link': mapa,
            'patrocinador': patrocinador,
        }
        saida.append({
            'chave_atleta': pessoa['chave'],
            'nome': pessoa.get('nome'),
            'telefone': numeros.get(str(pessoa['chave'])),
            'texto': textos.montar(cidade, textos.COBRANCA_MEDALHA, dados),
            'retirou': bool(pessoa.get('retirou')),
        })
    return saida


def aviso_de_mes_anterior(ano, mes):
    """Abertura em destaque para o aviso de medalha REPETIDO (01/10/2026).

    Pedido do dono: quando o aviso vai de novo, a primeira coisa que a pessoa
    lê é que ele é de um mês que já passou, não do mês atual.
    """
    from app.blueprints.ranking import MESES_PT
    return ('⚠️ *ATENÇÃO: este aviso é das medalhas de %s/%s — um mês '
            'anterior, NÃO do mês fechado*\n\n' % (MESES_PT.get(mes, str(mes)), ano))


def rodape_de_reincidencia():
    """Fecho breve e leve do aviso de medalha REPETIDO (pedido do dono, 01/10/2026)."""
    return '\n\n_Lembrete: as medalhas ficam disponíveis para retirada por 2 meses._'


def medalhistas_pendentes(cidade, ano, mes, so_pendentes=True):
    from app.blueprints.avisos import medalhistas_do_mes
    try:
        return medalhistas_do_mes(cidade, ano, mes, so_pendentes=so_pendentes)
    except Exception as exc:
        logger.warning('Nao consegui listar medalhistas de %s %s/%s: %s',
                       cidade, mes, ano, exc)
        return []


def mensagens_do_ciclo(cidade, hoje=None):
    """O que faz sentido HOJE, com o motivo de cada uma.

    `sugerida=True` marca o que e do dia. A tela mostra tudo; quem decide
    enviar e o admin (ou o automatico, quando estiver ligado).
    """
    hoje = hoje or datetime.now()
    ano_ant, mes_ant = _mes_anterior(hoje.year, hoje.month)
    janela_virada = hoje.day <= 5

    saida = []

    for item in agradecimento_patrocinadores(cidade, ano_ant, mes_ant):
        saida.append({
            'chave': f"agradecimento-{ano_ant:04d}-{mes_ant:02d}-{item['patrocinador_id']}",
            'tipo': 'agradecimento',
            'titulo': f"Agradecimento a {item['nome']}",
            'destino': 'privado do patrocinador',
            'telefone': item['telefone'],
            'periodo': f'{ano_ant:04d}-{mes_ant:02d}',
            'texto': item['texto'],
            'sugerida': janela_virada,
        })

    # O grupo precisa saber que o mes fechou ANTES de as medalhas serem
    # cobradas no privado — senao a primeira noticia do fechamento chega so
    # para quem bateu a meta.
    encerramento = encerramento_do_mes(cidade, ano_ant, mes_ant)
    if encerramento:
        saida.append({
            'chave': f'encerramento-{ano_ant:04d}-{mes_ant:02d}',
            'tipo': 'encerramento',
            'titulo': f'Encerramento de {mes_ant:02d}/{ano_ant}',
            'destino': 'grupo',
            'telefone': None,
            'periodo': f'{ano_ant:04d}-{mes_ant:02d}',
            'texto': encerramento['texto'],
            'sugerida': janela_virada,
        })

    abertura = abertura_do_mes(cidade, hoje.year, hoje.month)
    if abertura:
        saida.append({
            'chave': f'abertura-{hoje.year:04d}-{hoje.month:02d}',
            'tipo': 'abertura',
            'titulo': 'Abertura do mês',
            'destino': 'grupo',
            'telefone': None,
            'periodo': f'{hoje.year:04d}-{hoje.month:02d}',
            'texto': abertura['texto'],
            'sugerida': janela_virada,
        })

    # Patrocinador do MES QUE VEM ja cadastrado: o grupo merece saber agora.
    ano_prox, mes_prox = _proximo_mes(hoje.year, hoje.month)
    # P-51: o anuncio ja feito NAO some mais da lista. Ele some da SUGESTAO
    # (nao pisca pedindo para ser enviado), mas continua visivel com a marca
    # "ja anunciado" e o botao de mandar de novo. Sumir escondia a resposta da
    # pergunta que o admin mais faz aqui: "eu ja anunciei o patrocinador?".
    anuncio = anuncio_de_patrocinador(cidade, ano_prox, mes_prox, agora=hoje)
    if anuncio:
        feito = bool(anuncio['ja_anunciado'])
        saida.append({
            'chave': f'patrocinio-{ano_prox:04d}-{mes_prox:02d}',
            'tipo': 'patrocinio',
            'titulo': 'Patrocinador do próximo mês já garantido',
            'destino': 'grupo',
            'telefone': None,
            'periodo': f'{ano_prox:04d}-{mes_prox:02d}',
            'texto': anuncio['texto'],
            'sugerida': not feito,
            'ja_feito': feito,
            'motivo_feito': 'já anunciado no grupo',
        })

    # Patrocinador do mes CORRENTE cadastrado depois do dia 1.
    if hoje.day > 1:
        atual = anuncio_de_patrocinador(cidade, hoje.year, hoje.month, agora=hoje)
        if atual:
            feito = bool(atual['ja_anunciado'])
            saida.append({
                'chave': f'patrocinio-{hoje.year:04d}-{hoje.month:02d}',
                'tipo': 'patrocinio',
                'titulo': ('Patrocinador deste mês' if feito
                           else 'Patrocinador deste mês ainda não foi anunciado'),
                'destino': 'grupo',
                'telefone': None,
                'periodo': f'{hoje.year:04d}-{hoje.month:02d}',
                'texto': atual['texto'],
                'sugerida': not feito,
                'ja_feito': feito,
                'motivo_feito': 'já anunciado no grupo',
            })

    # AS MEDALHAS CHEGARAM — AVISO NO GRUPO (P-70, encaixado em 14/08/2026).
    #
    # O texto e a funcao (`medalhas_no_grupo`) existiam desde 12/08 e NINGUEM
    # os chamava: o modelo aparecia para editar na Central, mas o botao "Salvar
    # e enviar" percorre esta lista atras do item — nao achava nada, dizia "nao
    # havia mensagem para enviar agora" e a mensagem nunca ia para a fila.
    # Meia P-70 pronta e uma tela que nao contava isso.
    #
    # Vem ANTES do privado de proposito: o grupo sabe primeiro, senao a
    # primeira noticia de que a medalha chegou so alcanca quem bateu a meta.
    medalhas_grupo = medalhas_no_grupo(cidade, ano_ant, mes_ant)
    if medalhas_grupo:
        saida.append({
            'chave': f'medalhas-{ano_ant:04d}-{mes_ant:02d}',
            'tipo': 'medalhas_grupo',
            'titulo': f'Medalhas de {mes_ant:02d}/{ano_ant} disponíveis (no grupo)',
            'destino': 'grupo',
            'telefone': None,
            'periodo': f'{ano_ant:04d}-{mes_ant:02d}',
            'texto': medalhas_grupo['texto'],
            'mencionar': medalhas_grupo.get('mencionar') or [],
            # So aparece aqui quando JA PODE sair (tem lista e tem local), entao
            # estar na lista ja e a sugestao. Sozinha ela nao sai: `tipo` fora do
            # CALENDARIO do agendamensagens = ninguem publica isto no automatico.
            'sugerida': True,
        })

    # Medalha no privado de quem bateu a meta, uma por pessoa.
    #
    # P-51: antes, quem retirava a medalha SUMIA da lista. Parecia que o
    # sistema tinha comido a pessoa, e para saber quem ja tinha ido buscar so
    # decorando. Agora todo mundo continua aparecendo — quem retirou vem
    # marcado, no fim da lista, e ainda da para mandar de novo se precisar.
    for pessoa in cobranca_de_medalha(cidade, ano_ant, mes_ant, so_pendentes=False):
        saida.append({
            'chave': f"medalha-{ano_ant:04d}-{mes_ant:02d}-{pessoa['chave_atleta']}",
            'tipo': 'medalha',
            'titulo': f"Medalha de {pessoa['nome'] or 'atleta'}",
            'destino': 'privado do atleta',
            'telefone': pessoa['telefone'],
            'periodo': f'{ano_ant:04d}-{mes_ant:02d}',
            'texto': pessoa['texto'],
            'sugerida': janela_virada and not pessoa.get('retirou'),
            'ja_feito': bool(pessoa.get('retirou')),
            'motivo_feito': 'medalha já retirada',
        })

    return saida


# ===================== CONTATO COM O ATLETA =====================

# Tom combinado com o operador: a culpa e SEMPRE do aparelho/do Strava, nunca
# da pessoa. Ninguem e acusado de nada — o texto pede ajuda para entender.
_ABERTURAS = [
    'Oi, {nome}! Tudo bem?',
    'Olá, {nome}! Tudo certo?',
    'Oi, {nome}! Passando rapidinho aqui.',
]

_EXPLICACOES = {
    'velocidade_anormal': (
        'O Strava registrou uma velocidade bem acima do normal na sua atividade '
        'de {data}. Isso costuma acontecer quando o GPS do celular se perde um '
        'pouco no caminho — acontece bastante por aqui.'
    ),
    'tempo_impossivel': (
        'O tempo que o Strava marcou na sua atividade de {data} ficou estranho, '
        'bem diferente da distância. Quase sempre é o app que pausou ou retomou '
        'sozinho no meio.'
    ),
    'veiculo_strava': (
        'O próprio Strava marcou a atividade de {data} como feita em veículo. '
        'Isso acontece quando o app se confunde com o tipo de deslocamento.'
    ),
    'marcacao_falsa': (
        'A atividade de {data} ficou com o trajeto muito parecido com o de outra '
        'pessoa. Em treino de grupo isso é comum e o sistema acaba estranhando.'
    ),
    'velocidade_parcial': (
        'Um trecho da atividade de {data} ficou com a velocidade bem fora do '
        'resto do percurso. Costuma ser o GPS dando um salto.'
    ),
}

_PADRAO = (
    'O sistema estranhou um detalhe na sua atividade de {data}. Na maioria das '
    'vezes é coisa do GPS mesmo, nada com você.'
)

_FECHOS = [
    'Você lembra se aconteceu alguma coisa nesse dia (perdeu sinal, esqueceu de '
    'parar o app, celular no carro)? Qualquer coisa ajuda a gente a acertar. '
    'Seus km continuam valendo enquanto conferimos. \U0001f44d',
    'Se puder contar como foi esse treino, a gente ajusta aqui rapidinho. '
    'Nada foi tirado do seu ranking. \U0001f603',
]

# Vai no fim de tudo que o sistema manda no privado, em italico: quem recebe
# precisa saber que nao e uma pessoa digitando do outro lado.
ASSINATURA = '_mensagem automática do sistema_'


def mensagem_para_atleta(flag, nome=None, indice=0):
    """Texto pronto para falar com o atleta sobre uma revisão.

    Tom leve e culpa no aparelho — o objetivo e entender o que houve, nao
    acusar ninguem. Uma acusacao errada custa um atleta; uma pergunta gentil
    nao custa nada.
    """
    atividade = getattr(flag, 'atividade', None)
    data = '—'
    if atividade is not None and getattr(atividade, 'data_hora', None):
        data = atividade.data_hora.strftime('%d/%m')
    primeiro_nome = (nome or 'atleta').strip().split(' ')[0]

    abertura = _ABERTURAS[indice % len(_ABERTURAS)]
    explicacao = _EXPLICACOES.get(
        (getattr(flag, 'tipo_flag', '') or '').lower(), _PADRAO)
    fecho = _FECHOS[indice % len(_FECHOS)]

    return '\n\n'.join([
        abertura.format(nome=primeiro_nome),
        explicacao.format(data=data),
        fecho,
        ASSINATURA,
    ])


