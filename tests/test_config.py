from app.agents.chat_agent import make_checkpointer, pg_conninfo
from app.config import Settings


def _settings(**kw) -> Settings:
    base = {"database_url": "", "db_socket": "", "db_name": "", "db_user": "", "db_password": ""}
    return Settings(_env_file=None, **{**base, **kw})


def test_db_url_defaults_to_local_sqlite():
    assert _settings().db_url == "sqlite:///./triage.db"


def test_db_url_explicit_wins():
    url = "postgresql+psycopg://u:p@127.0.0.1:5432/triage"
    assert _settings(database_url=url, db_socket="/cloudsql/x").db_url == url


def test_db_url_cloud_sql_socket_escapes_password():
    url = _settings(db_socket="/cloudsql/proj:region:inst", db_name="triage", db_user="triage",
                    db_password="p@ss/w:rd?").db_url
    assert url.startswith("postgresql+psycopg://triage:")
    assert "p@ss/w:rd?" not in url  # special characters are percent-encoded
    assert url.endswith("/triage?host=%2Fcloudsql%2Fproj%3Aregion%3Ainst")


def test_pg_conninfo_for_psycopg():
    info = pg_conninfo("postgresql+psycopg://triage:p%40ss@/triage?host=%2Fcloudsql%2Fproj%3Ar%3Ai")
    assert info.startswith("postgresql://triage:p%40ss@/triage?host=")


def test_sqlite_checkpointer(tmp_path):
    from langgraph.checkpoint.sqlite import SqliteSaver

    assert isinstance(make_checkpointer(f"sqlite:///{tmp_path / 'x.db'}"), SqliteSaver)
