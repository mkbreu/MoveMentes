#/volume1/web/movimente-se/config.py
import os
from pathlib import Path

class Config:
    # O TOKEN CSRF NAO EXPIRA POR RELOGIO (14/08/2026).
    #
    # O padrao do Flask-WTF derruba o token depois de um tempo, e a tela de
    # upload fica ABERTA enquanto o processamento roda — as vezes por horas,
    # porque cada link e uma visita ao Strava com pausa entre elas. Quem
    # atualizava a pagina recebia "The CSRF token has expired" e achava que
    # tinha perdido o trabalho.
    #
    # `None` NAO desliga a protecao: o token continua sendo validado contra a
    # SESSAO do usuario. So deixa de morrer por idade — quem nao esta logado
    # continua sem conseguir postar nada.
    WTF_CSRF_TIME_LIMIT = None
    # Configurações básicas
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-key-change-in-production'
    
    # Diretórios
    BASE_DIR = Path(__file__).parent
    UPLOAD_FOLDER = BASE_DIR / 'uploads'
    UPLOAD_FOLDER_ZIP = BASE_DIR / 'uploads' / 'zip'
    UPLOAD_FOLDER_TXT = BASE_DIR / 'uploads' / 'txt'
    TEMP_EXTRACT_FOLDER = BASE_DIR / 'temp_extract'
    LOGS_FOLDER = BASE_DIR / 'logs'
    
    # Database
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///instance/movimente_se.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Upload settings
    MAX_CONTENT_LENGTH = 2 * 1024 * 1024 * 1024  # 2GB max file size
    ALLOWED_EXTENSIONS = {'txt', 'zip'}
    
    # Session
    PERMANENT_SESSION_LIFETIME = 7 * 24 * 60 * 60  # 7 days in seconds
    SESSION_COOKIE_SECURE = True
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'
    
    # Domínio e URLs
    # ⚠️ Esta classe Config NAO e' carregada por create_app() (app/enderecos.py
    # documenta isso) -- SERVER_NAME aqui e' morto, nunca aplicado. Mesmo
    # assim, atualizado para o dominio novo em 07/09/2026 para nao mentir se
    # alguem ler este arquivo. O endereco de verdade mora em app/enderecos.py.
    SERVER_NAME = os.environ.get('SERVER_NAME') or 'movementes.engekon.com'
    PREFERRED_URL_SCHEME = 'https'
    
    @staticmethod
    def init_app(app):
        pass

class DevelopmentConfig(Config):
    DEBUG = True
    SESSION_COOKIE_SECURE = False

class ProductionConfig(Config):
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    # TEMPLATE RECARREGA JUNTO COM O CODIGO (14/08/2026).
    #
    # Com DEBUG=False o Flask CACHEIA os templates: mexer num .html nao muda
    # nada ate o worker reiniciar. Custou tres tentativas do dono em 14/08:
    # ele digitava endereco e link do patrocinador, salvava, e nada
    # acontecia. O campo existia no arquivo e nao existia na pagina que o
    # navegador tinha. "Nao salvou" era, na verdade, "o formulario nem tinha
    # o campo".
    #
    # ESTA CLASSE NAO E USADA (16/08/2026). create_app() em app/__init__.py
    # monta app.config na mao e nunca chama app.config.from_object(config[...])
    # -- ProductionConfig, DevelopmentConfig e Config inteiras sao codigo
    # morto. Na pratica o .html recarrega sozinho mesmo assim (medido) e o
    # .py so recarrega quando wsgi.py muda de data (CLAUDE.md §1, ERROS.md
    # §4.8) -- NAO por causa deste TEMPLATES_AUTO_RELOAD, que nunca chega a
    # rodar. Fica registrado para quem for mexer aqui nao gastar tempo
    # achando que esta linha faz algo.
    TEMPLATES_AUTO_RELOAD = True

config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}