# app/vigia.py
# -*- coding: utf-8 -*-
"""O site vigiando a automacao — porque o site fica ligado e o Dell nao.

POR QUE ISTO EXISTE (pendencia P-35)
Em 02/08/2026 a fila de mensagens falhou as 15:05, 15:20, 15:35 e 15:50. As
quatro falhas foram para o log, e **ninguem soube**. O alerta de falha da
pipeline (P-04) nao cobre este caso: ele so dispara quando a rodada roda. Se a
maquina nem acorda, ou se quem falha e a drenagem da fila, nao ha rodada
nenhuma para disparar alerta.

Quem vigia tem que ser quem fica ligado. Por isso este modulo roda no DS215j,
dentro do proprio site, a cada _INTERVALO_MIN minutos.

O QUE ELE NAO FAZ, DE PROPOSITO
Nao tenta consertar nada e nao publica nada. Ele olha, e quando encontra algo
que ja deveria ter acontecido e nao aconteceu, chama o `alertas.registrar_falha`
— o mesmo caminho da P-04, entao ganha de graca o e-mail, o freio de repeticao
e o registro no historico.

O CUIDADO QUE MANDA AQUI
Alerta falso e pior que alerta nenhum: ensina a ignorar. Por isso cada checagem
so acusa o que e **inequivoco**. Ranking que nao saiu porque estava zerado, ou
porque os km nao mudaram, e comportamento correto — e nao vira alerta.
"""
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from app import db, secretstore

logger = logging.getLogger(__name__)

# De quanto em quanto tempo o vigia passa. A drenagem da fila roda de 15 em 15
# minutos no Windows; olhar no mesmo ritmo e o suficiente.
_INTERVALO_MIN = 15

# Nome da etapa do registro "nao da para vigiar". Fica numa constante porque e
# gravado e consultado em dois lugares — se os dois textos divergirem, o freio
# de 24 h para de funcionar em silencio.
_ETAPA_SEM_ENDERECO = 'Vigia sem endereco'

# Quanto tempo uma mensagem pode ficar esperando antes de virar problema.
# 45 minutos = tres drenagens perdidas. Menos que isso pegaria a fila no meio
# do trabalho normal e geraria alarme falso.
_FILA_PARADA_MIN = 45

# Quantos minutos antes da publicacao o site manda o sinal de ligar. E o tempo
# de subir WSL, Docker, os conteineres e a sessao ate ficarem prontos — medido,
# nao chutado. Publicar com a stack ainda subindo produz leitura incompleta.
_ANTECEDENCIA_WOL_MIN = 15
# Quanto tempo depois do horario da agenda o site ainda pede a rodada
# (P-139). Maior que os 15 min da passada do vigia, senao um dia a
# passada cai fora da janela e ninguem pede.
_JANELA_PEDIDO_MIN = 20

# Quanto tempo depois do horario agendado sem NENHUM sinal da automacao para
# considerar que a maquina nao acordou.
#
# 24/08/2026: baixado de 120 para 30, a pedido do dono (2h era demais — no
# incidente de 24/08 o e-mail so saiu as 07:34 para uma falha das 05:30, e
# ele ja tinha percebido sozinho antes disso). ATENCAO, TROCA CONSCIENTE: a
# tolerancia do publicador (horario_de_agora) e 90 min — o valor antigo
# ficava DE PROPOSITO acima dela, pra so alertar depois que a propria janela
# de publicacao ja tivesse se esgotado. Com 30, o alerta pode disparar
# ENQUANTO a rodada ainda teria tempo de se resolver sozinha (ex.: Dell
# ligou 40 min atrasado, ainda dentro dos 90 min de tolerancia) — e-mail de
# "falha" nesse caso pode ser falso alarme. Se isso incomodar, sobe de novo
# (o dono topou ajustar depois se precisar).
_SEM_SINAL_MIN = 30

# O uWSGI roda mais de um worker, e cada um sobe a sua thread. Sem esta trava,
# a mesma passada aconteceria duas vezes e o historico ficaria dobrado.
_TRAVA = Path(__file__).resolve().parents[1] / 'temp_extract' / 'vigia.lock'


