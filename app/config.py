import os

class Settings:
    def __init__(self):
        raw_db = os.getenv("DATABASE_URL", "")
        if raw_db.startswith("postgres://"):
            raw_db = raw_db.replace("postgres://", "postgresql+asyncpg://", 1)
        elif raw_db.startswith("postgresql://") and "+asyncpg" not in raw_db:
            raw_db = raw_db.replace("postgresql://", "postgresql+asyncpg://", 1)
        self.database_url: str = raw_db
        self.motherbot_token: str = os.getenv("MOTHERBOT_TOKEN", "")
        self.extella_token: str = os.getenv("EXTELLA_TOKEN", "")
        self.secret_key: str = os.getenv("SECRET_KEY", "dev_secret_key_CHANGE_ME")
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
        self.fal_api_key: str = os.getenv("FAL_API_KEY", "")
        self.debug: bool = os.getenv("DEBUG", "false").lower() == "true"
        rd = os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
        self.railway_url = f"https://{rd}" if rd else os.getenv("RAILWAY_URL", "")

settings = Settings()
