-- Schema PostgreSQL (Supabase) para o Sistema de Controle de Estoque e Pedidos de EPIs
-- Pode ser executado direto no SQL Editor do Supabase ou automaticamente pelo sistema via DATABASE_URL.

CREATE TABLE IF NOT EXISTS usuarios (
    id                    SERIAL PRIMARY KEY,
    nome                  TEXT NOT NULL,
    login                 TEXT NOT NULL UNIQUE,
    email                 TEXT,
    receber_notificacoes  INTEGER NOT NULL DEFAULT 1,
    foto_perfil           TEXT,
    senha_hash            TEXT NOT NULL,
    perfil                TEXT NOT NULL,
    ativo                 INTEGER NOT NULL DEFAULT 1,
    criado_em             TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS'))
);

ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS usuarios_perfil_check;
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS email TEXT;
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS receber_notificacoes INTEGER NOT NULL DEFAULT 1;
ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS foto_perfil TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_usuarios_login_lower ON usuarios (LOWER(login));

CREATE TABLE IF NOT EXISTS perfis_acesso (
    chave       TEXT PRIMARY KEY,
    nome        TEXT NOT NULL,
    descricao   TEXT,
    permissoes  TEXT NOT NULL DEFAULT '',
    sistema     INTEGER NOT NULL DEFAULT 0,
    criado_em   TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS'))
);

CREATE TABLE IF NOT EXISTS epis (
    id                    SERIAL PRIMARY KEY,
    nome                  TEXT NOT NULL,
    nome_formulario       TEXT,
    tamanho               TEXT,
    categoria             TEXT,
    ca                    TEXT,
    validade_ca           TEXT,
    unidade               TEXT NOT NULL DEFAULT 'un',
    estoque_minimo        INTEGER NOT NULL DEFAULT 0 CHECK (estoque_minimo >= 0),
    prazo_reposicao_dias  INTEGER NOT NULL DEFAULT 15 CHECK (prazo_reposicao_dias >= 0),
    imagem_url            TEXT,
    exibir_no_forms       INTEGER NOT NULL DEFAULT 1,
    ativo                 INTEGER NOT NULL DEFAULT 1,
    criado_em             TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS'))
);

ALTER TABLE epis ADD COLUMN IF NOT EXISTS nome_formulario TEXT;
ALTER TABLE epis ADD COLUMN IF NOT EXISTS tamanho TEXT;
ALTER TABLE epis ADD COLUMN IF NOT EXISTS imagem_url TEXT;
ALTER TABLE epis ADD COLUMN IF NOT EXISTS exibir_no_forms INTEGER NOT NULL DEFAULT 1;

ALTER TABLE epis DROP CONSTRAINT IF EXISTS epis_nome_key;
DROP INDEX IF EXISTS idx_epis_nome_lower;
DROP INDEX IF EXISTS idx_epis_nome_tamanho;
CREATE UNIQUE INDEX IF NOT EXISTS idx_epis_nome_tam_ca ON epis (LOWER(nome), COALESCE(tamanho, ''), COALESCE(ca, ''));

CREATE TABLE IF NOT EXISTS colaboradores (
    id         SERIAL PRIMARY KEY,
    nome       TEXT NOT NULL,
    matricula  TEXT UNIQUE,
    setor      TEXT,
    funcao     TEXT,
    whatsapp   TEXT,
    cpf        TEXT,
    data_nascimento TEXT,
    ativo      INTEGER NOT NULL DEFAULT 1,
    criado_em  TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS'))
);
ALTER TABLE colaboradores ADD COLUMN IF NOT EXISTS whatsapp TEXT;
ALTER TABLE colaboradores ADD COLUMN IF NOT EXISTS cpf TEXT;
ALTER TABLE colaboradores ADD COLUMN IF NOT EXISTS data_nascimento TEXT;

