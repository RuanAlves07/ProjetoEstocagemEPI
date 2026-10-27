#  Sistema de Gestão de Estoque e Pedidos de EPIs e Uniformes

Aplicação web completa (**Web Desktop em tela inteira + Web Mobile responsiva**) para controle de inventário de EPIs e uniformes, formulário público de solicitação para obras, fila de separação estilo comanda, padronização de kits por cargo, fichas de colaboradores, gestão de fornecedores e controle granular de perfis de acesso.

Compatível tanto com **SQLite local** (`instance/epi.sqlite`) quanto com **PostgreSQL (Supabase)** + deploy serverless na **Vercel**.

---

##  Principais Funcionalidades

### 1. Formulário Público de Solicitação (`/solicitar`)
- **Acesso público (sem necessidade de login)**: pode ser disponibilizado via link direto ou QR Code para os colaboradores nas obras pelo celular ou computador.
- **Data automática**: a data e hora do pedido são registradas automaticamente no momento em que o colaborador finaliza o envio.
- **Filtro de nome**: na pergunta 1, um campo de busca filtra a lista de colaboradores enquanto a pessoa digita (sem diferenciar acentos e maiúsculas). Quando sobra um único nome, ele já é selecionado.
- **Local de entrega por obra**: a pergunta 2 mostra somente o **nome das obras ativas** (cadastradas na aba **Obras**), em uma lista única. O pedido grava o nome da obra como local de entrega, e só são aceitas obras ativas.
- **Somente os EPIs do cargo**: ao selecionar o nome do colaborador, o formulário mostra **apenas** os equipamentos do kit padrão do cargo dele (definido em `/colaboradores/cargos`; se o colaborador não tiver cargo, vale o padrão individual da ficha). O servidor também recusa qualquer item fora desse padrão, nomes que não estão na lista de colaboradores e itens que não existem no formulário. Para pedir algo fora da função, o colaborador procura o gestor.
- **Seletores dinâmicos e condicionais de numeração e tamanho**:
  - **Calçados (`Sapatão`, `Botina` e `Bota`)**: abrem abas numéricas independentes (restritas a **somente números**) exclusivamente para os tipos de calçado marcados no pedido.
  - **Uniformes (`Camiseta` e `Calça`)**: abrem seletores de tamanho nos padrões **`M, G, GG, G1, G2, G3, G4 e G5`** somente quando camiseta ou calça são selecionadas.

### 1.1. Formulário do Gestor (`/solicitar/gestor`)
- **Login separado do gestor (`/login/gestor`)**: na tela de login, abaixo do card *Solicitação de Uniforme e EPI*, fica o card **Solicitação de Uniforme e EPI (Gestor)**, que abre uma tela de login própria do gestor. Só entra quem tem a permissão **`Formulário do Gestor (fora da função)`**: ela vem no perfil **Gestor** (criado automaticamente) e no Administrador, e pode ser liberada em qualquer perfil personalizado.
- Quem tem o perfil Gestor vê **somente** o formulário do gestor (sem os menus do sistema) e, ao sair, volta para o login do gestor. Para cadastrar um gestor: **Usuários → + Novo usuário → Perfil "Gestor"**.
- Mostra **todos** os EPIs e vestimentas do formulário: os do cargo do colaborador ficam em verde **`(recomendado pelo cargo)`** e os demais recebem a etiqueta **`(fora da função)`**.
- Ao marcar algum item fora da função, o campo **Justificativa** passa a ser obrigatório.
- O pedido entra na mesma fila do almoxarifado, com a observação **`[Solicitado pelo gestor: Nome]`** e **`[Fora da função: itens — Justificativa: ...]`**, que também aparece na aba *Equipamentos solicitados* da ficha do colaborador.
- Atalho no topo do formulário público (**👔 Formulário do Gestor**) e na aba *Formulário* do sistema.

### 2. Colaboradores e Padronização por Cargo (`/colaboradores` e `/colaboradores/cargos`)
- **Padrão de EPIs por Cargo (`/colaboradores/cargos`)**:
  - Lista todos os cargos cadastrados com a quantidade de colaboradores e de equipamentos vinculados.
  - Ao clicar sobre o nome de um cargo, abre a lista de equipamentos do inventário para marcar/desmarcar o kit padrão daquela função (a definição de quais produtos compõem o kit padrão é feita exclusivamente no Cargo).
