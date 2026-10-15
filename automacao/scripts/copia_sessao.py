#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A COPIA DA SESSAO: conferir se esta fresca, e guardar uma em arquivo.

POR QUE ISTO EXISTE (16/08/2026)

Sessao do WhatsApp so sobrevive a um reinicio se a copia (`RemoteAuth-<sessao>.zip`)
estiver gravada no Postgres — regra 17. E o reinicio acontece todo dia: a parada
limpa das 06:00 derruba os containers e o boot os levanta de novo.

Tres coisas medidas nesta noite mudaram o desenho:

  1. O job "RemoteAuth Backup Sync" roda **de 60 em 60 segundos** enquanto a
     sessao esta conectada. Ou seja: a copia se atualiza sozinha, e o que falta
     nao e mandar gravar — e **conferir antes de desligar**.
  2. Os `KnexTimeoutError` do WAHA aconteceram **todos** em `delete`, nenhum em
     gravacao. O risco esta em apagar, nao em salvar.
  3. A copia em ARQUIVO (`copias-sessao/`), que a regra 15-C promete como rede
     de seguranca, **parou em 11/08**. Uma rede de seguranca que ninguem
     alimenta ha cinco dias nao e rede de seguranca.

USO
    copia_sessao.py --conferir principal-pls       # idade da copia, em minutos
    copia_sessao.py --exportar principal-pls       # grava o zip em copias-sessao/
    copia_sessao.py --garantir principal-pls       # espera ficar fresca + exporta
                                                   # (e o que a parada limpa chama)

