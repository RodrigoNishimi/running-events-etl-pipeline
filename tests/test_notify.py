from corridas_etl.pipeline.notify import _price_message, _status_message


def test_status_open_message():
    assert _status_message("Corrida X", "closed", "open") == "Inscrições abriram para Corrida X"
    assert _status_message("Corrida X", "sold_out", "open").startswith("Vagas liberadas")


def test_status_sold_out_message():
    assert _status_message("Corrida X", "open", "sold_out") == "Corrida X esgotou"


def test_status_unknown_transition_is_generic():
    msg = _status_message("Corrida X", "coming_soon", "closed")
    assert "Corrida X" in msg and "coming_soon" in msg and "closed" in msg


def test_price_drop_and_rise():
    assert _price_message("X", "239.90", "199.90") == "Preço de X caiu de R$ 239.90 para R$ 199.90"
    assert _price_message("X", "199.90", "259.90") == "Preço de X subiu de R$ 199.90 para R$ 259.90"
