from datetime import datetime

from corridas_etl.connectors.yescom import YescomConnector
from corridas_etl.models import RawPayload
from corridas_etl.utils.distances import HALF_MARATHON_KM, MARATHON_KM

# Fixture minima no estilo dos microsites reais (capturado 2026-07-19).
_HTML = """
<html><head><title>31&ordf; Maratona Internacional de S&atilde;o Paulo 2027</title></head>
<body>
  <h1>MARATONA SP 2027</h1>
  <p>A prova sera realizada em 03 de Abril de 2027, com largada na Praca Charles Miller.</p>
  <p>Retirada de kit: 01 de abril e 02 de abril.</p>
  <p>Data oficial: 03 de Abril de 2027.</p>
</body></html>
"""


def _payload(html: str, url: str) -> RawPayload:
    return RawPayload(
        source="yescom",
        source_event_id="maratonasp/2027",
        source_url=url,
        fetched_at=datetime(2026, 7, 19),
        body=html,
    )


def _conn() -> YescomConnector:
    return YescomConnector.__new__(YescomConnector)


def test_parse_microsite():
    rec = _conn().parse(_payload(_HTML, "https://www.yescom.com.br/maratonasp/2027/index.asp"))
    assert rec is not None
    assert rec.name == "31ª Maratona Internacional de São Paulo 2027"
    assert rec.organizer_name == "Yescom"
    # data = moda das datas dentro do ano do URL (2027); datas de kit perdem
    assert rec.start_at is not None and rec.start_at.date() == datetime(2027, 4, 3).date()
    # local via hint de slug "maratonasp"
    assert (rec.city, rec.state) == ("São Paulo", "SP")
    # "Maratona" (sem "meia") no titulo -> 42.195
    assert rec.distances[0].distance_km == MARATHON_KM


def test_meia_beats_maratona_in_name():
    html = "<html><head><title>20ª Meia Maratona Internacional de São Paulo 2026</title></head><body></body></html>"
    rec = _conn().parse(_payload(html, "https://www.yescom.com.br/meiasp/2026/index.asp"))
    assert rec.distances[0].distance_km == HALF_MARATHON_KM


def test_no_date_when_year_missing_from_text():
    html = "<html><head><title>Corrida X</title></head><body>sem datas aqui</body></html>"
    rec = _conn().parse(_payload(html, "https://www.yescom.com.br/corridax/2026/index.asp"))
    assert rec.start_at is None
