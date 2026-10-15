#!/usr/bin/env python3
import sys
import os
from datetime import datetime

# Define a codificaÃ§Ã£o de I/O para UTF-8 para evitar UnicodeEncodeError em logs
# ESTA Ã‰ A CORREÃ‡ÃƒO PARA O UnicodeEncodeError
os.environ['PYTHONIOENCODING'] = 'utf8' 

# ATIVAR O VENV - ISSO Ã‰ O QUE ESTAVA FALTANDO!
activate_this = '/volume1/web/movimente-se/venv/bin/activate_this.py'
if os.path.exists(activate_this):
    with open(activate_this) as f:
        exec(f.read(), {'__file__': activate_this})
else:
    # Alternativa se nÃ£o tiver activate_this.py
    sys.path.insert(0, '/volume1/web/movimente-se/venv/lib/python3.9/site-packages')

# Log de debug (Ãºtil para verificar inicializaÃ§Ã£o em ambiente de produÃ§Ã£o)
# Modo 'w': mantÃ©m apenas o Ãºltimo boot (o arquivo crescia sem limite).
#
# ESTE ARQUIVO E O RELOGIO DO BOOT (16/08/2026). O py-autoreload do uWSGI
# nao pegou o conserto do 'pedido_ranking' em admin.py por HORAS -- o boot
# aqui registrado ficou parado em 15/08 18:31:45 mesmo com admin.py e
# rodada.py mudando depois disso. Editar SO wsgi.py (este arquivo) e o teste
# barato pra saber se o autoreload esta vivo: se um boot novo aparecer aqui
# pouco depois de salvar, ele reagiu a ESTE arquivo -- e o worker sobe com
# TUDO que estiver em disco nesse instante, nao so com o que mudou agora.
#
# Ultimo toque: 10/09/2026 (11a passada) -- BOTAO "PARAR A AUTOMACAO AGORA"
# FICA SEMPRE VISIVEL. Antes so aparecia depois de ja ter clicado "Manter de
# pe", escondendo o botao exatamente de quem queria parar uma automacao que
# subiu sozinha (ex.: pela janela normal do ranking). Clicar sem nada de pe
# e seguro (idempotente) -- este toque e o que faz a mudanca de template
# aparecer (Jinja fica em cache ate o proximo reload).
# Arquivos: app/templates/admin/dashboard.html.

# Ultimo toque: 10/09/2026 (10a passada) -- O GUARDIAO DERRUBAVA O WAHA NO
# MEIO DE UMA PUBLICACAO EM ANDAMENTO.
#
# Incidente de 09-10/09/2026, medido cruzando 3 fontes (log do site, log do
# Guardiao, log da propria automacao -- nao foi suposicao de horario, foi a
# mensagem de erro do processo): o dono marcou "manter de pe" por 30 min as
# 23:27:52 e clicou "Publicar agora" as 23:45:24. A pipeline demorou mais que
# o normal (competiu pelo limite do Strava com um upload manual concorrente)
# e so tentou enviar as 23:58:48 -- mas o Guardiao, que so olha o prazo e nao
# sabe se ha publicacao em andamento, tinha mandado `docker stop` as 23:57:55
# porque o prazo tinha vencido as ~23:55:52. A tentativa de envio bateu direto
# em "Connection refused" na porta do WAHA (send_ranking_20260909_235034.log).
# A sessao, cortada de forma abrupta em vez de terminar sozinha, ficou
# instavel e caiu de novo na tentativa seguinte.
#
# `publicar_agora()` agora chama `_garantir_containers_de_pe(..., 30)`: ao
# registrar o pedido, estende o prazo da janela para pelo menos 30 minutos a
# partir do clique -- nunca reduz um prazo maior ja marcado, nunca mexe em
# "ate mandarem parar". A pipeline que ELA MESMA disparou passa a ter tempo
# de terminar antes do Guardiao poder derruba-la.
#
# Arquivos: app/blueprints/admin.py (`_garantir_containers_de_pe`,
# `publicar_agora`).

# Ultimo toque: 09/09/2026 (9a passada) -- LIMPEZA, E UMA ORFA QUE ERA GRAVE.
#
# A varredura de codigo sem chamador (5 candidatas em ~50 mil linhas) achou
# `parametrosvel.teto_para` SEM NENHUM LEITOR: a tela "Velocidade plausivel"
# gravava a regua do dono e nada a lia -- ele ajustaria as flags e nada
# mudaria. Agora `consolidado._teto_do_dono` a le na tabela de recordes, com o
# recorde mundial ainda como limite absoluto. Provado: corrida 3 km = 17,0 /
# 21 km = 14,0 / ciclismo = 33,0 km/h, e o recorde de corrida caiu de 109 para
# 16,9 km/h.
# Junto saiu o que estava morto de verdade: `_is_rate_limited` e suas duas
# constantes (substituidas por `stravasinal` em 29/08 -- conferido que ele
# cobre inclusive o desafio do CloudFlare com HTTP 200) e dois templates sem
# referencia (`admin/mensagens.html`, com copia guardada em Backups, e
# `config/config.html`, que esta no git). As 3 funcoes que sobraram na lista
# sao chamadas pelo framework (event listener, before_app_request, WTForms).
# `parametrosvel.tetos` ganhou memoria curta com invalidacao no gravar: sem ela
# eram duas consultas por atividade num NAS de 490 MB.

# Ultimo toque: 09/09/2026 (8a passada) -- ATIVIDADE QUE PERDE A FLAG VOLTA AO
# RANKING. Defeito achado ao apagar as flags de grupo: `recalcular_controle_
# disciplinar` so visita atividade que AINDA tem flag, entao a que perdeu a
# ultima ficava removida para sempre, com o marcador colado. Medido: 59
# atividades e 712 km devolvidos (19.368 -> 19.427 ativas). Vale para qualquer
# flag apagada no futuro, nao so as de grupo.

# Ultimo toque: 09/09/2026 (7a passada) -- A FLAG "GRUPO SUSPEITO" DEIXOU DE
# EXISTIR, e a tela de revisoes ganhou "aprovar marcadas".
#
# O detector marcava quem se exercitou no MESMO HORARIO com distancia parecida,
# e so quando a atividade nao tinha GPS. Nao e evidencia de nada: treinar junto
# e o comportamento normal de um grupo de corrida de cidade pequena. Medido:
# 762 flags em aberto -- METADE de todas as flags do sistema -- enterrando as
# que importam. Quem prova marcacao falsa e o sha do trajeto
# (`detect_marcacao_falsa`), que ja existia. As 766 flags do tipo foram
# apagadas depois da remocao do detector (fonte antes do derivado, regra 9),
# com snapshot em `instance/movimente_se.pre_flags_grupo_20260909_213753.db`.
#
# Junto: `send_ranking._causa_da_queda()` passou a dizer POR QUE a sessao caiu
# (navegador morto / LOGOUT / Postgres sem conexao), porque cada uma pede uma
# acao diferente do dono -- e em 09/09 o conselho generico o teria mandado
# parear a toa.

