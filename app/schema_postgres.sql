-- Schema PostgreSQL (Supabase) para o Sistema de Controle de Estoque e Pedidos de EPIs
-- Pode ser executado direto no SQL Editor do Supabase ou automaticamente pelo sistema via DATABASE_URL.

CREATE TABLE IF NOT EXISTS usuarios (
    id          SERIAL PRIMARY KEY,
    nome        TEXT NOT NULL,
    login       TEXT NOT NULL UNIQUE,
    senha_hash  TEXT NOT NULL,
    perfil      TEXT NOT NULL,
    ativo       INTEGER NOT NULL DEFAULT 1,
    criado_em   TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS'))
);

ALTER TABLE usuarios DROP CONSTRAINT IF EXISTS usuarios_perfil_check;

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

ALTER TABLE epis ADD COLUMN IF NOT EXISTS tamanho TEXT;
ALTER TABLE epis ADD COLUMN IF NOT EXISTS imagem_url TEXT;
ALTER TABLE epis ADD COLUMN IF NOT EXISTS exibir_no_forms INTEGER NOT NULL DEFAULT 1;

ALTER TABLE epis DROP CONSTRAINT IF EXISTS epis_nome_key;
DROP INDEX IF EXISTS idx_epis_nome_lower;
CREATE UNIQUE INDEX IF NOT EXISTS idx_epis_nome_tamanho ON epis (LOWER(nome), COALESCE(tamanho, ''));

CREATE TABLE IF NOT EXISTS colaboradores (
    id         SERIAL PRIMARY KEY,
    nome       TEXT NOT NULL,
    matricula  TEXT UNIQUE,
    setor      TEXT,
    funcao     TEXT,
    ativo      INTEGER NOT NULL DEFAULT 1,
    criado_em  TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS'))
);

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
    id                  SERIAL PRIMARY KEY,
    origem_id           TEXT UNIQUE,
    solicitante         TEXT NOT NULL,
    local_destino       TEXT,
    motivo              TEXT,
    observacao          TEXT,
    colaborador_id      INTEGER REFERENCES colaboradores (id),
    status              TEXT NOT NULL DEFAULT 'novo'
                        CHECK (status IN ('novo', 'pendente', 'em_separacao', 'pronto', 'retirado', 'cancelado')),
    dados_brutos        TEXT,
    recebido_em         TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS')),
    iniciado_por        INTEGER REFERENCES usuarios (id),
    iniciado_em         TEXT,
    finalizado_por      INTEGER REFERENCES usuarios (id),
    finalizado_em       TEXT,
    ressalva_observacao TEXT,
    pedido_origem_id    INTEGER REFERENCES pedidos (id),
    grupo               TEXT,
    retirado_por_nome   TEXT,
    retirado_descricao  TEXT,
    retirado_em         TEXT,
    entregue_por        INTEGER REFERENCES usuarios (id),
    cancelado_motivo    TEXT,
    cancelado_por       INTEGER REFERENCES usuarios (id),
    cancelado_em        TEXT
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

CREATE TABLE IF NOT EXISTS cargo_epis_padrao (
    cargo      TEXT NOT NULL,
    epi_id     INTEGER NOT NULL REFERENCES epis (id),
    criado_em  TEXT NOT NULL DEFAULT (to_char(CURRENT_TIMESTAMP AT TIME ZONE 'America/Sao_Paulo', 'YYYY-MM-DD HH24:MI:SS')),
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
