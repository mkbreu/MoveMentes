# -*- coding: utf-8 -*-
"""Executa UM trabalho longo num processo proprio, fora do servidor do site.

POR QUE ISTO EXISTE (26/08/2026 — `ERROS.md` §4.24)
Ate hoje a importacao de atleta rodava como uma thread DENTRO do processo que
atende as paginas. Tres problemas, todos ja sentidos na pratica:

  1. O worker do uWSGI e reciclado (por `max-requests`, por reload, por salvar
     um `.py`) e a thread morria junto, no meio do trabalho. Foi a origem das
     "importacoes que sumiam" e das retomadas em serie.
  2. Cancelar era um PEDIDO: a thread so via o arquivo `.cancel` entre uma
     pausa e outra, e presa numa requisicao ao Strava nao via nada. Daí o
     "cliquei em cancelar e ele continuou".
  3. A memoria da importacao ficava presa no worker web depois de terminar, num
     aparelho com 490 MB.

Rodando aqui, o trabalho e' um processo com PID proprio: cancelar pode ser uma
parada de fato, reiniciar o site nao mata mais nada, e a memoria volta ao
sistema quando ele termina.

USO (quem chama e' o site, nao uma pessoa):
    python -m app.jobrunner <job_id>

O andamento continua no MESMO lugar de sempre (`temp_extract/jobs/<id>.json`),
entao nada muda para a tela — que nem sabe que isto existe.
"""
import logging
import os
import sys

logger = logging.getLogger(__name__)


def executar(job_id):
    """Roda o job `job_id` ate o fim. Devolve o codigo de saida do processo."""
    # A trava tem de estar de pe ANTES de o app ser criado: e' ela que impede o
    # vigia, a rotina diaria e o ticker de subirem tambem aqui.
    os.environ['MOVIMENTE_SO_JOB'] = '1'

    from app import create_app
    from app.blueprints import upload as up

    app = create_app()
    with app.app_context():
        job = up._read_progress_file(job_id)
        if not job:
            app.logger.error('jobrunner: job %s nao encontrado em disco', job_id)
            return 2

        params = dict(job.get('params') or {})
        runner = {
            'upload_txt':        up._run_upload_job,
            'importacao_atleta': up._run_importacao_atleta,
            'recuperacao_links': up._run_recuperacao_links,
            # Atualizar nome/foto tambem roda aqui (07/09/2026): fora do site,
            # o trabalho sobrevive ao reload que a thread antiga nao sobrevivia.
            'atualizar_perfis':  up._run_atualizar_perfis,
        }.get(params.get('kind'))
        if runner is None:
            app.logger.error('jobrunner: job %s sem tipo conhecido (%s)',
                             job_id, params.get('kind'))
            return 3

        # QUEM E' O DONO AGORA SOU EU. `_maybe_resume_job` decide se um job
        # morreu perguntando se o processo dono ainda esta de pe -- se ficasse
        # gravado o PID do worker web, o primeiro reload do site faria o ticker
        # concluir "morreu" e reanimar um trabalho que esta vivo aqui.
        params['owner_pid'] = os.getpid()
        up._set_job_params(job_id, params)

        app.logger.info('jobrunner: iniciando %s (%s) no processo %s',
                        job_id, params.get('kind'), os.getpid())
        try:
            runner(app, job_id, params)
        except Exception as exc:
            app.logger.error('jobrunner: %s terminou com erro: %s', job_id, exc,
                             exc_info=True)
            # A tela nao pode ficar girando para sempre por causa de uma falha
            # aqui: o job termina dizendo o que houve, e o que ja foi salvo fica.
            try:
                up._set_progress(
                    job_id, 'error',
                    'O trabalho parou por um erro inesperado (%s). O que ja foi '
                    'lido esta salvo; repetir continua de onde parou.' % exc,
                    0, job.get('zip_filename', ''), job.get('cidade', ''),
                )
            except Exception:
                pass
            return 1
        app.logger.info('jobrunner: %s concluido', job_id)
        return 0


def main(argv=None):
    argv = list(argv if argv is not None else sys.argv[1:])
    if len(argv) != 1:
        sys.stderr.write('uso: python -m app.jobrunner <job_id>\n')
        return 64
    return executar(argv[0])


if __name__ == '__main__':
    sys.exit(main())