# Ultimo toque: 08/09/2026 (6a passada) -- SESSAO QUE SUMIU SAI DO REGISTRO.
#
# MEDIDO no teste de exclusao do suplente: apaguei a sessao `suplente-pls` do
# WAHA e a tabela `instancias_waha` continuou com a linha dela — dizendo
# WORKING. O relato da maquina sempre foi um `upsert`: gravava o que chegava e
# nunca tirava o que deixava de chegar.
#
# Registro que afirma o que nao existe e' pior que registro vazio. Foi esse
# tipo de mentira que fez o dono clicar em "Limpar" esperando desconectar
# alguma coisa, e que me fez diagnosticar o site como fora do ar quando ele
# estava de pe.
#
# A GUARDA CONTRA O ESTRAGO OPOSTO: so' se apaga linha de container que
# RELATOU ALGUMA SESSAO nesta passada. Relato vazio quer dizer "nao consegui
# perguntar" (o WAHA pode estar subindo), e "nao sei" nunca pode virar "entao
# nao existe" — foi assim que uma faxina quase apagou sessao viva em 05/08.
# Container que nem apareceu no relato nao e' tocado.
#
# Arquivos: app/whatsapp.py.
#
# Toque anterior: 08/09/2026 (5a passada) -- LIMPAR SESSAO FICA JUNTO DO NUMERO.
#
# O botao de apagar tinha nascido numa tabela no rodape da tela ("O que existe
# na maquina"). Estava certo tecnicamente e errado na pratica: quem quer mexer
# numa sessao olha o CARD DO NUMERO, la em cima. Agora ele mora ali, dentro de
# um `<details>` fechado -- visivel para quem procura, fora do caminho de quem
# so' quer conectar.
#
# SO APARECE QUANDO HA O QUE APAGAR: com `estado.chave == 'sem_relato'` a
# maquina nunca contou que aquela sessao existe, e oferecer "limpar" ali seria
# prometer uma acao sem alvo.
#
# NAO CONFUNDIR COM O "Limpar" VIZINHO: aquele some com o REGISTRO do pedido e
# nao desconecta nada (o proprio titulo dele avisa). Este apaga a sessao no
# WAHA, e SO a dela -- as outras sessoes do mesmo container nao sao tocadas.
#
# Arquivos: app/templates/admin/whatsapp_sessao.html.
#
# Toque anterior: 08/09/2026 (4a passada) -- A LISTA DE ATLETAS CABE NA TELA.
#
# TRES FAIXAS, NAO SEIS. O topo virava titulo / botoes / busca / "mostrar
# arquivados" -- quatro faixas so de cabecalho numa tela de 812 px, mais o
# cabecalho da tabela. Agora sao TRES: controles, busca e o cabecalho da
# tabela. Medido no celular: o topo caiu de 150 px para 80, e passaram a caber
# 13 linhas de atleta.
#
# O QUE FEZ AS DUAS PRIMEIRAS FAIXAS SE JUNTAREM foi `flex-wrap: nowrap` --
# mas ele sozinho estourou a tela: a faixa fechou em 376 px num viewport de
# 375, e UM pixel basta para a pagina inteira ganhar rolagem lateral. O
# conserto e' deixar o titulo CEDER espaco (`min-width: 0` + reticencias) em
# vez de a faixa transbordar.
#
# NO CELULAR NAO SE ROLA MAIS DE LADO. Eram 8 colunas em 375 px: o conteudo
# pedia 477 e o que ficava escondido a direita era a coluna de ACOES, a unica
# em que se clica. Somem Cidade (a lista ja vem filtrada), Strava ID e Desde;
# os 134 px que faltavam sairam de onde nao custam informacao -- respiro
# lateral menor, rotulo dos botoes escondido (o icone continua dizendo o que
# faz) e as duas colunas de texto cortando com reticencias.
#
# QUEM CORTA E O APELIDO, NAO O NOME (correcao do dono). A primeira versao
# repartia a largura meio a meio e cortava o nome -- errado: o nome e' como a
# pessoa e' PROCURADA na lista, e nome cortado obriga a abrir a ficha para
# saber de quem se trata. Apelido cortado ainda se reconhece.
#
# Fechou em 343 px de conteudo para 343 disponiveis: zero rolagem horizontal,
# na pagina e na tabela. No computador nada muda -- as 8 colunas voltam.
#
# Arquivos: app/templates/admin/lista_atletas.html, app/static/css/style.css.
#
# Toque anterior: 08/09/2026 (3a passada) -- APAGAR SESSAO DO WHATSAPP, E O TOPO
# INTEIRO DA LISTA DE ATLETAS CONGELADO.
#
# (1) APAGAR SESSAO -- o terceiro caminho que faltava.
# A tela de Sessao tinha dois vizinhos e nenhum servia:
#   * "Limpar" (`parar_pedido`) some com o REGISTRO e NAO desconecta nada. Foi
#     clicando nele que a sessao sumiu da tela e continuou WORKING dentro do
#     WAHA, gastando memoria, enquanto a tela dizia "conectada" -- e dizia a
#     verdade, ela estava mesmo.
#   * "Refazer a conexao" apaga a sessao E RECRIA, entao sempre cobra um
#     pareamento.
# Faltava apagar SEM recriar: o unico jeito de devolver a memoria de um numero
# que nao se quer manter de pe agora.
#
# APAGAR SESSAO `WORKING` E PERMITIDO, de proposito. A primeira versao proibia,
# e proibir resolvia o clique errado criando outro problema: o dono queria
# exatamente isso -- tirar o suplente do ar para enxugar a RAM ate a migracao.
# Proibir seria fazer do dono refem do proprio sistema. A protecao e' o nome da
# sessao DIGITADO mais uma caixa de ciencia, porque apagar custa um pareamento
# e pareamento depende de gente com o celular na mao.
# O estado vem do WAHA NA HORA, nunca do registro do site -- registro e' foto,
# e foto envelhece. O banco da sessao NAO e' derrubado daqui: ele vive no
# Postgres do Dell, e site que apaga banco em outra maquina cobra caro no dia
# em que a rede oscilar.
#
# (2) O TOPO DA LISTA DE ATLETAS CONGELA INTEIRO.
# Congelar so o `thead` (2a passada) resolveu as colunas e deixou outro buraco:
# ao rolar, os botoes "Atualizar fotos/nomes" e a busca saiam da tela, e sao
# eles que a pessoa precisa alcancar de qualquer ponto de uma lista de 102
# nomes. Agora o bloco inteiro e' `sticky`.
#
# ONDE ELE COLA NAO PODE SER NUMERO FIXO: a navbar e' `sticky-top` e a altura
# dela MUDA com a largura -- medido em 08/09, 62px numa janela e 102px noutra,
# onde o menu quebra em duas linhas. `main.js` MEDE a navbar e publica
# `--altura-navbar`; o CSS usa `top: var(--altura-navbar, 72px)`, e o fallback
# cobre o caso de o script nao rodar.
#
# Arquivos: app/whatsapp.py, app/blueprints/admin.py,
# app/templates/admin/whatsapp_sessao.html, app/templates/admin/lista_atletas.html,
# app/static/css/style.css, app/static/js/main.js.
#
# Toque anterior: 08/09/2026 (2a passada) -- CABECALHO FIXO NA LISTA DE ATLETAS.
#
# Com 102 atletas, rolar a lista era perder as colunas: quem descia dez linhas
# ja nao sabia qual era "Nome no ranking" e qual era "Strava ID".
#
# O `thead` agora e' `sticky`. A armadilha: o `.table-responsive` do Bootstrap
# tem `overflow-x: auto`, e overflow num eixo cria contexto de rolagem nos DOIS
# -- o cabecalho grudaria no topo de um container que nao rola, e o efeito
# seria nenhum. Em vez de lutar contra isso, a area da tabela ganhou TETO DE
# ALTURA e passou a rolar por dentro; o cabecalho gruda no topo dela. De
# quebra, titulo e busca param de sair da tela.
#
# No celular o bloco de cima encolheu so' no vertical (botoes com `nowrap`,
# que antes quebravam em duas linhas cada). Alvo de toque mantido em 38px:
# abaixo disso erra o dedo, e errar em "Atualizar fotos" custa uma rodada no
# Strava.
#
# ESTE TOQUE E' O QUE FAZ A MUDANCA APARECER. CSS e HTML nao reiniciam a
# aplicacao, mas o Flask guarda o TEMPLATE compilado em memoria: as classes
# novas (`tabela-atletas`, `cabecalho-atletas`, `filtro-atletas`) so passam a
# ser renderizadas depois do reload. O CSS, esse, pode pedir Ctrl+F5 -- o
# cache de quem ja abriu a pagina e do navegador, nao nosso.
#
# Arquivos: app/templates/admin/lista_atletas.html, app/static/css/style.css.
#
# Toque anterior: 08/09/2026 -- MESCLAR ATLETAS DUPLICADOS, UMA LOGO POR VEZ NO
# RODAPE, E O DESLIGAMENTO CONDICIONAL AO CABO.
#
# (1) MESCLAGEM DE ATLETAS. Pedido do dono: atleta duplicado (2+ strava_id da
# mesma pessoa) nao consolidava km e aparecia repetido no ranking. Novo campo
# `Atleta.atleta_canonico_id` (models.py, auto-referenciado, sem cadeias):
# quem perde a fusao aponta pro cabeca do grupo e fica `arquivado=True`
# (reaproveitado, nao um estado novo). Km somado em TEMPO REAL (sem coluna
# agregada nem job novo) em todo lugar que soma por `id_usuario_strava` ou
# `atleta_id`: pagina publica do atleta (athlete.py, agora com tag por
# strava_id do grupo, a mais recente maior/vermelha, e faixa do cabecalho
# mais fina e fixa ao rolar), lista e ajuste manual do admin (admin.py, com
# a UI de selecionar 2+ e clicar "Mesclar"), anticheat (flag/disciplinar
# novos ja nascem no cabeca), RANKING MENSAL/ANUAL PUBLICADO
# (`ranking.process_ranking` e `_activity_athlete_key`, o ponto que evita a
# posicao duplicada) e as tabelas da home (`consolidado._chave_atleta`, via
# CASE WHEN so quando ha mesclagem de verdade). Importacao (`upload.py`)
# passou a aceitar link do proprio site (`/ath/<id>`,
# `/admin/atletas/<id>/ajuste-manual`) alem do link do Strava.
#
# (2) PATROCINADOR: SO UMA LOGO POR VEZ. Achado ao investigar o relato do
# dono (3 logos juntas no ranking das 15h30): o rodizio por `peso_frequencia`
# (`apoio_da_vez`) sempre filtrou corretamente o TEXTO do ranking -- o defeito
# estava em `api._sponsors_payload`, que mandava TODOS os patrocinadores do
# periodo pra automacao, sem filtro nenhum, sempre. Agora ela usa o mesmo
# `apoio_da_vez`: texto e logo sempre do mesmo sorteio, nunca separados.
# De quebra, a escala de frequencia foi redividida (pedido do dono): em vez
# de peso relativo numa fila infinita (so' aproximava uma MEDIA de longo
# prazo), agora e' uma GRADE FIXA de 14 vagas por semana (2 rankings/dia x 7
# dias), cada categoria com uma contagem exata (1=3x, 2=5x, 3=7x, 4=10x,
# 5=14x/semana), espalhada o mais uniforme possivel (`_grade_semanal`,
# `_posicoes_uniformes`). O principal continua no MESMO mecanismo (nao um
# fallback em separado), so' que processado por ultimo -- absorve o que
# sobrar. Aviso (nao bloqueio) quando a soma dos apoios passar de 14/semana.
#
# (3) DESLIGAR OU SUSPENDER, CONFORME O CABO. Sem cabo disponivel pro WoL
# cabeado, `Movimente-se_Desligar.ps1` (fora deste repo,
# C:\Users\USUARIO\Documents\SCRIPTS\MOVIMENTE) passou a checar `Ethernet 2`
# antes do switch de energia: conectada desliga (S5, como sempre); ausente
# suspende (S3, tentando WoL pela Wi-Fi, sem garantia -- so' um teste). A
# parada limpa de sempre roda igual pros dois casos, sem duplicar nada.
# `PrepararWoL.ps1` passou a armar tambem a Wi-Fi. `app/ligarmaquina.py`
# agora manda o pacote magico para VARIOS MACs (nao so' um): sem saber qual
# placa estava ativa quando a maquina dormiu, manda pras duas cadastradas.
#
# QUASE QUEBROU NO CAMINHO, E FICA REGISTRADO: a sequencia de varios `.py`
# salvos em cadeia (reload em cima de reload) deixou o site fora do ar por
# alguns minutos -- `curl` local (com o Host certo, sem depender do
# Cloudflare) travava em 15s, sem resposta. NAO era loop no codigo: era o
# vassal recriando `create_app()` do zero repetidas vezes antes de um deles
# terminar de subir. Resolvido pela propria regra do projeto (recriar o
# vassal por SSH sozinho, `sudo touch` no `.ini`, sem pedir ao dono) --
# depois de um boot limpo, respondeu em 0,14s. Terceira coisa que quase deu
# errado: a suspensao pedida no fim desta sessao foi feita so' DEPOIS de
# rodar `Movimente-se_PararLimpo.ps1` (containers + WSL, a parada limpa de
# sempre) -- na primeira tentativa ele ABORTOU sozinho porque a copia da
# sessao do WhatsApp estava com 431 minutos (limite 10), exatamente o
# cenario que matou a sessao em 11/08. Forcar uma exportacao nova
# (`copia_sessao.py --exportar`) resolveu, e so' entao a parada seguiu.
#
# Ultimo toque: 07/09/2026 (2a passada) -- ATUALIZAR NOME E FOTO VIRA TRABALHO
# DA FILA, como upload e importacao. Pedido do dono, e melhor do que a trava
# que eu tinha posto uma hora antes.
# EU TINHA RESOLVIDO PELO SINTOMA: mandei a manutencao "ceder a vez" para a
# importacao. Funcionava, e criava uma regra de prioridade nova para alguem
# manter. O dono apontou o certo -- por na FILA. Com isso, de graca e sem
# regra nenhuma: uma requisicao por vez (a vaga ja e' UMA), o trabalho visivel
# na tela de upload com o que esta fazendo, ninguem disputando com ninguem, e
# o fluxo natural obedecido. A trava de prioridade saiu.
# DE QUEBRA, resolve o zumbi na raiz em vez de contorna-lo: o trabalho passa a
# rodar em processo proprio (`app.jobrunner`), que sobrevive ao reload do site.
# A thread antiga morria a cada `.py` salvo e deixava um registro "rodando"
# para sempre -- o corte por idade que eu tinha inventado deixa de ser preciso.
# DOIS DEFEITOS MEUS APANHADOS NO TESTE SECO, antes de gravar:
# (1) chamei `_trabalhos_em_andamento`, que nao existe (e' `_trabalhos_em_aberto`)
#     -- e como a chamada estava dentro de `try/except: return None`, ela nao
#     quebraria nada: desligaria a atualizacao automatica EM SILENCIO.
# (2) o ramo `StravaRateLimited` nao guardava a fila que sobrou, e a mensagem
#     nova prometia "voltam sozinhos". Prometer sem guardar e pior que nao
#     prometer -- agora guarda, igual ao ramo do teto.
# upload.py, admin.py, jobrunner.py.
#
# Toque anterior: 07/09/2026 21h10 -- MIGRACAO DE DOMINIO COMPLETADA NO CODIGO,
# a pedido do dono ("nao usar mais o dominio-antigo.exemplo.com"). Varredura por
# termo em todo `app/` (nao leitura integral): so' DOIS arquivos tinham o
# dominio velho escrito a mao -- `app/enderecos.py` (`PADRAO`, que agora e'
# o dominio NOVO; o velho fica so' como `ANTIGO`, referencia historica) e
# `config.py` (`SERVER_NAME` -- campo MORTO, `create_app()` nunca carrega
# essa classe, mas foi atualizado para nao mentir pra quem ler). O resto do
# codigo ja lia de `enderecos.base_url()`/`dominio()`, dinamico pelo Host do
# pedido -- nao precisou mexer. `app/__init__.py`: removida a variavel
# `DESTINO`/`dominio_destino` (a migracao chegou, nao faz mais sentido um
# "para onde estamos indo" declarado). NOVO em `app/indice.py`: o mesmo
# detector que marca tela "orfa" (sem caminho ate ela) agora TAMBEM marca
# `dominio_velho` -- template que ainda escreve `dominio-antigo.exemplo.com` a
# mao aparece em vermelho em `/admin/indice`, badge propria ("domínio
# antigo"), pra nao voltar a acontecer sem ninguem notar. Varredura NAO
# achou nenhum template com o dominio errado -- o P-102 (28/08) ja tinha
# sido cuidadoso.
# ⚠️ BLOQUEIO REAL, FORA DO CODIGO: o Synology Web Station
# (`VirtualHost.json`) so' aceita `fqdn: "dominio-antigo.exemplo.com"` pra esta
# aplicacao -- e' uma camada ANTES do Flask, dona do arquivo e' root, e eu
# nao tenho a senha de sudo nesta sessao (`Permission denied` confirmado por
# SSH). Sem essa troca, `movementes.engekon.com` continua caindo na pagina
# padrao do Web Station, mesmo com o Cloudflare Tunnel certo. Avisado ao
# dono para decidir: me passar a senha, ou trocar pela GUI do DSM
# (Painel de Controle > Web Station > aplicativos > editar hostname).
# app/enderecos.py, app/__init__.py, config.py, app/indice.py,
# app/blueprints/admin.py, app/templates/admin/indice.html.
#
# Toque anterior: 07/09/2026 21h -- A MINHA AUTOMACAO ESTAVA DISPUTANDO A FILA DO
# STRAVA COM A IMPORTACAO. Achado ao vivo, com o dono olhando a tela.
# O SINTOMA: a fila de importacao parecia parada as 20:47 ("comecando" e nada
# acontecendo). MEDIDO: nao estava parada -- o job 85289c1b corria normalmente
# (mes 10 de 65, depois 18, depois 28). O que faltava era VELOCIDADE, e ela
# estava sendo comida por DOIS trabalhos de manutencao que eu mesmo pus para
# rodar sozinhos na P-128:
#     maint_2f  running  Buscando o nome 3/96 no Strava...
#     maint_9d  running  Buscando a foto 6/55 no Strava...
# DOIS DEFEITOS MEUS:
# (1) `tocar_atualizacao_de_perfis` fazia `return` depois do primeiro tipo e eu
#     chamei isso de "um por passada". E' verdade DENTRO de uma passada -- mas o
#     vigia passa de 15 em 15 min, e na passada seguinte ele disparava o OUTRO
#     tipo com o primeiro ainda rodando.
# (2) Nunca perguntei se havia IMPORTACAO em andamento. Manutencao pode esperar
#     dias; importacao e' o dono esperando na tela. Agora ela cede a vez.
# E A FOLGA DE 35% DE COTA NAO PROTEGIA DISSO: ela olha o TETO, e o teto estava
# folgado (188 de 2.000). O gargalo nao era o teto -- era o ESPACAMENTO entre
# requisicoes, que e' compartilhado (SharedPacer). Tres trabalhos na mesma fila
# andam a um terco da velocidade cada. Teto e espacamento sao coisas diferentes,
# e eu tinha protegido so' uma delas.
# TERCEIRA TRAVA, contra um risco que a segunda criou: trabalho de manutencao
# roda em THREAD do site, e o site reinicia a cada `.py` salvo -- a thread morre
# e o registro fica "running" para sempre. Sem corte por idade (10 min), um
# unico reload no meio de uma passada travaria a atualizacao automatica em
# definitivo, e o sintoma seria nada acontecer. Conferido: os dois maint acima
# estavam parados ha 23,6 min, mortos num reload.
# CONFERIDO TAMBEM, a pedido do dono: o fluxo mes a mes ESTA correto --
# `_gravar_mes_pela_listagem` e "um mes do perfil, do /interval direto para o
# banco, UMA requisicao", e grava com `dados_prontos` (zero requisicao extra)
# antes de passar ao proximo mes. Nao ha mes lido e deixado para tras.
# admin.py.
#
# Toque anterior: 07/09/2026 -- LOG PASSA A SE DIVIDIR POR MES (regra 19).
#
# O log so se dividia por TAMANHO (2 MB, 10 copias). Isso resolvia o arquivo
# gigante -- o `movimente_se.log.6` chegou a 11 MB --, mas nao respondia a
# pergunta que a gente faz quando algo quebra: "o que aconteceu no dia 30?".
# Com onze arquivos rotativos, o dia 30 podia estar em qualquer um, e achar
# exigia abrir todos: o oposto da regra 15.
#
# Agora a resposta e' o NOME do arquivo: `movimente_se-2026-09.log`. O teto de
# 2 MB continua valendo DENTRO do mes, entao mes movimentado nao gera arquivo
# pesado. Virar o mes NAO rotaciona nada -- o handler so troca de destino, e o
# arquivo do mes passado fica intacto (rotacionar ali criaria um
# `-2026-08.log.1` no dia 1o de setembro, mentindo sobre quando foi escrito).
#
# A politica do dono: 60 dias completos, depois um arquivo por mes. Quem aplica
# e' `logmensal.expurgar`, pendurado na faxina que ja rodava -- e ele so apaga
# as SOBRAS de tamanho vencidas, nunca o arquivo do mes.
#
# O `movimente_se.log` antigo NAO foi apagado nem migrado: para de crescer e
# fica como historico. Apagar log de producao para "organizar" nao vale o risco.
#
# Arquivos: app/logmensal.py (novo), app/__init__.py, app/blueprints/upload.py.
#
# Toque anterior: 07/09/2026 -- P-128: a atualizacao de perfis termina sozinha.
# O botao parou em 55 de 77 porque bateu o teto do quarto de hora. Ele avisava
# direito, mas o trabalho ficava pela metade e o clique seguinte parava no mesmo
# lugar. Botao que nunca termina o servico nao e' botao, e' pedido de desculpas.
# O PEDIDO, na frase do dono: "deve obedecer e acompanhar todo o fluxo de
# requisicoes ao Strava, porem, tudo deve funcionar automatico sem clique para a
# frequencia definida e mesmo assim seguir o fluxo de requisicoes permitidas".
# Entao NAO se pede uma requisicao a mais por minuto e NAO se fura teto nenhum.
# O que muda: o trabalho GUARDA de onde parou (`app/perfilauto.py`, novo) e o
# vigia -- que ja passa de 15 em 15 min, sem temporizador novo -- CONTINUA
# sozinho quando a janela virar. Passado o prazo, ele RECOMECA sozinho.
# TRES TRAVAS para isto nunca atrapalhar o que importa: so anda com 35% de folga
# em TODAS as janelas de cota; para na hora se a trava de bloqueio estiver ativa
# (`stravasinal`); e processa no maximo 12 atletas por passada -- o vigia volta
# em 15 minutos. Atualizar foto nunca pode ser o motivo de o ranking nao sair.
# FREQUENCIA: foto a cada 30 dias (a mesma janela que o job ja usava para pular
# quem esta em dia), nome a cada 7 -- o nome muda quando a pessoa quer, e e' ele
# que aparece no ranking. O prazo conta desde a ULTIMA CONCLUSAO, nao desde o
# ultimo comeco: assim uma passada longa, que atravessou varios tetos, nao
# dispara outra logo atras.
# O botao continua existindo para quem quer agora, e agora ele tambem termina --
# se o teto interromper, o vigia leva ate o fim.
# perfilauto.py (novo), admin.py, __init__.py.
#
# Ultimo toque: 07/09/2026 -- P-130, P-131 e P-132 fechadas.
#
# P-130 "soma X km mas a leitura nao devolveu atividade" -- 18 casos num dia.
# MEDIDO lado a lado, mesma rota `/interval`, mesmo mes:
#     perfil <id-5> ..  29,9 km   lista VAZIA     3.837 bytes
#     perfil  <id-6> .. 471,6 km   81 atividades 410.660 bytes
#     perfil <id-7> .. 107,6 km   16 atividades 686.164 bytes
# A resposta do primeiro e' CEM VEZES menor: traz os totais (que sao publicos) e
# `"preFetchedEntries":[]`. Nao e' falha nossa -- e' um perfil cujas ATIVIDADES
# sao restritas (privadas ou so' para seguidores). O Strava publica o agregado e
# esconde a lista.
# O ESTRAGO ERA O TRATAMENTO: isso virava `StravaListaVazia`, que quer dizer
# "nao consegui ler, repito depois". Entao o job descia mes a mes, gastava uma
# requisicao em cada, repetia na passada seguinte e terminava com "Nenhuma
# atividade encontrada" -- sem ninguem saber o motivo real. Agora existe
# `StravaPerfilRestrito`, PERMANENTE: o laco para no primeiro mes e o admin
# recebe o nome do atleta e o que fazer (pedir para abrir o perfil, ou aceitar
# a conta do projeto como seguidora). O quilometro existe; so' nao e' visivel.
# E UMA CORRECAO MINHA, no mesmo patch: eu tinha escrito no codigo que a
# resposta vinha DUPLAMENTE escapada e que era isso que quebrava o regex. Testei
# contra a resposta real e NAO E': uma passada basta, e o que eu vi eram as
# barras do `repr()` do Python. O comentario foi reescrito para dizer o que foi
# medido -- comentario que afirma o que ninguem mediu ja me traiu duas vezes
# esta semana (4.37 e 4.39).
#
# P-131 -- as 5 flags de anticheat orfas (`atleta_id=118`, inexistente). A flag
# guarda o atleta, mas o DONO DE VERDADE esta na atividade: religadas pelo
# `activities.id_usuario_strava` -- 1 para um atleta, 4 para Maykon.
# Zero orfas restantes. Backup conferido antes (config_20260907_120156.db).
#
# P-132 -- atleta com 0,0 km sumindo do ranking. O balde do atleta e' criado
# ANTES do teste de rejeicao, entao quem teve todas as atividades removidas
# sobrava no fim da lista com "0.0km", parecendo que treinou zero -- quando na
# verdade nao pontuou. O total do projeto nao muda: `total_km` soma o dicionario
# inteiro antes deste corte, e zero nao altera soma.
# processing.py, upload.py, ranking.py.
#
# Ultimo toque: 07/09/2026 -- O PRINCIPAL PASSA A REVEZAR NO RODAPE, E DOIS
# DEFEITOS QUE SO APARECERAM COM DADO NOVO.
# (1) AJUSTE PEDIDO: "o principal deixa marcado todos os dias e vai revezando
# com esses dois". Ele continua no CABECALHO em toda rodada -- isso e' o lugar
# de honra que ele comprou, e o formato consolidado nao se mexe (11-A). No
# RODAPE ele entrou na mesma fila dos apoios, com o peso dele (5): quem tem o
# cabecalho garantido pode dividir o rodape. Antes saia nos dois lugares sempre,
# e o apoio da vez virava acompanhante.
# (2) DEFEITO ACHADO AO CADASTRAR: `_nomes_patrocinadores` juntava TODOS os
# patrocinadores do periodo com ' | ', e esse texto alimenta a linha
# "Patrocinador do Mes: *X*". Com um patrocinador so' no mes -- que foi o caso
# ate hoje -- ninguem via. Assim que entraram a nutricionista e a ONTURBO, o
# cabecalho virou "Patrocinador A | Patrocinador B | ONTURBO TELECOM".
# Patrocinador do Mes e' UM. Agora devolve o principal, com queda para o
# primeiro do periodo em instalacao antiga.
# (3) DEFEITO IRMAO, no template: `{{ sp.nome_exibicao if sp else '' }}`
# renderiza o texto "None" quando o campo esta vazio no banco -- e o admin salva
# a palavra "None" como nome do patrocinador sem perceber. Aconteceu com a
# ONTURBO agora ha pouco. `or ''` resolve.
# OS DOIS SO APARECERAM PORQUE O DADO MUDOU. "Funciona hoje" nao e' o mesmo que
# "esta certo": os dois estavam errados desde sempre, esperando um segundo
# patrocinador para se mostrar.
# CADASTRADO EM 9/2026 (a pedido): Patrocinador A (principal, peso 5),
# ONTURBO TELECOM (apoio, peso 3, banca trofeus e parte das medalhas),
# Patrocinador B (apoio, peso 1). Fila de 9 vagas -> medido em 14 dias:
# 8,0 / 4,5 / 1,5 aparicoes por semana.
# ranking.py, patrocinadores.html.
#
# Ultimo toque: 06/09/2026 -- UM APOIO POR RANKING, COM FREQUENCIA REGULAVEL.
# Ate aqui o bloco "Nossos patrocinadores" saia com TODOS os apoios juntos, em
# toda rodada. Com o cadastro crescendo isso vira lista que ninguem le -- e
# tira do apoiador exatamente o que ele quer, que e' ser notado. O dono pediu
# um por vez, revezando, e com frequencia diferente para cada: "a depender do
# tipo de patrocinio que for oferecido". O caso concreto: uma bioimpedancia
# simbolica, oferecida para atrair cliente -- merece aparecer, nao merece
# aparecer toda rodada.
# O PATROCINADOR DO MES nao muda: continua fixo no cabecalho (regra 11-A, nao
# se mexe no formato consolidado). O rodizio e' so' entre os APOIOS, no bloco
# do fim.
# POR QUE SEM CONTADOR -- e este era o pedido literal, "que nao entre em
# conflito durante o envio". Guardar "quem apareceu por ultimo" quebra na
# primeira esquina: `/api/ranking/latest` e' consultado pela automacao, pela
# tela e por toda nova tentativa depois de falha, e cada consulta gastaria uma
# vaga sem nada ter sido enviado -- a tela mostraria um patrocinador e o grupo
# receberia outro. Entao o da vez SE CALCULA:
#     rodada = (dias desde 01/01/2026) * 2 + (0 de manha, 1 de tarde)
#     fila   = cada apoio repetido tantas vezes quanto o seu peso
#     da vez = fila[rodada % len(fila)]
# Consultar nao muda nada; dez chamadas dao o mesmo; a tela mostra o que vai
# sair; e nao ha estado para dessincronizar.
# `peso_frequencia` (0 a 5) nas DUAS tabelas -- na ficha do catalogo e' o
# padrao copiado para o mes novo; no mensal e' o que vale naquele mes. Coluna e
# modelo no MESMO gravar, como manda a convencao.
# MEDIDO no teste com 3 apoios (5/3/1) em 14 dias: 8, 4,5 e 1,5 aparicoes por
# semana. Peso 0 sai do rodizio sem apagar a ficha.
# BUG PEGO NO TESTE: `int(peso or 3)` transformava peso 0 em 3 -- zero e'
# falsy, e "nao aparecer" viraria "frequencia normal", o oposto do pedido.
# models.py, ranking.py, patrocinadores.html.
#
# Ultimo toque: 06/09/2026 -- OS LINKS DA MENSAGEM DO RANKING (relatado pelo dono).
# (1) O "Painel dos atletas" saia DUAS VEZES no mesmo bloco: era inserido antes
# da secao de corrida e DE NOVO depois dela, em `format_ranking_partes`. Ficou
# a primeira -- que e' onde ele serve: quem abre a mensagem ve o link antes da
# lista, nao depois de rolar tudo. A caixa de texto do site sempre mostrou uma
# vez so, e a mensagem do grupo tem de ser a mesma coisa.
# (2) O link da CONFERENCIA nunca aparecia. A pagina existe desde sempre
# (`/conferencia/<cidade>`) e nao era divulgada em lugar nenhum -- justo a que
# responde sozinha a pergunta mais repetida do grupo, "meu treino entrou?".
# Entrou no rodape, entre feedback e transparencia, que e' onde o leitor ja
# procura link de servico: "🔎 Seu treino nao entrou? confira aqui:".
# Conferido na tela: painel 1x no inicio, conferencia no rodape, pagina abre.
# ranking.py (`_conferencia_public_url`, `_bloco_rodape_links`).
#
# Ultimo toque: 06/09/2026 -- O NOME DO ATLETA VOLTA, COM A ANCORA MEDIDA, E
# AGORA SAO DOIS BOTOES.
# A SONDA (4 perfis reais) MOSTROU POR QUE O ERRO ACONTECEU: na pagina de
# perfil de TERCEIRO nao existe campo estruturado com o nome do dono --
#   "first_name" no HTML inteiro .. 1 ocorrencia, e e' a CONTA DO COOKIE
#   Strava.Models.* .............. so' `currentAthlete`, a conta do cookie
#   classe CSS athlete-name ...... "Maykon Abreu" (a barra de navegacao)
#   <title> ...................... "Bia Araujo Araujo Modelo"  <- o unico
# Ou seja, nao foi so' regex mal escrito: nao havia o que ancorar. O `<title>`
# e' a unica fonte, e veio certo nos cinco perfis testados.
# COMO A REGRA 4-V SE CUMPRE AQUI: o `<title>` nao carrega o id, entao a
# amarracao se faz pelo outro lado -- o id vai na REQUISICAO e a resposta e'
# validada contra ele. TRES TRAVAS: (1) a URL final tem de continuar sendo
# `/athletes/<id>`, senao um redirecionamento (perfil apagado, privado, login)
# traria o titulo de outra pagina; (2) nome igual ao da conta do cookie, com id
# diferente, e' RECUSADO -- e' a assinatura exata do estrago de 06/09; (3) o
# `_MAX_MESMO_NOME=3` no gravador, que nao sabe o que procurar e por isso pega
# o que ninguem previu. Testadas contra HTML real, inclusive o cenario do erro.
# DOIS BOTOES, porque sao trabalhos de custo e risco diferentes: a FOTO e'
# ancorada no id (`/pictures/athletes/<id>/`), nunca errou, e so precisa rodar
# a cada 30 dias; o NOME e' escrita em cima de dado bom e a pessoa troca quando
# quer -- nao ha janela que faca sentido. Juntos, um obrigava o outro: para
# corrigir um nome era preciso gastar requisicao com foto.
# A janela de 30 dias passou a valer SO no modo foto. O nome escolhido para o
# ranking (`nome_exibicao`) continua intocado pelos dois.
# processing.py (fetch_athlete_nome), admin.py, lista_atletas.html. ERROS.md 4.39.
#
# Ultimo toque: 06/09/2026 16h00 -- EU ESCREVI O NOME DA CONTA DO COOKIE EM 62
# ATLETAS. Conserto, restauracao e blindagem.
# O QUE EU FIZ DE ERRADO: em `fetch_athlete_avatar_url(com_nome=True)` colhi o
# nome com `re.search(r'"first_name"...', html)`. Na pagina de perfil de
# QUALQUER atleta o Strava tambem monta o objeto da conta do cookie
# (`currentAthlete`), e ele vem ANTES -- o regex pegava sempre o dono do cookie.
# O botao "Atualizar nome e foto" gravou "Maykon Abreu" em 62 de 101 atletas.
# E E' EXATAMENTE O MESMO ERRO da regra 4-D que eu tinha acabado de consertar na
# pagina da ATIVIDADE, horas antes (ERROS.md 4.37). Pior: o comentario que eu
# mesmo escrevi ali dizia "aqui a pagina E do atleta pedido, entao nao ha risco
# de confundir com a conta logada". Comentario que AFIRMA que a guarda nao e'
# necessaria e' o mesmo veneno de 27/08 -- faz a proxima leitura parar de olhar.
# O QUE MUDOU:
# (1) A COLHEITA DE NOME SAIU. Nao "melhorei o regex" no susto: nome errado
#     gravado em cima de nome certo e' perda de dado. O botao volta a ser so
#     foto -- que sempre teve o id do atleta DENTRO da ancora
#     (`/pictures/athletes/<id>/`) e por isso nunca errou. O nome so volta com
#     ancora igualmente amarrada ao id, provada em pagina real.
# (2) BLINDAGEM GENERICA: `_MAX_MESMO_NOME = 3`. Se o mesmo nome chegar para
#     tres atletas diferentes na mesma passada, a atualizacao ABORTA e desfaz o
#     que ja gravou -- duas pessoas com nome igual acontece (o banco tem tres
#     "Fulana Exemplo"), tres vindas da MESMA leitura nao. Ela nao sabe nada
#     sobre regex ou cookie: pega o proximo erro que eu ainda nao imaginei.
# RESTAURACAO: 62 nomes voltaram do backup `config_20260829_121931.db`, casados
# por `strava_id`. Conferido: 0 restaurados indevidamente (so mexeu em quem
# estava com "Maykon"), e sobrou 1 -- o Maykon de verdade.
# O RANKING NAO FOI AFETADO: ele usa `activities.nome_usuario`, e o job so
# escreve em `atletas.nome_atual`. As 292 atividades com "Maykon" sao todas do
# <id-2>. Os 3 `nome_exibicao` que o dono definiu (Bia Modelo, Dani Exemplo,
# Carlos Exemplo) ficaram intactos, como projetado.
# AS FLAGS DE ANTICHEAT NAO SE MEXERAM: elas guardam `atleta_id`, nao nome. As 8
# do Maykon apontam para atividades que sao mesmo dele; nenhuma flag nova desde
# 05/09. O nome errado era so' o rotulo, e voltou com a restauracao.
# processing.py, admin.py. ERROS.md.
#
# Ultimo toque: 29/08/2026 12h20 -- FASE 1 do plano da raspagem confiavel.
# O RANKING DAS 05:30 PUBLICOU 266,5 KM DE CORRIDA PARA UM ATLETA FANTASMA.
# Causa medida na pagina real: a pagina classica do Strava passou a servir
# TRES blocos `pageView.activity().set({...})`, e o do meio e' o de
# EQUIPAMENTO -- um `distance` por tenis, com a quilometragem ACUMULADA do
# calcado. `_bloco_da_atividade` pegava "o primeiro bloco que contenha a
# substring distance" e caiu no tenis: 266.509 (Adidas NMD R1) no lugar de
# 16.471 (a corrida). Como esse bloco tambem nao tem `activityAthleteId`, a
# atividade ficou sem dono, virou id '0', e o ranking -- que agrupa por NOME
# quando o id nao presta -- criou um SEGUNDO "Maykon" sem codigo.
# O que mudou: (1) `_bloco_da_atividade` escolhe por ASSINATURA (distance +
# moving_time/activityAthleteId, e recusa bloco com shoes/bikes), nao por
# posicao; (2) `_num` ganhou ancora de INICIO de chave -- antes casava
# qualquer chave TERMINADA em `distance`, a mesma guarda que
# `_extract_from_regex` ganhou em 27/08 e que faltou aqui; (3) SEM DONO NAO
# SE GRAVA: o `setdefault('id_usuario_strava','0')` saiu e virou recusa com
# motivo `sem_dono`; (4) `app/nomes.py` novo -- a regra de "isto nao e nome"
# (emoji, tipo sanguineo) so rodava ao EXIBIR, e quem gravava pegava o ultimo
# pedaco bruto: "Maykon Abreu A+" ia para o banco como "Maykon A+", perdendo
# o sobrenome; (5) ranking desambigua homonimo (duas "Maria Silva" viram
# "Maria Silva" e "Maria Oliveira"); (6) a conferencia parou de descartar em
# silencio -- leitura sem dono era contada como "treino em grupo" e sumiu com
# 16,471 km reais em 28/08 sem deixar uma linha de log (regra 4-I).
# Sonda antes/depois no HTML real: 266.509 km -> 16.471 km, dono <id-2>.
# processing.py, ranking.py, conferenciaauto.py, nomes.py (novo). ERROS.md.
#
# Ultimo toque: 29/08/2026 12h40 -- FASE 1 (2a passada): O DONO E O NOME SAEM
# DO OBJETO ESTRUTURADO, e nao mais catados de pedacos soltos da pagina.
# Medido nas paginas reais: a pagina classica monta DOIS objetos de atleta --
#   activityAthlete = new Strava.Models.Athlete({...})        <- o DONO
#   currentAthlete  = new Strava.Models.CurrentAthlete({...}) <- a conta do cookie
# Ler o primeiro resolve id E nome NA MESMA FONTE, que e o principio que
# faltava (metrica e identidade vem juntas). Duas coisas cairam com isso:
# (1) BUG LATENTE DO NOME, achado durante o conserto: o codigo procurava o
#     nome com `athletes/<id>[^>]*>(...)`, e o PRIMEIRO link com esse id e um
#     item de MENU -- atividade lida so pela pagina logada ia para o banco com
#     nome "Treinamento". So nao apareceu antes porque a leitura anonima
#     costuma chegar com o nome primeiro e `_juntar_leituras` nao sobrescreve.
# (2) `_dono_pela_pagina` como 2a via quando o bloco nao traz
#     `activityAthleteId` (medido na <id-da-atividade>). Sem isso a guarda nova
#     ("sem dono nao grava") recusaria atividade BOA.
# `app/nomes.py` ganhou `limpar_bordas`: o Strava devolve
# last_name="Abreu[emoji] [bandeira] | A+", e sem isso o ranking mostrava
# "Abreu[emoji]". Agora sai "Maykon Abreu".
# PROVA EM ESCALA (9 atletas diferentes, paginas reais): leitor novo acertou
# 9/9 em km, id do atleta e nome. E a medida que importa: esses 9 tem DOIS
# blocos na pagina; quem cadastra tenis/bicicleta no Strava passa a ter TRES,
# e e so ai que o bug dispara -- ou seja, qualquer um dos 101 atletas estava a
# um clique de ter km inventado. Por isso o conserto e no motor de leitura, nao
# no dado. processing.py, nomes.py. ERROS.md.
#
# Ultimo toque: 29/08/2026 14h30 -- FASE 3: LER O QUE O STRAVA RESPONDEU ANTES
# DE INSISTIR, e teto do dia de 1.000 -> 2.000.
# O NUMERO ESTAVA ERRADO PELO MOTIVO ERRADO: 100/15min e 1.000/dia sao os
# limites publicados da API OFICIAL do Strava, e este projeto NAO usa a API --
# le pagina com cookie de sessao. Aplicavamos ao scraping um teto de outra
# coisa, e ainda por baixo (o proprio tier Standard da API permite 200/15min e
# 2.000/dia). Medido em 29/08: das 1.008 requisicoes do dia, as janelas curtas
# ficaram ociosas (8 nos ultimos 15 min, teto 100) -- quem travava era so o dia.
# `app/stravasinal.py` (novo) da NOME e REMEDIO a cada resposta, em vez da
# pergunta unica "e bloqueio?" com o remedio unico "espera e repete".
# O discriminante que muda tudo: 429 COM `Retry-After` e limite honesto (obedece
# o prazo, sem risco); 429 SEM o cabecalho e deteccao de bot, e ai repetir
# PIORA. Tambem separa 403 privado de 403 CloudFlare, 200 com casca de login,
# 5xx instavel, e "nao sei" -- que por precaucao conta como bloqueio (21-G).
# A TRAVA ESCALONA em vez de dormir 24h de cara (desenho do dono): 1o bloqueio
# = 3 h e volta devagar; 2o = ate meia-noite UTC, que e quando o contador
# diario do Strava vira. Depois de 24 h limpo o contador esquece. Estado em
# disco, porque quem precisa dele sao processos diferentes.
# `_buscar_intervalo` passou a separar o HTTP 400: nao e falha, e "a conta nao
# existia nesse mes" -- fim do historico. O laco PARA de descer ali. Media
# 28/08: 57 requisicoes desperdicadas por atleta novo, 156 num dia so.
# Tetos agora sao regulaveis e MUDAM NA HORA: `_maybe_resume_job` pergunta ao
# pacer se ja cabe, em vez de olhar um `pausa_ate` calculado com o teto velho --
# senao subir o teto so valeria no dia seguinte.
# stravasinal.py (novo), ratepacer.py, processing.py, upload.py. ERROS.md.
#
# Ultimo toque: 29/08/2026 14h35 -- PAINEL DE COTA NA TELA + A FILA DEIXA DE
# SER ANONIMA.
# A coluna "Atleta" vinha vazia em TODOS os estados desde sempre, e o log saia
# como "Entrou na fila: " (25 ocorrencias em 28-29/08). Causa: o
# `params['athlete_id']` nunca era gravado neste fluxo -- a unica escrita do
# campo estava em `_listar_perfil_com_checkpoint`, funcao sem nenhum chamador.
# Duas correcoes, de proposito: agora o id e' GRAVADO no clique (esta no proprio
# link), e `_id_do_trabalho` ainda o DERIVA do `source_url` quando falta --
# assim a tela conserta tambem os trabalhos criados antes desta mudanca, em vez
# de so valer para os proximos.
# PAINEL "Leitura do Strava hoje" (`/upload/atleta`): quanto ja foi gasto em
# cada janela, quanto ainda cabe, e os tres tetos editaveis ali mesmo
# (`POST /upload/cota`). Ficar em variavel de ambiente do uWSGI nao servia: o
# numero certo muda toda semana e uma atualizacao do DSM apaga o `emperor.ini`
# (COMANDOS.md secao 9). Quem muda fica no log, com nome -- e' decisao de risco.
# MEDIDO AO VIVO: com o teto em 2.000 e 1.008 gastos, o job 89b4483f (Bia
# Exemplo), que estava congelado ate as 21:00, voltou sozinho as 14:25 -- que e' o
# que a reavaliacao do teto na retomada prometia.
# upload.py, importar_atleta.html. ERROS.md.
#
# Ultimo toque: 29/08/2026 14h45 -- FASE 4: QUEM ESPERA NA FILA PARA DE FICAR
# PARADO ("descascar o abacaxi", desenho do dono).
# EU TINHA DITO QUE NAO DAVA, E ESTAVA ERRADO. Medindo o fluxo: a importacao
# tem DUAS fases, e so a primeira custa Strava.
#   A) DESCOBERTA -- 1 requisicao `/interval` por mes, que ja devolve TODAS as
#      atividades daquele mes, com km, tipo, dono e data.
#   B) GRAVACAO   -- banco, anticheat, consolidacao. ZERO requisicao.
# A prova de que a B e' de graca: o job de um atleta gravou 1.414 atividades em 12
# minutos -- impossivel com uma requisicao por atividade; foram ~70, uma por mes.
# Entao o trabalho que espera adianta a fase A e guarda em
# `temp_extract/descasque/<job>.json` (`app/descasque.py`, novo). Na vez dele,
# sobra so a fase B: grava em segundos e DEVOLVE A VAGA -- que era o que
# realmente travava a fila.
# NAO GASTA REQUISICAO A MAIS: o total e' identico, muda so QUANDO. Por isso o
# adiantamento (1) usa o mesmo SharedPacer, (2) so roda com 30% de folga em
# TODAS as janelas, (3) para na hora se a trava de bloqueio estiver ativa, e
# (4) faz no maximo 4 meses por passada, um trabalho por vez -- o ticker volta
# em 2 minutos. Adiantar nao pode roubar a vez de quem esta rodando.
# ORDEM: de frente para tras na fila (o proximo a subir descasca primeiro), e
# nao de tras para frente -- assim o mais provavel de ser chamado e o que ja
# esta pronto. Quem sobe com descasque parcial continua do jeito antigo: estar
# pela metade nunca atrapalha.
# Descasque vence em 12 h (o atleta pode ter apagado ou corrigido atividade) e
# some junto com o trabalho, na faxina diaria.
# descasque.py (novo), upload.py. ERROS.md.
#
# Ultimo toque: 29/08/2026 15h05 -- FASE 5: O NOME QUE A PESSOA QUER VER NO
# RANKING (regra do dono).
# O PROBLEMA NAO ERA A FALTA DO CAMPO, ERA O SNAPSHOT: `nome_atual` e uma copia
# do Strava, e `anticheat._get_ou_criar_atleta` a reescreve a cada atividade
# nova. Qualquer correcao de nome durava ate o proximo link postado no grupo --
# era isso que o dono descrevia como "eu arrumo tudo e depois clico no botao e
# perco o que fiz".
# Agora ha `Atleta.nome_exibicao` (+ `_por` e `_em`, para saber quem escolheu):
# so muda quando GENTE muda. Ana Exemplo e conhecida como Ana Modelo; Bia
# Exemplo, como Bia Modelo; o carlos escreveu o proprio nome em minuscula. Nada
# disso e erro do Strava -- e so nao e como a cidade chama a pessoa.
# COLUNA E MODELO NO MESMO GRAVAR (convencao do projeto): entre um lote e outro
# o modelo pediria uma coluna que a tabela nao tem e TODA consulta a `atletas`
# quebraria.
# O ranking usa o nome escolhido quando existe, e a desambiguacao automatica de
# homonimo NAO mexe nele -- se duas pessoas escolheram o mesmo nome, quem
# resolve e o admin, nao um palpite do codigo.
# "Atualizar fotos" virou "Atualizar nome e foto": o nome vem na MESMA resposta
# (a pagina do perfil que carrega o avatar ja tem o nome), entao nao custa
# requisicao nenhuma -- e o `nome_exibicao` continua intocado, que era a queixa.
# LISTA DE ATLETAS: arquivado sai no SQL, nao so no CSS. A tela escondia com
# `d-none`, mas o registro vinha do banco em toda visita e ainda contava no
# total: o dono arquivava e continuava vendo a mesma lista, do mesmo tamanho.
# Conferido na tela: 101 -> 97. O checkbox "Mostrar arquivados" agora RECARREGA
# pedindo (esconder por CSS so servia enquanto eles vinham).
# E o botao Arquivar deixou de ser escondido para `strava_id == '0'` -- o unico
# caso em que ele era realmente necessario, e o motivo de "cliquei e nao saiu".
# models.py, ranking.py, anticheat.py, admin.py, processing.py, 2 templates.
#
# Ultimo toque: 29/08/2026 15h20 -- FASE 6: A CONFERENCIA SAI DA HORA ALEATORIA.
# CAUSA DO E-MAIL DAS 22:41 DE 28/08, medida: `rodar_agendadas()` era chamada de
# dentro de um `time.sleep(6 * 3600)` -- seis horas contadas A PARTIR DO BOOT
# DO SITE. Como salvar qualquer `.py` reinicia a aplicacao (py-autoreload), o
# horario andava a cada reinicio. Nao havia hora marcada: as 22:41 foram acaso.
# Agora a conferencia se ancora na PUBLICACAO, nao no boot -- e o horario sai da
# `PostagemAgenda`, que ja e' onde cada cidade define quando publica, em vez de
# uma constante nova. Janela: de 25 a 5 minutos antes de publicar (as
# conferencias reais de 28-29/08 levaram 4 e 5 minutos, entao cabe com folga, e
# a regra 16-C dos 20 s continua valendo).
# SAIU DA ROTINA DE 6 H E FOI PARA O VIGIA (15 em 15 min): so um relogio
# frequente consegue acertar a janela. Nao ha temporizador novo -- o do vigia ja
# existia, e criar um terceiro seria gastar a maquina duas vezes pelo mesmo
# trabalho.
# DOIS PORTOES: `vencida` (ja passou tempo em DIAS?) e `na_hora_certa` (agora e'
# a hora boa do dia?). "Conferir na proxima rodada" atravessa os dois: quem
# clicou quer agora.
# DISCORDEI DO DONO E REGISTRO: ele propos rodar DEPOIS do ranking e editar a
# mensagem. Ficou ANTES, porque assim o ranking ja sai certo e nao depende de a
# edicao de mensagem do WAHA funcionar -- um "se" a menos no caminho. Se a
# edicao se provar confiavel, mudar e trocar duas constantes.
# E O ALERTA GANHOU O NOME DO QUE ACONTECEU: chegava como "Falha na automacao --
# Conferencia recuperou link", e o dono leu como alarme falso, com razao: a
# conferencia tinha FUNCIONADO. O que falhou foi o WhatsApp, que perdeu 6 links.
# Agora e' "WhatsApp perdeu link (a conferencia trouxe)", com acao cadastrada no
# `alertas._ACOES` na MESMA sessao (regra 21-J) -- etapa sem acao vira e-mail
# generico, e generico e o mesmo que vazio.
# conferenciaauto.py, __init__.py, alertas.py. ERROS.md.
#
# Ultimo toque: 29/08/2026 16h00 -- EU ESTAVA ERRADO SOBRE O ALARME DA
# CONFERENCIA, E O DONO ESTAVA CERTO. Tres causas, nao uma.
#
# (1) A MARCA DE "CONFERIR NA PROXIMA RODADA" NUNCA ERA APAGADA -- causa raiz.
# `forcar_proxima(ligar=False)` desligava com `set_value(CONFERENCIA_FORCAR,
# '')`, e `set_value` comeca com `if not value: return False` (guarda que
# impede zerar segredo por engano). A marca ficava presa: conferido no cofre,
# `conferencia_forcar -> {'Palmeirópolis': '1'}`. Uma vez clicado, `_forcada`
# devolvia True PARA SEMPRE e a conferencia atravessava o prazo em toda
# chamada -- inclusive as 22:41. Agora desliga com `secretstore.delete`.
# Foi isso, e nao o `sleep` de 6 h, que explicou melhor o horario de ontem.
#
# (2) A CONFERENCIA NAO DISTINGUIA "PERDIDO" DE "AINDA NAO LIDO". Medido: em
# 28/08 o ultimo ZIP entrou as 15:30:37; os atletas postaram entre 18:30 e
# 19:51; as 22:41 ela leu o STRAVA (nunca le o WAHA), viu 6 atividades fora do
# banco e chamou de falha do WhatsApp. Cinco eram de 17:07-18:45 -- estavam a
# caminho, entrariam as 05:30. Agora `ate_quando_o_grupo_foi_lido()` da o
# marco (ultimo `automacao_*.zip` processado, com 30 min de folga): so conta
# como perda o que e ANTERIOR a ele. Continua trazendo tudo -- adiantar km nao
# machuca ninguem --, mas so AVISA sobre perda de verdade.
#
# (3) O HORARIO: revertido para DEPOIS da publicacao (40-60 min), como o dono
# pediu desde o inicio. Meu argumento para "antes" caiu na medicao: a leitura
# do grupo chega JUNTO com a publicacao, entao 25 min antes e' olhar o banco
# no momento mais atrasado dele. Com ranking as 05:30, confere 06:10-06:30.
#
# E O E-MAIL PAROU DE AFIRMAR A CAUSA. Ele mandava reparear o WhatsApp; o caso
# das 15:03 de hoje era a atividade <id-da-atividade> (fragmento de 44,88 m) que o
# NOSSO bug do dono='0' descartava. Reparear por defeito nosso e' pior que nao
# avisar: gasta o tempo do dono e arrisca a sessao (15-D). Agora separa o que
# sabe (esta no Strava, ja passou leitura, nao estava no ranking) das tres
# causas possiveis, e manda conferir o grupo ANTES de mexer na sessao.
#
# Junto: job de fotos trata `StravaLimiteDeVolume` (travava em 55/77 e morria
# com "Erro:", sem dizer que bastava esperar) e os tetos ficaram coerentes
# (200/15min com 2.000/dia, como na API oficial -- 100 com 2.000 era funil).
# Terminar o trabalho num clique so virou P-128, a pedido do dono.
# conferenciaauto.py, upload.py, admin.py, ratepacer.py, lista_atletas.html.
#
# Ultimo toque: 28/08/2026 21h15 -- P-122 FECHADA: filtro de periodo nas
# tabelas de "Quilometros por pessoa" da home. Botoes Desde o comeco/Este
# ano/Este semestre/Este trimestre/Este mes (MESMO vocabulario e marco fixo
# do `consolidado.intervalo_do_periodo`, corrigido mais cedo nesta sessao --
# nao inventa uma janela rolante diferente). `periodo_pessoas='tudo'` usa a
# foto em cache (0 consulta nova); qualquer outro periodo chama
# `classificacoes_da_capa(periodo=X)` NA HORA, so' para esta secao -- o
# resto da pagina (totais, historia, patrocinadores) continua vindo do
# cache diario, sem custo extra.
# app/__init__.py, app/consolidado.py, app/templates/publico/home.html.
#
# Ultimo toque: 28/08/2026 21h00 -- P-121 FECHADA: "Onde o projeto acontece"
# subiu para ANTES de "Quilometros por pessoa", cada cidade virou BOTAO
# (link), e entrou "Todas" como primeiro item (o estado sem filtro de
# hoje). Sem AJAX/refazer tabela na hora -- a foto do dia e' cache de 1x/dia
# (NAS de 490 MB), entao cada cidade leva para a pagina que JA existe e ja
# funciona por cidade (`ranking.consolidado_publico`), em vez de multiplicar
# o custo do cache por cidade. `foto.cidades_com_dado`/`cidades_em_implantacao`
# mudaram de lista de string para lista de {'nome','slug'} -- _VERSAO_FOTO
# subiu (11) por causa disso.
# app/consolidado.py, app/templates/publico/home.html.
#
# Ultimo toque: 28/08/2026 20h35 -- FASE 5 (parte 1) do plano das melhorias:
# duas pendencias fechadas.
# (1) CORRECAO DE TEXTO: "Quem construiu isto" ganhou "(Palmeiropolis)" --
# onde o projeto nasceu, pedido do dono.
# (2) P-124 FECHADA -- migrar_patrocinadores_para_periodo() rodava a CADA
# BOOT e RESSUSCITAVA patrocinador apagado pela tela (8 linhas da Relojoaria
# 3R voltaram com id novo, 28/08 de manha). Virou migracao de UMA VEZ SO' de
# verdade: tabela nova `migracoes_unicas` (marca por nome+data) guarda que
# ja rodou; da segunda chamada em diante a funcao nao toca em nada. Se
# ninguem tinha marcado nada ainda em producao, a PRIMEIRA chamada apos este
# boot ainda roda uma vez (idempotente, nao duplica) e so' DEPOIS trava.
# (3) SORTEIO NUTRICIONAL -- com a condicao de saude fora do cadastro
# publico desde a Fase 2, ela parou de fazer sentido como criterio de
# escolha do acompanhamento. `AcompanhamentoNutricional` ganhou
# `criterio_sorteio` (coluna+ALTER TABLE no mesmo gravar, regra 33);
# `criterio_saude` fica so' para registro historico (mostrado como "antigo"
# na tela, nao e' mais campo do formulario).
# app/models.py, app/blueprints/ranking.py, app/blueprints/execucao.py,
# app/templates/publico/home.html, app/templates/admin/execucao.html.
#
# Ultimo toque: 28/08/2026 20h15 -- FASE 4 do plano das melhorias (mexe em
# conta): QUATRO colunas novas (coluna+ALTER TABLE no mesmo gravar, regra
# 33) -- `Atleta.tipo_bicicleta` (speed/mtb/gravel, selo ao lado do nome,
# TAMBEM na tabela de km por pedido do dono nesta sessao -- Speed cobre mais
# km no mesmo tempo, nao so' e' mais veloz) e `Activity.velocidade_confiavel`
# (default True, o anticheat poe False sozinho em VELOCIDADE_ANORMAL/
# TEMPO_IMPOSSIVEL -- NAO tira km, regra 14, so' fica fora da media ate um
# admin confirmar em /admin/reviews).
# (1) `pace_corte_corrida` (Config, ja criado na Fase 2) agora DECIDE:
# `consolidado.ESCOPOS` ganhou 'corrida' -- esporte_strava quando existe
# (Run/TrailRun/VirtualRun), pace <= corte da CIDADE da atividade (subquery
# correlacionada) nas ~11 mil antigas sem o campo. Corrida e caminhada
# continuam juntas no ranking oficial (regra 35) -- a quarta tabela so'
# aparece na pagina inicial/consolidada.
# (2) `consolidado.mais_velozes()` NOVA: top 5 por MEDIA de velocidade,
# ciclismo e corrida, minimo 5 atividades (uma saida rapida sozinha nao vira
# media) -- so' conta atividade com `velocidade_confiavel=True`.
# (3) Checkbox nova em `/admin/reviews` (aprovar revisao de VELOCIDADE_
# ANORMAL/TEMPO_IMPOSSIVEL): "esta velocidade entra na media" -- desmarcada
# por padrao.
# app/models.py, app/consolidado.py, app/blueprints/admin.py,
# app/blueprints/anticheat.py, app/templates/publico/home.html,
# app/templates/admin/athlete_adjustment.html, app/templates/admin/reviews.html.
#
# Ultimo toque: 28/08/2026 19h50 -- FASE 3 do plano das melhorias: o
# `arquivar` atleta agora filtra de verdade. Medido antes: `ranking.py` e
# `consolidado.py` tinham ZERO mencoes a `Atleta.arquivado` -- o atleta
# continuava no ranking mensal e na pagina inicial mesmo arquivado. Duas
# colunas NOVAS (coluna+ALTER TABLE no mesmo gravar, regra 33):
# `Atleta.saiu_a_pedido` (separa duplicata esquecida -- volta sozinha -- de
# pedido de saida -- NAO volta) e `Activity.oculta_por_arquivamento` (marca
# SO o que este botao mexeu, pra desarquivar nao ressuscitar o que o
# anticheat rejeitou -- mesmo status='removida', causa diferente).
# `consolidado.ocultos()` passou a incluir arquivado (nao so
# oculto_no_publico) -- pagina inicial e consolidada param de mostrar nome
# de arquivado, km continuam no total. `consolidado.arquivados()` NOVA, so'
# para arquivado (sem oculto_no_publico, que continua aparecendo no
# ranking do proprio grupo por regra do termo) -- usada em
# `ranking.process_ranking()` pra tirar o nome da lista sem tirar do
# total_km. Botao de arquivar em `/admin/lista_atletas` ganhou caixa "e os
# km": marca `Activity.status='removida'` em lote (mesmo campo que toda
# soma do sistema ja respeita) + `saiu_a_pedido=True`; desarquivar com a
# caixa marcada restaura so' o que tem `oculta_por_arquivamento=True`.
# app/models.py, app/consolidado.py, app/blueprints/ranking.py,
# app/blueprints/admin.py, app/blueprints/anticheat.py,
# app/templates/admin/lista_atletas.html.
#
# Retoque 28/08/2026 19h35 -- ACHADO NO LOG enquanto conferia a P-81:
# `api.avisos_pendentes` (a rota que a automacao usa para montar o ranking
# em 3 partes) importava `BASE_URL` de `ranking.py` -- nome que NAO EXISTE
# mais desde a migracao de dominio (P-102), virou `BASE_URL_ATUAL()`
# (funcao, le o Host do pedido). O import falhava toda vez, silencioso
# (so um WARNING no log), e a automacao ficava sem o link do painel na
# mensagem. Corrigido: `app/blueprints/api.py` importa e chama
# `BASE_URL_ATUAL()`.
#
# Retoque 28/08/2026 19h30 -- CONFIRMADO NO AR (nao so ast.parse): pedi
# /participar e o combo so trouxe Palmeiropolis, Minaçu ficou de fora. O
# primeiro teste "deu vazio" por um bug no MEU grep de verificacao (o
# `<option value="..." >` do Jinja tem espaco antes do `>`), nao no codigo
# -- log de debug temporario confirmou `cidades_ativas()` certo o tempo
# todo, foi removido depois de provar. app/blueprints/adesao.py sem mudanca
# de comportamento desde o toque anterior.
#
# Ultimo toque: 28/08/2026 19h20 -- P-81: CIDADE ATIVA DE VERDADE. `Config`
# ganhou coluna `ativa` (coluna+ALTER TABLE no mesmo gravar, regra 33),
# default FALSO. Nova `consolidado.cidades_ativas()`: cidade e' ativa se tem
# ranking rodando (mesmo criterio de `cidades_com_dado`) OU foi marcada
# manualmente -- e' a UNIAO dos dois, nao so a flag. `vigia._cidades()`
# (Wake-on-LAN + alerta "sem sinal") e `adesao._cidades()` (inscricao
# publica) passam a usar esta funcao em vez de listar TODA linha de
# `Config`. Minaçu (vazia) para de mandar WoL as 10:27 e e-mail falso todo
# dia. Botao novo em `/admin/manage_cities` (rota
# `admin.toggle_cidade_ativa`, POST, master_or_aprovador) liga/desliga a
# flag manual -- para cidade nova em implantacao (sem dado ainda, mas deve
# operar) ou para desligar de vez uma que nao deve mais. Pedido do dono
# nesta sessao, ao ver o teste da Fase 2 oferecer Minaçu na inscricao.
# app/models.py, app/consolidado.py, app/vigia.py, app/blueprints/adesao.py,
# app/blueprints/admin.py, app/templates/admin/manage_cities.html.
#
# Ultimo toque: 28/08/2026 19h10 -- `/participar` so lista cidade ATIVA.
# `adesao._cidades()` lia TODO `Config.cidade`, inclusive Minaçu (vazia,
# sem grupo nem administrador rodando de verdade) -- quem escolhesse ficaria
# sem grupo para entrar. Passou a reaproveitar
# `consolidado.cidades_com_dado()`, a mesma fonte que a pagina consolidada
# ja usa para nao sugerir cidade sem atividade nenhuma (P-93). Achado pelo
# dono ao ver o teste desta sessao. app/blueprints/adesao.py.
#
# Ultimo toque: 28/08/2026 19h00 -- FASE 2 do plano das melhorias: a tela de
# inscricao parava de prometer o que o sistema nao faz. `Config` ganhou DUAS
# colunas NOVAS (coluna+ALTER TABLE no mesmo gravar, regra 33):
# `link_grupo_whatsapp` (mostrado na confirmacao da inscricao -- e' o que
# fecha o fluxo) e `pace_corte_corrida` (adiantando a Fase 4, mesma tabela).
# As duas editaveis em /config/edit, atras de master_or_aprovador_required
# (a rota inteira ja e' assim desde a P-92). `/participar`: SAIU "nao precisa
# de convite nem de estar em grupo nenhum" (falso -- sem link no grupo nao ha
# o que contar); SAIU o campo de e-mail, a caixa de condicao de saude
# (Adesao.observacao_saude PARA DE SER GRAVADA por esta rota -- registro
# antigo fica como estava) e a autorizacao de imagem (foram para os termos,
# VERSAO subiu para 2026-08-28b). ENTROU: titulo "Solicite sua participacao",
# campo opcional de link do PERFIL do Strava (SO REGEX, sem rede -- rota
# publica sem login nao pode gastar cota do throttle compartilhado, regra
# 4-C/4-P), telefone continua obrigatorio, e a confirmacao agora mostra o
# link do grupo (ou avisa que a organizacao entra em contato, se a cidade
# ainda nao tiver o link cadastrado). "Aparecer no publico" parou de ser
# pergunta na entrada (default True; a saida continua em `meus_dados`).
# app/models.py, app/blueprints/adesao.py, app/blueprints/config.py,
# app/termos.py, app/templates/publico/adesao.html,
# app/templates/publico/adesao_pronto.html, app/templates/config/edit.html.
#
# Ultimo toque: 28/08/2026 18h40 -- FASE 1 do plano das melhorias: textos
# errados no ar. (1) E-MAIL com o usuario trocado CORRIGIDO em 4 lugares
# (home.html x2, termos.py, _relatorio_base.html -- o dos relatorios era o
# mais grave, vai para o Ministerio e patrocinador). (2) Capa: "sua respectiva
# cidade" + Palmeiropolis desde 2021 em destaque. (3) "Como isto comecou"
# ganhou a origem real: a pandemia. (4) A oficina de bicicletas e' de um casal,
# e nao so de uma pessoa -- o Monarciclo e' dos dois. (5) Duas pessoas
# "ajudam a organizar", nao "participam". (6) "Este projeto e' de
# quem participa dele" virou enfase: NAO TEM DONO, quem esta a frente sao
# voluntarios. (7) "Apoie o projeto": telefone/WhatsApp como contato
# principal, com pedido de compartilhar o comprovante. (8) consolidado_publico
# .html: tirado o paragrafo "importado com autorizacao... nao passou pelo
# grupo" (tudo passou pelo grupo, so nao tinha sistema); tom do anticheat
# ficou informativo, nao punitivo ("nao entram" -> verificacao que funciona);
# saiu o paragrafo de "perfis repetidos" e entrou o caminho para pedir
# retirada dos km (por enquanto por e-mail -- o botao de verdade e' a Fase 3).
# ⚠️ NAO FEITO ainda: o texto de "o que banca" da Engekon (dominio, nao
# subdominio + infra/energia sem custo) e' DADO em patrocinadores_mensais,
# nao template -- fica para editar pela tela de admin, nao aqui.
#
# Ultimo toque: 28/08/2026 18h30 -- TRIMESTRE/SEMESTRE viram MARCO FIXO do
# calendario (jan-mar/abr-jun/jul-set/out-dez e jan-jun/jul-dez), nao janela
# rolante. Pedido do dono: entrar em setembro nao pode trazer junho (2o
# trimestre) para dentro do 3o. `consolidado.intervalo_do_periodo()` +
# rotulos em `consolidado.ROTULO_PERIODO` e `consolidado_publico.html`.
# Primeira fatia do plano `a-eu-refor-o-que-whimsical-lake.md` (melhorias da
# pagina inicial e do cadastro, pos-DOC E) -- as fases 1 a 5 seguem nesta
# mesma sessao.
#
# Ultimo toque: 28/08/2026 13h55 -- os retoques da porta de entrada, revisados
# ao vivo com o dono.
#
# (1) KM SEM CASA DECIMAL em quadro, tabela e card. O arredondamento e' feito no
#     DADO (`consolidado._km`), nao na tela: arredondar na exibicao faria os
#     anos exibidos somarem um numero diferente do total exibido, e a tabela
#     deixaria de fechar. Conferido: 51.095 + 141.732 = 192.827, e os seis anos
#     somam o mesmo.
# (2) O NUMERO NAO ESTOURA MAIS A CAIXA. Eram quatro colunas IGUAIS: "192.827"
#     cortava em tela estreita enquanto "95" desperdicava metade da dele. Virou
#     `flex: 1 1 auto` -- cada caixa pega a largura que o conteudo pede.
#     Medido em 320, 375, 414, 768 e 1280 px: zero numeros cortados.
# (3) TITULOS EM CAIXA ALTA pelo CSS, nao escrevendo "Como Isto Começou" no
#     HTML: o texto-fonte continua legivel e o acento fica certo.
# (4) A HISTORIA GANHOU O PESO QUE CADA UM TEVE: quem criou e sustentou o grupo
#     diariamente, quem fazia a contagem que nunca faltou, quem ajudou e quem fez
#     (medalhas e adesivos, administrador ate 05/2026), o Eng.º Maykon Abreu
#     como IDEALIZADOR E AUTOR do sistema desde 04/2025 (8 mil -> 50 mil
#     linhas), e a secao "quem faz acontecer" com o ponto de apoio da cidade
#     (Monarciclo, sorteios e brindes), e quem ajuda a organizar os eventos.
# (5) APOIE O PROJETO (PIX + QR) entrou ANTES da lista de patrocinadores.
# (6) Login do cabecalho ganhou "Lembrar" e o olho de mostrar/ocultar senha.
#
# ⚠️ PATROCINADORES: A LIMPEZA DA MANHA NAO PEGOU, e a causa vale a regra.
# Apaguei 8 linhas erradas e elas VOLTARAM com ids novos, porque
# `migrar_patrocinadores_para_periodo()` roda a CADA BOOT e recria tudo a partir
# de `monthly_backup.patrocinador_nome_mes`. Ela nao duplica, mas RESSUSCITA o
# que foi apagado -- que e' pior. A fonte errada estava no `monthly_backup`
# (08/2025 a 03/2026 com a Relojoaria congelada). Secada a fonte, as linhas
# sairam para valer. Agora: Engekon Engenharia continua desde 04/2025 (site),
# ONTURBO 04/2026, PATROCINADOR C 05/2026, SERVE MAIS 06/2026, Relojoaria SO 07/2026,
# Patrocinador D 08/2026, nutricionista continua desde 06/2026.
# ⚠️ `ordem` faz parte da chave unica (cidade, mes, ano, ordem): adicional entra
# na 1, o principal ocupa a 0.
# consolidado.py, home.html, base.html, __init__.py, login.html.
#
# Ultimo toque: 28/08/2026 11h45 -- P-95, P-99 e P-98, fechando o DOC E.
#
# `/admin/execucao` -- o que o projeto FEZ: acao educativa (data, tema, quem
# ministrou, canal, alcance, material anexado), acompanhamento nutricional (com
# o CRITERIO DE ESCOLHA em tres caixas separadas, porque "por que essa pessoa e
# nao outra?" e' a pergunta que derruba projeto social) e midia.
# ⚠️ A condicao de saude NAO e' copiada para ca: ela mora na `Adesao`, onde a
# pessoa a informou e de onde pode apaga-la. Espalhar dado sensivel por tabelas
# e' como se perde o controle de quem pode ve-lo.
# ⚠️ Midia NASCE INTERNA. So vira publica com consentimento confirmado, e video
# so com link do YouTube -- o DS215j nao serve video.
#
# `/admin/relatorios` -- os quatro documentos do Ministerio (beneficiarios,
# resultados, fotografico, despesas por rubrica). SEM BIBLIOTECA DE PDF: nao ha
# nenhuma instalada e sobram 243 MB de RAM. Sao paginas com `@media print`, e o
# navegador de quem le salva como PDF -- zero dependencia, zero RAM, zero
# trabalho em segundo plano. A lista de beneficiarios NAO leva telefone, e-mail
# nem saude, e quem pediu reserva entra como "Participante reservado" com os
# numeros preservados.
#
# `app/anexos.py` -- a validacao de arquivo agora e' UMA, para as tres telas:
# whitelist, magic bytes, nome novo gerado aqui, teto por familia e download
# sempre como attachment. O livro-caixa passou a usa-la em vez da copia dele.
#
# TRES DEFEITOS QUE O BANCO DE PROVAS PEGOU ANTES DE SUBIR: `mostrar_na_home`
# tinha caido na classe errada (`PatrocinadorCatalogo` em vez de
# `PatrocinadorMensal`) e quebrava a migracao de patrocinadores a cada boot; o
# `{% endif %}` do livro-caixa nao fechou; e a tela de relatorios usava `ind` com
# a rota mandando `indicadores`.
# models.py, anexos.py, blueprints/execucao.py, blueprints/relatorios.py,
# livrocaixa.py, consolidado.py, auditor.py, __init__.py, indice.py, 7 telas.
#
# Ultimo toque: 28/08/2026 11h35 -- P-97 (livro-caixa) e P-96 (a foto vazia).
#
# `/admin/caixa` -- escrituracao de verdade: data, tipo, categoria, valor,
# documento (NF/recibo/sem), de quem/para quem, rubrica do orcamento, anexo do
# comprovante e observacao. Mais conciliacao mensal com o extrato, e exportacao
# em CSV para o contador (CSV e nao PDF: ele abre no Excel e trabalha, e nao
# precisa de biblioteca nenhuma no servidor -- num NAS de 490 MB isso decide).
#
# ⚠️ A PRESTACAO DE CONTAS PASSOU A SER DERIVADA DO LIVRO (P-96). Ela tinha
# ZERO registros e a pagina publica de transparencia renderizava sem nenhum
# mes. Preenche-la a mao seria repetir o problema: dois lugares com o mesmo
# numero, divergindo. Agora `caixa.sincronizar_prestacao` leva os totais do mes
# para a foto publica a cada lancamento. Medido: 4 lancamentos viraram
# (2026, 8, patrocinado 500, medalhas 450, arte 80, outros 300).
#
# ANEXO DE COMPROVANTE segue a regra 28: whitelist de extensao, validacao por
# MAGIC BYTES (o arquivo E' o que a extensao promete?), nome NOVO gerado aqui
# (nome vindo de fora e' travessia de caminho), teto de 8 MB e download sempre
# como attachment -- nunca inline.
#
# APAGAR LANCAMENTO FAZ BACKUP ANTES, e **se o backup falhar nao apaga**
# (regra 9, do dono, de hoje). caixa.py, blueprints/livrocaixa.py, models.py
# (lancamentos_caixa + conciliacoes_caixa), __init__.py, indice.py.
#
# Ultimo toque: 28/08/2026 11h20 -- P-102: MoveMentes, e o endereco num lugar so.
#
# ⚠️ ACHADO QUE MUDA O PLANO: o `SERVER_NAME` do Flask NAO ESTA EM USO. Ele
# aparece so em `config.py`, e `create_app()` monta a configuracao a mao sem
# carregar aquela classe. O ponto "se o Host nao bater, o site inteiro devolve
# 404" -- o mais perigoso do DOC E e do DOC G -- simplesmente NAO EXISTE aqui.
# O site responde em qualquer Host que chegue nele, entao os dois dominios
# funcionam juntos assim que o Cloudflare apontar, sem instante critico.
#
# O endereco saiu de `ranking.py` e `alertas.py` e passou a morar em
# `app/enderecos.py`. Dentro de um pedido ele devolve o HOST QUE A PESSOA USOU
# -- quem entra pelo velho recebe links do velho, quem entra pelo novo recebe
# do novo, e ninguem e' jogado de um para o outro no meio da navegacao. Fora de
# pedido (e-mail de alerta) vale `SITE_URL` do ambiente.
# Os dois textos com comando copiavel agora leem `dominio_do_site`, entregue a
# todo template por um context_processor.
#
# MARCA: 71 ocorrencias em 52 templates + 24 em .py, trocadas LINHA A LINHA com
# lista de proibidos. NAO foram tocados: `@exemplo.com` (dominio de e-mail do
# cadastro -- "substituir tudo" ali derruba o login), os caminhos
# `movimente_se.db` / `/volume1/web/movimente-se`, e os nomes de tarefa do
# Windows (`Movimente-se_Dormir.ps1`). E **"Movimente-se com responsabilidade"
# em `termos.py` NAO e' marca, e' o verbo** -- ficou como estava.
# enderecos.py (novo), ranking.py, alertas.py, foradoperiodo.py, __init__.py,
# 52 templates.
#
# Ultimo toque: 28/08/2026 11h05 -- P-91 (patrocinador/auditor) + o cabecalho.
#
# TIPO NOVO `patrocinador`, nivel 1 (igual viewer, para que TODO decorator ja o
# barre) mais `pode_trocar_cidade` -- ele e' so-leitura E atravessa cidade, o
# que nao cabe na hierarquia linear. `is_supremo` deixou de ser o criterio de
# troca de cidade em `cidade_ativa`, `auth.trocar_cidade_ativa`, na navbar e no
# `inject_admin_badges`.
#
# A TRAVA E' ALLOWLIST E RODA NO SERVIDOR (`app/auditor.py`, `before_request`).
# Esconder card nunca fechou rota: medido, o auditor leva 403 nas 13 telas de
# operacao mesmo digitando o endereco, e abre as 7 de auditoria. Blocklist
# esqueceria a tela criada amanha -- tela nova nasce NEGADA, de proposito.
# Quem cria patrocinador tem de ser master ou supremo: nivel 1 sozinho o
# entregaria a quem cria um viewer.
#
# O CABECALHO QUE DESLIZAVA SOBRE O CONTEUDO: o `body` e' `display: flex`
# (segura o rodape embaixo) e a navbar `sticky-top` era um ITEM desse flex --
# item de flex e' esticado e encolhido por padrao, e o `sticky` passa a
# calcular sobre altura que muda. `body > nav.navbar { flex: 0 0 auto }` trava
# cada faixa no lugar. ⚠️ Nao consegui reproduzir o defeito no navegador de
# provas (ele nao rola de fato); esta e' a causa provavel, medida no CSS.
#
# LOGO DE PATROCINADOR AGORA TEM ETag + Cache-Control de 30 dias: elas pesam
# 896 KB a 1,8 MB (medido) e a inicial mostra todas em miniatura de 92 px.
# Segunda visita baixa 0 byte em vez de 4 MB. Encolher na origem e' a P-119.
# models.py, auditor.py (novo), __init__.py, auth.py, ranking.py, style.css,
# base.html.
#
# Ultimo toque: 28/08/2026 13h50 -- P-100: A ADESAO PUBLICA E OS TERMOS.
# `/participar` -- inscricao aberta a qualquer pessoa, SEM login e SEM convite.
# E' o que tira o projeto do "circuito privado" (Decreto 12.861/2026, art. 25,
# I), que sozinho reprovaria o projeto no Ministerio do Esporte. A mesma tela e'
# a do OAuth do Strava quando a P-89 acontecer.
#
# TRES CONSENTIMENTOS SEPARADOS, so o primeiro obrigatorio: participar do
# ranking; condicao de saude (dado sensivel, LGPD art. 11, com finalidade
# declarada e acesso restrito); uso de imagem. Cada um com a SUA data e a SUA
# revogacao. O aceite grava data, hora, IP (real, pelo cabecalho do Cloudflare)
# e a VERSAO do termo -- sem a versao, consentimento e' inverificavel.
#
# `/participar/meus-dados/<chave>` e' a porta de saida, e ela NAO pede login:
# exigir senha para retirar consentimento, num sistema onde a conta nasce
# inativa, seria trancar a saida. Retirar o consentimento de saude APAGA o
# texto, nao so desmarca.
#
# Termos versionados em `app/termos.py`. Levantei a estrutura lendo os termos do
# Strava (que assuntos um termo desses tem de cobrir) e trouxe so o que se
# aplica a projeto comunitario -- idade minima, conduta, risco de lesao, aviso
# de GEOLOCALIZACAO (o link do Strava mostra o trajeto, e quem controla isso e'
# a conta de la), dependencia de terceiros, retencao e direitos do titular.
# Ficou de fora tudo que e' de empresa: assinatura, publicidade, arbitragem,
# DMCA, licenca comercial sobre o conteudo de quem participa.
# models.py (tabela `adesoes`), termos.py, blueprints/adesao.py, __init__.py,
# indice.py, 4 telas em templates/publico/.
#
# Ultimo toque: 28/08/2026 10h40 -- A PORTA DE ENTRADA, revisada com o dono.
# (1) SAI a lista "somente corrida": sao DUAS modalidades e ponto -- nao existe
#     ritmo que separe corrida de caminhada com honestidade, e partir isso na
#     tela criaria uma competicao que o projeto nao quer. O `esporte_strava`
#     continua sendo gravado, para relatorio, nunca para partir o ranking.
# (2) A PESSOA ESCOLHE SE APARECE: `Atleta.oculto_no_publico`, com botao na tela
#     do atleta. Marcado, o nome some da inicial e das listas publicas e
#     `/ath/<id>` da 404 para visitante -- mas os KM CONTINUAM contando no
#     ranking, na medalha e no total. E' anonimato na vitrine, nao saida.
# (3) LISTA DE PATROCINADORES na inicial: nome, logo em miniatura, o que banca e
#     quando; quem apoia todo mes vira uma linha marcada "todo mes". Quem nao
#     quiser aparecer se desmarca (`PatrocinadorMensal.mostrar_na_home`).
# (4) Os numeros se refazem SOZINHOS a cada upload: a foto guarda `MAX(id)` das
#     atividades e so recalcula quando ele muda -- uma consulta de chave
#     primaria por visita, em vez de 6 agregacoes.
# (5) Ordem, titulos e textos da inicial como o dono pediu: os 3 cards subiram,
#     a historia veio antes das listas, entrou "Quem Esta A Frente Hoje" com
#     contato, e o aviso de limites deixou de dizer "pare" para dizer "reduza e
#     controle-se".
# models.py, consolidado.py, admin.py, athlete.py, ranking.py, base.html.
#
# Ultimo toque: 28/08/2026 09h32 -- O ESPORTE DO STRAVA PASSA A SER GUARDADO.
# O `sportType` chegava em toda raspagem e era jogado fora pelo SPORT_MAP, que
# reduz tudo a corrida_caminhada/ciclismo. Agora ele fica em
# `Activity.esporte_strava` (coluna nova, ALTER guardado por PRAGMA), gravado
# nos tres caminhos de escrita: listagem do perfil, JSON da atividade e o
# fallback por regex. E' o que separa CORRIDA de CAMINHADA com dado em vez de
# palpite -- nas ~10 mil linhas antigas o campo fica vazio e ali, so ali, o
# ritmo (<= 8 min/km) continua sendo o desempate, dito na tela.
#
# ⚠️ LICAO CARA, DESTA SESSAO: eu gravei o ATRIBUTO no modelo num lote e o
# ALTER TABLE no lote seguinte. Entre os dois, o modelo pedia uma coluna que o
# banco nao tinha e a producao devolveu 500 -- sete vezes, em pagina de atleta
# e na reconstrucao do ranking. Aqui salvar E' aplicar: modelo e ALTER vao no
# MESMO gravar, sempre. (`ERROS.md` §4.35.)
# models.py, processing.py, upload.py, consolidado.py.
#
# Ultimo toque: 28/08/2026 09h25 -- P-93 e a nova PORTA DE ENTRADA (P-94).
# `/` deixou de ser um desviador para o login e virou pagina publica: capa com
# a marca MoveMentes, UMA tabela com os quilometros ano a ano por modalidade
# (mes corrente destacado, total do projeto na ultima linha), historia do grupo
# com os creditos, cidades no estado real e o aviso de limites. Os campos de
# login subiram para o cabecalho verde (base.html), como o plano pedia.
# A conta mora em `app/consolidado.py`, o unico lugar que soma ATRAVESSANDO
# cidade -- com deduplicacao por `id_atividade` (a restricao unica e'
# (id_atividade, cidade), entao a mesma atividade pode existir em duas cidades)
# e tudo em SQL, nunca `.all()` em memoria. Foto do dia em disco: a pagina
# publica nao paga 6 agregacoes por visita, e a rotina diaria refaz.
# Regra de numero fixada: o total e' a SOMA DO QUE ESTA NA TELA -- somar o
# bruto dava 183.177,5 com as parcelas exibidas somando 183.177,6, e tabela de
# auditoria que nao fecha e' pergunta que ninguem quer responder.
# Pagina detalhada em /ranking/consolidado, com mes/trimestre/semestre/ano e
# periodo livre. consolidado.py (novo), ranking.py, __init__.py, indice.py.
#
# Ultimo toque: 28/08/2026 09h05 -- P-92, o buraco de permissao. A rota
# `config.edit_config` exigia so @login_required: qualquer usuario logado,
# inclusive um VIEWER, editava meta, chave PIX e configuracao da cidade. O
# template ja escondia o botao -- esconder card nunca protegeu rota. Agora ela
# exige @master_or_aprovador_required e `view_config` declara @viewer_required.
# Mais as duas divergencias catalogo x rota: `admin.comandos` (catalogo dizia
# is_master, rota e' supremo -> card dava 403) e
# `admin.limpeza_copias_palmeiropolis` (catalogo dizia is_supremo, rota era
# master -- o inverso, e mais grave: a rota valia menos que o anuncio).
# config.py, admin.py, indice.py.
#
# Ultimo toque: 28/08/2026 03h05 -- AS DUAS PAGINAS SE COMPLETAM. Para
# atividade recente o Strava da, na URL anonima, uma casca com a DATA e sem
# numeros; e, com cookie, a classica com a DISTANCIA e sem data. Cada uma
# sozinha era recusada pela cadeia (que exige as duas) e o link virava "nao
# pude ler" com os dois dados em maos. Agora a casca e' COLHIDA antes de ser
# descartada e as leituras se juntam (`_juntar_leituras`). Medido: a data da
# anonima bateu ao segundo em 6 de 6 e a distancia da logada em 6 de 6; numa
# amostra de 12 atividades de 2021 a 2026, dono 12/12, mes 12/12, ZERO
# falhas (antes desta correcao, 2 de 5 falhavam).
# ACHADO QUE CORRIGE O PLANO: a anonima quase nao traz mais `streams`, entao
# o gps_fingerprint veio em 0 de 12 mesmo com ela primeiro -- o cookie nao
# era a causa principal, a pagina degradou. Nao foi "restaurado".
# processing.py.
#
# Ultimo toque: 28/08/2026 02h40 -- `year_offset` passa a ser SEMPRE 0. Ele e'
# dica de paginacao da tela, nao endereco do mes (quem diz o mes e o
# `interval`), e so vale ate a idade da conta: pedir 5 anos atras num perfil
# criado ha 3 devolve HTTP 400. Era isso que fazia a importacao de atleta
# recente perder TODOS os meses anteriores a conta dele -- medido as 02h34:
# 43 de 64 meses, todos os anteriores a 2023 em 400, e cada nova tentativa
# gastava requisicao para levar 400 de novo. Provado que nao muda o mes:
# mesmo `#totals` com o offset calculado e com 0. processing.py.
#
# Ultimo toque: 28/08/2026 02h30 -- a CONFERENCIA tambem passou a ler a
# listagem como estrutura, e a filtrar pelo DONO. Ela ainda usava o regex de
# `entity_id`, que casa com id de COMENTARIO: numa conferencia isso vira "o
# atleta esqueceu de postar" para atividade que nunca existiu, e cada uma
# custa uma requisicao. Medido: um atleta 08/2022 sai de 76 ids para 28, que sao
# os dele. Saem `_activity_ids_from_text` e `_bloco_das_atividades`, sem
# chamador. processing.py.
#
# Ultimo toque: 28/08/2026 02h25 -- duas correcoes achadas rodando o primeiro
# atleta pelo caminho novo. (1) O tempo vem do rotulo "Tempo" da listagem, que
# e o tempo EM MOVIMENTO, e nao mais do `elapsedTime`, que conta paradas:
# numa amostra 9.240 s contra 10.082 s, e a diferenca fazia uma corrida sair a
# 0,32 km/h e o anti-cheat marcar suspeita. Sem tempo, a atividade entra sem
# velocidade -- numero errado e pior que numero ausente (4-M). (2) Sai o
# pre-filtro anual: medido em 6 variantes, o Strava responde 200 com corpo
# VAZIO ou 400, nunca com `#totals`. Ele nunca chegou a pular mes indevido
# (None nao pula), so gastava uma requisicao por ano para nao concluir nada.
# processing.py, upload.py.
#
# Ultimo toque: 28/08/2026 02h15 -- um trabalho pesado por vez
# (_LIMITE_TRABALHOS_PESADOS = 2 -> 1). O gargalo nao e' CPU, e' o espacamento
# ao Strava, que e' compartilhado: dois so faziam um esperar o outro. Medido
# nesta madrugada, os dois presos no mesmo mes com "Outro processamento esta
# usando o Strava". upload.py.
#
# Ultimo toque: 28/08/2026 02h -- upload de atleta pela LISTAGEM: um mes custa
# UMA requisicao (antes: 1 + uma por atividade; 08/2022 de um atleta gastava 77).
# Extrator estrutural do /interval no lugar do regex que casava id de
# COMENTARIO -- a causa dos 17 atletas estrangeiros de 27/08. Guarda pelo DONO
# da atividade: parceiro cadastrado leva os km dele, quem nao esta no cadastro
# nao entra. Volta a coleta do gps_fingerprint (pagina anonima primeiro), que
# eu quebrei em 27/08 ao mandar cookie em tudo. SharedPacer.reserve parou de
# apagar o contador de volume; tetos passam a ser os do Strava (100/15min,
# 1000/dia) e o espacamento vai a 9s no upload de atleta. Saem o flyover (que
# redireciona para /dashboard, proibido no robots.txt) e o leitor "marcador"
# (inventou 5,00 km numa pagina de marketing).
# processing.py, upload.py, ratepacer.py.
#
# Ultimo toque: 16/08/2026 23h35 -- robustez do botao Conectar: o site insiste
# com Wake-on-LAN, sessao FAILED e refeita do zero (medido: `start` nao sai do
# lugar e `restart` so pede para apagar a copia) e o proprio site confirma a
# conexao no fim. whatsapp.py, vigia.py, api.py, admin.py.
#
# Ultimo toque: 17/08/2026 -- itens 4.1-b/f e 5 do plano do MVP: alavanca
# "manter ligada" no painel (secretstore.py, api.py, admin.py, dashboard.html)
# e etapa nova "Sessao sem copia" em alertas.py.
#
# Ultimo toque: 17/08/2026 (2a passada) -- itens 0, A, B, C, D, E, F, G, H do
# MVP: botao "Desligar a maquina" (secretstore.py, api.py, admin.py,
# whatsapp_sessao.html), tela deixa de afirmar status velho como atual
# (admin.py, whatsapp_sessao.html). A maior parte dos itens desta passada e
# em scripts do Dell/Windows, fora deste repositorio.
#
# Ultimo toque: 20/08/2026 -- importacao de atleta parava de retomar apos 3
# interrupcoes mesmo com progresso real (checkpoint por mes existe desde
# 14/08, mas o contador de tentativas nunca zerava); CSRF de /upload/atleta
# passou a logar em portugues em vez de falhar calado (upload.py). Ver
# ERROS.md 4.14. Toque de conteudo, nao so carimbo (ver ERROS.md 4.13 e
# CLAUDE.md regra 1).
#
# Ultimo toque: 20/08/2026 21h -- importacao de atleta em LACO INFINITO
# (medido: 41 retomadas num dia, o mesmo link buscado 29 vezes no Strava,
# nenhuma atividade salva). Tres consertos: (1) o motivo de cada atividade
# ignorada passa a ser dito com o nome dele -- "de outras pessoas (treino em
# grupo)" nao e mais confundido com "fora do periodo"; (2) link ja resolvido
# nesta importacao nao e buscado de novo, e a tentativa e marcada ANTES da
# requisicao, para que um link que congela nao se repita a cada retomada;
# (3) o contador de tentativas so zera com trabalho CONCLUIDO (meses lidos +
# links resolvidos), nunca com batimento -- e ao esgotar, o job termina com
# aviso na tela em vez de ficar parado para sempre.
# processing.py, upload.py. Plano: docs/planos/2026-08-20_importacao-em-looping.md
#
# Ultimo toque: 20/08/2026 21h40 -- dois ajustes vistos AO VIVO na rodada
# que fechou a importacao do perfil <id-1>: a marca do link passa a ser
# gravada na hora (em lote ela se perdia justamente no link que congela) e
# o resumo final soma o job INTEIRO, nao so a ultima retomada -- a tela
# tinha fechado com "0 salvas" numa rodada que resolveu 23 links. Entrou
# tambem o numero de atividades que o Strava nao deixou ler. upload.py.
#
# Ultimo toque: 20/08/2026 22h -- P-80 RESOLVIDA, e nao era travamento: era
# SILENCIO. Um link faz ate 4 requisicoes (pagina normal e alternativa,
# cada uma com flyover) e cada uma esperava 75s sem dizer nada ao job:
# 4x75 + o espacamento = 318s calados, contra os 300s em que a importacao
# declara um job morto. Agora a espera e fatiada e avisa que esta viva
# (repetindo a mensagem da tela, sem apagar o "15 de 23"); um link inteiro
# tem teto de 150s; e as tres requisicoes que ainda escapavam do prazo
# passaram a usar o _hard_get. processing.py. Ver ERROS.md 4.18.
#
# Ultimo toque: 20/08/2026 22h15 -- sobrou um trecho MUDO fora da rede: um
# link ficou 5 min calado sem o _hard_get ter desistido e sem o pulso ter
# disparado, entao o tempo nao foi na requisicao. Suspeita: a leitura da
# pagina (html.parser em Python puro, pagina de 1 MB, NAS de 512 MB).
# Entrou cronometro que MEDE em vez de supor (avisa acima de 20s no parse e
# 60s no link) e o prazo de "job morto" da importacao voltou de 5 para 12
# minutos, porque retomar cedo demais faz os dois workers raspar o mesmo
# job ao mesmo tempo. processing.py, upload.py.
#
# Ultimo toque: 20/08/2026 23h -- importacao de atleta passa a processar
# MES A MES de verdade: lista, raspa e SALVA cada mes antes de seguir para
# o proximo (antes, a listagem inteira do periodo pedido rodava primeiro e
# so DEPOIS a validacao/gravacao rodava de uma vez so -- um travamento no
# meio, ex. mes 43 de 62, deixava ZERO atividades salvas mesmo com dezenas
# de meses ja listados). Tambem: piso de importacao virou configuravel POR
# CIDADE (Config.piso_importacao_ano/mes, Painel > Cidades > Editar) em vez
# de constante fixa; caixa de selecao nova para importar desde a fundacao
# do grupo mesmo sem link postado com o perfil atual (atleta que trocou de
# conta Strava); atividade importada por perfil ganhou a tag
# Activity.fora_do_grupo (NAO tira do ranking, so permite depois um total
# separado de "quanto veio de link postado no grupo"); e qualquer excecao
# nao categorizada ao raspar um link ou listar um mes deixou de derrubar o
# job inteiro -- vira pendencia com log detalhado (tipo da excecao +
# traceback) e o job segue. models.py, admin.py, upload.py, processing.py,
# templates admin/add_city.html, admin/edit_city.html,
# upload/importar_atleta.html. Plano: attach-curious-brooks.md.
#
# Ultimo toque: 20/08/2026 23h43 -- ACHADO AO VIVO logo no primeiro teste da
# caixa nova: cidade sem "fundacao do grupo" cadastrada + caixa marcada
# falhava DENTRO da thread de fundo, tao rapido (milissegundos) que o job ja
# estava em stage=error antes do redirect pos-POST conseguir encontra-lo --
# `_job_atleta_em_andamento` ignora job em estagio terminal de proposito. A
# tela parecia travada: clicou, redesenhou, nada apareceu. A mesma checagem
# agora roda SINCRONA na view, antes de iniciar a thread -- vira erro de
# formulario normal, na mesma pagina, como qualquer outro. upload.py.
#
# Ultimo toque: 21/08/2026 11h25 -- causa raiz dos 20 travamentos de 12 min
# na importacao do perfil <id-3> (madrugada de 21/08): a interpretacao do
# HTML (BeautifulSoup + extracao) rodava sem NENHUM teto de tempo -- so a
# requisicao de rede em si tinha. Ganhou o mesmo tratamento do _hard_get,
# com prazo de 30s (_hard_run, processing.py). Tambem corrigido: com 2
# processos no uwsgi.ini, a checagem de "job ja esta rodando" so olhava a
# memoria do PROPRIO processo (_LOCAL_THREADS) -- se o ticker de outro
# worker checava o job, nao via a thread e reanimava um trabalho que
# continuava vivo no worker original, causando execucao duplicada
# concorrente. Agora grava o PID dono no job e confere se ele ainda esta de
# pe antes de concluir "morreu" (upload.py: _maybe_resume_job, _is_stale_job).
# E a reconciliacao (candidato do mes sem classificacao) deixou de ser so um
# aviso no log: o mes com reconciliacao pendente nao e mais marcado como
# concluido, entao uma proxima retomada tem chance real de fechar a conta
# (processing.py, upload.py). Ver plano de medio prazo (migracao para API
# oficial do Strava) em docs/planos.
#
# Ultimo toque: 21/08/2026 20h -- importacao de atleta por perfil, modo
# "todos os periodos", varria mes a mes do PISO ate hoje, na ordem
# cronologica, sem parar: atleta que so comecou a postar em 2026 tinha cada
# mes vazio de 2021 a 2025 consultado no Strava do mesmo jeito (pedido do
# dono, 21/08). Agora, so nesse modo (periodo ESPECIFICO continua
# respeitado a risca), a varredura vai do mes mais recente para o mais
# antigo e para apos _PROFILE_SCAN_STOP_AFTER_EMPTY (12) meses seguidos sem
# atividade -- mesma politica ja aceita na importacao geral, reaproveitada,
# nao inventada (upload.py, processing.py). Tambem: telefone real e um LID
# real que apareciam como EXEMPLO em comentarios de 6 arquivos viraram
# placeholder generico (55DDD9XXXXXXXX), e o comando SSH de acesso a
# producao em admin/comandos.html deixou de ser hardcoded no template --
# agora vem do cofre (secretstore.SSH_HOSTNAME/SSH_USER, o mesmo mecanismo
# ja usado para as outras chaves da automacao; .env NAO serve pra isto,
# nada no projeto o le, e uwsgi.ini, que serviria, esta rastreado pelo Git).
# O painel continua mostrando o valor real para quem tem a tela -- so
# precisa ser gravado no cofre uma vez (instance/secrets.json, fora do Git).
# (admin.py, secretstore.py, agendatelefone.py, filamensagens.py,
# avisos.py, auth.py, models.py, templates/admin/comandos.html). Contexto:
# plano docs/planos/2026-08-21_F_github-portfolio_SONNET-medio.md.
#
# Ultimo toque: 22/08/2026 -- a mudanca de 21/08 acima (parar apos 12 meses
# vazios) foi REJEITADA pelo dono e desfeita: atleta pode sumir 2+ anos do
# Strava e voltar, e aquela heuristica pularia esse retorno. No lugar entrou
# um PRE-FILTRO POR ANO (_buscar_intervalo_ano, processing.py): o total de
# km de um ano inteiro e a soma dos 12 meses, entao ano com total <= 0 e
# PROVA (nao suposicao por silencio) de que nenhum mes dele tem atividade --
# falha de leitura ou ano com atividade nunca pula, cai na varredura normal.
# Vale para "todos os periodos" E "ano inteiro" (throttle_profile ==
# 'ano_especifico'); "periodo especifico" nunca entra nisso. A ordem final
# continua do mes mais recente pro mais antigo (pedido do dono -- o que
# estava errado nao era a ordem, era presumir vazio cedo demais). Codigo
# morto da tentativa antiga de "todos os periodos" dentro de
# _profile_urls_por_intervalo (processing.py) removido de vez -- nenhum
# chamador real jamais usava aquele caminho.
#
# TAMBEM NESTE LOTE: o mes vigente (ainda aberto) agora e EXCLUIDO de "todos
# os periodos" e de "ano inteiro" (_todos_meses_ate_hoje e _periodos_upload,
# upload.py) -- foi assim que um atleta (ja saiu do grupo) e uma atleta
# (nao posta link) entraram no ranking de agosto/2026 com km vindos direto
# do perfil Strava dela, nao de link postado no grupo. Cancelar uma
# importacao em andamento passou a ser garantidamente uma parada de fato:
# _hard_get/_hard_run (processing.py) checam cancelamento a cada 5s de
# espera (antes so checavam tempo), _finalize_stale_cancel sempre avisa uma
# thread que ainda esteja viva no mesmo processo, e _set_progress recusa
# reescrever um job ja terminal -- e o botao Cancelar em importar_atleta.html
# agora pede confirmacao antes de mandar o pedido. Nova rota
# admin.alternar_contagem_atividade + caixa de selecao em
# admin/athlete_adjustment.html: o admin marca/desmarca, na hora, se uma
# atividade conta no ranking (reaproveita Activity.status ativa/removida, o
# mesmo campo que admin.rejeitar_revisao ja usava, sem exigir flag de
# anticheat). Plano completo: docs/planos/attach-cheerful-widget.md.
# Arquivos: processing.py, upload.py, admin.py,
# templates/upload/importar_atleta.html,
# templates/admin/athlete_adjustment.html.
#
# Ultimo toque: 22/08/2026 00:57 -- card de sessao em /admin/whatsapp-sessao
# ficava preso em "Sem noticia recente" quase sempre (achado do dono, ao
# vivo): `instancias_waha.visto_em` so atualiza 1x por rodada (2x ao dia,
# registrar_instancias.py), e o prazo de validade do card e 40 min -- quase
# sempre estourado mesmo com tudo saudavel. `whatsapp.estado_da_sessao`
# agora pergunta a sessao AO VIVO primeiro (GET /api/sessions/<nome>, 6s de
# prazo, so leitura -- nunca reinicia/derruba nada), e so cai no relato
# antigo (com prazo de validade) quando a maquina nao responde agora.
# Testado localmente antes deste toque: painel_da_cidade('Palmeiropolis')
# devolveu as duas sessoes como "Conectado" em 0.14s, contra o
# "Sem noticia recente" que a tela mostrava com o relato de ontem 15:30.
# app/whatsapp.py. Plano: docs/planos/attach-cheerful-widget.md (extensao
# pedida pelo dono na madrugada de 22/08, depois das 4 fases originais).
#
# Retoque 22/08/2026 00:26 -- primeiro boot falhou por __pycache__ velho
# (upload.cpython-39.pyc nao tinha sido invalidado sozinho, ainda importava
# o nome removido _PROFILE_SCAN_STOP_AFTER_EMPTY). __pycache__ apagado antes
# deste toque -- sem mudanca de codigo.
#
# Retoque 22/08/2026 00:35 -- create_app() testado localmente (mesma copia
# do codigo, via Z:) e funciona limpo; o site em producao continua com
# "Internal Server Error" mesmo depois do toque anterior, sem nenhum boot
# novo em wsgi_debug.txt. Forcando novo toque para confirmar se o
# touch-reload ainda responde.
# Ultimo toque: 22/08/2026 15h55 -- EXIGENCIA DE E-MAIL @exemplo.com REMOVIDA
# do cadastro (decisao do dono). Motivo: o tenant Microsoft 365 E5 tem so 16
# licencas, entao a trava limitava o projeto inteiro a 16 pessoas -- e o
# projeto precisa de porta de entrada publica (requisito do Ministerio do
# Esporte, P-100). NAO abre buraco de seguranca: o cadastro publico ja cria a
# conta com `ativo=False` e ela so vale depois que um admin ativa; no
# `admin.add_user` quem cria ja e um admin autenticado. A camada que entra no
# lugar e a confirmacao de e-mail (P-103), usando o remetente que ja existe
# (o remetente guardado no cofre, via Microsoft Graph em app/correio.py). Tocados:
# auth.py, admin.py e os placeholders de add_user.html/edit_user.html (diziam
# "email@exemplo.com"). Nada de automacao/pipeline foi tocado.
# Ultimo toque: 23/08/2026 -- pode-dormir passa a olhar a agenda de
# publicacao (PostagemAgenda) antes de liberar o desligar: pedido manual
# dentro dos 15 min (mesma antecedencia do Wake-on-LAN, vigia.py) antes de
# qualquer horario ativo (ranking, parabens, avisos, promocoes) agora e
# recusado, com o motivo dizendo qual horario e' esse. Achado no incidente
# de 22/08 as 20h50 (ver ERROS.md 4.21) -- nao chegou a acontecer daquela
# vez, mas nada impedia. api.py.
#
# Ultimo toque: 24/08/2026 -- alerta de "Automacao sem sinal" (vigia.py)
# baixado de 120 para 30 minutos de tolerancia, a pedido do dono (o ranking
# das 05:30 sumiu e o e-mail so saiu as 07:34, tarde demais pra ser util).
# ATENCAO: 30 min fica ABAIXO da tolerancia de 90 min do publicador -- pode
# gerar alarme falso para uma rodada atrasada mas que ainda ia dar certo;
# risco aceito conscientemente, o dono topou reajustar se incomodar. Ver
# ERROS.md 4.23. vigia.py. (A correcao da Trava 3 de Movimente-se_Dormir.ps1
# do mesmo incidente e' script do Dell, fora deste repositorio.)
#
# Ultimo toque: 27/08/2026 (6a passada) -- O RANKING ATRAVESSA A TRAVA; O
# UPLOAD DE ATLETA OBEDECE. Corrige um efeito colateral que o teto da 5a
# passada criou: upload de atleta NAO pode causar falha na saida do ranking.
# A finalidade viaja com o trabalho (`params['prioridade']`): o TXT que a
# automacao manda as 05:30/15:30 entra como 'ranking' e passa por cima do teto;
# upload humano e importacao/recuperacao de perfil obedecem e CONGELAM.
# O ranking CONTA o que gasta (senao o retrato do dia fica falso), so nao e'
# barrado. Nada de cota reservada: ele usa poucas dezenas.
# E SE O STRAVA barrar o ranking -- ai e' bloqueio de verdade -- vira aviso com
# a PROVA junto (links visitados, pendencias, o nosso contador do dia e a frase
# "a trava interna NAO barra o ranking"). O desfecho aceitavel e' "o ranking
# nao saiu"; nunca "ninguem soube".
# Raciocinio que fecha (dono): a janela diaria vira as 21h (meia-noite UTC), o
# servidor desliga 23:05 -- nao ha como gastar 1.000 nesse intervalo, entao a
# rodada das 05:30 sempre encontra cota, e a fila volta a esvaziar depois das 6h.
# Testado sem rede: com o teto estourado, o upload para e o ranking passa.
# api.py, upload.py, processing.py. Ver ERROS.md 4.33.
#
# Ultimo toque: 27/08/2026 (5a passada) -- TETO DE VOLUME NO ACESSO AO STRAVA.
# O dono apontou o que faltava, e estava certo: havia ESPACAMENTO (SharedPacer,
# ~4,6s entre requisicoes) e nao havia TETO. Espacamento sozinho nao impede 500
# requisicoes por hora, hora apos hora. MEDIDO HOJE: 1.312 requisicoes no dia,
# pico de 522 numa hora (01h) -- e depois 7x HTTP 429 e 12x 403, com o
# /interval devolvendo 400 em todos os year_offset.
# Agora o `SharedPacer` conta em TRES janelas naturais (15 min / 1 hora / 24 h,
# fechando em 0/15/30/45 e a meia-noite UTC, como o Strava alinha as dele), com
# o contador EM DISCO -- sobrevive a reload e a reinicio. Tetos: 60/250/1000,
# metade do que nos bloqueou, ajustaveis por variavel de ambiente.
# O teto e' consultado em `StravaThrottle.get`, o portao unico: vale para
# importacao, upload, conferencia e recuperacao sem nenhuma precisar lembrar.
# Batendo o teto o trabalho **CONGELA, nao morre**: reusa o `pausa_programada`
# do desligamento do servidor (tira da deteccao de "parado", LIBERA A VAGA,
# aparece como pausado na tela) e volta pelo RELOGIO (`pausa_ate`) quando a
# janela virar. Testado sem rede, 6 casos, em diretorio proprio.
# ratepacer.py, processing.py, upload.py. Ver ERROS.md 4.32.
#
# Ultimo toque: 27/08/2026 (4a passada) -- CADA MODO DE FALHA COM NOME PROPRIO,
# E A DATA QUE FALTAVA. Pedido do dono: "separe o que e bloqueio do Strava do
# que e erro propriamente dito", sem ficar restritivo a ponto de bloquear tudo.
# SEIS MODOS, medidos um a um: (1) muro de cadastro, 200 -> /register/free;
# (2) casca de autenticacao, 200 com isLoggedIn:false; (3) **PAINEL**, 200 ->
# /dashboard; (4) Microfrontend, 200 sem numero nenhum; (5) **bloco sem
# startDateLocal**; (6) limite do Strava, 429/403 -- o UNICO que e' bloqueio.
# O CASO 3 ERA O PERIGOSO: quando a atividade nao esta disponivel para a conta
# do cookie, o Strava entrega o PAINEL (o feed da propria conta) com HTTP 200.
# Nada via isso, e o leitor de ultimo recurso varre a pagina atras de numeros:
# seria km de OUTRA PESSOA gravado como do atleta. No teste a ancora segurou,
# mas por pouco (`activity_id` esta no painel) -- agora e' detecao explicita,
# ANTES de qualquer leitor tocar no corpo.
# O CASO 5 CUSTAVA ATIVIDADE DE VERDADE: atividade recente traz distance,
# elev_gain, moving_time, calories... e NAO traz startDateLocal. Sem data a
# cadeia rejeitava a leitura inteira. A data esta na pagina, em texto humano,
# na tag <time>, NO IDIOMA DO Accept-Language (pt-BR aqui). Os dois formatos
# sao lidos e conferidos contra a pagina real:
#   pt "17:10, terca-feira, 18 de agosto de 2026" / en "5:10 PM on Tuesday...".
# Tambem: `conferenciaauto` passou a usar o THROTTLE -- ela chamava
# `_process_single_strava_url` em laco SEM espacamento, a regra 4-C furada por
# dentro. E entrou o trabalho `recuperacao_links`, que reprocessa o que uma
# importacao nao conseguiu ler: 405 links de 9 atletas ja na fila, com o mes
# vigente de fora (e do processamento do mes) e a guarda 4-D no fim.
# processing.py, upload.py, jobrunner.py, conferenciaauto.py. ERROS.md 4.32.
#
# Ultimo toque: 27/08/2026 (3a passada) -- SCRAPING A PROVA DE FALHA: CINCO
# LEITORES E UMA FONTE QUE NAO MUDA (pedido do dono).
# O Strava serve TRES paginas para a mesma atividade, medido hoje de ponta a
# ponta: (1) sem cookie, Next.js de 865 KB; (2) com cookie e atividade antiga,
# pagina classica de ~120 KB com os numeros em `pageView.activity().set({...})`
# -- e ha DOIS blocos com esse nome, so o segundo tem os numeros; (3) com
# cookie e atividade recente (2023+), casca **Microfrontend** de ~101 KB SEM
# numero nenhum (chegam por XHR). Raspar HTML no caso 3 nunca funciona, e o
# `/flyover` NAO salva: ali ele devolve o FEED DA CONTA LOGADA -- regex ingenuo
# colhe km de outra pessoa.
# A FONTE ESTAVEL E' O EMBED (`strava-embeds.com/activity/<id>`): sem cookie,
# 27 KB (30x menor que a pagina anonima), contrato publico de incorporacao.
# Limites medidos: 1 casa decimal, sem hora, e SEM id do atleta -- o id sai dos
# links `/athletes/<id>` da pagina menos a conta logada (`_dono_pela_pagina`).
# Sem dono nao ha guarda da regra 4-D, entao o dado e' descartado.
# Cadeia: next_data -> pagina logada -> regex -> marcador -> embed, e o log diz
# SEMPRE qual leitor resolveu -- e' assim que a proxima sessao descobre que um
# deles apodreceu ANTES de virar link perdido.
# O leitor de ultimo recurso ganhou guarda de ancora: no teste ele achou um
# `"distance": 5` solto numa pagina de marketing e devolveu 5,00 km. Leitor que
# INVENTA quilometragem e' pior que leitor que falha -- km errado ninguem ve.
# VEREDITO DAS 76 "nao puderam ser lidas" de 26/08: 38 eram treino de terceiro
# (4-D, descarte certo), 3 fragmentos de 0,0 km, 14 indeterminadas e **19 PERDA
# REAL, todas de uma atleta (<id-4>)**, de 2022-10 a 2026-02 -- nenhuma em agosto,
# entao o ranking do mes nao muda. processing.py. Ver ERROS.md 4.31.
#
# Ultimo toque: 27/08/2026 (2a passada) -- TODO ACESSO AO STRAVA VAI COM O
# COOKIE DE SESSAO (regra do dono). Causa raiz das "76 nao puderam ser lidas"
# da noite de 26/08: `parse_strava_activity` raspava ANONIMO -- so a listagem
# de perfil mandava cookie. MEDIDO abrindo tres das 76 pela fila
# compartilhada: HTTP **200**, 865 KB, __NEXT_DATA__ presente, pageProps com
# isLoggedIn/sessionData/authenticationData e NENHUM objeto de atividade. E' a
# casca de autenticacao servida na propria URL, sem redirecionar -- por isso a
# checagem por '/login' na URL nunca pegou, e por isso o ramo `exige_login`
# (que so olha 401/403) tambem nao. A atividade caia em "Distancia invalida".
# O cookie agora vive no THROTTLE, que ja e o portao unico de todo acesso ao
# Strava (regra 4-C): "todos usam" virou estrutura, nao disciplina, e o numero
# de requisicoes NAO muda -- mesmo GET, um cabecalho a mais; espacamento e
# backoff intactos, que e o que protege do ban. Alem disso a casca passou a
# ser reconhecida (`_e_casca_de_login`) e vira pendencia RETENTAVEL, nao
# "ilegivel": a falha e nossa, nao da atividade. processing.py, upload.py.
# Ver ERROS.md 4.31.
#
# Ultimo toque: 27/08/2026 -- INVESTIGACAO DO APAGAO DE LINKS + IMPORTACAO.
# (1) A conferencia automatica parou de assinar como humano: quem grava por
# ela sai com arquivo_origem='conferencia_automatica', nao mais
# 'link_avulso_admin' -- o dono viu 14 atividades gravadas as 03:44 da
# madrugada, sem ninguem no sistema, e nao tinha como saber pelo banco que
# fora a maquina (upload.py, conferenciaauto.py).
# (2) A FILA DE IMPORTACAO PAROU DE SUMIR NO CLIQUE: com um trabalho em
# andamento, o pedido novo era descartado em silencio -- nao virava job, nao
# entrava na fila, nao avisava. O dono mandou 5 atletas e viu um so. A
# protecao de 20/08 continua, mas agora compara o PERFIL pedido (clique duplo)
# em vez da mera existencia de outro trabalho. A tela lista a fila em ordem de
# chegada, com o nome do atleta (upload.py, importar_atleta.html).
# (3) O log passou a dizer DE QUEM e a raspagem ("Fulano (12345)" em vez de so
# o numero) e a linha da fila diz quem entrou (processing.py, upload.py).
# Tambem corrigido o cabecalho do conferenciaauto.py, que afirmava que o
# whatsapp-web.js NUNCA registra o lote de sincronizacao -- a medicao de hoje
# mostra que as vezes registra. Ver ERROS.md 4.31.
#
# Ultimo toque: 26/08/2026 (10a passada) -- P-105 fechada: a automacao
# (movimente-automacao/scripts/send_ranking.py, fora deste repositorio)
# montava o chatId de aviso pela mao a partir do campo pronto que o site
# manda; corrigido la para resolver por _jid_contato() como o ranking ja faz
# (regra 22-C). Aqui, so secretstore.py ganhou SSH_SUDO_PASSWORD -- pedido do
# dono, 26/08: e o agente quem faz reload/SSH/sudo, entao a senha fica no
# cofre em vez de ser perguntada toda vez. secretstore.py.
#
# Ultimo toque: 26/08/2026 (8a passada) -- Passo 6 do plano
# `o-ranking-de-manh-ethereal-feather.md`: o cache da tela de Conferencia
# (`confcache_*`) saiu de `temp_extract/jobs/` para uma pasta propria
# (`jobstore.CACHE_DIR` = `temp_extract/confcache/`). A pasta de trabalhos
# passa a ter so trabalho de verdade -- o ticker de retomada, de 2 em 2 min,
# parava de ler cache que nao lhe dizia respeito. `faxina_de_jobs` limpa a
# pasta nova (mesmo prazo de 2h) e varre uma vez o que sobrou da pasta velha.
# jobstore.py, upload.py, conferencia.py.
#
# Ultimo toque: 26/08/2026 (9a passada) -- Passo 8 do plano
# `o-ranking-de-manh-ethereal-feather.md`: codigo morto removido (funcao
# escrita e nunca chamada por ninguem, conferido em toda a pasta app/ antes de
# apagar). `_waha_pedir` (admin.py, WAHA-helper substituido pelo modulo
# whatsapp.py) -- `definir_nome_sessao` (whatsapp.py, campo de digitar nome de
# sessao saiu da tela em 14/08, regra 16-0-D) -- `AVISOS_STATUS` +
# `_carregar_infos_atletas` + `_aviso_ranking` (ranking.py, subsistema orfao
# inteiro) -- `generate_yearly_stats` (ranking.py, substituida por
# `_build_yearly_public_sections`, que calcula ao vivo) --
# `_get_current_month_sponsor_context` (ranking.py, quem chama usa
# `_get_sponsor_context` direto) -- `set_ranking_data` (models.py, quem grava
# usa `.json_ranking =` direto) -- `pergunta_de_telefone` (mensagens.py) --
# `telefone_para_jid` (avisos.py, e formava `@c.us` na mao, o padrao que a
# regra 22-C proibe -- ver achado novo em PENDENCIAS.md). NAO mexi em
# `mesmo_numero`/`analisar` (telefone.py) nem em `peek` (ratepacer.py): sao
# utilidades corretas e documentadas, so ainda sem chamador -- apagar
# ensinaria a reinventar a roda errada depois.
#
# Ultimo toque: 27/08/2026 (2a passada) -- botao "Conferir na proxima rodada" na
# tela do ranking: quando se descobre que a coleta falhou, esperar os 4 dias e
# esperar demais. E um PEDIDO PONTUAL — some sozinho depois de atendido, para
# ninguem deixar a conferencia diaria ligada por engano. Ganha ate de
# "desligada": botao que nao faz nada e pior que botao nenhum.
# conferenciaauto.py, ranking.py, ranking.html.

