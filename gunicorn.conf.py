import os

# nginx проксирует сюда. Наружу порт не открываем.
bind = os.getenv("GUNICORN_BIND", "127.0.0.1:8000")

# ВАЖНО: workers = 1.
# Приложение держит историю чата в памяти процесса (assistant.sessions) и
# грузит RAG-индекс один раз на процесс. Несколько воркеров разнесут историю
# по разным процессам и продублируют индекс в памяти. Конкурентность — через
# потоки (gthread), а не процессы.
workers = 1
worker_class = "gthread"
threads = int(os.getenv("GUNICORN_THREADS", "4"))

# Запросы к GigaChat и Минздраву бывают медленными — даём время.
timeout = int(os.getenv("GUNICORN_TIMEOUT", "120"))
graceful_timeout = 30
keepalive = 5

# Логи в stdout/stderr — их подхватит journald (systemd).
accesslog = "-"
errorlog = "-"
loglevel = os.getenv("GUNICORN_LOGLEVEL", "info")