- **Perfil Individual do Colaborador (`/colaboradores/<id>`)**:
  - **Aba `Equipamentos utilizados por padrão`**: exibe somente os equipamentos que pertencem ao padrão do cargo do colaborador e traz a coluna **`Tamanho / Numeração`**, permitindo deixar gravados no perfil os números de **Bota, Sapatão e Botina** (`Nº 33` a `Nº 48`) e as letras de **Camiseta e Calça** (`M` a `G5`).
  - **Aba `Equipamentos solicitados`**: lista tudo o que o colaborador já pediu ou recebeu fora do padrão do cargo dele (ex.: período de teste em outra função para possível promoção ou atividade específica na obra), com campo de **Observação** para registrar o motivo.
  - **Aba `Ficha de EPIs entregues`**: histórico completo de entregas com geração de comprovante para impressão e assinatura.

### 2.1. Obras (`/obras`)
- Aba ao lado de **Colaboradores** com o cadastro das obras: **Nome**, **Cidade**, **Endereço**, **Link do mapa (Google Maps)** e **Observações**, além da quantidade de pedidos de cada obra.
- **Ativa / Inativa**: obra ativa aparece automaticamente no formulário de solicitação; obra inativa some do formulário, mas continua no cadastro e no histórico de pedidos. O botão **Ativar/Inativar** fica direto na lista.
- Endereço e mapa são só para uso interno: no formulário aparece apenas o nome da obra.
- Na primeira inicialização, as 19 obras atuais são cadastradas automaticamente.
- Usa as permissões de **Colaboradores, Cargos e Obras** (visualizar / editar).

### 3. Gestão de Fornecedores (`/fornecedores`)
- Cadastro completo de fornecedores de EPIs e uniformes: **Nome Fantasia**, **Razão Social**, **CNPJ**, **Inscrição Estadual**, **Vendedor/Contato**, **Telefone/WhatsApp**, **E-mail**, **Endereço completo**, **Cidade/UF**, **CEP**, **Prazo médio de entrega**, **Condição de pagamento**, **Categorias fornecidas** e **Observações**.
- Ficha detalhada do fornecedor (`/fornecedores/<id>`) com indicadores de notas fiscais registradas, total de unidades compradas, valor acumulado em entradas, histórico de compras e atalho direto para registrar nova entrada de estoque daquele fornecedor.

### 4. Fluxo de Pedidos (Comanda → Separação → Baixa → Retirada) (`/pedidos`)
1. **Chegada do pedido**: pedidos enviados em `/solicitar` entram imediatamente na fila com alerta visual e sonoro no canto da tela.
2. **Em separação / Pendentes**: o almoxarifado pode iniciar a separação imediatamente ou guardar o pedido em *Pendentes (para mais tarde)*.
3. **Checklist de separação e baixa automática**: ao marcar todos os itens separados e clicar em **Finalizar pedido**, o sistema valida o saldo e o CA, dá **baixa automática no estoque** e muda o status para **Pronto para retirada**.
4. **Registro de retirada e comprovante**: registra quem retirou (próprio colaborador, encarregado ou portador), data/hora e emite o comprovante de entrega.

### 5. Painel em Tela Inteira, Estoque e Relatórios (`/`, `/epis`, `/movimentos`, `/relatorios`)
- **Painel (`/`)**: layout distribuído em tela inteira com 7 indicadores em tempo real, tabela **Hora de pedir** (sugestão automática de compra baseada no estoque mínimo e cobertura em dias), ranking **Mais saem**, **CAs para atualizar**, **Pedidos em aberto** e **Últimas movimentações**.
- **Livro-razão auditável (`/movimentos`)**: entradas com NF e fornecedor, entregas a colaboradores, baixas por avaria/perda e ajustes de inventário. Lançamentos nunca são apagados (apenas cancelados/estornados mediante justificativa).
- **Relatórios e Exportação CSV (`/relatorios`)**: análises por período, setor, obra/local de destino, colaborador e evolução mensal, além de exportação CSV compatível com Excel.