CREATE TABLE IF NOT EXISTS fornecedores (
    id                     SERIAL PRIMARY KEY,
    nome                   TEXT NOT NULL UNIQUE,
    razao_social           TEXT,
    cnpj                   TEXT UNIQUE,
    inscricao_estadual     TEXT,
    contato_nome           TEXT,
    telefone               TEXT,
    email                  TEXT,
    endereco               TEXT,
    cidade_uf              TEXT,
    cep                    TEXT,
    prazo_entrega_dias     INTEGER DEFAULT 15,
    condicao_pagamento     TEXT,
    categorias_fornecidas  TEXT,
    observacao             TEXT,
    ativo                  INTEGER NOT NULL DEFAULT 1,
    criado_em              TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_fornecedores_nome_lower ON fornecedores (LOWER(nome));

CREATE TABLE IF NOT EXISTS pedidos (
    id                     SERIAL PRIMARY KEY,
    origem_id              TEXT UNIQUE,
    solicitante            TEXT NOT NULL,
    local_destino          TEXT,
    motivo                 TEXT,
    observacao             TEXT,
    colaborador_id         INTEGER REFERENCES colaboradores (id),
    status                 TEXT NOT NULL DEFAULT 'novo'
                           CHECK (status IN ('novo', 'pendente', 'em_separacao', 'pronto', 'retirado', 'cancelado')),
    dados_brutos           TEXT,
    recebido_em            TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS')),
    iniciado_por           INTEGER REFERENCES usuarios (id),
    iniciado_em            TEXT,
    finalizado_por         INTEGER REFERENCES usuarios (id),
    finalizado_em          TEXT,
    ressalva_observacao    TEXT,
    pedido_origem_id       INTEGER REFERENCES pedidos (id),
    grupo                  TEXT,
    retirado_por_nome      TEXT,
    retirado_descricao     TEXT,
    retirado_em            TEXT,
    entregue_por           INTEGER REFERENCES usuarios (id),
    cancelado_motivo       TEXT,
    cancelado_por          INTEGER REFERENCES usuarios (id),
    cancelado_em           TEXT,
    solicitante_usuario_id INTEGER REFERENCES usuarios (id),
    solicitante_email      TEXT
);

CREATE TABLE IF NOT EXISTS movimentacoes (
    id                 SERIAL PRIMARY KEY,
    grupo              TEXT NOT NULL,
    tipo               TEXT NOT NULL CHECK (tipo IN ('entrada', 'saida', 'baixa', 'ajuste_mais', 'ajuste_menos')),
    epi_id             INTEGER NOT NULL REFERENCES epis (id),
    quantidade         INTEGER NOT NULL CHECK (quantidade > 0),
    data               TEXT NOT NULL,
    colaborador_id     INTEGER REFERENCES colaboradores (id),
    setor              TEXT,
    ca                 TEXT,
    fornecedor         TEXT,
    nota_fiscal        TEXT,
    custo_unitario     REAL,
    motivo             TEXT,
    observacao         TEXT,
    pedido_id          INTEGER REFERENCES pedidos (id),
    local_destino      TEXT,
    usuario_id         INTEGER NOT NULL REFERENCES usuarios (id),
    criado_em          TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS')),
    cancelada          INTEGER NOT NULL DEFAULT 0,
    cancelada_motivo   TEXT,
    cancelada_por      INTEGER REFERENCES usuarios (id),
    cancelada_em       TEXT
);

CREATE INDEX IF NOT EXISTS idx_mov_epi ON movimentacoes (epi_id, cancelada);
CREATE INDEX IF NOT EXISTS idx_mov_data ON movimentacoes (data);
CREATE INDEX IF NOT EXISTS idx_mov_colab ON movimentacoes (colaborador_id);
CREATE INDEX IF NOT EXISTS idx_mov_grupo ON movimentacoes (grupo);
CREATE INDEX IF NOT EXISTS idx_mov_pedido ON movimentacoes (pedido_id);

CREATE TABLE IF NOT EXISTS configuracoes (
    chave  TEXT PRIMARY KEY,
    valor  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pedido_itens (
    id              SERIAL PRIMARY KEY,
    pedido_id       INTEGER NOT NULL REFERENCES pedidos (id),
    coluna          TEXT NOT NULL,
    texto_original  TEXT NOT NULL,
    quantidade      INTEGER NOT NULL DEFAULT 1 CHECK (quantidade > 0),
    epi_id          INTEGER REFERENCES epis (id),
    separado        INTEGER NOT NULL DEFAULT 0,
    ignorado        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS epi_apelidos (
    chave   TEXT PRIMARY KEY,
    epi_id  INTEGER REFERENCES epis (id)
);

CREATE INDEX IF NOT EXISTS idx_pedidos_status ON pedidos (status);
CREATE INDEX IF NOT EXISTS idx_pedido_itens_pedido ON pedido_itens (pedido_id);

ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS ressalva_observacao TEXT;
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS pedido_origem_id INTEGER REFERENCES pedidos (id);
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS solicitante_usuario_id INTEGER REFERENCES usuarios (id);
ALTER TABLE pedidos ADD COLUMN IF NOT EXISTS solicitante_email TEXT;

CREATE TABLE IF NOT EXISTS cargo_epis_padrao (
    cargo          TEXT NOT NULL,
    epi_id         INTEGER NOT NULL,
    nome_generico  TEXT,
    criado_em      TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS')),
    PRIMARY KEY (cargo, epi_id)
);

CREATE TABLE IF NOT EXISTS colaborador_epis_padrao (
    colaborador_id  INTEGER NOT NULL REFERENCES colaboradores (id),
    epi_id          INTEGER NOT NULL REFERENCES epis (id),
    criado_em       TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS')),
    PRIMARY KEY (colaborador_id, epi_id)
);

CREATE TABLE IF NOT EXISTS colaborador_epis_solicitados (
    id              SERIAL PRIMARY KEY,
    colaborador_id  INTEGER NOT NULL REFERENCES colaboradores (id),
    epi_id          INTEGER NOT NULL REFERENCES epis (id),
    quantidade      INTEGER NOT NULL DEFAULT 1 CHECK (quantidade > 0),
    data            TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD')),
    origem_chave    TEXT UNIQUE,
    observacao      TEXT,
    usuario_id      INTEGER REFERENCES usuarios (id),
    criado_em       TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS'))
);

CREATE INDEX IF NOT EXISTS idx_colab_solicitados ON colaborador_epis_solicitados (colaborador_id);

CREATE TABLE IF NOT EXISTS colaborador_tamanhos (
    colaborador_id  INTEGER NOT NULL REFERENCES colaboradores (id),
    chave           TEXT NOT NULL,
    tamanho         TEXT NOT NULL,
    atualizado_em   TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS')),
    PRIMARY KEY (colaborador_id, chave)
);

CREATE TABLE IF NOT EXISTS base_caepi (
    ca                  TEXT PRIMARY KEY,
    validade_ca         TEXT,
    situacao            TEXT,
    nome_equipamento    TEXT,
    descricao           TEXT,
    categoria           TEXT,
    unidade             TEXT NOT NULL DEFAULT 'un',
    fabricante          TEXT,
    cnpj_fabricante     TEXT,
    referencia          TEXT,
    atualizado_em       TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS'))
);

-- Obras (local de entrega do formulário). Só as ativas aparecem no formulário.
CREATE TABLE IF NOT EXISTS obras (
    id          SERIAL PRIMARY KEY,
    nome        TEXT NOT NULL UNIQUE,
    cidade      TEXT,
    endereco    TEXT,
    mapa        TEXT,
    observacao  TEXT,
    ativo       INTEGER NOT NULL DEFAULT 1,
    criado_em   TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_obras_nome_lower ON obras (LOWER(nome));

CREATE TABLE IF NOT EXISTS recuperacao_senha (
    id              SERIAL PRIMARY KEY,
    usuario_id      INTEGER NOT NULL REFERENCES usuarios (id),
    token_hash      TEXT NOT NULL UNIQUE,
    codigo_hash     TEXT NOT NULL,
    expira_em       TEXT NOT NULL,
    usado_em        TEXT,
    ip_solicitante  TEXT,
    criado_em       TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS'))
);

CREATE INDEX IF NOT EXISTS idx_rec_senha_usuario ON recuperacao_senha (usuario_id);

CREATE TABLE IF NOT EXISTS emails_enviados (
    id             SERIAL PRIMARY KEY,
    destinatarios  TEXT NOT NULL,
    assunto        TEXT NOT NULL,
    tipo           TEXT NOT NULL DEFAULT 'geral',
    status         TEXT NOT NULL DEFAULT 'enviado',
    erro           TEXT,
    criado_em      TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS'))
);


-- Inscrições de notificações Web Push Mobile
CREATE TABLE IF NOT EXISTS push_inscricoes (
    id          SERIAL PRIMARY KEY,
    usuario_id  INTEGER REFERENCES usuarios (id) ON DELETE CASCADE,
    endpoint    TEXT NOT NULL UNIQUE,
    p256dh      TEXT NOT NULL,
    auth        TEXT NOT NULL,
    user_agent  TEXT,
    apelido     TEXT,
    criado_em   TEXT NOT NULL DEFAULT to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS')
);
ALTER TABLE push_inscricoes ADD COLUMN IF NOT EXISTS apelido TEXT;

CREATE INDEX IF NOT EXISTS idx_push_usuario ON push_inscricoes (usuario_id);

-- Ficha de EPI da retirada de um pedido, assinada pelo colaborador que retirou (WhatsApp ou papel)
CREATE TABLE IF NOT EXISTS fichas_epi (
    id                  SERIAL PRIMARY KEY,
    pedido_id           INTEGER REFERENCES pedidos (id),
    colaborador_id      INTEGER NOT NULL REFERENCES colaboradores (id),  -- quem retirou e assina
    beneficiario_id     INTEGER REFERENCES colaboradores (id),           -- para quem o pedido foi feito
    status              TEXT NOT NULL DEFAULT 'aguardando'
                        CHECK (status IN ('aguardando', 'assinada', 'assinada_papel')),
    documento           TEXT NOT NULL,   -- JSON com os dados e itens congelados no momento da retirada
    documento_hash      TEXT NOT NULL,   -- SHA-256 do documento, mostrado junto da assinatura
    token_hash          TEXT UNIQUE,     -- link enviado pelo WhatsApp (guardamos só o hash)
    link_expira_em      TEXT,
    codigo_hash         TEXT,
    codigo_expira_em    TEXT,
    codigo_tentativas   INTEGER NOT NULL DEFAULT 0,
    codigo_enviado_em   TEXT,
    whatsapp_numero     TEXT,
    whatsapp_enviado_em TEXT,
    assinatura          TEXT,            -- imagem PNG (data URL) da assinatura
    assinado_em         TEXT,
    assinado_ip         TEXT,
    assinado_aparelho   TEXT,
    anexo_arquivo       TEXT,            -- foto/PDF da ficha assinada em papel, em instance/fichas/
    anexo_nome          TEXT,
    papel_por           INTEGER REFERENCES usuarios (id),
    criado_por          INTEGER NOT NULL REFERENCES usuarios (id),
    criado_em           TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS'))
);

CREATE INDEX IF NOT EXISTS idx_fichas_colab ON fichas_epi (colaborador_id);
CREATE INDEX IF NOT EXISTS idx_fichas_benef ON fichas_epi (beneficiario_id);
CREATE INDEX IF NOT EXISTS idx_fichas_pedido ON fichas_epi (pedido_id);

-- Modelos de documento (ex.: ficha de EPI): layout fixo, com textos, campos e colunas configuráveis
CREATE TABLE IF NOT EXISTS modelos_documento (
    id            SERIAL PRIMARY KEY,
    nome          TEXT NOT NULL,
    tipo          TEXT NOT NULL DEFAULT 'ficha_epi',
    config        TEXT NOT NULL,   -- JSON com os parâmetros do modelo
    padrao        INTEGER NOT NULL DEFAULT 0,  -- usado automaticamente na retirada de pedidos
    ativo         INTEGER NOT NULL DEFAULT 1,
    criado_por    INTEGER REFERENCES usuarios (id),
    criado_em     TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS')),
    atualizado_em TEXT
);

-- Itens genéricos do formulário criados pela tela (somam-se às listas fixas de pedidos.py, na ordem do id)
CREATE TABLE IF NOT EXISTS itens_genericos_formulario (
    id        SERIAL PRIMARY KEY,
    nome      TEXT NOT NULL UNIQUE,
    tipo      TEXT NOT NULL DEFAULT 'epi' CHECK (tipo IN ('epi', 'uniforme')),
    criado_em TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS'))
);
