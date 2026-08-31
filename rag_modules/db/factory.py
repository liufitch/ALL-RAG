from rag_modules.config.settings import settings


def get_database_type() -> str:
    return settings.database_type