### 6. Usuários, Perfis Customizáveis e Permissões (`/usuarios`)
- Dividido em duas abas limpas (acesso exclusivo do **Administrador**):
  - **`Usuários Cadastrados`**: listagem de usuários com possibilidade de criar novas contas e editar **Nome**, **Login de acesso**, **Perfil**, **Senha** e **Situação (Ativo/Inativo)**.
  - **`Perfis e Permissão`**: lista todos os perfis criados. Ao clicar sobre o nome de um perfil (ou criar um **+ Novo perfil**), abre a tela de customização com **Nome do perfil**, **Descrição do que faz** e os checkboxes para liberar ou bloquear o acesso a cada aba e ação do sistema.

---

##  Como Rodar Localmente

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # Windows: .venv\Scripts\pip install -r requirements.txt
.venv/bin/python run.py                       # Abre em http://localhost:5000
```

- **Popular base de demonstração** (cadastra os 33 itens reais entre EPIs e vestimentas, 9 cargos padronizados, 3 fornecedores, 158 colaboradores das obras e pedidos de exemplo, preservando seus usuários):
  ```bash
  .venv/bin/python popular_teste.py
  ```
- **Executar a suíte de testes automatizados**:
  ```bash
  .venv/bin/python -m unittest discover -s tests -v
  ```

---

##  Deploy em Produção (Supabase + Vercel)

O projeto já possui configuração nativa para rodar em nuvem com **Supabase (PostgreSQL)** e **Vercel**:

1. **Banco de Dados (Supabase)**:
   - Crie um projeto no [Supabase](https://supabase.com/) e copie a **Connection String (URI)** do *Session Pooler* (porta `5432`) ou *Transaction Pooler* (porta `6543`).
   - As tabelas são criadas automaticamente a partir de [`app/schema_postgres.sql`](app/schema_postgres.sql) (ou você pode popular o banco remoto executando `.venv/bin/python popular_teste.py "postgresql://..."`).
2. **Aplicação Web (Vercel)**:
   -Os arquivos [`vercel.json`](vercel.json) e [`api/index.py`](api/index.py) já configuram o ambiente serverless Python.
   - Ao importar o repositório na [Vercel](https://vercel.com/), defina em **Environment Variables**:
     - `DATABASE_URL`: URI de conexão PostgreSQL do seu projeto no Supabase.
     - `EPI_SECRET_KEY`: chave secreta para assinatura das sessões de login.

---

##  Estrutura do Projeto

```text
├── api/
│   └── index.py               # Entrypoint WSGI para deploy serverless na Vercel
├── app/
│   ├── __init__.py            # Factory Flask, filtros Jinja e configuração híbrida SQLite/Postgres
│   ├── auth.py                # Autenticação, CSRF, perfis dinâmicos e controle de permissões por rota
│   ├── cadastros.py           # Rotas de EPIs, Fornecedores, Colaboradores, Cargos e Usuários/Perfis
│   ├── db.py                  # Camada de banco híbrida (SQLite3 + adaptador PostgreSQL/Supabase)
│   ├── estoque.py             # Regras de negócio de saldo, cobertura, validade de CA e movimentações
│   ├── movimentos.py          # Entradas, entregas, baixas, ajustes e comprovantes
│   ├── painel.py              # Painel em tela inteira e relatórios gerenciais
│   ├── pedidos.py             # Serviço de pedidos, matching inteligente de itens e fluxo de separação
│   ├── rotas_pedidos.py       # Rotas da fila de pedidos e do formulário público (/solicitar)
│   ├── schema.sql             # Schema SQLite
│   ├── schema_postgres.sql    # Schema PostgreSQL (Supabase)
│   ├── static/                # CSS responsivo (Web Desktop + Web Mobile) e JavaScript
│   └── templates/             # Templates HTML Jinja2
├── tests/                     # Testes automatizados (estoque, acessos, perfis, fornecedores e pedidos)
├── popular_teste.py           # Script de carga inicial com os 33 itens, 9 cargos e 158 colaboradores
├── requirements.txt           # Dependências Python (Flask, psycopg2-binary)
├── run.py                     # Inicializador local de desenvolvimento / rede interna
└── vercel.json                # Configuração de rotas para a Vercel
```