# Ultimo toque: 27/08/2026 -- CONFERENCIA AUTOMATICA (`app/conferenciaauto.py`):
# a rede contra a coleta do WhatsApp perder link. Compara o Strava de cada
# atleta que postou no mes com o ranking e traz o que faltou; roda pela rotina
# diaria, com prazo POR CIDADE e memoria (1/2/3/4 dias ou desligada, na tela do
# ranking; padrao 4). Atleta que nao quer participar sai pela caixa em
# /admin/atletas/<id>/ajuste-manual, e a saida vale so naquele mes. Achar link
# manda e-mail avisando que a coleta pode ter falhado e o que conferir no grupo
# (etapa nova em alertas._ACOES, regra 21-J). Causa: ERROS.md 4.30.
# conferenciaauto.py, ranking.py, admin.py, alertas.py, secretstore.py,
# __init__.py, ranking.html, athlete_adjustment.html.

# Ultimo toque: 26/08/2026 (7a passada) -- TETO DE FOLGA, NAO DE APERTO: no
# maximo 2 trabalhos pesados ao mesmo tempo no servidor (o numero de nucleos).
# Com uma ou duas cidades nada muda e ninguem e' recusado -- o terceiro entra na
# FILA e comeca sozinho quando abrir vaga, pelo ticker que ja passava de 2 em 2
# minutos. Pausado e na fila nao ocupam vaga, e processo morto nao segura vaga.
# A tela distingue os quatro estados (rodando, na fila, pausado, sem resposta) —
# dono morto com batimento fresco deixou de aparecer como "rodando".
# upload.py, importar_atleta.html.