CODIGO DE SAIDA do `--garantir`: 0 = pode desligar; 1 = NAO pode (sem copia
fresca). Quem chama trata 1 como "nao derrube os containers" — perder a sessao
custa uma pessoa com um celular; ficar ligado custa energia (regra 21-D).
"""
import argparse
import base64
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wahasessao  # noqa: E402  (mesmo modulo que a pipeline usa — regra 16)

BASE = Path(__file__).resolve().parent.parent
# COPIA DE SESSAO NAO E BACKUP (13/09/2026, correcao do dono no mesmo dia).
# Eu a tinha mandado para `~/backups/waha` junto com os backups de verdade, e
# esta errado: este zip e MATERIAL DE OPERACAO — e dele que a sessao volta
# (regra 15-C), e o `provisionar` le daqui para repor a copia sem pedir QR.
# Copia de sessao perdida nao se recupera de backup nenhum; guardar copia
# invalida so gera bloat. Fica na pasta de trabalho, com o rodizio de sempre.
DESTINO = BASE / 'copias-sessao'

# Quantas copias de cada sessao ficam guardadas em arquivo. Tres cobrem "a de
# hoje, a de ontem e mais uma" — o resto e bloat: cada uma tem 12 a 33 MB, e em
# 16/08 havia um arquivo de 33 MB de 09/08 que ninguem ia usar.
MANTER_POR_SESSAO = 3

# Idade maxima da copia para a maquina poder desligar em paz. O Backup Sync roda
# a cada 60 s; 10 minutos aceitam uma batida perdida sem aceitar uma copia velha.
FRESCA_MIN = 10

# Quanto tempo esperar o Backup Sync acontecer antes de desistir.
ESPERA_MAX_SEG = 360
INTERVALO_SEG = 30


def _psql(banco, sql, timeout=90):
    """(saida, erro). Nunca levanta excecao."""
    env = wahasessao.carregar_env()
    cmd = ['docker', 'exec',
           env.get('POSTGRES_CONTAINER') or 'auto-postgres',
           'psql', '-U', env.get('POSTGRES_USER') or 'automation',
           '-d', banco, '-t', '-A', '-c', sql]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return None, str(exc)
    if r.returncode != 0:
        return None, (r.stderr or '').strip()[:200]
    return (r.stdout or '').strip(), None


def idade_da_copia(sessao):
    """(minutos, quando, erro). minutos=None significa NAO SEI ou nao ha copia.

    O horario do Postgres vem em UTC; quem le e gente, entao a conversao para o
    relogio de Palmeiropolis acontece AQUI, nunca na tela (regra do fuso).
    """
    quantos, quando, erro = wahasessao.copia_da_sessao(sessao)
    if erro:
        return None, None, erro
    if not quantos:
        return None, None, None
    if not quando:
        return None, None, 'copia sem data'
    try:
        bruto = datetime.fromisoformat(str(quando).strip())
    except ValueError:
        return None, None, 'data ilegivel: %r' % quando
    if bruto.tzinfo is None:
        bruto = bruto.replace(tzinfo=timezone.utc)
    local = bruto.astimezone()
    minutos = (datetime.now(timezone.utc) - bruto).total_seconds() / 60.0
    return minutos, local, None


def copia_em_arquivo(sessao):
    """O zip mais recente desta sessao guardado em `copias-sessao/`, ou None.

    Compara sem caixa porque nome de sessao nao distingue maiuscula (regra
    16-0): ha zips antigos gravados como `RemoteAuth-Principal-PLS_...`.
    """
    alvo = 'remoteauth-%s_' % (sessao or '').strip().lower()
    achados = [a for a in DESTINO.glob('RemoteAuth-*.zip')
               if a.name.lower().startswith(alvo)]
    if not achados:
        return None
    return max(achados, key=lambda p: p.stat().st_mtime)


def repor(sessao, arquivo=None):
    """Poe de volta no Postgres a copia guardada em arquivo. (ok, detalhe).

    POR QUE ISTO PRECISOU EXISTIR (20/08/2026)

    A copia no banco se perde de duas formas, e as duas aconteceram:

      * corrompida — depois que o ext4 do WSL quebrou, a tabela `files` ficou
        com a linha certa, o tamanho certo, e `md5(content)` respondendo
        "missing chunk number 0 for toast value ...". O WAHA entrava em laco
        tentando ler e a sessao nunca saia de STARTING.
      * apagada — a sessao cai em FAILED, mandam reiniciar, e o
        `whatsapp-web.js` chama `logout()` -> `deleteRemoteSession()`. Foi o
        que houve em 19/08 as 19:50, com a copia que o Dormir tinha acabado de
        gravar as 19:36.

    Nos dois casos existe um zip bom em `copias-sessao/`, que a regra 17 manda
    guardar exatamente para isto — e ate hoje ninguem o usava: o caminho era
    sempre parear de novo, com uma pessoa e um celular.

    O zip vai pelo STDIN do psql, nunca por argumento: sao 14 MB, e argumento
    desse tamanho estoura o limite do shell.
    """
    zip_path = Path(arquivo) if arquivo else copia_em_arquivo(sessao)
    if not zip_path or not zip_path.exists():
        return False, ('nao ha copia em arquivo para "%s" — so o pareamento '
                       'resolve' % sessao)

    banco, erro = wahasessao.banco_da_sessao(sessao)
    if erro:
        return False, 'nao descobri o banco da sessao: %s' % erro
    if not banco:
        return False, 'esta sessao ainda nao tem banco — nasce no pareamento'

    conteudo = zip_path.read_bytes()
    if not conteudo:
        return False, 'o arquivo %s esta vazio' % zip_path.name

    env = wahasessao.carregar_env()
    b64 = base64.b64encode(conteudo).decode('ascii')
    # A linha antiga sai antes (`fullpath` e UNICO). DELETE e nao TRUNCATE: o
    # TRUNCATE trava quando a linha esta com o TOAST corrompido (medido 19/08).
    comando = (
        "delete from files;\n"
        "create temp table _repor (b64 text);\n"
        "\\copy _repor from stdin\n"
        "%s\n\\.\n"
        "insert into files (fullpath, content, created_at) "
        "select 'RemoteAuth-%s.zip', decode(string_agg(b64, ''), 'base64'), now() "
        "from _repor;\n" % (b64, sessao)
    )
    cmd = ['docker', 'exec', '-i',
           env.get('POSTGRES_CONTAINER') or 'auto-postgres',
           'psql', '-U', env.get('POSTGRES_USER') or 'automation',
           '-d', banco, '-v', 'ON_ERROR_STOP=1']
    try:
        r = subprocess.run(cmd, input=comando, capture_output=True,
                           text=True, timeout=300)
    except Exception as exc:
        return False, 'nao consegui repor: %s' % exc
    if r.returncode != 0:
        return False, 'o banco recusou: %s' % (r.stderr or '').strip()[:200]

    # PROVA DE QUE FICOU LEGIVEL: `md5` percorre o conteudo inteiro. Sem isto,
    # uma copia reposta e ilegivel passaria por boa — que e exatamente o estado
    # de onde estamos saindo.
    saida, erro = _psql(banco, 'select md5(content) from files;', timeout=120)
    if erro or not (saida or '').strip():
        return False, ('reposta, mas nao consegui ler de volta: %s'
                       % (erro or 'sem resposta'))
    return True, ('copia de %s (%.1f MB) reposta e conferida'
                  % (zip_path.name, len(conteudo) / 1024 / 1024))


def forcar(sessao):
    """GERA a copia do perfil vivo e grava no Postgres. (ok, detalhe).

    POR QUE ISTO EXISTE (12/09/2026 — regra do dono: "a copia nao pode falhar")

    Ate hoje a copia dependia INTEIRAMENTE do job "RemoteAuth Backup Sync" do
    WAHA, que grava de 60 em 60 s. Medido nesta madrugada: quando a injecao do
    whatsapp-web.js falha na subida, a sessao fica WORKING + CONNECTED mas nunca
    fica "ready" — e **o job nao comeca**. A copia congela (ficou parada em 23:49
    com a sessao de pe) e no proximo reinicio o WAHA restaura um retrato velho.
    O WAHA nao oferece jeito de forcar: conferido na documentacao oficial, nao ha
    endpoint de backup nem variavel de intervalo.

    Entao a copia passa a ser NOSSA quando precisa ser. O formato foi medido
    abrindo o zip que o proprio WAHA gravou: e o perfil do Chromium, com duas
    subarvores e nada mais —

        Default/Local Storage/leveldb    (6 arquivos)
        Default/IndexedDB/https_web.whatsapp.com_0.indexeddb.leveldb  (31)
        Default/IndexedDB/https_web.whatsapp.com_0.indexeddb.blob     (5)

    ... e o `zip` usado e o que ja existe dentro do container (`WAHA_ZIPPER=
    ZIPUNZIP`). A copia gerada aqui bateu entrada por entrada com a do WAHA.

    O CAMINHO: gera o zip dentro do container -> tira para `copias-sessao/` ->
    confere que o zip abre (`testzip`) -> `repor()` grava no Postgres e confere
    com `md5`. Nenhum passo depende do job do WAHA.

    NAO FORCA sessao morta: perfil de aparelho desvinculado nao serve de copia.
    """
    import zipfile

    env = wahasessao.carregar_env()
    # A PROMESSA ACIMA AGORA E CUMPRIDA (18/09/2026, plano A8). A docstring
    # dizia 'NAO FORCA sessao morta', mas o codigo nao olhava o estado: so
    # o `sessao_viva` (um dos tres caminhos) filtrava. Em 12/09 uma copia de
    # perfil sem vinculo foi gravada por cima da boa. So WORKING + CONNECTED;
    # sem resposta do WAHA, NAO SEI -> nao forca.
    _item = wahasessao.detalhar(env.get('WAHA_BASE') or 'http://localhost:3000',
                                env.get('WAHA_API_KEY', ''), sessao)
    _st, _mo = wahasessao.estado_de(_item)
    if not wahasessao.pronta(_st, _mo):
        return False, ('nao forco copia da sessao %s: ela esta em %s/%s, sem '
                       'vinculo vivo provado' % (sessao, _st or '?', _mo or '?'))
    container = env.get('WAHA_CONTAINER') or 'auto-waha-pls'
    perfil = '/app/.wwebjs_auth/RemoteAuth-%s' % sessao
    dentro = '/tmp/copia_forcada_%s.zip' % sessao

    def _no_container(comando, timeout=300):
        try:
            r = subprocess.run(['docker', 'exec', container, 'sh', '-c', comando],
                               capture_output=True, text=True, timeout=timeout)
        except Exception as exc:
            return None, str(exc)[:200]
        if r.returncode != 0:
            return None, (r.stderr or r.stdout or '').strip()[:200]
        return (r.stdout or '').strip(), None

    _saida, erro = _no_container('test -d "%s/Default/IndexedDB"' % perfil, 60)
    if erro is not None:
        return False, ('nao achei o perfil vivo em %s (container %s): %s'
                       % (perfil, container, erro))

    # LIMPAR ORFAOS ANTES DE GERAR (12/09/2026 — recomendacao da comunidade do
    # whatsapp-web.js, issues #3724 e #2530). Processo que morre no meio do
    # backup deixa `wwebjs_temp_session_*` e zip pela metade, e e' isso que
    # corrompe a restauracao seguinte. O WAHA apaga o tempDir no fim do backup
    # dele, mas nao apaga o que sobrou de uma queda. Aqui nao custa nada.
    _no_container('rm -rf /app/.wwebjs_auth/wwebjs_temp_session_* '
                  '/app/.wwebjs_auth/*.zip', 60)

    # REPETIR UMA VEZ SE O ZIP FALHAR. O perfil esta VIVO: o leveldb pode estar
    # sendo escrito no exato momento da leitura (a comunidade relata EBUSY no
    # `fs.copy` do proprio WAHA, issue #1893). Uma segunda tentativa 5 s depois
    # resolve o caso comum; duas falhas seguidas e problema de verdade.
    comando_zip = ('cd "%s" && rm -f "%s" && zip -r -q "%s" '
                   '"Default/Local Storage/leveldb" "Default/IndexedDB"'
                   % (perfil, dentro, dentro))
    _saida, erro = _no_container(comando_zip)
    if erro is not None:
        time.sleep(5)
        _saida, erro = _no_container(comando_zip)
    if erro is not None:
        return False, 'o zip nao foi gerado (duas tentativas): %s' % erro

    DESTINO.mkdir(parents=True, exist_ok=True)
    carimbo = datetime.now().strftime('%Y%m%d_%H%M')
    arquivo = DESTINO / ('RemoteAuth-%s_%s.zip' % (sessao, carimbo))
    try:
        r = subprocess.run(['docker', 'cp', '%s:%s' % (container, dentro),
                            str(arquivo)], capture_output=True, text=True,
                           timeout=300)
    except Exception as exc:
        return False, 'nao consegui tirar o zip do container: %s' % str(exc)[:150]
    if r.returncode != 0:
        return False, ('nao consegui tirar o zip do container: %s'
                       % (r.stderr or '').strip()[:150])
    _no_container('rm -f "%s"' % dentro, 60)

    # ZIP QUE NAO ABRE NAO E COPIA. Sem esta conferencia, gravar um arquivo
    # truncado no banco seria trocar "sem copia" por "copia que engana".
    try:
        with zipfile.ZipFile(arquivo) as z:
            ruim = z.testzip()
            nomes = z.namelist()
    except Exception as exc:
        return False, 'o zip gerado nao abre (%s)' % str(exc)[:120]
    if ruim is not None:
        return False, 'o zip gerado esta corrompido em %s' % ruim
    # AS DUAS SUBARVORES QUE O WAHA GUARDA (`REQUIRED_DIRS` do RemoteAuth dele):
    # IndexedDB e' onde mora a credencial; Local Storage e' o que o WhatsApp Web
    # le no arranque. Zip sem uma das duas restaura pela metade e cai em QR.
    if not any('indexeddb' in n.lower() for n in nomes):
        return False, ('o zip gerado nao tem o IndexedDB do WhatsApp — %d '
                       'entrada(s) so' % len(nomes))
    if not any('local storage' in n.lower() for n in nomes):
        return False, ('o zip gerado nao tem o Local Storage — %d entrada(s) so'
                       % len(nomes))

    _rodizio(sessao)
    ok, detalhe = repor(sessao, arquivo)
    tamanho = arquivo.stat().st_size / 1024 / 1024
    if not ok:
        return False, 'gerei %s (%.1f MB) mas %s' % (arquivo.name, tamanho, detalhe)
    return True, ('copia GERADA do perfil vivo (%s, %.1f MB, %d entradas) e '
                  'gravada no banco — %s' % (arquivo.name, tamanho, len(nomes),
                                             detalhe))


def exportar(sessao):
    """Grava a copia do Postgres num arquivo. (caminho, erro)."""
    banco, erro = wahasessao.banco_da_sessao(sessao)
    if erro:
        return None, erro
    if not banco:
        return None, 'nao ha banco desta sessao — logo, nao ha copia'

    saida, erro = _psql(banco,
                        "select encode(content, 'base64') from files "
                        "order by created_at desc limit 1;")
    if erro:
        return None, erro
    if not saida:
        return None, 'a tabela files esta vazia'

    DESTINO.mkdir(parents=True, exist_ok=True)
    carimbo = datetime.now().strftime('%Y%m%d_%H%M')
    arquivo = DESTINO / ('RemoteAuth-%s_%s.zip' % (sessao, carimbo))
    try:
        arquivo.write_bytes(base64.b64decode(saida))
    except Exception as exc:
        return None, 'nao consegui gravar o arquivo: %s' % exc
    _rodizio(sessao)
    return arquivo, None


def _rodizio(sessao):
    """Mantem so as copias mais recentes desta sessao. Bloat tambem e defeito."""
    arquivos = sorted(DESTINO.glob('RemoteAuth-%s_*.zip' % sessao),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    for velho in arquivos[MANTER_POR_SESSAO:]:
        try:
            velho.unlink()
            print('   removida a copia antiga %s' % velho.name)
        except Exception as exc:
            print('   nao consegui remover %s: %s' % (velho.name, exc))


def garantir(sessao):
    """Espera a copia ficar fresca e guarda uma em arquivo. True = pode desligar."""
    fim = time.time() + ESPERA_MAX_SEG
    avisou = False
    while True:
        minutos, quando, erro = idade_da_copia(sessao)
        if erro:
            print('NAO SEI dizer da copia de %s: %s' % (sessao, erro))
            # `nao sei` nao autoriza desligar: e o mesmo criterio da regra 21-D.
            return False
        if minutos is not None and minutos <= FRESCA_MIN:
            print('Copia de %s gravada as %s (ha %d minuto(s)) — pode desligar.'
                  % (sessao, quando.strftime('%d/%m %H:%M'), int(minutos)))
            arquivo, erro = exportar(sessao)
            if arquivo:
                print('Copia guardada tambem em arquivo: %s' % arquivo.name)
            else:
                # Nao impede o desligamento: a copia do Postgres, que e a que o
                # WAHA usa para voltar, ja esta boa. O arquivo e o plano B.
                print('AVISO: nao consegui guardar a copia em arquivo (%s)' % erro)
            return True

        if time.time() >= fim:
            if minutos is None:
                print('SEM COPIA GRAVADA para %s depois de %d minutos de espera.'
                      % (sessao, ESPERA_MAX_SEG // 60))
            else:
                print('A copia de %s tem %d minutos e nao foi atualizada.'
                      % (sessao, int(minutos)))

            # ESPERAR NAO E A UNICA OPCAO (12/09/2026 — regra do dono: "a copia
            # nao pode falhar"). Se o job do WAHA nao gravou, nos gravamos: o
            # `forcar` zipa o perfil vivo do Chromium e poe no Postgres. So
            # depois disso e que "nao da para desligar" e verdade.
            print('Esperar nao resolveu — vou GERAR a copia do perfil vivo.')
            ok, detalhe = forcar(sessao)
            print('   %s' % detalhe)
            if ok:
                minutos, quando, erro = idade_da_copia(sessao)
                if erro is None and minutos is not None and minutos <= FRESCA_MIN:
                    print('Copia forcada conferida (ha %d minuto(s)) — pode '
                          'desligar.' % int(minutos))
                    return True
                print('A copia forcada nao apareceu fresca na leitura seguinte '
                      '(%s) — nao vou autorizar a parada.'
                      % (erro or 'idade desconhecida'))
            return False

        if not avisou:
            avisou = True
            if minutos is None:
                print('Ainda nao ha copia de %s — o Backup Sync do WAHA roda de '
                      'minuto em minuto; esperando ate %d minutos.'
                      % (sessao, ESPERA_MAX_SEG // 60))
            else:
                print('A copia de %s tem %d minutos (o limite e %d) — esperando '
                      'o proximo Backup Sync.' % (sessao, int(minutos), FRESCA_MIN))
        time.sleep(INTERVALO_SEG)


def sessao_viva():
    """O nome da sessao de pe, descoberto aqui. None quando nao ha.

    POR QUE ISTO EXISTE (17/08/2026, bug meu na primeira parada limpa)

    O `Dormir.ps1` descobria o nome rodando `wahasessao.py` e PASSANDO O TEXTO
    para ca. O PowerShell estava com a saida em UTF-16 naquele trecho (o `wsl -l`
    responde assim) e o nome chegou embaralhado — `牰湩楣慰⵬汰米...` —, a copia
    "nao foi encontrada" e a parada limpa abortou, com a sessao perfeitamente
    gravada. A trava funcionou, mas pelo motivo errado.

    O conserto nao e ajustar a codificacao e torcer: e **nao passar texto**.
    Quem precisa do nome descobre o nome, no mesmo modulo que a pipeline usa
    (regra 16: um lugar so decide qual sessao e a boa).
    """
    try:
        nome, status, _estado, _quantas = wahasessao.resolver()
    except Exception as exc:
        print('nao consegui descobrir a sessao viva: %s' % exc)
        return None
    # SESSAO SEM VINCULO NAO TEM O QUE PROTEGER (14/09/2026).
    #
    # Isto devolvia o nome de QUALQUER sessao encontrada, sem olhar o estado.
    # Medido em 14/09: com a sessao em SCAN_QR_CODE (nunca pareada), o Guardiao
    # dizia "Sessao de pe", esperava 6 minutos por uma copia que nao tinha como
    # existir e tentava forcar copia de um perfil vazio — o mesmo caminho que em
    # 12/09 gravou lixo por cima da copia boa. Seis minutos presos a cada
    # passada de dez.
    #
    # A pergunta que quem chama faz e "ha um VINCULO que se perde se eu
    # derrubar?". SCAN_QR_CODE, FAILED e STOPPED garantidamente nao tem vinculo
    # vivo. WORKING e STARTING continuam protegidos de proposito: podem ser uma
    # sessao pareada subindo, e na duvida o container fica de pe.
    if nome and status in ('SCAN_QR_CODE', 'FAILED', 'STOPPED'):
        print('Sessao %s em %s — sem vinculo vivo, nada a proteger.' % (nome, status))
        return None
    return nome


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--conferir', metavar='SESSAO')
    p.add_argument('--exportar', metavar='SESSAO')
    p.add_argument('--garantir', metavar='SESSAO')
    p.add_argument('--garantir-a-viva', action='store_true',
                   help='descobre a sessao de pe e garante a copia dela. '
                        'E o que a parada limpa chama — sem passar nome nenhum.')
    p.add_argument('--repor', metavar='SESSAO',
                   help='poe de volta no Postgres o zip guardado em '
                        'copias-sessao/ — evita parear quando a copia do banco '
                        'foi apagada ou corrompida.')
    p.add_argument('--de', metavar='ARQUIVO.zip',
                   help='com --repor: usa este arquivo em vez do mais recente.')
    p.add_argument('--forcar', metavar='SESSAO',
                   help='GERA a copia do perfil vivo do Chromium e grava no '
                        'Postgres, sem depender do job do WAHA. E o que se usa '
                        'quando a copia envelheceu com a sessao de pe.')
    args = p.parse_args()

    if args.forcar:
        ok, detalhe = forcar(args.forcar)
        print(detalhe)
        return 0 if ok else 1

    if args.repor:
        ok, detalhe = repor(args.repor, args.de)
        print(detalhe)
        return 0 if ok else 1

    if args.garantir_a_viva:
        nome = sessao_viva()
        if not nome:
            print('Nenhuma sessao de pe — nao ha copia a proteger.')
            return 0        # sem sessao, parar e seguro
        print('Sessao de pe: %s' % nome)
        return 0 if garantir(nome) else 1

    if args.conferir:
        minutos, quando, erro = idade_da_copia(args.conferir)
        if erro:
            print('nao sei: %s' % erro)
            return 2
        if minutos is None:
            print('SEM COPIA')
            return 1
        print('%d minuto(s) — gravada em %s'
              % (int(minutos), quando.strftime('%d/%m/%Y %H:%M')))
        return 0

    if args.exportar:
        arquivo, erro = exportar(args.exportar)
        if erro:
            print('nao deu: %s' % erro)
            return 1
        print(arquivo)
        return 0

    if args.garantir:
        return 0 if garantir(args.garantir) else 1

    p.print_help()
    return 2


if __name__ == '__main__':
    sys.exit(main())
