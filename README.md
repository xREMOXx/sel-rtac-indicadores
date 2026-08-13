# Indicadores — Atuação de equipamentos da rede de distribuição

Painel web do histórico de aberturas de religadores, disjuntores e chaves,
lido da API REST do RTAC SEL. Somente leitura: só faz `GET`, nunca escreve
nem comanda equipamento.

## Rodar

```
python historico_aberturas.py     # painel real   -> http://localhost:8422
python mock_aberturas.py          # dados falsos  -> http://localhost:8423
python test_pareamento.py         # 6 checagens do pareamento abre/fecha
```

O processo morre junto com o terminal que o iniciou.

## Arquivos de cada instalação

Nada específico de uma rede está no código. Três arquivos ficam de fora do
versionamento e você cria os seus:

| Arquivo | Do quê | Sem ele |
|---|---|---|
| `.env` | endereço e credencial do RTAC | não sobe — copie de `.env.example` |
| `nomes_equipamento.json` | apelido/alimentador de cada equipamento | painel mostra só o código (`RL-01`) |
| `logo.png` | marca da distribuidora, embutida na página como data URI | página sobe sem logo e sem favicon |

Rodando pelo `.exe`, os três ficam **ao lado do executável**, não dentro dele.

`sel_rtac_scraper.py` e o `.env` compartilhado ficam na **raiz do projeto**, um
nível acima, junto com `live_138kv.py` e `eventos_abertura.py`. Não há cópia
deles aqui de propósito — credencial duplicada é credencial esquecida.

## Conta de serviço

Crie uma conta **dedicada e exclusiva** para o painel. Não use conta de
operador nem de engenharia. O painel é somente leitura e a conta tem que
refletir isso: se a credencial vazar, o estrago possível é o que a conta
permite, não o que o código faz.

A API exige exatamente duas permissões para os endpoints deste painel:
`API_Login` e `Report_Read`. Em **User → User Roles**:

* **recomendado** — papel personalizado com apenas essas duas marcadas;
* **aceitável** — papel `Monitor` de fábrica, que funciona mas carrega
  `Reboot_Device` junto: quem tiver a senha reinicia o RTAC.

O papel `File Transfer` **não serve**: tem `Report_Read` mas não tem
`API_Login`, então toda requisição volta `401`. É acesso por SFTP, não por API.

Senha própria, diferente do nome de usuário e não reaproveitada de outro
sistema — ela fica em texto puro no `.env`.

## Rede

Padrão do código: `BIND_HOST = "0.0.0.0"`. O painel **não tem autenticação**:
quem alcança a porta vê o inventário da subestação e o histórico completo de
interrupções. Trocar para `"127.0.0.1"` restringe o painel à máquina local.

Quem limita o alcance é a regra de firewall, e trocar o bind para `"0.0.0.0"`
**não basta** para publicar na rede. Duas coisas costumam derrubar o acesso de
fora, e o sintoma das duas é o mesmo — o servidor sobe, responde em
`localhost` e recusa conexão vinda de outra máquina:

* regra de **bloqueio** de entrada para `python.exe` (ou para o `.exe` do
  painel) no perfil ativo;
* a placa classificada como **Public**, perfil onde essas regras de bloqueio
  normalmente vivem.

Descubra o seu caso antes de mexer:

```powershell
Get-NetConnectionProfile                       # qual placa, qual perfil
Get-NetIPAddress -AddressFamily IPv4           # endereços e sub-redes locais
Get-NetFirewallRule -Action Block -Enabled True -Direction Inbound |
  Where-Object DisplayName -match 'python|Painel'
```

Publicando, restrinja à sub-rede da operação em vez de liberar geral —
substitua a sub-rede e o perfil pelos que apareceram acima:

```powershell
New-NetFirewallRule -DisplayName "Painel Aberturas 8422" -Direction Inbound `
  -Action Allow -Protocol TCP -LocalPort 8422 `
  -RemoteAddress 10.0.0.0/24 -Profile Private
```

Máquina com mais de uma placa é o caso comum aqui: uma na rede corporativa,
outra na rede de automação onde vive o RTAC. Vale conferir por qual rota o
RTAC responde (`Test-NetConnection <host> -Port 443`, `route print`) — placa
desconectada ou endereço de outra faixa dá timeout que parece erro de
credencial.

## Decisões que não são óbvias no código

**Pareamento.** Uma abertura é o evento `Alarmed` casado com o `Normalized`
seguinte da mesma tag. `Acknowledged` não conta: reconhecer um alarme gera
registro novo com a mesma `Message`, e usá-la contaria reconhecimento como
abertura. `Alarmed` repetido enquanto o ponto já está aberto é ignorado — o
religador precisa fechar para abrir de novo, então são o mesmo evento gravado
duas vezes. Sem isso, um religador fechado aparecia como pendente de
fechamento.

**Nomes de alimentador.** Não existem na API de alarmes nem no
`dicionario_tags.csv`. Só no projeto de HMI, nos `DiagramTitle` no formato
`RL-01 (5009) - NOME DO ALIMENTADOR`. Vêm de
`GET /api/v1/hmi/projects/<SEU_PROJETO_HMI>`, um JSON com o XML do HMI em LZMA
*alone* dentro do campo `Contents`, em base64 — `GET /api/v1/hmi/projects`
lista os nomes de projeto do seu RTAC. Chaves de interligação, bancos de
capacitor e os disjuntores da subestação costumam não ter nome lá e aparecem
só com o código, sem quebrar nada. O resultado vai para
`nomes_equipamento.json`, no formato de `nomes_equipamento.exemplo.json`.

**Apelido só do cadastro.** O complemento do `Category` não vira nome:
`BC-02 CHAVE VÁCUO` mostraria "BC-02 VÁCUO", como se houvesse um alimentador
chamado Vácuo.

**Cores.** A paleta do `:root` é ponto de partida — troque os tokens `--brand*`
pelos da sua distribuidora. O verde-limão `--lime` fica fora das séries de
dados de propósito, e a regra vale para qualquer cor de marca que entre no
lugar: contra o verde da marca não alcança o piso de separação para visão
normal, e contra o laranja quebra no daltonismo deutan. É cor de marca, não de
dado. As séries 1-3 passam o validador de CVD nos dois modos; não trocar sem
revalidar.

**Nome do equipamento vai por `data-eq`, não por `onclick`.** Dentro de um
atributo `onclick` o nome vai escapado para HTML, e um equipamento com
apóstrofo ou `&` chegava ao handler como `O&#39;HIGGINS` — nunca casava com a
chave crua e a linha não expandia, sem erro no console.

**Erro da API não vai para o cliente.** `/data.json` manda só um booleano:
`str(exc)` do `requests` carrega a URL completa e entregaria o IP e a estrutura
da API do RTAC a qualquer visitante. O detalhe fica no log do servidor.

## Teste de carga com o tempo

`mock_aberturas.py` gera 7 anos de histórico sintético com semente fixa e o
passa pelo `_pair_open_close` **real**. Casos plantados: ano vazio no meio da
série, mês vazio, dia muito acima do topo da rampa de calor, duração de vários
dias, 29/02, primeiro e último dia do mês, pendência real, `Alarmed` duplicado,
equipamento sem cadastro, equipamento sem palavra de classe no `Category`, e
nome com apóstrofo e `&`.