# Ultimo toque: 26/08/2026 (6a passada) -- LISTA DE TRABALHOS EM ABERTO na tela
# de importacao: o que roda, de qual cidade, quem comecou, ha quanto tempo, em
# que ponto esta, e Cancelar em cada linha respeitando hierarquia (admin nao
# para o que um master comecou). Fechar a pagina nunca parou nada, mas quem
# chegava depois nao tinha como saber que havia trabalho rodando nem como parar.
# Uma varredura so da pasta (`_trabalhos_em_aberto`), e o tempo em portugues
# ("ha 2 horas e 10 minutos", nunca "130 min"). upload.py, importar_atleta.html.

# Ultimo toque: 26/08/2026 (5a passada) -- O SERVIDOR DESLIGA A NOITE, e agora o
# codigo sabe disso: `app/energia.py` le o horario de desligar do proprio
# crontab do Synology e o de ligar do ultimo boot (`/proc/stat`), entao mudar o
# agendamento no painel do DSM nao exige tocar em codigo. A importacao de atleta
# congela 15 min antes da parada -- entre um mes e outro, com o checkpoint
# gravado -- e volta sozinha quando a maquina liga. A pausa NAO gasta uma das 3
# tentativas de retomada e NAO vira "sem resposta" na tela. upload.py,
# energia.py, importar_atleta.html.