def _cidades():
    """So as ATIVAS (P-81) -- nao toda linha de `Config`.

    Antes disto, Minaçu (vazia, sem grupo nem administrador rodando) mandava
    Wake-on-LAN as 10:27 e e-mail de "Automacao sem sinal" todo dia, so por
    ter uma linha em `Config`. Regra do dono: cidade ativa e' quem tem
    ranking rodando OU foi marcada ativa manualmente (`consolidado.cidades_ativas`).
    """
    try:
        from app import consolidado as cons
        return cons.cidades_ativas()
    except Exception as exc:
        logger.warning('vigia: nao consegui listar cidades: %s', exc)
        return []


def _estado_da_automacao(cidade):
    """(ranking_enviado_em, visto_em) da tabela automacao_estado.

    visto_em e o SINAL DE VIDA — a automacao carimba no comeco de toda rodada,
    tenha ela o que gravar ou nao. Usar "atualizado_em" aqui daria alarme falso
    toda vez que a rodada acontecesse sem novidade: ela roda, nao grava campo
    nenhum, e o site concluiria que a maquina nao acordou.
    """
    try:
        linha = db.session.execute(db.text(
            'SELECT ranking_enviado_em, COALESCE(visto_em, atualizado_em) '
            'FROM automacao_estado WHERE cidade = :c'), {'c': cidade}).first()
    except Exception:
        return None, None
    if not linha:
        return None, None
    return linha[0], linha[1]


# Quanto tempo a maquina precisa ficar quebrada antes de virar alerta. Uma
# passada do vigia (15 min) + folga: sobe/desce de container nao alcanca isso.
_QUEBRA_CONFIRMA_MIN = 12


def _confirmar_quebra(cidade):
    """A máquina está quebrada há tempo suficiente para valer um alerta?

    Guarda no cofre o instante da PRIMEIRA medição ruim. A segunda passada,
    15 minutos depois, é que autoriza o alerta — e só se a máquina continuar
    quebrada. Assim um reinício de container nunca vira e-mail.
    """
    marca = (secretstore.get(secretstore.MAQUINA_QUEBRADA_DESDE, scope=cidade) or '').strip()
    agora = datetime.now()
    if not marca:
        secretstore.set_value(secretstore.MAQUINA_QUEBRADA_DESDE,
                              agora.isoformat(timespec='seconds'),
                              scope=cidade, updated_by='vigia')
        logger.info('vigia (%s): maquina sem responder — esperando confirmar na '
                    'proxima passada antes de avisar alguem.', cidade)
        return False
    try:
        desde = datetime.fromisoformat(marca)
    except ValueError:
        return True
    return (agora - desde) >= timedelta(minutes=_QUEBRA_CONFIRMA_MIN)


def _limpar_quebra(cidade):
    """Voltou ao normal: esquece a medição ruim anterior."""
    if (secretstore.get(secretstore.MAQUINA_QUEBRADA_DESDE, scope=cidade) or '').strip():
        secretstore.delete(secretstore.MAQUINA_QUEBRADA_DESDE, scope=cidade)


# Fora da janela de publicacao, a maquina desligada (ou com WSL/containers
# fora) e o NORMAL — o Guardiao so sobe a stack dentro da janela, de proposito
# (regra do dono, 15/08). Alertar "quebrada" fora dela seria alarme por um
# estado que a propria automacao criou de caso pensado.
_JANELA_ANTES_MIN = _ANTECEDENCIA_WOL_MIN     # mesma antecedencia do Wake-on-LAN
_JANELA_DEPOIS_MIN = 90                       # mesma tolerancia de publicador.horario_de_agora


