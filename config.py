from functools import lru_cache
#pydantic ek validation library hai
from pydantic_settings import BaseSettings,SettingsConfigDict
from langchain_google_genai import ChatGoogleGenerativeAI

class Settings(BaseSettings):
    gemini_api_key:str
    gemini_model:str="gemini-2.5-flash"
    database_url:str

    # model_config is a special class attribute recognised by Pydantic.
    model_config=SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

#Caching means storing the result of an operation so that you can reuse it instead of performing the same operation repeatedly.
@lru_cache
def get_settings()->Settings:
    """Reads .env once and caches the Settings object in memory."""
    return Settings()

# def get_llm() -> ChatGoogleGenerativeAI:
#     """Creates a configured Gemini client for LangChain/LangGraph nodes."""
#     settings=get_settings()
#     return ChatGoogleGenerativeAI(
#         model=settings.gemini_model,
#         google_api_key=settings.gemini_api_key,
#         temperature=0,
#     )