# Retoque 26/08/2026 -- ACHADO NO TESTE da parada forcada, antes de ir ao ar:
# processo morto por SIGKILL vira ZUMBI ate o pai recolher, e `os.kill(pid, 0)`
# continua achando ele. Com isso o cancelamento "nao funcionava" e, pior,
# `_maybe_resume_job` daria um trabalho morto como vivo para sempre, sem nunca
# retomar. `_processo_vivo` passa a ler o estado em /proc (Z = zumbi) e a faxina
# recolhe os filhos terminados. upload.py.

# Ultimo toque: 26/08/2026 (4a passada) -- TRABALHO LONGO SAIU DE DENTRO DO
# SITE: a importacao de atleta passa a rodar em processo proprio
# (`app/jobrunner.py`, lancado por `_lancar_job_em_processo`), com queda para a
# thread de sempre se o processo nao puder subir. Com isso: cancelar vira
# parada de fato (`_garantir_parada`: pede, espera 30s, so entao encerra),
# reciclar/recarregar o site nao mata mais job no meio, e a memoria da
# importacao volta ao sistema no fim. `MOVIMENTE_SO_JOB=1` impede o processo de
# subir um segundo vigia/ticker/rotina diaria. upload.py, jobrunner.py,
# __init__.py.

# Ultimo toque: 26/08/2026 (3a passada) -- backup e log auditados: o essencial
# deixava CLAUDE.md, scripts/ e test_dashboard.py de fora e listava dois
# arquivos que nao existem mais; o banco passa a ter copia DIARIA e o codigo
# SEMANAL, com retencao avo-pai-filho (7 diarios + 4 semanais + 6 mensais) que
# tambem passou a valer para o backup do botao (11 copias, 1,17 GB, sem
# rotacao nenhuma); o log vira arquivo que se divide sozinho (2 MB x 10) em vez
# de um unico de 10,9 MB; e botao "Fazer faxina agora" em /admin/manutencao.
# admin.py, backupauto.py, __init__.py, manutencao.html.

