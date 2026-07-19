# Corridas ETL

Pipeline de dados (ETL) que agrega eventos de corrida de rua de múltiplas
organizadoras em uma base canônica única para uma plataforma de descoberta
de corridas.

## Visão geral

O sistema coleta eventos publicados por diferentes organizadoras e
agregadores, normaliza essas informações em um formato único e as
disponibiliza para busca em uma aplicação de descoberta de corridas.

## Arquitetura

O pipeline segue uma arquitetura em camadas (medallion architecture):

```
Fontes  →  Extração  →  Bronze (raw)  →  Transformação  →  Silver  →  Resolução + Enriquecimento  →  Gold  →  Serving
```

- **Bronze**: dado bruto como recebido da fonte, preservado para
  auditoria e reprocessamento.
- **Silver**: dado parseado e normalizado por fonte.
- **Gold**: evento canônico, já deduplicado entre fontes.
- **Serving**: camada de consulta/busca usada pela aplicação.

O pipeline inclui, em alto nível, etapas de extração por conector,
resolução de entidades (deduplicação entre fontes) e detecção de
mudanças (preço/status de inscrição) para notificação aos usuários.

## Fontes agregadas

O pipeline agrega eventos das seguintes fontes:

- Ticket Sports
- Iguana Sports
- Yescom
- Running Land
- Ativo.com
- LIVE! Run XP
- TFSports

## Stack técnica

Python, PostgreSQL (com suporte opcional a PostGIS) e Playwright para
coleta de dados dinâmicos.

## Licença e uso

Este é um projeto proprietário. Todo o código-fonte, documentação e
demais artefatos deste repositório são de propriedade do autor. Não é
permitido copiar, redistribuir, sublicenciar ou utilizar este código,
no todo ou em parte, sem autorização prévia e por escrito. Veja o
arquivo [LICENSE](LICENSE) para detalhes.
