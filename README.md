# MoveMentes

**MoveMentes — mover o corpo para mover a mente.**

Sistema voluntário e sem fins lucrativos para grupos comunitários de caminhada,
corrida e pedal. As pessoas postam o link da atividade (Strava) no grupo de
WhatsApp; o sistema lê os links, soma os quilômetros, publica um ranking mensal
no grupo e entrega medalhas bancadas por patrocinadores locais. Cada cidade é
isolada das outras.

## Licença: somente uso não comercial

Este código é distribuído sob a
[PolyForm Noncommercial License 1.0.0](LICENSE). Em português claro:

- **Pode:** estudar, usar, adaptar e redistribuir para fins **não comerciais** —
  comunidades, grupos voluntários, escolas, ONGs e uso pessoal.
- **Não pode:** usar para ganho comercial, nem cobrar dinheiro de quem participa
  (mensalidade, taxa de grupo, venda de acesso).
- **Quer outro tipo de uso?** Peça autorização **antes**: abra uma *issue* neste
  repositório com o título "Autorização de uso". O autor responde e libera caso a caso.

O texto que vale juridicamente é o do arquivo [LICENSE](LICENSE); o resumo acima
serve só para orientar. Os avisos que devem acompanhar o código estão em [NOTICE](NOTICE).

**A intenção:** o projeto nasceu de trabalho **voluntário** e deve continuar assim —
participar de um grupo, de um ranking ou receber uma medalha **não custa nada**
para ninguém.

## Duas partes que trabalham juntas

Este repositório tem **duas pastas**, e uma não funciona sem a outra:

| Pasta | O que é | Onde roda |
|---|---|---|
| [`site/`](site/) | A **aplicação web** (Flask + SQLite): recebe os links, lê o Strava, soma os km, guarda o ranking, mostra as páginas públicas e o painel de administração, e expõe uma **API** (`/api/*`) para a automação. | Um servidor que fica ligado (qualquer Linux com Python; no projeto original, um NAS). |
| [`automacao/`](automacao/) | A **automação do WhatsApp**: contêineres (Postgres + [WAHA](https://waha.devlike.pro), a API do WhatsApp) e scripts Python/Bash que leem o grupo, mandam para o site e publicam o ranking de volta. | Um computador com Docker (no projeto original, WSL/Debian num PC com Windows, ligado só nos horários do ranking). |

### Como as duas partes se falam

```
 grupo de WhatsApp                    automacao/                          site/
 ─────────────────   mensagens   ┌───────────────────┐   POST /api/upload   ┌──────────────────┐
  pessoas postam   ──────────►   │ WAHA (contêiner)  │ ───────────────────► │ lê os links,     │
  links do Strava                │   ▲           │   │   (conversa em .txt) │ consulta o Strava│
                                 │   │           ▼   │                      │ e soma os km     │
  ◄──── ranking e medalhas ───── │ scripts/send_ranking.py  ◄───────────────│ GET /api/ranking/latest
                                 └───────────────────┘   texto pronto       └──────────────────┘
```

1. A cada rodada (por exemplo 2 vezes ao dia), `automacao/scripts/executar.sh` sobe
   os contêineres (se estiverem parados) e espera o WAHA ficar pronto.
2. `send_ranking.py` pergunta ao WAHA as mensagens novas do grupo e monta um `.txt`
   no formato de conversa exportada do WhatsApp.
3. Envia o `.txt` ao site (`POST /api/upload`); o site extrai os links, consulta o
   Strava, soma os quilômetros e guarda tudo, **por cidade**.
4. A automação acompanha o processamento (`GET /api/upload/<job>`) e, quando termina,
   busca o ranking pronto (`GET /api/ranking/latest`) — o texto já vem dividido em
   mensagens e com o patrocinador da vez.
5. Publica no grupo pelo WAHA e confere se a entrega aconteceu.
6. Todas as chamadas à API levam o cabeçalho `X-Automation-Token`, um token **por
   cidade** gerado no painel do site (Painel › Automação). Uma cidade só enxerga a si mesma.

Quem decide **qual sessão do WhatsApp usar** é um só módulo (`automacao/scripts/wahasessao.py`),
e `prontidao.py` confere cada etapa antes de publicar: se algo falha, **nada é publicado**
no grupo e o erro é avisado.

## Como usar com o seu próprio número

Nenhum telefone, e-mail, nome, senha, chave ou id vem neste repositório. **Cada pessoa
cadastra o próprio WhatsApp** e as próprias chaves, e elas ficam só na máquina dela:

1. **Site** — copie `site/dados_locais.exemplo.json` para `site/instance/dados_locais.json`
   e preencha (contato, chave PIX). O cofre de segredos e o banco também ficam em `site/instance/`.
   Instale com `pip install -r site/requirements.txt`; a entrada é `site/wsgi.py` (uWSGI).
2. **Automação** — copie `automacao/.env.exemplo` para `automacao/.env` e preencha
   (senha do Postgres, chave do WAHA, endereço do seu site, token da API gerado no painel).
   Suba com `docker compose up -d`.
3. **WhatsApp** — pareie **o seu** número pelo painel do site (código de pareamento ou QR);
   a sessão fica guardada no seu Postgres, nunca no repositório.
4. Confira tudo com `automacao/scripts/executar.sh --diagnose` (sem publicar nada) e teste
   o ciclo com `--dry-run`.

## O que de propósito NÃO está aqui

Dados e logs; backups; sessões e cópias de sessão do WhatsApp; `.env` e o cofre;
estado de pareamento (códigos de pareamento valem poucos minutos e nunca vão para o
repositório); o QR do PIX; a seção "quem faz acontecer" da página inicial (nomes de
pessoas); os scripts do Agendador do Windows que ligam e desligam o computador; e
qualquer coisa de outros serviços que dividem a mesma máquina.

## Privacidade e verificação

A regra do projeto: **nenhum dado pessoal ou segredo no código**, nem em comentário
nem como exemplo — só o formato (`(DD) 9XXXX-XXXX`, `55DD9XXXXXXXX`). Antes de
cada commit, `ferramentas/verificar_sensiveis.py` confere se algo escapou. Para o git
barrar o commit sozinho:

```bash
python ferramentas/verificar_sensiveis.py --instalar-hook
```

## Organização

```
site/         aplicação web (app/, migrations/, wsgi.py, config.py…)
automacao/    docker-compose.yml, .env.exemplo e scripts/
ferramentas/  verificação de dados sensíveis
```