# Ultimo toque: 26/08/2026 (2a passada) -- faxina que nunca existiu: a fila de
# mensagens guardava 28 MB de imagem ja entregue (metade do banco, que dobrou de
# 25 para 52 MB em duas semanas) e temp_extract/jobs tinha 217 arquivos abertos
# a cada 2 min por todo mundo. Entram aliviar_imagens_entregues + limpar_antigas
# (esta existia e NUNCA era chamada) e faxina_de_jobs, na rotina diaria.
# filamensagens.py, upload.py, __init__.py.

# Ultimo toque: 26/08/2026 -- causa do 502 de 25/08: as telas de andamento
# perguntavam sem recuo e sem esperar a resposta; a de status ainda tentava
# reanimar o job a cada consulta. Recuo progressivo nas tres telas, retomada so
# no ticker, e cidade/hierarquia conferidas em ver-andamento e cancelar.
# upload.py, importar_atleta.html, upload.html, manutencao.html. Ver ERROS.md.

debug_file = '/volume1/web/movimente-se/wsgi_debug.txt'
try:
    with open(debug_file, 'w') as f:
        f.write(f"[{datetime.now()}] WSGI.PY iniciado\n")
        f.write(f"Python: {sys.version}\n")
        f.write(f"sys.path: {sys.path}\n\n")
except Exception as e:
    # Fallback if cannot write to debug_file (e.g., permissions)
    sys.stderr.write(f"WARNING: Could not write to wsgi_debug.txt: {e}\n")

# Adicionar o diretÃ³rio do projeto ao path
sys.path.insert(0, '/volume1/web/movimente-se')

# Importar a aplicaÃ§Ã£o Flask usando o factory pattern
try:
    from app import create_app
    application = create_app()

    with open(debug_file, 'a') as f:
        f.write("Flask app criado com sucesso usando create_app()!\n")
except Exception as e:
    try:
        with open(debug_file, 'a') as f:
            f.write(f"ERRO ao criar app: {str(e)}\n")
    except Exception as debug_e:
        sys.stderr.write(f"WARNING: Could not log error to debug file: {debug_e}\n")
    raise # Re-raise the exception so uWSGI reports it

# Reload: 19/08/2026 — a tela de Sessao do WhatsApp passou a mostrar o ESTADO
# real da sessao (whatsapp.estado_da_sessao, lendo instancias_waha) em vez do
# desfecho do ultimo PEDIDO de pareamento, que deixava o card verde escrito
# "Conectado" sobre sessoes ja apagadas.

# Reload: 20/08/2026 — o codigo de pareamento passa a sumir da tela quando a
# sessao conecta (e os vencidos sao apagados ao abrir o painel). Ele ficava
# guardado para sempre: em 20/08 o codigo da vespera ainda aparecia e foi lido
# como pedido de reconexao, com a sessao conectada e publicando.

# Reload: 20/08/2026 (2a passada, testado ao vivo com o dono acompanhando) —
# cancelar um job de importacao de atleta travado estava reiniciando ele em
# vez de parar (_maybe_resume_job nao olhava pedido de cancelamento pendente
# antes de retomar); prazo de deteccao de job travado caiu de 20 para 5 min
# (upload.py). Ver ERROS.md 4.15 e P-80 em PENDENCIAS.md (causa raiz do
# travamento da requisicao ao Strava ainda em aberto).
#
# Reload: 20/08/2026 (3a passada, achado ao vivo pelo dono) — A CAUSA
# PRINCIPAL de "F5 sempre volta pro mes 1": /upload/atleta respondia ao POST
# renderizando a pagina direto, sem redirecionar. F5 nessa pagina reenvia o
# formulario (o navegador avisa "confirmar reenvio" — nao e aviso deste
# app), e reenviar o POST cria um job NOVO do zero toda vez. Agora o POST
# redireciona para o GET (padrao Post/Redirect/Get) — F5 fica seguro.
# upload.py.
#
# Reload: 20/08/2026 (4a passada) — clicar "Iniciar importacao" duas vezes
# (ou um segundo clique por impaciencia) criava um SEGUNDO job competindo
# pelo mesmo Strava com o primeiro — as duas ficavam se revezando na fila
# compartilhada, nenhuma terminava (achado ao vivo: 3 jobs simultaneos do
# mesmo atleta). Agora o POST recusa comecar outro se ja ha um em andamento
# na cidade (upload.py). Tambem forcado IPv4 na resolucao de DNS do Strava
# (processing.py) — mitigacao para o travamento de rede do P-80, causa raiz
# ainda nao confirmada.

#
# Ultimo toque: 09/09/2026 00h40 -- O COFRE VOLTOU A SER LIDO. Um script de
# configuracao (Movimente-se_Rota_Cloudflare.ps1) regravou o secrets.json com
# `Set-Content -Encoding UTF8`, que no Windows PowerShell 5.1 poe BOM. O
# `json.load` recusava o arquivo e `_load_status()` devolvia ({}, False): o
# site enxergava o cofre VAZIO com as 31 chaves intactas dentro dele. Em
# 08/09, das 15h46 em diante, isso produziu NOVE sintomas de uma vez -- API em
# 503 ("token nao configurado"), botao de conectar sem porta, "Wake-on-LAN nao
# cadastrado" (estava cadastrado, com os dois MACs), modo teste com campo
# vazio, nenhum e-mail de alerta, maquina "desligada" na tela e, o pior, o
# cookie do Strava invisivel: a gravacao de atividade parou aos 15h em ponto
# (4.681 gravadas ate 14h, ZERO depois) e links validos foram carimbados como
# "nao e atividade valida, reenviar nao adianta". 971 falhas em duas horas.
# Consertado em tres frentes: (1) o arquivo regravado sem BOM, com backup;
# (2) o script passa a gravar com WriteAllText/UTF8Encoding($false); (3) AQUI,
# a rede de seguranca -- `secretstore._load_status()` le com `utf-8-sig`, que
# aceita com marca e sem marca, para o proximo script que errar nao derrubar o
# site inteiro. Medido depois: /api/health responde 200 e o log parou de
# acusar erro de cofre. app/secretstore.py.
#
# Ultimo toque: 09/09/2026 01h00 -- A PORTA QUE MENTIA. O site escolhia a porta
# de um WhatsApp novo perguntando "alguem aceita conexao aqui?" (um aperto de
# mao TCP). O `expor_waha_na_rede.ps1` cria atalho de rede (portproxy) para uma
# lista FIXA de portas, mesmo sem container atras -- e atalho vazio aceita o
# TCP e so' falha depois, ao repassar. Resultado: a 3001, que estava LIVRE, era
# dada como ocupada, e o pedido do suplente nasceu na 3002 -- um segundo WAHA
# da mesma cidade, contra a regra de um container por cidade. Medido em
# 09/09 00h50: 3000/3001/3002 aceitavam TCP, e so' a 3000 tinha WAHA.
# Agora `maquina.porta_tem_waha()` pergunta em HTTP: 200/401/403 e' o WAHA
# (aberto ou pedindo chave); conexao cortada e' porta LIVRE; "nao sei" (None)
# continua valendo como ocupada, que e' a guarda contra o zumbi da 3001.
# Provado depois do conserto: a escolha devolve 3001, nao 3002.
# app/maquina.py, app/whatsapp.py (proxima_porta_livre).
#
# Ultimo toque: 09/09/2026 01h10 -- UM CONTAINER POR CIDADE, DE VERDADE. O
# pedido de pareamento procurava a instancia DO PAPEL
# (`instancia_do_papel(cidade, papel)`). No desenho atual nao existe container
# "de suplente": e' um WAHA por cidade, com as duas sessoes dentro dele, e o
# relato da maquina traz UMA linha, com papel `principal`. Pedir o suplente nao
# achava nada, caia em `proxima_porta_livre()` e mandava criar um SEGUNDO WAHA
# da mesma cidade -- o container da 3002 de 08/09, removido na mao depois.
# Agora, quando o papel nao tem instancia propria, `container_da_cidade()` diz
# onde a cidade ja mora e a sessao nova nasce LA DENTRO. Aproveita so' o
# endereco (container e porta): o nome da sessao continua vindo do papel de
# quem pediu, senao o suplente herdaria a sessao do principal e a gravaria no
# cofre -- defeito corrigido em 14/08 que nao pode voltar por esta porta.
# Provado contra o banco: pedindo o suplente, a consulta devolve auto-waha-pls
# na 3000, e nao uma porta nova. app/whatsapp.py.
#
# Ultimo toque: 09/09/2026 01h50 -- DUAS CORRECOES DA NOITE DO COFRE.
# (1) OVO E GALINHA DO PAREAMENTO: `registrar_instancias.py` nao relata
# container SEM sessao, e a identidade de `instancias_waha` e' a sessao. Logo,
# no exato momento de parear -- quando nao ha sessao -- o site ficava sem saber
# a porta e recusava com "ainda nao sei em que porta esta cidade roda". So' nao
# travou de vez porque sobrou o registro de uma passada anterior. Agora a
# decisao tem QUATRO degraus: instancia do papel > container da cidade >
# PORTA DO ENDERECO CADASTRADO NO COFRE (funciona sem sessao nenhuma) >
# proxima porta livre. Nada e' inventado -- o terceiro degrau le o mesmo
# `waha_base_url` que o site usa para publicar.
# (2) LINK BOM CARIMBADO COMO DEFEITO: em `parse_strava_activity` o teste de
# "falta sessao" (retentavel) estava DEPOIS do que carimba `formato`
# (permanente), e nunca era alcancado. Sem cookie o Strava serve a casca de
# login, nada e' extraido, e o link virava "nao e atividade valida, reenviar
# nao adianta". Foi o que devolveu "2 pendencias permanentes" em 08/09 para
# dois links bons -- Dani Exemplo (5,04 km) e Edu Exemplo (4,89 km) --, ambos
# lidos de primeira depois que o cofre foi consertado. O teste subiu de lugar.
# Tambem: a mensagem do embed dizia "404 = nao publica" ate para 403, que nao
# fala da atividade e sim do servico de embed recusando.
# app/whatsapp.py, app/blueprints/processing.py.
#
# Ultimo toque: 09/09/2026 02h05 -- A TABELA DE MAIS VELOZES DEIXOU DE EXPOR
# GENTE. A pagina inicial mostrava uma moradora com 109,6 km/h em corrida.
# NAO era fraude dela: uma atividade tinha 1,18 km em 4 SEGUNDOS de tempo em
# movimento (o GPS pega um primeiro ponto longe antes de ficar preciso). Tres
# defeitos somados: (a) a tabela usava o escopo "somente corrida", que em
# atividade antiga so' aceita ritmo abaixo de 7 min/km -- o filtro escolhia
# justamente os fragmentos de GPS e descartava as caminhadas de verdade
# (medido na mesma pessoa: 34 atividades/56,3 km/h contra 211/13,5 km/h);
# (b) media aritmetica simples, indefesa a um unico outlier; (c) 320
# atividades JA FLAGRADAS ainda contavam, porque a coluna
# `velocidade_confiavel` nasceu em 28/08 com default True e nunca houve
# retroacao.
# Agora: corrida+caminhada juntas, MEDIANA no lugar da media, corte por desvio
# absoluto mediano (MAD -- e nao desvio padrao, que o proprio outlier infla),
# faixa de sanidade por modalidade (1-25 km/h a pe, 5-50 de bike, o que tira
# tambem as quase-paradas), minimo de 5 atividades LIMPAS, top 5, nome
# corrigido do cadastro em vez do nome do Strava, e km/h + ritmo lado a lado.
# Backfill: 267 atividades com flag aberta/rejeitada saiam da conta -- e NENHUM
# quilometro mudou (regra 14: total de setembro seguiu 971,4 km).
# Resultado medido: corrida passa de 109,6 para 12,4 km/h no topo.
# app/consolidado.py, app/templates/publico/home.html.
#
# Ultimo toque: 09/09/2026 16h35 -- LOGIN NAO DEPENDE MAIS DO COFRE ESTAR BOM.
# Ate aqui "o cofre esta vazio" e "o cofre nao pode ser lido" davam no mesmo:
# os dois caiam na SECRET_KEY legada. Em 08/09 o cofre ficou ilegivel, o site
# adotou a legada e, quando o cofre foi consertado, voltou para a chave real --
# INVALIDANDO O LOGIN DE TODOS em silencio. No dia seguinte o dono clicou em
# "Publicar agora" e nada aconteceu, porque estava deslogado sem saber.
# Agora `secretstore.load_status()` (nova, publica) distingue os dois casos, e
# cofre ILEGIVEL nao troca a chave: mantem a legada e grita no log dizendo como
# corrigir. Cofre VAZIO de verdade continua gerando chave nova, como antes.
# Testado nos quatro cenarios (bom, com BOM, corrompido, vazio).
# app/__init__.py, app/secretstore.py.
#
# Ultimo toque: 09/09/2026 16h55 -- PUBLICACAO MANUAL PASSA A CONTAR.
# Ate aqui so' a automacao deixava rastro: o ranking copiado na tela e colado
# no grupo era invisivel para o sistema. Em 09/09 o dono publicou a mao a
# 01:37 e, as 09:53, o aviso do Windows o acordou dizendo que "o ranking das
# 05:30 nao saiu". Agora o botao "Copiar para WhatsApp" chama
# `/ranking/marcar-copiado`, que registra a INTENCAO no cofre da cidade, e o
# `/api/automacao/estado` devolve isso em campo PROPRIO (`copiado_em`,
# `copiado_por`), sem somar ao `ranking_enviado_em` da automacao -- copiar e'
# intencao, publicar e' fato, e confundir os dois trocaria um alarme falso por
# um silencio falso (regra 10). Quem confirma de verdade e' a rodada seguinte,
# procurando o cabecalho do ranking na conversa do grupo.
# app/blueprints/ranking.py, app/blueprints/api.py,
# app/templates/ranking/ranking.html.
#
# Reload: 09/09/2026 17h00 -- conserto do toque anterior. Eu havia inserido a
# funcao auxiliar `_copiado_manual` LOGO ABAIXO do decorador de rota, entre o
# `@api_bp.route(...)` e o `def automacao_estado()`. O decorador passou a
# registrar a funcao errada, o blueprint nao montou e o site inteiro foi para
# 500 ("cannot import name 'api_bp'"). Funcao auxiliar entra ANTES do
# decorador, nunca entre ele e a def que ele decora. api.py.
#
# Ultimo toque: 09/09/2026 17h10 -- BOTAO "AUTOMACAO DE PE", COM PRAZO.
# Bloco PROPRIO no painel, separado de "manter a maquina ligada" a pedido do
# dono: aquele mantem o COMPUTADOR aceso, este mantem os CONTAINERS DA
# APLICACAO de pe (30min / 1h / 2h / 3h / ate mandar parar), e tem botao de
# parada limpa imediata. Outro servico da maquina NAO entra na conta -- ele fica de
# pe enquanto a maquina estiver ligada, porque alguem pode estar usando
# enquanto trabalha, e isso nao tem hora marcada.
# O prazo mora em `automacao_estado` (colunas novas `containers_ate` e
# `containers_por`), NAO no cofre: cofre e' para credencial que nao pode ir ao
# Git, e vinha virando lixeira de estado (decisao do dono, 09/09).
# Sem prazo, subir a automacao para conferir uma coisa a deixava de pe o resto
# do dia comendo memoria -- foi o que aconteceu na madrugada de hoje.
# app/blueprints/api.py, app/blueprints/admin.py,
# app/templates/admin/dashboard.html.
#
# Ultimo toque: 09/09/2026 17h20 -- OUTRO SERVICO SEMPRE, AUTOMACAO SOB DEMANDA.
# Corrige o que EU mesmo fiz na madrugada de hoje: a ancora do Guardiao passou
# a valer o dia inteiro e, com ela, TUDO ficava de pe -- inclusive o WAHA e o
# Postgres, que fora do horario do ranking so' comem memoria. Decisao do dono:
# o outro servico nao tem horario (alguem pode estar usando enquanto
# trabalha) e fica de pe enquanto a maquina estiver ligada; a automacao sobe
# nas janelas de ranking ou quando alguem pedir no painel, com prazo.
# Medido antes de separar: o outro servico NAO depende do Postgres (sem depends_on
# no compose, dados em /data proprio), entao da' para parar o banco sem
# encostar nele. E na duvida NAO DERRUBA: se o site nao responder, o
# Guardiao deixa tudo como esta -- gastar memoria e' melhor que derrubar quem
# esta usando. Movimente-se_Guardiao.ps1.
#
# Ultimo toque: 09/09/2026 18h30 -- DESLIGAR VENCE AS TRAVAS DO PAINEL.
# Havia duas alavancas que impedem a maquina de parar, e em telas diferentes:
# `manter_ligada` (mantem o COMPUTADOR aceso) e `containers_ate` (mantem a
# AUTOMACAO de pe). Quem clicava em "Desligar a maquina" tinha de lembrar de
# desarmar as duas antes, ou o pedido ficava preso sem ninguem entender por
# que. Agora desligar desarma as duas e DIZ que fez.
# Nao atropela a parada limpa: o pedido continua sendo entregue pela propria
# maquina, que para os programas na ordem e confere a copia da sessao antes
# (regra 17) -- desarmar so' tira o impedimento, nao desliga na marra.
# Testado sobre COPIA do banco, com producao intacta.
# app/blueprints/admin.py.
#
# Ultimo toque: 09/09/2026 18h45 -- TETO DE VELOCIDADE POR DISTANCIA, e a
# funcao `recordistas()` (a segunda tabela, ainda NAO publicada).
# Um teto unico trata 5 km e 15 km igual: foi por isso que 16,47 km a 3:28/km
# passou como legitima, e que a tabela de recorde elegeu 23,4 km/h em 10,7 km
# -- ACIMA do recorde mundial dos 10 km. O corte agora e' o RECORDE MUNDIAL de
# cada distancia (12:35 Cheptegei 5k, 26:24 Kipruto 10k, 57:30 Kejelcha meia,
# 1:59:30 Sawe maratona): nao e' opiniao, e' limite fisico publico. Ninguem num
# grupo de moradores corre mais que o campeao do mundo -- passou disso, o dado
# esta errado.
# E' TETO ABSOLUTO, nao criterio de suspeita: o teto realista do amador fica na
# tela de parametros, por cidade e por atleta, e vence este quando existir.
# A tabela de recordes NAO foi ligada na home: com o teto atual ela ainda
# mostraria 22,8 km/h em 1,71 km (distancia curta, onde o erro de GPS domina),
# e publicar isso exporia a pessoa. Aguarda a decisao do dono sobre distancia
# minima. app/consolidado.py.
#
# Ultimo toque: 09/09/2026 19h00 -- A HOME SE ATUALIZA QUANDO O KM MUDA, e a
# tabela de RECORDES entrou no ar.
# (1) A marca que decide refazer a foto era so' `MAX(Activity.id)`, que muda
# apenas quando ENTRA atividade -- nao quando uma sai por revisao, nem quando
# alguem corrige a quilometragem, nem quando o antitrapaca marca 267 de uma
# vez (foi o que aconteceu em 09/09 e a home seguiu com numero velho). Agora a
# marca junta maior id + contagem + soma dos km: continua UMA consulta, so' roda
# quando alguem abre a pagina. Provado em copia do banco: corrigir 1 km muda a
# marca nova e nao mudava a antiga.
# (2) `recordistas()` no ar, com DISTANCIA MINIMA DE 2,2 km (escolha do dono):
# nao e' o calculo que erra -- a velocidade ja sai do `moving_time` em segundos
# --, e' o tempo do Strava que inclui os primeiros metros com GPS impreciso, e
# em atividade curta esse trecho e' uma fatia grande do total. A velocidade na
# tabela e' CLICAVEL e abre a atividade no Strava em aba nova: nenhum filtro
# automatico substitui um par de olhos quando se diz publicamente quem foi o
# mais rapido. app/consolidado.py, app/templates/publico/home.html.
#
# Ultimo toque: 09/09/2026 19h15 -- RECORDE MUNDIAL COMO REGUA (pedido do dono).
# "17,2 km/h" nao diz nada a quem nao corre. Agora cada linha da tabela de
# recordes traz "X% do recorde mundial", escolhido pela DISTANCIA daquela
# atividade -- comparar 10 km com o recorde dos 5 km seria trapaca na conta.
# Corrida: World Athletics (5 km Cheptegei 12:35; 10 km Kipruto 26:24; meia
# Kejelcha 57:30; maratona Sawe 1:59:30). Ciclismo: recorde da hora da UCI,
# Ganna 56,792 km (2022) -- em VELODROMO, com bicicleta de pista, e o texto da
# tela diz isso para a comparacao nao virar humilhacao de ninguem.
# EFEITO COLATERAL UTIL, medido: a porcentagem acusa sozinha o que o teto
# deixou passar -- a primeira colocada de corrida aparece com 97% do recorde
# mundial da maratona, que nenhum amador faz. Vira triagem para o admin
# conferir. app/consolidado.py, app/templates/publico/home.html.
#
# Ultimo toque: 09/09/2026 19h40 -- TEXTOS DA PAGINA INICIAL EDITAVEIS PELA
# TELA (pedido do dono), em /admin/textos-da-home.
# Trocar uma frase da porta de entrada exigia editar template EM PRODUCAO, e
# isso derruba o site por uma virgula. Agora o dono ajusta sozinho, com previa
# ao vivo ao lado do editor (sem ver o resultado, marcacao escrita a mao e'
# adivinhacao) e botoes de negrito/italico/sublinhado/MAIUSCULA.
# So' formatacao: cor e tamanho NAO entram -- quem escolhe e' o site, senao a
# pagina vira colcha de retalhos. E o que chega da tela e' limpo tag por tag:
# testado que <script>, <a href>, onclick e style viram TEXTO, nao HTML.
# Trecho vazio volta ao texto de fabrica, que segue versionado no Git.
# app/textossite.py (novo), app/blueprints/admin.py, app/indice.py,
# app/__init__.py (helper `texto()`), app/templates/admin/textos_site.html,
# app/templates/publico/home.html.
#
# Reload: 09/09/2026 19h50 -- frase da marca alinhada ao CLAUDE.md. O titulo da
# home e o cabecalho dos relatorios diziam "mover o corpo para uma mente mais
# saudavel"; a frase oficial e' "mover o corpo para mover a mente", e o
# vocabulario do projeto e' NAO-CLINICO de proposito (bem-estar, disposicao --
# nunca algo que soe a diagnostico). Achado ao ligar os textos editaveis.
# publico/home.html, admin/_relatorio_base.html.
#
# Ultimo toque: 09/09/2026 20h05 -- SEXO DO ATLETA, MARCADO A MAO NA PROPRIA
# TABELA (/admin/atletas), e contagem AGREGADA na home.
# O Strava NAO publica esse dado: a documentacao oficial diz que a escolha de
# genero "will not appear on your profile page", e a medicao confirmou por dois
# caminhos -- com cookie vem o genero da CONTA LOGADA (a mesma armadilha que
# escreveu o nome errado em 62 atletas, 4.39); sem cookie nao vem nada, embora
# nome e cidade venham. Nao ha o que raspar.
# Entao: seletor na LISTA de atletas, que salva sozinho, sem abrir a tela de
# ajustar km e sem recarregar. Coluna so' no DESKTOP; quem esta sem preencher
# fica com a borda destacada, para o dono resolver batendo o olho.
# ⚠️ NUNCA se expoe o sexo de ninguem em pagina publica: a home mostra so' a
# porcentagem do grupo, e so' quando ha 30% ou mais preenchido. Porcentagem, e
# nao contagem, porque o dado e' manual e sempre havera quem falte -- somar
# duas partes que nao dao o todo faria a pagina parecer errada.
# app/models.py, app/blueprints/admin.py, app/consolidado.py,
# app/templates/admin/lista_atletas.html, app/templates/publico/home.html.
#
# Ultimo toque: 09/09/2026 20h20 -- AVISO DE ATLETA NOVO (pedido do dono).
# Atleta entra sozinho -- basta alguem postar um link no grupo. Mas o sexo o
# Strava nao da, e sem ele a home nao consegue dizer quantas atividades sao de
# homens e quantas de mulheres. Sem aviso, isso so' e' descoberto meses depois.
# Um e-mail por dia, com TODOS os novos numa lista e o link direto para a
# pagina de cada um -- e nenhum e-mail quando nao ha ninguem para cadastrar.
# POR QUE PERIODICO E NAO A CADA ATLETA: ele nasce em quatro pontos do codigo
# (conferencia, anticheat, importacao, upload); disparar em cada um mandaria
# dezenas de e-mails num dia de upload em massa.
# Quem recebe sai de `alertas.destinatarios`, que ja e' a lista unica -- criar
# uma segunda faria alguem sair do projeto e seguir recebendo por meses.
# app/atletanovo.py (novo), app/__init__.py.
#
# Reload: 09/09/2026 20h25 -- removida a `fetch_athlete_sexo` que eu havia
# deixado orfa. Ela nasceu para raspar o sexo do Strava, a medicao provou que
# esse dado NAO existe publicamente (a doc oficial diz que a escolha de genero
# "will not appear on your profile page"), e o caminho virou o preenchimento a
# mao. Manter 72 linhas que ninguem chama e' divida: o proximo a ler acharia
# que ha uma raspagem funcionando e tentaria liga-la de novo.
# O que aprendemos fica no ERROS.md e nos carimbos acima, que e' onde se
# procura -- nao em codigo morto. app/blueprints/processing.py.
#
# Ultimo toque: 09/09/2026 21h05 -- CONSERTO DO SEXO QUE NAO SALVAVA + TELA DE
# VELOCIDADE PLAUSIVEL.
# (1) O DEFEITO, achado ao vivo pelo dono: o seletor de sexo respondia "nao
# consegui salvar" a cada clique. Meu JS buscava `input[name=csrf_token]` no
# DOM, e a lista de atletas NAO tem formulario com esse campo -- as outras
# funcoes da mesma tela sempre usaram `{{ csrf_token() }}` do Jinja, e eu nao
# segui o padrao que estava na minha frente. O mesmo defeito estava no botao
# "Copiar ranking" que eu havia feito antes: os dois corrigidos. No
# upload.html o padrao do DOM continua, e continua CERTO -- lah existe
# `form.hidden_tag()` gerando o campo.
# (2) O sexo tambem faltava na tela de ajuste do atleta (o dono procurou e nao
# achou): bloco novo ao lado do tipo de bicicleta, e a rota agora responde aos
# DOIS chamadores -- JSON para o `fetch` da lista, redirect com mensagem para o
# formulario.
# (3) TELA "VELOCIDADE PLAUSIVEL" (/admin/parametros-velocidade): teto por
# modalidade e por faixa de distancia (ate 5, 5-10, 10-15, acima de 15 km),
# com PREVIA de quantas atividades cada valor deixa de fora -- e' o que
# transforma "17 km/h parece razoavel" em decisao informada. Medido: a
# sugestao inicial tira 1,0% das corridas e 0,9% do ciclismo. O recorde
# mundial segue como teto absoluto: nenhum ajuste passa dele.
# app/parametrosvel.py (novo), app/blueprints/admin.py, app/indice.py,
# templates: parametros_velocidade.html (novo), lista_atletas.html,
# athlete_adjustment.html, ranking/ranking.html.
#
# Ultimo toque: 12/09/2026 00h26 -- CLIQUE DEIXA RASTRO, E /login NAO DA MAIS 404.
# (1) "Manter a automacao de pe" mudava o estado (containers_ate, containers_por)
# e NAO escrevia uma linha de log. Medido nesta madrugada: o dono clicou, a API
# passou a dizer "faltam 118 minuto(s)" e o log do site, no mesmo minuto, so'
# tinha as linhas do vigia. Sem esse rastro nao da' para separar "o Guardiao
# subiu sozinho" de "o dono pediu" -- e foi exatamente o que faltou na auditoria
# de 10/09, quando a subida das 18:08 (que terminou com o WhatsApp desvinculado
# as 18:09:49) ficou sem dono conhecido. Agora sai uma linha DONO com quem pediu
# e ate quando.
# (2) `/login` respondia 404: o login mora em `/auth/login` (prefixo do
# blueprint). Aconteceu duas vezes em setembro, uma delas com o dono tentando
# entrar pelo celular depois de o navegador esquecer o login -- o login so' fica
# guardado se "Lembrar-me" estiver marcado. Agora `/login` desvia, levando o
# `next` junto. Conferido no ar: /login 404 -> agora desvia; /auth/login 200;
# POST no botao sem login continua 302 para o login, sem executar nada.
# app/blueprints/admin.py, app/__init__.py.

