"""One-shot container initialization."""
from app.cli import migrate, seed
from app.settings import get_settings

if __name__ == "__main__":
    settings = get_settings()
    migrate(settings)
    seed(settings)
    print("Корсик: база данных подготовлена")
