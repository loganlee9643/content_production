from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from psycopg.conninfo import make_conninfo


CONFIG_PATH = Path(__file__).with_name("config.yaml")


@dataclass(frozen=True, repr=False)
class Settings:
    database_dsn: str
    admin_dsn: str | None
    collection_interval_hours: int


def connection_info(section, name, *, default_database, default_user, host="127.0.0.1", port=5432):
    if not isinstance(section, dict):
        raise ValueError(f"{name}은 YAML 매핑이어야 합니다.")
    database = section.get("name", default_database)
    user = section.get("user", default_user)
    password = section.get("password")
    if not all(isinstance(value, str) and value.strip() for value in (host, database, user, password)):
        raise ValueError(f"{name}의 host, name, user, password는 비어 있지 않은 문자열이어야 합니다.")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError(f"{name}.port는 1~65535 범위의 정수여야 합니다.")
    return make_conninfo(
        host=host, port=port, dbname=database, user=user, password=password,
    )


@lru_cache(maxsize=1)
def get_settings():
    try:
        data = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise RuntimeError("apps/backend/config.example.yaml을 config.yaml로 복사하고 DB 접속 정보를 입력해 주세요.") from None
    except yaml.YAMLError:
        raise RuntimeError("config.yaml의 YAML 형식을 확인해 주세요.") from None
    if not isinstance(data, dict):
        raise ValueError("config.yaml 최상위에는 database 설정을 입력해 주세요.")

    collection = data.get("collection", {})
    if not isinstance(collection, dict):
        raise ValueError("collection은 YAML 매핑이어야 합니다.")
    interval_hours = collection.get("interval_hours", 6)
    if type(interval_hours) is not int or not 1 <= interval_hours <= 168:
        raise ValueError("collection.interval_hours에는 1~168 범위의 정수를 입력해 주세요.")

    database_section = data.get("database")
    if not isinstance(database_section, dict):
        raise ValueError("database는 YAML 매핑이어야 합니다.")
    if isinstance(database_section.get("user"), dict) or "admin" in database_section or "bootstrap" in database_section:
        address = {
            "host": database_section.get("host", "127.0.0.1"),
            "port": database_section.get("port", 5432),
        }
        enabled = database_section.get("bootstrap", False)
        if type(enabled) is not bool:
            raise ValueError("database.bootstrap에는 true 또는 false를 입력해 주세요.")
        database = connection_info(
            database_section.get("user"), "database.user",
            default_database="shortform", default_user="shortform", **address,
        )
        admin = None
        if enabled:
            admin = connection_info(
                database_section.get("admin"), "database.admin",
                default_database="postgres", default_user="postgres", **address,
            )
        return Settings(
            database_dsn=database, admin_dsn=admin, collection_interval_hours=interval_hours,
        )

    database = connection_info(
        database_section, "database", default_database="shortform", default_user="shortform",
        host=database_section.get("host", "127.0.0.1"), port=database_section.get("port", 5432),
    )
    bootstrap = data.get("bootstrap", {})
    if not isinstance(bootstrap, dict) or type(bootstrap.get("enabled", False)) is not bool:
        raise ValueError("bootstrap.enabled에는 true 또는 false를 입력해 주세요.")
    admin = None
    if bootstrap.get("enabled", False):
        admin_section = bootstrap.get("admin")
        if not isinstance(admin_section, dict):
            raise ValueError("bootstrap.admin은 YAML 매핑이어야 합니다.")
        admin = connection_info(
            admin_section, "bootstrap.admin", default_database="postgres", default_user="postgres",
            host=admin_section.get("host", "127.0.0.1"), port=admin_section.get("port", 5432),
        )
    return Settings(
        database_dsn=database, admin_dsn=admin, collection_interval_hours=interval_hours,
    )