# Ultimo toque: 13/09/2026 18h25 -- LIMPAR SESSAO SEM PRECISAR SABER O NOME DELA.
# A tela exigia DIGITAR o nome da sessao (`principal-pls`) para apagar. Quem
# clica nao tem como saber esse nome: ele vive dentro do container, e em outra
# cidade sera outro. Hoje as 18:06 o dono travou no proprio botao por isso, e
# acabou clicando no "Limpar" vizinho -- que so apaga o REGISTRO do pedido e nao
# desconecta nada (medido no log do site: "Limpei 1 registro(s) antigo(s) de
# pareamento", 18:07:19). Trava que impede o dono de usar o sistema nao protege
# nada. Agora a confirmacao e uma caixinha ("sim, apagar esta sessao e tudo o
# que e dela"), obrigatoria, mais a caixinha de ciencia quando a sessao esta
# conectada: continua exigindo ato deliberado, sem exigir informacao que so o
# container tem.
# app/blueprints/admin.py, app/templates/admin/whatsapp_sessao.html.

# Ultimo toque: 14/09/2026 22h46 -- A TELA NAO DIZ MAIS "LIGANDO O COMPUTADOR" COM ELE LIGADO.
# O laco que atende o pareamento pelo site (`whatsapp._insistir`) perguntava so
# pela porta do WAHA; com os containers parados fora da janela, concluia
# "desligado", mandava Wake-on-LAN e pintava "Ligando o computador". Em 13/09
# 19:00:17 a maquina escreveu "pedindo o codigo" e 60 ms depois o site voltou a
# tela para "ligando". Agora: carimbo do Windows fresco = maquina ligada (sem
# WoL, etapa "subindo"), e a etapa nao anda para tras. app/whatsapp.py.

# Ultimo toque: 14/09/2026 23h05 -- PEDIDO ENCERRADO NAO GIRA MAIS BOLINHA.
# O pedido 51 falhou em 13/09 19:01 com a etapa guardada em "ligando", e a tela
# ficou 27 horas mostrando "Ligando o computador" com a bolinha rodando, sem
# processo nenhum por tras (medido no banco: situacao=falhou, etapa=ligando).
# Agora o passo de pedido encerrado vira "parou aqui", sem bolinha, com a hora.
# app/whatsapp.py, app/templates/admin/whatsapp_sessao.html.

# Ultimo toque: 15/09/2026 -- "A INSTANCIA RESPONDEU" SO QUANDO E O WHATSAPP.
# `whatsapp._maquina_responde` usava `porta_responde` (aperto de mao TCP); a
# ponte do Windows aceita a 3000 com o container parado. Pedidos 52 e 53
# (14/09) disseram "a instancia respondeu" sem container nenhum. Agora usa
# `maquina.porta_tem_waha`. app/whatsapp.py.

# Ultimo toque: 15/09/2026 -- LIMPAR A SESSAO NAO FAZ O SITE ESQUECER A INSTANCIA.
# `apagar_sessao` fazia DELETE da linha que tambem guarda host/porta/chave; o
# Conectar seguinte ficava sem saber onde a cidade mora e a tela voltava a
# "Ligando" (14/09 23:26). Agora a linha fica com status SEM_SESSAO, e o
# atendimento cai no `container_da_cidade` quando nao ha linha do papel.
# app/whatsapp.py.

# Ultimo toque: 15/09/2026 -- SESSAO CONECTADA PELO SITE ENTRA NO REGISTRO NA HORA.
# O suplente conectou as 14:27:02 e a tela ficou ate 14:35:25 em "Sem noticia da
# maquina", sem o botao de limpar, esperando o relato da Fila. Agora
# `_esperar_conectar` grava a sessao (WORKING/CONNECTED) em `instancias_waha`
# no instante da confirmacao, buscando pela sessao. app/whatsapp.py.

# Ultimo toque: 15/09/2026 -- SEM LINHA NO REGISTRO, A TELA PERGUNTA AO VIVO.
# `estado_da_sessao` dizia "Sem noticia da maquina" sem perguntar ao WhatsApp
# quando a sessao nao tinha linha no registro. Agora pergunta ao container da
# cidade (`container_da_cidade`) antes de concluir. app/whatsapp.py.

# Ultimo toque: 15/09/2026 -- SESSAO NAO APARECE "CONECTADA" COM O WHATSAPP PARADO.
# Containers parados as 14:40; o suplente seguiu "Conectado — pode publicar" ate
# 15:15 porque, sem resposta ao vivo, a tela voltava ao relato de 40 min. Agora,
# se `porta_tem_waha` diz que nao ha WhatsApp na porta, o quadro mostra
# "WhatsApp parado". app/whatsapp.py.

# Ultimo toque: 15/09/2026 -- CARD "AUTOMACAO" DIZ PARADA OU DE PE.
# O titulo era "Automacao de pe" nos dois estados. templates/admin/dashboard.html.

# Ultimo toque: 15/09/2026 -- LINK DO STRAVA SEM KM AVISA, E NAO RETENTA PARA SEMPRE.
# Alguem postou o link curto do PROPRIO PERFIL achando que postava a atividade;
# o sistema chamou de "shortlink nao resolvido" (retentavel para sempre) e ficou
# calado -- os km dele nao entraram no ranking e ninguem soube. Agora qualquer
# endereco do Strava que nao seja atividade (perfil, clube, segmento) vira
# pendencia PERMANENTE (etapa `nao_atividade`), a tela mostra quem postou e
# quando, e toda pendencia sem volta (essa, atividade apagada, privada, sem
# distancia) vira UM e-mail curto ao admin: link, motivo, quem, quando.
# app/linksemkm.py, blueprints/processing.py, blueprints/upload.py,
# templates/upload/pending_details.html.

# Ultimo toque: 16/09/2026 -- ATUALIZACAO DE PERFIS: UMA VEZ POR MES, NAO 4x POR HORA.
# Medido no log de 15/09: a cidade Minaçu abriu um processo novo de
# `atualizar_perfis` a cada 15 minutos, o dia inteiro. Causa: quando nao ha
# nada a atualizar o job saia SEM carimbar a conclusao, e sem carimbo
# `perfilauto.vencida()` respondia "sim" para sempre. Agora o ramo carimba, e a
# frequencia passou a ser por MES DE CALENDARIO (primeira passada do mes) em
# vez de 7/30 dias corridos. app/perfilauto.py, blueprints/admin.py.

# Ultimo toque: 16/09/2026 -- RASTRO DE TODO ACESSO AO SITE.
# Nao havia como saber se um clique existiu: o log so registra o que a funcao
# faz, e clique recusado antes dela (sessao expirada -> 302 para o login) nao
# deixava linha nenhuma. Medido em 15/09 23:47:56, e de novo em 16/09 00:16
# ja com o conserto. Nem o nginx nem o uWSGI guardam acesso nesta maquina.
# Agora cada requisicao escreve uma linha em logs/acesso-AAAA-MM.log: quem,
# de onde, o que pediu, o que recebeu, quanto demorou e, no redirect, para
# onde foi. app/acesso.py, app/__init__.py.

# Ultimo toque: 17/09/2026 -- SESSAO E AUTOMACAO SAO DUAS CAMADAS NA TELA.
# Revertido o "WhatsApp parado" de 15/09, que TROCAVA o estado da sessao
# quando o container nao respondia e apagava o vinculo que continuava valido.
# Agora o vinculo fica como e', e ao lado vem `automacao_parada` ("a automacao
# nao esta rodando agora"); a frase "de pe e pode publicar" so aparece quando
# esta de pe mesmo. O sinal e' injetado num ponto so (`estado_da_sessao` chama
# `_calcular_estado_da_sessao`) e viaja no JSON para o redesenho de 3 em 3 s.
# Nome `_calcular_...` de proposito: ja existe `_estado_da_sessao(base, cab,
# nome)` no modulo, e reusar o nome sobrescreveu uma pela outra (corrigido
# na mesma sessao). Tambem: rota /api/rodada/pendente (so leitura) para o
# pedido imediato. app/whatsapp.py, blueprints/admin.py, blueprints/api.py,
# templates/admin/_estado_sessao.html, templates/admin/whatsapp_sessao.html.

# Ultimo toque: 17/09/2026 -- O PEDIDO DE RODADA PROTEGE OS CONTAINERS.
# Medido as 10:45-10:51: pedido atendido, e o Guardiao derrubou os containers
# no meio da coleta ("fora do horario e sem pedido em aberto"), porque a
# protecao de 30 min so existia na rota do botao. Agora `rodada.pedir` grava o
# prazo, e todo pedido passa por ele. app/rodada.py.

# Ultimo toque: 17/09/2026 -- LOG CANONICO SEMANAL, LADO DO SITE (item D1).
# Um formato so nas tres camadas (Windows, WSL, site):
#   AAAA-MM-DD HH:MM:SS | MAQUINA | CAMADA | ORIGEM | NIVEL | mensagem
# num arquivo por semana que vira na segunda 05:00. Aqui: logs/canonico_*.log
# com AVISO/ERRO do site, os INFO que contam uma acao (e-mail, rodada, jobs)
# e os cliques de quem esta logado. app/logcanon.py, app/acesso.py,
# app/__init__.py.
# Ultimo toque: 17/09/2026 -- logcanon: o filtro de INFO aceita tambem o nome
# do logger (app.rodada -> rodada), nao so o arquivo que chamou. app/logcanon.py.

# Ultimo toque: 17/09/2026 -- SELETOR DO "MANTER DE PE" MOSTRA O QUE VALE (D1-B).
# "1 hora" era fixo como selecionado. Com "ate eu mandar parar" ativo, um novo
# clique rebaixava o prazo para 1 hora sem avisar e o Guardiao derrubava depois.
# Medido: as 10:49 de 17/09 o prazo gravado era 0 (zerado por "parar" em 15/09
# 14:59); o "ate mandarem parar" foi clicado as 10:51:15, depois da derrubada.
# templates/admin/dashboard.html.

# Ultimo toque: 17/09/2026 -- A CONSULTA DE MINUTO EM MINUTO NAO GRAVA NO HD DO NAS.
# Medido: 7 ms por consulta, nenhuma gravacao no cofre; a unica escrita era a
# linha no log de acesso (~130 KB/dia). Agora so' entra se a rota falhar.
# app/acesso.py.

# Ultimo toque: 17/09/2026 -- AVISO DE LINK SEM KM: UMA VEZ POR LINK (A11).
# O mesmo link de perfil gerou 4 e-mails em 15-17/09 (processed_files 189-192):
# a "varredura de seguranca" rele ate 48 h quando a rodada anterior nao
# publica, e o aviso nao tinha memoria. Agora `link_sem_km_avisado`
# (cidade, url) guarda o que ja foi avisado; a marca so e gravada depois de o
# e-mail sair. app/linksemkm.py.

# Ultimo toque: 17/09/2026 -- BOTAO "EXECUTAR PEDIDOS AGORA" (A12), lado do site.
# Manter de pe e Conectar-com-container-parado esperam a maquina (medido:
# 10:51:15 -> 10:58:03; 23:26:57 -> 23:35:03). O botao grava um pedido
# (`rodada.pedir_execucao`, chave `execucao_pedida`); `/api/rodada/pendente`
# o devolve (so leitura) e `/api/execucao/atendida` marca (uma gravacao por
# clique). Nomes escolhidos depois do Grep: `fila_pendente` ja existia em
# whatsapp.py (regra 24). app/rodada.py, app/secretstore.py,
# blueprints/api.py, blueprints/admin.py, templates/admin/dashboard.html.

# Ultimo toque: 17/09/2026 -- TRAVA NA GRAVACAO DO COFRE (D5, lado do site).
# Ler-mudar-gravar sem trava: duas gravacoes no mesmo instante liam a mesma
# versao e a ultima apagava a mudanca da primeira. Medido no NAS com 8
# processos gravando juntos: SEM trava 35 de 48 gravacoes perdidas (e
# FileNotFoundError no arquivo temporario); COM trava, nenhuma perda.
# `flock` no `instance/secrets.lock`; onde nao houver, o comportamento e o de
# antes. app/secretstore.py (set_value e delete).

# Ultimo toque: 17/09/2026 -- OS BOTOES NAO ESPERAM MAIS A PASSADA DA MAQUINA.
# Manter de pe/Parar, Conectar, Limpar a sessao (quando o WhatsApp nao
# responde), Desligar a maquina e as mensagens que sobram na fila passaram a
# deixar um pedido de execucao (`_adiantar_execucao` -> `rodada.pedir_execucao`).
# A consulta de minuto em minuto do Dell chama Guardiao e fila na hora, um por
# vez. Nenhuma funcao mudou o que faz. blueprints/admin.py.

# Ultimo toque: 17/09/2026 -- ATUALIZACAO DE PERFIS: SO CIDADE ATIVA E COM STRAVA.
# Minacu (inativa, sem cookie) abria `atualizar_perfis` 4x por hora o dia todo:
# o relogio percorria toda linha de Config e o job saia em "Sessao do Strava
# nao configurada" ANTES de marcar a passada (medido no andamento do job,
# temp_extract/jobs). Agora so `consolidado.cidades_ativas()` e so com cookie.
# app/__init__.py, blueprints/admin.py (tocar_atualizacao_de_perfis).

# Ultimo toque: 17/09/2026 -- A PARADA AGENDADA RESPEITA "ATE EU MANDAR PARAR".
# /api/maquina/pode-dormir responde manter_ligada=True quando os containers estao
# com prazo -1. Em 17/09 16:00 a parada ignorou o -1, desceu tudo, e a subida
# das 16:09 quebrou a principal. "Desligar a maquina" do dono segue valendo (o
# botao zera o prazo antes). blueprints/api.py.

# Ultimo toque: 17/09/2026 -- TESTEMUNHA DO ACORDAR PELO WI-FI (so log).
# maquina.estado registra a hora de cada pergunta a porta do Dell que ficou sem
# resposta, para comparar com a hora em que o Windows acordou sozinho. app/maquina.py.

# Ultimo toque: 17/09/2026 -- perfilauto._ler diz por que nao leu (antes calava).
# Palmeiropolis concluiu 23:46 e recomecou 23:51: leitura falhando em silencio
# vira "nunca rodou". Diagnostico na proxima passada. app/perfilauto.py.
# Ultimo toque: 17/09/2026 -- perfilauto._ler: so FileNotFoundError e "nunca rodou". app/perfilauto.py.

# Ultimo toque: 18/09/2026 -- PERFIS: NOME DO ARQUIVO DE ESTADO SEM ACENTO.
# O uWSGI le nomes de arquivo como ASCII (sem LANG); "Palmeirópolis_nome.json"
# nao abria no site e a atualizacao recomecava a cada 15 min. Replica fiel
# provou. Agora "Palmeiropolis_nome.json". app/perfilauto.py.

# Ultimo toque: 18/09/2026 16h30 -- LIMPAR A SESSAO COM O WHATSAPP PARADO (A2/E2).
# O botao aparece mesmo com a maquina parada; o pedido fica guardado no cofre
# e se cumpre quando a maquina conta que subiu (/api/instancias).
# app/whatsapp.py (apagar_pendente, cumprir_apagar_pendente), app/blueprints/api.py,
# admin/whatsapp_sessao.html.

# Ultimo toque: 18/09/2026 16h45 -- TOPO DA TELA SESSAO WHATSAPP POR CAMADA (A3).
# Windows (porta 135) / WSL (resposta HTTP) / container de cada cidade, ao vivo,
# sem sessao no topo. app/maquina.py (camadas, WSL_IP), app/blueprints/admin.py,
# app/blueprints/api.py (wsl_ip), admin/whatsapp_sessao.html.

# Ultimo toque: 18/09/2026 16h55 -- CONECTAR SEGURA OS CONTAINERS 45 MIN (A4/E6).
# pedir_vinculacao chama _garantir_containers_de_pe (a mesma do Publicar agora):
# o Guardiao nao derruba o WhatsApp no meio do pareamento. app/whatsapp.py.

# Ultimo toque: 18/09/2026 17h05 -- O SITE PARA DE ACORDAR O DELL PELO WI-FI (item 22).
# A pergunta a porta do Dell casou ao segundo com o acordar (00:22:30, 00:37:40,
# 06:10:59). Com o aviso 'vou parar' valendo, maquina.estado e camadas nao
# perguntam nada. app/maquina.py.

# Ultimo toque: 18/09/2026 17h15 -- TELA DE SESSAO NAO REPETE PERGUNTA A ENDERECO MUDO (C7).
# /andamento levava 10,3 s por volta com containers parados; endereco que nao
# respondeu fica 60 s sem ser perguntado (so exibicao). app/whatsapp.py.

# Ultimo toque: 18/09/2026 17h25 -- CARD DA AUTOMACAO DIZ 'PEDIDA - SUBINDO' ATE O WHATSAPP
# RESPONDER (Pendente 15). So pergunta com pedido ativo. app/blueprints/admin.py,
# admin/dashboard.html.

# Ultimo toque: 18/09/2026 17h30 -- CODIGO DE PAREAMENTO MOSTRA 'GERADO HA X' (A9).
# admin/whatsapp_sessao.html (so o quadro do codigo).

# Ultimo toque: 18/09/2026 18h05 -- BOTAO 'FAZER A FAXINA' DA CIDADE (A10 + item 5).
# Pedido no cofre da cidade; a maquina busca em /api/faxina/pendente e conta em
# /api/faxina/resultado (scripts/faxina_cidade.py). So o container e os nomes
# com o sufixo da cidade ativa. app/whatsapp.py, app/blueprints/api.py,
# app/blueprints/admin.py, admin/whatsapp_sessao.html.

# Ultimo toque: 18/09/2026 18h08 -- faxina: consulta de pareamento em curso protegida
# (erro vira recusa explicada, nao tela 500). app/whatsapp.py.