def _dentro_da_janela_de_publicacao(cidade, agora=None):
    """A automacao DEVERIA estar de pe agora? (plano attach-tidy-globe, C2/C3)

    Fonte unica: a agenda de `/admin/postagem` (a mesma que
    `_acordar_antes_da_publicacao` usa para o Wake-on-LAN) — nao um horario
    fixo duplicado aqui. Mudar o horario na tela move esta janela junto.
    """
    from app import publicador
    agora = agora or datetime.now()
    try:
        itens = publicador.agenda(cidade)
    except Exception:
        return True   # na duvida, nao esconde um alerta que podia ser real
    for item in itens:
        if not getattr(item, 'ativo', False):
            continue
        marcado = _hora_para_datetime(item.horario, agora)
        if not marcado:
            continue
        inicio = marcado - timedelta(minutes=_JANELA_ANTES_MIN)
        fim = marcado + timedelta(minutes=_JANELA_DEPOIS_MIN)
        if inicio <= agora <= fim:
            return True
    return False


def _hora_para_datetime(hhmm, dia):
    try:
        hora, minuto = str(hhmm).split(':')
        return dia.replace(hour=int(hora), minute=int(minuto),
                           second=0, microsecond=0)
    except (ValueError, AttributeError):
        return None


def problemas(cidade, agora=None):
    """[(etapa, texto)] do que esta errado NESTA cidade agora. Lista vazia = tudo bem."""
    agora = agora or datetime.now()
    achados = []

    # ---- 1. a fila parou de andar ----
    try:
        from app import filamensagens
        resumo = filamensagens.resumo(cidade)
        if resumo.get('falhadas'):
            achados.append((
                'Fila de mensagens',
                '%d mensagem(ns) desistiram depois de tentar varias vezes e NAO '
                'vao sair sozinhas.\n\n'
                'O QUE FAZER, nesta ordem:\n'
                '1) Abra a Central de Mensagens e veja quais sao — a lista diz '
                'para quem cada uma ia.\n'
                '2) Sem telefone? Cadastre em Telefones e mande de novo.\n'
                '3) Com telefone e mesmo assim falhando, o WhatsApp da cidade '
                'provavelmente precisa ser pareado outra vez.'
                % resumo['falhadas']))
        mais_antiga = resumo.get('mais_antiga')
        if resumo.get('pendentes') and mais_antiga:
            if isinstance(mais_antiga, str):
                try:
                    mais_antiga = datetime.fromisoformat(mais_antiga)
                except ValueError:
                    mais_antiga = None
            if mais_antiga and (agora - mais_antiga) > timedelta(minutes=_FILA_PARADA_MIN):
                minutos = int((agora - mais_antiga).total_seconds() // 60)
                # MAQUINA DORMINDO NAO E FILA PARADA (13/08). Ela dorme de
                # proposito entre as janelas de publicacao; a fila esperando o
                # despertar e o funcionamento normal, e alarmar nisso seria
                # alarme todo dia — o que ensina a ignorar alarme (regra 21-G).
                dormindo = False
                try:
                    from app import maquina
                    dormindo = (maquina.estado(cidade).get('situacao')
                                == maquina.DESLIGADA)
                except Exception:
                    pass
                if dormindo:
                    logger.info('vigia/fila (%s): %s mensagem(ns) esperando ha '
                                '%s min, mas a maquina esta dormindo — normal.',
                                cidade, resumo['pendentes'], minutos)
                else:
                    achados.append((
                        'Fila de mensagens',
                        '%d mensagem(ns) esperando ha %d minutos, com a maquina '
                        'da automacao LIGADA. A drenagem roda de 15 em 15 '
                        'minutos, entao alguma coisa esta impedindo a entrega.\n\n'
                        'O QUE FAZER, nesta ordem:\n'
                        '1) Veja a Sessao do WhatsApp: se ela estiver caida, e '
                        'so parear de novo e a fila sai sozinha.\n'
                        '2) Se a sessao estiver de pe, confira se o celular de '
                        'destino esta ligado e com internet.\n'
                        '3) Nada precisa ser reenviado: o que esta na fila sai '
                        'assim que o caminho voltar.'
                        % (resumo['pendentes'], minutos)))
    except Exception as exc:
        logger.warning('vigia/fila (%s): %s', cidade, exc)

    # ---- 2. passou o horario e a automacao nao deu sinal ----
    # NAO dizemos "o ranking nao saiu": ranking zerado e ranking sem novidade
    # nao saem de proposito. O que este teste afirma e mais simples e sempre
    # verdadeiro quando acusa: a automacao nao encostou no site nesta janela.
    try:
        from app import publicador
        _, visto_em = _estado_da_automacao(cidade)
        atualizado_em = visto_em
        if isinstance(atualizado_em, str):
            try:
                atualizado_em = datetime.fromisoformat(atualizado_em)
            except ValueError:
                atualizado_em = None
        for item in publicador.agenda(cidade):
            if not item.ativo:
                continue
            marcado = _hora_para_datetime(item.horario, agora)
            if not marcado or marcado > agora:
                continue
            atraso = (agora - marcado).total_seconds() / 60
            if atraso < _SEM_SINAL_MIN or atraso > _SEM_SINAL_MIN + _INTERVALO_MIN * 2:
                # Fora da janela de cobranca: ou ainda e cedo, ou ja cobramos.
                continue
            if atualizado_em and atualizado_em >= marcado:
                continue
            achados.append((
                'Automacao sem sinal',
                'Passaram %d minutos do horario das %s e a automacao nao deu '
                'sinal de vida nesta janela. O mais provavel e que o computador '
                'nao tenha acordado.' % (int(atraso), item.horario)))
    except Exception as exc:
        logger.warning('vigia/horarios (%s): %s', cidade, exc)

    # ---- 3. a maquina esta LIGADA e sem prestar ----
    # So este caso vira alerta. Desligada e o normal: a maquina da automacao nao
    # fica ligada de proposito (energia custa, e o projeto e filantropico), a
    # BIOS liga as 21:45 e o Dormir.ps1 desliga no fim. Reclamar de desligada
    # encheria o admin de alarme falso todo dia — e alarme falso ensina a
    # ignorar alarme.
    #
    # Este e o buraco que ficou aberto em 06/08: a sessao caiu as 11h10 com a
    # maquina ligada e ninguem saberia ate a rodada da noite. Nao precisou de
    # sentinela dentro do Dell: o WAHA ja responde na rede local e o site mora
    # na mesma rede.
    try:
        from app import maquina
        situacao = maquina.estado(cidade)
        if situacao['situacao'] == maquina.QUEBRADA:
            # SO ALERTA NA SEGUNDA MEDICAO SEGUIDA (15/08/2026).
            #
            # A maquina sobe e desce varias vezes por dia: o Guardiao levanta o
            # WSL, o `Agendado.bat` encerra a distro no fim da rodada, e durante
            # o arranque o WAHA responde `engine=OPENING` por um ou dois
            # minutos. Cada um desses instantes era um e-mail dizendo
            # "automacao quebrada" sobre uma sessao que, medida um minuto
            # depois, estava WORKING+CONNECTED e publicando.
            #
            # Quebra de verdade nao se conserta sozinha em 15 minutos: esperar
            # a confirmacao custa uma passada de atraso e mata o alarme falso,
            # que e o que ensina a ignorar alarme (regra 21-A).
            #
            # 17/08/2026 (plano attach-tidy-globe, C2) — FORA DA JANELA DE
            # PUBLICACAO, "LIGADA MAS SEM PRESTAR" E O NORMAL, NAO A FALHA. O
            # Guardiao so sobe WSL/Docker/containers DENTRO da janela, de
            # proposito (regra do dono, 15/08) — o computador pode estar
            # ligado por outro motivo o dia inteiro, sem que isso signifique
            # nada quebrado. Alertar aqui seria acusar o proprio desenho do
            # sistema. So vira e-mail quando a automacao DEVERIA estar de pe
            # (mesma janela usada para o Wake-on-LAN) e mesmo assim continua
            # quebrada depois de a confirmacao dar tempo do Guardiao agir.
            if not _dentro_da_janela_de_publicacao(cidade, agora):
                logger.info('vigia/maquina (%s): ligada e sem prestar fora da '
                            'janela de publicacao — normal, sem aviso.', cidade)
                _limpar_quebra(cidade)
            elif _confirmar_quebra(cidade):
                achados.append((
                    'Automacao quebrada',
                    '%s Enquanto ficar assim, a proxima publicacao nao acontece.'
                    % situacao['detalhe']))
        elif situacao['situacao'] == maquina.SEM_ENDERECO:
            # REGISTRA, MAS NAO MANDA E-MAIL (regra 21-A). Cidade sem endereco
            # cadastrado nao e uma falha que aconteceu: e uma vigilancia que
            # NUNCA COMECOU. Descoberto em 06\08 — o cofre de Palmeiropolis nao
            # tinha `waha_base_url`, entao a checagem acima jamais rodou e nada
            # denunciava isso.
            #
            # Por que sem e-mail: falta de cadastro nao se conserta sozinha, e o
            # freio do alerta e de 60 min. Seria um e-mail por hora, para sempre,
            # sobre algo que so muda quando alguem preenche um campo — e alarme
            # que nunca cala ensina a ignorar alarme (a mesma razao pela qual
            # 'desligada' nao alerta). O lugar de aparecer e o painel.
            #
            # E UMA LINHA POR DIA, nao uma a cada passada: o vigia passa de 15
            # em 15 min e isto so muda quando alguem preenche um campo — 96
            # linhas iguais por dia sujariam o historico de falhas ate ele nao
            # servir para achar mais nada.
            from app import alertas
            if not _registrado_nas_ultimas_horas(cidade, _ETAPA_SEM_ENDERECO, 24):
                alertas.registrar_falha(
                    cidade, _ETAPA_SEM_ENDERECO,
                    'A cidade nao tem o endereco do WhatsApp cadastrado, entao o '
                    'site nao consegue vigiar a maquina da automacao. Cadastre em '
                    'Painel > Sessao do WhatsApp.',
                    origem='vigia-do-site', so_registrar=True)
        if situacao['situacao'] != maquina.QUEBRADA:
            # Voltou ao normal (ou nunca esteve ruim): esquece a medicao
            # anterior, senao a proxima queda alertaria de primeira.
            _limpar_quebra(cidade)
    except Exception as exc:
        logger.warning('vigia/maquina (%s): %s', cidade, exc)

    # ---- 4. a sessao do Strava expirou ----
    try:
        if secretstore.get(secretstore.STRAVA_SESSION, scope=cidade):
            saude = secretstore.session_health(cidade) or {}
            if saude.get('ok') is False:
                achados.append((
                    'Sessao do Strava',
                    'O Strava recusou a sessao: %s. Enquanto nao recolarem o '
                    'cookie, a conferencia de km e a importacao por perfil ficam '
                    'paradas.' % (saude.get('message') or 'sem detalhe')))
    except Exception as exc:
        logger.warning('vigia/strava (%s): %s', cidade, exc)

    return achados


def _acordar_antes_da_publicacao(cidade, agora=None):
    """Manda o Wake-on-LAN alguns minutos antes de cada horario de publicacao.

    POR QUE AQUI (14/08/2026, decisao do dono)
    O horario de publicacao mora na tela (`/admin/postagem`), e mudar 10:30 para
    11:00 nao pode obrigar ninguem a mexer em outra maquina. Como o pedido de
    ligar sai do SITE — que fica ligado de dia —, o mesmo campo que define
    quando publicar passa a definir quando acordar. Um numero so, num lugar so.

    CORRECAO DE 26/08/2026: a frase acima dizia que o site "fica ligado o tempo
    todo". Nao fica -- o DS215j se desliga as 23:05 e volta as 05:13 (ver
    `app/energia.py`). Publicacao agendada para dentro dessa janela nao tem
    quem mande o Wake-on-LAN.

    OS 15 MINUTOS SAO O TEMPO DE FICAR PRONTO, nao enfeite: subir WSL, Docker,
    os conteineres e a sessao leva isso. Publicar com a stack subindo produz
    leitura incompleta, e ranking sobre leitura incompleta sai errado sem
    ninguem perceber (regra 8).

    NAO E ALTERNATIVA AO AGENDADOR (regra 21-H): o Agendador tira da SUSPENSAO,
    o Wake-on-LAN liga a maquina DESLIGADA. Sao problemas diferentes e os dois
    caminhos se somam — quem estiver disponivel resolve.

    So manda quando a maquina nao responde: pacote em maquina ligada e barulho
    a toa. Uma vez por janela, controlado pelo registro em `alertas`.
    """
    from datetime import timedelta
    agora = agora or datetime.now()
    try:
        from app import ligarmaquina, maquina, publicador
    except Exception:
        return
    if not ligarmaquina.configurado(cidade):
        return

    for item in publicador.agenda(cidade):
        if not getattr(item, 'ativo', False):
            continue
        marcado = _hora_para_datetime(item.horario, agora)
        if not marcado:
            continue
        aviso = marcado - timedelta(minutes=_ANTECEDENCIA_WOL_MIN)
        # Janela estreita: so a passada do vigia que cai dentro dela e que
        # manda. Antes disso e cedo; depois, a maquina ja devia estar de pe.
        if not (aviso <= agora < marcado):
            continue
        etapa = 'Wake-on-LAN %s' % item.horario
        if _registrado_nas_ultimas_horas(cidade, etapa, 6):
            return
        try:
            if maquina.estado(cidade).get('situacao') == maquina.LIGADA:
                return      # ja esta de pe: nada a fazer
        except Exception:
            pass
        ok, detalhe = ligarmaquina.ligar(cidade)
        logger.warning('Wake-on-LAN para a publicacao das %s (%s): %s',
                       item.horario, cidade, detalhe)
        # REGISTRA, MAS NAO MANDA E-MAIL (regra 21-A). Isto e rotina, nao
        # falha: um e-mail duas vezes por dia dizendo "liguei o computador"
        # ensina o admin a ignorar e-mail.
        try:
            from app import alertas
            alertas.registrar_falha(
                cidade, etapa,
                'Sinal de ligar enviado %d minutos antes da publicacao das %s. %s'
                % (_ANTECEDENCIA_WOL_MIN, item.horario, detalhe),
                origem='vigia-do-site', so_registrar=True)
        except Exception as exc:
            logger.debug('vigia/wol: nao consegui registrar: %s', exc)
        return


def _pedir_rodada_no_horario(cidade, agora=None):
    """No horario da agenda, registra o pedido de rodada (P-139, 20/09/2026).

    POR QUE O SITE PEDE, E NAO O AGENDADOR
    O horario mora na tela "Quem publica e quando". A tarefa do Windows tem
    05:30 e 15:30 escritos nela e mudar isso exige privilegio de administrador
    (medido em 20/09: "Acesso negado"). Enquanto a tarefa nao muda, quem faz a
    rodada acontecer na hora NOVA e' este pedido: a maquina pergunta de minuto
    em minuto se ha pedido e atende.

    NAO PUBLICA NADA AQUI. Registra o pedido — o mesmo do botao "Publicar
    agora" — e quem publica continua sendo a pipeline, com todas as travas
    dela. Duas rodadas no mesmo horario nao viram duas mensagens: `pedir`
    recusa pedido em andamento e a pipeline nao republica sem novidade.

    Uma vez por horario, controlado pelo registro em `alertas` (o mesmo
    mecanismo do Wake-on-LAN logo acima).
    """
    from datetime import timedelta
    agora = agora or datetime.now()
    try:
        from app import publicador, rodada
    except Exception:
        return
    for item in publicador.agenda(cidade):
        if not getattr(item, 'ativo', False):
            continue
        if not getattr(item, 'envia_ranking', False):
            continue        # horario que nao publica ranking nao pede rodada
        marcado = _hora_para_datetime(item.horario, agora)
        if not marcado:
            continue
        # A JANELA E ESTREITA de proposito: so a passada do vigia que cai
        # dentro dela pede. Pedir tarde demais e publicar fora de hora.
        if not (marcado <= agora < marcado + timedelta(minutes=_JANELA_PEDIDO_MIN)):
            continue
        etapa = 'Rodada agendada %s' % item.horario
        if _registrado_nas_ultimas_horas(cidade, etapa, 6):
            return
        # A RODADA DESTA JANELA JA ACONTECEU? (auditoria de 20/09/2026)
        # A tarefa do Agendador continua disparando no horario que esta nela.
        # Quando ela dispara, a pipeline busca o ranking em
        # `/api/ranking/latest`, e o site anota a hora. Se essa anotacao e'
        # posterior ao horario marcado, o ranking ja saiu — pedir de novo faria
        # uma segunda coleta inteira (fila do Strava, CPU do Dell) para nao
        # publicar nada, porque a trava "sem novidade" barraria.
        if _ranking_ja_entregue_depois(cidade, marcado):
            logger.info('vigia (%s): a rodada das %s ja buscou o ranking — '
                        'nao vou pedir outra.', cidade, item.horario)
            return
        try:
            ok, aviso = rodada.pedir(cidade, por='agenda do site (%s)' % item.horario)
        except Exception as exc:
            logger.error('vigia: nao consegui pedir a rodada das %s em %s: %s',
                         item.horario, cidade, exc)
            return
        logger.warning('Rodada das %s pedida pela agenda do site (%s): %s — %s',
                       item.horario, cidade, 'aceito' if ok else 'recusado', aviso)
        try:
            from app import alertas
            alertas.registrar_falha(
                cidade, etapa,
                'Pedido de rodada registrado no horario da agenda (%s). %s'
                % (item.horario, aviso),
                origem='vigia-do-site', so_registrar=True)
        except Exception as exc:
            logger.debug('vigia/rodada: nao consegui registrar: %s', exc)
        return


def _ranking_ja_entregue_depois(cidade, quando):
    """A automacao ja buscou o ranking DESTA cidade depois de `quando`?

    A marca e' gravada pelo proprio `/api/ranking/latest` (P-139). Sem marca —
    cidade nova, cofre ilegivel, primeira vez — devolve False: na duvida, o
    pedido nasce. Ficar sem ranking e' pior que uma rodada a mais.
    """
    from app import secretstore
    bruto = (secretstore.get(secretstore.RANKING_SERVIDO_EM, scope=cidade) or '').strip()
    if not bruto:
        return False
    try:
        return datetime.fromisoformat(bruto) >= quando
    except (ValueError, TypeError):
        return False


def _registrado_nas_ultimas_horas(cidade, etapa, horas):
    """Ja existe registro desta etapa nesta cidade dentro da janela?

    Serve para o que nao se conserta sozinho (falta de cadastro): registrar
    interessa, repetir a cada passada nao. Na duvida devolve False — registro a
    mais incomoda, registro que nunca sai e o problema que este modulo existe
    para resolver.
    """
    try:
        corte = datetime.now() - timedelta(hours=horas)
        linha = db.session.execute(db.text(
            'SELECT 1 FROM alertas_automacao WHERE cidade=:c AND etapa=:e '
            'AND criado_em >= :corte LIMIT 1'),
            {'c': cidade, 'e': etapa, 'corte': corte}).first()
        return linha is not None
    except Exception as exc:
        logger.debug('vigia: nao consegui conferir registro anterior: %s', exc)
        return False


def _pode_rodar_agora():
    """Trava entre workers: so um faz a passada em cada intervalo."""
    try:
        _TRAVA.parent.mkdir(parents=True, exist_ok=True)
        if _TRAVA.exists():
            idade = time.time() - _TRAVA.stat().st_mtime
            if idade < (_INTERVALO_MIN * 60) - 30:
                return False
        _TRAVA.write_text(str(time.time()), encoding='utf-8')
        return True
    except Exception:
        # Sem trava, prefira rodar: alerta duplicado incomoda, alerta que nunca
        # sai e o problema que este modulo existe para resolver.
        return True


def _retomar_pareamentos():
    """Relanca a insistencia dos pedidos de Conectar que ficaram sem dono.

    POR QUE (16/08/2026)
    A insistencia do pareamento (`whatsapp.atender_no_site`) vive numa thread do
    worker que atendeu o clique. Em producao o worker recicla — e a thread ia
    junto, em silencio, deixando o pedido parado na tela em "Ligando o
    computador da automacao". Este ponto passa de 15 em 15 minutos e recomeca o
    que ficou pelo caminho; quem ja esta sendo atendido e ignorado pela propria
    trava do modulo do WhatsApp.
    """
    import threading

    from flask import current_app

    from app import whatsapp as wa
    try:
        abertos = wa.pedidos_abertos()
    except Exception as exc:
        logger.warning('vigia: nao consegui listar pareamentos abertos: %s', exc)
        return
    if not abertos:
        return
    app = current_app._get_current_object()
    for pedido_id, cidade in abertos:
        logger.info('vigia: retomando a insistencia do pareamento %s (%s)',
                    pedido_id, cidade)
        threading.Thread(target=wa.atender_no_site,
                         args=(app, cidade, pedido_id),
                         daemon=True, name='pareamento-%s' % pedido_id).start()


def _confirmar_pareamentos():
    """Codigo entregue e ninguem disse que conectou — o site vai olhar.

    Sem isto a tela do pareamento parava em "Esperando voce digitar o codigo"
    mesmo com o WhatsApp ja de pe, porque quem escrevia `conectado` era so a
    rodada da maquina — que com a fila vazia podia nem acontecer.
    """
    import threading

    from flask import current_app

    from app import whatsapp as wa
    try:
        confirmar = wa.pedidos_para_confirmar()
    except Exception as exc:
        logger.warning('vigia: nao consegui listar pareamentos a confirmar: %s', exc)
        return
    if not confirmar:
        return
    app = current_app._get_current_object()
    for pedido_id, cidade in confirmar:
        threading.Thread(target=wa.confirmar_conexao,
                         args=(app, cidade, pedido_id),
                         daemon=True, name='confirma-%s' % pedido_id).start()


def rodar(agora=None, forcar=False):
    """Uma passada por todas as cidades. Devolve o que foi encontrado."""
    from app import alertas
    if not forcar and not _pode_rodar_agora():
        return []
    # PAREAMENTO VEM ANTES DE RECLAMAR, pelo mesmo motivo do Wake-on-LAN abaixo:
    # tem gente com o celular na mao esperando um codigo.
    try:
        _retomar_pareamentos()
    except Exception as exc:
        logger.warning('vigia: retomada de pareamento falhou: %s', exc)
    try:
        _confirmar_pareamentos()
    except Exception as exc:
        logger.warning('vigia: confirmacao de pareamento falhou: %s', exc)
    encontrados = []
    for cidade in _cidades():
        # LIGAR VEM ANTES DE RECLAMAR. Se a maquina precisa estar de pe daqui a
        # pouco, o util e acorda-la agora — nao registrar depois que a janela
        # passou.
        try:
            _acordar_antes_da_publicacao(cidade, agora)
        except Exception as exc:
            logger.warning('vigia/wol (%s): %s', cidade, exc)
        # PEDIR A RODADA NO HORARIO DA TELA (P-139). Depois do Wake-on-LAN, de
        # proposito: primeiro a maquina fica de pe, depois o pedido espera por
        # ela. Falha aqui nao derruba o resto da passada.
        try:
            _pedir_rodada_no_horario(cidade, agora)
        except Exception as exc:
            logger.warning('vigia/rodada (%s): %s', cidade, exc)
        for etapa, texto in problemas(cidade, agora):
            encontrados.append((cidade, etapa, texto))
            try:
                alertas.registrar_falha(cidade, etapa, texto,
                                        origem='vigia-do-site')
            except Exception as exc:
                logger.error('vigia: nao consegui alertar %s/%s: %s',
                             cidade, etapa, exc)
    if encontrados:
        logger.warning('vigia encontrou %d problema(s): %s', len(encontrados),
                       '; '.join('%s/%s' % (c, e) for c, e, _ in encontrados))
    return encontrados
