"""Тесты для MetricsRegistry."""
from app.services.metrics import MetricsRegistry


def test_counter_increment():
    m = MetricsRegistry()
    m.inc("foo_total")
    m.inc("foo_total")
    out = m.render()
    assert "foo_total 2.0" in out


def test_counter_with_labels():
    m = MetricsRegistry()
    m.inc("hits", model="opus", source="file")
    m.inc("hits", model="opus", source="file")
    m.inc("hits", model="haiku", source="file")
    out = m.render()
    assert 'hits{model="opus",source="file"} 2.0' in out
    assert 'hits{model="haiku",source="file"} 1.0' in out


def test_label_escaping():
    m = MetricsRegistry()
    m.inc("test", path='C:\\path\\with"quote')
    out = m.render()
    # Backslash і кавички мають бути екрановані
    assert 'C:\\\\path\\\\with\\"quote' in out


def test_duration_summary():
    m = MetricsRegistry()
    m.observe_duration("op_seconds", 1.5, model="medium")
    m.observe_duration("op_seconds", 2.5, model="medium")
    out = m.render()
    assert 'op_seconds_sum{model="medium"} 4.0' in out
    assert 'op_seconds_count{model="medium"} 2' in out


def test_gauges_rendered():
    m = MetricsRegistry()
    out = m.render(gauges={"foo_gauge": 42})
    assert "# TYPE foo_gauge gauge" in out
    assert "foo_gauge 42" in out


def test_prometheus_format():
    """Smoke: вихід має валідні HELP/TYPE заголовки."""
    m = MetricsRegistry()
    m.inc("requests_total", method="GET")
    m.observe_duration("latency_seconds", 0.1, route="/foo")
    out = m.render()
    lines = out.split("\n")
    help_lines = [l for l in lines if l.startswith("# HELP")]
    type_lines = [l for l in lines if l.startswith("# TYPE")]
    assert len(help_lines) >= 2
    assert len(type_lines) >= 2