# Ultimo toque: 18/09/2026 23h59 -- AUDITORIA DO TESTE: E-A (Apagar de 'O que existe na
# maquina' pede caixinha, nao nome digitado), E-B (botao 'Limpar' vira 'Apagar historico
# do pedido (nao desconecta)'), E-E ('pareada ha' com 3 casas de hora, nao degraus de
# 6 min). admin/whatsapp_sessao.html, app/whatsapp.py.

# Ultimo toque: 19/09/2026 00h40 -- O CLIQUE DIZ QUAL BOTAO: log de acesso e canonico
# ganham [acao sessao] do formulario (so essas duas palavras). app/acesso.py.

# Ultimo toque: 19/09/2026 01h00 -- CIDADE INATIVA NAO ACIONA: agenda de mensagens e
# conferencia diaria so percorrem consolidado.cidades_ativas() (antes: todo Config).
# app/agendamensagens.py, app/conferenciaauto.py.

# Ultimo toque: 19/09/2026 14h05 -- CIDADE ATIVA = SO O CAMPO MARCADO (P-135).
# consolidado.cidades_ativas deixa de somar 'tem dado'. Palmeiropolis marcada pelo
# dono antes. app/consolidado.py.

# Ultimo toque: 19/09/2026 14h25 -- CIDADE INATIVA SEM PAGINA PUBLICA + botao 'Apagar
# historico do pedido' removido (decisoes do dono). ranking.py, conferencia.py,
# athlete.py (_resolve/_cidade_por_slug/lista_publica so ativas); whatsapp_sessao.html.

# Ultimo toque: 19/09/2026 15h56 -- REFAZER A CONEXAO (item 62): le o grupo; nao le ->
# reinicia ate 3x; so entao apaga e refaz (FAILED refaz direto); recria 1x por pedido;
# pede a limpeza das copias em arquivo daquela sessao. app/whatsapp.py, app/blueprints/api.py.

# Ultimo toque: 19/09/2026 16h07 -- SELETOR DE CIDADE DO SUPREMO VOLTA (item 64): o
# @app.context_processor de inject_admin_badges estava duplicado em inject_endereco;
# a lista de cidades e as bolinhas do cabecalho nunca eram montadas. + rotas
# /api/versoes* (item 67) ja salvas. app/__init__.py, app/blueprints/api.py, app/whatsapp.py.

# Ultimo toque: 19/09/2026 16h11 -- QUADRO DE VERSOES (item 67), so master supremo: WAHA
# (instalada, a frente/atras, latest como ultimo recurso), motor, Docker, Debian, WSL;
# troca do WAHA pedida aqui e feita por scripts/versoes.py. admin.py, whatsapp_sessao.html.

# Ultimo toque: 19/09/2026 16h20 -- MANUTENCAO SO DO MASTER SUPREMO (item 65: pagina,
# backup, restauracao, cache, faxina) e /login fora do 'sem tela' do indice (item 66).
# app/blueprints/admin.py, app/indice.py.

# Ultimo toque: 19/09/2026 20h02 -- CLIQUE POR LINK REGISTRADO (item 70: acao GET entra
# no rastro; botao novo e POST e ja entrava), FEEDBACK DOS ATLETAS VAI PARA PAGINAS
# PUBLICAS (item 71) e CARD DO PAINEL CONTA SO O QUE O PERFIL VE (item 72).
# app/acesso.py, app/indice.py, app/blueprints/admin.py.

# Ultimo toque: 19/09/2026 20h06 -- FIM DO AVISO FALSO 'pareada ha N minutos' (itens 48/61):
# so conta pedido que REALMENTE gerou codigo; 'ja estava conectada' nao conta. app/whatsapp.py.

# Ultimo toque: 19/09/2026 20h40 -- RECORDES (item 76): texto explicativo UNICO embaixo das
# duas tabelas + botao publico de aviso (feedback da cidade) + admin logado tira uma marca
# das tabelas de velocidade com motivo (flag do anticheat + refaz a foto do dia).
# app/blueprints/ranking.py, publico/home.html.

# Ultimo toque: 19/09/2026 20h40 -- TEXTOS DA PAGINA INICIAL (item 74): os 9 titulos de
# secao e o paragrafo de apresentacao entraram no catalogo editavel pela tela
# 'Textos da pagina inicial'. Numero, nome e tabela continuam vindo do banco.
# app/textossite.py, publico/home.html.


# Ultimo toque: 19/09/2026 20h21 -- ANTICHEAT (item 75): na tela de Regras, quem esta
# sendo flagado por velocidade — por atleta e modalidade, quantas flags em aberto e a
# faixa (menor/media/maior), para o limite individual nao ser palpite. So leitura.
# app/blueprints/admin.py (_resumo_flags_velocidade), admin/anticheat.html.

# Ultimo toque: 19/09/2026 21h53 -- HIERARQUIA (item 73): o admin comum entra no
# painel e ve so o que pode usar; horarios, cookie do Strava, token/endereco,
# cidades e 'Comandos do sistema' (que foi para Manutencao) ficaram so do
# supremo; ligar/desligar o modo teste continua so do master.
# app/blueprints/admin.py, app/indice.py, admin/dashboard.html.

# Ultimo toque: 19/09/2026 22h05 -- HIERARQUIA (item 73, parte b): abrir, ativar e
# apagar cidade viraram do supremo; a tela de assunto nao abre mais vazia (sem
# nenhuma tela que a pessoa possa abrir, ela volta ao painel); na Sessao do
# WhatsApp, 'Ajustar o endereco na mao' (IP, MAC, chave do WAHA, ligar/desligar
# a maquina) ficou so do supremo e a faxina, do master para cima.
# app/blueprints/admin.py, app/indice.py, admin/whatsapp_sessao.html.

# Ultimo toque: 20/09/2026 11h05 -- HIERARQUIA (item 73, parte c, pedidos do dono):
# o admin ajuda o master no LIVRO-CAIXA e na PRESTACAO DE CONTAS (lancar, anexar
# comprovante, conciliar, exportar); botao 'Painel' do cabecalho passa a aparecer
# para ele; reconsolidar o ranking DA PROPRIA cidade tambem e dele; o card
# 'Usuarios' so aparece para quem a rota deixa entrar (master/aprovador).
# app/blueprints/livrocaixa.py, config.py, admin.py, app/indice.py, base.html.

# Ultimo toque: 20/09/2026 11h50 -- CONTA POR E-MAIL (P-103): a senha passa a ser
# de quem usa. Convite ao criar usuario (a pessoa cria a propria senha por link
# no e-mail dela), 'Esqueci a minha senha' na tela de entrar, e troca de e-mail
# confirmada no endereco ATUAL (avisa os dois lados). Colunas email_novo,
# email_token e email_token_expiry criadas no boot por contas.ensure_schema().
# app/contas.py (novo), models.py, __init__.py, blueprints/auth.py, admin.py,
# app/indice.py, auth/esqueci.html, auth/trocar_email.html, login.html, base.html.

# Ultimo toque: 20/09/2026 12h05 -- P-103, correcao do dono: NAO criar tela onde ja
# existe uma. As duas telas novas sairam: 'Esqueci a minha senha' virou um bloco
# dentro da tela de entrar e 'Trocar o meu e-mail', um bloco dentro de 'Alterar
# Senha' (que passa a ser a tela da conta). As rotas viraram POST; quem abre pelo
# endereco cai na tela que ja existia.
# app/blueprints/auth.py, auth/login.html, auth/change_password.html, indice.py.

# Ultimo toque: 20/09/2026 12h11 -- HIERARQUIA (item 73, fim): visualizador e
# patrocinador tambem entram no painel e veem SO o que acompanham (ranking,
# prestacao de contas, livro-caixa, o que o projeto fez, relatorios, conferencia
# e paginas publicas). No catalogo, nivel vazio passou a significar
# 'pode_administrar' (NIVEL_PADRAO) -- esquecer o nivel numa tela nova FECHA em
# vez de abrir -- e a tela de leitura diz 'pode_ver'. Os 6 formularios de
# escrita de 'O que o projeto fez' so aparecem para quem administra. E o nome
# 'n8n' saiu dos textos do site (ficou so a explicacao historica do correio).
# app/models.py, indice.py, blueprints/admin.py, execucao.py, api.py, ranking.py,
# admin/area.html, admin/execucao.html e 6 telas com texto do n8n.

# Ultimo toque: 20/09/2026 12h16 -- HIERARQUIA (item 73), os dois furos que a prova
# dos 5 perfis mostrou: a PRESTACAO DE CONTAS agora ABRE em leitura para
# visualizador e patrocinador (era o que eles vinham ver) e os botoes de gravar
# so aparecem para quem administra -- com recusa tambem no servidor, no POST; 'O
# que o projeto fez' entrou na allowlist do auditor; e o INDICE DAS TELAS virou
# manual de leitura para todo perfil.
# app/blueprints/config.py, admin.py, app/auditor.py, config/prestacao.html.

# Ultimo toque: 20/09/2026 12h36 -- P-136 FECHAMENTO DO MES: a conferencia passou a
# rodar nos dias FIXOS 10, 20 e ULTIMO do mes (era 'a cada N dias'). Nos dias 10 e
# 20 roda depois do ULTIMO ranking do dia e lanca no maximo 3 links por pessoa --
# e nenhum se faltar link anterior ao marco anterior (o resto vira aviso 'precisa
# postar'). No ultimo dia, as 22h30, lanca TUDO, sem o teto de 60 atletas (que
# deixou de existir); se entrar km novo, pede a rodada e sai mais um ranking,
# igual aos outros. A tela do ranking passou a explicar a regra.
# app/conferenciaauto.py, app/blueprints/processing.py (fetch_athlete_month_entries),
# ranking/ranking.html.

# Ultimo toque: 20/09/2026 12h45 -- LIMPEZA (pedido do dono): saiu o que ninguem
# chamava -- a pagina inicial ANTIGA (app/templates/index.html), o test_app.py
# (chamava create_app() sem database_uri, apontando para producao), a copia velha
# do CLAUDE.md, o banco_provas.db de 0 byte, as pastas vazias templates/ e
# nginx_tmp/ e a static/ da raiz (duas imagens que o Flask nunca serviu) e o
# validate_file() que so tinha `pass`. A lista do backup foi acertada junto.
# Cópia de tudo antes: Backups/extra/codigo/codigo_antes_da_limpeza_20260920_123615.zip
# app/forms.py, app/blueprints/admin.py.

# Ultimo toque: 20/09/2026 13h25 -- P-137 RANKING POR PERIODO: tela unica com abas
# (mensal, trimestral, semestral, anual), blocos FIXOS do calendario (jan-mar,
# jan-jun...), e o botao Copiar leva SEMPRE o texto da aba aberta. A escolha do
# periodo com trofeu e' por cidade (cofre: periodo_premiado); nenhum marcado =
# nao ha patrocinador e o ranking do periodo nao vai ao grupo. O painel publico
# do atleta ganhou trimestre e semestre, e o link do ranking abre no periodo dele.
# A soma reaproveita a do anual (recorte de meses), para nao existirem duas
# verdades sobre o mesmo km.
# app/blueprints/ranking.py, athlete.py, app/secretstore.py, app/indice.py,
# ranking/periodo.html (nova), athlete/dashboard.html.

# Ultimo toque: 20/09/2026 13h40 -- P-137, correcao achada pela propria prova:
# marcar 'nenhum trofeu' nao desmarcava nada. O `secretstore.set_value` recusa
# valor vazio e devolve False em silencio; agora 'nenhum' APAGA a chave.
# app/blueprints/ranking.py (definir_periodo_premiado).

# Ultimo toque: 20/09/2026 22h25 -- P-137-b: o ranking do PERIODO sai sozinho no
# grupo, no lugar do mensal (nunca os dois, decisao do dono). Quem decide e' o
# site, dentro do /api/ranking/latest que a automacao ja pergunta -- a pipeline
# nao mudou uma linha. Trimestral por semana (1/2/3x ou diario), semestral por
# mes (4/6/8x), anual so na virada; sempre depois do ULTIMO horario da agenda da
# cidade, uma vez por dia. No dia 1o, com o mes zerado, sai o periodo premiado --
# ou o anual, se nenhum estiver marcado.
# app/rankingperiodo.py (novo), app/blueprints/api.py, ranking.py,
# ranking/periodo.html.

# Ultimo toque: 20/09/2026 22h30 -- P-137-b, conserto MEDIDO pela propria prova:
# `format_ranking_partes` e' do ranking MENSAL (exige `mes_nome` e a meta) e
# estourava com o ranking do periodo, caindo numa mensagem so, SEM o link do
# painel. O periodo ganhou as partes dele (`format_periodo_partes`), sem meta nem
# medalhista, e o link entrou no texto unico tambem -- quem copia da tela leva o
# painel junto.
# app/blueprints/ranking.py, app/blueprints/api.py.

# Ultimo toque: 20/09/2026 22h43 -- P-139: os horarios da tela passam a mandar na
# automacao. MEDIDO: `executar.sh` tinha a janela escrita dentro dele (05:10-06:15
# e 15:10-16:15) e nao seguia a tela 'Quem publica e quando'; o Wake-on-LAN do
# site ja seguia. Agora: (1) `executar.sh` pergunta ao site (/api/agenda/agora) e
# cai na janela de seguranca se o site nao responder; (2) endereco publico minimo
# /api/janela/<cidade>, para o Agendador do Windows perguntar 'e hora?' ANTES de
# ligar o WSL (o token mora dentro do WSL, e liga-lo a cada 15 min era o custo a
# evitar). Provado ao vivo: agenda 22:38 -> 'estamos na janela'; agenda 03:00 ->
# recusa; site fora do ar -> janela de seguranca.
# app/blueprints/api.py e ~/movimente-automacao/scripts/executar.sh.

# Ultimo toque: 20/09/2026 22h53 -- P-139 (peca que faltava): o SITE pede a rodada
# no horario da tela. MEDIDO: a tarefa do Agendador tem 05:30/15:30 escritos nela
# e alterar exige administrador ('Acesso negado'); sem isto, mudar o horario para
# 17:00 faria o sistema parar de publicar na hora errada mas nao publicar na hora
# nova. Agora o vigia registra `rodada.pedir` na janela do horario da agenda (o
# mesmo caminho do botao 'Publicar agora') e a maquina atende em 1 minuto.
# Provado: 7 casos (horario velho nao pede, o novo pede, nao pede duas vezes,
# horario sem ranking e horario inativo nao pedem).
# app/vigia.py.

# Ultimo toque: 20/09/2026 22h58 -- P-140 ETIQUETAS: botao 'Baixar PDF' na folha de
# etiquetas. A grade da tela estava certa (parametros Pimaco, conta simetrica); o
# que estragava era o caminho ate o papel -- escala 'ajustar a pagina', papel A4
# no lugar de Carta (2,7% menor) ou margem do navegador. No PDF a pagina e Carta
# de verdade e cada etiqueta tem coordenada absoluta; sai com uma regua de 100 mm
# no rodape, para o dono medir e descobrir se a impressora esta escalando.
# Gerador proprio, sem biblioteca (o NAS nao tem reportlab nem weasyprint).
# Provado: 28 casos medindo as coordenadas DENTRO do PDF.
# app/etiquetaspdf.py (novo), app/blueprints/admin.py, admin/etiquetas.html.

# Ultimo toque: 20/09/2026 23h52 -- P-139, furo achado NA AUDITORIA do proprio
# teste: as 15:30 passariam a existir DOIS disparos (a tarefa do Agendador, que
# continua com 15:30 escrito nela, e o pedido novo do site). A segunda rodada nao
# publicaria duas vezes (a trava 'sem novidade' barra), mas refaria a coleta
# inteira a toa. Agora o site anota quando entregou o ranking
# (`ranking_servido_em`, gravado pelo proprio /api/ranking/latest) e o vigia nao
# pede se a rodada daquela janela ja aconteceu. Provado: 9 cenarios, inclusive
# maquina dormindo (pede) e marca de ontem (pede).
# app/secretstore.py, app/blueprints/api.py, app/vigia.py.

# Ultimo toque: 21/09/2026 00h29 -- RELOGIO DA RODADA (pedido do dono): o pedido
# passa a nascer NO MINUTO do horario da tela. Ate aqui quem pedia era a passada
# de 15 em 15 min do vigia — no teste das 00:25 o pedido saiu as 00:25:51, e num
# dia ruim sairia 14 min atrasado. Agora uma thread dorme ate o proximo horario
# da agenda (reconferindo a cada 5 min, para mudanca na tela valer na hora) e
# chama a MESMA guarda do vigia, que segue como rede de seguranca.
# app/relogiorodada.py (novo), app/__init__.py.

# Ultimo toque: 21/09/2026 03h15 -- RELOGIO DA RODADA, conserto medido: ele
# acordava 2 SEGUNDOS ANTES do horario e a guarda recusava ('horario <= agora'),
# e na volta seguinte o proximo alvo ja era o de amanha — o pedido nunca nascia.
# Foi o que fez o teste das 00:40 sair as 00:49:29, pela passada do vigia. Agora
# acorda 3 segundos DEPOIS.
# app/relogiorodada.py.

# Ultimo toque: 24/09/2026 19h07 -- LIVRO-CAIXA COM SALDO, HISTORICO E PLANILHA. Saldo
# consolidado (o que veio de antes + o do periodo) em todas as telas de dinheiro, saida
# sempre negativa, fluxo de caixa, planilha .xlsx formatada com link do comprovante,
# historico de quem lancou/corrigiu/apagou (tabela nova historico_caixa), editar so para
# master. app/caixa.py, planilha.py (novo), models.py, blueprints/livrocaixa.py, relatorios.py,
# config.py, ranking.py, auditor.py, indice.py.

# Ultimo toque: 24/09/2026 19h13 -- ACOES E EVENTOS DO PROJETO. A tela 'O que o projeto faz'
# aceita evento/pedalada/acao educativa (coluna tipo), liga foto/video/documento ao evento
# (midias_projeto.acao_id), master apaga (backup antes; a midia fica solta), acompanhamento
# nutricional encerrado (so consulta). Colunas novas por ALTER, declaradas no modelo.
# app/models.py, blueprints/execucao.py, blueprints/relatorios.py.

# Ultimo toque: 24/09/2026 22h06 -- LIVRO-CAIXA RODADA 2. Link do comprovante da planilha volta a
# funcionar (o login ja logado segue o next), comprovante abre no navegador (imagem/PDF),
# tipos de evento configuraveis (tabela tipos_acao) com campos marcados na criacao,
# historico completo (conciliacao, prestacao, comprovante publico), textos das telas
# enxugados, coluna Rubrica some da planilha se vazia. auth.py, caixa.py, models.py,
# blueprints/execucao.py, livrocaixa.py, config.py, relatorios.py.

# Ultimo toque: 25/09/2026 10h37 -- ISOLAMENTO ENTRE CIDADES E LINK PUBLICO DO COMPROVANTE.
# admin.py: feedback_status, override_remover e alternar_contagem_atividade passam a barrar o
# master de OUTRA cidade (is_master -> is_supremo). Tela 'Corrigir lancamento' voltou a abrir
# (erro de sintaxe Jinja). Comprovante com link aleatorio (lancamentos_caixa.link_token, rota
# /ranking/comprovante/<token>) na planilha e nos relatorios; master troca o link.
# app/models.py, caixa.py, auditor.py, blueprints/admin.py, livrocaixa.py, ranking.py, relatorios.py.

# Ultimo toque: 25/09/2026 12h53 -- FUROS ENTRE CIDADES FECHADOS (is_master -> is_supremo): upload.py
# (ver e apagar historico de upload), admin.py (lista de atletas, ajuste manual, privacidade,
# bicicleta, anticheat), athlete.py (ajuste manual de km, agora 403 para atleta de outra cidade),
# config.py (anexar comprovante publico da prestacao). Provado com 2 cidades em banco de teste.
# blueprints/admin.py, athlete.py, config.py, upload.py.

# Ultimo toque: 25/09/2026 13h22 -- PEDIDOS PUBLICOS. Blueprint novo blueprints/pedidos.py: (1) cadastro
# de visualizador pelo proprio atleta (telefone conferido, e-mail confirmado, aviso a supremo +
# master/admin da cidade, aprovacao); (2) pedido de relatorio completo na transparencia, com link
# publico e comprovantes que valem 30 dias. Tabelas novas: tentativas_publicas, pedidos_relatorio,
# cadastros_atleta. correio.enviar ganhou responder_para. app/__init__.py, models.py, correio.py.

# Ultimo toque: 25/09/2026 16h54 -- cadastro pelo atleta: quando o telefone/e-mail JA tem acesso, a
# mensagem diz qual e a tela mostra Entrar e Esqueci a minha senha (que abre o campo). Caso real:
# o telefone do atleta Maykon Abreu ja era do usuario twf. blueprints/pedidos.py.

# Ultimo toque: 25/09/2026 17h36 -- ENTRAR COMO ATLETA. Pagina neutra /pedidos/acesso/<id> (entrar ou
# criar); entrar pela pagina do atleta usa a mesma conta mas a SESSAO vale so como visualizador
# (Usuario.tipo_efetivo, amarrado ao id do logado; supremo nao atravessa cidade); aviso 'Voce esta
# como atleta' no topo; login comum desfaz o modo atleta. models.py, auth.py, pedidos.py, base.html.

# Ultimo toque: 25/09/2026 18h36 -- P-137-c ABAS DO RANKING POR PERIODO. Abas Mensal/Trimestral/Semestral/Anual
# no /ranking (antes so existiam dentro de /ranking/periodo/<p>, sem porta de entrada) + linha do trofeu;
# rota /ranking/periodo (card do painel); abas na pagina publica do link do grupo (public-yearly aceita
# ?periodo= e ?bloco=); link do ranking de periodo agora e /r/<slug>?periodo= (publico, antes pedia senha).
# ranking.py, api.py, indice.py, 4 telas de ranking + _abas_periodo.html e _abas_publicas.html.

# Ultimo toque: 01/10/2026 17h22 -- VOLTAR AO MESMO LUGAR + MEDALHAS + REJEITAR EM MASSA. app/navegacao.py (novo):
# voltar() devolve ao endereco de onde veio o clique (so do proprio site); 25 retornos na Central e Revisoes
# (admin.py) e 6 nos Avisos (avisos.py); base.html lembra aba aberta e altura da pagina. Revisoes: botao
# rejeitar em massa (justificativa opcional, confirma contagem). Medalhistas: 'Cobrar quem nao retirou';
# aviso do grupo sem quem ja retirou; aviso repetido abre dizendo que e de mes anterior. mensagens.py.

# Ultimo toque: 01/10/2026 18h06 -- MEDALHISTAS DE SETEMBRO (foto tirada com o mes aberto: mes em andamento nunca
# vira foto; foto invalida se refaz; avisos.py) + texto do alerta 'NAO do mes fechado' + contador 'ja saiu Nx' na
# Central e Medalhistas + rodape '2 meses' no reenvio (admin.py, mensagens.py, 2 telas) + P-147: voltar() em admin,
# execucao, livrocaixa, ranking, config + botao Voltar pela trilha (base.html) + e-mail do cadastro de visualizador
# vai para a agenda (pedidos.py). Ensaios na copia: 0 falhas; 260 telas sem 500.

# Ultimo toque: 01/10/2026 18h2x -- 'Mandar de novo' da Central acha a mensagem do mes escolhido (antes: 'Mensagem nao
# encontrada' fora do mes anterior ao de hoje; mesmo defeito do 'Salvar e enviar' em 14/08). admin.py::central_enviar.

# Ultimo toque: 01/10/2026 22h4x -- 22:30 do ultimo dia: a conferencia SEMPRE pede o ranking final (antes so quando
# gravava algo; decisao do dono). conferenciaauto.py. No Dell, a pipeline envia o ranking das 22:00-24:00 do ultimo dia
# mesmo sem km novo; no dia 1 republica o final do mes anterior se os km dele mudaram; @ do aviso vira @id (lid).

# Ultimo toque: 06/10/2026 -- A LOGO DO PATROCINADOR NAO SAIA NAS RODADAS SEM VAGA NA GRADE.
# CAUSA (medida com o codigo real sobre a copia do banco das 11h22): _grade_semanal so dava ao
# principal as vagas do PROPRIO peso; no padrao (peso 3 = 7x) sobravam vagas VAZIAS e apoio_da_vez
# devolvia None -- o ranking saia sem logo (05/10 15h30, so com o Toim; 06/10 15h30, Toim + Ab-Laine).
# Antes: 01 a 05/10 de manha, outubro nao tinha patrocinador cadastrado (Toim salvo 05/10 14h41).
# CONSERTO: o principal absorve toda vaga que sobrar (era o desenho de 07/09); peso 0 continua de fora.
# Apoios nao mudaram (3 vagas/semana). Rodadas sem patrocinador em 14 dias: 8 de 28 -> 0 de 28.
# Arquivo: app/blueprints/ranking.py (_grade_semanal).

# Ultimo toque: 15/10/2026 -- NENHUM DADO PESSOAL OU SEGREDO NO CODIGO (regra 28 do CLAUDE.md, pedido do dono).
# O codigo vai para um GitHub publico sob licenca de uso nao comercial (PolyForm). Contato, chave PIX padrao e a
# chave legada saem do codigo e passam a morar em instance/dados_locais.json (app/dadoslocais.py; nos templates
# {{ dado_local('chave') }}). A secao de pessoas da home foi para publico/_creditos.html (fora do git).
# Telefones, e-mails, nomes e ids reais em comentarios, mensagens e exemplos viraram formato ou nome ficticio.
# Provado antes: replica sobre copia do banco, 121 de 132 paginas identicas e as 11 diferentes so no texto de exemplo.
