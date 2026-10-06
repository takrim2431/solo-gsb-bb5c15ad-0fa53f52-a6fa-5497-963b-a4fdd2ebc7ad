"""应用配置：全部可通过环境变量覆盖。"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # PostgreSQL 连接串
    database_url: str = "postgresql://postgres:postgres@localhost:5432/calibration"

    # 连接池
    db_pool_min_size: int = 1
    db_pool_max_size: int = 10

    # 启动时等待数据库就绪的重试次数（每次间隔 1 秒）
    db_connect_retries: int = 30


settings = Settings()
