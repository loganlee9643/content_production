import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from config import get_settings


def ensure_local_database():
    settings = get_settings()
    admin_url = settings.admin_dsn
    if not admin_url:
        return

    admin = conninfo_to_dict(admin_url)
    app = conninfo_to_dict(settings.database_dsn)
    for connection in (admin, app):
        if connection.get("host") not in {"localhost", "127.0.0.1", "::1"}:
            raise RuntimeError("자동 DB 초기화는 로컬 PostgreSQL 연결에만 사용할 수 있습니다.")
    if admin["host"] != app["host"] or admin.get("port", "5432") != app.get("port", "5432"):
        raise RuntimeError("관리자와 앱의 PostgreSQL 호스트 및 포트를 동일하게 설정해 주세요.")
    if app.get("user") != "shortform" or app.get("dbname") != "shortform":
        raise RuntimeError("자동 초기화 대상은 shortform 역할과 shortform DB입니다.")

    password = app.get("password")
    if not password:
        raise RuntimeError("config.yaml의 사용자 계정에 개발용 비밀번호를 설정해 주세요.")

    try:
        with psycopg.connect(admin_url, autocommit=True, connect_timeout=5) as db:
            db.execute("SELECT pg_advisory_lock(716480271)")
            if db.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", ("shortform",)).fetchone() is None:
                db.execute(sql.SQL("CREATE ROLE shortform LOGIN PASSWORD {}").format(sql.Literal(password)))
            if db.execute("SELECT 1 FROM pg_database WHERE datname = %s", ("shortform",)).fetchone() is None:
                db.execute("CREATE DATABASE shortform OWNER shortform")
    except psycopg.Error as error:
        raise RuntimeError("로컬 PostgreSQL 초기화 실패. 관리자 접속 정보와 역할·DB 생성 권한을 확인해 주세요.") from None
