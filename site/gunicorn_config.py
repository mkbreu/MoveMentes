# ⚠️ ARQUIVO DESATIVADO - Projeto usa uWSGI + nginx
# Este arquivo foi mantido para referência histórica apenas
# 
# O servidor web atual usa:
# - uWSGI (porta 8000, via socket unix)
# - nginx (proxy reverso na porta 80)
#
# Para voltar a usar Gunicorn, descomente o código abaixo:
"""
import multiprocessing
bind = "127.0.0.1:8001"
workers = 2
worker_class = "sync"
timeout = 60
proc_name = 'movimente-se'
chdir = '/volume1/web/movimente-se'
accesslog = '/volume1/web/movimente-se/logs/gunicorn_access.log'
errorlog = '/volume1/web/movimente-se/logs/gunicorn_error.log'
"""
