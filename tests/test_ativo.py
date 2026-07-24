import json
from datetime import date, datetime, timedelta

import httpx
import pytest

from corridas_etl.connectors.ativo import AtivoConnector, _classify_pay_page
from corridas_etl.models import RawPayload, RegistrationStatus

_FUTURE = (date.today() + timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
_PAST = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
_FAR = (date.today() + timedelta(days=400)).strftime("%Y-%m-%d 00:00:00")

# Item reduzido do /eventos.json real (capturado 2026-07-19). Data no futuro
# para representar um evento vindouro. O status de inscricao NAO vem no dump: e
# anexado em fetch() como `_pay_status` a partir da pagina de inscricao.
_ITEM = {
    "id_evento": "40315",
    "post_title": "Corrida do Bem Eco - Marab&aacute;",
    "dt_evento": _FUTURE,
    "ds_cidade": "Marab&aacute;",
    "ds_estado": "PA",
    "nome_organizador": "Eco Eventos",
    "distancias": [
        {"ds_distancia": "5k"},
        {"ds_distancia": "10k"},
    ],
    "thumbnail": "https://media.ativo.com/upload/evento/40315/img_logo_evento.jpg",
    "post_json": "https://www.ativo.com/calendario/eventos/america-do-sul/br/pa/maraba/corrida-de-rua/40315/x/index.json",
    "fl_suspenso": 0,
    # fl_resultado=1 mesmo em provas futuras: NAO indica inscricao encerrada
    # (bug antigo). Deixado como 1 de proposito para travar a regressao.
    "fl_resultado": "1",
}


def _payload(item: dict) -> RawPayload:
    return RawPayload(
        source="ativo",
        source_event_id=str(item["id_evento"]),
        source_url="https://www.ativo.com/x",
        fetched_at=datetime(2026, 7, 19),
        content_type="application/json",
        body=json.dumps(item, ensure_ascii=False),
    )


def _conn() -> AtivoConnector:
    return AtivoConnector.__new__(AtivoConnector)


# --- parse (campos estruturais) -------------------------------------------

def test_parse_event():
    rec = _conn().parse(_payload({**_ITEM, "_pay_status": "open"}))
    assert rec is not None
    assert rec.name == "Corrida do Bem Eco - Marabá"       # entidade HTML resolvida
    assert rec.city == "Marabá"
    assert rec.state == "PA"
    assert rec.country == "BR"
    assert {d.distance_km for d in rec.distances} == {5.0, 10.0}
    assert rec.official_url == "https://pay.ativo.com/evento/40315"
    assert rec.registration_status == RegistrationStatus.OPEN


def test_valid_image_passes_through():
    rec = _conn().parse(_payload(_ITEM))
    assert rec.image_url == "https://media.ativo.com/upload/evento/40315/img_logo_evento.jpg"


def test_corrupted_image_extension_becomes_none():
    """Extensao corrompida no dump ('.çpo', '.alq'...) -> sem imagem (link quebrado)."""
    for bad in (
        "https://media.ativo.com/upload/evento/40315/img_logo_evento.çpo",
        "https://media.ativo.com/upload/evento/40315/img_logo_evento.alq",
        "https://media.ativo.com/upload/evento/40315/img_logo_evento.pçw",
    ):
        item = {**_ITEM, "thumbnail": bad}
        rec = _conn().parse(_payload(item))
        assert rec.image_url is None, bad


def test_country_from_post_json_path():
    item = {
        **_ITEM,
        "post_json": "https://www.ativo.com/calendario/eventos/america-do-sul/uy/mo/punta/corrida-de-rua/1/x/index.json",
        "ds_estado": "MO",
    }
    rec = _conn().parse(_payload(item))
    assert rec.country == "UY"
    assert rec.state is None          # subdivisão estrangeira não é UF


def test_empty_distances_ok():
    item = {**_ITEM, "distancias": []}
    rec = _conn().parse(_payload(item))
    assert rec.distances == []


# --- status de inscricao ---------------------------------------------------

@pytest.mark.parametrize("pay,expected", [
    ("open", RegistrationStatus.OPEN),
    ("closed", RegistrationStatus.CLOSED),
    ("coming_soon", RegistrationStatus.COMING_SOON),
    ("sold_out", RegistrationStatus.SOLD_OUT),
])
def test_status_from_pay_signal(pay, expected):
    rec = _conn().parse(_payload({**_ITEM, "_pay_status": pay}))
    assert rec.registration_status == expected


def test_no_pay_signal_is_unknown():
    """Sem o sinal da pagina de inscricao (fora da janela / pagina ilegivel)
    o status e UNKNOWN — nao chutamos a partir do dump."""
    rec = _conn().parse(_payload(_ITEM))          # _ITEM nao tem _pay_status
    assert rec.registration_status == RegistrationStatus.UNKNOWN


def test_past_event_is_closed():
    item = {**_ITEM, "dt_evento": _PAST, "_pay_status": "open"}  # passado vence o sinal
    rec = _conn().parse(_payload(item))
    assert rec.registration_status == RegistrationStatus.CLOSED


def test_suspended_event_is_closed():
    item = {**_ITEM, "fl_suspenso": 1, "_pay_status": "open"}    # suspenso vence o sinal
    rec = _conn().parse(_payload(item))
    assert rec.registration_status == RegistrationStatus.CLOSED


def test_fl_resultado_does_not_close_future_event():
    """Regressao: fl_resultado=1 nao pode encerrar a inscricao de prova futura.
    Sem sinal da pagina, o status e UNKNOWN — mas nunca CLOSED por fl_resultado."""
    item = {**_ITEM, "fl_resultado": "1", "dt_evento": _FUTURE}
    rec = _conn().parse(_payload(item))
    assert rec.registration_status != RegistrationStatus.CLOSED
    assert rec.registration_status == RegistrationStatus.UNKNOWN


# --- classificador da pagina de inscricao ----------------------------------

@pytest.mark.parametrize("html,expected", [
    ("<h2>Inscri&ccedil;&otilde;es encerradas!</h2>", "closed"),
    ("ATENÇÃO: Inscrições encerradas para público geral!", "closed"),
    ("<h2>Inscrições em breve...</h2>", "coming_soon"),
    ("Escolha do Kit ... Quero este Kit R$ 115,00", "open"),
    # pagina de esgotado tambem diz "encerradas" -> sold_out tem prioridade
    ("Inscrições ENCERRADAS. Todas as vagas preenchidas devido à alta demanda.", "sold_out"),
    ("<html><body>pagina inesperada</body></html>", None),
])
def test_classify_pay_page(html, expected):
    assert _classify_pay_page(html) == expected


# --- fetch: janela e anexo do _pay_status ----------------------------------

class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text


def test_fetch_attaches_pay_status_within_window(monkeypatch):
    c = _conn()
    c._items = {"40315": _ITEM}
    monkeypatch.setattr(c, "http_get", lambda url: _Resp("<h2>Inscrições encerradas!</h2>"))
    payload = c.fetch("40315")
    assert json.loads(payload.body)["_pay_status"] == "closed"
    assert c.parse(payload).registration_status == RegistrationStatus.CLOSED


def test_fetch_skips_pay_page_out_of_window(monkeypatch):
    c = _conn()
    c._items = {"40315": {**_ITEM, "dt_evento": _FAR}}

    def boom(url):
        raise AssertionError("nao deve buscar a pagina de inscricao fora da janela")

    monkeypatch.setattr(c, "http_get", boom)
    payload = c.fetch("40315")
    assert json.loads(payload.body)["_pay_status"] is None
    assert c.parse(payload).registration_status == RegistrationStatus.UNKNOWN


def test_fetch_tolerates_pay_page_error(monkeypatch):
    c = _conn()
    c._items = {"40315": _ITEM}

    def boom(url):
        raise RuntimeError("rede indisponivel")

    monkeypatch.setattr(c, "http_get", boom)
    payload = c.fetch("40315")                    # nao propaga o erro
    assert json.loads(payload.body)["_pay_status"] is None
    assert c.parse(payload).registration_status == RegistrationStatus.UNKNOWN


def test_fetch_classifies_non_2xx_body(monkeypatch):
    """pay.ativo.com serve a pagina de 'esgotado' com HTTP 418; o corpo ainda
    traz o sinal e deve ser classificado (nao virar UNKNOWN)."""
    c = _conn()
    c._items = {"40315": _ITEM}
    req = httpx.Request("GET", "https://pay.ativo.com/evento/40315")
    resp = httpx.Response(418, request=req,
                          text="Inscrições ENCERRADAS. Vagas preenchidas, alta demanda.")

    def raise_418(url):
        raise httpx.HTTPStatusError("418", request=req, response=resp)

    monkeypatch.setattr(c, "http_get", raise_418)
    payload = c.fetch("40315")
    assert json.loads(payload.body)["_pay_status"] == "sold_out"
    assert c.parse(payload).registration_status == RegistrationStatus.SOLD_OUT
