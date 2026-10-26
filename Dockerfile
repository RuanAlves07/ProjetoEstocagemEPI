# Imagem base oficial Python minimalista
FROM python:3.11-slim

# Evita criação de arquivos .pyc e garante logs em tempo real
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000

WORKDIR /app

# Instala dependências do projeto
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copia os arquivos da aplicação
COPY app/ ./app/
COPY run.py ./
COPY popular_caepi.py ./

# Cria pasta de dados persistentes para o SQLite e chaves de sessão
RUN mkdir -p /app/instance

# Declara volume para persistência dos dados (banco SQLite, upload de fotos e chaves)
VOLUME ["/app/instance"]

# Expõe a porta padrão da aplicação
EXPOSE 5000

# Healthcheck interno do container utilizando a rota pública /solicitar
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/solicitar')" || exit 1

# Inicializa via Gunicorn (servidor de produção com multi-threads)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "--threads", "4", "--timeout", "120", "run:app"]
