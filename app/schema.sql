CREATE TABLE IF NOT EXISTS usuarios (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    nome        TEXT NOT NULL,
    login       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    senha_hash  TEXT NOT NULL,
    perfil      TEXT NOT NULL CHECK (perfil IN ('admin', 'almoxarifado', 'consulta')),
    ativo       INTEGER NOT NULL DEFAULT 1,
    criado_em   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS epis (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    nome                  TEXT NOT NULL UNIQUE COLLATE NOCASE,
    categoria             TEXT,
    ca                    TEXT,
    validade_ca           TEXT,
    unidade               TEXT NOT NULL DEFAULT 'un',
    estoque_minimo        INTEGER NOT NULL DEFAULT 0 CHECK (estoque_minimo >= 0),
    prazo_reposicao_dias  INTEGER NOT NULL DEFAULT 15 CHECK (prazo_reposicao_dias >= 0),
    ativo                 INTEGER NOT NULL DEFAULT 1,
    criado_em             TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS colaboradores (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    nome       TEXT NOT NULL,
    matricula  TEXT UNIQUE,
    setor      TEXT,
    funcao     TEXT,
    ativo      INTEGER NOT NULL DEFAULT 1,
    criado_em  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Livro-razão do estoque: nunca se apaga, só se cancela (cancelada = 1).
-- saldo = entradas + ajuste_mais - saidas - baixas - ajuste_menos
CREATE TABLE IF NOT EXISTS movimentacoes (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    grupo              TEXT NOT NULL,   -- agrupa os itens de uma mesma entrega/entrada
    tipo               TEXT NOT NULL CHECK (tipo IN ('entrada', 'saida', 'baixa', 'ajuste_mais', 'ajuste_menos')),
    epi_id             INTEGER NOT NULL REFERENCES epis (id),
    quantidade         INTEGER NOT NULL CHECK (quantidade > 0),
    data               TEXT NOT NULL,   -- AAAA-MM-DD (data efetiva da movimentação)
    colaborador_id     INTEGER REFERENCES colaboradores (id),
    setor              TEXT,            -- setor do colaborador na data da entrega
    ca                 TEXT,            -- CA do EPI na data da movimentação
    fornecedor         TEXT,
    nota_fiscal        TEXT,
    custo_unitario     REAL,
    motivo             TEXT,
    observacao         TEXT,
    pedido_id          INTEGER REFERENCES pedidos (id),
    local_destino      TEXT,            -- obra/local para onde o EPI foi levado
    usuario_id         INTEGER NOT NULL REFERENCES usuarios (id),
    criado_em          TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    cancelada          INTEGER NOT NULL DEFAULT 0,
    cancelada_motivo   TEXT,
    cancelada_por      INTEGER REFERENCES usuarios (id),
    cancelada_em       TEXT
);

CREATE INDEX IF NOT EXISTS idx_mov_epi ON movimentacoes (epi_id, cancelada);
CREATE INDEX IF NOT EXISTS idx_mov_data ON movimentacoes (data);
CREATE INDEX IF NOT EXISTS idx_mov_colab ON movimentacoes (colaborador_id);
CREATE INDEX IF NOT EXISTS idx_mov_grupo ON movimentacoes (grupo);

CREATE TABLE IF NOT EXISTS configuracoes (
    chave  TEXT PRIMARY KEY,
    valor  TEXT NOT NULL
);

-- Pedidos de EPI vindos do Microsoft Forms (ou lançados à mão).
-- Fluxo: novo -> (pendente) -> em_separacao -> pronto -> retirado   |   cancelado
CREATE TABLE IF NOT EXISTS pedidos (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    origem_id           TEXT UNIQUE,     -- id da resposta no Forms (evita duplicar pedidos)
    solicitante         TEXT NOT NULL,   -- "Nome" do formulário
    local_destino       TEXT,
    motivo              TEXT,
    observacao          TEXT,
    colaborador_id      INTEGER REFERENCES colaboradores (id),
    status              TEXT NOT NULL DEFAULT 'novo'
                        CHECK (status IN ('novo', 'pendente', 'em_separacao', 'pronto', 'retirado', 'cancelado')),
    dados_brutos        TEXT,            -- JSON recebido, para auditoria
    recebido_em         TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    iniciado_por        INTEGER REFERENCES usuarios (id),
    iniciado_em         TEXT,
    finalizado_por      INTEGER REFERENCES usuarios (id),
    finalizado_em       TEXT,
    grupo               TEXT,            -- grupo das movimentações geradas na finalização
    retirado_por_nome   TEXT,
    retirado_descricao  TEXT,
    retirado_em         TEXT,
    entregue_por        INTEGER REFERENCES usuarios (id),
    cancelado_motivo    TEXT,
    cancelado_por       INTEGER REFERENCES usuarios (id),
    cancelado_em        TEXT
);

CREATE TABLE IF NOT EXISTS pedido_itens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    pedido_id       INTEGER NOT NULL REFERENCES pedidos (id),
    coluna          TEXT NOT NULL,       -- campo do formulário (EPI, Sapatão, Camiseta...)
    texto_original  TEXT NOT NULL,
    quantidade      INTEGER NOT NULL DEFAULT 1 CHECK (quantidade > 0),
    epi_id          INTEGER REFERENCES epis (id),   -- NULL = ainda não identificado
    separado        INTEGER NOT NULL DEFAULT 0,
    ignorado        INTEGER NOT NULL DEFAULT 0
);

-- Aprendizado: "Camiseta|m" -> EPI "Camiseta M". epi_id NULL = ignorar sempre.
CREATE TABLE IF NOT EXISTS epi_apelidos (
    chave   TEXT PRIMARY KEY,
    epi_id  INTEGER REFERENCES epis (id)
);

CREATE INDEX IF NOT EXISTS idx_pedidos_status ON pedidos (status);
CREATE INDEX IF NOT EXISTS idx_pedido_itens_pedido ON pedido_itens (pedido_id);

-- Equipamentos utilizados por padrão por cargo/função (marcados via checkbox a partir do inventário)
CREATE TABLE IF NOT EXISTS cargo_epis_padrao (
    cargo      TEXT NOT NULL COLLATE NOCASE,
    epi_id     INTEGER NOT NULL REFERENCES epis (id),
    criado_em  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (cargo, epi_id)
);

-- Fallback individual quando o colaborador ainda não possui cargo definido
CREATE TABLE IF NOT EXISTS colaborador_epis_padrao (
    colaborador_id  INTEGER NOT NULL REFERENCES colaboradores (id),
    epi_id          INTEGER NOT NULL REFERENCES epis (id),
    criado_em       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    PRIMARY KEY (colaborador_id, epi_id)
);

-- Equipamentos solicitados fora do padrão do cargo e suas observações/justificativas
CREATE TABLE IF NOT EXISTS colaborador_epis_solicitados (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    colaborador_id  INTEGER NOT NULL REFERENCES colaboradores (id),
    epi_id          INTEGER NOT NULL REFERENCES epis (id),
    quantidade      INTEGER NOT NULL DEFAULT 1 CHECK (quantidade > 0),
    data            TEXT NOT NULL DEFAULT (date('now', 'localtime')),
    origem_chave    TEXT UNIQUE,   -- 'pedido_item:ID' ou 'mov:ID' quando vem automático; NULL quando lançado manualmente
    observacao      TEXT,          -- descrição do motivo (teste em outro cargo, promoção, atividade específica...)
    usuario_id      INTEGER REFERENCES usuarios (id),
    criado_em       TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_colab_solicitados ON colaborador_epis_solicitados (colaborador_id);
